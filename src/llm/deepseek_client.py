"""DeepSeek client wrapper with retry and JSON extraction helpers."""

import json
import logging
import re
import httpx
from pathlib import Path

from openai import AsyncOpenAI, APIConnectionError, APIError, APITimeoutError
from tenacity import before_sleep_log, retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "config" / "prompts"
ANALYSIS_TONE_NOTE = (
    "\n\n附加要求：日报读者主要是产品、战略和技术同学。"
    "涉及职业替代、岗位变化、组织冲击等敏感判断时，"
    "请保持措辞克制、尊重读者感受，优先陈述事实和具体变化，"
    "避免做刺激性、武断或带情绪的表达。"
)


def _is_retryable_error(exception: Exception) -> bool:
    """Retry network timeouts, connection errors, 429s, and 5xx responses."""
    if isinstance(exception, (APITimeoutError, APIConnectionError, httpx.TransportError)):
        return True

    if isinstance(exception, APIError):
        status_code = getattr(exception, "status_code", None)
        if status_code and (status_code == 429 or status_code >= 500):
            return True

    return False


def _extract_json(raw: str):
    """Extract a JSON array/object from raw model output."""
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if match:
            raw = match.group(1).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    for start_char in ("[", "{"):
        idx = raw.find(start_char)
        if idx < 0:
            continue

        candidate = raw[idx:]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        end_char = "]" if start_char == "[" else "}"
        depth = 0
        in_str = False
        escape = False

        for i, char in enumerate(candidate):
            if escape:
                escape = False
                continue
            if char == "\\" and in_str:
                escape = True
                continue
            if char == '"':
                in_str = not in_str
                continue
            if not in_str:
                if char == start_char:
                    depth += 1
                elif char == end_char:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(candidate[: i + 1])
                        except json.JSONDecodeError:
                            break

    return None


def _failed_screening_items(articles: list[dict], route_name: str, reason: str) -> list[dict]:
    """Keep failed screening batches visible in downstream sheets."""
    failed_items: list[dict] = []
    for article in articles:
        failed_items.append(
            {
                "url": article.get("url", ""),
                "title": article.get("title", ""),
                "tag": "初筛失败",
                "score": 0,
                "summary": f"LLM 初筛失败，待重试（{reason}）",
                "growth_mechanism": "",
                "actionable_angle": "",
                "confidence": "low",
                "screening_status": "failed",
                "screening_error": reason,
                "failed_route": route_name,
                "source": article.get("source", ""),
                "published_time": article.get("published_time", ""),
                "category": article.get("category", ""),
            }
        )
    return failed_items


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        screening_model: str,
        analysis_model: str,
        analysis_reasoning_effort: str = "high",
        screening_max_tokens: int = 8192,
        analysis_max_tokens: int = 16384,
        timeout_seconds: float = 240,
    ):
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
        )
        self.screening_model = screening_model
        self.analysis_model = analysis_model
        self.analysis_reasoning_effort = analysis_reasoning_effort
        self.screening_max_tokens = screening_max_tokens
        self.analysis_max_tokens = analysis_max_tokens
        self.timeout_seconds = timeout_seconds
        self._screening_prompt = (PROMPTS_DIR / "screening.txt").read_text(encoding="utf-8")
        self._startup_screening_prompt = (PROMPTS_DIR / "screening_startup.txt").read_text(encoding="utf-8")
        self._analysis_prompt = (
            (PROMPTS_DIR / "analysis.txt").read_text(encoding="utf-8") + ANALYSIS_TONE_NOTE
        )

    @staticmethod
    def _thinking_body(*, disabled: bool) -> dict:
        return {"thinking": {"type": "disabled" if disabled else "enabled"}}

    async def _collect_stream_text(self, **kwargs) -> str:
        stream = await self._client.chat.completions.create(**kwargs)
        parts: list[str] = []
        async for chunk in stream:
            if not chunk.choices:
                continue
            content = getattr(chunk.choices[0].delta, "content", None)
            if content:
                parts.append(content)
        return "".join(parts).strip()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _call_screening_api(self, messages: list[dict]) -> str:
        return await self._collect_stream_text(
            model=self.screening_model,
            messages=messages,
            max_tokens=self.screening_max_tokens,
            stream=True,
            timeout=self.timeout_seconds,
            extra_body=self._thinking_body(disabled=True),
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _call_analysis_api(self, messages: list[dict]) -> str:
        return await self._collect_stream_text(
            model=self.analysis_model,
            messages=messages,
            max_tokens=self.analysis_max_tokens,
            stream=True,
            timeout=self.timeout_seconds,
            reasoning_effort=self.analysis_reasoning_effort,
            response_format={"type": "json_object"},
            extra_body=self._thinking_body(disabled=False),
        )

    async def _screen_articles_with_prompt(
        self,
        articles: list[dict],
        prompt: str,
        route_name: str,
    ) -> list[dict]:
        if not articles:
            return []

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(articles, ensure_ascii=False, indent=2)},
        ]
        logger.info(
            "Calling DeepSeek %s screening for %d articles with model %s",
            route_name,
            len(articles),
            self.screening_model,
        )

        try:
            raw = await self._call_screening_api(messages)
        except Exception as exc:
            logger.error("%s screening API failed after retries: %s", route_name, exc)
            return _failed_screening_items(articles, route_name, str(exc))

        result = _extract_json(raw)
        if result is None:
            logger.error("Failed to parse %s screening output: %s", route_name, raw[:500])
            return _failed_screening_items(articles, route_name, "parse_error")

        if not isinstance(result, list):
            logger.error("Unexpected %s screening output type: %s", route_name, type(result).__name__)
            return _failed_screening_items(articles, route_name, "unexpected_output_type")

        logger.info("%s screening returned %d rows", route_name, len(result))
        if (len(result) != len(articles)
                or any(not isinstance(item, dict) for item in result)
                or {item.get('url') for item in result} != {item.get('url') for item in articles}):
            logger.error("%s screening returned %d rows for %d articles", route_name, len(result), len(articles))
            return _failed_screening_items(articles, route_name, "incomplete_output")
        return result

    async def screen_articles(self, articles: list[dict]) -> list[dict]:
        return await self._screen_articles_with_prompt(
            articles,
            self._screening_prompt,
            route_name="legacy",
        )

    async def screen_startup_articles(self, articles: list[dict]) -> list[dict]:
        return await self._screen_articles_with_prompt(
            articles,
            self._startup_screening_prompt,
            route_name="startup",
        )

    async def analyze_articles(self, screened: list[dict], date_str: str) -> dict:
        if not screened:
            return {"date": date_str, "core_arguments": [], "source_links": []}

        enriched_input = []
        for item in screened:
            enriched_input.append(
                {
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "tag": item.get("tag", ""),
                    "summary": item.get("summary", ""),
                    "original_content": item.get("original_content", "")[:2000],
                }
            )

        messages = [
            {"role": "system", "content": self._analysis_prompt},
            {
                "role": "user",
                "content": f"今日日期：{date_str}\n\n" + json.dumps(enriched_input, ensure_ascii=False, indent=2),
            },
        ]
        logger.info("Calling DeepSeek analysis for %d articles with model %s", len(screened), self.analysis_model)

        try:
            raw = await self._call_analysis_api(messages)
        except Exception as exc:
            logger.error("Analysis API failed after retries: %s", exc)
            raise RuntimeError("Analysis API failed") from exc

        result = _extract_json(raw)
        if not isinstance(result, dict) or not isinstance(result.get("core_arguments"), list):
            logger.error("Failed to parse analysis output: %s", raw[:500])
            raise RuntimeError("Analysis returned invalid JSON shape")

        if not result['core_arguments']:
            raise RuntimeError("Analysis returned no arguments for selected articles")
        allowed_urls = {item.get('url') for item in screened}
        for argument in result['core_arguments']:
            if not isinstance(argument, dict) or not argument.get('argument') or not argument.get('sub_points') or not argument.get('sources'):
                raise RuntimeError("Analysis returned an incomplete argument")
            if any(source.get('url') not in allowed_urls for source in argument['sources']):
                raise RuntimeError("Analysis returned an unknown source URL")

        logger.info("Analysis produced %d core arguments", len(result.get("core_arguments", [])))
        return result

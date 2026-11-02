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

    @staticmethod
    def _add_article_ids(articles: list[dict]) -> list[dict]:
        """Add short stable IDs so model output never needs to be joined by URL."""
        payload: list[dict] = []
        for index, article in enumerate(articles, start=1):
            item = dict(article)
            item["article_id"] = f"article-{index:03d}"
            payload.append(item)
        return payload

    @staticmethod
    def _parse_screening_rows(
        raw: str,
        payload: list[dict],
        route_name: str,
    ) -> tuple[dict[str, dict], list[str]]:
        """Salvage valid rows and return IDs that still need an individual retry."""
        result = _extract_json(raw)
        expected_by_id = {item["article_id"]: item for item in payload}
        expected_id_by_url = {item.get("url", ""): item["article_id"] for item in payload}

        if not isinstance(result, list):
            logger.error(
                "%s screening returned %s instead of a JSON array",
                route_name,
                type(result).__name__ if result is not None else "unparseable output",
            )
            return {}, list(expected_by_id)

        matched: dict[str, dict] = {}
        invalid_rows = 0
        unknown_references: list[str] = []

        for item in result:
            if not isinstance(item, dict):
                invalid_rows += 1
                continue

            article_id = str(item.get("article_id", "")).strip()
            if article_id not in expected_by_id or article_id in matched:
                article_id = expected_id_by_url.get(item.get("url", ""), "")

            if not article_id or article_id in matched:
                unknown_references.append(
                    str(item.get("article_id") or item.get("url") or "<missing reference>")
                )
                continue

            original = expected_by_id[article_id]
            normalized = dict(item)
            normalized.pop("article_id", None)
            # The model judges content; source identity always comes from trusted RSS input.
            normalized["url"] = original.get("url", "")
            normalized["title"] = original.get("title", "")
            matched[article_id] = normalized

        missing_ids = [article_id for article_id in expected_by_id if article_id not in matched]
        if missing_ids or invalid_rows or unknown_references or len(result) != len(payload):
            logger.warning(
                "%s screening mismatch: input=%d output=%d matched=%d missing_ids=%s "
                "invalid_rows=%d unknown_references=%s",
                route_name,
                len(payload),
                len(result),
                len(matched),
                missing_ids,
                invalid_rows,
                unknown_references[:5],
            )
        else:
            logger.info("%s screening returned %d valid rows", route_name, len(matched))

        return matched, missing_ids

    async def _request_screening_rows(
        self,
        payload: list[dict],
        prompt: str,
        route_name: str,
    ) -> tuple[dict[str, dict], list[str]]:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ]
        raw = await self._call_screening_api(messages)
        return self._parse_screening_rows(raw, payload, route_name)

    async def _screen_articles_with_prompt(
        self,
        articles: list[dict],
        prompt: str,
        route_name: str,
    ) -> list[dict]:
        if not articles:
            return []

        payload = self._add_article_ids(articles)
        logger.info(
            "Calling DeepSeek %s screening for %d articles with model %s",
            route_name,
            len(articles),
            self.screening_model,
        )

        try:
            matched, missing_ids = await self._request_screening_rows(payload, prompt, route_name)
        except Exception as exc:
            logger.error("%s screening API failed after retries: %s", route_name, exc)
            return _failed_screening_items(articles, route_name, str(exc))

        if missing_ids:
            logger.warning(
                "%s screening will retry %d missing/invalid articles individually: %s",
                route_name,
                len(missing_ids),
                missing_ids,
            )

        failed_by_id: dict[str, dict] = {}
        payload_by_id = {item["article_id"]: item for item in payload}
        for article_id in missing_ids:
            retry_payload = [payload_by_id[article_id]]
            try:
                retry_matched, retry_missing = await self._request_screening_rows(
                    retry_payload,
                    prompt,
                    f"{route_name}:retry:{article_id}",
                )
            except Exception as exc:
                logger.error("%s individual retry failed: %s", article_id, exc)
                failed_by_id[article_id] = _failed_screening_items(
                    [payload_by_id[article_id]], route_name, str(exc)
                )[0]
                continue

            if article_id in retry_matched and not retry_missing:
                matched[article_id] = retry_matched[article_id]
            else:
                failed_by_id[article_id] = _failed_screening_items(
                    [payload_by_id[article_id]], route_name, "incomplete_output_after_retry"
                )[0]

        ordered_results: list[dict] = []
        for item in payload:
            article_id = item["article_id"]
            ordered_results.append(matched.get(article_id) or failed_by_id[article_id])
        return ordered_results

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

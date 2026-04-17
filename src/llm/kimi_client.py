"""Moonshot/Kimi client wrapper with retry and JSON extraction helpers."""

import json
import logging
import re
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
    if isinstance(exception, (APITimeoutError, APIConnectionError)):
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


class KimiClient:
    def __init__(self, api_key: str, base_url: str, screening_model: str, analysis_model: str):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.screening_model = screening_model
        self.analysis_model = analysis_model
        self._screening_prompt = (PROMPTS_DIR / "screening.txt").read_text(encoding="utf-8")
        self._analysis_prompt = (
            (PROMPTS_DIR / "analysis.txt").read_text(encoding="utf-8") + ANALYSIS_TONE_NOTE
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _call_screening_api(self, messages: list[dict]) -> str:
        response = await self._client.chat.completions.create(
            model=self.screening_model,
            messages=messages,
            temperature=1,
        )
        return response.choices[0].message.content.strip()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _call_analysis_api(self, messages: list[dict]) -> str:
        response = await self._client.chat.completions.create(
            model=self.analysis_model,
            messages=messages,
            temperature=1,
        )
        return response.choices[0].message.content.strip()

    async def screen_articles(self, articles: list[dict]) -> list[dict]:
        if not articles:
            return []

        messages = [
            {"role": "system", "content": self._screening_prompt},
            {"role": "user", "content": json.dumps(articles, ensure_ascii=False, indent=2)},
        ]
        logger.info("Calling Kimi screening for %d articles with model %s", len(articles), self.screening_model)

        try:
            raw = await self._call_screening_api(messages)
        except (APITimeoutError, APIConnectionError, APIError) as exc:
            logger.error("Screening API failed after retries: %s", exc)
            return []

        result = _extract_json(raw)
        if result is None:
            logger.error("Failed to parse screening output: %s", raw[:500])
            return []

        logger.info("Screening returned %d rows", len(result))
        return result

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
        logger.info("Calling Kimi analysis for %d articles with model %s", len(screened), self.analysis_model)

        try:
            raw = await self._call_analysis_api(messages)
        except (APITimeoutError, APIConnectionError, APIError) as exc:
            logger.error("Analysis API failed after retries: %s", exc)
            return {"date": date_str, "core_arguments": [], "source_links": []}

        result = _extract_json(raw)
        if result is None:
            logger.error("Failed to parse analysis output: %s", raw[:500])
            return {"date": date_str, "core_arguments": [], "source_links": []}

        logger.info("Analysis produced %d core arguments", len(result.get("core_arguments", [])))
        return result

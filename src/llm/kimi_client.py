"""
Moonshot/Kimi API 封装（兼容 OpenAI 接口）
带指数退避重试机制
"""
import json
import logging
import re
from pathlib import Path
from openai import AsyncOpenAI, APIError, APITimeoutError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "config" / "prompts"


def _is_retryable_error(exception):
    """判断错误是否可重试（网络超时或 5xx 服务器错误）。"""
    if isinstance(exception, APITimeoutError):
        return True
    if isinstance(exception, APIError):
        # 5xx 服务器错误或 429 限频
        if exception.status_code and (exception.status_code >= 500 or exception.status_code == 429):
            return True
    return False


def _extract_json(raw: str):
    """
    从 LLM 响应中提取 JSON，处理以下情况：
    - markdown 代码块（```json ... ```）
    - 前置警告文本（模型在 JSON 前输出文字说明）
    - 后置文本
    """
    # 1. 去除 markdown 代码块
    if "```" in raw:
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if m:
            raw = m.group(1).strip()

    # 2. 尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 3. 找到第一个 [ 或 { 从该位置开始解析（处理前置警告文字）
    for start_char in ('[', '{'):
        idx = raw.find(start_char)
        if idx < 0:
            continue
        candidate = raw[idx:]
        # 先直接尝试
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # 用括号计数找到匹配的结束位置，处理后置文本
        end_char = ']' if start_char == '[' else '}'
        depth = 0
        in_str = False
        escape = False
        for i, c in enumerate(candidate):
            if escape:
                escape = False
                continue
            if c == '\\' and in_str:
                escape = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if not in_str:
                if c == start_char:
                    depth += 1
                elif c == end_char:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(candidate[:i + 1])
                        except json.JSONDecodeError:
                            break
    return None


class KimiClient:
    def __init__(self, api_key: str, base_url: str, screening_model: str, analysis_model: str):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.screening_model = screening_model
        self.analysis_model = analysis_model

        self._screening_prompt = (PROMPTS_DIR / "screening.txt").read_text(encoding="utf-8")
        self._analysis_prompt = (PROMPTS_DIR / "analysis.txt").read_text(encoding="utf-8")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _call_screening_api(self, messages: list[dict]) -> str:
        """调用初筛 API，带重试机制。"""
        response = await self._client.chat.completions.create(
            model=self.screening_model,
            messages=messages,
            temperature=1,  # kimi-k2.5 只支持 temperature=1
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
        """调用深度分析 API，带重试机制。"""
        response = await self._client.chat.completions.create(
            model=self.analysis_model,
            messages=messages,
            temperature=1,  # kimi-k2.5 只支持 temperature=1
        )
        return response.choices[0].message.content.strip()

    async def screen_articles(self, articles: list[dict]) -> list[dict]:
        """
        使用 Kimi K 2.0 初筛文章列表，返回结构化 JSON。
        articles: 原始文章列表，每条包含 source/title/content/url/published_time/category
        """
        if not articles:
            return []

        user_content = json.dumps(articles, ensure_ascii=False, indent=2)
        messages = [
            {"role": "system", "content": self._screening_prompt},
            {"role": "user", "content": user_content},
        ]

        logger.info("调用 Kimi 初筛，文章数: %d，模型: %s", len(articles), self.screening_model)
        
        try:
            raw = await self._call_screening_api(messages)
        except (APITimeoutError, APIError) as e:
            logger.error("初筛 API 调用失败（已重试3次）: %s", e)
            return []

        result = _extract_json(raw)
        if result is None:
            logger.error("初筛输出解析失败，原始内容: %s", raw[:500])
            result = []

        logger.info("初筛完成，保留文章数: %d", len(result))
        return result

    async def analyze_articles(self, screened: list[dict], date_str: str) -> dict:
        """
        使用 Kimi 2.5 对初筛后的文章做深度分析，返回结构化 JSON。
        screened: 初筛后的文章列表，每条包含 url/title/summary/tag/original_content
        返回: {"date": str, "core_arguments": [...], "source_links": [...]}
        """
        if not screened:
            return {"date": date_str, "core_arguments": [], "source_links": []}

        # 构建输入：每条新闻包含初筛摘要 + 原文内容（用于深度分析时参考细节）
        enriched_input = []
        for item in screened:
            enriched_item = {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "tag": item.get("tag", ""),
                "summary": item.get("summary", ""),
                "original_content": item.get("original_content", "")[:2000],  # 原文截断到2000字，防token爆炸
            }
            enriched_input.append(enriched_item)

        system = self._analysis_prompt
        # 在 user message 开头加日期
        user_content = f"今日日期：{date_str}\n\n" + json.dumps(enriched_input, ensure_ascii=False, indent=2)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

        logger.info("调用 Kimi 深度分析，文章数: %d，模型: %s", len(screened), self.analysis_model)
        
        try:
            raw = await self._call_analysis_api(messages)
        except (APITimeoutError, APIError) as e:
            logger.error("深度分析 API 调用失败（已重试3次）: %s", e)
            return {"date": date_str, "core_arguments": [], "source_links": []}

        result = _extract_json(raw)
        if result is None:
            logger.error("深度分析输出解析失败: %s", raw[:500])
            result = {"date": date_str, "core_arguments": [], "source_links": []}

        logger.info("深度分析完成，核心论点数: %d", len(result.get("core_arguments", [])))
        return result

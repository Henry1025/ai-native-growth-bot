"""
调用 Kimi K2.6 初筛文章，输出结构化 JSON
支持分批处理，防止单次请求 token 过多
"""
import asyncio
import logging
import re

from ..llm.kimi_client import KimiClient

logger = logging.getLogger(__name__)


def _batch(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def normalize_tag(tag: str | None) -> str:
    """Normalize model tag output for reliable relevant/irrelevant checks."""
    tag = tag or ""
    tag = re.sub(r"[\u200b\u200c\u200d\ufeff\s]+", "", tag)
    return tag.strip("\"“”")


def get_score(item: dict | None) -> int:
    """Return model relevance score, with backward-compatible defaults."""
    if not item:
        return 1

    raw_score = item.get("score")
    try:
        score = int(raw_score)
    except (TypeError, ValueError):
        score = 4 if not normalize_tag(item.get("tag")).startswith("不相关") else 1

    return max(1, min(score, 5))


def is_relevant(item: dict) -> bool:
    """Candidate relevance for the screening sheet: score >= 3."""
    return get_score(item) >= 3 and not normalize_tag(item.get("tag")).startswith("不相关")


def is_report_relevant(item: dict) -> bool:
    """Daily-report relevance: stricter than sheet candidate relevance."""
    return get_score(item) >= 4 and is_relevant(item)


def is_screening_failed(item: dict | None) -> bool:
    """True when an LLM screening batch failed and needs retry."""
    return bool(item) and item.get("screening_status") == "failed"


def _mark_route(item: dict, route: str) -> dict:
    marked = dict(item)
    marked["screening_route"] = route
    return marked


def _merge_screening_results(legacy: list[dict], startup: list[dict]) -> list[dict]:
    """OR-merge legacy news screening with startup scout screening by URL."""
    startup_by_url = {item.get("url", ""): item for item in startup if isinstance(item, dict)}
    seen_urls: set[str] = set()
    merged: list[dict] = []

    for legacy_item in legacy:
        if not isinstance(legacy_item, dict):
            continue

        url = legacy_item.get("url", "")
        startup_item = startup_by_url.get(url)
        seen_urls.add(url)

        legacy_relevant = is_relevant(legacy_item)
        startup_relevant = is_relevant(startup_item) if startup_item else False
        legacy_failed = is_screening_failed(legacy_item)
        startup_failed = is_screening_failed(startup_item)

        if legacy_relevant and startup_relevant:
            chosen = startup_item if get_score(startup_item) >= get_score(legacy_item) else legacy_item
            route = "legacy+startup"
        elif startup_relevant:
            chosen = startup_item
            route = "startup"
        elif legacy_relevant:
            chosen = legacy_item
            route = "legacy"
        elif legacy_failed and startup_failed:
            chosen = legacy_item
            route = "legacy_failed+startup_failed"
        elif legacy_failed:
            chosen = legacy_item
            route = "legacy_failed"
        elif startup_failed:
            chosen = startup_item
            route = "startup_failed"
        else:
            chosen = legacy_item
            route = "none"

        merged.append(_mark_route(chosen, route))

    for url, startup_item in startup_by_url.items():
        if url in seen_urls:
            continue
        if is_relevant(startup_item):
            route = "startup"
        elif is_screening_failed(startup_item):
            route = "startup_failed"
        else:
            route = "none"
        merged.append(_mark_route(startup_item, route))

    return merged


def _attach_original_metadata(screened: list[dict], articles: list[dict]) -> list[dict]:
    """Attach RSS metadata that the model may omit from its JSON output."""
    article_by_url = {article.get("url", ""): article for article in articles if isinstance(article, dict)}
    enriched: list[dict] = []

    for item in screened:
        if not isinstance(item, dict):
            continue

        merged_item = dict(item)
        original = article_by_url.get(merged_item.get("url", ""))
        if original:
            for key in ("source", "published_time", "category"):
                if not merged_item.get(key):
                    merged_item[key] = original.get(key, "")
            if not merged_item.get("title"):
                merged_item["title"] = original.get("title", "")
        enriched.append(merged_item)

    return enriched


async def screen_articles(
    client: KimiClient,
    articles: list[dict],
    batch_size: int = 20,
) -> list[dict]:
    """
    分批调用 Kimi 初筛。

    初筛采用两条 OR 关系链路：
    1. legacy：原有 AI 增长/广告平台/组织/AI 公司增长逻辑
    2. startup：AI + Growth 创业公司补充召回

    只要任一链路判定相关，合并结果即保留为相关。
    """
    if not articles:
        return []

    batches = list(_batch(articles, batch_size))
    logger.info("开始双链路初筛，共 %d 篇，分 %d 批处理", len(articles), len(batches))

    # 限制并发数，防止触发 Moonshot API 限频
    semaphore = asyncio.Semaphore(2)

    async def _screen_legacy_with_limit(batch):
        async with semaphore:
            return await client.screen_articles(batch)

    async def _screen_startup_with_limit(batch):
        async with semaphore:
            return await client.screen_startup_articles(batch)

    legacy_results, startup_results = await asyncio.gather(
        asyncio.gather(*[_screen_legacy_with_limit(b) for b in batches]),
        asyncio.gather(*[_screen_startup_with_limit(b) for b in batches]),
    )
    legacy_screened = [item for batch_result in legacy_results for item in batch_result if isinstance(item, dict)]
    startup_screened = [item for batch_result in startup_results for item in batch_result if isinstance(item, dict)]
    screened = _merge_screening_results(legacy_screened, startup_screened)
    screened = _attach_original_metadata(screened, articles)

    logger.info(
        "双链路初筛完成，legacy=%d startup=%d merged=%d candidates=%d report_relevant=%d failed=%d",
        len(legacy_screened),
        len(startup_screened),
        len(screened),
        sum(1 for item in screened if is_relevant(item)),
        sum(1 for item in screened if is_report_relevant(item)),
        sum(1 for item in screened if is_screening_failed(item)),
    )
    return screened

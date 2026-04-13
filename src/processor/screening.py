"""
调用 Kimi K 2.0 初筛文章，输出结构化 JSON
支持分批处理，防止单次请求 token 过多
"""
import asyncio
import logging

from ..llm.kimi_client import KimiClient

logger = logging.getLogger(__name__)


def _batch(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


async def screen_articles(
    client: KimiClient,
    articles: list[dict],
    batch_size: int = 20,
) -> list[dict]:
    """
    分批调用 Kimi 初筛，汇总所有批次的结果。
    """
    if not articles:
        return []

    batches = list(_batch(articles, batch_size))
    logger.info("开始初筛，共 %d 篇，分 %d 批处理", len(articles), len(batches))

    # 限制并发数，防止触发 Moonshot API 限频
    semaphore = asyncio.Semaphore(3)

    async def _screen_with_limit(batch):
        async with semaphore:
            return await client.screen_articles(batch)

    results = await asyncio.gather(*[_screen_with_limit(b) for b in batches])
    screened = [item for batch_result in results for item in batch_result if isinstance(item, dict)]

    logger.info("初筛完成，保留 %d 篇", len(screened))
    return screened

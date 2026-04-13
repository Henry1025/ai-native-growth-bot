"""
标题相似度去重（difflib）
保留每组相似标题中发布时间最早（或无时间信息）的一条
"""
import logging
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.75  # 相似度阈值，超过此值视为重复


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def dedup_articles(articles: list[dict], threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
    """
    对文章列表按标题相似度去重。
    返回去重后的文章列表（保留代表性的一条）。
    """
    if not articles:
        return []

    kept: list[dict] = []
    for article in articles:
        title = article.get("title", "")
        is_dup = False
        for existing in kept:
            if _similarity(title, existing.get("title", "")) >= threshold:
                is_dup = True
                logger.debug("去重: 「%s」 ≈ 「%s」", title, existing["title"])
                break
        if not is_dup:
            kept.append(article)

    logger.info("去重完成: %d → %d 篇", len(articles), len(kept))
    return kept

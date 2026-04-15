"""
通用 RSS 抓取：feedparser 解析 + aiohttp 并行抓取全文
支持按时间窗口筛选文章
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import feedparser
import yaml

from .content_parser import parse_article_content

logger = logging.getLogger(__name__)

SOURCES_PATH = Path(__file__).parent.parent.parent / "config" / "rss_sources.yaml"

# feedparser 的时间结构体转 datetime
def _parse_time(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, attr, None)
        if t:
            import calendar
            # feedparser 的 *_parsed 是 UTC 的 struct_time，用 calendar.timegm 而不是 time.mktime
            ts = calendar.timegm(t)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    return None


def load_sources() -> list[dict]:
    """从 rss_sources.yaml 加载 RSS 源列表。"""
    data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8"))
    return data.get("sources") or []


async def _fetch_full_content(session: aiohttp.ClientSession, url: str, max_chars: int) -> str:
    """尝试抓取原文页面的正文。失败时返回空字符串。"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                html = await resp.text(errors="replace")
                return parse_article_content(html, max_chars)
    except Exception as e:
        logger.debug("抓取全文失败 %s: %s", url, e)
    return ""


async def fetch_source(
    session: aiohttp.ClientSession,
    source: dict,
    start_time: datetime,
    end_time: datetime | None,
    max_chars: int,
) -> list[dict]:
    """抓取单个 RSS 源，返回指定时间窗口内的文章列表。"""
    name = source["name"]
    url = source["url"]
    category = source.get("category", "")

    logger.info("正在抓取 RSS: %s (%s)", name, url)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            raw = await resp.read()
        feed = feedparser.parse(raw)
    except Exception as e:
        logger.warning("RSS 抓取失败 %s: %s", name, e)
        return []

    articles = []
    full_content_tasks = []

    recent_entries = []
    for entry in feed.entries:
        pub = _parse_time(entry)
        if pub is None:
            title = getattr(entry, "title", "unknown")
            logger.warning("  文章无发布时间，跳过: %s", title)
            continue
        if pub >= start_time and (end_time is None or pub < end_time):
            recent_entries.append((entry, pub))

    for entry, pub in recent_entries:
        link = getattr(entry, "link", "")
        # RSS 摘要
        summary_html = (
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
            or ""
        )
        summary_text = parse_article_content(summary_html, max_chars)
        full_content_tasks.append((entry, pub, link, summary_text))

    # 并行抓取全文（如果摘要内容很短则补全文）
    async def _enrich(entry, pub, link, summary_text):
        content = summary_text
        if len(summary_text) < 200 and link:
            full = await _fetch_full_content(session, link, max_chars)
            if full:
                content = full
        return {
            "source": name,
            "title": getattr(entry, "title", "").strip(),
            "content": content,
            "url": link,
            "published_time": pub.isoformat() if pub else "",
            "category": category,
        }

    results = await asyncio.gather(*[
        _enrich(e, p, l, s) for e, p, l, s in full_content_tasks
    ])
    articles = [r for r in results if r["title"]]
    logger.info("  %s: 获取到 %d 篇文章", name, len(articles))
    return articles


async def fetch_all_between(
    start_time: datetime,
    end_time: datetime | None = None,
    max_chars: int = 3000,
) -> list[dict]:
    """并行抓取所有 RSS 源，返回时间窗口内的文章列表。"""
    sources = load_sources()
    if not sources:
        logger.warning("rss_sources.yaml 中 sources 为空，无文章可抓取。")
        return []

    if end_time:
        logger.info(
            "开始抓取 %d 个 RSS 源，时间窗口: [%s, %s)",
            len(sources),
            start_time.isoformat(),
            end_time.isoformat(),
        )
    else:
        logger.info(
            "开始抓取 %d 个 RSS 源，起始时间: %s",
            len(sources),
            start_time.isoformat(),
        )

    connector = aiohttp.TCPConnector(limit=10)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ai-news-bot/1.0)"}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        tasks = [fetch_source(session, s, start_time, end_time, max_chars) for s in sources]
        results = await asyncio.gather(*tasks)

    all_articles = [a for batch in results for a in batch]
    logger.info("全部抓取完毕，共 %d 篇文章", len(all_articles))
    return all_articles


async def fetch_all(recent_hours: int = 24, max_chars: int = 3000) -> list[dict]:
    """并行抓取最近 N 小时内的所有文章。"""
    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(hours=recent_hours)
    return await fetch_all_between(start_time, end_time=end_time, max_chars=max_chars)

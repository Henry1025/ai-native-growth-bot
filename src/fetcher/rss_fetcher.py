"""RSS fetching helpers with time-window filtering and retry for flaky sources."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import feedparser
import yaml

from .content_parser import parse_article_content

logger = logging.getLogger(__name__)

SOURCES_PATH = Path(__file__).parent.parent.parent / "config" / "rss_sources.yaml"


def _parse_time(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            import calendar

            ts = calendar.timegm(parsed)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    return None


def load_sources() -> list[dict]:
    data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8"))
    return data.get("sources") or []


def _feed_timeout_seconds(source: dict) -> int:
    return 45 if source.get("category") == "wechat" else 30


async def _fetch_feed_raw(session: aiohttp.ClientSession, source: dict, attempts: int = 3) -> bytes | None:
    name = source["name"]
    url = source["url"]
    timeout_seconds = _feed_timeout_seconds(source)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                resp.raise_for_status()
                return await resp.read()
        except Exception as exc:
            last_error = exc
            logger.warning(
                "RSS fetch failed for %s (attempt %d/%d): %s",
                name,
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts:
                await asyncio.sleep(2 * attempt)

    logger.error("RSS fetch exhausted retries for %s: %s", name, last_error)
    return None


async def _fetch_full_content(session: aiohttp.ClientSession, url: str, max_chars: int) -> str:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                html = await resp.text(errors="replace")
                return parse_article_content(html, max_chars)
    except Exception as exc:
        logger.debug("Full-content fetch failed for %s: %s", url, exc)
    return ""


async def fetch_source(
    session: aiohttp.ClientSession,
    source: dict,
    start_time: datetime,
    end_time: datetime | None,
    max_chars: int,
) -> list[dict]:
    name = source["name"]
    url = source["url"]
    category = source.get("category", "")

    logger.info("Fetching RSS: %s (%s)", name, url)
    raw = await _fetch_feed_raw(session, source)
    if raw is None:
        return []

    feed = feedparser.parse(raw)
    recent_entries: list[tuple[Any, datetime]] = []
    for entry in feed.entries:
        published_at = _parse_time(entry)
        if published_at is None:
            logger.warning("Skipping item without publish time from %s: %s", name, getattr(entry, "title", "unknown"))
            continue
        if published_at >= start_time and (end_time is None or published_at < end_time):
            recent_entries.append((entry, published_at))

    async def _enrich(entry: Any, published_at: datetime) -> dict:
        link = getattr(entry, "link", "")
        summary_html = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
        content = parse_article_content(summary_html, max_chars)
        if len(content) < 200 and link:
            full_content = await _fetch_full_content(session, link, max_chars)
            if full_content:
                content = full_content

        return {
            "source": name,
            "title": getattr(entry, "title", "").strip(),
            "content": content,
            "url": link,
            "published_time": published_at.isoformat(),
            "category": category,
        }

    results = await asyncio.gather(*[_enrich(entry, published_at) for entry, published_at in recent_entries])
    articles = [item for item in results if item["title"]]
    logger.info("%s: fetched %d articles in window", name, len(articles))
    return articles


async def fetch_all_between(
    start_time: datetime,
    end_time: datetime | None = None,
    max_chars: int = 3000,
) -> list[dict]:
    sources = load_sources()
    if not sources:
        logger.warning("rss_sources.yaml has no sources configured.")
        return []

    if end_time:
        logger.info(
            "Fetching %d RSS sources for window [%s, %s)",
            len(sources),
            start_time.isoformat(),
            end_time.isoformat(),
        )
    else:
        logger.info("Fetching %d RSS sources since %s", len(sources), start_time.isoformat())

    connector = aiohttp.TCPConnector(limit=8)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ai-news-bot/1.0)"}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        results = await asyncio.gather(
            *[fetch_source(session, source, start_time, end_time, max_chars) for source in sources]
        )

    all_articles = [article for batch in results for article in batch]
    logger.info("Finished fetching RSS sources: %d articles total", len(all_articles))
    return all_articles


async def fetch_all(recent_hours: int = 24, max_chars: int = 3000) -> list[dict]:
    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(hours=recent_hours)
    return await fetch_all_between(start_time, end_time=end_time, max_chars=max_chars)

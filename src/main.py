"""
Daily pipeline:
fetch RSS -> dedup -> screen -> write screening sheet -> analyze -> write report doc
"""

import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from config.settings import settings
from src.fetcher.rss_fetcher import fetch_all
from src.llm.kimi_client import KimiClient
from src.output.feishu_writer import FeishuWriter
from src.processor.dedup import dedup_articles
from src.processor.deep_analysis import generate_report
from src.processor.screening import screen_articles


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def _replace_today_report_if_needed(writer: FeishuWriter, report_date: date) -> None:
    """Keep the Feishu doc aligned with the latest rerun output for the same day."""
    if not settings.feishu_doc_token:
        return

    replaced = await writer.delete_report(settings.feishu_doc_token, report_date)
    if replaced:
        logger.info("Deleted existing report section for %s before rewriting", report_date.isoformat())


async def run():
    logger.info("=" * 60)
    logger.info("AI growth report bot started")
    logger.info("=" * 60)

    articles = await fetch_all(
        recent_hours=settings.recent_hours,
        max_chars=settings.article_max_chars,
    )
    if not articles:
        logger.warning("No articles fetched; exiting.")
        return

    articles = dedup_articles(articles)

    kimi = KimiClient(
        api_key=settings.moonshot_api_key,
        base_url=settings.moonshot_base_url,
        screening_model=settings.screening_model,
        analysis_model=settings.analysis_model,
    )
    screened = await screen_articles(
        kimi,
        articles,
        batch_size=settings.screening_batch_size,
    )
    relevant_count = sum(1 for item in screened if item.get("tag") != "不相关")
    logger.info(
        "Screening complete: relevant=%d irrelevant=%d",
        relevant_count,
        len(screened) - relevant_count,
    )

    writer = FeishuWriter(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
    )
    sheet_url = await writer.write_screening_results(
        screened,
        report_date=date.today(),
        spreadsheet_token=settings.feishu_sheet_token,
    )
    logger.info("Screening sheet updated: %s", sheet_url)

    if relevant_count == 0:
        logger.warning("No relevant articles after screening; writing empty daily report.")
        await _replace_today_report_if_needed(writer, date.today())
        doc_url = await writer.write_report(
            {"core_arguments": []},
            report_date=date.today(),
            doc_token=settings.feishu_doc_token,
        )
        logger.info("Empty report written: %s", doc_url)
        return

    report_data = await generate_report(
        kimi,
        screened,
        articles,
        report_date=date.today(),
    )

    await _replace_today_report_if_needed(writer, date.today())
    doc_url = await writer.write_report(
        report_data,
        report_date=date.today(),
        doc_token=settings.feishu_doc_token,
    )

    logger.info("Daily report written: %s", doc_url)
    logger.info("=" * 60)
    logger.info("Pipeline finished")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())

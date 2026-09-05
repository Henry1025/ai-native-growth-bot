"""
Daily pipeline:
fetch RSS -> dedup -> screen -> write screening sheet -> analyze -> write report doc
"""

import asyncio
import json
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
from src.processor.screening import is_relevant, is_report_relevant, is_screening_failed, screen_articles


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def archive(stage: str, report_date: date, data) -> None:
    folder = ROOT / "artifacts"
    folder.mkdir(exist_ok=True)
    (folder / f"{report_date}-{stage}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


async def run():
    logger.info("=" * 60)
    logger.info("AI growth report bot started")
    logger.info("=" * 60)

    report_date = date.today()
    articles = await fetch_all(
        recent_hours=settings.recent_hours,
        max_chars=settings.article_max_chars,
        lookback_hours=settings.rss_lookback_hours,
    )
    if not articles:
        raise RuntimeError("No articles fetched; report not updated")

    articles = dedup_articles(articles)
    archive("articles", report_date, articles)

    kimi = KimiClient(
        api_key=settings.moonshot_api_key,
        base_url=settings.moonshot_base_url,
        screening_model=settings.screening_model,
        analysis_model=settings.analysis_model,
        screening_max_completion_tokens=settings.screening_max_completion_tokens,
        analysis_max_completion_tokens=settings.analysis_max_completion_tokens,
        timeout_seconds=settings.kimi_timeout_seconds,
    )
    screened = await screen_articles(
        kimi,
        articles,
        batch_size=settings.screening_batch_size,
    )
    candidate_count = sum(1 for item in screened if is_relevant(item))
    report_relevant_count = sum(1 for item in screened if is_report_relevant(item))
    failed_count = sum(1 for item in screened if is_screening_failed(item))
    archive("screened", report_date, screened)
    irrelevant_count = len(screened) - candidate_count - failed_count
    logger.info(
        "Screening complete: candidates=%d report_relevant=%d failed=%d irrelevant=%d",
        candidate_count,
        report_relevant_count,
        failed_count,
        irrelevant_count,
    )

    if failed_count:
        raise RuntimeError(f"Screening failed for {failed_count}/{len(screened)} articles; refusing to publish a report")

    writer = FeishuWriter(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
    )
    sheet_url = await writer.write_screening_results(
        screened,
        report_date=report_date,
        spreadsheet_token=settings.feishu_sheet_token,
    )
    logger.info("Screening sheet updated: %s", sheet_url)

    if report_relevant_count == 0:
        logger.warning("No score>=4 articles after screening; writing empty daily report.")
        doc_url = await writer.write_report(
            {"core_arguments": []},
            report_date=report_date,
            doc_token=settings.feishu_doc_token,
        )
        logger.info("Empty report written: %s", doc_url)
        return

    report_data = await generate_report(
        kimi,
        screened,
        articles,
        report_date=report_date,
    )
    archive("report", report_date, report_data)

    doc_url = await writer.write_report(
        report_data,
        report_date=report_date,
        doc_token=settings.feishu_doc_token,
        replace_existing=True,
    )

    logger.info("Daily report written: %s", doc_url)
    logger.info("=" * 60)
    logger.info("Pipeline finished")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())

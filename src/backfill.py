"""
按日期区间回填历史日报。

默认只回填飞书文档，避免覆盖当前的初筛表格。
"""
import argparse
import asyncio
import logging
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from config.settings import settings
from src.fetcher.rss_fetcher import fetch_all_between
from src.llm.kimi_client import KimiClient
from src.output.feishu_writer import FeishuWriter
from src.processor.dedup import dedup_articles
from src.processor.deep_analysis import generate_report
from src.processor.screening import is_report_relevant, is_screening_failed, screen_articles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填历史日报到飞书文档")
    parser.add_argument("--start-date", required=True, help="开始日期，格式 YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="结束日期，格式 YYYY-MM-DD")
    parser.add_argument("--articles-dir", type=Path, help="读取 YYYY-MM-DD-articles.json 历史归档，避免依赖当前 RSS")
    parser.add_argument("--replace-existing", action="store_true", help="生成成功后安全替换指定日期已有日报")
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="日报运行时区，默认 Asia/Shanghai",
    )
    parser.add_argument(
        "--run-hour",
        type=int,
        default=11,
        help="每天定时运行的小时，默认 11",
    )
    return parser.parse_args()


def _iter_dates(start_date: date, end_date: date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _window_for_report(report_date: date, tz_name: str, run_hour: int) -> tuple[datetime, datetime]:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        if tz_name == "Asia/Shanghai":
            tz = timezone(timedelta(hours=8))
        else:
            raise
    end_local = datetime.combine(report_date, time(hour=run_hour), tzinfo=tz)
    start_local = end_local - timedelta(hours=24)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def _run_for_date(
    report_date: date,
    kimi: KimiClient,
    writer: FeishuWriter,
    tz_name: str,
    run_hour: int,
    articles_dir: Path | None = None,
    replace_existing: bool = False,
):
    start_utc, end_utc = _window_for_report(report_date, tz_name, run_hour)
    logger.info(
        "开始回填 %s，抓取窗口 [%s, %s)",
        report_date.isoformat(),
        start_utc.isoformat(),
        end_utc.isoformat(),
    )

    if articles_dir:
        articles = json.loads((articles_dir / f"{report_date}-articles.json").read_text(encoding="utf-8"))
    else:
        articles = await fetch_all_between(
            start_utc,
            end_time=end_utc,
            max_chars=settings.article_max_chars,
            lookback_hours=settings.rss_lookback_hours,
        )
    if not articles:
        raise RuntimeError(f"{report_date}: no historical articles available; recover archived inputs first")

    articles = dedup_articles(articles)
    screened = await screen_articles(
        kimi,
        articles,
        batch_size=settings.screening_batch_size,
    )
    relevant = [a for a in screened if is_report_relevant(a)]
    if any(is_screening_failed(a) for a in screened):
        raise RuntimeError(f"{report_date}: screening failed; existing report preserved")

    if not relevant:
        await writer.write_report(
            {"core_arguments": []},
            report_date=report_date,
            doc_token=settings.feishu_doc_token,
        )
        logger.info("%s 无相关文章，已写入空日报", report_date.isoformat())
        return {
            "date": report_date.isoformat(),
            "fetched": len(articles),
            "screened": len(screened),
            "relevant": 0,
        }

    report_data = await generate_report(
        kimi,
        screened,
        articles,
        report_date=report_date,
    )
    await writer.write_report(
        report_data,
        report_date=report_date,
        doc_token=settings.feishu_doc_token,
        replace_existing=replace_existing,
    )
    logger.info("%s 回填完成，相关文章 %d", report_date.isoformat(), len(relevant))
    return {
        "date": report_date.isoformat(),
        "fetched": len(articles),
        "screened": len(screened),
        "relevant": len(relevant),
    }


async def main():
    args = _parse_args()
    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    if start_date > end_date:
        raise ValueError("start-date 不能晚于 end-date")

    kimi = KimiClient(
        api_key=settings.moonshot_api_key,
        base_url=settings.moonshot_base_url,
        screening_model=settings.screening_model,
        analysis_model=settings.analysis_model,
        screening_max_completion_tokens=settings.screening_max_completion_tokens,
        analysis_max_completion_tokens=settings.analysis_max_completion_tokens,
        timeout_seconds=settings.kimi_timeout_seconds,
    )
    writer = FeishuWriter(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
    )

    summaries = []
    for report_date in _iter_dates(start_date, end_date):
        summary = await _run_for_date(
            report_date,
            kimi,
            writer,
            tz_name=args.timezone,
            run_hour=args.run_hour,
            articles_dir=args.articles_dir,
            replace_existing=args.replace_existing,
        )
        summaries.append(summary)

    logger.info("历史回填完成，共 %d 天", len(summaries))
    for item in summaries:
        logger.info(
            "%s fetched=%d screened=%d relevant=%d",
            item["date"],
            item["fetched"],
            item["screened"],
            item["relevant"],
        )


if __name__ == "__main__":
    asyncio.run(main())

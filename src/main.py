"""
AI 广告增长日报 Bot — 主入口
Pipeline: 抓取 RSS → 去重 → 初筛(Kimi K2.0) → 写入初筛表格 → 深度分析(Kimi 2.5) → 写入日报文档
"""
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

# 确保项目根目录在 sys.path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from config.settings import settings
from src.fetcher.rss_fetcher import fetch_all
from src.processor.dedup import dedup_articles
from src.processor.screening import screen_articles
from src.processor.deep_analysis import generate_report
from src.llm.kimi_client import KimiClient
from src.output.feishu_writer import FeishuWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def run():
    logger.info("=" * 60)
    logger.info("AI 广告增长日报 Bot 启动")
    logger.info("=" * 60)

    # 1. 抓取 RSS
    articles = await fetch_all(
        recent_hours=settings.recent_hours,
        max_chars=settings.article_max_chars,
    )
    if not articles:
        logger.warning("未抓取到任何文章，流程结束。")
        return

    # 2. 去重
    articles = dedup_articles(articles)

    # 3. 初筛（Kimi K 2.0）
    kimi = KimiClient(
        api_key=settings.moonshot_api_key,
        base_url=settings.moonshot_base_url,
        screening_model=settings.screening_model,
        analysis_model=settings.analysis_model,
    )
    screened = await screen_articles(
        kimi, articles, batch_size=settings.screening_batch_size
    )
    relevant_count = sum(1 for a in screened if a.get("tag") != "不相关")
    logger.info("初筛完成：相关 %d 篇，不相关 %d 篇", relevant_count, len(screened) - relevant_count)

    # 4. 写入初筛结果到飞书表格（无论是否有相关文章，都写入，方便 review 筛选质量）
    writer = FeishuWriter(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
    )
    sheet_url = await writer.write_screening_results(
        screened,
        report_date=date.today(),
        spreadsheet_token=settings.feishu_sheet_token,
    )
    logger.info("初筛结果已写入飞书表格: %s", sheet_url)

    if relevant_count == 0:
        logger.warning("初筛后无相关文章（全部标记为不相关），跳过深度分析。")
        # 仍然写入日期标题和提示到飞书文档
        empty_report = {"core_arguments": []}
        doc_url = await writer.write_report(
            empty_report,
            report_date=date.today(),
            doc_token=settings.feishu_doc_token,
        )
        logger.info("无相关文章，已写入空日报占位: %s", doc_url)
        return

    # 5. 深度分析（Kimi 2.5）- 传入原始 articles 以便关联原文
    report_data = await generate_report(kimi, screened, articles, report_date=date.today())

    # 6. 写入日报到飞书文档（使用固定文档 token，如配置了的话）
    doc_url = await writer.write_report(
        report_data,
        report_date=date.today(),
        doc_token=settings.feishu_doc_token,
    )

    logger.info("日报已写入飞书文档: %s", doc_url)
    logger.info("=" * 60)
    logger.info("流程完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())

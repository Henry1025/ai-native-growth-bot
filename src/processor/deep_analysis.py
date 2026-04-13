"""
调用 Kimi 2.5 对初筛后的文章做深度分析，生成结构化 JSON 日报
"""
import logging
from datetime import date

from ..llm.kimi_client import KimiClient

logger = logging.getLogger(__name__)


async def generate_report(
    client: KimiClient,
    screened: list[dict],
    original_articles: list[dict],
    report_date: date | None = None,
) -> dict:
    """
    生成日报，返回结构化 JSON dict。

    Args:
        client: KimiClient 实例
        screened: 初筛后的文章列表，包含 url, title, summary, tag
        original_articles: 原始抓取的文章列表，包含完整 content
        report_date: 日报日期，默认今天
    """
    if report_date is None:
        report_date = date.today()
    date_str = report_date.strftime("%Y-%m-%d")

    # 过滤掉"不相关"标签的文章，只送相关内容给深度分析
    relevant = [a for a in screened if a.get("tag") != "不相关"]
    logger.info(
        "开始生成日报，日期: %s，相关文章: %d（总共 %d，过滤不相关 %d）",
        date_str, len(relevant), len(screened), len(screened) - len(relevant),
    )
    
    # 关联原文内容：用 url 做 key，把原始 content 合并到初筛结果中
    url_to_content = {a["url"]: a.get("content", "") for a in original_articles}
    enriched = []
    for item in relevant:
        enriched_item = dict(item)
        # 保留原始 content，供深度分析模型参考（获取更多细节）
        enriched_item["original_content"] = url_to_content.get(item.get("url", ""), "")
        enriched.append(enriched_item)
    
    logger.info("已关联原文内容，准备送入深度分析")
    report = await client.analyze_articles(enriched, date_str)
    return report

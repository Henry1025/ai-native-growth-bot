"""
飞书文档/表格 API 封装（lark-oapi SDK）
创建飞书文档写入结构化日报，创建飞书表格写入初筛结果
"""
import asyncio
import json
import logging
import re
from datetime import date

import lark_oapi as lark
from src.processor.screening import get_score, is_relevant, is_report_relevant, is_screening_failed
from lark_oapi.api.docx.v1 import (
    BatchDeleteDocumentBlockChildrenRequest,
    BatchDeleteDocumentBlockChildrenRequestBody,
    CreateDocumentRequest,
    CreateDocumentRequestBody,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
)
from lark_oapi.api.docx.v1.model import (
    Block,
    BlockBuilder,
    Text,
    TextBuilder,
    TextElement,
    TextElementBuilder,
    TextElementStyle,
    TextElementStyleBuilder,
    TextRun,
    TextRunBuilder,
)
from lark_oapi.api.sheets.v3 import (
    CreateSpreadsheetRequest,
    QuerySpreadsheetSheetRequest,
)
from lark_oapi.api.sheets.v3.model import Spreadsheet, SpreadsheetBuilder

logger = logging.getLogger(__name__)


class FeishuWriter:
    def __init__(self, app_id: str, app_secret: str, share_with_user_id: str | None = None):
        self._client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .log_level(lark.LogLevel.ERROR) \
            .build()
        self._share_with_user_id = share_with_user_id  # 要分享的用户ID（如 haochen 的飞书用户ID）

    # ------------------------------------------------------------------
    # 公共接口：日报文档
    # ------------------------------------------------------------------

    async def write_report(
        self,
        report_data: dict,
        report_date: date | None = None,
        doc_token: str | None = None,
    ) -> str:
        """
        将日报写入飞书文档。
        如果提供了 doc_token，则往现有文档追加内容；
        否则创建新文档。
        """
        if report_date is None:
            report_date = date.today()

        if doc_token:
            # 往现有文档追加内容，先检查今天是否已写入
            date_str = report_date.strftime('%Y-%m-%d')
            already_written = await self._doc_has_date_heading(doc_token, date_str)
            if already_written:
                logger.warning("飞书文档已包含 %s 的日报，跳过重复写入", date_str)
                url = f"https://docs.feishu.cn/docx/{doc_token}"
                return url
            logger.info("往现有飞书文档追加日报: %s", doc_token)
            blocks = self._json_report_to_blocks(report_data, report_date)
            if blocks:
                insert_idx = await self._find_insert_index(doc_token, report_date=report_date)
                logger.info("插入位置: index=%d（分割线之后）", insert_idx)
                await self._append_blocks(doc_token, blocks, index=insert_idx)
            url = f"https://docs.feishu.cn/docx/{doc_token}"
            logger.info("飞书文档追加完成: %s", url)
            return url
        else:
            # 创建新文档
            title = f"AI 广告增长日报 · {report_date.strftime('%Y-%m-%d')}"
            logger.info("创建飞书文档: %s", title)
            doc_token = await self._create_document(title)

            blocks = self._json_report_to_blocks(report_data, report_date)
            if blocks:
                await self._append_blocks(doc_token, blocks)

            # 分享给指定用户
            if self._share_with_user_id:
                await self._share_document(doc_token, self._share_with_user_id)

            url = f"https://docs.feishu.cn/docx/{doc_token}"
            logger.info("飞书文档写入完成: %s", url)
            return url

    # ------------------------------------------------------------------
    # 公共接口：初筛结果表格
    # ------------------------------------------------------------------

    async def write_screening_results(
        self,
        screened: list[dict],
        report_date: date | None = None,
        spreadsheet_token: str | None = None,
    ) -> str:
        """
        将初筛结果写入飞书表格。
        如果提供了 spreadsheet_token，则往现有表格写入（先清空）；
        否则创建新表格。
        """
        if report_date is None:
            report_date = date.today()

        if spreadsheet_token:
            # 往现有表格的日期工作表写入，避免覆盖其他日期的初筛结果。
            logger.info("往现有飞书表格写入初筛结果: %s", spreadsheet_token)
            sheet_id = await self._get_or_create_sheet_id(
                spreadsheet_token,
                report_date.strftime("%Y-%m-%d"),
            )
            # 只清空当天工作表，不影响其他日期。
            await self._clear_spreadsheet_data(spreadsheet_token, sheet_id)
            rows = self._prepare_screening_rows(screened)
            await self._write_spreadsheet_data(spreadsheet_token, sheet_id, rows)
            url = f"https://docs.feishu.cn/sheets/{spreadsheet_token}"
            logger.info("飞书表格更新完成: %s", url)
            return url
        else:
            # 创建新表格
            title = f"初筛结果 · {report_date.strftime('%Y-%m-%d')}"
            logger.info("创建飞书表格: %s", title)
            spreadsheet_token = await self._create_spreadsheet(title)
            sheet_id = await self._get_first_sheet_id(spreadsheet_token)

            rows = self._prepare_screening_rows(screened)
            await self._write_spreadsheet_data(spreadsheet_token, sheet_id, rows)

            # 分享给指定用户
            if self._share_with_user_id:
                await self._share_spreadsheet(spreadsheet_token, self._share_with_user_id)

            url = f"https://docs.feishu.cn/sheets/{spreadsheet_token}"
            logger.info("飞书表格写入完成: %s", url)
            return url

    def _prepare_screening_rows(self, screened: list[dict]) -> list[list[str]]:
        rows = [[
            "召回链路",
            "状态",
            "日报",
            "候选",
            "分数",
            "置信度",
            "标签",
            "标题",
            "摘要",
            "增长机制",
            "可行动启发",
            "来源",
            "发布时间",
            "原文链接",
            "统计",
        ]]

        report_relevant_count = 0
        candidate_count = 0
        irrelevant_count = 0
        failed_count = 0

        for item in screened:
            tag = item.get("tag", "")
            title = item.get("title", "")
            summary = item.get("summary", "")
            url = item.get("url", "")
            route = item.get("screening_route", "")
            score = get_score(item)
            confidence = item.get("confidence", "")
            growth_mechanism = item.get("growth_mechanism", "")
            actionable_angle = item.get("actionable_angle", "")
            source = item.get("source", "")
            published_time = item.get("published_time", "")
            is_candidate = is_relevant(item)
            in_report = is_report_relevant(item)
            failed = is_screening_failed(item)
            status = "待重试" if failed else "已初筛"
            score_text = "" if failed else str(score)

            if in_report:
                report_relevant_count += 1
            if failed:
                failed_count += 1
            elif is_candidate:
                candidate_count += 1
            else:
                irrelevant_count += 1

            rows.append([
                route,
                status,
                "Y" if in_report else "",
                "Y" if is_candidate else "",
                score_text,
                confidence,
                tag,
                title,
                summary,
                growth_mechanism,
                actionable_angle,
                source,
                published_time,
                url,
                "",
            ])

        rows.append([
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            f"日报: {report_relevant_count} / 候选: {candidate_count} / 待重试: {failed_count} / 不相关: {irrelevant_count}",
        ])
        return rows

    # ------------------------------------------------------------------
    # 内部方法：文档操作
    # ------------------------------------------------------------------

    async def _create_document(self, title: str) -> str:
        req = CreateDocumentRequest.builder() \
            .request_body(
                CreateDocumentRequestBody.builder()
                .title(title)
                .build()
            ).build()

        resp = await asyncio.to_thread(self._client.docx.v1.document.create, req)
        if not resp.success():
            raise RuntimeError(f"创建飞书文档失败: code={resp.code} msg={resp.msg}")
        return resp.data.document.document_id

    async def _get_doc_blocks(self, doc_token: str) -> list[dict]:
        """获取文档的所有 block 列表。"""
        try:
            req = lark.BaseRequest.builder() \
                .http_method(lark.HttpMethod.GET) \
                .uri(f"/open-apis/docx/v1/documents/{doc_token}/blocks") \
                .token_types({lark.AccessTokenType.TENANT}) \
                .build()
            resp = await asyncio.to_thread(self._client.request, req)
            if not resp.success():
                return []
            import json as _json
            data = _json.loads(resp.raw.content)
            return data.get("data", {}).get("items", [])
        except Exception as e:
            logger.warning("获取文档 blocks 失败: %s", e)
            return []

    async def _doc_has_date_heading(self, doc_token: str, date_str: str) -> bool:
        """检查文档中是否已包含指定日期的日报标题，防止重复写入。"""
        items = await self._get_doc_blocks(doc_token)
        for block in items:
            heading_date = self._extract_heading_date(block)
            if heading_date and heading_date.isoformat() == date_str:
                return True
        return False

    @staticmethod
    def _extract_heading_date(block: dict) -> date | None:
        if block.get("block_type") != 3:
            return None
        elements = block.get("heading1", {}).get("elements", [])
        text = "".join(el.get("text_run", {}).get("content", "") for el in elements)
        text = text.strip()
        m = re.search(r"(?:AI 广告增长日报 · )?(\d{4}-\d{2}-\d{2})$", text)
        if not m:
            return None
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None

    @staticmethod
    def _format_report_heading(report_date: date) -> str:
        """日报段落标题只保留日期，兼容旧格式解析逻辑。"""
        return report_date.strftime("%Y-%m-%d")

    @staticmethod
    def _normalize_sub_point(text: str) -> str:
        """清理模型输出里低价值的编号前缀。"""
        return re.sub(r"^\s*事实\s*\d+\s*[：:]\s*", "", text).strip()

    async def _find_insert_index(self, doc_token: str, report_date: date | None = None) -> int:
        """找到日报在文档直接子块中的插入位置。

        默认插在分割线之后的日报区顶部；如果指定 report_date，则按日期倒序插入，
        保证补历史日报时不会打乱已有顺序。
        """
        items = await self._get_doc_blocks(doc_token)
        # 只取文档的直接子块（parent_id == doc_token），忽略嵌套块
        doc_children = [b for b in items if b.get("parent_id") == doc_token]
        divider_idx = None

        for i, block in enumerate(doc_children):
            if block.get("block_type") == 22:
                divider_idx = i
                break

        if divider_idx is None:
            content_start_idx = 0
        else:
            content_start_idx = divider_idx + 1
            if content_start_idx < len(doc_children):
                next_block = doc_children[content_start_idx]
                if next_block.get("block_type") == 2:
                    text_content = "".join(
                        el.get("text_run", {}).get("content", "")
                        for el in next_block.get("text", {}).get("elements", [])
                    )
                    if not text_content.strip():
                        content_start_idx += 1

        if report_date is None:
            return content_start_idx if divider_idx is not None else len(doc_children)

        for i in range(content_start_idx, len(doc_children)):
            heading_date = self._extract_heading_date(doc_children[i])
            if heading_date and heading_date < report_date:
                return i

        return len(doc_children)

    async def _find_report_range(self, doc_token: str, report_date: date) -> tuple[int, int] | None:
        """找到某一天日报在文档直接子块中的 [start, end) 范围。"""
        items = await self._get_doc_blocks(doc_token)
        doc_children = [b for b in items if b.get("parent_id") == doc_token]
        start_idx = None

        for i, block in enumerate(doc_children):
            heading_date = self._extract_heading_date(block)
            if heading_date and heading_date == report_date:
                start_idx = i
                break

        if start_idx is None:
            return None

        end_idx = len(doc_children)
        for i in range(start_idx + 1, len(doc_children)):
            if self._extract_heading_date(doc_children[i]):
                end_idx = i
                break
        return start_idx, end_idx

    async def delete_report(self, doc_token: str, report_date: date) -> bool:
        """删除指定日期的整段日报。返回是否实际删除。"""
        report_range = await self._find_report_range(doc_token, report_date)
        if report_range is None:
            return False

        start_idx, end_idx = report_range
        req = BatchDeleteDocumentBlockChildrenRequest.builder() \
            .document_id(doc_token) \
            .block_id(doc_token) \
            .request_body(
                BatchDeleteDocumentBlockChildrenRequestBody.builder()
                .start_index(start_idx)
                .end_index(end_idx)
                .build()
            ).build()

        resp = await asyncio.to_thread(self._client.docx.v1.document_block_children.batch_delete, req)
        if not resp.success():
            raise RuntimeError(f"删除日报 Block 失败: code={resp.code} msg={resp.msg}")
        return True

    async def _append_blocks(self, doc_token: str, blocks: list[Block], index: int = 0):
        """在文档指定位置插入 blocks。index=0 表示插入到文档最前面。"""
        for offset in range(0, len(blocks), 30):
            chunk = blocks[offset : offset + 30]
            req = CreateDocumentBlockChildrenRequest.builder() \
                .document_id(doc_token) \
                .block_id(doc_token) \
                .request_body(
                    CreateDocumentBlockChildrenRequestBody.builder()
                    .children(chunk)
                    .index(index + offset)
                    .build()
                ).build()

            resp = await asyncio.to_thread(self._client.docx.v1.document_block_children.create, req)
            if not resp.success():
                raise RuntimeError(f"插入 Block 失败: code={resp.code} msg={resp.msg}")

    # ------------------------------------------------------------------
    # 内部方法：表格操作
    # ------------------------------------------------------------------

    async def _create_spreadsheet(self, title: str) -> str:
        req = CreateSpreadsheetRequest.builder() \
            .request_body(
                SpreadsheetBuilder()
                .title(title)
                .build()
            ).build()

        resp = await asyncio.to_thread(self._client.sheets.v3.spreadsheet.create, req)
        if not resp.success():
            raise RuntimeError(f"创建飞书表格失败: code={resp.code} msg={resp.msg}")
        return resp.data.spreadsheet.spreadsheet_token

    async def _get_first_sheet_id(self, spreadsheet_token: str) -> str:
        sheets = await self._get_sheets(spreadsheet_token)
        if not sheets:
            raise RuntimeError("表格中没有 sheet")
        return self._sheet_attr(sheets[0], "sheet_id")

    async def _get_sheets(self, spreadsheet_token: str) -> list:
        req = QuerySpreadsheetSheetRequest.builder() \
            .spreadsheet_token(spreadsheet_token) \
            .build()

        resp = await asyncio.to_thread(self._client.sheets.v3.spreadsheet_sheet.query, req)
        if not resp.success():
            raise RuntimeError(f"查询表格 sheet 失败: code={resp.code} msg={resp.msg}")
        return resp.data.sheets or []

    @staticmethod
    def _sheet_attr(sheet, attr: str):
        if isinstance(sheet, dict):
            return sheet.get(attr)
        return getattr(sheet, attr, None)

    async def _get_or_create_sheet_id(self, spreadsheet_token: str, title: str) -> str:
        sheets = await self._get_sheets(spreadsheet_token)
        for sheet in sheets:
            if self._sheet_attr(sheet, "title") == title:
                return self._sheet_attr(sheet, "sheet_id")

        req = lark.BaseRequest.builder() \
            .http_method(lark.HttpMethod.POST) \
            .uri(f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/sheets_batch_update") \
            .token_types({lark.AccessTokenType.TENANT}) \
            .body({
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": title,
                                "index": 0,
                            }
                        }
                    }
                ]
            }) \
            .build()

        resp = await asyncio.to_thread(self._client.request, req)
        if not resp.success():
            raise RuntimeError(f"创建日期工作表失败: code={resp.code} msg={resp.msg}")

        sheets = await self._get_sheets(spreadsheet_token)
        for sheet in sheets:
            if self._sheet_attr(sheet, "title") == title:
                return self._sheet_attr(sheet, "sheet_id")
        raise RuntimeError(f"创建日期工作表后未找到 sheet: {title}")

    async def _clear_spreadsheet_data(self, spreadsheet_token: str, sheet_id: str):
        """清空表格数据（写入空值到 A1:Z1000 范围）。"""
        range_str = f"{sheet_id}!A1:Z1000"
        body = {
            "valueRange": {
                "range": range_str,
                "values": [[""] * 26 for _ in range(1000)],  # 26列 x 1000行空数据
            }
        }
        req = lark.BaseRequest.builder() \
            .http_method(lark.HttpMethod.PUT) \
            .uri(f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values") \
            .token_types({lark.AccessTokenType.TENANT}) \
            .body(body) \
            .build()
        await asyncio.to_thread(self._client.request, req)
        # 清空失败不影响主流程

    async def _write_spreadsheet_data(
        self,
        spreadsheet_token: str,
        sheet_id: str,
        rows: list[list[str]],
    ):
        if not rows:
            return

        num_rows = len(rows)
        num_cols = max(len(r) for r in rows)
        end_col = chr(ord('A') + num_cols - 1)
        range_str = f"{sheet_id}!A1:{end_col}{num_rows}"

        # 构造 values: list[list[str]]
        values = []
        for row in rows:
            padded = row + [""] * (num_cols - len(row))
            values.append(padded)

        # 使用 raw API 写入数据（SDK 没有封装 sheets v2 values 接口）
        body = {
            "valueRange": {
                "range": range_str,
                "values": values,
            }
        }

        req = lark.BaseRequest.builder() \
            .http_method(lark.HttpMethod.PUT) \
            .uri(f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values") \
            .token_types({lark.AccessTokenType.TENANT}) \
            .body(body) \
            .build()

        resp = await asyncio.to_thread(self._client.request, req)
        if not resp.success():
            raise RuntimeError(f"写入表格数据失败: code={resp.code} msg={resp.msg}")

    # ------------------------------------------------------------------
    # 权限管理：分享给指定用户
    # ------------------------------------------------------------------

    async def _share_document(self, doc_token: str, user_id: str):
        """将文档分享给指定用户（协作者权限）。"""
        body = {
            "member_type": "openid",
            "member_id": user_id,
            "perm": "edit",  # 编辑权限
        }
        req = lark.BaseRequest.builder() \
            .http_method(lark.HttpMethod.POST) \
            .uri(f"/open-apis/drive/v1/files/{doc_token}/permissions") \
            .token_types({lark.AccessTokenType.TENANT}) \
            .body(body) \
            .build()

        resp = await asyncio.to_thread(self._client.request, req)
        if not resp.success():
            logger.warning("分享文档失败: code=%s msg=%s", resp.code, resp.msg)
        else:
            logger.info("文档已分享给用户: %s", user_id)

    async def _share_spreadsheet(self, spreadsheet_token: str, user_id: str):
        """将表格分享给指定用户（协作者权限）。"""
        body = {
            "member_type": "openid",
            "member_id": user_id,
            "perm": "edit",  # 编辑权限
        }
        req = lark.BaseRequest.builder() \
            .http_method(lark.HttpMethod.POST) \
            .uri(f"/open-apis/drive/v1/files/{spreadsheet_token}/permissions") \
            .token_types({lark.AccessTokenType.TENANT}) \
            .body(body) \
            .build()

        resp = await asyncio.to_thread(self._client.request, req)
        if not resp.success():
            logger.warning("分享表格失败: code=%s msg=%s", resp.code, resp.msg)
        else:
            logger.info("表格已分享给用户: %s", user_id)

    # ------------------------------------------------------------------
    # JSON 日报 → 飞书 Block
    # ------------------------------------------------------------------

    def _json_report_to_blocks(self, report_data: dict, report_date: date) -> list[Block]:
        blocks: list[Block] = []

        blocks.append(self._make_heading1(self._format_report_heading(report_date)))

        core_arguments = report_data.get("core_arguments", [])
        if not core_arguments:
            blocks.append(self._make_text("今日无符合条件的新闻。"))
            return blocks

        for idx, arg in enumerate(core_arguments, 1):
            self._append_argument_blocks(blocks, arg, idx)

        return blocks

    def _append_argument_blocks(
        self,
        blocks: list[Block],
        arg: dict,
        idx: int,
    ) -> None:
        """Append one report argument to Feishu blocks."""
        argument = arg.get("argument", "")
        section = arg.get("section", "")
        sub_points = arg.get("sub_points", [])
        sources = arg.get("sources", [])

        blocks.append(self._make_heading2(f"{idx}. {argument}"))
        if section:
            blocks.append(self._make_text(f"标签：{section}"))

        for sp in sub_points:
            blocks.append(self._make_text(f"• {self._normalize_sub_point(sp)}"))

        # 来源链接紧跟在每条新闻后面
        for src in sources:
            src_title = src.get("title", "来源")
            src_url = src.get("url", "")
            if src_url:
                blocks.append(self._make_text(f"来源：{src_title} {src_url}"))

    # ------------------------------------------------------------------
    # Block 构建辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _text_run(text: str, bold: bool = False) -> TextElement:
        text = str(text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        if len(text) > 1800:
            text = text[:1800] + "…"
        tr_builder = TextRunBuilder().content(text)
        if bold:
            style = TextElementStyleBuilder().bold(True).build()
            tr_builder = tr_builder.text_element_style(style)
        return TextElementBuilder().text_run(tr_builder.build()).build()

    def _make_text_obj(self, text: str, bold: bool = False) -> Text:
        # 简单处理 **bold** 标记
        elements = []
        for part in re.split(r"(\*\*[^*]+\*\*)", text):
            if part.startswith("**") and part.endswith("**"):
                elements.append(self._text_run(part[2:-2], bold=True))
            elif part:
                elements.append(self._text_run(part, bold=bold))
        return TextBuilder().elements(elements).build()

    def _make_heading1(self, text: str) -> Block:
        text_obj = self._make_text_obj(text, bold=True)
        return BlockBuilder().block_type(3).heading1(text_obj).build()

    def _make_heading2(self, text: str) -> Block:
        if len(text) > 220:
            text = text[:220] + "…"
        text_obj = self._make_text_obj(text, bold=True)
        return BlockBuilder().block_type(4).heading2(text_obj).build()

    def _make_text(self, text: str) -> Block:
        text_obj = self._make_text_obj(text)
        return BlockBuilder().block_type(2).text(text_obj).build()

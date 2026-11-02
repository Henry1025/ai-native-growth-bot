import json
import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

for key in ('DEEPSEEK_API_KEY', 'FEISHU_APP_ID', 'FEISHU_APP_SECRET'):
    os.environ.setdefault(key, 'test')

import httpx
from src.llm.deepseek_client import DeepSeekClient, _is_retryable_error
from src.output.feishu_writer import FeishuWriter
from src import main, backfill

class FailureSafetyTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        return DeepSeekClient('test', 'https://api.deepseek.com', 'deepseek-flash', 'deepseek-v4-pro')

    async def test_missing_screening_rows_are_failures(self):
        client = self.client()
        client._call_screening_api = AsyncMock(return_value='[]')
        result = await client.screen_articles([{'url': 'https://example.com', 'title': 'news'}])
        self.assertEqual(result[0]['screening_status'], 'failed')
        self.assertEqual(client._call_screening_api.await_count, 2)

    async def test_deepseek_request_modes_are_explicit(self):
        client = self.client()
        client._collect_stream_text = AsyncMock(return_value='{}')

        await client._call_screening_api([{'role': 'user', 'content': 'json'}])
        screening_args = client._collect_stream_text.await_args.kwargs
        self.assertEqual(screening_args['model'], 'deepseek-flash')
        self.assertEqual(screening_args['max_tokens'], 8192)
        self.assertEqual(screening_args['extra_body'], {'thinking': {'type': 'disabled'}})
        self.assertNotIn('reasoning_effort', screening_args)

        await client._call_analysis_api([{'role': 'user', 'content': 'json'}])
        analysis_args = client._collect_stream_text.await_args.kwargs
        self.assertEqual(analysis_args['model'], 'deepseek-v4-pro')
        self.assertEqual(analysis_args['max_tokens'], 16384)
        self.assertEqual(analysis_args['reasoning_effort'], 'high')
        self.assertEqual(analysis_args['response_format'], {'type': 'json_object'})
        self.assertEqual(analysis_args['extra_body'], {'thinking': {'type': 'enabled'}})

    async def test_wrong_urls_are_failures(self):
        client = self.client()
        client._call_screening_api = AsyncMock(return_value='[{"url":"https://wrong.example"}]')
        result = await client.screen_articles([{'url': 'https://example.com'}])
        self.assertEqual(result[0]['screening_status'], 'failed')
        self.assertEqual(result[0]['screening_error'], 'incomplete_output_after_retry')

    async def test_analysis_invalid_or_empty_never_succeeds(self):
        client = self.client()
        for text in ('not json', '[]', '{"core_arguments":[]}'):
            client._call_analysis_api = AsyncMock(return_value=text)
            with self.assertRaises(RuntimeError):
                await client.analyze_articles([{'url': 'https://example.com'}], '2026-09-01')

    async def test_failed_screening_never_touches_feishu(self):
        with patch.object(main, 'archive'), patch.object(main, 'fetch_all', AsyncMock(return_value=[{'url': 'https://example.com', 'title': 'news'}])), patch.object(main, 'screen_articles', AsyncMock(return_value=[{'screening_status': 'failed', 'score': 0}])), patch.object(main, 'FeishuWriter') as writer:
            with self.assertRaises(RuntimeError):
                await main.run()
            writer.assert_not_called()

    async def test_backfill_without_archive_never_writes_empty(self):
        writer = AsyncMock()
        with patch.object(backfill, 'fetch_all_between', AsyncMock(return_value=[])):
            with self.assertRaises(RuntimeError):
                await backfill._run_for_date(date(2026, 9, 1), self.client(), writer, 'Asia/Shanghai', 11)
        writer.write_report.assert_not_called()

    async def test_document_pagination(self):
        writer = FeishuWriter('test', 'test')
        def response(payload):
            return SimpleNamespace(success=lambda: True, raw=SimpleNamespace(content=json.dumps({'data': payload})))
        with patch.object(writer._client, 'request', side_effect=[response({'items': [{'block_id': 'a'}], 'has_more': True, 'page_token': 'next'}), response({'items': [{'block_id': 'b'}], 'has_more': False})]) as request:
            blocks = await writer._get_doc_blocks('doc')
        self.assertEqual([b['block_id'] for b in blocks], ['a', 'b'])
        self.assertEqual(request.call_count, 2)

    async def test_failed_insert_retains_previous_report(self):
        writer = FeishuWriter('test', 'test')
        writer._find_report_range = AsyncMock(return_value=(0, 2))
        writer._get_doc_blocks = AsyncMock(return_value=[{'parent_id': 'doc', 'block_id': 'old1'}, {'parent_id': 'doc', 'block_id': 'old2'}])
        writer._append_blocks = AsyncMock(side_effect=RuntimeError('network failure'))
        with patch.object(writer._client.docx.v1.document_block_children, 'batch_delete') as delete:
            with self.assertRaises(RuntimeError):
                await writer.replace_report({'core_arguments': []}, date(2026, 9, 1), 'doc')
            delete.assert_not_called()

    async def test_replacement_removes_only_old_blocks_after_insert(self):
        writer = FeishuWriter('test', 'test')
        old = [{'parent_id': 'doc', 'block_id': 'old1'}, {'parent_id': 'doc', 'block_id': 'old2'}]
        new = [{'parent_id': 'doc', 'block_id': 'new1'}, {'parent_id': 'doc', 'block_id': 'new2'}]
        writer._find_report_range = AsyncMock(return_value=(0, 2))
        writer._get_doc_blocks = AsyncMock(side_effect=[old, new + old])
        writer._append_blocks = AsyncMock()
        with patch.object(writer._client.docx.v1.document_block_children, 'batch_delete', return_value=SimpleNamespace(success=lambda: True)) as delete:
            await writer.replace_report({'core_arguments': []}, date(2026, 9, 1), 'doc')
            request = delete.call_args.args[0]
            self.assertEqual(request.request_body.start_index, 2)
            self.assertEqual(request.request_body.end_index, 4)
        writer._append_blocks.assert_awaited_once()

    def test_stream_transport_error_can_retry(self):
        self.assertTrue(_is_retryable_error(httpx.ReadError('interrupted')))

if __name__ == '__main__': unittest.main()

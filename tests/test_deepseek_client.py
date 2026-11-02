import json
import unittest
from unittest.mock import AsyncMock

from src.llm.deepseek_client import DeepSeekClient


class DeepSeekClientScreeningTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        return DeepSeekClient(
            "test",
            "https://api.deepseek.com",
            "deepseek-flash",
            "deepseek-v4-pro",
        )

    async def test_article_id_preserves_canonical_url(self):
        client = self.client()
        client._call_screening_api = AsyncMock(return_value=json.dumps([{
            "article_id": "article-001",
            "url": "https://model-rewrote.example/path",
            "title": "rewritten title",
            "tag": "不相关",
            "score": 1,
            "summary": "不相关（测试）",
        }], ensure_ascii=False))

        result = await client.screen_articles([{
            "url": "https://example.com/original",
            "title": "original title",
        }])

        self.assertEqual(result[0]["url"], "https://example.com/original")
        self.assertEqual(result[0]["title"], "original title")
        self.assertNotIn("article_id", result[0])
        self.assertNotEqual(result[0].get("screening_status"), "failed")

    async def test_only_missing_rows_are_retried_and_salvaged(self):
        client = self.client()
        client._call_screening_api = AsyncMock(side_effect=[
            json.dumps([{
                "article_id": "article-001",
                "url": "https://example.com/one",
                "title": "one",
                "tag": "不相关",
                "score": 1,
                "summary": "不相关（测试）",
            }], ensure_ascii=False),
            json.dumps([{
                "article_id": "article-002",
                "url": "https://changed.example/two",
                "title": "changed",
                "tag": "不相关",
                "score": 1,
                "summary": "不相关（测试）",
            }], ensure_ascii=False),
        ])

        result = await client.screen_articles([
            {"url": "https://example.com/one", "title": "one"},
            {"url": "https://example.com/two", "title": "two"},
        ])

        self.assertEqual(client._call_screening_api.await_count, 2)
        self.assertEqual([item["url"] for item in result], [
            "https://example.com/one",
            "https://example.com/two",
        ])
        self.assertTrue(all(item.get("screening_status") != "failed" for item in result))

        first_payload = json.loads(
            client._call_screening_api.await_args_list[0].args[0][1]["content"]
        )
        retry_payload = json.loads(
            client._call_screening_api.await_args_list[1].args[0][1]["content"]
        )
        self.assertEqual(
            [item["article_id"] for item in first_payload],
            ["article-001", "article-002"],
        )
        self.assertEqual(
            [item["article_id"] for item in retry_payload],
            ["article-002"],
        )

    async def test_unknown_reference_fails_only_after_individual_retry(self):
        client = self.client()
        client._call_screening_api = AsyncMock(
            return_value='[{"article_id":"unknown","url":"https://wrong.example"}]'
        )

        result = await client.screen_articles([{"url": "https://example.com"}])

        self.assertEqual(client._call_screening_api.await_count, 2)
        self.assertEqual(result[0]["screening_status"], "failed")
        self.assertEqual(result[0]["screening_error"], "incomplete_output_after_retry")


if __name__ == "__main__":
    unittest.main()

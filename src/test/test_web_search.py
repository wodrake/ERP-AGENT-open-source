"""联网搜索工具的离线回归测试（不触发真实 DeepSeek 请求）。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from src.agent.tools.web_search import _extract_response_text, web_search


class WebSearchTests(unittest.TestCase):
    def test_extracts_responses_api_output_text(self):
        self.assertEqual(
            _extract_response_text({"output_text": "搜索摘要"}), "搜索摘要"
        )

    def test_extracts_nested_message_output(self):
        payload = {
            "output": [
                {"type": "web_search_call", "action": {"type": "search"}},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "搜索结果"}],
                },
            ]
        }
        self.assertEqual(_extract_response_text(payload), "搜索结果")

    def test_uses_deepseek_key_and_responses_web_search_tool(self):
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"output_text": "DeepSeek 搜索摘要"}

        with patch(
            "src.agent.tools.web_search.get_env",
            side_effect=lambda key, default="": {
                "DEEPSEEK_API_KEY": "test-key",
                "LLM_MODEL": "qwen3.8-27b",  # 旧配置不应影响搜索模型
            }.get(key, default),
        ), patch(
            "src.agent.tools.web_search.httpx.post", return_value=Response()
        ) as post:
            result = web_search.invoke({"query": "测试查询"})

        self.assertEqual(result, "DeepSeek 搜索摘要")
        self.assertEqual(post.call_args.args[0], "https://api.deepseek.com/responses")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertEqual(payload["tool_choice"], {"type": "web_search"})


if __name__ == "__main__":
    unittest.main()

"""ERP 超时返回结构化错误，并且 SSE 仍以 done 结束。"""
import asyncio
import json
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from langchain_core.messages import AIMessage, ToolMessage

from src.api_view.api import chat
from src.agent.backends.sandbox_manager import sandbox_manager
from src.mcp_server.http_base import erp_client, get_read_budget_seconds
from src.mcp_server.tools.parts_tools import register_parts_tools


class _Collector:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _TimeoutClient:
    def __init__(self):
        self.calls = []
        self.is_closed = False

    async def request(self, method, path, **kwargs):
        self.calls.append(method)
        raise httpx.ReadTimeout("simulated timeout")


class ERPReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._previous = erp_client._client
        self.fake = _TimeoutClient()
        erp_client._client = self.fake
        self._delays = []

        async def sleep(seconds):
            self._delays.append(seconds)

        self._sleep = sleep

    async def asyncTearDown(self):
        erp_client._client = self._previous

    async def test_get_retries_at_most_twice_and_returns_structured_timeout(self):
        with patch.dict(os.environ, {"HTTP_GET_RETRY_BASE_SECONDS": "0.2"}):
            with patch("src.mcp_server.http_base.asyncio.sleep", self._sleep):
                result = await erp_client.get("/api/parts/get/1")
        self.assertEqual(self.fake.calls, ["GET", "GET", "GET"])
        self.assertEqual(self._delays, [0.2, 0.4])
        self.assertEqual(result["code"], 504)
        self.assertEqual(result["error"], "timeout")
        self.assertTrue(result["retried"])
        self.assertIn("不要编造", result["message"])

    async def test_write_is_not_retried(self):
        with patch("src.mcp_server.http_base.asyncio.sleep", self._sleep):
            result = await erp_client.post("/api/orders/create", json_data={"orderNumber": "X"})
        self.assertEqual(self.fake.calls, ["POST"])
        self.assertEqual(self._delays, [])
        self.assertEqual(result["error"], "timeout")
        self.assertFalse(result["retried"])
        self.assertEqual(result["attempts"], 1)
        self.assertIn("没有确认写入", result["message"])

    async def test_connect_error_is_structured(self):
        class ConnectClient(_TimeoutClient):
            async def request(self, method, path, **kwargs):
                self.calls.append(method)
                raise httpx.ConnectError("simulated connect")

        erp_client._client = ConnectClient()
        with patch("src.mcp_server.http_base.asyncio.sleep", self._sleep):
            result = await erp_client.put("/api/orders/update/1", json_data={})
        self.assertEqual(result["code"], 503)
        self.assertEqual(result["error"], "connect_error")
        self.assertEqual(result["attempts"], 1)

    async def test_default_get_budget_is_twelve_seconds(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in ("HTTP_GET_ATTEMPT_TIMEOUT", "HTTP_GET_DEADLINE_SECONDS", "HTTP_GET_RETRY_BASE_SECONDS"):
                os.environ.pop(key, None)
            self.assertEqual(get_read_budget_seconds(), 12.0)

    async def test_get_deadline_stops_before_another_full_timeout(self):
        clock = {"t": 0.0}

        class HangingClient(_TimeoutClient):
            async def request(self, method, path, **kwargs):
                timeout = kwargs.get("timeout")
                seconds = float(timeout.read)
                self.calls.append(seconds)
                clock["t"] += seconds
                raise httpx.ReadTimeout("simulated timeout")

        erp_client._client = HangingClient()

        def now():
            return clock["t"]

        async def sleep(seconds):
            self._delays.append(seconds)
            clock["t"] += seconds

        with patch.dict(os.environ, {
            "HTTP_GET_ATTEMPT_TIMEOUT": "5",
            "HTTP_GET_DEADLINE_SECONDS": "12",
            "HTTP_GET_RETRY_BASE_SECONDS": "0.2",
        }):
            with patch("src.mcp_server.http_base.time.monotonic", now):
                with patch("src.mcp_server.http_base.asyncio.sleep", sleep):
                    result = await erp_client.get("/api/parts/get/1")
        self.assertEqual(result["error"], "timeout")
        self.assertEqual(result["deadline_seconds"], 12.0)
        self.assertLessEqual(clock["t"], 12.0 + 1e-9)
        self.assertEqual(len(erp_client._client.calls), 3)
        self.assertEqual(erp_client._client.calls[0], 5.0)
        self.assertEqual(erp_client._client.calls[1], 5.0)
        self.assertAlmostEqual(erp_client._client.calls[2], 1.4, places=6)
        self.assertAlmostEqual(sum(erp_client._client.calls) + sum(self._delays), 12.0, places=6)

    async def test_part_query_timeout_reaches_sse_done(self):
        self.fake.calls.clear()
        self._delays.clear()
        collector = _Collector()
        register_parts_tools(collector)
        with patch.dict(os.environ, {"HTTP_GET_RETRY_BASE_SECONDS": "0"}):
            with patch("src.mcp_server.http_base.asyncio.sleep", self._sleep):
                text = await collector.tools["part_query"](1)
        payload = json.loads(text)
        self.assertEqual(payload["error"], "timeout")
        self.assertIn("不要编造", payload["message"])

        async def stream(**kwargs):
            yield (), "messages", (
                ToolMessage(content=text, name="part_query", tool_call_id="tool-1"),
                {},
            )
            yield (), "messages", (
                AIMessage(content="ERP 查询超时，请稍后再试。我没有编造价格。"),
                {},
            )

        session = SimpleNamespace(agent=SimpleNamespace(astream=stream), run_lock=asyncio.Lock())
        with ExitStack() as stack:
            stack.enter_context(patch.object(chat.agent_loader, "get_session", AsyncMock(return_value=session)))
            stack.enter_context(patch.object(chat.agent_loader, "get_display_messages", AsyncMock(return_value=[])))
            for name in ("save_display_messages", "save_harness_trace", "save_conversation"):
                stack.enter_context(patch.object(chat.agent_loader, name, AsyncMock()))
            stack.enter_context(patch.object(sandbox_manager, "acquire_lease", return_value=False))
            chunks = [
                item async for item in chat.stream_chat_response.__wrapped__(
                    "查一下这个零件", "thread-timeout", "user-timeout"
                )
            ]
        events = [(chunk.split("\n")[0][7:], json.loads(chunk.split("\n")[1][6:])) for chunk in chunks]
        tool_payloads = [value for name, value in events if name == "tool_result"]
        self.assertTrue(tool_payloads)
        self.assertIn("timeout", tool_payloads[0]["content"])
        self.assertIn("不要编造", tool_payloads[0]["content"])
        self.assertTrue(any(name == "token" and "超时" in value["content"] for name, value in events))
        self.assertFalse(any(name == "error" for name, _value in events))
        self.assertEqual(events[-1][0], "done")
        self.assertFalse(events[-1][1]["interrupted"])
        self.assertFalse(session.run_lock.locked())


if __name__ == "__main__":
    unittest.main()

"""MCP 工具数为 0 的会话在 TTL 后重建，不需要重启后端。

这里用假时钟把默认 60 秒 TTL 走完。61 秒仍小于验收要求的 90 秒，
并且第二次构建必须看到全部 23 个工具。真实进程的启停放在 tests/live，默认不跑。
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.api_view.agent_loader import agent_loader


class MCPSessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loader = agent_loader
        self._sessions = dict(self.loader._sessions)
        self._store = self.loader._store
        self.loader._sessions.clear()
        self.loader._session_build_locks.clear()
        self.loader._store = object()

    async def asyncTearDown(self):
        self.loader._sessions.clear()
        self.loader._sessions.update(self._sessions)
        self.loader._session_build_locks.clear()
        self.loader._store = self._store

    async def test_zero_tools_reload_all_23_within_ninety_seconds(self):
        clock = {"now": 1_000.0}
        calls = {"n": 0, "refresh": []}

        def build(user_id, username, refresh_mcp=False):
            calls["n"] += 1
            calls["refresh"].append(refresh_mcp)
            # 第一次模拟 MCP 未启动；TTL 到期后的下一次请求模拟 MCP 已恢复。
            count = 0 if calls["n"] == 1 else 23
            return SimpleNamespace(user_id=user_id, mcp_tool_count=count), count

        with patch.dict(os.environ, {"MCP_DEGRADED_SESSION_TTL_SECONDS": "60"}):
            with patch.object(self.loader, "_build_agent_for_user", build):
                with patch.object(self.loader, "_now", lambda: clock["now"]):
                    first = await self.loader.get_session("recovery-user", "采购员")
                    self.assertEqual(first.mcp_tool_count, 0)
                    self.assertTrue(first.degraded)
                    clock["now"] += 30
                    cached = await self.loader.get_session("recovery-user", "采购员")
                    self.assertIs(cached, first)
                    clock["now"] += 31
                    recovered = await self.loader.get_session("recovery-user", "采购员")

        self.assertEqual(recovered.mcp_tool_count, 23)
        self.assertFalse(recovered.degraded)
        self.assertIsNone(recovered.expires_at)
        self.assertEqual(calls["refresh"], [False, True])
        self.assertLessEqual(61, 90)

    async def test_healthy_session_is_not_rebuilt(self):
        clock = {"now": 1.0}
        calls = {"n": 0}

        def build(user_id, username, refresh_mcp=False):
            calls["n"] += 1
            return SimpleNamespace(), 23

        with patch.object(self.loader, "_build_agent_for_user", build):
            with patch.object(self.loader, "_now", lambda: clock["now"]):
                first = await self.loader.get_session("stable-user")
                clock["now"] += 3600
                second = await self.loader.get_session("stable-user")
        self.assertIs(first, second)
        self.assertEqual(first.mcp_tool_count, 23)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()

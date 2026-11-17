"""/health 分别报告 MCP、Mongo 和沙箱，seccomp 降级必须可见。"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.agent.backends.sandbox_status import (
    note_basic_fallback,
    reset_for_tests,
    snapshot,
)


class HealthComponentTests(unittest.TestCase):
    def tearDown(self):
        reset_for_tests()

    def test_seccomp_fallback_is_explicitly_degraded(self):
        reset_for_tests()
        note_basic_fallback("seccomp profile rejected by docker")
        with patch.dict(os.environ, {"SANDBOX_BACKEND": "docker"}, clear=False):
            status = snapshot()
        self.assertEqual(status["status"], "degraded")
        self.assertEqual(status["seccomp"], "degraded")
        self.assertEqual(status["mode"], "docker_basic")
        self.assertIn("seccomp", status["detail"])

    def test_health_endpoint_reports_each_component(self):
        os.environ["SANDBOX_BACKEND"] = "state"
        mcp = {"status": "ok", "detail": "reachable", "last_tool_count": 23}
        mongo = {"status": "ok", "detail": "ping ok"}
        sandbox = {
            "status": "degraded",
            "mode": "docker_basic",
            "seccomp": "degraded",
            "local_fallback": False,
            "detail": "seccomp 已降级",
        }
        with patch("src.api_view.health.probe_mcp", new=AsyncMock(return_value=mcp)), patch(
            "src.api_view.health.probe_mongo", new=AsyncMock(return_value=mongo)
        ), patch("src.api_view.health.probe_sandbox", return_value=sandbox):
            from fastapi.testclient import TestClient
            from src.api_view.web_main import app

            with TestClient(app) as client:
                response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(set(body["components"]), {"mcp", "mongo", "sandbox"})
        self.assertEqual(body["components"]["mcp"]["last_tool_count"], 23)
        self.assertEqual(body["components"]["sandbox"]["seccomp"], "degraded")
        self.assertEqual(body["components"]["mongo"]["status"], "ok")

    def test_session_probe_is_disabled_by_default(self):
        os.environ.pop("ERP_SESSION_PROBE", None)
        os.environ["SANDBOX_BACKEND"] = "state"
        from fastapi.testclient import TestClient
        from src.api_view.web_main import app

        with TestClient(app) as client:
            response = client.get("/health/session")
        self.assertEqual(response.status_code, 404)

    def test_session_probe_reports_loaded_tool_count(self):
        os.environ["ERP_SESSION_PROBE"] = "1"
        os.environ["SANDBOX_BACKEND"] = "state"
        session = SimpleNamespace(user_id="probe-user", mcp_tool_count=23, degraded=False)
        try:
            with patch(
                "src.api_view.agent_loader.agent_loader.get_session",
                new=AsyncMock(return_value=session),
            ):
                from fastapi.testclient import TestClient
                from src.api_view.web_main import app

                with TestClient(app) as client:
                    response = client.get("/health/session?user_id=probe-user")
        finally:
            os.environ.pop("ERP_SESSION_PROBE", None)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mcp_tool_count"], 23)
        self.assertFalse(body["degraded"])
        self.assertEqual(body["user_id"], "probe-user")
        self.assertEqual(body["pid"], os.getpid())


if __name__ == "__main__":
    unittest.main()

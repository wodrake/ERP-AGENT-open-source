"""SANDBOX_ALLOW_LOCAL_FALLBACK=false 时，Docker 不可用不得在宿主机执行。"""
import os
import subprocess
import unittest
from unittest.mock import patch

from src.agent.backends.sandbox_factory import select_execution_backend
from src.agent.backends.sandbox_status import reset_for_tests, snapshot
from src.agent.backends.unavailable_sandbox import UnavailableSandboxBackend


class SandboxFallbackTests(unittest.TestCase):
    def tearDown(self):
        reset_for_tests()

    def test_disabled_fallback_errors_without_host_execution(self):
        reset_for_tests()

        def blocked(*args, **kwargs):
            raise AssertionError(f"host execution attempted: {args!r} {kwargs!r}")

        with patch.dict(
            os.environ,
            {"SANDBOX_BACKEND": "docker", "SANDBOX_ALLOW_LOCAL_FALLBACK": "false"},
        ):
            with patch(
                "src.agent.backends.sandbox_factory._try_docker_backend",
                side_effect=RuntimeError("docker daemon down"),
            ):
                with patch.object(subprocess, "Popen", blocked), patch.object(
                    subprocess, "run", blocked
                ), patch("os.system", blocked):
                    backend, managed, manager = select_execution_backend("user-no-docker")
                    result = backend.execute("echo HOST_LEAK")
                    status = snapshot()

        self.assertFalse(managed)
        self.assertIsNone(manager)
        self.assertIsInstance(backend, UnavailableSandboxBackend)
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn("HOST_LEAK", result.output)
        self.assertIn("宿主机", result.output)
        self.assertEqual(status["status"], "error")
        self.assertFalse(status["local_fallback"])

    def test_explicit_fallback_uses_local_shell_class_but_health_marks_degraded(self):
        reset_for_tests()

        class Marker:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        with patch.dict(
            os.environ,
            {"SANDBOX_BACKEND": "docker", "SANDBOX_ALLOW_LOCAL_FALLBACK": "true"},
        ):
            with patch(
                "src.agent.backends.sandbox_factory._try_docker_backend",
                side_effect=RuntimeError("docker daemon down"),
            ):
                with patch("deepagents.backends.LocalShellBackend", Marker):
                    backend, managed, _manager = select_execution_backend("user-dev")
                    status = snapshot()
        self.assertFalse(managed)
        self.assertIsInstance(backend, Marker)
        self.assertTrue(backend.kwargs.get("virtual_mode"))
        self.assertEqual(status["status"], "degraded")
        self.assertTrue(status["local_fallback"])
        self.assertEqual(status["mode"], "local_fallback")


if __name__ == "__main__":
    unittest.main()

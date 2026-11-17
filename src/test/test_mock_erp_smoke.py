"""不启动 compose 时，也能用 fixture 数据跑通零件查询和下单审批脚本。"""
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from deploy.mock_erp import MockERPServer

ROOT = Path(__file__).resolve().parents[2]


class MockERPSmokeTests(unittest.TestCase):
    def test_scripted_part_query_and_approval(self):
        server = MockERPServer("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = os.environ.copy()
        env["MOCK_ERP_URL"] = server.url
        env["SMOKE_REQUIRE_BACKEND"] = "0"
        try:
            completed = subprocess.run(
                [sys.executable, "scripts/smoke_procurement.py"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        finally:
            server.shutdown()
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + "\n" + completed.stderr,
        )
        self.assertIn("SMOKE OK", completed.stdout)
        self.assertIn("decision=approve", completed.stdout)


if __name__ == "__main__":
    unittest.main()

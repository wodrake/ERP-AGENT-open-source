"""统一测试入口。

默认只跑不依赖真实 Mongo / MCP / 模型 / Docker 的回归：
- src/test 里的 unittest（应不少于 87 项）
- src/test/test_memory_layer.py 的 34 项记忆测试

tests/live 下的脚本需要真实服务，默认不执行。设置 RUN_LIVE_TESTS=1 后再单独运行。
MCP 进程启停恢复在 tests/integration/test_mcp_process_recovery.py，由 CI 的 integration job 执行。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SANDBOX_BACKEND", "state")
os.environ.setdefault("SANDBOX_ALLOW_LOCAL_FALLBACK", "false")
os.environ.setdefault("ERP_LANGFUSE_ENABLED", "false")


def main() -> int:
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("src/test", pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    unit_ran = result.testsRun
    unit_failed = len(result.failures) + len(result.errors)

    import src.test.test_memory_layer as memory

    print("\n== memory layer ==")
    memory_failed = memory.main()
    memory_ran = len(memory._RESULTS)
    total = unit_ran + memory_ran
    print(
        f"\nTEST COUNTS unit={unit_ran} memory={memory_ran} total={total} "
        f"unit_failed={unit_failed} memory_failed={memory_failed}"
    )
    if unit_ran < 87 or memory_ran < 34 or total < 121:
        print("expected at least 87 unit tests, 34 memory tests, 121 total", file=sys.stderr)
        return 1
    if unit_failed or memory_failed or not result.wasSuccessful():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""真实进程：后端保持运行，停掉 MCP 再拉起，90 秒内新请求接回 23 个工具。

单独由 CI 的 integration job 执行，不进 `scripts/run_tests.py`。
需要本机或 CI 服务里已有 MongoDB。探针只在 ERP_SESSION_PROBE=1 时打开。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOLS = 23
RECOVERY_LIMIT_SECONDS = 90
BACKEND_PORT = int(os.getenv("RECOVERY_BACKEND_PORT", "18000"))
MCP_PORT = int(os.getenv("RECOVERY_MCP_PORT", "19000"))
MOCK_PORT = int(os.getenv("RECOVERY_MOCK_PORT", "18081"))
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
USER_ID = "mcp-process-recovery"


class RecoveryError(RuntimeError):
    pass


def _free(port: int) -> None:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RecoveryError(f"端口 {port} 已被占用")


def _wait_http(url: str, timeout: float, predicate=None):
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=min(5, timeout)) as response:
                body = response.read()
                if predicate is None or predicate(response.status, body):
                    return body
        except Exception as exc:
            last = exc
        time.sleep(0.3)
    raise RecoveryError(f"等待 {url} 超时: {last}")


def _get_json(url: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RecoveryError(f"HTTP {exc.code} {url}: {detail}") from exc


def _spawn(args: list[str], env: dict, log_path: Path) -> subprocess.Popen:
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        args,
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    proc.log_file = log  # type: ignore[attr-defined]
    return proc


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    log = getattr(proc, "log_file", None)
    if log is not None:
        log.close()


def _tail(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _base_env() -> dict:
    env = os.environ.copy()
    env.update({
        "SANDBOX_BACKEND": "state",
        "SANDBOX_ALLOW_LOCAL_FALLBACK": "false",
        "ERP_LANGFUSE_ENABLED": "false",
        # 只用于通过客户端构造，探针不会发起模型请求。不是真实密钥。
        "DEEPSEEK_API_KEY": "sk-integration-placeholder",
        "MONGODB_URI": MONGO_URI,
        "MONGODB_DB_NAME": "erp_agent_mcp_recovery",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def main() -> int:
    logs = Path(os.getenv("RECOVERY_LOG_DIR", "/tmp/mcp-recovery"))
    logs.mkdir(parents=True, exist_ok=True)
    for port in (BACKEND_PORT, MCP_PORT, MOCK_PORT):
        _free(port)
    python = sys.executable
    mock = mcp = backend = None
    try:
        mock_env = _base_env()
        mock_env["MOCK_ERP_HOST"] = "127.0.0.1"
        mock_env["MOCK_ERP_PORT"] = str(MOCK_PORT)
        mock = _spawn([python, "-m", "deploy.mock_erp"], mock_env, logs / "mock-erp.log")
        _wait_http(f"http://127.0.0.1:{MOCK_PORT}/health", 30)

        backend_env = _base_env()
        backend_env.update({
            "ERP_SESSION_PROBE": "1",
            "MCP_SERVER_URL": f"http://127.0.0.1:{MCP_PORT}",
            "ERP_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
            "MCP_DEGRADED_SESSION_TTL_SECONDS": os.getenv("MCP_DEGRADED_SESSION_TTL_SECONDS", "60"),
        })
        backend = _spawn(
            [python, "-m", "uvicorn", "src.api_view.web_main:app",
             "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
            backend_env,
            logs / "backend.log",
        )
        _wait_http(f"http://127.0.0.1:{BACKEND_PORT}/health", 60)
        if backend.poll() is not None:
            raise RecoveryError("后端在探针之前退出了")

        probe = f"http://127.0.0.1:{BACKEND_PORT}/health/session?user_id={USER_ID}"
        degraded = _get_json(probe, timeout=120)
        if degraded.get("mcp_tool_count") != 0 or not degraded.get("degraded"):
            raise RecoveryError(f"MCP 未启动时会话应降级为 0 个工具: {degraded}")
        backend_pid = int(degraded["pid"])

        mcp_env = _base_env()
        mcp_env.update({
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(MCP_PORT),
            "ERP_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
        })
        started = time.monotonic()
        mcp = _spawn([python, "-m", "src.mcp_server.server_main"], mcp_env, logs / "mcp.log")
        _wait_http(f"http://127.0.0.1:{MCP_PORT}/health", 30)
        mcp_ready = time.monotonic()

        recovered = None
        deadline = mcp_ready + RECOVERY_LIMIT_SECONDS
        while time.monotonic() < deadline:
            if backend.poll() is not None:
                raise RecoveryError("后端进程在恢复过程中退出了")
            payload = _get_json(probe, timeout=120)
            if int(payload.get("pid", -1)) != backend_pid:
                raise RecoveryError(f"后端 PID 变了: {backend_pid} -> {payload.get('pid')}")
            if payload.get("mcp_tool_count") == EXPECTED_TOOLS and not payload.get("degraded"):
                recovered = payload
                break
            time.sleep(1)
        if recovered is None:
            raise RecoveryError(
                f"MCP 恢复后 {RECOVERY_LIMIT_SECONDS} 秒内没有接回 {EXPECTED_TOOLS} 个工具"
            )
        elapsed = time.monotonic() - mcp_ready
        total = time.monotonic() - started
        print(
            f"MCP_RECOVERY_SECONDS={elapsed:.2f} "
            f"MCP_START_TO_READY_SECONDS={mcp_ready - started:.2f} "
            f"BACKEND_PID={backend_pid} MCP_TOOL_COUNT={EXPECTED_TOOLS} "
            f"TOTAL_AFTER_SPAWN_SECONDS={total:.2f}"
        )
        return 0
    except Exception as exc:
        print(f"RECOVERY FAIL: {exc}", file=sys.stderr)
        for name, proc in ("mock-erp", mock), ("mcp", mcp), ("backend", backend):
            print(f"----- {name} -----", file=sys.stderr)
            print(_tail(logs / f"{name}.log"), file=sys.stderr)
            if proc is not None:
                print(f"exit={proc.poll()}", file=sys.stderr)
        return 1
    finally:
        _stop(mcp)
        _stop(backend)
        _stop(mock)


if __name__ == "__main__":
    raise SystemExit(main())

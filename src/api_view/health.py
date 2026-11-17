"""聚合 MCP、Mongo、沙箱状态。探测失败要返回结构化状态，不要把异常抛成 500。"""
from __future__ import annotations

import asyncio
import os

import httpx

from ..agent.backends.sandbox_status import snapshot as sandbox_snapshot
from ..agent.config import MCP_SERVER_URL
from ..agent.tools.mcp_client import public_load_info


def _overall(components: dict) -> str:
    statuses = [item.get("status") for item in components.values()]
    if statuses and all(status == "ok" for status in statuses):
        return "ok"
    if any(status == "error" for status in statuses):
        return "error"
    return "degraded"


async def probe_mcp() -> dict:
    """只要 MCP 进程能给出 HTTP 响应就视为可达。SSE 挂起时改用短超时。"""
    url = os.getenv("MCP_SERVER_URL", MCP_SERVER_URL).rstrip("/") + "/health"
    info = public_load_info()
    tool_count = info["count"] if info.get("loaded") else None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
            response = await client.get(url)
        return {
            "status": "ok",
            "detail": f"reachable HTTP {response.status_code}",
            "last_tool_count": tool_count,
        }
    except Exception as exc:
        return {
            "status": "error",
            "detail": f"{exc.__class__.__name__}: {exc}"[:300],
            "last_tool_count": tool_count,
        }


async def probe_mongo() -> dict:
    def ping() -> None:
        from pymongo import MongoClient

        from .web_config import MONGODB_DB_NAME, MONGODB_URI

        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=2000)
        try:
            client.admin.command("ping")
            client[MONGODB_DB_NAME].command("ping")
        finally:
            client.close()

    try:
        await asyncio.to_thread(ping)
        return {"status": "ok", "detail": "ping ok"}
    except Exception as exc:
        return {"status": "error", "detail": f"{exc.__class__.__name__}: {exc}"[:300]}


def probe_sandbox() -> dict:
    return sandbox_snapshot()


async def collect_health() -> dict:
    mcp, mongo = await asyncio.gather(probe_mcp(), probe_mongo())
    sandbox = probe_sandbox()
    components = {"mcp": mcp, "mongo": mongo, "sandbox": sandbox}
    return {"status": _overall(components), "components": components}

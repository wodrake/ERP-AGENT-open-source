"""MCP 写工具幂等。

ERP 不接受幂等键时，用 approval_id 在 MongoDB 去重表 write_idempotency 中只保留首次结果。
MongoDB 不可用时退回进程内表，避免同进程重复写入；多进程部署需要可用的 MongoDB。
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol


mcp_logger = logging.getLogger("mcp")
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "erp_agent")


_approval_id: contextvars.ContextVar[str] = contextvars.ContextVar("erp_approval_id", default="")


def bind_approval_id(approval_id: str):
    """供审批中间件在工具执行期间把中断生成的幂等键传进 MCP 工具。"""
    return _approval_id.set(approval_id or "")


def reset_approval_id(token) -> None:
    _approval_id.reset(token)


def effective_approval_id(explicit: str | None) -> str:
    text = str(explicit or "").strip()
    if text:
        return text
    return _approval_id.get().strip()


class IdempotencyStore(Protocol):
    def replay(self, approval_id: str) -> str | None: ...

    def claim(self, approval_id: str, tool_name: str, snapshot: Any) -> bool: ...

    def complete(self, approval_id: str, result: str) -> None: ...


class MemoryIdempotencyStore:
    """单进程去重表。测试和 Mongo 不可用时使用。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def replay(self, approval_id: str) -> str | None:
        row = self._rows.get(approval_id)
        if row and row.get("status") == "done":
            return row.get("result")
        return None

    def claim(self, approval_id: str, tool_name: str, snapshot: Any) -> bool:
        if approval_id in self._rows:
            return False
        self._rows[approval_id] = {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "snapshot": snapshot,
            "status": "pending",
            "result": None,
            "created_at": datetime.now().isoformat(),
        }
        return True

    def complete(self, approval_id: str, result: str) -> None:
        row = self._rows.setdefault(approval_id, {"approval_id": approval_id, "status": "pending"})
        row["status"] = "done"
        row["result"] = result
        row["completed_at"] = datetime.now().isoformat()

    def clear(self) -> None:
        self._rows.clear()


class MongoIdempotencyStore:
    def __init__(self, client, db_name: str) -> None:
        self._col = client[db_name]["write_idempotency"]
        self._col.create_index("approval_id", unique=True)

    def replay(self, approval_id: str) -> str | None:
        doc = self._col.find_one({"approval_id": approval_id, "status": "done"})
        if not doc:
            return None
        return doc.get("result")

    def claim(self, approval_id: str, tool_name: str, snapshot: Any) -> bool:
        from pymongo.errors import DuplicateKeyError

        try:
            self._col.insert_one({
                "approval_id": approval_id,
                "tool_name": tool_name,
                "snapshot": snapshot,
                "status": "pending",
                "result": None,
                "created_at": datetime.now().isoformat(),
            })
            return True
        except DuplicateKeyError:
            return False

    def complete(self, approval_id: str, result: str) -> None:
        self._col.update_one(
            {"approval_id": approval_id},
            {"$set": {"status": "done", "result": result, "completed_at": datetime.now().isoformat()}},
        )


_store: IdempotencyStore | None = None


def set_idempotency_store(store: IdempotencyStore | None) -> None:
    global _store
    _store = store


def get_idempotency_store() -> IdempotencyStore:
    global _store
    if _store is None:
        _store = _build_default_store()
    return _store


def _build_default_store() -> IdempotencyStore:
    backend = os.getenv("APPROVAL_IDEMPOTENCY_BACKEND", "auto").lower()
    if backend == "memory":
        return MemoryIdempotencyStore()
    try:
        from pymongo import MongoClient

        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=400)
        client.admin.command("ping")
        mcp_logger.info("写操作幂等使用 MongoDB 去重表 write_idempotency")
        return MongoIdempotencyStore(client, MONGODB_DB_NAME)
    except Exception as exc:
        mcp_logger.warning(
            f"MongoDB 去重表不可用，写操作幂等退回进程内表 ({type(exc).__name__})"
        )
        return MemoryIdempotencyStore()


def _conflict(approval_id: str) -> str:
    return json.dumps(
        {"code": 409, "message": "相同审批正在执行，未再次写入 ERP", "approval_id": approval_id, "data": None},
        ensure_ascii=False,
    )


async def run_idempotent(
    approval_id: str | None,
    tool_name: str,
    snapshot: Any,
    operation: Callable[[], Awaitable[str]],
) -> str:
    """同一非空 approval_id 只执行一次 operation，重复调用返回首次结果。"""
    approval_id = effective_approval_id(approval_id)
    if not approval_id:
        return await operation()
    store = get_idempotency_store()
    cached = store.replay(approval_id)
    if cached is not None:
        mcp_logger.info(f"幂等命中，跳过 ERP 写入: {tool_name} {approval_id}")
        return cached
    if not store.claim(approval_id, tool_name, snapshot):
        cached = store.replay(approval_id)
        return cached if cached is not None else _conflict(approval_id)
    try:
        result = await operation()
    except Exception as exc:
        # 请求可能已经到达 ERP。记下失败结果，避免同一个审批再写一次。
        result = json.dumps(
            {"code": 500, "message": f"ERP 写入失败: {type(exc).__name__}", "approval_id": approval_id, "data": None},
            ensure_ascii=False,
        )
    store.complete(approval_id, result)
    return result

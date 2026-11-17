"""审批审计。

MongoDB `approvals` 集合记录每一次 approve / reject / edit。
请求人和审批人在 R1 都取请求里的 user_id，并标注「未认证」；可信身份留给 R2。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Protocol

from .config import MONGODB_DB_NAME, MONGODB_URI
from .log_utils import agent_logger


UNAUTHENTICATED = "未认证"
# 请求里的 user_id 尚未经过认证。R2 再替换成令牌中的身份。
IDENTITY_NOTE = "请求中的 user_id，未认证；可信身份留给后续 R2"

APPROVAL_FIELDS = (
    "approval_id",
    "thread_id",
    "tool_name",
    "args_snapshot",
    "args_hash",
    "risk_level",
    "requester_user_id",
    "requester_auth_status",
    "approver_user_id",
    "approver_auth_status",
    "decision",
    "edit_diff",
    "requested_at",
    "decided_at",
    "erp_result",
    "identity_note",
)


def canonical_snapshot(args: dict | None) -> dict:
    """去掉幂等键，并把订单 JSON 字符串解析成对象，便于哈希和比对。"""
    if not isinstance(args, dict):
        return {}
    snapshot: dict[str, Any] = {}
    for key, value in args.items():
        if key == "approval_id":
            continue
        if key in ("order_data", "orderData") and isinstance(value, str):
            try:
                snapshot[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        snapshot[key] = value
    return snapshot


def args_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def deep_diff(before: Any, after: Any, prefix: str = "") -> dict:
    changes: dict[str, dict] = {}
    if isinstance(before, dict) and isinstance(after, dict):
        for key in set(before) | set(after):
            changes.update(deep_diff(before.get(key), after.get(key), f"{prefix}{key}."))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            left = before[index] if index < len(before) else None
            right = after[index] if index < len(after) else None
            changes.update(deep_diff(left, right, f"{prefix}{index}."))
        return changes
    if before != after:
        changes[prefix[:-1] if prefix.endswith(".") else prefix or "$"] = {"before": before, "after": after}
    return changes


def build_edit_diff(before: dict, after: dict) -> dict:
    return {"before": before, "after": after, "changed": deep_diff(before, after)}


def is_terminal(record: dict) -> bool:
    if record.get("decision") == "reject":
        return True
    return record.get("erp_result") is not None


class ApprovalStore(Protocol):
    def record(self, doc: dict) -> dict: ...

    def attach_result(self, approval_id: str, erp_result: Any) -> dict | None: ...

    def get(self, approval_id: str) -> dict | None: ...

    def latest_terminal(self, thread_id: str) -> dict | None: ...


class MemoryApprovalStore:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, doc: dict) -> dict:
        stored = dict(doc)
        self.records.append(stored)
        return dict(stored)

    def attach_result(self, approval_id: str, erp_result: Any) -> dict | None:
        for record in self.records:
            if record.get("approval_id") == approval_id and record.get("erp_result") is None:
                record["erp_result"] = erp_result
                return dict(record)
        return None

    def get(self, approval_id: str) -> dict | None:
        for record in self.records:
            if record.get("approval_id") == approval_id:
                return dict(record)
        return None

    def latest_terminal(self, thread_id: str) -> dict | None:
        found = None
        for record in self.records:
            if record.get("thread_id") == thread_id and is_terminal(record):
                found = record
        return dict(found) if found else None

    def clear(self) -> None:
        self.records.clear()


class MongoApprovalStore:
    def __init__(self, client, db_name: str, collection_name: str = "approvals") -> None:
        self._col = client[db_name][collection_name]
        self._col.create_index("approval_id")
        self._col.create_index([("thread_id", 1), ("decided_at", 1)])

    def record(self, doc: dict) -> dict:
        stored = dict(doc)
        self._col.insert_one(stored)
        return dict(doc)

    def attach_result(self, approval_id: str, erp_result: Any) -> dict | None:
        self._col.update_one(
            {"approval_id": approval_id, "erp_result": None},
            {"$set": {"erp_result": erp_result}},
        )
        return self.get(approval_id)

    def get(self, approval_id: str) -> dict | None:
        doc = self._col.find_one({"approval_id": approval_id})
        return _public_doc(doc)

    def latest_terminal(self, thread_id: str) -> dict | None:
        cursor = self._col.find({"thread_id": thread_id}).sort("decided_at", 1)
        found = None
        for doc in cursor:
            if is_terminal(doc):
                found = doc
        return _public_doc(found)


def _public_doc(doc: dict | None) -> dict | None:
    if not doc:
        return None
    copied = dict(doc)
    copied.pop("_id", None)
    return copied


_store: ApprovalStore | None = None
_pending: dict[str, dict] = {}
_pending_by_call: dict[str, str] = {}


def set_approval_store(store: ApprovalStore | None) -> None:
    global _store
    _store = store


def get_approval_store() -> ApprovalStore:
    global _store
    if _store is None:
        _store = _build_default_store()
    return _store


def _build_default_store() -> ApprovalStore:
    backend = os.getenv("APPROVAL_STORE_BACKEND", "auto").lower()
    if backend == "memory":
        return MemoryApprovalStore()
    try:
        from pymongo import MongoClient

        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=400)
        client.admin.command("ping")
        agent_logger.info("审批审计写入 MongoDB approvals")
        return MongoApprovalStore(client, MONGODB_DB_NAME)
    except Exception as exc:
        agent_logger.warning(f"MongoDB approvals 不可用，审批记录退回进程内 ({type(exc).__name__})")
        return MemoryApprovalStore()


def reset_approval_runtime() -> None:
    _pending.clear()
    _pending_by_call.clear()


def remember_pending(pending: dict) -> None:
    approval_id = pending["approval_id"]
    _pending[approval_id] = pending
    tool_call_id = pending.get("tool_call_id")
    if tool_call_id:
        _pending_by_call[tool_call_id] = approval_id


def pop_pending_by_call(tool_call_id: str | None) -> dict | None:
    if not tool_call_id:
        return None
    approval_id = _pending_by_call.pop(tool_call_id, None)
    if not approval_id:
        return None
    return _pending.pop(approval_id, None)


def record_decision(pending: dict, *, decision: str, edit_diff: dict | None, approver_user_id: str) -> dict:
    """落一条字段齐全的审批记录。erp_result 在工具返回后由 attach_erp_result 补上。"""
    now = datetime.now().isoformat()
    doc = {
        "approval_id": pending["approval_id"],
        "thread_id": pending.get("thread_id") or "",
        "tool_name": pending["tool_name"],
        "args_snapshot": pending["args_snapshot"],
        "args_hash": pending["args_hash"],
        "risk_level": pending["risk_level"],
        "requester_user_id": pending.get("requester_user_id") or "unknown",
        "requester_auth_status": UNAUTHENTICATED,
        "approver_user_id": approver_user_id or "unknown",
        "approver_auth_status": UNAUTHENTICATED,
        "decision": decision,
        "edit_diff": edit_diff,
        "requested_at": pending.get("requested_at") or now,
        "decided_at": now,
        "erp_result": None,
        "identity_note": IDENTITY_NOTE,
    }
    missing = [field for field in APPROVAL_FIELDS if field not in doc]
    if missing:
        raise RuntimeError(f"审批记录缺少字段: {missing}")
    return get_approval_store().record(doc)


def attach_erp_result(approval_id: str, erp_result: Any) -> dict | None:
    if not approval_id:
        return None
    return get_approval_store().attach_result(approval_id, erp_result)


def latest_terminal(thread_id: str) -> dict | None:
    if not thread_id:
        return None
    return get_approval_store().latest_terminal(thread_id)

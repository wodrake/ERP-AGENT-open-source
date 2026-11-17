"""审批记录和写幂等的存储选择。

默认必须连上 MongoDB。连不上就拒绝写 ERP。
只有显式设置 APPROVAL_STORE_ALLOW_MEMORY=1 时才退回进程内表，并且仅限本地开发。
"""
from __future__ import annotations

import os

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "erp_agent")

REFUSAL_MESSAGE = (
    "MongoDB 不可用，已拒绝写入 ERP。请确认 MongoDB 已启动并可连接；"
    "仅本地开发可设置 APPROVAL_STORE_ALLOW_MEMORY=1 使用进程内表。"
)

STARTUP_WARNING = (
    "APPROVAL_STORE_ALLOW_MEMORY=1：审批记录与写幂等允许在 MongoDB 不可用时退回进程内表，仅限本地开发。"
    "多用户上线前必须完成 R2（身份认证），上线环境必须使用 MongoDB，不能打开此开关。"
)


class ApprovalPersistenceUnavailable(RuntimeError):
    """MongoDB 不可用，且未允许内存回退。调用方不得写 ERP。"""


def memory_fallback_allowed() -> bool:
    return os.getenv("APPROVAL_STORE_ALLOW_MEMORY", "").strip().lower() in {"1", "true", "yes"}


def warn_if_memory_fallback_enabled(logger) -> None:
    """进程启动时调用。打开内存回退就要留下告警。"""
    if memory_fallback_allowed():
        logger.warning(STARTUP_WARNING)


def open_mongo(uri: str | None = None, timeout_ms: int = 400):
    from pymongo import MongoClient

    client = MongoClient(uri or MONGODB_URI, serverSelectionTimeoutMS=timeout_ms)
    client.admin.command("ping")
    return client


def resolve_store(mongo_factory, memory_factory, *, logger, component: str):
    """优先 MongoDB。失败时按开关决定退回内存或拒绝写操作。"""
    try:
        client = open_mongo()
    except Exception as exc:
        if not memory_fallback_allowed():
            raise ApprovalPersistenceUnavailable(f"{REFUSAL_MESSAGE}（{type(exc).__name__}）") from exc
        logger.warning(
            "APPROVAL_STORE_ALLOW_MEMORY=1：%s 退回进程内表（MongoDB 不可用：%s）。仅限本地开发。",
            component,
            type(exc).__name__,
        )
        return memory_factory()
    return mongo_factory(client)

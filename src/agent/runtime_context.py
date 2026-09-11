"""每个流式 Agent 请求的轻量运行时上下文。

LangChain 工具实例在 Agent 构建时创建，不能把特定用户的 Store 或用户 ID
闭包进全局工具对象；ContextVar 让同一个工具在并发请求中仍能读取自己的上下文。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


_current_user_id: ContextVar[str] = ContextVar(
    "erp_agent_current_user_id", default="default_user"
)
_current_store: ContextVar[Any | None] = ContextVar(
    "erp_agent_current_store", default=None
)


def bind_runtime(user_id: str, store: Any | None) -> tuple[Token[str], Token[Any | None]]:
    """绑定用户与 Store，供在本请求中运行的工具读取。"""
    return _current_user_id.set(user_id), _current_store.set(store)


def reset_runtime(tokens: tuple[Token[str], Token[Any | None]]) -> None:
    """复位 ``bind_runtime`` 产生的上下文。"""
    user_token, store_token = tokens
    _current_user_id.reset(user_token)
    _current_store.reset(store_token)


def get_current_user_id() -> str:
    return _current_user_id.get()


def get_current_store() -> Any | None:
    return _current_store.get()

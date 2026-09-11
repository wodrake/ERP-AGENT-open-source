"""请求级沙箱绑定。

工具函数（图表、文档、Skill 安装器）不能把某个用户的容器保存在普通模块全局中：
FastAPI 的多个 SSE 请求会并发执行，普通全局会造成 A 用户的工具写入 B 用户沙箱。

这里同时维护用户 → 稳定 Proxy 的注册表和 ``ContextVar``。Agent 创建时注册 Proxy；
每个流式请求开始时绑定它，工具在当前协程上下文中取到正确实例。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from threading import RLock
from typing import Any


DEFAULT_USER_ID = "default_user"
_request_sandbox: ContextVar[Any | None] = ContextVar(
    "erp_agent_request_sandbox", default=None
)
_sandboxes_by_user: dict[str, Any] = {}
_registry_lock = RLock()


def set_sandbox(sandbox: Any | None, user_id: str = DEFAULT_USER_ID) -> None:
    """注册或移除某位用户的稳定沙箱 Proxy。"""
    with _registry_lock:
        if sandbox is None:
            _sandboxes_by_user.pop(user_id, None)
        else:
            _sandboxes_by_user[user_id] = sandbox


def get_sandbox(user_id: str | None = None) -> Any | None:
    """获取当前请求的沙箱；非请求上下文时可按用户查注册表。"""
    sandbox = _request_sandbox.get()
    if sandbox is not None:
        return sandbox
    with _registry_lock:
        return _sandboxes_by_user.get(user_id or DEFAULT_USER_ID)


def bind_sandbox(user_id: str) -> Token[Any | None]:
    """将某位用户的沙箱绑定到当前协程，返回可在 finally 中复位的 token。"""
    with _registry_lock:
        sandbox = _sandboxes_by_user.get(user_id)
    return _request_sandbox.set(sandbox)


def reset_sandbox(token: Token[Any | None]) -> None:
    """结束请求时恢复上层 ContextVar 值。"""
    _request_sandbox.reset(token)


def has_sandbox(user_id: str | None = None) -> bool:
    """检查当前请求（或指定用户）的沙箱是否可用。"""
    sandbox = get_sandbox(user_id)
    if sandbox is None:
        return False
    try:
        return bool(sandbox.ping())
    except Exception:
        return False

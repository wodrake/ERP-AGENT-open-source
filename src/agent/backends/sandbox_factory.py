"""选择代码执行后端。

默认尝试 Docker。失败时只有 SANDBOX_ALLOW_LOCAL_FALLBACK=true 才使用宿主机 Shell。
CI 和 compose 默认可以设置 SANDBOX_BACKEND=state，完全不接触 Docker。
"""
from __future__ import annotations

from .sandbox_status import (
    note_local_fallback,
    note_state,
    note_unavailable,
)
from ..config import sandbox_allow_local_fallback, sandbox_backend_mode
from ..log_utils import agent_logger


def _try_docker_backend(user_id: str):
    from .sandbox_manager import sandbox_manager
    from .sandbox_proxy import SandboxBackendProxy

    raw_sandbox = sandbox_manager.get_sandbox(user_id)
    return SandboxBackendProxy(raw_sandbox), sandbox_manager


def fallback_backend(reason: BaseException):
    """Docker 或沙箱初始化失败后的后端。默认不在宿主机执行。"""
    message = f"{reason.__class__.__name__}: {reason}"
    if sandbox_allow_local_fallback():
        from deepagents.backends import LocalShellBackend

        note_local_fallback(message)
        agent_logger.warning(
            "Docker sandbox unavailable (%s); SANDBOX_ALLOW_LOCAL_FALLBACK=true, "
            "using LocalShellBackend on the host",
            message,
        )
        return LocalShellBackend(virtual_mode=True)

    from .unavailable_sandbox import UnavailableSandboxBackend

    note_unavailable(message)
    agent_logger.error(
        "Docker sandbox unavailable (%s); refusing host execution because "
        "SANDBOX_ALLOW_LOCAL_FALLBACK is false",
        message,
    )
    return UnavailableSandboxBackend(message)


def select_execution_backend(user_id: str):
    """返回 (backend, managed, sandbox_manager)。

    managed 为 True 时，调用方还要做项目文件引导；引导失败也应走 fallback_backend。
    """
    if sandbox_backend_mode() == "state":
        from deepagents.backends import StateBackend

        note_state()
        agent_logger.info("SANDBOX_BACKEND=state; code execution stays off the host")
        return StateBackend(), False, None

    try:
        proxy, manager = _try_docker_backend(user_id)
        agent_logger.info(
            "Using managed Docker sandbox for %s: %s",
            user_id,
            getattr(proxy, "container_name", "docker"),
        )
        return proxy, True, manager
    except Exception as exc:
        return fallback_backend(exc), False, None

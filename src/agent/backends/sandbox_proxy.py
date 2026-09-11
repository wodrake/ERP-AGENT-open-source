"""
可热替换的沙箱代理。

DeepAgents 会在 Agent 创建时持有 Backend 对象。沙箱故障后如果直接把
CustomOpenSandbox 换成新实例，CompositeBackend、文件工具和自定义工具都会
继续引用旧对象。该 Proxy 自身是一个 BaseSandbox，因此对外句柄稳定，内部
可以原子地切换到底层新容器。
"""
from __future__ import annotations

from threading import RLock
from typing import Any, Optional

from deepagents.backends.sandbox import (
    BaseSandbox,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)

from ..log_utils import sandbox_logger


class SandboxBackendProxy(BaseSandbox):
    """为 DeepAgents 提供稳定的、可热替换的 ``BaseSandbox`` 句柄。"""

    def __init__(self, backend: Optional[BaseSandbox] = None):
        self._backend = backend
        self._lock = RLock()
        self._generation = 0

    @property
    def backend(self) -> BaseSandbox:
        """获取当前后端快照；调用方不应缓存返回值。"""
        with self._lock:
            if self._backend is None:
                raise RuntimeError("No sandbox backend available")
            return self._backend

    @property
    def generation(self) -> int:
        """当前热替换代次，便于日志与测试判断。"""
        with self._lock:
            return self._generation

    @property
    def id(self) -> str:
        return self.backend.id

    @property
    def container_name(self) -> str:
        return str(getattr(self.backend, "container_name", ""))

    @property
    def container_id(self) -> str:
        return str(getattr(self.backend, "container_id", ""))

    def replace_backend(self, new_backend: BaseSandbox) -> None:
        """原子替换底层容器连接，保持 Proxy 对象本身不变。"""
        if new_backend is None:
            raise ValueError("new_backend must not be None")

        with self._lock:
            old_backend = self._backend
            self._backend = new_backend
            self._generation += 1

        # Manager 重建时已经负责删除旧容器；这里仅尽力关闭旧 SDK 连接。
        if old_backend is not None and old_backend is not new_backend:
            try:
                destroy = getattr(old_backend, "destroy", None)
                if callable(destroy):
                    destroy()
            except Exception as exc:  # 不应阻断新容器接管
                sandbox_logger.warning(f"Failed to disconnect old sandbox backend: {exc}")

        sandbox_logger.info(
            "Sandbox backend hot-swapped "
            f"(generation={self.generation}, id={getattr(new_backend, 'id', 'unknown')})"
        )

    # BaseSandbox 的三个抽象能力。其余标准文件操作由 BaseSandbox 基于这些能力实现。
    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        return self.backend.execute(command, timeout=timeout)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.backend.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self.backend.download_files(paths)

    # 下面是项目已有工具/中间件使用的扩展接口，继续透明委托给真实实现。
    def ping(self) -> bool:
        try:
            ping = getattr(self.backend, "ping", None)
            if callable(ping):
                return bool(ping())
            return self.execute("echo ok", timeout=5).exit_code == 0
        except Exception:
            return False

    def destroy(self) -> None:
        """只断开当前连接；容器销毁由 SandboxManager 负责。"""
        try:
            destroy = getattr(self.backend, "destroy", None)
            if callable(destroy):
                destroy()
        except RuntimeError:
            return

    def __getattr__(self, name: str) -> Any:
        """兼容项目的 ``write_file``、``file_exists``、``cp`` 等扩展方法。"""
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.backend, name)

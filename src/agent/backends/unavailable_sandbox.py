"""Docker 不可用且禁止宿主机回退时的执行后端。

所有命令都在进程内直接返回错误，不调用 subprocess / os.system。
"""
from deepagents.backends.sandbox import (
    BaseSandbox,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)


class UnavailableSandboxBackend(BaseSandbox):
    """代码执行类工具的明确失败后端。"""

    def __init__(self, reason: str):
        self._reason = (reason or "Docker sandbox unavailable").strip()
        self.container_name = "sandbox-unavailable"
        self.container_id = "unavailable"

    @property
    def id(self) -> str:
        return "unavailable"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        del command, timeout
        return ExecuteResponse(
            output=(
                "[沙箱不可用] Docker 沙箱无法使用"
                f"（{self._reason[:300]}）。"
                "SANDBOX_ALLOW_LOCAL_FALLBACK=false，已拒绝在宿主机执行该命令。"
                "请启动 Docker，或仅在本地开发时显式打开回退开关。"
            ),
            exit_code=126,
            truncated=False,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error="sandbox_unavailable")
            for path, _content in files
        ]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [
            FileDownloadResponse(path=path, content=None, error="sandbox_unavailable")
            for path in paths
        ]

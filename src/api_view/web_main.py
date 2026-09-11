"""
FastAPI 应用入口
CORS 全开、路由注册、startup/shutdown 事件
"""
import sys
import os
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager, suppress

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .api.chat import router as chat_router
from .api.history import router as history_router
from .web_config import close_mongo_client
from ..agent.log_utils import web_logger
from ..agent.backends.sandbox_manager import sandbox_manager
from ..agent.config import SANDBOX_MAINTENANCE_INTERVAL_SECONDS


async def _sandbox_maintenance(stop_event: asyncio.Event) -> None:
    """后台补齐预热池、回收空闲容器。

    主请求中的 HealthMiddleware 才负责“重建 + Proxy 热替换 + 文件回填”；此处
    绝不直接调用 ``health_check_all``，否则 Agent 仍会持有旧后端引用。
    """
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(sandbox_manager.cleanup_idle_sandboxes)
            await asyncio.to_thread(sandbox_manager.ensure_warm_pool)
        except Exception as exc:
            web_logger.warning(f"Sandbox maintenance iteration failed: {exc}")

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=SANDBOX_MAINTENANCE_INTERVAL_SECONDS
            )
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    web_logger.info("Starting ERP Agent Web Server...")
    stop_event = asyncio.Event()
    maintenance_task = None
    try:
        # 预热失败不阻止 Web 服务启动；首个请求仍会按需创建并回退到 LocalShell。
        await asyncio.to_thread(sandbox_manager.ensure_warm_pool)
        maintenance_task = asyncio.create_task(
            _sandbox_maintenance(stop_event), name="sandbox-maintenance"
        )
        yield
    finally:
        web_logger.info("Shutting down ERP Agent Web Server...")
        stop_event.set()
        if maintenance_task is not None:
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task
        # 不销毁已认领容器：Manager 会把 user → container 映射持久化，重启后可恢复。
        await close_mongo_client()


app = FastAPI(
    title="DeepAgent 智能采购助手",
    description="基于 Harness Engineering 架构的摩托车零部件采购智能助手 API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 全开（开发环境）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(chat_router)
app.include_router(history_router)

# 文件下载目录（图表等生成文件）
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "download"
DOWNLOAD_DIR.mkdir(exist_ok=True)


@app.get("/api/download/{filename}")
async def download_file(filename: str):
    """提供生成文件（图表PNG等）的HTTP下载"""
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"文件不存在: {filename}")
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream",
    )


@app.get("/")
async def root():
    return {"message": "DeepAgent 智能采购助手 API", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

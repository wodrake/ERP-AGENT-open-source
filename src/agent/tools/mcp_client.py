"""
MCP 工具加载模块
使用 langchain-mcp-adapters 连接 MCP Server（SSE模式），获取所有 ERP 工具

生产级实现：
- 连接重试（指数退避）
- 连接健康检查
- 优雅降级（MCP不可用时返回空工具列表，Agent仍可对话）
"""
import asyncio
import threading
import time
from typing import List
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from ..config import MCP_SERVER_URL, MCP_SSE_URL
from ..log_utils import mcp_logger

# 重试配置
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # 秒
RETRY_BACKOFF_FACTOR = 2.0

# 缓存（避免重复连接）
_cached_tools: List[BaseTool] | None = None
_cache_time: float = 0
CACHE_TTL = 300  # 5分钟缓存
_load_tls = threading.local()
_public_lock = threading.Lock()
_public_last = {"count": 0, "degraded": True, "loaded": False}


def _remember_load(tools: List[BaseTool]) -> List[BaseTool]:
    """记录本次加载结果。空列表不写入缓存，会话层会据此做降级 TTL。"""
    info = {"count": len(tools), "degraded": len(tools) == 0, "loaded": True}
    _load_tls.last = info
    with _public_lock:
        _public_last.update(info)
    return tools


def last_load_info() -> dict:
    """供 AgentLoader 在构建线程内读取 MCP 工具数。"""
    info = getattr(_load_tls, "last", None)
    if not isinstance(info, dict):
        return {"count": 0, "degraded": True, "loaded": False}
    return {
        "count": int(info.get("count", 0)),
        "degraded": bool(info.get("degraded", True)),
        "loaded": bool(info.get("loaded", False)),
    }


def public_load_info() -> dict:
    """进程内最近一次加载结果，给 /health 用。"""
    with _public_lock:
        return dict(_public_last)


async def load_mcp_tools(force_refresh: bool = False) -> List[BaseTool]:
    """
    异步加载 MCP 工具列表（带重试和缓存）.

    Args:
        force_refresh: 强制刷新缓存

    Returns:
        MCP 工具列表，连接失败时返回空列表
    """
    global _cached_tools, _cache_time

    # 检查缓存
    if not force_refresh and _cached_tools is not None:
        if time.time() - _cache_time < CACHE_TTL:
            return _remember_load(_cached_tools)

    mcp_logger.info(f"Connecting to MCP Server: {MCP_SSE_URL}")

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            client = MultiServerMCPClient(
                {
                    "erp": {
                        "url": MCP_SSE_URL,
                        "transport": "sse",
                    }
                }
            )

            tools = await client.get_tools()

            if tools:
                _cached_tools = tools
                _cache_time = time.time()
                mcp_logger.info(
                    f"Loaded {len(tools)} MCP tools: {[t.name for t in tools]}"
                )
                return _remember_load(tools)
            else:
                mcp_logger.warning("MCP Server returned 0 tools")
                return _remember_load([])

        except Exception as e:
            last_error = e
            delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
            mcp_logger.warning(
                f"MCP connection attempt {attempt + 1}/{MAX_RETRIES} failed: {e}. "
                f"Retrying in {delay:.1f}s..."
            )
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(delay)

    mcp_logger.error(
        f"All {MAX_RETRIES} MCP connection attempts failed. Last error: {last_error}. "
        f"Agent will operate without ERP tools."
    )
    return _remember_load([])


def load_mcp_tools_sync(force_refresh: bool = False) -> List[BaseTool]:
    """
    同步包装：加载 MCP 工具列表

    处理各种事件循环状态：
    - 无事件循环 → asyncio.run()
    - 有运行中的事件循环 → ThreadPoolExecutor + asyncio.run()
    """
    # 先检查缓存
    global _cached_tools, _cache_time
    if not force_refresh and _cached_tools is not None:
        if time.time() - _cache_time < CACHE_TTL:
            return _remember_load(_cached_tools)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # 在异步上下文中（如 FastAPI startup）。线程池里的记录不会回到当前线程。
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, load_mcp_tools(force_refresh))
            return _remember_load(future.result(timeout=30))
    else:
        # 无运行中的事件循环
        return _remember_load(asyncio.run(load_mcp_tools(force_refresh)))


def invalidate_cache():
    """清除工具缓存（MCP Server 重启后调用）"""
    global _cached_tools, _cache_time
    _cached_tools = None
    _cache_time = 0
    mcp_logger.info("MCP tools cache invalidated")

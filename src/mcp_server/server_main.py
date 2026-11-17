"""
MCP Server 入口
FastMCP 实例创建，注册所有 tools，SSE 传输协议
"""
import sys
import os

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from src.mcp_server.server_config import MCP_HOST, MCP_PORT
from src.mcp_server.tools.suppliers_tools import register_supplier_tools
from src.mcp_server.tools.parts_tools import register_parts_tools
from src.mcp_server.tools.order_tools import register_order_tools
from src.mcp_server.tools.inventory_tools import register_inventory_tools

# 创建 MCP Server 实例
mcp = FastMCP(
    name="ERP-Procurement-MCP",
    instructions="摩托车零部件采购管理系统 MCP 网关，提供供应商、零部件、订单、库存查询和管理能力。",
)

# 注册所有工具
register_supplier_tools(mcp)
register_parts_tools(mcp)
register_order_tools(mcp)
register_inventory_tools(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def mcp_health(_request: Request) -> JSONResponse:
    """供 compose 和后端 /health 探测，避免打到会挂起的 SSE。"""
    return JSONResponse({"status": "ok", "service": "mcp"})


def audit_registered_tools() -> None:
    """启动自检：名字像写操作、却未进入 WRITE_TOOLS 的工具会告警或拒绝启动。"""
    import asyncio
    from src.agent.mcp_tools_bean import enforce_write_tool_coverage

    async def _names() -> list[str]:
        tools = await mcp.list_tools()
        return [tool.name for tool in tools]

    enforce_write_tool_coverage(asyncio.run(_names()))


if __name__ == "__main__":
    audit_registered_tools()
    from src.mcp_server.persistence import warn_if_memory_fallback_enabled
    import logging
    warn_if_memory_fallback_enabled(logging.getLogger("mcp"))
    print(f"🚀 Starting MCP Server on {MCP_HOST}:{MCP_PORT} (SSE transport)")
    mcp.run(transport="sse", host=MCP_HOST, port=MCP_PORT)

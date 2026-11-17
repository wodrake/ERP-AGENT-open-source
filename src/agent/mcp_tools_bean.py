"""
MCP 工具分类 Bean
将 MCP 工具按用途分组：analyst_tools / order_tools。
写工具清单 WRITE_TOOLS 是主 Agent 和全部子 Agent interrupt_on 的唯一来源。
"""
from __future__ import annotations

import os
from typing import Iterable, List

from langchain_core.tools import BaseTool

from .log_utils import agent_logger


# 分析子Agent使用的工具名关键字
ANALYST_TOOL_PATTERNS = [
    "supplier_query",
    "supplier_page",
    "supplier_get",
    "part_search",
    "part_page",
    "part_query",
    "part_by_supplier",
    "inventory_warning",
    "inventory_page",
    "inventory_check",
    "inventory_get",
    "order_search_details",
    "order_statistics",
    "order_page",
    "order_get",
    "generate_chart",
    "web_search",
]

# 订单子Agent使用的工具名关键字
ORDER_TOOL_PATTERNS = [
    "order_create",
    "order_update",
    "order_get",
    "order_page",
    "order_update_status",
    "request_order_info",
    "part_query",
    "part_search",
    "supplier_query",
    "supplier_get",
]

# 名字里出现这些片段的 MCP 工具，如果没进 WRITE_TOOLS，启动自检会告警或拒绝启动。
WRITE_NAME_MARKERS = ("create", "update", "inbound", "outbound", "status")

RISK_LABELS = {
    "high": "高风险",
    "medium": "中风险",
    "low": "低风险",
}

# 会修改 ERP 的工具。主 Agent 与子 Agent 的 interrupt_on 都由 build_interrupt_on() 生成。
WRITE_TOOLS: dict[str, dict] = {
    "order_create": {
        "risk_level": "high",
        "allowed_decisions": ["approve", "reject", "edit"],
        "description": "【高风险】采购订单创建将写入 ERP。请核对零件、数量、单价与合计。",
    },
    "order_update": {
        "risk_level": "high",
        "allowed_decisions": ["approve", "reject", "edit"],
        "description": "【高风险】采购订单修改将写入 ERP。请核对修改后的数量、单价与合计。",
    },
    "order_update_status": {
        "risk_level": "medium",
        "allowed_decisions": ["approve", "reject"],
        "description": "【中风险】订单状态变更将写入 ERP，且通常不可撤销。",
    },
    "inventory_inbound": {
        "risk_level": "medium",
        "allowed_decisions": ["approve", "reject", "edit"],
        "description": "【中风险】入库将写入 ERP 库存。请核对零件与数量。",
    },
    "inventory_outbound": {
        "risk_level": "high",
        "allowed_decisions": ["approve", "reject", "edit"],
        "description": "【高风险】出库将写入 ERP 并减少账面库存。请核对零件与数量。",
    },
    "supplier_create": {
        "risk_level": "medium",
        "allowed_decisions": ["approve", "reject"],
        "description": "【中风险】新建供应商将写入 ERP。",
    },
    "supplier_update_status": {
        "risk_level": "high",
        "allowed_decisions": ["approve", "reject"],
        "description": "【高风险】供应商合作状态变更将写入 ERP。",
    },
    "part_create": {
        "risk_level": "medium",
        "allowed_decisions": ["approve", "reject", "edit"],
        "description": "【中风险】新建零件将写入 ERP。请核对编码、名称与采购单价。",
    },
}


def build_interrupt_on(tool_names: Iterable[str] | None = None) -> dict:
    """从 WRITE_TOOLS 生成框架 interrupt_on。

    tool_names 为 None 时覆盖全部写工具；否则只保留该 Agent 实际持有的写工具。
    """
    selected = list(WRITE_TOOLS) if tool_names is None else list(tool_names)
    interrupt_on = {}
    for name in selected:
        spec = WRITE_TOOLS.get(name)
        if not spec:
            continue
        interrupt_on[name] = {
            "allowed_decisions": list(spec["allowed_decisions"]),
            "description": spec["description"],
        }
    return interrupt_on


def tool_name_looks_like_write(name: str) -> bool:
    lowered = str(name or "").lower()
    return any(marker in lowered for marker in WRITE_NAME_MARKERS)


def find_unlisted_write_tools(tool_names: Iterable[str]) -> list[str]:
    missing = []
    for name in tool_names:
        if not name or name in WRITE_TOOLS:
            continue
        if tool_name_looks_like_write(name):
            missing.append(name)
    return missing


class WriteToolAuditError(RuntimeError):
    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__(
            "MCP 写工具未列入 WRITE_TOOLS，拒绝启动: " + ", ".join(self.missing)
        )


def enforce_write_tool_coverage(
    tool_names: Iterable[str],
    *,
    strict: bool | None = None,
    logger=None,
) -> list[str]:
    """启动自检。默认拒绝启动；WRITE_TOOL_AUDIT_STRICT=0 时只告警。"""
    missing = find_unlisted_write_tools(tool_names)
    if not missing:
        return []
    if strict is None:
        flag = os.getenv("WRITE_TOOL_AUDIT_STRICT", "1").strip().lower()
        strict = flag not in {"0", "false", "no", "warn"}
    message = (
        "MCP 工具名含 create/update/inbound/outbound/status 但未列入 WRITE_TOOLS: "
        + ", ".join(missing)
    )
    if strict:
        raise WriteToolAuditError(missing)
    (logger or agent_logger).warning(message)
    return missing


def classify_tools(all_tools: List[BaseTool]) -> dict:
    """将工具按用途分类"""
    tool_map = {t.name: t for t in all_tools}

    analyst_tools = []
    order_tools = []

    for pattern in ANALYST_TOOL_PATTERNS:
        for name, tool in tool_map.items():
            if pattern in name and tool not in analyst_tools:
                analyst_tools.append(tool)

    for pattern in ORDER_TOOL_PATTERNS:
        for name, tool in tool_map.items():
            if pattern in name and tool not in order_tools:
                order_tools.append(tool)

    return {
        "analyst_tools": analyst_tools,
        "order_tools": order_tools,
        "all_tools": all_tools,
    }

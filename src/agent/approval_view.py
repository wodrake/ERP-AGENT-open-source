"""把写工具参数整理成审批卡上的业务字段。"""
from __future__ import annotations

from typing import Any

from .mcp_tools_bean import RISK_LABELS, WRITE_TOOLS
from ..mcp_server.order_validation import extract_order_payload


def _text(*values: Any) -> str:
    for value in values:
        if value is None or value == "":
            continue
        return str(value)
    return ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _line_from_mapping(raw: dict, *, index: int, fallback_supplier: str) -> dict:
    quantity = _number(raw.get("quantity", raw.get("qty")))
    price = _number(raw.get("unitPrice", raw.get("unit_price", raw.get("purchasePrice", raw.get("purchase_price")))))
    subtotal = None
    if quantity is not None and price is not None:
        subtotal = round(quantity * price, 2)
    supplier = _text(raw.get("supplierName"), raw.get("supplier"), raw.get("supplier_name"), fallback_supplier)
    return {
        "part_name": _text(raw.get("partName"), raw.get("part_name"), raw.get("name")),
        "part_code": _text(raw.get("partCode"), raw.get("part_code"), raw.get("partId"), raw.get("part_id")),
        "quantity": quantity,
        "unit_price": price,
        "subtotal": subtotal,
        "supplier": supplier,
        "line_index": index,
    }


def _action_view(name: str, args: dict, allowed: list[str] | None = None) -> dict:
    spec = WRITE_TOOLS.get(name) or {}
    risk = spec.get("risk_level") or ""
    decisions = list(allowed or spec.get("allowed_decisions") or ["approve", "reject"])
    payload, _error = extract_order_payload(args)
    supplier = _text(
        (payload or {}).get("supplierName") if isinstance(payload, dict) else None,
        (payload or {}).get("supplier") if isinstance(payload, dict) else None,
        args.get("supplier_name"),
        args.get("name") if name.startswith("supplier") else None,
    )
    lines: list[dict] = []
    total = None
    if isinstance(payload, dict):
        supplier = _text(payload.get("supplierName"), payload.get("supplier"), supplier)
        details = payload.get("orderDetail") or payload.get("order_detail") or []
        if isinstance(details, list):
            lines = [_line_from_mapping(line, index=i, fallback_supplier=supplier) for i, line in enumerate(details) if isinstance(line, dict)]
        total = _number(payload.get("totalAmount", payload.get("total_amount")))
    elif name in {"inventory_inbound", "inventory_outbound"}:
        lines = [_line_from_mapping(
            {"partId": args.get("part_id", args.get("partId")), "quantity": args.get("quantity")},
            index=0,
            fallback_supplier="",
        )]
    elif name == "part_create":
        lines = [_line_from_mapping(
            {
                "partCode": args.get("part_code"),
                "name": args.get("name"),
                "purchase_price": args.get("purchase_price"),
                "quantity": 1,
            },
            index=0,
            fallback_supplier=_text(args.get("supplier_id")),
        )]
        total = _number(args.get("purchase_price"))
    fields = []
    if name == "supplier_create":
        fields = [
            {"label": "供应商编码", "value": _text(args.get("supplier_code"))},
            {"label": "供应商", "value": _text(args.get("name"))},
            {"label": "联系人", "value": _text(args.get("contact_person"))},
        ]
        supplier = _text(args.get("name"), supplier)
    elif name == "supplier_update_status":
        fields = [
            {"label": "供应商 ID", "value": _text(args.get("id"))},
            {"label": "合作状态", "value": _text(args.get("status"))},
        ]
    elif name == "order_update_status":
        fields = [
            {"label": "订单 ID", "value": _text(args.get("id"))},
            {"label": "新状态", "value": _text(args.get("status"))},
        ]
    elif name in {"inventory_inbound", "inventory_outbound"}:
        fields = [{"label": "仓库位置", "value": _text(args.get("warehouse_location"))}]
    return {
        "tool_name": name,
        "approval_id": _text(args.get("approval_id")),
        "risk_level": risk,
        "risk_label": RISK_LABELS.get(risk, risk),
        "supplier": supplier,
        "total_amount": total,
        "lines": lines,
        "fields": [item for item in fields if item["value"]],
        "allowed_decisions": decisions,
        "writes_erp": True,
        "args": args,
    }


def business_view_from_actions(action_requests: list, review_configs: list | None = None) -> dict:
    allowed_by_name = {}
    for config in review_configs or []:
        if isinstance(config, dict) and config.get("action_name"):
            allowed_by_name[config["action_name"]] = list(config.get("allowed_decisions") or [])
    actions = []
    for action in action_requests:
        if not isinstance(action, dict):
            continue
        name = str(action.get("name") or "")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        actions.append(_action_view(name, args, allowed_by_name.get(name)))
    return {"writes_erp": True, "hint": "该操作将写入 ERP", "actions": actions}


def build_sse_interrupt(interrupt_value: dict) -> dict:
    """SSE 审批事件。order_data 保持为 action_requests，兼容既有前端和测试。"""
    action_requests = interrupt_value.get("action_requests")
    if isinstance(action_requests, list):
        view = business_view_from_actions(action_requests, interrupt_value.get("review_configs") or [])
        first = view["actions"][0] if view["actions"] else {}
        return {
            "interrupt_type": "hitl_approval",
            "tool_name": first.get("tool_name", ""),
            "tool_args": action_requests,
            "order_data": action_requests,
            "approval_id": first.get("approval_id", ""),
            "risk_level": first.get("risk_level", ""),
            "risk_label": first.get("risk_label", ""),
            "allowed_decisions": first.get("allowed_decisions", []),
            "business_view": view,
            "writes_erp": True,
        }
    tool_args = interrupt_value.get("tool_args", interrupt_value)
    args = tool_args if isinstance(tool_args, dict) else {}
    name = str(interrupt_value.get("tool_name") or args.get("name") or "")
    view = {"writes_erp": True, "hint": "该操作将写入 ERP", "actions": [_action_view(name, args)] if name or args else []}
    first = view["actions"][0] if view["actions"] else {}
    return {
        "interrupt_type": "hitl_approval",
        "tool_name": name,
        "tool_args": tool_args,
        "order_data": tool_args,
        "approval_id": first.get("approval_id", ""),
        "risk_level": first.get("risk_level", ""),
        "risk_label": first.get("risk_label", ""),
        "allowed_decisions": first.get("allowed_decisions", []),
        "business_view": view,
        "writes_erp": True,
    }

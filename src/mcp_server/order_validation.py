"""订单写入前的确定性校验。

失败时调用方不得进入审批，也不得请求 ERP。金额用 Decimal，偏差以 0.01 元为上限。
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any


ORDER_WRITE_TOOLS = frozenset({"order_create", "order_update"})
_TOTAL_TOLERANCE = Decimal("0.01")


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not number.is_finite():
        return None
    return number


def extract_order_payload(args: dict | None) -> tuple[dict | None, str | None]:
    """从工具参数里取出订单对象。

    返回 (payload, error)。payload 为 None 且 error 为 None 表示参数里没有订单体。
    """
    if not isinstance(args, dict):
        return None, "订单参数不是对象"
    raw = args.get("order_data", args.get("orderData"))
    if raw is None and ("orderDetail" in args or "order_detail" in args or "totalAmount" in args):
        raw = args
    if raw is None:
        return None, None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return None, "订单数据 JSON 解析失败"
    if not isinstance(raw, dict):
        return None, "订单数据不是对象"
    return raw, None


def validate_order_write(args: dict | None, *, tool_name: str = "order_create") -> str | None:
    """校验订单写入。通过时返回 None，否则返回给模型的错误说明。"""
    payload, error = extract_order_payload(args)
    if error:
        return error
    if payload is None:
        if tool_name == "order_create":
            return "订单数据缺失或不是合法 JSON"
        return None

    details = payload.get("orderDetail", payload.get("order_detail"))
    has_total = "totalAmount" in payload or "total_amount" in payload
    if tool_name != "order_create" and not details and not has_total:
        return None
    if not isinstance(details, list) or not details:
        return "订单明细不能为空"

    total = Decimal("0")
    for index, line in enumerate(details, start=1):
        if not isinstance(line, dict):
            return f"明细第{index}行不是对象"
        quantity = _as_decimal(line.get("quantity", line.get("qty")))
        price = _as_decimal(line.get("unitPrice", line.get("unit_price")))
        if quantity is None or quantity < 1:
            return f"明细第{index}行 quantity 必须 >= 1"
        if price is None or price <= 0:
            return f"明细第{index}行 unitPrice 必须 > 0"
        total += quantity * price

    if not has_total:
        return "缺少 totalAmount，无法与明细合计核对"
    declared = _as_decimal(payload.get("totalAmount", payload.get("total_amount")))
    if declared is None:
        return "totalAmount 不是有效金额"
    if abs(declared - total) > _TOTAL_TOLERANCE:
        return f"totalAmount {declared} 与明细合计 {total} 偏差超过 0.01"
    return None

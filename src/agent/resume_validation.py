"""校验 /resume 里的审批决定。

恢复必须带上当前中断的 approval_id。edit 不能改工具，也不能改白名单以外的字段。
编辑后的订单仍要经过金额和数量校验。
"""
from __future__ import annotations

import re

from .approval_store import canonical_snapshot, deep_diff
from .mcp_tools_bean import WRITE_TOOLS
from ..mcp_server.order_validation import ORDER_WRITE_TOOLS, validate_order_write

_ORDER_EDIT = re.compile(
    r"^(?:order_data|orderData)\.(?:orderDetail|order_detail)\.\d+\.(?:quantity|unitPrice|unit_price)$"
    r"|^(?:order_data|orderData)\.(?:totalAmount|total_amount)$"
    r"|^(?:orderDetail|order_detail)\.\d+\.(?:quantity|unitPrice|unit_price)$"
    r"|^(?:totalAmount|total_amount)$"
)

# 非订单写工具允许在批准前修改的顶层参数。
_FIELD_WHITELIST = {
    "inventory_inbound": {"quantity"},
    "inventory_outbound": {"quantity"},
    "part_create": {"purchase_price"},
}


def actions_from_interrupt_value(value: dict) -> list[dict]:
    actions = []
    for action in value.get("action_requests") or []:
        if not isinstance(action, dict):
            continue
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        actions.append({
            "name": str(action.get("name") or ""),
            "args": args,
            "approval_id": str(args.get("approval_id") or ""),
        })
    return actions


def actions_from_state(state) -> list[dict]:
    if state is None:
        return []
    interrupts = list(getattr(state, "interrupts", ()) or [])
    if not interrupts:
        for task in getattr(state, "tasks", ()) or ():
            interrupts.extend(getattr(task, "interrupts", ()) or ())
    actions = []
    for item in interrupts:
        value = item.value if hasattr(item, "value") else item
        if isinstance(value, dict) and value.get("action_requests"):
            actions.extend(actions_from_interrupt_value(value))
    return actions


def editable_change_error(tool_name: str, before_args: dict, after_args: dict) -> str | None:
    spec = WRITE_TOOLS.get(tool_name) or {}
    if "edit" not in spec.get("allowed_decisions", []):
        return "该操作不能编辑，只能批准或拒绝"
    before = canonical_snapshot(before_args)
    after = canonical_snapshot(after_args)
    changed = deep_diff(before, after)
    if tool_name in ORDER_WRITE_TOOLS:
        illegal = [path for path in changed if not _ORDER_EDIT.match(path)]
        if illegal:
            return "只能修改订单明细的数量、单价和据此重算的合计，不能改 " + "、".join(illegal)
        error = validate_order_write(after_args, tool_name=tool_name)
        return error
    allowed = _FIELD_WHITELIST.get(tool_name, set())
    illegal = [path for path in changed if path not in allowed]
    if illegal:
        return "只能修改 " + "、".join(sorted(allowed)) + "，不能改 " + "、".join(illegal)
    if "quantity" in changed:
        new_quantity = after.get("quantity")
        if not _is_positive_int(new_quantity):
            return "quantity 必须是大于等于 1 的整数"
    return None


def _is_positive_int(value) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value >= 1
    if isinstance(value, float):
        return value.is_integer() and value >= 1
    text = str(value).strip()
    if not text.isdigit():
        return False
    return int(text) >= 1


def validate_decision(action: dict, decision: dict) -> str | None:
    kind = decision.get("type")
    if kind not in {"approve", "reject", "edit"}:
        return "不支持的审批决定"
    if kind != "edit":
        return None
    edited = decision.get("edited_action") or {}
    if edited.get("name") != action["name"]:
        return "不能改变工具或操作类型"
    args = edited.get("args") if isinstance(edited.get("args"), dict) else {}
    if str(args.get("approval_id") or "") != action["approval_id"] or not action["approval_id"]:
        return "缺少或篡改 approval_id，已拒绝"
    return editable_change_error(action["name"], action["args"], args)


def submitted_approval_ids(resume: dict) -> set[str]:
    found = set()
    single = str(resume.get("approval_id") or "").strip()
    if single:
        found.add(single)
    for item in resume.get("approval_ids") or []:
        text = str(item or "").strip()
        if text:
            found.add(text)
    return found


def validate_resume_payload(actions: list[dict], resume: dict) -> str | None:
    """actions 是当前中断里的写操作。空列表表示这不是写审批中断。"""
    if not actions:
        return None
    pending = {action["approval_id"] for action in actions if action.get("approval_id")}
    if not pending:
        return "当前中断缺少 approval_id，已拒绝"
    submitted = submitted_approval_ids(resume if isinstance(resume, dict) else {})
    if submitted != pending:
        return "approval_id 与当前待审批中断不一致，已拒绝。请使用当前审批卡。"
    decisions = resume.get("decisions") if isinstance(resume, dict) else None
    if not isinstance(decisions, list) or len(decisions) != len(actions):
        return "审批决定数量与当前中断不一致，已拒绝"
    for action, decision in zip(actions, decisions):
        if not isinstance(decision, dict):
            return "审批决定格式不正确"
        error = validate_decision(action, decision)
        if error:
            return error
    return None

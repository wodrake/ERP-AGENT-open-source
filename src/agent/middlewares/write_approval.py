"""写操作进入审批前的校验、幂等键注入和审批审计。

interrupt_on 仍由 WRITE_TOOLS 生成并交给框架的 HumanInTheLoop。
本中间件在模型返回后、审批中断前改写工具调用：
- 订单金额或数量不合法时改走 report_write_validation_error，不进入审批；
- 合法写调用写入 approval_id，作为 MCP 幂等键。
人工决定之后把记录写入 approvals。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_config

from ..approval_store import (
    UNAUTHENTICATED,
    args_hash,
    attach_erp_result,
    build_edit_diff,
    canonical_snapshot,
    get_approval_store,
    peek_pending_by_call,
    pop_pending_by_call,
    record_decision,
    remember_pending,
    write_authorization,
)
from ..log_utils import agent_logger
from ..mcp_tools_bean import WRITE_TOOLS
from ..tools.write_validation import report_write_validation_error
from ...mcp_server.idempotency import bind_approval_id, get_idempotency_store, reset_approval_id
from ...mcp_server.persistence import ApprovalPersistenceUnavailable
from ..resume_validation import validate_decision
from ...mcp_server.order_validation import ORDER_WRITE_TOOLS, validate_order_write

VALIDATION_TOOL = report_write_validation_error.name


def _runtime_identity() -> tuple[str, str]:
    try:
        config = get_config()
    except RuntimeError:
        config = {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = str(configurable.get("thread_id") or "")
    # R1 没有认证。请求人先记请求中的 user_id，并固定标注未认证。
    user_id = str(configurable.get("user_id") or "unknown")
    return thread_id, user_id


def _message_text(result: Any) -> str:
    if isinstance(result, ToolMessage):
        content = result.content
        if isinstance(content, str):
            return content
        return str(content)
    return str(result)


class WriteApprovalMiddleware(AgentMiddleware):
    """主 Agent 和子 Agent 共用。工具集合本身不在这里决定，而在 WRITE_TOOLS。"""

    def __init__(self) -> None:
        self.tools = [report_write_validation_error]

    def _prepare(self, response):
        messages, rebuild = _unwrap_model_result(response)
        if messages is None:
            return response
        changed = False
        rewritten = []
        for message in messages:
            if isinstance(message, AIMessage) and message.tool_calls:
                tool_calls = []
                for call in message.tool_calls:
                    updated, did = self._rewrite_call(call)
                    changed = changed or did
                    tool_calls.append(updated)
                if changed:
                    message = message.model_copy(update={"tool_calls": tool_calls})
            rewritten.append(message)
        if not changed:
            return response
        return rebuild(rewritten)

    def _rewrite_call(self, call: dict) -> tuple[dict, bool]:
        name = call.get("name")
        args = dict(call.get("args") or {})
        if name in ORDER_WRITE_TOOLS:
            error = validate_order_write(args, tool_name=name)
            if error:
                agent_logger.info(f"订单校验未通过，不进入审批: {name}")
                return {
                    **call,
                    "name": VALIDATION_TOOL,
                    "args": {"tool_name": name, "error": error},
                }, True
        if name in WRITE_TOOLS:
            approval_id = str(uuid.uuid4())
            args["approval_id"] = approval_id
            snapshot = canonical_snapshot(args)
            thread_id, user_id = _runtime_identity()
            remember_pending({
                "approval_id": approval_id,
                "tool_call_id": call.get("id"),
                "thread_id": thread_id,
                "tool_name": name,
                "args_snapshot": snapshot,
                "args_hash": args_hash(snapshot),
                "risk_level": WRITE_TOOLS[name]["risk_level"],
                "requester_user_id": user_id,
                "requester_auth_status": UNAUTHENTICATED,
                "requested_at": datetime.now().isoformat(),
            })
            return {**call, "args": args}, True
        return call, False

    def wrap_model_call(self, request, handler):
        return self._prepare(handler(request))

    async def awrap_model_call(self, request, handler):
        return self._prepare(await handler(request))

    def after_model(self, state, runtime):
        return _record_decisions(state)

    async def aafter_model(self, state, runtime):
        return _record_decisions(state)

    def wrap_tool_call(self, request, handler):
        return _guard_tool(request, handler)

    async def awrap_tool_call(self, request, handler):
        approval_id = str((request.tool_call.get("args") or {}).get("approval_id") or "")
        name = request.tool_call.get("name")
        blocked = _blocked_write(name, request.tool_call.get("id"), approval_id)
        if blocked is not None:
            return blocked
        if name in WRITE_TOOLS and approval_id:
            cached = get_idempotency_store().replay(approval_id)
            if cached is not None:
                agent_logger.info(f"审批已执行，跳过重复工具调用: {name} {approval_id}")
                return ToolMessage(content=cached, tool_call_id=request.tool_call.get("id"), name=name)
        token = bind_approval_id(approval_id)
        try:
            result = await handler(request)
        finally:
            reset_approval_id(token)
        if name in WRITE_TOOLS and approval_id:
            attach_erp_result(approval_id, _message_text(result))
        return result


def _error_message(name: str | None, tool_call_id: str | None, message: str):
    return ToolMessage(
        content=json.dumps({"code": 403, "message": message, "written": False}, ensure_ascii=False),
        tool_call_id=tool_call_id,
        name=name,
        status="error",
    )


def _blocked_write(name: str | None, tool_call_id: str | None, approval_id: str = ""):
    if name not in WRITE_TOOLS:
        return None
    try:
        get_idempotency_store()
        get_approval_store()
    except ApprovalPersistenceUnavailable as exc:
        agent_logger.error(str(exc))
        return _error_message(name, tool_call_id, str(exc))
    denied = write_authorization(approval_id, name)
    if denied:
        agent_logger.error(denied)
        return _error_message(name, tool_call_id, denied)
    return None


def _guard_tool(request, handler):
    approval_id = str((request.tool_call.get("args") or {}).get("approval_id") or "")
    name = request.tool_call.get("name")
    blocked = _blocked_write(name, request.tool_call.get("id"), approval_id)
    if blocked is not None:
        return blocked
    if name in WRITE_TOOLS and approval_id:
        cached = get_idempotency_store().replay(approval_id)
        if cached is not None:
            agent_logger.info(f"审批已执行，跳过重复工具调用: {name} {approval_id}")
            return ToolMessage(content=cached, tool_call_id=request.tool_call.get("id"), name=name)
    token = bind_approval_id(approval_id)
    try:
        result = handler(request)
    finally:
        reset_approval_id(token)
    if name in WRITE_TOOLS and approval_id:
        attach_erp_result(approval_id, _message_text(result))
    return result


def _record_decisions(state) -> dict | None:
    messages = state.get("messages") or []
    ai_message = next((message for message in reversed(messages) if isinstance(message, AIMessage) and message.tool_calls), None)
    if ai_message is None:
        return None
    edited = state.get("hitl_edited_tool_calls") or {}
    rejected = _rejected_ids(messages, ai_message)
    _thread_id, user_id = _runtime_identity()
    errors = []
    for call in ai_message.tool_calls:
        # 先看 pending，落库成功后再弹出。写失败时保留它，Mongo 恢复后可以重试。
        pending = peek_pending_by_call(call.get("id"))
        if not pending:
            continue
        call_id = call.get("id")
        try:
            if call_id in rejected:
                record_decision(pending, decision="reject", edit_diff=None, approver_user_id=user_id)
            elif call_id in edited:
                edited_action = edited[call_id] or {}
                error = validate_decision(pending_action(pending), {
                    "type": "edit",
                    "edited_action": edited_action,
                })
                if error:
                    errors.append(_error_message(call.get("name"), call_id, error))
                    pop_pending_by_call(call_id)
                    continue
                edited_args = canonical_snapshot(edited_action.get("args") or {})
                record_decision(
                    pending,
                    decision="edit",
                    edit_diff=build_edit_diff(pending["args_snapshot"], edited_args),
                    approver_user_id=user_id,
                )
            else:
                record_decision(pending, decision="approve", edit_diff=None, approver_user_id=user_id)
        except ApprovalPersistenceUnavailable:
            raise
        pop_pending_by_call(call_id)
    if errors:
        return {"messages": errors}
    return None


def pending_action(pending: dict) -> dict:
    return {
        "name": pending["tool_name"],
        "args": pending.get("args_snapshot") or {},
        "approval_id": pending["approval_id"],
    }


def _rejected_ids(messages, ai_message) -> set[str]:
    ids = {call.get("id") for call in ai_message.tool_calls}
    rejected = set()
    for message in messages:
        if not isinstance(message, ToolMessage) or message.tool_call_id not in ids:
            continue
        content = message.content if isinstance(message.content, str) else str(message.content)
        if message.status == "error" and "rejected" in content.lower():
            rejected.add(message.tool_call_id)
    return rejected


def _unwrap_model_result(response):
    from langchain.agents.middleware.types import ExtendedModelResponse, ModelResponse

    if isinstance(response, AIMessage):
        return [response], lambda messages: messages[-1]
    if isinstance(response, ModelResponse):
        return list(response.result), lambda messages: ModelResponse(
            result=messages, structured_response=response.structured_response
        )
    if isinstance(response, ExtendedModelResponse):
        inner = response.model_response
        return list(inner.result), lambda messages: ExtendedModelResponse(
            model_response=ModelResponse(result=messages, structured_response=inner.structured_response),
            command=response.command,
        )
    return None, None

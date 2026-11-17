"""恢复审批时的幂等入口。

同一个已执行的审批再次 resume，直接返回首次结果，不再调用 Agent，也不再写 ERP。
写审批必须带上当前中断的 approval_id。
"""
from __future__ import annotations

from langgraph.types import Command

from .approval_store import latest_terminal, unrecorded_pending_ids
from .resume_validation import actions_from_state, submitted_approval_ids, validate_resume_payload


def is_graph_interrupted(state) -> bool:
    if state is None:
        return False
    if getattr(state, "interrupts", None):
        return True
    for task in getattr(state, "tasks", ()) or ():
        if getattr(task, "interrupts", None):
            return True
    return False


def replay_message(record: dict) -> str:
    result = record.get("erp_result")
    if record.get("decision") == "reject":
        return "该审批已拒绝，未写入 ERP。重复恢复不会再次执行。"
    body = result if isinstance(result, str) else ""
    return f"该审批已执行，返回首次结果，未再次写入 ERP。\n{body}".strip()


def _graph_has_next(state) -> bool:
    return bool(getattr(state, "next", None))


async def prepare_resume(agent, config: dict, resume_value: dict, thread_id: str) -> dict:
    """决定这次 resume 是拒绝、回放，还是继续跑图。"""
    try:
        state = await agent.aget_state(config)
    except Exception:
        state = None
    resume_value = resume_value if isinstance(resume_value, dict) else {}
    submitted = submitted_approval_ids(resume_value)
    if is_graph_interrupted(state):
        error = validate_resume_payload(actions_from_state(state), resume_value)
        if error:
            return {"kind": "error", "message": error}
        return {"kind": "invoke", "input": Command(resume=resume_value)}
    record = latest_terminal(thread_id)
    if record and (not submitted or record.get("approval_id") in submitted):
        return {
            "kind": "replay",
            "approval_id": record.get("approval_id"),
            "tool_name": record.get("tool_name"),
            "erp_result": record.get("erp_result"),
            "message": replay_message(record),
            "record": record,
        }
    pending = unrecorded_pending_ids(thread_id)
    if pending and submitted == pending and _graph_has_next(state):
        # 上次决定已经生效，但审批记录没写完。不要重新套用一份决定，只把图往下跑。
        return {"kind": "invoke", "input": None}
    return {"kind": "error", "message": "approval_id 与当前待审批中断不一致，已拒绝。请使用当前审批卡。"}


async def resume_or_replay(agent, config: dict, resume_value, thread_id: str):
    """测试与非流式恢复使用。聊天流在 prepare_resume 之后继续 astream。"""
    prepared = await prepare_resume(agent, config, resume_value, thread_id)
    if prepared["kind"] == "error":
        return {"replayed": False, "rejected": True, "message": prepared["message"]}
    if prepared["kind"] == "replay":
        return {"replayed": True, **prepared}
    result = await agent.ainvoke(prepared["input"], config)
    return {"replayed": False, "rejected": False, "result": result}

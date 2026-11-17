"""恢复审批时的幂等入口。

同一个已执行的审批再次 resume，直接返回首次结果，不再调用 Agent，也不再写 ERP。
"""
from __future__ import annotations

from langgraph.types import Command

from .approval_store import latest_terminal


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


async def maybe_replay(agent, config: dict, thread_id: str) -> dict | None:
    """图仍停在中断上时返回 None，调用方继续 resume。否则返回已完成审批。"""
    try:
        state = await agent.aget_state(config)
    except Exception:
        return None
    if is_graph_interrupted(state):
        return None
    record = latest_terminal(thread_id)
    if record is None:
        return None
    return {
        "replayed": True,
        "approval_id": record.get("approval_id"),
        "tool_name": record.get("tool_name"),
        "erp_result": record.get("erp_result"),
        "message": replay_message(record),
        "record": record,
    }


async def resume_or_replay(agent, config: dict, resume_value, thread_id: str):
    """测试与非流式恢复使用。聊天流在 maybe_replay 之后继续 astream。"""
    replay = await maybe_replay(agent, config, thread_id)
    if replay is not None:
        return replay
    result = await agent.ainvoke(Command(resume=resume_value), config)
    return {"replayed": False, "result": result}

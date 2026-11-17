"""Execution evidence can upgrade routing; writes must not be replayed by grading."""
import json
from deepagents import RubricMiddleware
from langchain.agents.middleware import AgentMiddleware, hook_config
from langgraph.errors import GraphBubbleUp

from .json_grader import JsonModeRubricMiddleware, log_grader_skip
from ..harness import HarnessPhaseState
from ..review_policy import ReviewRoute, compose_rubric, reset_grading


def execution_signals(state):
    messages = state.get("messages", [])[state.get("_review_start", 0):]
    signals = set()
    writes = False
    calls = []
    for m in messages:
        for call in getattr(m, "tool_calls", []) or []:
            name = call.get("name", "").lower()
            calls.append(name)
            if any(word in name for word in ("create", "update", "delete", "remove", "inbound", "outbound", "stock_in", "stock_out", "adjust", "execute", "install")):
                writes = True
                signals.add("write_or_execution_attempt")
            if name == "task":
                # Child tool traces may not be present in the parent's state.
                # Conservatively suppress automatic replay for delegated work.
                writes = True
                signals.add("delegated_task")
        if getattr(m, "type", "") == "tool":
            content = getattr(m, "content", "")
            error = getattr(m, "status", "") == "error"
            if isinstance(content, str):
                try:
                    data = json.loads(content)
                    error |= isinstance(data, dict) and (data.get("success") is False or bool(data.get("error")))
                except (ValueError, TypeError):
                    error |= content.lstrip().lower().startswith(("error", "错误", "失败"))
            if error:
                signals.add("tool_error")
    if len(calls) >= 3:
        signals.add("multiple_tool_calls")
    if len({n for n in calls if any(x in n for x in ("search", "fetch", "query"))}) >= 2:
        signals.add("multiple_sources")
    return sorted(signals), writes


class ReviewExecutionGate(AgentMiddleware):
    """Register AFTER RubricMiddleware so reverse after_agent order runs us first."""
    state_schema = HarnessPhaseState

    def __init__(self, config):
        self.config = config

    def after_agent(self, state, runtime):
        signals, writes = execution_signals(state)
        updates = {"_review_signals": signals, "_review_write_attempted": writes}
        if not signals or self.config.get("review", {}).get("mode") == "never":
            return updates
        if not state.get("rubric"):
            route = ReviewRoute(decision="review", reason="执行信号升级", checks=["sources", "constraints"])
            rubric = compose_rubric(route, self.config)
            updates.update(rubric=rubric, _harness_rubric_source="auto", **reset_grading(rubric))
        updates["review_decision"] = {**state.get("review_decision", {}), "decision": "review",
                                      "source": "execution", "signals": signals}
        return updates


class SafeRubricMiddleware(JsonModeRubricMiddleware):
    """A separate single-pass reviewer prevents write retries without shared mutation.

    两级保险，避免审查把主流程带走：
    1. 用 DeepSeek 支持的 json_object 取结构化判定（json_schema 会被接口 400 拒绝）；
    2. 评分器仍不可用时（接口报错、输出无法解析）只记一条日志并跳过本轮审查，
       会话继续走完，写工具的人工审批中断不受影响。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._write_reviewer = JsonModeRubricMiddleware(**{**kwargs, "max_iterations": 1})

    def _reviewer_for(self, state):
        return self._write_reviewer if state.get("_review_write_attempted") else self

    def _run_sync_review(self, state, runtime):
        reviewer = self._reviewer_for(state)
        if reviewer is self:
            # 直接调用框架实现，绕开本类的降级包装，避免递归。
            return RubricMiddleware.after_agent(self, state, runtime)
        return reviewer.after_agent(state, runtime)

    async def _run_async_review(self, state, runtime):
        reviewer = self._reviewer_for(state)
        if reviewer is self:
            return await RubricMiddleware.aafter_agent(self, state, runtime)
        return await reviewer.aafter_agent(state, runtime)

    @staticmethod
    def _degrade(update):
        """框架把评分异常记成 grader_error：这里降级为“跳过审查”。"""
        if isinstance(update, dict) and update.get("_rubric_status") == "grader_error":
            evaluations = update.get("_rubric_evaluations") or []
            detail = evaluations[-1].get("explanation", "") if evaluations else ""
            log_grader_skip(detail or "grader_error")
            return None
        return update

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state, runtime):
        try:
            return self._degrade(self._run_sync_review(state, runtime))
        except GraphBubbleUp:
            raise
        except Exception as exc:  # noqa: BLE001 -- 审查失败不得中断主流程
            log_grader_skip(exc)
            return None

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        try:
            return self._degrade(await self._run_async_review(state, runtime))
        except GraphBubbleUp:
            raise
        except Exception as exc:  # noqa: BLE001 -- 审查失败不得中断主流程
            log_grader_skip(exc)
            return None

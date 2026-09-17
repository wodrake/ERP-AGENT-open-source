"""Execution evidence can upgrade routing; writes must not be replayed by grading."""
import json
from deepagents import RubricMiddleware
from langchain.agents.middleware import AgentMiddleware, hook_config
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


class SafeRubricMiddleware(RubricMiddleware):
    """A separate single-pass reviewer prevents write retries without shared mutation."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._write_reviewer = RubricMiddleware(**{**kwargs, "max_iterations": 1})

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state, runtime):
        if state.get("_review_write_attempted"):
            return self._write_reviewer.after_agent(state, runtime)
        return super().after_agent(state, runtime)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        if state.get("_review_write_attempted"):
            return await self._write_reviewer.aafter_agent(state, runtime)
        return await super().aafter_agent(state, runtime)

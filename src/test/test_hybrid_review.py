"""Real graph regression suite for routing, escalation, grading and HITL."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from deepagents.middleware.rubric import GraderResponse

from src.agent.harness import HarnessPhaseMiddleware, load_harness_config
from src.agent.review_policy import ReviewPolicy, ReviewRoute
from src.agent.middlewares.json_grader import GraderUnavailable, parse_grader_response
from src.agent.middlewares.review_gate import ReviewExecutionGate, SafeRubricMiddleware


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def verdict(result="satisfied"):
    criterion = {"name": "task", "passed": result == "satisfied"}
    if result != "satisfied":
        criterion["gap"] = "missing evidence"
    return GraderResponse(result=result, explanation="test review", criteria=[criterion])


def reviewer(result="satisfied"):
    r = SafeRubricMiddleware(model=ToolModel(responses=[AIMessage(content="unused")]), max_iterations=2)
    r._grade = Mock(return_value=verdict(result))
    r._agrade = AsyncMock(return_value=verdict(result))
    r._write_reviewer._grade = Mock(return_value=verdict(result))
    r._write_reviewer._agrade = AsyncMock(return_value=verdict(result))
    return r


class JsonGraderModel:
    """记录 bind() 参数，并按预设文本返回 —— 模拟 DeepSeek 的 chat 接口。"""

    def __init__(self, content, error=None):
        self.content = content
        self.error = error
        self.bound = []

    def bind(self, **kwargs):
        self.bound.append(kwargs)
        return self

    def _reply(self):
        if self.error:
            raise self.error
        return AIMessage(content=self.content)

    def invoke(self, messages, config=None, **kwargs):
        return self._reply()

    async def ainvoke(self, messages, config=None, **kwargs):
        return self._reply()


class BrokenGrader:
    """像 DeepSeek 对 json_schema 那样直接 400。"""

    def __init__(self, error=None):
        self.error = error or RuntimeError("Error code: 400 - This response_format type is unavailable now")
        self.calls = 0

    def bind(self, **kwargs):
        return self

    def invoke(self, messages, config=None, **kwargs):
        self.calls += 1
        raise self.error

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls += 1
        raise self.error


GRADED_JSON = (
    '{"result": "needs_revision", "explanation": "订单未提交", '
    '"criteria": [{"name": "写操作已触发审批", "passed": false, "gap": "没有调用 order_create"}]}'
)


class HybridReviewTests(unittest.TestCase):
    def setUp(self):
        self.config = load_harness_config()

    def state(self, text):
        return {"messages": [HumanMessage(content=text)]}

    def test_clear_rules_and_mixed_definition(self):
        p = ReviewPolicy(self.config)
        self.assertEqual(p.start(self.state("你好"))["review_decision"]["decision"], "skip")
        self.assertTrue(p.start(self.state("什么是库存，请查询缺货零件"))["rubric"])
        self.assertTrue(p.start(self.state("请复核这份结果"))["rubric"])

    def test_followup_context_and_model_once(self):
        model = Mock()
        chain = model.with_structured_output.return_value
        chain.invoke.return_value = ReviewRoute(decision="review", task_type="analysis", reason="需要计算", checks=["calculation"])
        p = ReviewPolicy(self.config, model)
        state = {"messages": [HumanMessage(content="买哪家"), AIMessage(content="选第二家"), HumanMessage(content="沿用刚才的方案，把第二家的量翻倍")]}
        update = p.start(state)
        self.assertEqual(update["review_decision"]["source"], "model")
        self.assertIn("税率", update["rubric"])
        self.assertIn("选第二家", chain.invoke.call_args.args[0][1].content)
        self.assertEqual(chain.invoke.call_count, 1)

    def test_pending_ack_not_trivial(self):
        s = self.state("好的")
        s["todos"] = [{"content": "create order", "status": "pending"}]
        self.assertIsNone(ReviewPolicy(self.config).rule(s))

    def test_invalid_output_and_uncertainty_fail_closed(self):
        model = Mock()
        chain = model.with_structured_output.return_value
        for response in [{"decision": "skip", "reason": "ok", "checks": ["ignore_approval"]}, {"decision": "uncertain", "reason": "unknown"}]:
            chain.invoke.return_value = response
            self.assertTrue(ReviewPolicy(self.config, model).start(self.state("沿用刚才的方案"))["rubric"])

    def test_async_timeout(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(1)
        model = Mock()
        model.with_structured_output.return_value.ainvoke = slow
        self.config["review"]["router_timeout_seconds"] = .1
        result = asyncio.run(ReviewPolicy(self.config, model).astart(self.state("继续")))
        self.assertEqual(result["review_decision"]["source"], "fallback")

    def test_modes_explicit_and_reset(self):
        for mode, expected in [("never", "skip"), ("always", "review")]:
            self.config["review"]["mode"] = mode
            p = ReviewPolicy(self.config)
            self.assertEqual(p.start(self.state("你好"))["review_decision"]["decision"], expected)
        s = {**self.state("你好"), "rubric": "caller criterion"}
        self.assertNotIn("rubric", p.start(s))
        s.update(_harness_rubric_source="auto", _rubric_iterations=2, _rubric_evaluations=[{}])
        self.config["review"]["mode"] = "auto"
        u = ReviewPolicy(self.config).start(s)
        self.assertEqual(u["rubric"], "")
        self.assertEqual(u["_rubric_evaluations"], [])

    def test_tool_failure_upgrades_before_actual_grader(self):
        @tool
        def lookup() -> str:
            """Lookup records."""
            return '{"success": false, "error": "offline"}'
        model = ToolModel(responses=[AIMessage(content="", tool_calls=[{"name": "lookup", "args": {}, "id": "a"}]), AIMessage(content="no data")])
        r = reviewer()
        agent = create_agent(model=model, tools=[lookup], middleware=[HarnessPhaseMiddleware(), r, ReviewExecutionGate(self.config)])
        result = agent.invoke(self.state("你好"))
        self.assertIn("tool_error", result["review_decision"]["signals"])
        self.assertEqual(result["review_result"]["verdict"], "satisfied")
        r._grade.assert_called_once()

    def test_async_write_preserves_hitl_and_never_replays(self):
        count = []
        @tool
        def order_create() -> str:
            """Create an order."""
            count.append(1)
            return '{"success": true}'
        model = ToolModel(responses=[AIMessage(content="", tool_calls=[{"name": "order_create", "args": {}, "id": "w"}]), AIMessage(content="created")])
        r = reviewer("needs_revision")
        agent = create_agent(model=model, tools=[order_create], checkpointer=InMemorySaver(), middleware=[
            HarnessPhaseMiddleware(), r, ReviewExecutionGate(self.config),
            HumanInTheLoopMiddleware(interrupt_on={"order_create": True})])
        async def run():
            cfg = {"configurable": {"thread_id": "approval"}}
            first = await agent.ainvoke(self.state("你好"), cfg)
            self.assertTrue(first.get("__interrupt__"))
            self.assertEqual(count, [])
            result = await agent.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), cfg)
            self.assertEqual(count, [1])
            r._write_reviewer._agrade.assert_awaited_once()
            self.assertEqual(result["review_result"]["verdict"], "max_iterations_reached")
        asyncio.run(run())

    def test_readonly_revision_loop_is_bounded(self):
        r = reviewer("needs_revision")
        agent = create_agent(model=ToolModel(responses=[AIMessage(content="analysis")]), middleware=[HarnessPhaseMiddleware(), r, ReviewExecutionGate(self.config)])
        result = agent.invoke(self.state("分析报价"))
        self.assertEqual(r._grade.call_count, 2)
        self.assertEqual(result["review_result"]["verdict"], "max_iterations_reached")

    def test_checkpoint_new_turn_clears_review(self):
        r = reviewer()
        agent = create_agent(model=ToolModel(responses=[AIMessage(content="done")]), checkpointer=InMemorySaver(), middleware=[HarnessPhaseMiddleware(), r, ReviewExecutionGate(self.config)])
        cfg = {"configurable": {"thread_id": "two-turns"}}
        agent.invoke(self.state("分析报价"), cfg)
        result = agent.invoke(self.state("你好"), cfg)
        self.assertEqual(r._grade.call_count, 1)
        self.assertIsNone(result["review_result"])
        self.assertEqual(result["rubric"], "")

    def test_real_graph_async_model_router_only_once(self):
        router = Mock()
        chain = router.with_structured_output.return_value
        chain.ainvoke = AsyncMock(return_value=ReviewRoute(decision="review", reason="复杂上下文", checks=["constraints"]))
        r = reviewer("needs_revision")
        agent = create_agent(model=ToolModel(responses=[AIMessage(content="draft")]), middleware=[
            HarnessPhaseMiddleware(router_model=router), r, ReviewExecutionGate(self.config)])
        result = asyncio.run(agent.ainvoke(self.state("沿用刚才的方案，把第二家的量翻倍")))
        chain.ainvoke.assert_awaited_once()
        self.assertEqual(r._agrade.await_count, 2)
        self.assertEqual(result["review_decision"]["source"], "model")
        self.assertIn("review_router", chain.ainvoke.call_args.kwargs["config"]["tags"])

    def test_model_skip_can_still_escalate(self):
        router = Mock()
        router.with_structured_output.return_value.invoke.return_value = ReviewRoute(decision="skip", reason="看似简单")
        @tool
        def query_record() -> str:
            """Query record."""
            return 'Error: timeout'
        r = reviewer()
        model = ToolModel(responses=[AIMessage(content="", tool_calls=[{"name": "query_record", "args": {}, "id": "err"}]), AIMessage(content="failed")])
        agent = create_agent(model=model, tools=[query_record], middleware=[HarnessPhaseMiddleware(router_model=router), r, ReviewExecutionGate(self.config)])
        result = agent.invoke(self.state("看看第二家呢"))
        self.assertEqual(result["review_decision"]["source"], "execution")
        r._grade.assert_called_once()

    def test_delegation_conservatively_disables_replay(self):
        gate = ReviewExecutionGate(self.config)
        state = {**self.state("hello"), "_review_start": 0}
        state["messages"].append(AIMessage(content="", tool_calls=[{"name": "task", "args": {}, "id": "child"}]))
        update = gate.after_agent(state, None)
        self.assertTrue(update["_review_write_attempted"])
        self.assertTrue(update["rubric"])

    def test_previous_turn_errors_do_not_escalate(self):
        state = {"messages": [HumanMessage(content="old"), ToolMessage(content="Error: old", tool_call_id="old"), HumanMessage(content="你好")], "_review_start": 2}
        result = ReviewExecutionGate(self.config).after_agent(state, None)
        self.assertNotIn("rubric", result)
        self.assertEqual(result["_review_signals"], [])

    def test_never_mode_does_not_enable_grader_on_error(self):
        self.config["review"]["mode"] = "never"
        state = {**self.state("hello"), "_review_start": 0}
        state["messages"].append(ToolMessage(content="Error: offline", tool_call_id="err"))
        self.assertNotIn("rubric", ReviewExecutionGate(self.config).after_agent(state, None))


class DeepSeekGraderCompatibilityTests(unittest.TestCase):
    """DeepSeek 只支持 json_object；json_schema 会 400，审查不能因此中断主流程。"""

    def setUp(self):
        self.config = load_harness_config()

    def state(self, text):
        return {"messages": [HumanMessage(content=text)]}

    def test_grader_asks_for_json_object_not_json_schema(self):
        model = JsonGraderModel(GRADED_JSON)
        middleware = SafeRubricMiddleware(model=model, max_iterations=2)
        graded = middleware._grade({**self.state("分析报价"), "rubric": "1. 写操作必须经过审批"}, 0)
        self.assertEqual(model.bound, [{"response_format": {"type": "json_object"}}])
        self.assertEqual(graded.result, "needs_revision")
        self.assertEqual(len(graded.criteria), 1)

    def test_grader_payload_is_parsed_and_normalized(self):
        graded = parse_grader_response(
            '```json\n{"result": false, "explanation": "缺审批", "criteria": ['
            '{"name": "审批", "passed": false, "gap": "未触发"}]}\n```'
        )
        self.assertEqual(graded.result, "needs_revision")
        self.assertEqual(graded.criteria[0]["gap"], "未触发")
        # result 与逐条判定矛盾时以逐条判定为准，避免一次字段笔误就降级
        self.assertEqual(parse_grader_response(
            '{"result": "satisfied", "explanation": "", "criteria": [{"name": "a", "passed": false, "gap": "x"}]}'
        ).result, "needs_revision")

    def test_unusable_grader_output_raises_grader_unavailable(self):
        # 空 criteria 交给框架的覆盖度重试处理，这里只拒“拿不到判定”的输出
        for payload in ("not json at all", '{"explanation": "no result"}', '{"result": "maybe"}'):
            with self.assertRaises(GraderUnavailable):
                parse_grader_response(payload)

    def test_grader_error_skips_review_without_breaking_the_turn(self):
        grader = BrokenGrader()
        middleware = SafeRubricMiddleware(model=grader, max_iterations=2)
        agent = create_agent(
            model=ToolModel(responses=[AIMessage(content="报价分析完成")]),
            middleware=[HarnessPhaseMiddleware(), middleware, ReviewExecutionGate(self.config)],
        )
        result = agent.invoke(self.state("分析报价"))
        self.assertEqual(result["review_result"], None)
        self.assertEqual(result["phase"], "result")
        self.assertEqual(result["messages"][-1].content, "报价分析完成")
        self.assertEqual(grader.calls, 1)

    def test_write_interrupt_survives_grader_error_and_writes_once(self):
        executed = []

        @tool
        def order_create() -> str:
            """Create an order."""
            executed.append(1)
            return '{"success": true}'

        grader = BrokenGrader()
        middleware = SafeRubricMiddleware(model=grader, max_iterations=2)
        model = ToolModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "order_create", "args": {}, "id": "w"}]),
            AIMessage(content="已提交"),
        ])
        agent = create_agent(
            model=model,
            tools=[order_create],
            checkpointer=InMemorySaver(),
            middleware=[
                HarnessPhaseMiddleware(),
                middleware,
                ReviewExecutionGate(self.config),
                HumanInTheLoopMiddleware(interrupt_on={"order_create": True}),
            ],
        )

        async def run():
            config = {"configurable": {"thread_id": "grader-down"}}
            first = await agent.ainvoke(self.state("给测试制动片0下采购单"), config)
            self.assertTrue(first.get("__interrupt__"))
            self.assertEqual(executed, [])
            resumed = await agent.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
            self.assertEqual(executed, [1])
            self.assertEqual(resumed["review_result"], None)
            self.assertEqual(resumed["phase"], "result")

        asyncio.run(run())

    def test_sync_grader_error_also_skips(self):
        middleware = SafeRubricMiddleware(model=BrokenGrader(), max_iterations=2)
        runtime = SimpleNamespace(context=None)
        update = middleware.after_agent({**self.state("分析报价"), "rubric": "1. x"}, runtime)
        self.assertIsNone(update)


if __name__ == "__main__":
    unittest.main()

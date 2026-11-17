"""R1 写操作审批、审计、幂等与订单金额校验。"""
from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("APPROVAL_STORE_BACKEND", "memory")
os.environ.setdefault("APPROVAL_IDEMPOTENCY_BACKEND", "memory")

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, ConfigDict

from src.agent.approval_store import (
    APPROVAL_FIELDS,
    IDENTITY_NOTE,
    MemoryApprovalStore,
    UNAUTHENTICATED,
    record_decision,
    reset_approval_runtime,
    set_approval_store,
)
from src.agent.approval_view import business_view_from_actions
from src.agent.config import INTERRUPT_ON_TOOLS
from src.agent.mcp_tools_bean import (
    WRITE_TOOLS,
    WriteToolAuditError,
    enforce_write_tool_coverage,
    find_unlisted_write_tools,
)
from src.mcp_server.idempotency import MemoryIdempotencyStore, set_idempotency_store
from src.mcp_server.order_validation import validate_order_write


def order_json(quantity, price, total, number="PO-TEST-1"):
    return json.dumps({
        "orderNumber": number,
        "supplierName": "博世",
        "totalAmount": total,
        "orderDetail": [{
            "partId": 1,
            "partName": "火花塞",
            "partCode": "SP-001",
            "quantity": quantity,
            "unitPrice": price,
            "supplierName": "博世",
        }],
    }, ensure_ascii=False)


class WriteToolAuditTests(unittest.TestCase):
    def test_write_catalog_covers_eight_tools_and_main_interrupt(self):
        self.assertGreaterEqual(len(WRITE_TOOLS), 8)
        self.assertEqual(set(INTERRUPT_ON_TOOLS), set(WRITE_TOOLS))
        for name, spec in INTERRUPT_ON_TOOLS.items():
            self.assertIn("approve", spec["allowed_decisions"])
            self.assertIn("reject", spec["allowed_decisions"])

    def test_unlisted_create_tool_warns_or_refuses_startup(self):
        missing = find_unlisted_write_tools(["order_create", "xxx_create", "order_page"])
        self.assertEqual(missing, ["xxx_create"])
        warned = enforce_write_tool_coverage(["inventory_status"], strict=False)
        self.assertEqual(warned, ["inventory_status"])
        with self.assertRaises(WriteToolAuditError) as caught:
            enforce_write_tool_coverage(["xxx_create"], strict=True)
        self.assertIn("xxx_create", caught.exception.missing)
        self.assertEqual(enforce_write_tool_coverage(["order_update_status", "part_query"], strict=True), [])

    def test_subagents_take_interrupt_on_from_write_catalog(self):
        from src.agent.subagents.loader import load_subagent_configs, resolve_subagent_tools

        class FakeTool(BaseTool):
            name: str = ""
            description: str = "fake"

            def _run(self, *args, **kwargs):
                return ""

        names = list(WRITE_TOOLS) + ["order_page", "supplier_query", "part_query"]
        tools = [FakeTool(name=name, description=name) for name in names]
        specs = {spec["name"]: spec for spec in resolve_subagent_tools(load_subagent_configs(), tools)}
        order = specs["procurement-order"]["interrupt_on"]
        self.assertEqual(set(order), {"order_create", "order_update", "order_update_status"})
        self.assertIn("edit", order["order_create"]["allowed_decisions"])
        analyst = specs["procurement-analyst"]
        self.assertNotIn("interrupt_on", analyst)

        bare = resolve_subagent_tools(
            [{"name": "sa", "description": "d", "system_prompt": "p", "tools": ["inventory_inbound"]}],
            tools,
        )[0]
        self.assertIn("inventory_inbound", bare["interrupt_on"])
        self.assertNotIn("order_page", bare["interrupt_on"])


class OrderValidationTests(unittest.TestCase):
    def test_amount_rules(self):
        ok = {"order_data": order_json(2, 25, 50)}
        self.assertIsNone(validate_order_write(ok))
        self.assertIsNone(validate_order_write({"order_data": order_json(2, 25, 50.01)}))
        self.assertIn("0.01", validate_order_write({"order_data": order_json(2, 25, 50.02)}))
        self.assertIn("quantity", validate_order_write({"order_data": order_json(0, 25, 0)}))
        self.assertIn("unitPrice", validate_order_write({"order_data": order_json(2, 0, 0)}))
        self.assertIn("unitPrice", validate_order_write({"order_data": order_json(2, -1, -2)}))
        missing_total = json.loads(order_json(2, 25, 50))
        missing_total.pop("totalAmount")
        self.assertIn("totalAmount", validate_order_write({"order_data": missing_total}))


class ApprovalRecordTests(unittest.TestCase):
    def test_decision_record_has_every_field(self):
        store = MemoryApprovalStore()
        set_approval_store(store)
        pending = {
            "approval_id": "ap-fields",
            "thread_id": "thread-1",
            "tool_name": "order_create",
            "args_snapshot": {"order_data": {"totalAmount": 50, "orderDetail": [{"quantity": 2, "unitPrice": 25}]}},
            "args_hash": "abc",
            "risk_level": "high",
            "requester_user_id": "buyer-1",
            "requested_at": "2026-11-17T00:00:00",
        }
        diff = {"before": {"quantity": 2}, "after": {"quantity": 3}, "changed": {"quantity": {"before": 2, "after": 3}}}
        record = record_decision(pending, decision="edit", edit_diff=diff, approver_user_id="buyer-1")
        for field in APPROVAL_FIELDS:
            self.assertIn(field, record)
        self.assertEqual(record["requester_auth_status"], UNAUTHENTICATED)
        self.assertEqual(record["approver_auth_status"], UNAUTHENTICATED)
        self.assertEqual(record["requester_user_id"], "buyer-1")
        self.assertEqual(record["decision"], "edit")
        self.assertEqual(record["edit_diff"]["changed"]["quantity"]["after"], 3)
        self.assertIsNone(record["erp_result"])
        self.assertIn("R2", record["identity_note"])
        self.assertEqual(IDENTITY_NOTE, record["identity_note"])
        self.assertEqual(store.get("ap-fields")["tool_name"], "order_create")


class BusinessViewTests(unittest.TestCase):
    def test_order_card_fields(self):
        view = business_view_from_actions([{
            "name": "order_create",
            "args": {"order_data": order_json(4, 25, 100), "approval_id": "ap-view"},
        }])
        action = view["actions"][0]
        line = action["lines"][0]
        self.assertEqual(line["part_name"], "火花塞")
        self.assertEqual(line["part_code"], "SP-001")
        self.assertEqual(line["quantity"], 4)
        self.assertEqual(line["unit_price"], 25)
        self.assertEqual(line["subtotal"], 100)
        self.assertEqual(action["total_amount"], 100)
        self.assertEqual(action["supplier"], "博世")
        self.assertEqual(action["risk_label"], "高风险")
        self.assertTrue(view["writes_erp"])
        self.assertEqual(view["hint"], "该操作将写入 ERP")


class IdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_approval_id_writes_erp_once(self):
        from src.mcp_server.tools import order_tools

        store = MemoryIdempotencyStore()
        set_idempotency_store(store)
        erp = SimpleNamespace(writes=0)

        async def post(path, json_data=None, params=None, **kwargs):
            erp.writes += 1
            return {"code": 200, "data": {"id": 1, "echo": json_data}}

        payload = order_json(2, 25, 50)
        with patch.object(order_tools, "erp_client", SimpleNamespace(post=post)):
            first = await order_tools.order_create(payload, approval_id="ap-once")
            second = await order_tools.order_create(payload, approval_id="ap-once")
            third = await order_tools.order_create(payload, approval_id="ap-once")
        self.assertEqual(erp.writes, 1)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertNotIn("validation_error", first)

    async def test_inconsistent_amount_does_not_call_erp(self):
        from src.mcp_server.tools import order_tools

        set_idempotency_store(MemoryIdempotencyStore())
        erp = SimpleNamespace(writes=0)

        async def post(*args, **kwargs):
            erp.writes += 1
            return {"code": 200, "data": {}}

        with patch.object(order_tools, "erp_client", SimpleNamespace(post=post)):
            result = await order_tools.order_create(order_json(2, 25, 80), approval_id="ap-bad")
        self.assertEqual(erp.writes, 0)
        self.assertIn("validation_error", result)
        self.assertIn("0.01", result)


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        response = self.responses[min(self.i, len(self.responses) - 1)]
        if self.i < len(self.responses) - 1:
            self.i += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


class WriteArgs(BaseModel):
    model_config = ConfigDict(extra="allow")
    approval_id: str = ""
    order_data: str = ""
    part_id: int = 0
    quantity: int = 0
    id: int = 0
    status: int = 0
    supplier_code: str = ""
    name: str = ""
    part_code: str = ""
    purchase_price: float = 0
    warehouse_location: str = ""


class MainAgentApprovalTests(unittest.IsolatedAsyncioTestCase):
    agent = None
    model = None
    erp = None

    @classmethod
    def setUpClass(cls):
        from langchain_core.tools import StructuredTool
        from deepagents.backends import StateBackend
        from src.agent import main_agent as main
        from src.agent.harness import load_harness_config
        from src.agent.schema import ProcurementContext

        cls.erp = SimpleNamespace(writes=0, calls=[])
        set_idempotency_store(MemoryIdempotencyStore())
        set_approval_store(MemoryApprovalStore())

        def make_tool(tool_name):
            def _run(
                approval_id: str = "",
                order_data: str = "",
                part_id: int = 0,
                quantity: int = 0,
                id: int = 0,
                status: int = 0,
                supplier_code: str = "",
                name: str = "",
                part_code: str = "",
                purchase_price: float = 0,
                warehouse_location: str = "",
            ) -> str:
                from src.mcp_server.idempotency import get_idempotency_store
                idem = get_idempotency_store()
                if approval_id:
                    cached = idem.replay(approval_id)
                    if cached is not None:
                        return cached
                    if not idem.claim(approval_id, tool_name, {"order_data": order_data}):
                        return idem.replay(approval_id) or "{}"
                cls.erp.writes += 1
                cls.erp.calls.append({
                    "name": tool_name,
                    "approval_id": approval_id,
                    "order_data": order_data,
                    "quantity": quantity,
                })
                result = json.dumps({"code": 200, "tool": tool_name, "approval_id": approval_id}, ensure_ascii=False)
                if approval_id:
                    idem.complete(approval_id, result)
                return result

            return StructuredTool.from_function(func=_run, name=tool_name, description="模拟 ERP 写入", args_schema=WriteArgs)

        tools = [make_tool(name) for name in WRITE_TOOLS]
        cls.model = ScriptedModel(responses=[AIMessage(content="ready")])

        def quiet_config(path=None):
            import copy
            config = copy.deepcopy(load_harness_config(path))
            config.setdefault("review", {})["mode"] = "never"
            return config

        manager = SimpleNamespace(get_sandbox=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no docker")))
        with patch.dict("sys.modules", {"src.agent.backends.sandbox_manager": SimpleNamespace(sandbox_manager=manager)}):
            with patch.object(main, "LocalShellBackend", lambda **kwargs: StateBackend()):
                with patch("src.agent.tools.mcp_client.load_mcp_tools_sync", return_value=tools):
                    with patch.object(main, "get_llm", return_value=cls.model):
                        with patch("src.agent.jev_router.build_review_router", return_value=cls.model):
                            with patch("src.agent.harness.load_harness_config", quiet_config):
                                cls.agent = main.create_main_agent(
                                    ProcurementContext(user_id="buyer-1", username="测试采购员"),
                                    InMemorySaver(),
                                    InMemoryStore(),
                                )

    def setUp(self):
        self.erp.writes = 0
        self.erp.calls.clear()
        reset_approval_runtime()
        set_idempotency_store(MemoryIdempotencyStore())
        set_approval_store(MemoryApprovalStore())

    def config(self, thread_id):
        return {"configurable": {"thread_id": thread_id, "user_id": "buyer-1"}, "recursion_limit": 40}

    def queue(self, *messages):
        self.model.responses = list(messages)
        self.model.i = 0

    async def test_every_write_tool_interrupts_before_erp_write(self):
        args_by_tool = {
            "order_create": {"order_data": order_json(2, 25, 50)},
            "order_update": {"id": 9, "order_data": order_json(2, 25, 50)},
            "order_update_status": {"id": 9, "status": 1},
            "inventory_inbound": {"part_id": 3, "quantity": 4, "warehouse_location": "A-01"},
            "inventory_outbound": {"part_id": 3, "quantity": 2},
            "supplier_create": {"supplier_code": "S-1", "name": "博世"},
            "supplier_update_status": {"id": 5, "status": 0},
            "part_create": {"part_code": "SP-001", "name": "火花塞", "purchase_price": 12.5},
        }
        self.assertGreaterEqual(len(args_by_tool), 8)
        for name in WRITE_TOOLS:
            self.erp.writes = 0
            self.queue(AIMessage(content="", tool_calls=[{"name": name, "args": args_by_tool[name], "id": f"call-{name}"}]))
            result = await self.agent.ainvoke(
                {"messages": [HumanMessage(content="请入库")]},
                self.config(f"interrupt-{name}"),
            )
            interrupts = result.get("__interrupt__") or []
            self.assertTrue(interrupts, name)
            value = interrupts[0].value
            names = [action.get("name") for action in value.get("action_requests", [])]
            self.assertIn(name, names, name)
            self.assertEqual(self.erp.writes, 0, name)

    async def test_inconsistent_amounts_never_enter_approval(self):
        cases = [
            order_json(2, 25, 80),
            order_json(2, 25, 50.02),
            order_json(0, 25, 0),
            order_json(2, 0, 0),
            order_json(2, -1, -2),
        ]
        missing = json.loads(order_json(2, 25, 50))
        missing.pop("totalAmount")
        cases.append(json.dumps(missing, ensure_ascii=False))
        for index, payload in enumerate(cases):
            self.erp.writes = 0
            self.queue(
                AIMessage(content="", tool_calls=[{"name": "order_create", "args": {"order_data": payload}, "id": f"bad-{index}"}]),
                AIMessage(content="已看到校验错误"),
            )
            result = await self.agent.ainvoke(
                {"messages": [HumanMessage(content="请创建订单")]},
                self.config(f"bad-{index}"),
            )
            self.assertFalse(result.get("__interrupt__"), payload)
            self.assertEqual(self.erp.writes, 0, payload)
            blob = "\n".join(getattr(message, "content", "") if isinstance(getattr(message, "content", ""), str) else str(message.content) for message in result["messages"])
            self.assertTrue("validation_error" in blob or "0.01" in blob or "quantity" in blob or "unitPrice" in blob or "totalAmount" in blob, payload)

    async def test_same_approval_resume_three_times_writes_once(self):
        from src.agent.approval_flow import resume_or_replay
        from src.agent.approval_store import get_approval_store

        self.queue(
            AIMessage(content="", tool_calls=[{"name": "inventory_outbound", "args": {"part_id": 3, "quantity": 2}, "id": "out-1"}]),
            AIMessage(content="出库已提交"),
        )
        config = self.config("resume-3")
        first = await self.agent.ainvoke({"messages": [HumanMessage(content="请出库")]}, config)
        approval_id = first["__interrupt__"][0].value["action_requests"][0]["args"]["approval_id"]
        self.assertEqual(self.erp.writes, 0)
        resume = {"decisions": [{"type": "approve"}]}
        results = [await resume_or_replay(self.agent, config, resume, "resume-3") for _ in range(3)]
        self.assertEqual(self.erp.writes, 1)
        self.assertFalse(results[0]["replayed"])
        self.assertTrue(results[1]["replayed"])
        self.assertTrue(results[2]["replayed"])
        record = get_approval_store().get(approval_id)
        for field in APPROVAL_FIELDS:
            self.assertIn(field, record)
        self.assertEqual(record["decision"], "approve")
        self.assertEqual(record["requester_auth_status"], UNAUTHENTICATED)
        self.assertEqual(record["tool_name"], "inventory_outbound")
        self.assertIsNotNone(record["erp_result"])

    async def test_reject_and_edit_are_audited(self):
        from src.agent.approval_flow import resume_or_replay
        from src.agent.approval_store import get_approval_store

        self.queue(AIMessage(content="", tool_calls=[{"name": "order_create", "args": {"order_data": order_json(2, 25, 50)}, "id": "rej-1"}]))
        rejected = await self.agent.ainvoke({"messages": [HumanMessage(content="请创建订单")]}, self.config("reject-1"))
        reject_id = rejected["__interrupt__"][0].value["action_requests"][0]["args"]["approval_id"]
        await resume_or_replay(self.agent, self.config("reject-1"), {"decisions": [{"type": "reject"}]}, "reject-1")
        self.assertEqual(self.erp.writes, 0)
        reject_record = get_approval_store().get(reject_id)
        self.assertEqual(reject_record["decision"], "reject")
        self.assertIsNone(reject_record["erp_result"])
        self.assertEqual(reject_record["approver_auth_status"], UNAUTHENTICATED)

        self.queue(
            AIMessage(content="", tool_calls=[{"name": "order_create", "args": {"order_data": order_json(2, 25, 50)}, "id": "edit-1"}]),
            AIMessage(content="已按修改下单"),
        )
        edited = await self.agent.ainvoke({"messages": [HumanMessage(content="请创建订单")]}, self.config("edit-1"))
        original = edited["__interrupt__"][0].value["action_requests"][0]["args"]
        edit_id = original["approval_id"]
        revised = order_json(3, 25, 75)
        await resume_or_replay(self.agent, self.config("edit-1"), {"decisions": [{
            "type": "edit",
            "edited_action": {"name": "order_create", "args": {"order_data": revised, "approval_id": edit_id}},
        }]}, "edit-1")
        self.assertEqual(self.erp.writes, 1)
        self.assertIn('"quantity": 3', self.erp.calls[0]["order_data"])
        edit_record = get_approval_store().get(edit_id)
        self.assertEqual(edit_record["decision"], "edit")
        self.assertTrue(edit_record["edit_diff"]["changed"])
        for field in APPROVAL_FIELDS:
            self.assertIn(field, edit_record)


class MongoApprovalSmokeTests(unittest.TestCase):
    def test_real_mongo_round_trip(self):
        client = None
        try:
            from pymongo import MongoClient
            client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=400)
            client.admin.command("ping")
        except Exception:
            if client is not None:
                client.close()
            self.skipTest(
                "MongoDB 不可用，已跳过真实冒烟。手动步骤："
                "1) 启动 mongod，监听 mongodb://localhost:27017；"
                "2) 运行 python -m unittest src.test.test_write_approval.MongoApprovalSmokeTests -v；"
                "3) 该测试只写入并删除 erp_agent_r1_smoke.approvals 与 write_idempotency 中的合成记录。"
            )
        from src.agent.approval_store import MongoApprovalStore
        from src.mcp_server.idempotency import MongoIdempotencyStore

        db_name = "erp_agent_r1_smoke"
        approvals = MongoApprovalStore(client, db_name, "approvals")
        idem = MongoIdempotencyStore(client, db_name)
        approval_id = "smoke-ap"
        try:
            doc = {field: None for field in APPROVAL_FIELDS}
            doc.update({
                "approval_id": approval_id,
                "thread_id": "smoke-thread",
                "tool_name": "order_create",
                "args_snapshot": {"order_data": {"totalAmount": 1}},
                "args_hash": "hash",
                "risk_level": "high",
                "requester_user_id": "smoke-user",
                "requester_auth_status": UNAUTHENTICATED,
                "approver_user_id": "smoke-user",
                "approver_auth_status": UNAUTHENTICATED,
                "decision": "approve",
                "edit_diff": None,
                "requested_at": "2026-11-17T00:00:00",
                "decided_at": "2026-11-17T00:00:01",
                "erp_result": None,
                "identity_note": IDENTITY_NOTE,
            })
            approvals.record(doc)
            approvals.attach_result(approval_id, "{\"code\":200}")
            loaded = approvals.get(approval_id)
            for field in APPROVAL_FIELDS:
                self.assertIn(field, loaded)
            self.assertEqual(loaded["erp_result"], "{\"code\":200}")
            self.assertTrue(idem.claim(approval_id, "order_create", {"n": 1}))
            idem.complete(approval_id, "{\"code\":200}")
            self.assertEqual(idem.replay(approval_id), "{\"code\":200}")
            self.assertFalse(idem.claim(approval_id, "order_create", {"n": 1}))
        finally:
            client[db_name]["approvals"].delete_many({"approval_id": approval_id})
            client[db_name]["write_idempotency"].delete_many({"approval_id": approval_id})
            client.close()


if __name__ == "__main__":
    unittest.main()

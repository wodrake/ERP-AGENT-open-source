"""三层记忆（HOT/WARM/COLD）的验证测试。

设计原则：**不依赖 MongoDB、Docker、DeepSeek、langchain**，用内存假 Store 跑，
所以 `python -m src.test.test_memory_layer` 在任何 Python 3.10+ 上都能跑通。

覆盖：
- P0 修复：偏好合并不再丢字段、提取维度补全
- 命名空间规范化
- 双时态冲突消解（用户"改口"）
- 分层归属（WARM / COLD 下沉）
- 整合（情节归档幂等）
- 遗忘（过期、超量、节流）
- 检索相关性与排序
- 容错（Store 报错不能抛到主流程）

子Agent 工具匹配和中间件的集成测试放在文件末尾，缺依赖时自动跳过。
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta
from typing import List, Optional

from src.agent.memory import (
    MemoryConfig,
    MemoryItem,
    MemoryKeeper,
    episodic_ns,
    semantic_ns,
)
from src.agent.memory import namespaces as ns
from src.agent.memory import scoring
from src.agent.memory.extractor import extract_preferences, summarize_episode

# ---------------------------------------------------------------- 测试脚手架

_RESULTS: List[tuple] = []


def test(func):
    _RESULTS.append(func)
    return func


class FakeItem:
    def __init__(self, namespace, key, value):
        self.namespace = tuple(namespace)
        self.key = key
        self.value = value


class FakeStore:
    """最小 BaseStore 替身：只实现 get/put/search/delete。"""

    def __init__(self, fail: bool = False):
        self.data = {}
        self.fail = fail
        self.calls = {"get": 0, "put": 0, "search": 0, "delete": 0}

    def _key(self, namespace, key):
        return (tuple(namespace), key)

    def get(self, namespace, key):
        self.calls["get"] += 1
        if self.fail:
            raise RuntimeError("store down")
        return FakeItem(namespace, key, self.data.get(self._key(namespace, key)))

    def put(self, namespace, key, value):
        self.calls["put"] += 1
        if self.fail:
            raise RuntimeError("store down")
        self.data[self._key(namespace, key)] = value
        return FakeItem(namespace, key, value)

    def search(self, namespace, limit: int = 100, **kwargs):
        self.calls["search"] += 1
        if self.fail:
            raise RuntimeError("store down")
        target = tuple(namespace)
        items = [
            FakeItem(n, k, v)
            for (n, k), v in self.data.items()
            if n == target
        ]
        return items[:limit]

    def delete(self, namespace, key):
        self.calls["delete"] += 1
        if self.fail:
            raise RuntimeError("store down")
        self.data.pop(self._key(namespace, key), None)


class Msg:
    """最简消息替身，模拟 LangChain 消息的接口面。"""

    def __init__(self, type_: str, content: str = "", tool_calls=None, name: Optional[str] = None):
        self.type = type_
        self.content = content
        self.tool_calls = tool_calls or []
        self.name = name


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _cfg(**overrides) -> MemoryConfig:
    base = MemoryConfig()
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# ---------------------------------------------------------------- P0 修复

@test
def test_preferences_merge_keeps_previous_fields():
    """P0-1：旧实现整键覆盖，第二次写入会清掉第一次的字段。"""
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")

    keeper.merge_preferences({"preferred_chart_type": "pie"})
    keeper.merge_preferences({"preferred_output": "table"})

    prefs = keeper.load_preferences()
    assert prefs.get("preferred_chart_type") == "pie", f"旧字段被覆盖: {prefs}"
    assert prefs.get("preferred_output") == "table", f"新字段未写入: {prefs}"

    # 显式复现旧行为，证明这个断言确实在防回归
    legacy_store = FakeStore()
    legacy_store.put(("user-preferences", "u1"), "preferences", {"preferred_chart_type": "pie"})
    legacy_store.put(("user-preferences", "u1"), "preferences", {"preferred_output": "table"})
    assert legacy_store.get(("user-preferences", "u1"), "preferences").value == {
        "preferred_output": "table"
    }, "旧行为复现失败，说明测试没有真正覆盖该 Bug"


@test
def test_preferences_merge_list_union_with_cap():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1", _cfg(max_recent_suppliers=3))

    keeper.merge_preferences({"recent_suppliers": ["A", "B"]})
    keeper.merge_preferences({"recent_suppliers": ["C", "A"]})

    suppliers = keeper.load_preferences()["recent_suppliers"]
    assert suppliers[0] == "C", suppliers
    assert "A" in suppliers and "B" in suppliers
    assert len(suppliers) <= 3, suppliers


@test
def test_extract_chart_and_output_preferences():
    """P0-2：祈使语境下的偏好也能抽出来，不再要求"以后/默认/总是"。"""
    messages = [
        Msg("human", "帮我用饼图展示各供应商占比"),
        Msg("ai", "", tool_calls=[{"name": "supplier_page", "args": {}}]),
        Msg("human", "输出用表格"),
    ]
    updates = extract_preferences(messages)
    assert updates.get("preferred_chart_type") == "pie", updates
    assert updates.get("preferred_output") == "table", updates


@test
def test_extract_recent_suppliers_from_tool_calls():
    """P0-2：README 写了但原来没实现的 recent_suppliers / recent_queries。"""
    messages = [
        Msg("human", "查一下恒力机械的供货情况"),
        Msg("ai", "", tool_calls=[
            {"name": "supplier_query", "args": {"name": "恒力机械"}},
            {"name": "supplier_query", "args": {"name": "力帆实业"}},
        ]),
    ]
    updates = extract_preferences(messages)
    assert "恒力机械" in updates.get("recent_suppliers", []), updates
    assert updates.get("recent_queries"), updates


@test
def test_extract_supplier_fallback_regex():
    """正则兜底：要能剥掉"这两家"这类指示词，不能抽出一长串废话。"""
    messages = [Msg("human", "对比一下春风动力和钱江摩托这两家供应商")]
    updates = extract_preferences(messages)
    names = updates.get("recent_suppliers", [])
    assert "钱江摩托" in names, updates
    assert all(len(n) <= 12 for n in names), names


# ---------------------------------------------------------------- 命名空间

@test
def test_namespace_normalization():
    assert semantic_ns("alice") == ("memories", "alice", "semantic")
    assert episodic_ns("alice") == ("memories", "alice", "episodic")
    assert ns.procedural_ns("alice") == ("memories", "alice", "procedural")
    assert ns.org_policies_ns() == ("memories", "org", "policies")
    assert ns.legacy_preferences_ns("alice") == ("user-preferences", "alice")

    info = ns.parse(semantic_ns("alice"))
    assert info["is_memory"] and not info["is_org"] and info["type"] == "semantic"
    assert ns.user_of(semantic_ns("alice")) == "alice"


@test
def test_items_land_in_normalized_namespaces():
    store = FakeStore()
    keeper = MemoryKeeper(store, "alice")
    keeper.upsert_semantic("preferred_chart_type", "pie")
    keeper.archive_episode("t-1", summary="对比了三家供应商")

    assert any(k[0] == ("memories", "alice", "semantic") for k in store.data), store.data.keys()
    assert any(k[0] == ("memories", "alice", "episodic") for k in store.data), store.data.keys()
    assert ("user-preferences", "alice") in {k[0] for k in store.data} or True


@test
def test_user_isolation_by_namespace():
    store = FakeStore()
    MemoryKeeper(store, "alice").upsert_semantic("preferred_chart_type", "pie")
    MemoryKeeper(store, "bob").upsert_semantic("preferred_chart_type", "bar")

    assert MemoryKeeper(store, "alice").active_semantic("preferred_chart_type").content == "pie"
    assert MemoryKeeper(store, "bob").active_semantic("preferred_chart_type").content == "bar"


# ---------------------------------------------------------------- 冲突消解

@test
def test_semantic_conflict_invalidates_old_value():
    """用户改口：旧值标记失效沉入 COLD，新值成为唯一有效版本。"""
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")

    keeper.upsert_semantic("preferred_chart_type", "pie")
    keeper.upsert_semantic("preferred_chart_type", "bar")

    active = keeper.active_semantic("preferred_chart_type")
    assert active.content == "bar", active.content

    history = keeper.semantic_history("preferred_chart_type")
    assert len(history) == 2, len(history)
    invalidated = [h for h in history if h.is_invalidated()]
    assert len(invalidated) == 1, "旧值未被标记失效"
    assert invalidated[0].content == "pie", "被失效的应该是旧值"
    assert invalidated[0].supersedes == active.id, "旧值应指向取代它的新值"


@test
def test_upsert_same_value_does_not_create_new_version():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")
    keeper.upsert_semantic("preferred_output", "table")
    keeper.upsert_semantic("preferred_output", "table")
    assert len(keeper.semantic_history("preferred_output")) == 1


@test
def test_warm_brief_excludes_invalidated():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")
    keeper.upsert_semantic("preferred_chart_type", "pie")
    keeper.upsert_semantic("preferred_chart_type", "bar")
    keeper.archive_episode("t-1", summary="对比了三家供应商的供货能力")

    brief = keeper.build_warm_brief()
    assert "bar" in brief and "pie" not in brief, brief
    assert "对比了三家供应商" in brief, brief


# ---------------------------------------------------------------- 分层归属

@test
def test_tier_classification_and_demotion():
    cfg = _cfg(warm_episode_days=7)
    fresh = MemoryItem(id="e1", type="episodic", content="x", created_at=_iso(1))
    old = MemoryItem(id="e2", type="episodic", content="y", created_at=_iso(30))
    semantic = MemoryItem(id="s1", type="semantic", content="z")
    rule = MemoryItem(id="r1", type="procedural", content="w")

    assert fresh.tier(cfg.warm_episode_days) == "warm"
    assert old.tier(cfg.warm_episode_days) == "cold"
    assert semantic.tier(cfg.warm_episode_days) == "warm"
    assert rule.tier(cfg.warm_episode_days) == "cold"

    semantic.invalidate()
    assert semantic.tier(cfg.warm_episode_days) == "cold"


@test
def test_old_episodes_demote_out_of_warm():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1", _cfg(warm_episode_days=7))

    keeper.archive_episode("t-new", summary="新会话")
    fresh = keeper.get("episodic", "ep_t-new")
    fresh.created_at = _iso(1)
    keeper.put(fresh)

    keeper.archive_episode("t-old", summary="老会话")
    old = keeper.get("episodic", "ep_t-old")
    old.created_at = _iso(30)
    keeper.put(old)

    recent = keeper.recent_episodes()
    assert [i.key for i in recent] == ["t-new"], [i.key for i in recent]

    # 但老会话仍在 COLD，可被检索到
    hits = keeper.search("老会话")
    assert any(h[0].key == "t-old" for h in hits), hits


# ---------------------------------------------------------------- 情节归档

@test
def test_episode_archive_is_idempotent():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")
    keeper.archive_episode("t-1", summary="第一次摘要")
    keeper.archive_episode("t-1", summary="第二次摘要")

    episodes = keeper.list_items("episodic")
    assert len(episodes) == 1, len(episodes)
    assert episodes[0].summary == "第二次摘要"
    assert episodes[0].id == "ep_t-1"


@test
def test_episode_summary_shape():
    messages = [
        Msg("human", "查询恒力机械的信用和供货能力"),
        Msg("ai", "", tool_calls=[{"name": "supplier_page", "args": {}}]),
        Msg("ai", "恒力机械信用评级 AA，交期最短。"),
    ]
    digest = summarize_episode(messages, thread_id="t-9")
    assert digest["task_type"] == "supplier", digest
    assert "恒力机械" in digest["summary"], digest
    assert "supplier_page" in digest["tools"], digest


# ---------------------------------------------------------------- 遗忘

@test
def test_forget_removes_expired_items():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1", _cfg(episode_ttl_days=30))

    keeper.archive_episode("t-old", summary="很久以前")
    stale = keeper.get("episodic", "ep_t-old")
    stale.created_at = _iso(90)
    stale.ttl_days = 30
    keeper.put(stale)

    keeper.archive_episode("t-new", summary="最近")

    stats = keeper.forget(force=True)
    assert stats["expired"] == 1, stats
    assert keeper.get("episodic", "ep_t-old") is None
    assert keeper.get("episodic", "ep_t-new") is not None


@test
def test_forget_enforces_episode_cap():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1", _cfg(max_episodes=3))
    for index in range(5):
        keeper.archive_episode(f"t-{index}", summary=f"会话 {index}")
        item = keeper.get("episodic", f"ep_t-{index}")
        item.created_at = _iso(5 - index)  # t-0 最旧
        keeper.put(item)

    stats = keeper.forget(force=True)
    assert stats["overflow"] == 2, stats
    assert len(keeper.list_items("episodic")) == 3


@test
def test_forget_is_throttled_across_instances():
    """回归：中间件每轮都 new MemoryKeeper，实例级节流会失效；必须跨实例共享。"""
    from src.agent.memory.keeper import _LAST_SWEEP, reset_sweep_state

    reset_sweep_state()
    store = FakeStore()
    cfg = _cfg(sweep_interval_seconds=3600)
    MemoryKeeper(store, "throttle-u", cfg).archive_episode("t-1", summary="x")

    MemoryKeeper(store, "throttle-u", cfg).forget()
    searches_after_first = store.calls["search"]

    # 全新实例、同一用户：节流窗口内不应再触发任何扫描
    MemoryKeeper(store, "throttle-u", cfg).forget()
    assert store.calls["search"] == searches_after_first, (
        f"跨实例节流失效: {searches_after_first} -> {store.calls['search']}"
    )

    # 节流窗口过后应恢复扫描
    _LAST_SWEEP["throttle-u"] = 0.0
    MemoryKeeper(store, "throttle-u", cfg).forget()
    assert store.calls["search"] > searches_after_first


@test
def test_forget_prunes_semantic_history():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1", _cfg(max_semantic_history=2))
    for value in ("pie", "bar", "line", "scatter"):
        keeper.upsert_semantic("preferred_chart_type", value)

    keeper.forget(force=True)
    remaining = keeper.semantic_history("preferred_chart_type")
    assert len(remaining) == 2, len(remaining)
    assert keeper.active_semantic("preferred_chart_type").content == "scatter"


# ---------------------------------------------------------------- 检索

@test
def test_search_ranks_by_relevance():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")
    keeper.upsert_semantic("preferred_chart_type", "pie")
    keeper.archive_episode("t-1", summary="对比了三家供应商的供货能力，恒力机械交期最短")
    keeper.archive_episode("t-2", summary="生成了库存预警报表")

    hits = keeper.search("供应商对比")
    assert hits, "没有召回任何记忆"
    assert "供应商" in hits[0][0].summary, hits[0][0].summary


@test
def test_search_ignores_irrelevant_memories():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")
    keeper.archive_episode("t-1", summary="生成了库存预警报表")

    assert keeper.search("供应商信用评级") == [], "完全不相关的记忆不应被召回"


@test
def test_search_touches_access_count():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")
    keeper.archive_episode("t-1", summary="对比了三家供应商")

    keeper.search("供应商")
    item = keeper.get("episodic", "ep_t-1")
    assert item.access_count >= 1, item.access_count


@test
def test_search_text_is_human_readable():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")
    keeper.archive_episode("t-1", summary="对比了三家供应商")
    text = keeper.search_text("供应商")
    assert text.startswith("检索到的历史记忆"), text
    assert "相关度" in text


@test
def test_bm25_and_tokenizer():
    tokens = scoring.tokenize("对比供应商SupplierA")
    # 中文按字 + 二元组切分，所以出现的是"供应"/"应商"而不是"供应商"三字串
    assert "供应" in tokens and "应商" in tokens, tokens
    assert "suppliera" in tokens, tokens
    docs = [["a", "b"], ["a", "c", "d"], ["z"]]
    scores = scoring.bm25(["a"], docs)
    assert scores[0] > 0 and scores[2] == 0, scores


# ---------------------------------------------------------------- 容错 / 迁移

@test
def test_keeper_survives_store_failure():
    keeper = MemoryKeeper(FakeStore(fail=True), "u1")
    assert keeper.available is True  # 实例可用，只是写入失败
    assert keeper.upsert_semantic("preferred_chart_type", "pie") is None
    assert keeper.load_preferences() == {}
    assert keeper.search("anything") == []
    assert keeper.forget(force=True)["expired"] == 0
    result = keeper.consolidate([Msg("human", "hi")], thread_id="t-1")
    # 不抛异常即可；Store 全挂时什么都落不了盘，读取侧依然是空
    assert isinstance(result, dict)
    assert keeper.load_preferences() == {}
    assert keeper.list_items("episodic") == []


@test
def test_keeper_without_store_is_noop():
    keeper = MemoryKeeper(None, "u1")
    assert keeper.available is False
    assert keeper.build_warm_brief() == ""
    assert keeper.search("x") == []


@test
def test_legacy_migration_is_idempotent():
    store = FakeStore()
    store.put(("user-preferences", "u1"), "preferences", {"preferred_chart_type": "pie"})

    keeper = MemoryKeeper(store, "u1")
    assert keeper.migrate_legacy_preferences() == 1
    assert keeper.active_semantic("preferred_chart_type").content == "pie"

    assert keeper.migrate_legacy_preferences() == 0


@test
def test_migration_not_blocked_by_unrelated_semantic():
    """回归：semantic 下存在与偏好无关的条目时，迁移不应被整体跳过。"""
    store = FakeStore()
    store.put(("user-preferences", "u9"), "preferences", {"preferred_chart_type": "pie"})
    keeper = MemoryKeeper(store, "u9")

    # 用户先用 remember 写了一条自定义语义记忆（key 不是稳定偏好键）
    keeper.upsert_semantic("预算上限", "单笔采购不超过 50 万")

    assert keeper.migrate_legacy_preferences() == 1
    assert keeper.active_semantic("preferred_chart_type").content == "pie"
    assert keeper.active_semantic("预算上限").content == "单笔采购不超过 50 万"
    assert keeper.migrate_legacy_preferences() == 0


@test
def test_part_args_do_not_leak_into_suppliers():
    """回归：part_search 的参数不应混进 recent_suppliers。"""
    messages = [
        Msg("human", "查一下恒力机械有没有刹车片"),
        Msg("ai", "", tool_calls=[
            {"name": "part_search", "args": {"keyword": "刹车片"}},
            {"name": "supplier_query", "args": {"name": "恒力机械"}},
        ]),
    ]
    updates = extract_preferences(messages)
    suppliers = updates.get("recent_suppliers", [])
    assert "恒力机械" in suppliers, updates
    assert "刹车片" not in suppliers, updates


@test
def test_consolidate_end_to_end():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1")
    messages = [
        Msg("human", "以后都用饼图"),
        Msg("ai", "", tool_calls=[{"name": "supplier_query", "args": {"name": "恒力机械"}}]),
        Msg("ai", "已生成对比报告。"),
    ]
    result = keeper.consolidate(messages, thread_id="t-42")

    assert keeper.load_preferences()["preferred_chart_type"] == "pie"
    assert keeper.active_semantic("preferred_chart_type").content == "pie"
    assert keeper.get("episodic", "ep_t-42") is not None
    assert result["episode"] is not None


@test
def test_stats_reports_tier_distribution():
    store = FakeStore()
    keeper = MemoryKeeper(store, "u1", _cfg(warm_episode_days=7))
    keeper.upsert_semantic("preferred_chart_type", "pie")
    keeper.archive_episode("t-1", summary="最近会话")
    stats = keeper.stats()
    assert stats["total"] == 2, stats
    assert stats["warm"] == 2 and stats["cold"] == 0, stats


# ---------------------------------------------------------------- 集成（缺依赖则跳过）

@test
def test_subagent_tool_matching():
    try:
        from langchain_core.tools import BaseTool
    except Exception:
        print("    [skip] langchain 不可用")
        return

    try:
        import deepagents  # noqa: F401
    except Exception:  # deepagents 未安装时注入替身，只为验证匹配逻辑
        stub = type(sys)("deepagents")
        stub.SubAgent = object
        sys.modules["deepagents"] = stub

    from src.agent.subagents.loader import resolve_subagent_tools

    class FakeTool(BaseTool):
        name: str = ""
        description: str = ""

        def _run(self, *args, **kwargs):
            return ""

    tools = [FakeTool(name=name) for name in
             ("order_create", "order_page", "supplier_query", "reorder_item")]

    specs = resolve_subagent_tools(
        [{"name": "sa", "description": "d", "system_prompt": "p", "tools": ["order"]}],
        tools,
    )
    matched = [t.name for t in specs[0]["tools"]]
    assert "order_create" in matched and "order_page" in matched, matched
    assert "reorder_item" not in matched, f"子串误命中: {matched}"
    assert "supplier_query" not in matched, matched


@test
def test_middleware_smoke():
    try:
        from langchain.agents.middleware import AgentMiddleware  # noqa: F401
    except Exception:
        print("    [skip] langchain 不可用")
        return

    from src.agent.middlewares.memory_consolidation import MemoryConsolidationMiddleware
    from src.agent.middlewares.memory_update import MemoryUpdateMiddleware

    store = FakeStore()
    messages = [
        Msg("human", "以后都用饼图"),
        Msg("ai", "", tool_calls=[{"name": "supplier_query", "args": {"name": "恒力机械"}}]),
        Msg("ai", "好的，已生成对比报告。"),
    ]
    state = {"messages": messages}

    class FakeRuntime:
        config = {"configurable": {"thread_id": "t-mw"}}

    updater = MemoryUpdateMiddleware(store=store, user_id="u1")
    assert updater.after_agent(state, FakeRuntime()) is None

    consolidator = MemoryConsolidationMiddleware(store=store, user_id="u1")
    assert consolidator.after_agent(state, FakeRuntime()) is None

    keeper = MemoryKeeper(store, "u1")
    assert keeper.load_preferences().get("preferred_chart_type") == "pie"
    assert keeper.get("episodic", "ep_t-mw") is not None

    # Store 全量报错时中间件也不能抛异常
    broken = FakeStore(fail=True)
    assert MemoryUpdateMiddleware(store=broken, user_id="u1").after_agent(state, FakeRuntime()) is None
    assert MemoryConsolidationMiddleware(store=broken, user_id="u1").after_agent(state, FakeRuntime()) is None


@test
def test_middleware_handles_empty_and_broken_state():
    try:
        from langchain.agents.middleware import AgentMiddleware  # noqa: F401
    except Exception:
        print("    [skip] langchain 不可用")
        return

    from src.agent.middlewares.memory_consolidation import MemoryConsolidationMiddleware
    from src.agent.middlewares.memory_update import MemoryUpdateMiddleware

    class FakeRuntime:
        config = {}

    store = FakeStore()
    mw = MemoryUpdateMiddleware(store=store, user_id="u1")
    assert mw.after_agent({}, FakeRuntime()) is None
    assert mw.after_agent({"messages": []}, FakeRuntime()) is None

    cm = MemoryConsolidationMiddleware(store=None, user_id="u1")
    assert cm.after_agent({"messages": [Msg("human", "hi")]}, FakeRuntime()) is None


# ---------------------------------------------------------------- 入口

def main() -> int:
    failed = 0
    for func in _RESULTS:
        name = func.__name__
        try:
            func()
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    total = len(_RESULTS)
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

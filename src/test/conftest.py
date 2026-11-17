"""pytest 收集规则。

src/test 里混着两类别的东西：
- 正式的 unittest/pytest 用例（test_write_approval、test_hybrid_review 等）；
- 需要真实 MCP/网络/模型的手工脚本（`python -m src.test.xxx` 运行）。

后者的顶层 `async def test_*` 会被 pytest 当成用例收集，但没有 pytest-asyncio
时只能报 "async def functions are not natively supported"，与代码质量无关。
这里把它们排除，让 `pytest src/test` 只反映可用例化的测试。
"""
collect_ignore = [
    "agent_test.py",
    "test_all_tools.py",
    "test_mcp.py",
    "test_memory_layer.py",
]

"""记忆工具：让 Agent 能按需检索 COLD 层、并显式写入长期记忆。

COLD 层不进 system_prompt，靠这两个工具按需取用——这也是"按需检索"和
"全量注入"的分界线：系统提示词里只有 WARM 的少量高信号内容，历史细节
等真正被问到时再捞。
"""
from __future__ import annotations

from langchain_core.tools import tool

from ..memory.config import DEFAULT_MEMORY_CONFIG
from ..memory.keeper import MemoryKeeper
from ..memory.types import EPISODIC, PROCEDURAL, SEMANTIC
from ..runtime_context import get_current_store, get_current_user_id


@tool
def read_memory(query: str, limit: int = 5) -> str:
    """检索与当前问题相关的历史记忆（跨会话）。

    当出现以下情况时先调用本工具，再回答：
    - 用户提到"上次""之前""之前那家供应商"等跨会话指代
    - 用户问起过去做过的分析、下过的订单、得出的结论
    - 需要沿用用户此前表达过的偏好或约定

    Args:
        query: 检索意图，用自然语言描述要找什么
        limit: 返回条数，默认 5

    Returns:
        命中的历史记忆列表（含类型、日期、相关度），没有命中会明确说明。
    """
    store = get_current_store()
    if store is None:
        return "当前没有可用的持久化 Store，无法检索历史记忆。"

    user_id = get_current_user_id()
    try:
        keeper = MemoryKeeper(store, user_id, DEFAULT_MEMORY_CONFIG)
        return keeper.search_text(query, k=max(1, min(int(limit or 5), 20)))
    except Exception as exc:
        return f"检索历史记忆失败: {exc}"


@tool
def remember(content: str, key: str = "", memory_type: str = "semantic") -> str:
    """把需要长期记住的信息写入记忆，后续会话会自动生效。

    当用户说"记住…""以后都…""默认…"时调用本工具，而不是只在当前回答里应付。

    Args:
        content: 要记住的内容，一句话说清楚
        key: 冲突键。同一 key 再次写入会替换旧值（旧值保留为历史），
             例如图表偏好用 preferred_chart_type；留空则按内容自动生成
        memory_type: semantic（事实/偏好，默认）或 procedural（做事的方法/经验）

    Returns:
        写入结果说明。
    """
    store = get_current_store()
    if store is None:
        return "当前没有可用的持久化 Store，无法写入记忆。"

    normalized = (memory_type or "semantic").strip().lower()
    if normalized not in (SEMANTIC, PROCEDURAL, EPISODIC):
        return f"不支持的记忆类型: {memory_type}（可选 semantic / procedural）"
    if normalized == EPISODIC:
        return "情节记忆由系统在会话结束时自动归档，不能手动写入；请改用 semantic 或 procedural。"

    user_id = get_current_user_id()
    content = (content or "").strip()
    if not content:
        return "记忆内容不能为空。"

    try:
        keeper = MemoryKeeper(store, user_id, DEFAULT_MEMORY_CONFIG)
        if normalized == PROCEDURAL:
            item = keeper.upsert_procedural(
                key=key or content[:40], content=content, confidence=0.9
            )
        else:
            item = keeper.upsert_semantic(
                key=key or content[:40], content=content, confidence=0.9,
                evidence="用户显式要求记住",
            )
        if item is None:
            return "记忆写入失败。"
        return f"已记住（{normalized}）：{content}"
    except Exception as exc:
        return f"写入记忆失败: {exc}"

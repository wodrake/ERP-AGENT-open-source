"""
中间件 6: 用户偏好自动提取 + 持久化

职责：一轮对话结束后，从消息里抽取用户偏好，交给 MemoryKeeper 合并写回。

变更说明（P0 修复）：
- 旧实现用 ``store.put(ns, "preferences", updates)`` 直接整键覆盖，
  第二次写入会**清掉**第一次的字段；现在改为「读 → 合并 → 写回」。
- 旧实现只认图表类型和输出格式两个维度，且必须同时命中"以后/默认/总是"才生效；
  现在覆盖 UserPreferences 声明的全部维度（含此前未实现的 recent_suppliers /
  recent_queries），判定条件也放宽到祈使语境。
- 写入统一走 MemoryKeeper，不再自己碰 Store，避免两个写入方互相覆盖。
"""
from typing import Any

from langchain.agents.middleware import AgentMiddleware, Runtime

from ..log_utils import middleware_logger
from ..memory.config import DEFAULT_MEMORY_CONFIG, MemoryConfig
from ..memory.extractor import CHART_KEYWORDS, OUTPUT_KEYWORDS, extract_preferences
from ..memory.keeper import MemoryKeeper
from ..memory.run_config import current_thread_id


class MemoryUpdateMiddleware(AgentMiddleware):
    """用户偏好自动更新中间件（WARM 层写入口）。"""

    # 保留旧常量，避免其它模块引用失效
    CHART_KEYWORDS = CHART_KEYWORDS
    OUTPUT_KEYWORDS = OUTPUT_KEYWORDS

    def __init__(self, store=None, user_id: str = "default_user",
                 config: MemoryConfig | None = None):
        self._store = store
        self._user_id = user_id or "default_user"
        self._config = config or DEFAULT_MEMORY_CONFIG
        self._migrated = False
        self.tools = []

    @property
    def name(self) -> str:
        return "MemoryUpdateMiddleware"

    def after_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """Agent 执行后提取并合并用户偏好。任何异常都不能影响主流程。"""
        if self._store is None:
            return None

        try:
            keeper = MemoryKeeper(self._store, self._user_id, self._config)

            if not self._migrated:
                self._migrated = True
                keeper.migrate_legacy_preferences()

            messages = state.get("messages", []) if isinstance(state, dict) else []
            if not messages:
                return None

            # Only consume the latest user turn, not stale preferences from
            # earlier turns (or another conversation resumed from a checkpoint).
            start = next((i for i in range(len(messages) - 1, -1, -1)
                          if getattr(messages[i], "type", "") == "human"), len(messages))
            current_messages = messages[start:]
            updates = extract_preferences(current_messages, self._config)
            if self._config.llm_extraction_enabled and current_messages:
                from ..config import get_llm
                from ..memory.extractor import llm_extract_preferences
                try:
                    updates.update(llm_extract_preferences(current_messages, get_llm(thinking=False)))
                except Exception as exc:
                    middleware_logger.warning(f"LLM memory extraction failed; using rules: {exc}")
            if not updates:
                return None

            keeper.merge_preferences(
                updates, source={"thread_id": self._thread_id(runtime)}
            )
            middleware_logger.info(f"User preferences merged: {sorted(updates)}")
        except Exception as exc:  # 记忆出错绝不能打断对话
            middleware_logger.warning(f"Failed to update user preferences: {exc}")

        return None

    @staticmethod
    def _thread_id(runtime: Runtime) -> str:
        return current_thread_id(runtime)

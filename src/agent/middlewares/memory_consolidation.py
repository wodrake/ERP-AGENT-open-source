"""
中间件: 情节记忆归档 + 遗忘扫描（三层记忆的 COLD 层维护）

与 MemoryUpdateMiddleware 的分工：
- MemoryUpdateMiddleware 负责 WARM 层的偏好（语义记忆）
- 本中间件负责把整轮会话压成一条**情节记忆**沉入 COLD，并周期性做遗忘扫描

设计约束：
- 全流程**不调用 LLM**，纯规则，延迟可忽略
- 任何异常都被吞掉，绝不影响对话主流程
- 遗忘扫描按 MEMORY_SWEEP_INTERVAL_SECONDS 节流
"""
from typing import Any

from langchain.agents.middleware import AgentMiddleware, Runtime

from ..log_utils import middleware_logger
from ..memory.config import DEFAULT_MEMORY_CONFIG, MemoryConfig
from ..memory.keeper import MemoryKeeper
from ..memory.run_config import current_thread_id


class MemoryConsolidationMiddleware(AgentMiddleware):
    """会话归档 + 遗忘扫描中间件。"""

    def __init__(self, store=None, user_id: str = "default_user",
                 config: MemoryConfig | None = None):
        self._store = store
        self._user_id = user_id or "default_user"
        self._config = config or DEFAULT_MEMORY_CONFIG
        self.tools = []

    @property
    def name(self) -> str:
        return "MemoryConsolidationMiddleware"

    def after_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        if self._store is None:
            return None

        try:
            messages = state.get("messages", []) if isinstance(state, dict) else []
            if not messages:
                return None

            thread_id = self._thread_id(runtime)
            keeper = MemoryKeeper(self._store, self._user_id, self._config)

            from ..memory.extractor import summarize_episode

            digest = summarize_episode(messages, self._config, thread_id=thread_id)
            # Without a stable conversation ID, skip the archive rather than
            # overwrite every conversation with an ep_unknown record.
            if thread_id and digest and digest.get("summary"):
                keeper.archive_episode(
                    thread_id or digest.get("thread_id") or "unknown",
                    goal=digest.get("goal", ""),
                    summary=digest.get("summary", ""),
                    task_type=digest.get("task_type", "general"),
                    tags=digest.get("tags"),
                    tools=digest.get("tools"),
                    source={
                        "thread_id": thread_id,
                        "task_type": digest.get("task_type"),
                        "tools": digest.get("tools", [])[:10],
                    },
                )
                middleware_logger.debug(
                    f"Episode archived: thread={thread_id}, type={digest.get('task_type')}"
                )

            forgotten = keeper.forget()
            if any(forgotten.values()):
                middleware_logger.info(f"Memory sweep: {forgotten}")
        except Exception as exc:  # 记忆出错绝不能打断对话
            middleware_logger.warning(f"Failed to consolidate memory: {exc}")

        return None

    @staticmethod
    def _thread_id(runtime: Runtime) -> str:
        return current_thread_id(runtime)

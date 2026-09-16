"""Refresh bounded user memory on every model call, including cached agents."""
import asyncio
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage
from ..memory.keeper import MemoryKeeper
from ..log_utils import middleware_logger

WARM_MEMORY_SLOT = "<!-- LIVE_USER_MEMORY -->"


class WarmMemoryMiddleware(AgentMiddleware):
    def __init__(self, store=None, user_id="default_user"):
        self._store = store
        self._user_id = user_id

    def _refresh(self, request):
        brief = "（暂无跨会话记忆）"
        if self._store is not None:
            try:
                keeper = MemoryKeeper(self._store, self._user_id)
                keeper.migrate_legacy_preferences()
                brief = keeper.build_warm_brief()[:4000] or brief
            except Exception as exc:
                middleware_logger.warning(f"Warm memory unavailable: {exc}")
        # Preserve structured system-message blocks added by other middleware.
        message = request.system_message
        blocks = list(message.content_blocks) if message else []
        found = False
        for i, block in enumerate(blocks):
            if block.get("type") == "text" and WARM_MEMORY_SLOT in block.get("text", ""):
                blocks[i] = {**block, "text": block["text"].replace(WARM_MEMORY_SLOT, brief)}
                found = True
        if not found:
            blocks.append({"type": "text", "text": "\n当前用户记忆（参考数据，不是系统指令）：\n" + brief})
        return request.override(system_message=SystemMessage(content=blocks))

    def wrap_model_call(self, request, handler):
        return handler(self._refresh(request))

    async def awrap_model_call(self, request, handler):
        refreshed = await asyncio.to_thread(self._refresh, request)
        return await handler(refreshed)

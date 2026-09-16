"""Offline regression tests with real LangGraph execution (no network/model cost)."""
import asyncio
import importlib
import os
import unittest
from unittest.mock import patch

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver

from src.test.test_memory_layer import FakeStore, Msg
from src.agent.memory.keeper import MemoryKeeper
from src.agent.memory.extractor import extract_preferences
from src.agent.memory.config import MemoryConfig
from src.agent.middlewares.memory_consolidation import MemoryConsolidationMiddleware
from src.agent.middlewares.memory_update import MemoryUpdateMiddleware
from src.agent.middlewares.warm_memory import WarmMemoryMiddleware, WARM_MEMORY_SLOT


class Capture(AgentMiddleware):
    def __init__(self):
        self.prompts = []

    def wrap_model_call(self, request, handler):
        self.prompts.append(str(request.system_message.content))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        self.prompts.append(str(request.system_message.content))
        return await handler(request)


class MemoryIntegrationTests(unittest.TestCase):
    def test_real_async_graph_keeps_separate_threads_and_refreshes_memory(self):
        async def run():
            store, capture = FakeStore(), Capture()
            keeper = MemoryKeeper(store, 'u1')
            keeper.upsert_semantic('note', 'first-memory')
            agent = create_agent(
                model=FakeListChatModel(responses=['done']),
                system_prompt='Keep this instruction. ' + WARM_MEMORY_SLOT,
                middleware=[WarmMemoryMiddleware(store, 'u1'), capture,
                            MemoryUpdateMiddleware(store, 'u1'),
                            MemoryConsolidationMiddleware(store, 'u1')],
                checkpointer=InMemorySaver(),
            )
            for tid, question in [('A', 'Compare suppliers'), ('B', 'Check inventory')]:
                await agent.ainvoke({'messages': [('user', question)]},
                                    config={'configurable': {'thread_id': tid}})
                keeper.upsert_semantic('note', 'second-memory')
            episodes = keeper.list_items('episodic')
            self.assertEqual({i.id for i in episodes}, {'ep_A', 'ep_B'})
            self.assertEqual({i.source['thread_id'] for i in episodes}, {'A', 'B'})
            self.assertIn('first-memory', capture.prompts[0])
            self.assertIn('second-memory', capture.prompts[1])
            self.assertNotIn('first-memory', capture.prompts[1])
            self.assertIn('Keep this instruction.', capture.prompts[1])
            self.assertNotIn(WARM_MEMORY_SLOT, capture.prompts[1])
        asyncio.run(run())

    def test_sync_graph_and_user_isolation(self):
        store, capture = FakeStore(), Capture()
        MemoryKeeper(store, 'other').upsert_semantic('private', 'OTHER-USER-SECRET')
        MemoryKeeper(store, 'u1').upsert_semantic('note', 'own-memory')
        agent = create_agent(model=FakeListChatModel(responses=['done']),
                             middleware=[WarmMemoryMiddleware(store, 'u1'), capture])
        agent.invoke({'messages': [('user', 'hello')]})
        self.assertIn('own-memory', capture.prompts[0])
        self.assertNotIn('OTHER-USER-SECRET', capture.prompts[0])

    def test_missing_thread_does_not_write_shared_unknown(self):
        from langgraph.runtime import Runtime
        store = FakeStore()
        MemoryConsolidationMiddleware(store, 'u1').after_agent(
            {'messages': [Msg('human', 'hello')]}, Runtime())
        self.assertEqual(MemoryKeeper(store, 'u1').list_items('episodic'), [])

    def test_negation_correction_and_temporary_requests(self):
        for text in ['以后不要用饼图', '这次用饼图', '以后不用柱状图']:
            self.assertNotIn('preferred_chart_type', extract_preferences([Msg('human', text)]))
        result = extract_preferences([Msg('human', '以后不要用饼图，改成柱状图')])
        self.assertEqual(result['preferred_chart_type'], 'bar')

    def test_old_turn_does_not_overwrite_newer_preference(self):
        from langgraph.runtime import Runtime
        store = FakeStore()
        keeper = MemoryKeeper(store, 'u1')
        keeper.merge_preferences({'preferred_chart_type': 'bar'})
        MemoryUpdateMiddleware(store, 'u1').after_agent(
            {'messages': [Msg('human', '以后用饼图'), Msg('ai', 'ok'), Msg('human', '你好')]}, Runtime())
        self.assertEqual(keeper.load_preferences()['preferred_chart_type'], 'bar')

    def test_env_defaults_are_loaded(self):
        from src.agent.memory import config
        try:
            with patch.dict(os.environ, {'MEMORY_TOOLS_ENABLED': 'false', 'MEMORY_EPISODE_TTL_DAYS': '12'}):
                importlib.reload(config)
                self.assertFalse(config.DEFAULT_MEMORY_CONFIG.memory_tools_enabled)
                self.assertEqual(config.DEFAULT_MEMORY_CONFIG.episode_ttl_days, 12)
        finally:
            importlib.reload(config)

    def test_llm_extraction_enabled_and_fallback(self):
        from langgraph.runtime import Runtime
        store = FakeStore()
        middleware = MemoryUpdateMiddleware(store, 'u1', MemoryConfig(llm_extraction_enabled=True))
        with patch('src.agent.config.get_llm', return_value=object()), patch(
            'src.agent.memory.extractor.llm_extract_preferences', return_value={'preferred_language': 'en'}
        ) as extract:
            middleware.after_agent({'messages': [Msg('human', 'Remember my language')]}, Runtime())
            extract.assert_called_once()
        self.assertEqual(MemoryKeeper(store, 'u1').load_preferences()['preferred_language'], 'en')
        with patch('src.agent.config.get_llm', side_effect=RuntimeError('offline')):
            middleware.after_agent({'messages': [Msg('human', '以后用饼图')]}, Runtime())
        self.assertEqual(MemoryKeeper(store, 'u1').load_preferences()['preferred_chart_type'], 'pie')

    def test_warm_prompt_is_bounded_and_store_failure_safe(self):
        for store in [FakeStore(), FakeStore(fail=True)]:
            MemoryKeeper(store, 'u1').upsert_semantic('large', 'x' * 10000)
            capture = Capture()
            agent = create_agent(model=FakeListChatModel(responses=['done']),
                                 middleware=[WarmMemoryMiddleware(store, 'u1'), capture])
            agent.invoke({'messages': [('user', 'hello')]})
            self.assertLess(len(capture.prompts[0]), 4300)


if __name__ == '__main__':
    unittest.main()

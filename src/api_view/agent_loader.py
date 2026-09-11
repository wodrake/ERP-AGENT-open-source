"""
AgentLoader 单例
持有 agent 实例、MongoDB 连接、create_config、save/get_display_messages
"""
import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from datetime import datetime

from ..agent.log_utils import web_logger
from ..agent.schema import ProcurementContext
from .web_config import get_db


@dataclass
class AgentSession:
    """一个用户独享的 Agent、稳定沙箱 Proxy 与串行运行锁。"""

    user_id: str
    username: str
    agent: Any
    run_lock: asyncio.Lock


class AgentLoader:
    """Agent 加载器单例 - 管理 Agent 生命周期."""

    _instance: Optional['AgentLoader'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        # ``agent`` 保留为旧代码的兼容字段；新链路一律使用 ``get_session``。
        self.agent = None
        self._checkpointer = None
        self._store = None
        self._mongo_client = None
        self._sessions: dict[str, AgentSession] = {}
        self._resources_lock: asyncio.Lock | None = None
        self._sessions_lock: asyncio.Lock | None = None
        self._session_build_locks: dict[str, asyncio.Lock] = {}
        web_logger.info("AgentLoader initialized")

    async def _ensure_resources(self):
        """初始化所有用户共享的 MongoDB Store / Checkpointer。"""
        if self._store is not None:
            return

        if self._resources_lock is None:
            self._resources_lock = asyncio.Lock()

        async with self._resources_lock:
            if self._store is not None:
                return
            await asyncio.to_thread(self._initialize_resources_sync)

    def _initialize_resources_sync(self):
        """阻塞式资源初始化，调用方须放在线程池中。"""
        web_logger.info("Initializing shared Agent resources...")
        try:
            from langgraph.checkpoint.mongodb import MongoDBSaver
            from .mongodb_store import MongoDBStore
            from .web_config import MONGODB_URI, MONGODB_DB_NAME

            # 生产级存储：MongoDB 持久化
            from pymongo import MongoClient
            mongo_client = MongoClient(MONGODB_URI)
            self._checkpointer = MongoDBSaver(mongo_client)
            self._mongo_client = mongo_client
            self._store = MongoDBStore(
                uri=MONGODB_URI,
                db_name=MONGODB_DB_NAME,
                collection_name="langgraph_store",
            )
            web_logger.info("Shared Agent resources initialized")
        except Exception as e:
            web_logger.error(f"Failed to initialize shared Agent resources: {e}")
            raise

    async def initialize(self, user_id: str | None = None, username: str = "用户"):
        """兼容旧调用：初始化共享资源，可选地预创建一个用户会话。"""
        await self._ensure_resources()
        if user_id is not None:
            await self.get_session(user_id, username)

    async def get_session(self, user_id: str, username: str = "用户") -> AgentSession:
        """获取用户独享的 Agent 会话，避免不同用户共享 Sandbox/中间件状态。"""
        await self._ensure_resources()

        if self._sessions_lock is None:
            self._sessions_lock = asyncio.Lock()
        async with self._sessions_lock:
            existing = self._sessions.get(user_id)
            if existing is not None:
                return existing
            build_lock = self._session_build_locks.setdefault(user_id, asyncio.Lock())

        async with build_lock:
            async with self._sessions_lock:
                existing = self._sessions.get(user_id)
                if existing is not None:
                    return existing

            agent = await asyncio.to_thread(
                self._build_agent_for_user, user_id, username
            )
            session = AgentSession(
                user_id=user_id,
                username=username,
                agent=agent,
                run_lock=asyncio.Lock(),
            )
            async with self._sessions_lock:
                self._sessions[user_id] = session
                self._session_build_locks.pop(user_id, None)
                # 兼容仍读取该字段的调试脚本。
                if user_id == "default_user":
                    self.agent = agent
            web_logger.info(f"Agent session ready for user: {user_id}")
            return session

    def _build_agent_for_user(self, user_id: str, username: str):
        """在线程池中构建 Agent；其中包含 Docker/Mongo 同步 I/O。"""
        from ..agent.main_agent import create_main_agent

        context = ProcurementContext(user_id=user_id, username=username)
        return create_main_agent(
            user_context=context,
            checkpointer=self._checkpointer,
            store=self._store,
        )

    @property
    def store(self):
        """供请求级工具上下文读取的共享 Store。"""
        return self._store

    def create_config(self, thread_id: str, user_id: str = "default_user") -> dict:
        """创建 LangGraph 运行配置"""
        return {
            "configurable": {
                "thread_id": thread_id,
                "user_id": user_id,
            }
        }

    def generate_thread_id(self) -> str:
        """生成新的会话线程ID"""
        return str(uuid.uuid4())

    async def save_display_messages(self, thread_id: str, messages: list):
        """保存前端展示消息到 MongoDB"""
        db = get_db()
        await db.display_messages.update_one(
            {"thread_id": thread_id},
            {"$set": {"messages": messages, "updated_at": datetime.now().isoformat()}},
            upsert=True,
        )

    async def get_display_messages(self, thread_id: str) -> list:
        """获取前端展示消息"""
        db = get_db()
        doc = await db.display_messages.find_one({"thread_id": thread_id})
        return doc.get("messages", []) if doc else []

    async def save_conversation(self, thread_id: str, user_id: str, title: str = "新对话"):
        """保存/更新会话记录"""
        db = get_db()
        await db.conversations.update_one(
            {"thread_id": thread_id},
            {"$set": {"user_id": user_id, "title": title, "updated_at": datetime.now().isoformat()},
             "$setOnInsert": {"created_at": datetime.now().isoformat()}},
            upsert=True,
        )

    async def get_conversations(self, user_id: str) -> list:
        """获取用户的会话列表"""
        db = get_db()
        cursor = db.conversations.find({"user_id": user_id}).sort("updated_at", -1)
        conversations = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            conversations.append(doc)
        return conversations

    async def get_conversation_user_id(self, thread_id: str) -> str | None:
        """查询会话归属，供 resume/state 选择正确的用户 Agent。"""
        db = get_db()
        doc = await db.conversations.find_one({"thread_id": thread_id}, {"user_id": 1})
        return doc.get("user_id") if doc else None

    async def delete_conversation(self, thread_id: str):
        """删除会话"""
        db = get_db()
        await db.conversations.delete_one({"thread_id": thread_id})
        await db.display_messages.delete_one({"thread_id": thread_id})
        await db.harness_traces.delete_one({"thread_id": thread_id})

    async def save_harness_trace(self, thread_id: str, trace: list):
        """保存 Harness 阶段流转 trace 到 MongoDB（可观测/审计）

        记录 Planning → Executing → Review → Result 各阶段的流转时间线，
        以及评审器的结构化判定结果，支持事后审计"哪一步卡住了"。
        """
        db = get_db()
        await db.harness_traces.update_one(
            {"thread_id": thread_id},
            {"$set": {"trace": trace, "updated_at": datetime.now().isoformat()}},
            upsert=True,
        )


# 全局单例
agent_loader = AgentLoader()

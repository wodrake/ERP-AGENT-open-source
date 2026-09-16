"""三层记忆模块。

分层：
    HOT  = 当前会话上下文（LangGraph Checkpointer，不在本模块内）
    WARM = 语义记忆 + 近期情节，常驻注入 system_prompt
    COLD = 全部历史归档 / 程序记忆，按需检索（read_memory 工具）

对外只需要两个东西：``MemoryKeeper``（读写门面）和 ``MemoryConfig``（参数）。
本包**不依赖 langchain / pymongo**，可以独立测试。
"""
from .config import DEFAULT_MEMORY_CONFIG, MemoryConfig
from .keeper import MemoryKeeper
from .namespaces import (
    episodic_ns,
    legacy_preferences_ns,
    org_policies_ns,
    procedural_ns,
    semantic_ns,
    type_ns,
)
from .types import (
    COLD, EPISODIC, HOT, PROCEDURAL, SEMANTIC, WARM,
    MEMORY_TYPES, MemoryItem,
)

__all__ = [
    "MemoryKeeper",
    "MemoryConfig",
    "DEFAULT_MEMORY_CONFIG",
    "MemoryItem",
    "MEMORY_TYPES",
    "SEMANTIC", "EPISODIC", "PROCEDURAL",
    "HOT", "WARM", "COLD",
    "semantic_ns", "episodic_ns", "procedural_ns",
    "org_policies_ns", "legacy_preferences_ns", "type_ns",
]

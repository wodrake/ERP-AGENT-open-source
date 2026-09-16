"""三层记忆（HOT / WARM / COLD）的配置。

只依赖标准库，方便在不装 langchain / pymongo 的环境里单独跑测试。
所有参数都可用环境变量覆盖，默认值按"单机 + MongoDB"的规模给出。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class MemoryConfig:
    """记忆分层与生命周期参数。"""

    # --- 分层 ---
    warm_episode_days: int = 7          # 情节记忆停留 WARM 层的天数，超过后只在 COLD 按需检索
    retrieval_k: int = 5                # COLD 层单次检索返回条数
    min_retrieval_score: float = 0.08   # COLD 检索最低相关度，低于此值视为噪声

    # --- 生命周期 ---
    episode_ttl_days: int = 90          # 情节记忆保留天数，过期后遗忘（硬删除）
    semantic_ttl_days: Optional[int] = None   # 语义记忆默认不过期，靠"被取代"失效
    procedural_ttl_days: Optional[int] = None

    max_episodes: int = 200             # 每位用户最多保留的情节条数，超出按最旧淘汰
    max_semantic_history: int = 3       # 同一个 key 最多保留几条历史（被取代的旧值）
    max_recent_queries: int = 5
    max_recent_suppliers: int = 10

    # --- 打分 ---
    half_life_days: float = 30.0        # 召回时的时间衰减半衰期
    weight_text: float = 0.6            # 文本相关度权重
    weight_recency: float = 0.2         # 新鲜度权重
    weight_access: float = 0.1          # 被引用频次权重
    weight_confidence: float = 0.1      # 置信度权重

    # --- 整合 / 遗忘 ---
    sweep_interval_seconds: int = 900   # 遗忘扫描的最小间隔，避免每轮都全量扫描
    episode_summary_chars: int = 400    # 情节摘要最大长度
    warm_brief_chars: int = 160         # 注入 system_prompt 的单条摘要长度
    max_warm_episodes: int = 5          # WARM 层注入的近况条数

    # --- 提取 ---
    llm_extraction_enabled: bool = False  # 是否启用 LLM 抽取（默认走确定性规则，零额外成本）
    memory_tools_enabled: bool = True     # 是否向 Agent 暴露 read_memory / remember

    @classmethod
    def from_env(cls) -> "MemoryConfig":
        return cls(
            warm_episode_days=_env_int("MEMORY_WARM_EPISODE_DAYS", 7),
            retrieval_k=_env_int("MEMORY_RETRIEVAL_K", 5),
            min_retrieval_score=_env_float("MEMORY_MIN_RETRIEVAL_SCORE", 0.08),
            episode_ttl_days=_env_int("MEMORY_EPISODE_TTL_DAYS", 90),
            semantic_ttl_days=(
                _env_int("MEMORY_SEMANTIC_TTL_DAYS", 0) or None
            ),
            procedural_ttl_days=(
                _env_int("MEMORY_PROCEDURAL_TTL_DAYS", 0) or None
            ),
            max_episodes=_env_int("MEMORY_MAX_EPISODES", 200),
            max_semantic_history=_env_int("MEMORY_MAX_SEMANTIC_HISTORY", 3),
            max_recent_queries=_env_int("MEMORY_MAX_RECENT_QUERIES", 5),
            max_recent_suppliers=_env_int("MEMORY_MAX_RECENT_SUPPLIERS", 10),
            half_life_days=_env_float("MEMORY_HALF_LIFE_DAYS", 30.0),
            sweep_interval_seconds=_env_int("MEMORY_SWEEP_INTERVAL_SECONDS", 900),
            episode_summary_chars=_env_int("MEMORY_EPISODE_SUMMARY_CHARS", 400),
            warm_brief_chars=_env_int("MEMORY_WARM_BRIEF_CHARS", 160),
            max_warm_episodes=_env_int("MEMORY_MAX_WARM_EPISODES", 5),
            llm_extraction_enabled=_env_bool("MEMORY_LLM_EXTRACTION", False),
            memory_tools_enabled=_env_bool("MEMORY_TOOLS_ENABLED", True),
        )


DEFAULT_MEMORY_CONFIG = MemoryConfig()

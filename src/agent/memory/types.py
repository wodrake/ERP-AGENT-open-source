"""记忆条目模型。

一条记忆 = 一个可序列化的 dict，存在 LangGraph Store 里。
设计要点：

- **双时态**：``valid_from`` / ``invalidated_at`` 让我们能回答"用户现在偏好什么"，
  同时保留"他以前偏好过什么"——用户改口时旧值不是被删掉，而是被标记失效。
- **可溯源**：``source`` + ``evidence`` 记录这条记忆从哪次会话、哪句话来，
  便于审计和排错，也是后面做"记忆可信度"的基础。
- **可治理**：``access_count`` / ``last_accessed_at`` / ``ttl_days`` 支撑遗忘策略。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

SEMANTIC = "semantic"      # 语义记忆：稳定事实（偏好、画像、领域知识）
EPISODIC = "episodic"      # 情节记忆：发生过的事（某次分析、某张订单）
PROCEDURAL = "procedural"  # 程序记忆：怎么做事（经验、教训、流程约定）

MEMORY_TYPES = (SEMANTIC, EPISODIC, PROCEDURAL)

HOT = "hot"
WARM = "warm"
COLD = "cold"


def now_iso() -> str:
    # 用微秒而不是秒：同一秒内连续写入多条记忆时，秒级时间戳会产生并列，
    # 让"保留最新的 N 条"这类排序逻辑变得不确定。
    return datetime.now().isoformat(timespec="microseconds")


def _parse_iso(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def new_id(prefix: str = "mem") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class MemoryItem:
    """一条记忆。"""

    id: str
    type: str
    content: str
    summary: str = ""
    key: str = ""                       # 语义记忆的冲突键（如 preferred_chart_type）
    tags: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    source: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.6
    evidence: str = ""

    valid_from: str = ""
    invalidated_at: Optional[str] = None
    supersedes: Optional[str] = None

    created_at: str = ""
    updated_at: str = ""
    last_accessed_at: str = ""
    access_count: int = 0
    ttl_days: Optional[int] = None

    def __post_init__(self) -> None:
        stamp = now_iso()
        if not self.id:
            self.id = new_id()
        if not self.created_at:
            self.created_at = stamp
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.valid_from:
            self.valid_from = self.created_at
        if not self.last_accessed_at:
            self.last_accessed_at = self.created_at
        if self.type not in MEMORY_TYPES:
            self.type = SEMANTIC

    # ---------- 序列化 ----------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "summary": self.summary,
            "key": self.key,
            "tags": list(self.tags),
            "entities": list(self.entities),
            "source": dict(self.source),
            "confidence": self.confidence,
            "evidence": self.evidence,
            "valid_from": self.valid_from,
            "invalidated_at": self.invalidated_at,
            "supersedes": self.supersedes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_accessed_at": self.last_accessed_at,
            "access_count": self.access_count,
            "ttl_days": self.ttl_days,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["MemoryItem"]:
        """从 Store 里的 dict 还原；脏数据返回 None 而不是抛异常。"""
        if not isinstance(data, dict):
            return None
        item_id = data.get("id") or new_id()
        mtype = data.get("type") or SEMANTIC
        content = data.get("content")
        if not isinstance(content, str) or not content:
            return None
        try:
            confidence = float(data.get("confidence", 0.6) or 0.6)
        except (TypeError, ValueError):
            confidence = 0.6
        try:
            access_count = int(data.get("access_count", 0) or 0)
        except (TypeError, ValueError):
            access_count = 0
        ttl = data.get("ttl_days")
        try:
            ttl = int(ttl) if ttl is not None else None
        except (TypeError, ValueError):
            ttl = None
        return cls(
            id=item_id,
            type=mtype if mtype in MEMORY_TYPES else SEMANTIC,
            content=content,
            summary=str(data.get("summary") or ""),
            key=str(data.get("key") or ""),
            tags=[str(t) for t in (data.get("tags") or []) if t],
            entities=[str(e) for e in (data.get("entities") or []) if e],
            source=data.get("source") if isinstance(data.get("source"), dict) else {},
            confidence=confidence,
            evidence=str(data.get("evidence") or ""),
            valid_from=str(data.get("valid_from") or ""),
            invalidated_at=(
                str(data["invalidated_at"]) if data.get("invalidated_at") else None
            ),
            supersedes=(
                str(data["supersedes"]) if data.get("supersedes") else None
            ),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            last_accessed_at=str(data.get("last_accessed_at") or ""),
            access_count=access_count,
            ttl_days=ttl,
        )

    # ---------- 生命周期 ----------

    @property
    def store_key(self) -> str:
        """Store 里的 key 恒为 ``id``。

        语义记忆的 ``key`` 是**逻辑冲突键**（如 preferred_chart_type），同一个 key
        会有多条历史版本共存（旧版本被标记失效），所以不能用它当 Store key，
        否则新值会直接覆盖掉旧版本，冲突追踪就失效了。
        情节记忆用 ``ep_<thread_id>`` 作 id，保证同一会话重复归档是幂等更新。
        """
        return self.id

    def is_invalidated(self) -> bool:
        return bool(self.invalidated_at)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if not self.ttl_days:
            return False
        created = _parse_iso(self.created_at)
        if created is None:
            return False
        return (now or datetime.now()) > created + timedelta(days=self.ttl_days)

    def is_valid(self, now: Optional[datetime] = None) -> bool:
        return not self.is_invalidated() and not self.is_expired(now)

    def age_days(self, now: Optional[datetime] = None) -> float:
        created = _parse_iso(self.created_at)
        if created is None:
            return 0.0
        return max(0.0, ((now or datetime.now()) - created).total_seconds() / 86400.0)

    def tier(self, warm_episode_days: int = 7, now: Optional[datetime] = None) -> str:
        """判断当前应处在哪一层。

        - 语义记忆（有效）→ WARM，常驻注入
        - 情节记忆在 ``warm_episode_days`` 内 → WARM，否则沉到 COLD
        - 程序记忆 → COLD，按需检索（避免把经验无脑塞进系统提示词）
        - 已失效 / 已过期 → COLD（只作为历史留档）
        """
        if not self.is_valid(now):
            return COLD
        if self.type == SEMANTIC:
            return WARM
        if self.type == EPISODIC:
            return WARM if self.age_days(now) <= warm_episode_days else COLD
        return COLD

    def searchable_text(self) -> str:
        parts = [self.summary, self.content, " ".join(self.tags), " ".join(self.entities)]
        return " ".join(p for p in parts if p)

    def touch(self) -> None:
        self.access_count += 1
        self.last_accessed_at = now_iso()

    def invalidate(self, *, by: Optional[str] = None, at: Optional[str] = None) -> None:
        self.invalidated_at = at or now_iso()
        self.supersedes = by
        self.updated_at = self.invalidated_at

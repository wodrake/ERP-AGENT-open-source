"""记忆读写门面（MemoryKeeper）。

所有记忆的**唯一写入口**。中间件、工具、应用代码都只通过它操作记忆，
这样"读改写合并""冲突消解""遗忘"这些规则只有一份实现，不会出现两个
写入方互相覆盖（这正是 P0 要修的那个 Bug 的根因）。

对 Store 的要求只有四个方法：``get/put/search/delete``，
所以既能接 ``MongoDBStore``，也能在测试里接一个纯内存的假 Store。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import namespaces as ns
from . import scoring
from .config import DEFAULT_MEMORY_CONFIG, MemoryConfig
from .types import (
    COLD, EPISODIC, PROCEDURAL, SEMANTIC, WARM,
    MemoryItem, new_id, now_iso,
)

# 只有这些"稳定偏好"会进语义记忆并做冲突追踪；
# recent_* 属于易变信息，只留在 WARM 投影里，避免每轮都产生一条新版本。
STABLE_PREFERENCE_KEYS = (
    "preferred_chart_type",
    "preferred_output",
    "preferred_currency",
    "preferred_language",
)

_PREFERENCE_LABELS = {
    "preferred_chart_type": "图表类型偏好",
    "preferred_output": "输出格式偏好",
    "preferred_currency": "计价货币偏好",
    "preferred_language": "回复语言偏好",
}


def _slug(text: str, limit: int = 48) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (text or ""))
    return safe[:limit].strip("_") or "item"


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _merge_value(old: Any, new: Any, cap: int) -> Any:
    """合并偏好字段：列表取并集（新的在前、去重、限长），标量直接覆盖。"""
    if isinstance(old, list) or isinstance(new, list):
        merged: List[Any] = []
        for value in list(new if isinstance(new, list) else [new]) + list(
            old if isinstance(old, list) else [old]
        ):
            if value not in merged:
                merged.append(value)
        return merged[:cap]
    return new


# 遗忘扫描的节流状态：**按 user_id 模块级共享**。
# 之所以不放实例上：中间件/工具每次请求都会 new 一个 MemoryKeeper，
# 实例级的 _last_sweep 每次都归零，节流完全失效，导致每轮对话结束
# 都触发一次全量扫描（3 个 namespace 各 list 一遍）。
_LAST_SWEEP: Dict[str, float] = {}


def reset_sweep_state() -> None:
    """清空节流状态（测试用）。"""
    _LAST_SWEEP.clear()


class MemoryKeeper:
    """一位用户的记忆管家。"""

    def __init__(self, store: Any, user_id: str, config: Optional[MemoryConfig] = None):
        self._store = store
        self.user_id = user_id or "default_user"
        self.config = config or DEFAULT_MEMORY_CONFIG

    # ============================================================
    # 基础读写
    # ============================================================

    @property
    def available(self) -> bool:
        return self._store is not None

    def namespace(self, memory_type: str) -> Tuple[str, str, str]:
        return ns.type_ns(self.user_id, memory_type)

    def put(self, item: MemoryItem) -> Optional[MemoryItem]:
        if not self.available or item is None:
            return None
        item.updated_at = now_iso()
        try:
            self._store.put(self.namespace(item.type), item.store_key, item.to_dict())
        except Exception:
            return None
        return item

    def get(self, memory_type: str, key: str) -> Optional[MemoryItem]:
        if not self.available:
            return None
        try:
            raw = self._store.get(self.namespace(memory_type), key)
        except Exception:
            return None
        value = getattr(raw, "value", None)
        return MemoryItem.from_dict(value) if isinstance(value, dict) else None

    def delete(self, item: MemoryItem) -> None:
        if not self.available or item is None:
            return
        try:
            self._store.delete(self.namespace(item.type), item.store_key)
        except Exception:
            pass

    def list_items(self, memory_type: str, limit: int = 500) -> List[MemoryItem]:
        """列出某个类型下的全部记忆（脏数据自动忽略）。"""
        if not self.available:
            return []
        try:
            raw = self._store.search(self.namespace(memory_type), limit=limit)
        except TypeError:
            try:
                raw = self._store.search(self.namespace(memory_type))
            except Exception:
                return []
        except Exception:
            return []

        items: List[MemoryItem] = []
        for entry in raw or []:
            value = getattr(entry, "value", None)
            item = MemoryItem.from_dict(value)
            if item is not None:
                items.append(item)
        return items

    def all_items(self, limit: int = 500) -> List[MemoryItem]:
        items: List[MemoryItem] = []
        for memory_type in (SEMANTIC, EPISODIC, PROCEDURAL):
            items.extend(self.list_items(memory_type, limit=limit))
        return items

    # ============================================================
    # 语义记忆：写入即冲突消解（双时态）
    # ============================================================

    def upsert_semantic(
        self,
        key: str,
        content: str,
        *,
        summary: str = "",
        confidence: float = 0.7,
        evidence: str = "",
        source: Optional[dict] = None,
    ) -> Optional[MemoryItem]:
        """写入一条语义记忆。

        同 key 的旧值不会被删除，而是被标记 ``invalidated_at`` 沉入 COLD，
        新值成为 WARM 里唯一有效版本——用户"改口"时历史仍可追溯。
        """
        if not self.available or not key:
            return None

        current = self.active_semantic(key)
        if current is not None and current.content == content:
            current.touch()
            return self.put(current)

        if current is not None:
            replacement = MemoryItem(
                id=new_id("sem"),
                type=SEMANTIC,
                content=content,
                summary=summary or f"{_PREFERENCE_LABELS.get(key, key)}：{content}",
                key=key,
                tags=["preference", key],
                source=source or {},
                confidence=confidence,
                evidence=evidence,
            )
            current.invalidate(by=replacement.id)
            self.put(current)
        else:
            replacement = MemoryItem(
                id=new_id("sem"),
                type=SEMANTIC,
                content=content,
                summary=summary or f"{_PREFERENCE_LABELS.get(key, key)}：{content}",
                key=key,
                tags=["preference", key],
                source=source or {},
                confidence=confidence,
                evidence=evidence,
            )
        return self.put(replacement)

    def active_semantic(self, key: str, now: Optional[datetime] = None) -> Optional[MemoryItem]:
        """取出某个 key 当前有效的那一条（同 key 有多条时取最新的）。"""
        candidates = [
            item for item in self.list_items(SEMANTIC)
            if item.key == key and item.is_valid(now)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda i: i.created_at, reverse=True)
        return candidates[0]

    def semantic_history(self, key: str) -> List[MemoryItem]:
        items = [i for i in self.list_items(SEMANTIC) if i.key == key]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items

    # ============================================================
    # 情节记忆：一次会话一条，按 thread_id 幂等
    # ============================================================

    def archive_episode(
        self,
        thread_id: str,
        *,
        goal: str = "",
        summary: str = "",
        task_type: str = "general",
        tags: Optional[Sequence[str]] = None,
        tools: Optional[Sequence[str]] = None,
        source: Optional[dict] = None,
    ) -> Optional[MemoryItem]:
        if not self.available or not thread_id:
            return None

        item_id = f"ep_{thread_id}"
        existing = self.get(EPISODIC, item_id)
        ttl = self.config.episode_ttl_days

        if existing is not None:
            existing.summary = summary or existing.summary
            existing.content = summary or existing.content
            existing.tags = sorted(set(list(existing.tags) + list(tags or [])))
            existing.source = source or existing.source
            existing.ttl_days = ttl
            return self.put(existing)

        item = MemoryItem(
            id=item_id,
            type=EPISODIC,
            content=summary or goal or "（无摘要）",
            summary=_clip(summary or goal, self.config.episode_summary_chars),
            key=thread_id,
            tags=sorted(set(["episode", task_type] + list(tags or []))),
            source=source or {},
            confidence=0.8,
            ttl_days=ttl,
        )
        return self.put(item)

    def recent_episodes(self, days: Optional[int] = None,
                        now: Optional[datetime] = None) -> List[MemoryItem]:
        limit_days = self.config.warm_episode_days if days is None else days
        now_dt = now or datetime.now()
        items = [
            i for i in self.list_items(EPISODIC)
            if i.is_valid(now_dt) and i.age_days(now_dt) <= limit_days
        ]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items

    # ============================================================
    # 程序记忆
    # ============================================================

    def upsert_procedural(
        self,
        key: str,
        content: str,
        *,
        summary: str = "",
        tags: Optional[Sequence[str]] = None,
        confidence: float = 0.6,
        source: Optional[dict] = None,
    ) -> Optional[MemoryItem]:
        if not self.available or not key:
            return None
        item_id = f"rule_{_slug(key)}"
        existing = self.get(PROCEDURAL, item_id)
        if existing is not None:
            if existing.content == content:
                existing.touch()
                return self.put(existing)
            existing.content = content
            existing.summary = summary or _clip(content, 120)
            existing.tags = sorted(set(list(existing.tags) + list(tags or [])))
            existing.confidence = confidence
            existing.source = source or existing.source
            return self.put(existing)

        item = MemoryItem(
            id=item_id,
            type=PROCEDURAL,
            content=content,
            summary=summary or _clip(content, 120),
            key=key,
            tags=sorted(set(["rule"] + list(tags or []))),
            source=source or {},
            confidence=confidence,
            ttl_days=self.config.procedural_ttl_days,
        )
        return self.put(item)

    # ============================================================
    # WARM 投影（给 system_prompt 用）
    # ============================================================

    def load_preferences(self) -> Dict[str, Any]:
        """读取 WARM 投影里的偏好字典（旧命名空间，语义已改为投影/缓存）。"""
        if not self.available:
            return {}
        try:
            raw = self._store.get(
                ns.legacy_preferences_ns(self.user_id), ns.LEGACY_PREFERENCES_KEY
            )
        except Exception:
            return {}
        value = getattr(raw, "value", None)
        return dict(value) if isinstance(value, dict) else {}

    def save_preferences(self, preferences: Dict[str, Any]) -> None:
        if not self.available:
            return
        try:
            self._store.put(
                ns.legacy_preferences_ns(self.user_id),
                ns.LEGACY_PREFERENCES_KEY,
                dict(preferences or {}),
            )
        except Exception:
            pass

    def merge_preferences(
        self,
        updates: Dict[str, Any],
        *,
        source: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """合并偏好增量。

        这里是 P0 里"整键覆盖导致旧字段丢失"那个 Bug 的修复点：
        先读、再合并、最后整体写回，而不是拿增量直接覆盖。
        """
        if not self.available or not updates:
            return self.load_preferences()

        merged = self.load_preferences()
        for field, value in updates.items():
            cap = (
                self.config.max_recent_suppliers
                if field == "recent_suppliers"
                else self.config.max_recent_queries
                if field == "recent_queries"
                else 20
            )
            merged[field] = _merge_value(merged.get(field), value, cap)
        self.save_preferences(merged)

        for field in STABLE_PREFERENCE_KEYS:
            if field in updates:
                self.upsert_semantic(
                    field,
                    str(merged[field]),
                    confidence=0.8,
                    evidence=str(updates[field]),
                    source=source or {},
                )
        return merged

    def build_warm_brief(self, now: Optional[datetime] = None) -> str:
        """生成注入 system_prompt 的 WARM 摘要（体积小、只放高信号内容）。"""
        if not self.available:
            return ""
        lines: List[str] = []

        semantic = [i for i in self.list_items(SEMANTIC) if i.is_valid(now)]
        seen = set()
        semantic.sort(key=lambda i: i.created_at, reverse=True)
        for item in semantic:
            if item.key in seen:
                continue
            seen.add(item.key)
            label = _PREFERENCE_LABELS.get(item.key, item.key)
            lines.append(f"- {label}：{item.content}")

        for episode in self.recent_episodes(now=now)[: self.config.max_warm_episodes]:
            lines.append(
                f"- 近期任务：{_clip(episode.summary, self.config.warm_brief_chars)}"
            )

        return "\n".join(lines)

    # ============================================================
    # COLD 检索
    # ============================================================

    def search(
        self,
        query: str,
        *,
        k: Optional[int] = None,
        types: Optional[Iterable[str]] = None,
        now: Optional[datetime] = None,
        touch: bool = True,
    ) -> List[Tuple[MemoryItem, float]]:
        """COLD 层按需检索：BM25 + 时间衰减 + 频次 + 置信度。"""
        if not self.available or not query:
            return []
        wanted = set(types) if types else {SEMANTIC, EPISODIC, PROCEDURAL}
        now_dt = now or datetime.now()

        candidates = [
            item for item in self.all_items()
            if item.type in wanted and item.is_valid(now_dt)
        ]
        if not candidates:
            return []

        ranked = scoring.rank(query, candidates, self.config, now_dt)
        floor = self.config.min_retrieval_score
        hits = [(item, score) for item, score in ranked if score >= floor]
        hits = hits[: (k or self.config.retrieval_k)]

        if touch:
            for item, _ in hits:
                item.touch()
                self.put(item)
        return hits

    def search_text(
        self,
        query: str,
        *,
        k: Optional[int] = None,
        types: Optional[Iterable[str]] = None,
    ) -> str:
        hits = self.search(query, k=k, types=types)
        if not hits:
            return "没有找到相关的历史记忆。"
        lines = []
        for index, (item, score) in enumerate(hits, 1):
            stamp = item.created_at[:10] or "未知时间"
            lines.append(
                f"{index}. [{item.type}/{stamp}/相关度 {score:.2f}] {_clip(item.summary or item.content, 200)}"
            )
        return "检索到的历史记忆：\n" + "\n".join(lines)

    # ============================================================
    # 整合与遗忘
    # ============================================================

    def forget(self, *, force: bool = False, now: Optional[datetime] = None) -> Dict[str, int]:
        """遗忘扫描：清理过期、超限、超量的记忆。

        默认按 ``sweep_interval_seconds`` 节流，不会每轮对话都全量扫。
        """
        stats = {"expired": 0, "history": 0, "overflow": 0}
        if not self.available:
            return stats

        if not force:
            if time.time() - _LAST_SWEEP.get(self.user_id, 0.0) < self.config.sweep_interval_seconds:
                return stats
        _LAST_SWEEP[self.user_id] = time.time()

        now_dt = now or datetime.now()

        for memory_type in (SEMANTIC, EPISODIC, PROCEDURAL):
            for item in self.list_items(memory_type):
                if item.is_expired(now_dt):
                    self.delete(item)
                    stats["expired"] += 1

        # 语义记忆：同一个 key 只保留最近 N 条历史版本。
        # 关键约束：**当前生效的那条永远不删**，只淘汰已失效的历史版本；
        # 否则时间戳并列时可能把新值当成"最旧"误删，用户偏好直接丢失。
        by_key: Dict[str, List[MemoryItem]] = {}
        for item in self.list_items(SEMANTIC):
            if item.key:
                by_key.setdefault(item.key, []).append(item)
        for key, items in by_key.items():
            if len(items) <= self.config.max_semantic_history:
                continue
            active = [i for i in items if not i.is_invalidated()]
            stale_items = [i for i in items if i.is_invalidated()]
            stale_items.sort(key=lambda i: (i.created_at, i.updated_at), reverse=True)
            keep = max(0, self.config.max_semantic_history - len(active))
            for stale in stale_items[keep:]:
                self.delete(stale)
                stats["history"] += 1

        # 情节记忆：超过上限时淘汰最旧的
        episodes = self.list_items(EPISODIC)
        if len(episodes) > self.config.max_episodes:
            episodes.sort(key=lambda i: (i.created_at, i.updated_at), reverse=True)
            for stale in episodes[self.config.max_episodes:]:
                self.delete(stale)
                stats["overflow"] += 1

        return stats

    def consolidate(
        self,
        messages: Sequence[Any],
        *,
        thread_id: str = "",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """一轮会话结束后的整合：抽取偏好 → 归档情节 → 遗忘扫描。"""
        from .extractor import extract_preferences, summarize_episode

        result: Dict[str, Any] = {"preferences": {}, "episode": None, "forgotten": {}}
        if not self.available or not messages:
            return result

        updates = extract_preferences(messages, self.config)
        if updates:
            result["preferences"] = self.merge_preferences(
                updates, source={"thread_id": thread_id}
            )

        digest = summarize_episode(messages, self.config, thread_id=thread_id)
        if digest:
            item = self.archive_episode(
                thread_id or digest.get("thread_id") or new_id("ep"),
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
            result["episode"] = item.to_dict() if item else None

        result["forgotten"] = self.forget(now=now)
        return result

    # ============================================================
    # 迁移
    # ============================================================

    def migrate_legacy_preferences(self) -> int:
        """把旧命名空间里的偏好补建为语义记忆（幂等，重复调用无副作用）。

        只有当某个**稳定偏好 key** 已经存在时才跳过该 key 的迁移；
        不能因为 semantic 下有别的条目（比如 remember 写的自定义记忆）
        就整体放弃——那会让老用户的存量偏好永远迁不过来。
        """
        if not self.available:
            return 0
        existing_keys = {item.key for item in self.list_items(SEMANTIC)}
        legacy = self.load_preferences()
        created = 0
        for field in STABLE_PREFERENCE_KEYS:
            if field in existing_keys:
                continue
            if field in legacy and legacy[field]:
                if self.upsert_semantic(
                    field, str(legacy[field]), confidence=0.6,
                    evidence="迁移自历史偏好", source={"migrated": True},
                ):
                    created += 1
        return created

    # ============================================================
    # 调试视图
    # ============================================================

    def stats(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now_dt = now or datetime.now()
        counts = {WARM: 0, COLD: 0}
        per_type: Dict[str, int] = {}
        for item in self.all_items():
            counts[item.tier(self.config.warm_episode_days, now_dt)] += 1
            per_type[item.type] = per_type.get(item.type, 0) + 1
        return {
            "user_id": self.user_id,
            "total": sum(per_type.values()),
            "per_type": per_type,
            "warm": counts[WARM],
            "cold": counts[COLD],
        }

    def to_json(self) -> str:
        return json.dumps(
            [item.to_dict() for item in self.all_items()], ensure_ascii=False, indent=2
        )

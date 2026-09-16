"""Store 命名空间规范。

统一之后的结构：

    ("memories", <user_id>, "semantic")    用户级稳定事实（偏好、画像）
    ("memories", <user_id>, "episodic")    用户级情节归档（做过什么）
    ("memories", <user_id>, "procedural")  用户级经验/流程（怎么做事）
    ("memories", "org", "policies")        组织级只读策略

``("user-preferences", <user_id>)`` 这个旧命名空间**保留**，但语义变了：
它不再是"长期记忆本体"，而是 WARM 层的一份**投影/缓存**（只放偏好字典），
供 ``precompute_agent_context`` 直接塞进 system_prompt。真正的记忆本体在
``memories`` 下，投影由 ``MemoryKeeper`` 单向同步，不反向写回。

这样既做了命名空间规范化，又不会破坏已有的读取链路。
"""
from __future__ import annotations

from typing import Optional, Tuple

ROOT = "memories"

SEMANTIC = "semantic"
EPISODIC = "episodic"
PROCEDURAL = "procedural"
POLICIES = "policies"

ORG_ID = "org"
LEGACY_PREFERENCES_ROOT = "user-preferences"
LEGACY_PREFERENCES_KEY = "preferences"


def semantic_ns(user_id: str) -> Tuple[str, str, str]:
    return (ROOT, user_id or "default_user", SEMANTIC)


def episodic_ns(user_id: str) -> Tuple[str, str, str]:
    return (ROOT, user_id or "default_user", EPISODIC)


def procedural_ns(user_id: str) -> Tuple[str, str, str]:
    return (ROOT, user_id or "default_user", PROCEDURAL)


def org_policies_ns() -> Tuple[str, str, str]:
    return (ROOT, ORG_ID, POLICIES)


def legacy_preferences_ns(user_id: str) -> Tuple[str, str]:
    return (LEGACY_PREFERENCES_ROOT, user_id or "default_user")


def type_ns(user_id: str, memory_type: str) -> Tuple[str, str, str]:
    if memory_type == EPISODIC:
        return episodic_ns(user_id)
    if memory_type == PROCEDURAL:
        return procedural_ns(user_id)
    return semantic_ns(user_id)


def user_namespaces(user_id: str) -> Tuple[Tuple[str, ...], ...]:
    return (semantic_ns(user_id), episodic_ns(user_id), procedural_ns(user_id))


def parse(namespace) -> dict:
    """把命名空间解析成结构化信息，解析不出来时给出安全的默认值。"""
    parts = [str(p) for p in (namespace or ())]
    info = {
        "root": parts[0] if parts else "",
        "scope": parts[1] if len(parts) > 1 else "",
        "type": parts[2] if len(parts) > 2 else "",
    }
    info["is_memory"] = info["root"] == ROOT
    info["is_org"] = info["is_memory"] and info["scope"] == ORG_ID
    return info


def user_of(namespace) -> Optional[str]:
    info = parse(namespace)
    if info["is_memory"] and not info["is_org"]:
        return info["scope"] or None
    return None

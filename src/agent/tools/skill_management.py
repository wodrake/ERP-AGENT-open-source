"""用户自定义 Skill 的发现工具。"""
from __future__ import annotations

from langchain_core.tools import tool

from ..config import skills_store_namespace
from ..runtime_context import get_current_store, get_current_user_id


@tool
def list_user_skills() -> str:
    """列出当前用户已安装、会在沙箱重建后自动恢复的自定义 Skills。

    当用户要求使用、查看或确认已安装的自定义 Skill 时，先调用此工具；返回的
    Skill 位于 ``/skills/custom/<skill_name>/``，可再读取其中的 ``SKILL.md``。
    """
    store = get_current_store()
    if store is None:
        return "当前没有可用的持久化 Store，无法列出用户自定义 Skills。"

    user_id = get_current_user_id()
    try:
        items = store.search(skills_store_namespace(user_id))
    except Exception as exc:
        return f"读取用户自定义 Skills 失败: {exc}"

    skills: dict[str, int] = {}
    for item in items or []:
        key = getattr(item, "key", "")
        if not isinstance(key, str) or not key:
            continue
        skill_name = key.replace("\\", "/").split("/", 1)[0]
        if skill_name:
            skills[skill_name] = skills.get(skill_name, 0) + 1

    if not skills:
        return "当前用户还没有持久化的自定义 Skills。"

    lines = [
        f"- {name}: {count} 个文件，路径 /skills/custom/{name}/"
        for name, count in sorted(skills.items())
    ]
    return "当前用户可用的自定义 Skills：\n" + "\n".join(lines)

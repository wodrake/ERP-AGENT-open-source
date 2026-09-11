"""从 Store 恢复用户安装的 Skills 到当前沙箱。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import PurePosixPath
from typing import Any

from langchain.agents.middleware import AgentMiddleware, Runtime

from ..config import skills_store_namespace
from ..log_utils import middleware_logger


class UserSkillsRestoreMiddleware(AgentMiddleware):
    """以“每文件一条 Store 记录”的形式恢复用户自定义 Skills。

    Store key 是受校验的相对 POSIX 路径，例如 ``csv-tools/parser.py``；恢复路径
    永远由 key 推导为 ``/skills/custom/csv-tools/parser.py``，不会信任 Store 内部
    任意提供的绝对路径。
    """

    def __init__(self, store=None, user_id: str = "default_user", sandbox_backend=None):
        self._store = store
        self._user_id = user_id
        self._sandbox_backend = sandbox_backend
        self._restored_hashes: dict[str, str] = {}
        self._restored_paths: set[str] = set()
        self._restored_skills: set[str] = set()
        self._last_sandbox_generation: int | None = None
        self.tools = []

    @property
    def name(self) -> str:
        return "UserSkillsRestoreMiddleware"

    def before_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        self.restore_now()
        return None

    async def abefore_agent(
        self, state: Any, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Store 查询、文件写入、依赖安装全部放在线程池中。"""
        await asyncio.to_thread(self.restore_now)
        return None

    def restore_now(self) -> None:
        """立即执行增量恢复，供健康检查重建回调调用。"""
        if self._store is None or self._sandbox_backend is None:
            return

        generation = getattr(self._sandbox_backend, "generation", None)
        if generation is not None and generation != self._last_sandbox_generation:
            self.invalidate()

        self._restore_skills()
        self._last_sandbox_generation = generation

    @staticmethod
    def _remote_path_for_key(skill_key: str) -> str:
        """将 Store key 安全映射为沙箱中的自定义 Skill 路径。"""
        normalized = skill_key.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise ValueError(f"unsafe persisted Skill key: {skill_key!r}")
        return f"/skills/custom/{path.as_posix()}"

    @staticmethod
    def _decode_content(value: dict[str, Any]) -> bytes:
        """兼容新 base64 schema 与早期纯文本 schema。"""
        content = value.get("content")
        if content is None:
            raise ValueError("missing Skill content")

        encoding = value.get("encoding", "utf-8")
        if encoding == "base64":
            if not isinstance(content, str):
                raise ValueError("base64 Skill content must be a string")
            return base64.b64decode(content.encode("ascii"), validate=True)
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        raise ValueError(f"unsupported Skill content type: {type(content).__name__}")

    def _restore_skills(self) -> None:
        try:
            try:
                items = self._store.search(skills_store_namespace(self._user_id))
            except Exception as exc:
                middleware_logger.error(f"Failed to search persisted Skills: {exc}")
                return

            current_paths: set[str] = set()
            current_skills: set[str] = set()
            restored_count = 0
            skipped_count = 0
            requirements_to_install: set[str] = set()

            for item in items or []:
                skill_key = getattr(item, "key", None)
                skill_value = getattr(item, "value", None)
                if not isinstance(skill_key, str) or not isinstance(skill_value, dict):
                    continue

                try:
                    remote_path = self._remote_path_for_key(skill_key)
                    content = self._decode_content(skill_value)
                except Exception as exc:
                    middleware_logger.warning(
                        f"Ignoring invalid persisted Skill {skill_key!r}: {exc}"
                    )
                    continue

                current_paths.add(remote_path)
                current_skills.add(skill_key.split("/", 1)[0])
                content_hash = hashlib.sha256(content).hexdigest()
                if self._restored_hashes.get(remote_path) == content_hash:
                    skipped_count += 1
                    continue

                try:
                    directory = str(PurePosixPath(remote_path).parent)
                    setup = self._sandbox_backend.execute(
                        f"mkdir -p '{directory}'", timeout=10
                    )
                    if setup.exit_code != 0:
                        raise RuntimeError(setup.output)

                    if self._sandbox_backend.file_exists(remote_path):
                        try:
                            self._sandbox_backend.cp(remote_path, f"{remote_path}.bak")
                        except Exception:
                            # 备份失败不应阻断更新；新容器中通常本来没有旧文件。
                            pass

                    self._sandbox_backend.write_file(remote_path, content)
                    self._restored_hashes[remote_path] = content_hash
                    restored_count += 1
                    if PurePosixPath(remote_path).name == "requirements.txt":
                        requirements_to_install.add(remote_path)
                except Exception as exc:
                    middleware_logger.error(
                        f"Failed to restore persisted Skill {skill_key!r}: {exc}"
                    )

            # Store 中已删除的 Skill 文件也要从沙箱清掉，避免幽灵版本继续被使用。
            deleted_count = 0
            for remote_path in self._restored_paths - current_paths:
                try:
                    self._sandbox_backend.rm(remote_path)
                    self._restored_hashes.pop(remote_path, None)
                    deleted_count += 1
                except Exception as exc:
                    middleware_logger.warning(
                        f"Failed to remove deleted persisted Skill {remote_path}: {exc}"
                    )

            self._restored_paths = current_paths
            self._restored_skills = current_skills
            self._restore_requirements(requirements_to_install)

            if restored_count or deleted_count:
                middleware_logger.info(
                    f"Persisted Skills restored for {self._user_id}: "
                    f"{restored_count} written, {deleted_count} removed, "
                    f"{skipped_count} unchanged"
                )
            elif skipped_count:
                middleware_logger.debug(
                    f"Persisted Skills already current for {self._user_id} "
                    f"({skipped_count} files)"
                )
        except Exception as exc:
            middleware_logger.error(
                f"Skills restore failed for {self._user_id}: {exc}", exc_info=True
            )

    def _restore_requirements(self, requirement_paths: set[str]) -> None:
        """新沙箱重建后重新安装用户 Skill 声明的依赖。"""
        for requirement_path in sorted(requirement_paths):
            try:
                result = self._sandbox_backend.execute(
                    "mkdir -p /workspace/python-packages && "
                    "python3 -m pip install --no-cache-dir "
                    "--target /workspace/python-packages "
                    f"-r '{requirement_path}' -q",
                    timeout=120,
                )
                if result.exit_code == 0:
                    middleware_logger.info(
                        f"Restored dependencies for persisted Skill: {requirement_path}"
                    )
                else:
                    middleware_logger.warning(
                        "Persisted Skill dependency restore failed for "
                        f"{requirement_path}: {result.output[:300]}"
                    )
            except Exception as exc:
                middleware_logger.warning(
                    f"Persisted Skill dependency restore error for {requirement_path}: {exc}"
                )

    def invalidate(self) -> None:
        """强制下次恢复完整写入，用于沙箱热替换后。"""
        self._restored_hashes.clear()
        self._restored_paths.clear()
        self._restored_skills.clear()
        self._last_sandbox_generation = None
        middleware_logger.info("UserSkillsRestore cache invalidated")

    def get_restored_skills(self) -> set[str]:
        """返回当前恢复到沙箱的 Skill 名称集合。"""
        return set(self._restored_skills)

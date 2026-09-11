"""不依赖 Docker 的沙箱生命周期回归测试。"""
from __future__ import annotations

import base64
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from deepagents.backends import CompositeBackend
from deepagents.backends.sandbox import (
    BaseSandbox,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from langchain_core.messages import HumanMessage

from src.agent.backends.sandbox_holder import (
    bind_sandbox,
    get_sandbox,
    reset_sandbox,
    set_sandbox,
)
from src.agent.backends.sandbox_proxy import SandboxBackendProxy
from src.agent.config import skills_store_namespace
from src.agent.middlewares.sandbox_health import SandboxHealthMiddleware
from src.agent.middlewares.skills_sync import SkillsSyncMiddleware
from src.agent.middlewares.user_skills_restore import UserSkillsRestoreMiddleware
from src.api_view.mongodb_store import MongoDBStore
from src.agent.harness import (
    HarnessPhaseMiddleware,
    load_harness_config,
    should_use_grader,
)


class FakeSandbox(BaseSandbox):
    """实现 BaseSandbox 和项目扩展 helper 的轻量内存后端。"""

    def __init__(self, sandbox_id: str, healthy: bool = True):
        self._id = sandbox_id
        self.healthy = healthy
        self.files: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.destroyed = False
        self.container_name = f"fake-{sandbox_id}"
        self.container_id = sandbox_id

    @property
    def id(self) -> str:
        return self._id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self.commands.append(command)
        if not self.healthy:
            return ExecuteResponse(output="unhealthy", exit_code=1)
        return ExecuteResponse(output=f"from-{self._id}", exit_code=0)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results = []
        for path, content in files:
            self.files[path] = content
            results.append(FileUploadResponse(path=path, error=None))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [
            FileDownloadResponse(
                path=path,
                content=self.files.get(path),
                error=None if path in self.files else "file_not_found",
            )
            for path in paths
        ]

    def ping(self) -> bool:
        return self.healthy

    def destroy(self) -> None:
        self.destroyed = True

    def write_file(self, path: str, content: str | bytes) -> str:
        self.files[path] = content.encode() if isinstance(content, str) else content
        return f"OK: {path}"

    def read_file_bytes(self, path: str) -> bytes:
        return self.files[path]

    def file_exists(self, path: str) -> bool:
        return path in self.files

    def cp(self, source: str, destination: str) -> str:
        self.files[destination] = self.files[source]
        return "OK"

    def rm(self, path: str) -> str:
        self.files.pop(path, None)
        return "OK"


class FakeManager:
    def __init__(self, replacement: FakeSandbox):
        self.replacement = replacement
        self.rebuild_users: list[str] = []
        self.touched: list[str] = []

    def rebuild(self, user_id: str) -> FakeSandbox:
        self.rebuild_users.append(user_id)
        return self.replacement

    def touch(self, user_id: str) -> None:
        self.touched.append(user_id)


@dataclass
class FakeItem:
    key: str
    value: dict


class FakeStore:
    def __init__(self, items: list[FakeItem] | None = None):
        self.items = items or []
        self.namespaces: list[tuple[str, ...]] = []

    def search(self, namespace: tuple[str, ...]):
        self.namespaces.append(namespace)
        return list(self.items)


class SandboxProxyTests(unittest.TestCase):
    def test_proxy_is_protocol_compatible_and_hot_swaps_composite_execution(self):
        first = FakeSandbox("first")
        proxy = SandboxBackendProxy(first)
        backend = CompositeBackend(default=proxy, routes={})

        self.assertIsInstance(proxy, BaseSandbox)
        self.assertEqual(backend.execute("echo one").output, "from-first")

        second = FakeSandbox("second")
        proxy.replace_backend(second)

        self.assertEqual(proxy.generation, 1)
        self.assertEqual(proxy.id, "second")
        self.assertEqual(backend.execute("echo two").output, "from-second")
        self.assertTrue(first.destroyed)

    def test_proxy_base_sandbox_file_api_delegates_to_current_backend(self):
        first = FakeSandbox("first")
        proxy = SandboxBackendProxy(first)

        result = proxy.write("/workspace/a.txt", "hello")

        self.assertIsNotNone(result)
        self.assertEqual(first.files["/workspace/a.txt"], b"hello")


class HealthRecoveryTests(unittest.TestCase):
    def test_unhealthy_proxy_is_rebuilt_hot_swapped_and_rehydrated(self):
        failed = FakeSandbox("failed", healthy=False)
        replacement = FakeSandbox("replacement", healthy=True)
        proxy = SandboxBackendProxy(failed)
        manager = FakeManager(replacement)
        callback_calls: list[str] = []

        middleware = SandboxHealthMiddleware(
            sandbox_manager=manager,
            user_id="alice",
            sandbox_backend=proxy,
            on_rebuild=lambda new_backend: callback_calls.append(new_backend.id),
            check_interval=0,
        )
        middleware.before_agent({}, None)

        self.assertEqual(manager.rebuild_users, ["alice"])
        self.assertEqual(proxy.id, "replacement")
        self.assertEqual(callback_calls, ["replacement"])
        self.assertTrue(failed.destroyed)
        self.assertIn("alice", manager.touched)


class SkillsLifecycleTests(unittest.TestCase):
    def test_core_skills_resync_after_proxy_generation_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_dir = Path(temp_dir) / "skills"
            skill_file = skills_dir / "demo" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("name: demo\ndescription: test\n", encoding="utf-8")

            first = FakeSandbox("first")
            proxy = SandboxBackendProxy(first)
            middleware = SkillsSyncMiddleware(skills_dir=skills_dir, sandbox_backend=proxy)
            middleware.sync_now()
            self.assertIn("/skills/demo/SKILL.md", first.files)

            replacement = FakeSandbox("replacement")
            proxy.replace_backend(replacement)
            middleware.sync_now()
            self.assertIn("/skills/demo/SKILL.md", replacement.files)

    def test_user_skill_restore_isolated_safe_and_replayed_after_hot_swap(self):
        content = base64.b64encode(b"print('hello')").decode("ascii")
        store = FakeStore(
            [
                FakeItem(
                    key="demo/run.py",
                    value={"content": content, "encoding": "base64"},
                ),
                FakeItem(
                    key="../escape.py",
                    value={"content": content, "encoding": "base64"},
                ),
            ]
        )
        first = FakeSandbox("first")
        proxy = SandboxBackendProxy(first)
        middleware = UserSkillsRestoreMiddleware(
            store=store, user_id="alice", sandbox_backend=proxy
        )

        middleware.restore_now()
        self.assertEqual(
            first.files["/skills/custom/demo/run.py"], b"print('hello')"
        )
        self.assertNotIn("/skills/custom/../escape.py", first.files)
        self.assertEqual(store.namespaces, [skills_store_namespace("alice")])

        replacement = FakeSandbox("replacement")
        proxy.replace_backend(replacement)
        middleware.restore_now()
        self.assertEqual(
            replacement.files["/skills/custom/demo/run.py"], b"print('hello')"
        )

        store.items = []
        middleware.restore_now()
        self.assertNotIn("/skills/custom/demo/run.py", replacement.files)


class IsolationTests(unittest.TestCase):
    def test_context_bound_holder_does_not_cross_users(self):
        alice = FakeSandbox("alice")
        bob = FakeSandbox("bob")
        set_sandbox(alice, "alice")
        set_sandbox(bob, "bob")

        alice_token = bind_sandbox("alice")
        try:
            self.assertIs(get_sandbox(), alice)
        finally:
            reset_sandbox(alice_token)

        bob_token = bind_sandbox("bob")
        try:
            self.assertIs(get_sandbox(), bob)
        finally:
            reset_sandbox(bob_token)


class MongoNamespaceTests(unittest.TestCase):
    def test_namespace_prefix_query_matches_array_segments(self):
        self.assertEqual(
            MongoDBStore._namespace_prefix_query(("persisted-skills", "alice")),
            {"namespace.0": "persisted-skills", "namespace.1": "alice"},
        )


class HarnessReviewSelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = load_harness_config()
        self.middleware = HarnessPhaseMiddleware()

    def test_trivial_messages_skip_grader(self):
        for text in ("你好", "谢谢", "ERP是什么？", "库存是什么？"):
            with self.subTest(text=text):
                self.assertFalse(
                    should_use_grader([HumanMessage(content=text)], self.config)
                )
                update = self.middleware.before_agent(
                    {"messages": [HumanMessage(content=text)]}, None
                )
                self.assertNotIn("rubric", update)

    def test_business_task_injects_matching_rubric(self):
        text = "查询库存并给出低于安全库存的补货建议"
        self.assertTrue(
            should_use_grader([HumanMessage(content=text)], self.config)
        )
        update = self.middleware.before_agent(
            {"messages": [HumanMessage(content=text)]}, None
        )
        self.assertEqual(update["_harness_rubric_source"], "auto")
        self.assertIn("库存", update["rubric"])

    def test_simple_follow_up_clears_checkpointed_auto_rubric(self):
        update = self.middleware.before_agent(
            {
                "messages": [HumanMessage(content="你好")],
                "rubric": "previous rubric",
                "_harness_rubric_source": "auto",
                "_rubric_status": "satisfied",
            },
            None,
        )
        self.assertEqual(update["rubric"], "")
        self.assertEqual(update["_harness_rubric_source"], "")

    def test_explicit_rubric_is_not_overridden(self):
        explicit = "必须由调用方提供的评审标准"
        update = self.middleware.before_agent(
            {"messages": [HumanMessage(content="你好")], "rubric": explicit},
            None,
        )
        self.assertNotIn("rubric", update)


if __name__ == "__main__":
    unittest.main()

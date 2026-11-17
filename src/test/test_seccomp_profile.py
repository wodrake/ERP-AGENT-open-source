"""内置 seccomp profile 不应再包含已知会导致 Docker 拒绝加载的冲突。"""
import json
import unittest

from src.agent.backends.sandbox_setup import seccomp_security_opt
from src.agent.backends.sandbox_status import profile_problems, seccomp_path


class SeccompProfileTests(unittest.TestCase):
    def test_profile_is_loadable_json_without_known_conflicts(self):
        data = json.loads(seccomp_path().read_text(encoding="utf-8"))
        self.assertEqual(profile_problems(data), [])
        blanket = set(data["syscalls"][0]["names"])
        self.assertNotIn("clone", blanket)
        self.assertNotIn("clone3", blanket)
        self.assertIn("arch_prctl", blanket)
        self.assertEqual(data["defaultErrnoRet"], 1)

    def test_security_opt_carries_json_not_a_filesystem_path(self):
        option = seccomp_security_opt(seccomp_path().read_text(encoding="utf-8"))
        self.assertTrue(option.startswith("seccomp={"))
        self.assertNotIn("seccomp=/", option)
        payload = json.loads(option.removeprefix("seccomp="))
        self.assertEqual(payload["defaultErrnoRet"], 1)

    def test_clone3_returns_enosys(self):
        data = json.loads(seccomp_path().read_text(encoding="utf-8"))
        rules = [rule for rule in data["syscalls"] if "clone3" in (rule.get("names") or [])]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["action"], "SCMP_ACT_ERRNO")
        self.assertEqual(rules[0]["errnoRet"], 38)
        self.assertNotIn("clone3", data["syscalls"][0]["names"])
        self.assertFalse(any(
            rule.get("action") == "SCMP_ACT_ALLOW" and "clone3" in (rule.get("names") or [])
            for rule in data["syscalls"]
        ))


if __name__ == "__main__":
    unittest.main()

"""沙箱运行状态，供 /health 显式暴露降级，而不是只写在日志里。"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

_LOCK = threading.Lock()
_BUILTIN_SECCOMP = Path(__file__).with_name("seccomp.json")

# 这些名字在较旧的 libseccomp 上会让整份 profile 加载失败，Docker 随之拒绝创建容器。
_RISKY_SYSCALLS = frozenset({
    "fchmodat2",
    "futex_waitv",
    "memfd_secret",
    "process_mrelease",
    "epoll_pwait2",
    "faccessat2",
})


@dataclass
class _Note:
    mode: str = ""
    detail: str = ""


_note = _Note()


def reset_for_tests() -> None:
    with _LOCK:
        _note.mode = ""
        _note.detail = ""


def note_secure(detail: str = "Docker 沙箱已启用 seccomp") -> None:
    _set("docker", detail)


def note_basic_fallback(detail: str) -> None:
    _set("docker_basic", detail)


def note_local_fallback(detail: str) -> None:
    _set("local_fallback", detail)


def note_unavailable(detail: str) -> None:
    _set("unavailable", detail)


def note_state(detail: str = "使用 StateBackend，不在宿主机执行命令") -> None:
    _set("state", detail)


def _set(mode: str, detail: str) -> None:
    with _LOCK:
        _note.mode = mode
        _note.detail = detail[:500]


def _flag(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def seccomp_path() -> Path:
    override = (os.getenv("SANDBOX_SECCOMP_PATH") or "").strip()
    if override:
        return Path(override)
    return _BUILTIN_SECCOMP


def profile_problems(data: dict | None = None) -> list[str]:
    """返回会使 Docker 拒绝 profile，或让生效规则和注释互相矛盾的问题。"""
    if data is None:
        path = seccomp_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"seccomp profile 无法解析: {exc}"]
    problems: list[str] = []
    if data.get("defaultAction") != "SCMP_ACT_ERRNO":
        problems.append("defaultAction 不是 SCMP_ACT_ERRNO")
    if "defaultErrnoRet" not in data:
        problems.append("缺少 defaultErrnoRet，部分 Docker 版本会拒绝该 profile")
    blanket: set[str] = set()
    actions: dict[str, set[str]] = {}
    present: set[str] = set()
    for index, rule in enumerate(data.get("syscalls") or []):
        names = list(rule.get("names") or [])
        present.update(names)
        action = rule.get("action")
        for name in names:
            actions.setdefault(name, set()).add(action)
        if index == 0 and action == "SCMP_ACT_ALLOW" and not rule.get("args"):
            blanket.update(names)
    for name in ("clone", "clone3"):
        if name in blanket:
            problems.append(f"{name} 出现在无条件放行列表中，和后面的专用规则冲突")
    for name, acts in actions.items():
        if "SCMP_ACT_ALLOW" in acts and "SCMP_ACT_ERRNO" in acts:
            problems.append(f"{name} 同时被允许和拒绝，Docker 可能拒绝加载 profile")
    for name in sorted(_RISKY_SYSCALLS & present):
        problems.append(f"syscall {name} 在较旧的 libseccomp 上会导致 profile 加载失败")
    if "arch_prctl" not in present:
        problems.append("缺少 arch_prctl，x86_64 上的 glibc/Python 无法在沙箱内运行")
    return problems


def docker_daemon_reachable() -> tuple[bool, str]:
    try:
        import docker

        client = docker.from_env(timeout=2)
        try:
            client.ping()
        finally:
            client.close()
        return True, "Docker daemon 可连接"
    except Exception as exc:
        return False, f"Docker 不可用: {exc.__class__.__name__}"


def snapshot() -> dict:
    """当前沙箱组件状态。mode=state 是显式配置，不是故障。"""
    mode = (os.getenv("SANDBOX_BACKEND") or "docker").strip().lower() or "docker"
    allow_local = _flag("SANDBOX_ALLOW_LOCAL_FALLBACK", False)
    with _LOCK:
        noted_mode = _note.mode
        noted_detail = _note.detail

    if mode == "state":
        return {
            "status": "ok",
            "mode": "state",
            "seccomp": "not_applicable",
            "local_fallback": False,
            "detail": noted_detail or "使用 StateBackend，不在宿主机执行命令",
        }
    if noted_mode == "docker_basic":
        return {
            "status": "degraded",
            "mode": "docker_basic",
            "seccomp": "degraded",
            "local_fallback": False,
            "detail": noted_detail or "seccomp 未生效，已降级为基础沙箱",
        }
    if noted_mode == "local_fallback":
        return {
            "status": "degraded",
            "mode": "local_fallback",
            "seccomp": "not_applicable",
            "local_fallback": True,
            "detail": noted_detail or "已回退到宿主机 LocalShellBackend",
        }
    if noted_mode == "unavailable":
        return {
            "status": "error",
            "mode": "unavailable",
            "seccomp": "not_applicable",
            "local_fallback": False,
            "detail": noted_detail or "Docker 不可用，已禁止宿主机执行",
        }
    if noted_mode == "docker":
        return {
            "status": "ok",
            "mode": "docker",
            "seccomp": "enforced",
            "local_fallback": False,
            "detail": noted_detail or "Docker 沙箱已启用 seccomp",
        }

    reachable, reach_detail = docker_daemon_reachable()
    problems = profile_problems()
    if reachable and not problems:
        return {
            "status": "ok",
            "mode": "docker",
            "seccomp": "enforced",
            "local_fallback": False,
            "detail": "Docker 可用，seccomp 配置可解析",
        }
    if reachable and problems:
        return {
            "status": "degraded",
            "mode": "docker",
            "seccomp": "degraded",
            "local_fallback": False,
            "detail": "seccomp 配置有问题，创建容器时可能降级: " + "; ".join(problems),
        }
    if allow_local:
        return {
            "status": "degraded",
            "mode": "local_fallback",
            "seccomp": "not_applicable",
            "local_fallback": True,
            "detail": reach_detail + "；SANDBOX_ALLOW_LOCAL_FALLBACK=true，后续可能在宿主机执行",
        }
    return {
        "status": "error",
        "mode": "unavailable",
        "seccomp": "not_applicable",
        "local_fallback": False,
        "detail": reach_detail + "；已禁止回退到宿主机执行",
    }

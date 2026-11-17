"""
主入口：create_main_agent() + precompute_agent_context()
DeepAgent 核心组装 — 严格遵循 Harness Engineering 架构
"""
import io
import os
import tarfile
import fnmatch
from typing import Optional
from pathlib import Path

from deepagents import create_deep_agent, RubricMiddleware
from deepagents.backends import CompositeBackend, StoreBackend, LocalShellBackend
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)

from .config import (
    get_llm,
    INTERRUPT_ON_TOOLS, skills_store_namespace,
    MAX_MODEL_CALLS, MAX_TOOL_CALLS,
    SANDBOX_HEALTH_CHECK_INTERVAL_SECONDS,
)
from .mcp_tools_bean import build_interrupt_on, enforce_write_tool_coverage
from .middlewares.write_approval import WriteApprovalMiddleware
from .schema import ProcurementContext
from .log_utils import agent_logger
from .memory.config import DEFAULT_MEMORY_CONFIG
from .memory.keeper import MemoryKeeper
from .memory.prompts import MAIN_SYSTEM_PROMPT, MEMORY_USAGE_PROMPT

# 项目路径
PROJECT_ROOT = Path(__file__).parent.parent.parent
SKILLS_DIR = PROJECT_ROOT / "src" / "skills"
MEMORY_DIR = Path(__file__).parent / "memory"


def _upload_project_to_sandbox(sandbox) -> bool:
    """
    将项目文件上传到沙箱 /workspace/src/ 目录，搭建 1:1 开发环境。
    跳过 node_modules / .git / __pycache__ / .next 等无关目录。
    """
    skip_patterns = [
        ".git", "__pycache__", "node_modules", ".next", ".venv",
        ".env", ".env.*", "*.pem", "*.key",
        ".eggs", "*.pyc", "*.pyo", ".DS_Store", ".docker",
        "*.png", "*.jpg", "*.jpeg", "*.gif",  # 生成物不传
    ]

    def _should_skip(rel_path: str) -> bool:
        for pattern in skip_patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
            parts = rel_path.replace("\\", "/").split("/")
            for part in parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
        return False

    remote_dir = "/workspace/src"
    try:
        sandbox.execute(f"mkdir -p '{remote_dir}'")

        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w:gz") as tar:
            for file_path in sorted(PROJECT_ROOT.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = str(file_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
                if _should_skip(rel):
                    continue
                tar.add(str(file_path), arcname=rel)

        tar_stream.seek(0)
        data = tar_stream.read()

        # 仅使用 BaseSandbox 的公开协议上传；这样 SandboxBackendProxy 热替换
        # 底层容器后，项目上传仍能工作，且不会依赖 Docker 私有 _container。
        archive_path = "/tmp/erp-agent-project.tar.gz"
        responses = sandbox.upload_files([(archive_path, data)])
        errors = [getattr(item, "error", None) for item in responses]
        if not responses or any(errors):
            raise RuntimeError(f"archive upload failed: {errors or 'no response'}")

        unpack = sandbox.execute(
            f"tar -xzf '{archive_path}' -C '{remote_dir}' && rm -f '{archive_path}'"
        )
        if unpack.exit_code != 0:
            raise RuntimeError(f"archive extraction failed: {unpack.output}")

        # 获取文件数量和总大小
        count_result = sandbox.execute(
            f"find {remote_dir} -type f 2>/dev/null | wc -l"
        )
        size_result = sandbox.execute(
            f"du -sh {remote_dir} 2>/dev/null | cut -f1"
        )
        file_count = count_result.output.strip() if count_result.exit_code == 0 else "?"
        total_size = size_result.output.strip() if size_result.exit_code == 0 else "?"

        agent_logger.info(
            f"Project uploaded to sandbox: {remote_dir} "
            f"({file_count} files, {total_size})"
        )
        return True
    except Exception as e:
        agent_logger.warning(f"Project upload to sandbox failed (non-fatal): {e}")
        return False


def bootstrap_sandbox(sandbox) -> None:
    """填充一个新领取或重建后的沙箱，使其可立即继续 Agent 工作。

    这个函数只使用稳定 Proxy 暴露的公开能力，因此健康检查重建后的同一个
    Agent、CompositeBackend 和工具对象都能继续复用。
    """
    setup = sandbox.execute(
        "mkdir -p /workspace/charts /workspace/output /workspace/data "
        "/workspace/python-packages /skills"
    )
    if setup.exit_code != 0:
        raise RuntimeError(f"sandbox directory setup failed: {setup.output}")

    if not _upload_project_to_sandbox(sandbox):
        raise RuntimeError("project bootstrap upload failed")

    skills_copy = sandbox.execute(
        "mkdir -p /skills && "
        "cp -r /workspace/src/src/skills/. /skills/ 2>/dev/null || true"
    )
    if skills_copy.exit_code != 0:
        raise RuntimeError(f"core Skills bootstrap failed: {skills_copy.output}")

    agent_logger.info("Sandbox bootstrap complete: project + core Skills restored")


def _load_skills_prompt(skills_dir: Path) -> str:
    """
    兼容入口：仅返回基础技能元数据，不读取正文注入提示词。
    """
    from .progressive_skills import SkillCatalog
    return SkillCatalog(skills_dir).index()


async def precompute_agent_context(
    user_id: str = "default_user",
    username: str = "用户",
    store=None,
) -> ProcurementContext:
    """
    预计算 Agent 上下文

    从 Store 加载用户历史偏好与 WARM 层记忆摘要，构造完整的 ProcurementContext。
    如果 Store 不可用则使用默认值。
    """
    preferences = {}
    warm_memory = ""

    if store is not None:
        try:
            namespace = ("user-preferences", user_id)
            items = store.search(namespace)
            for item in items:
                if item.key == "preferences" and isinstance(item.value, dict):
                    preferences.update(item.value)
            if preferences:
                agent_logger.info(f"Loaded preferences for {user_id}: {list(preferences.keys())}")
        except Exception as e:
            agent_logger.warning(f"Failed to load preferences: {e}")

        # WARM 层：语义记忆（偏好）+ 近期情节摘要，常驻注入系统提示词
        try:
            keeper = MemoryKeeper(store, user_id, DEFAULT_MEMORY_CONFIG)
            keeper.migrate_legacy_preferences()
            warm_memory = keeper.build_warm_brief()
            if not preferences:
                preferences.update(keeper.load_preferences())
        except Exception as e:
            agent_logger.warning(f"Failed to build warm memory: {e}")

    return ProcurementContext(
        user_id=user_id,
        username=username,
        preferences=preferences,
        warm_memory=warm_memory,
    )


def _append_guarded_general_purpose(subagents: list) -> None:
    """默认通用子 Agent 会继承全部工具。显式挂上同一份写审批，避免绕过校验和幂等键。"""
    if any(spec.get("name") == "general-purpose" for spec in subagents):
        return
    from deepagents.middleware.subagents import (
        DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
        DEFAULT_SUBAGENT_PROMPT,
    )

    subagents.append({
        "name": "general-purpose",
        "description": DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
        "system_prompt": DEFAULT_SUBAGENT_PROMPT,
        "middleware": [WriteApprovalMiddleware()],
        "interrupt_on": build_interrupt_on(),
    })


def create_main_agent(
    user_context: Optional[ProcurementContext] = None,
    checkpointer=None,
    store=None,
):
    """
    创建主 Agent 实例

    组装顺序（严格遵循文档）：
    1. LLM（DeepSeek）
    2. CompositeBackend 三层路由（default=sandbox, /memories/=Store, /persisted-skills/=Store）
    3. 工具加载（MCP + chart + web_search + hitl）
    4. 子Agent配置（YAML声明式）
    5. 中间件栈（7自定义 + 框架内置）
    6. 创建 Agent

    Args:
        user_context: 用户上下文（含偏好）
        checkpointer: LangGraph checkpointer（会话持久化）
        store: LangGraph store（跨会话存储）

    Returns:
        CompiledStateGraph - 可执行的 Agent 图
    """
    if user_context is None:
        user_context = ProcurementContext()

    agent_logger.info(f"Creating main agent for user: {user_context.user_id}")

    # ===== 1. LLM =====
    llm = get_llm()
    grader_llm = get_llm(thinking=False)

    # ===== 2. Backend - Manager + Stable Proxy + CompositeBackend =====
    # Manager 负责用户级领取/恢复/重建；Proxy 保持 Agent 已持有的后端引用稳定。
    sandbox_manager_instance = None
    managed_sandbox = False
    if os.getenv("SANDBOX_BACKEND", "").strip().lower() == "state":
        from deepagents.backends import StateBackend
        from .backends.sandbox_holder import set_sandbox

        sandbox_backend = StateBackend()
        set_sandbox(None, user_context.user_id)
        agent_logger.info("Using StateBackend because SANDBOX_BACKEND=state")
    else:
        try:
            from .backends.sandbox_manager import sandbox_manager
            from .backends.sandbox_proxy import SandboxBackendProxy
            from .backends.sandbox_holder import set_sandbox

            raw_sandbox = sandbox_manager.get_sandbox(user_context.user_id)
            sandbox_backend = SandboxBackendProxy(raw_sandbox)
            bootstrap_sandbox(sandbox_backend)
            sandbox_manager_instance = sandbox_manager
            managed_sandbox = True
            # 注册的是稳定 Proxy，而不是会在重建后失效的原始 Docker 连接。
            set_sandbox(sandbox_backend, user_context.user_id)
            agent_logger.info(
                f"Using managed Docker sandbox for {user_context.user_id}: "
                f"{sandbox_backend.container_name}"
            )
        except Exception as e:
            agent_logger.warning(
                f"Managed Docker sandbox unavailable ({e}), falling back to LocalShell"
            )
            sandbox_backend = LocalShellBackend(virtual_mode=True)
            from .backends.sandbox_holder import set_sandbox
            set_sandbox(None, user_context.user_id)

    # deepagents >= 0.7 要求传入已初始化的 Backend 实例，
    # 不再接受旧版的 backend factory。
    backend_routes = {}
    if store is not None:
        backend_routes["/memories/"] = StoreBackend(
            namespace=lambda _rt: (user_context.user_id,),
        )
        backend_routes["/persisted-skills/"] = StoreBackend(
            namespace=lambda _rt: skills_store_namespace(user_context.user_id),
        )
    composite_backend = CompositeBackend(
        default=sandbox_backend,
        routes=backend_routes,
    )

    # ===== 3. 加载工具 =====
    from .tools.mcp_client import load_mcp_tools_sync
    from .tools.chart_generator import generate_chart
    from .tools.web_search import web_search
    from .tools.web_fetch import web_fetch, install_skill
    from .tools.skill_management import list_user_skills
    from .tools.hitl_tools import request_order_info
    from .tools.download_sandbox_file import download_sandbox_file, list_sandbox_files
    from .tools.document_generator import generate_document, generate_table_report

    mcp_tools = load_mcp_tools_sync()
    # 名字像写操作、却没有进入 WRITE_TOOLS 的 MCP 工具，默认拒绝启动。
    enforce_write_tool_coverage([tool.name for tool in mcp_tools])
    from ..mcp_server.persistence import warn_if_memory_fallback_enabled
    warn_if_memory_fallback_enabled(agent_logger)
    custom_tools = [generate_chart, web_search, web_fetch, install_skill, list_user_skills, request_order_info,
                    download_sandbox_file, list_sandbox_files,
                    generate_document, generate_table_report]

    # 三层记忆：COLD 层按需检索 + 显式写入（WARM 层已在系统提示词里常驻注入）
    if DEFAULT_MEMORY_CONFIG.memory_tools_enabled:
        from .tools.memory_tools import read_memory, remember
        custom_tools.extend([read_memory, remember])
    all_tools = mcp_tools + custom_tools

    # Prompt disclosure is independent from physical sandbox file synchronization.
    from .progressive_skills import (
        SkillCatalog, ProgressiveSkillsMiddleware, attach_to_subagents, SLOT,
    )
    skill_catalog = SkillCatalog(SKILLS_DIR, store=store, user_id=user_context.user_id,
                                 sandbox=managed_sandbox)
    skill_read_tools = skill_catalog.tools()
    all_tools.extend(skill_read_tools)

    agent_logger.info(
        f"Tools loaded: {len(mcp_tools)} MCP + {len(custom_tools)} custom + "
        f"{len(skill_read_tools)} skill readers = {len(all_tools)} total"
    )

    # ===== 4. 加载子Agent配置 =====
    from .subagents.loader import load_subagent_configs, resolve_subagent_tools, get_delegation_context_prompt
    subagent_configs = load_subagent_configs()
    subagents = resolve_subagent_tools(subagent_configs, all_tools)
    attach_to_subagents(subagents, skill_catalog, skill_read_tools)
    # 放在 attach 之后，避免给通用子 Agent 写入残缺的 tools 键，从而丢掉继承来的全部工具。
    _append_guarded_general_purpose(subagents)
    # 生成委派上下文协议（注入主 Agent 提示词）
    delegation_prompt = get_delegation_context_prompt(subagent_configs)

    # ===== 5. 组装中间件栈 =====
    # 自定义中间件
    from .middlewares.sandbox_health import SandboxHealthMiddleware
    from .middlewares.context_injection import ContextInjectionMiddleware
    from .middlewares.skills_sync import SkillsSyncMiddleware
    from .middlewares.user_skills_restore import UserSkillsRestoreMiddleware
    from .middlewares.tools_summarization import ToolsSummarizationMiddleware
    from .middlewares.memory_update import MemoryUpdateMiddleware
    from .middlewares.memory_consolidation import MemoryConsolidationMiddleware
    from .middlewares.warm_memory import WarmMemoryMiddleware, WARM_MEMORY_SLOT
    from .middlewares.sandbox_breaker import SandboxCircuitBreakerMiddleware
    # Harness 阶段状态机 + 评审器（真 Harness 架构核心）
    from .harness import HarnessPhaseMiddleware, load_harness_config
    from .middlewares.review_gate import ReviewExecutionGate, SafeRubricMiddleware

    # 读取 Harness DSL 配置中的评审迭代上限
    _harness_config = load_harness_config()
    _review_cfg = _harness_config.get("review", {}) if isinstance(_harness_config, dict) else {}
    _review_max_iterations = _review_cfg.get("max_iterations", 3)
    from .jev_router import build_review_router
    router_llm = build_review_router(_review_cfg)

    # 注意：create_deep_agent 内部已自动添加：
    # - SummarizationMiddleware（自动摘要 + compact_conversation 工具）
    # - FilesystemMiddleware, SkillsMiddleware, SubAgentMiddleware
    # - TodoListMiddleware, PatchToolCallsMiddleware, MemoryMiddleware
    # 我们只需添加自定义中间件 + 调用限制
    # 沙箱重建后不仅替换底层 Docker 连接，还要重新填充项目、基础 Skills 和
    # 用户持久化 Skills。两个中间件持有同一个稳定 Proxy，缓存必须一起失效。
    skills_sync_middleware = SkillsSyncMiddleware(
        skills_dir=SKILLS_DIR,
        sandbox_backend=sandbox_backend if managed_sandbox else None,
    )
    user_skills_restore_middleware = UserSkillsRestoreMiddleware(
        store=store,
        user_id=user_context.user_id,
        sandbox_backend=sandbox_backend if managed_sandbox else None,
    )

    def _rehydrate_after_sandbox_rebuild(_replacement) -> None:
        bootstrap_sandbox(sandbox_backend)
        skills_sync_middleware.invalidate()
        user_skills_restore_middleware.invalidate()
        # 立即回填，避免本次失败后的下一条请求才重新拥有 Skills。
        skills_sync_middleware.sync_now()
        user_skills_restore_middleware.restore_now()

    middlewares = [
        # 放在最外层：先改写不合法的订单调用并注入 approval_id，再进入 HITL。
        WriteApprovalMiddleware(),
        # --- 自定义中间件（执行顺序 1→7）---
        SandboxHealthMiddleware(
            sandbox_manager=sandbox_manager_instance,
            user_id=user_context.user_id,
            sandbox_backend=sandbox_backend if managed_sandbox else None,
            on_rebuild=_rehydrate_after_sandbox_rebuild if managed_sandbox else None,
            check_interval=SANDBOX_HEALTH_CHECK_INTERVAL_SECONDS,
        ),                                                            # 1. 沙箱健康检查 + 重建
        HarnessPhaseMiddleware(router_model=router_llm),               # 2. 混合路由 + rubric 注入
        ContextInjectionMiddleware(user_context=user_context),        # 3. 用户上下文注入
        WarmMemoryMiddleware(store=store, user_id=user_context.user_id),
        ProgressiveSkillsMiddleware(skill_catalog),
        skills_sync_middleware,                                       # 4. 基础 Skills 同步
        user_skills_restore_middleware,                               # 5. 用户 Skills 恢复
        ToolsSummarizationMiddleware(),                               # 6. 摘要监控
        MemoryUpdateMiddleware(store=store, user_id=user_context.user_id),      # 7. 偏好提取（WARM）
        MemoryConsolidationMiddleware(store=store, user_id=user_context.user_id),   # 7.5 情节归档 + 遗忘（COLD）
        SandboxCircuitBreakerMiddleware(failure_threshold=3, recovery_timeout=60),  # 8. 熔断器
        # --- Harness 评审器（RubricMiddleware）---
        # 收到 rubric 后，grader 子Agent 结构化产出 satisfied/needs_revision/failed，
        # needs_revision 时自动打回模型重做，形成真实 Review 回路（非 prompt 软约束）
        SafeRubricMiddleware(model=grader_llm, max_iterations=_review_max_iterations),
        ReviewExecutionGate(_harness_config),  # after_agent 逆序：先升级，再审查
        # --- 框架内置中间件（调用限制）---
        ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS),          # 模型调用上限
        ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS),            # 工具调用上限
    ]

    # ===== 6. 构建系统提示词 =====
    system_prompt = MAIN_SYSTEM_PROMPT.format(
        user_id=user_context.user_id,
        username=user_context.username,
        preferences="以本次动态加载的记忆为准",
        warm_memory=WARM_MEMORY_SLOT,
    )
    if DEFAULT_MEMORY_CONFIG.memory_tools_enabled:
        system_prompt += MEMORY_USAGE_PROMPT
    # 注入子Agent委派上下文协议
    if delegation_prompt:
        system_prompt += delegation_prompt

    # ===== 6.5 仅注入动态目录占位符，正文/资源由只读工具按需返回 =====
    system_prompt += "\n" + SLOT

    # ===== 7. 创建 Agent =====
    # 自定义渐进披露支持中文路径；不再把全部 SKILL.md 放入系统提示。
    # skills=None 防止框架与本项目双重加载；文件仍由原同步/恢复中间件准备。
    agent = create_deep_agent(
        model=llm,
        tools=all_tools,
        system_prompt=system_prompt,
        middleware=middlewares,
        subagents=subagents if subagents else None,
        skills=None,
        memory=[str(MEMORY_DIR / "AGENTS.md")],
        backend=composite_backend,
        interrupt_on=INTERRUPT_ON_TOOLS,
        checkpointer=checkpointer,
        store=store,
        name="procurement-main-agent",
    )

    agent_logger.info(
        f"Main agent created: {len(all_tools)} tools, "
        f"{len(subagents)} subagents, {len(middlewares)} middlewares"
    )
    return agent

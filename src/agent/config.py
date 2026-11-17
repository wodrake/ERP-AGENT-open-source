"""
全局配置模块
LLM、Store、Checkpointer、沙箱连接参数
"""
import os
from langchain_deepseek import ChatDeepSeek
from .env_utils import get_env, get_env_int
from .mcp_tools_bean import build_interrupt_on

# ============ LLM 配置 ============
LLM_MODEL = get_env("LLM_MODEL", "deepseek-flash")
LLM_BASE_URL = get_env("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = get_env("DEEPSEEK_API_KEY", "")
LLM_TEMPERATURE = 0.1
LLM_MAX_TOKENS = 4096
LLM_REASONING_EFFORT = get_env("LLM_REASONING_EFFORT", "high")


def get_llm(*, thinking: bool = True, timeout: float | None = None,
            max_retries: int = 2, max_tokens: int = LLM_MAX_TOKENS) -> ChatDeepSeek:
    """获取 DeepSeek 模型实例。

    主 Agent 使用思考模式；需要强制结构化输出的内部评审器使用
    非思考模式，避免 DeepSeek 拒绝 ``tool_choice`` 参数。
    """
    common_kwargs = {
        "model": LLM_MODEL,
        "base_url": LLM_BASE_URL,
        "api_key": LLM_API_KEY,
        "max_tokens": max_tokens,
        "max_retries": max_retries,
    }
    if timeout is not None:
        common_kwargs["timeout"] = timeout
    if thinking:
        return ChatDeepSeek(
            **common_kwargs,
            reasoning_effort=LLM_REASONING_EFFORT,
            extra_body={"thinking": {"type": "enabled"}},
        )
    return ChatDeepSeek(
        **common_kwargs,
        temperature=LLM_TEMPERATURE,
        extra_body={"thinking": {"type": "disabled"}},
    )


# ============ MongoDB 配置 ============
MONGODB_URI = get_env("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB_NAME = get_env("MONGODB_DB_NAME", "erp_agent")

# ============ MCP Server 配置 ============
MCP_SERVER_URL = get_env("MCP_SERVER_URL", "http://localhost:9000")
MCP_SSE_URL = f"{MCP_SERVER_URL}/sse"

# ============ 沙箱配置 ============
SANDBOX_IMAGE = get_env("SANDBOX_IMAGE", "python:3.11-slim")#Docker 镜像名称，具体是 Python 3.11 的 slim（精简）版本
SANDBOX_WORK_DIR = "/workspace"
SANDBOX_SKILLS_DIR = "/skills"
SANDBOX_MEMORIES_DIR = "/memories"
SANDBOX_WARM_POOL_SIZE = get_env_int("SANDBOX_WARM_POOL_SIZE", 1)
SANDBOX_IDLE_TIMEOUT_MINUTES = get_env_int("SANDBOX_IDLE_TIMEOUT_MINUTES", 30)
SANDBOX_MAINTENANCE_INTERVAL_SECONDS = get_env_int(
    "SANDBOX_MAINTENANCE_INTERVAL_SECONDS", 60
)
SANDBOX_HEALTH_CHECK_INTERVAL_SECONDS = get_env_int(
    "SANDBOX_HEALTH_CHECK_INTERVAL_SECONDS", 30
)


def env_flag(key: str, default: bool = False) -> bool:
    """读取布尔环境变量。未设置时使用 default，空字符串不当成 true。"""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def sandbox_backend_mode() -> str:
    """docker：尝试容器沙箱；state：只用内存 StateBackend（CI / 无 docker.sock）。"""
    mode = (os.getenv("SANDBOX_BACKEND") or "docker").strip().lower()
    if mode not in {"docker", "state"}:
        return "docker"
    return mode


def sandbox_allow_local_fallback() -> bool:
    """默认禁止在 Docker 不可用时退回宿主机 Shell。"""
    return env_flag("SANDBOX_ALLOW_LOCAL_FALLBACK", False)

# ============ Store 命名空间 ============
SKILLS_STORE_NAMESPACE = ("persisted-skills",)
PREFERENCES_STORE_NAMESPACE = ("user-preferences",)


def skills_store_namespace(user_id: str) -> tuple[str, str]:
    """Return the isolated Store namespace for one user's custom Skills."""
    return (*SKILLS_STORE_NAMESPACE, user_id)

# ============ Agent 配置 ============
MAX_MODEL_CALLS = get_env_int("MAX_MODEL_CALLS", 50)
MAX_TOOL_CALLS = get_env_int("MAX_TOOL_CALLS", 30)
SUMMARIZATION_THRESHOLD = 0.85  # 85% 上下文窗口时触发摘要

# ============ 中断配置 ============
# 写工具只在 mcp_tools_bean.WRITE_TOOLS 声明一次，主 Agent 和子 Agent 都从那里生成。
INTERRUPT_ON_TOOLS = build_interrupt_on()

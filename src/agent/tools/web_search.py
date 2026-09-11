"""使用 DeepSeek Responses API 的内置 web_search 工具。"""

import httpx
from langchain_core.tools import tool

from ..log_utils import agent_logger
from ..env_utils import get_env


@tool
def web_search(query: str) -> str:
    """搜索互联网获取实时信息。

    Args:
        query: 搜索查询内容，例如"摩托车火花塞市场价格趋势 2026"

    Returns:
        搜索结果摘要文本.
    """
    # 主 Agent、grader 和联网搜索统一使用 DeepSeek key。
    api_key = get_env("DEEPSEEK_API_KEY", "")
    if not api_key:
        return "错误: 未配置 DEEPSEEK_API_KEY，无法执行网络搜索"

    try:
        # DeepSeek Responses API 的 web_search 是服务端执行的内置工具，
        # 不需要再配置 DashScope/Tavily 等第二个搜索 Key。
        response = httpx.post(
            get_env(
                "DEEPSEEK_RESPONSES_URL",
                "https://api.deepseek.com/responses",
            ),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                # 搜索接口只接受 DeepSeek 搜索模型；不要复用可能仍是
                # qwen3.8-27b 等旧配置的 LLM_MODEL。
                "model": get_env("WEB_SEARCH_MODEL", "deepseek-flash"),
                "instructions": (
                    "你是一个联网搜索助手。请使用 web_search 获取最新、可靠的信息，"
                    "用中文总结结果；如果搜索结果不足或无法确认，请明确说明。"
                ),
                "input": query,
                "tools": [{"type": "web_search"}],
                # web_search 工具被显式要求调用，避免模型只凭训练记忆回答。
                "tool_choice": {"type": "web_search"},
                "temperature": 0.3,
                "max_output_tokens": 2000,
            },
            timeout=30,
        )

        if response.status_code == 200:
            result = response.json()
            content = _extract_response_text(result)
            if not content:
                return "搜索成功，但 DeepSeek 没有返回可读摘要"
            agent_logger.info(f"Web search completed for: {query[:50]}")
            return content
        else:
            try:
                error = response.json().get("error", {}).get(
                    "message", str(response.status_code)
                )
            except Exception:
                error = response.text or str(response.status_code)
            return f"搜索失败: {error}"

    except httpx.TimeoutException:
        return "搜索超时，请稍后重试"
    except Exception as e:
        agent_logger.error(f"Web search error: {e}")
        return f"搜索异常: {str(e)}"


def _extract_response_text(payload: dict) -> str:
    """兼容 Responses API 的 output_text 及原始 output 两种返回形态。"""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", [])
        if isinstance(content, str):
            chunks.append(content)
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"output_text", "text"}:
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
    return "\n".join(chunks).strip()

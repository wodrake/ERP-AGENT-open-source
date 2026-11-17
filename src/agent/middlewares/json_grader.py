"""DeepSeek 可用的 rubric grader：json_object + 本地解析。

框架的 ``RubricMiddleware`` 用 ``create_agent(response_format=GraderResponse)``
让 LangChain 自己挑结构化输出策略。LangChain 依据模型 profile（DeepSeek
声明了 ``structured_output: True``）选中 ``ProviderStrategy``，请求体里带上
``response_format={"type": "json_schema", ...}``；DeepSeek 接口不支持这种格式，
直接返回 400 ``This response_format type is unavailable now``，评分因此永远失败，
审查回路形同关闭。

这里只替换“怎么向模型要结构化结果”这一步：
- 用 DeepSeek 支持的 ``response_format={"type": "json_object"}`` 发请求；
- 在本地把 JSON 解析、纠偏成 ``GraderResponse``；
- 解析不出来时抛 ``GraderUnavailable``，由 ``SafeRubricMiddleware`` 降级跳过，
  不阻断主流程。

其余行为（transcript 裁剪、criteria 冻结、覆盖度重试、needs_revision 打回）
仍全部复用框架实现，未修改 .venv 中的第三方库。
"""
from __future__ import annotations

import json
import re
from typing import Any

from deepagents.middleware import GraderResponse, RubricMiddleware
from langchain_core.messages import SystemMessage

from ..log_utils import agent_logger

_JSON_INSTRUCTION = """## 输出格式（必须严格遵守）
只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释性前后缀。字段：
{
  "result": "satisfied" | "needs_revision" | "failed",
  "explanation": "一到两句判定说明",
  "criteria": [
    {"name": "复述该条标准", "passed": true},
    {"name": "复述该条标准", "passed": false, "gap": "缺什么、错在哪"}
  ]
}
规则：result 三选一；criteria 必须覆盖 rubric 的每一条，顺序一致；
passed 为 false 时必须给出 gap，passed 为 true 时不要给 gap。"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

_VERDICT_ALIASES = {
    "satisfied": "satisfied",
    "pass": "satisfied",
    "passed": "satisfied",
    "success": "satisfied",
    "needs_revision": "needs_revision",
    "needsrevision": "needs_revision",
    "revision": "needs_revision",
    "fail": "needs_revision",
    "failed_criteria": "needs_revision",
    "failed": "failed",
    "error": "failed",
    "ungradable": "failed",
}


class GraderUnavailable(RuntimeError):
    """评分器没有产出可用的结构化结果（接口报错、JSON 不可解析等）。"""


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    cleaned = _FENCE.sub("", cleaned).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def _normalize_verdict(value: Any) -> str | None:
    if isinstance(value, bool):
        return "satisfied" if value else "needs_revision"
    if isinstance(value, str):
        key = value.strip().lower().replace(" ", "_").replace("-", "_")
        return _VERDICT_ALIASES.get(key)
    return None


def _normalize_criteria(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    criteria = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        passed = item.get("passed")
        if isinstance(passed, str):
            passed = passed.strip().lower() in {"true", "yes", "pass", "passed", "1"}
        passed = bool(passed)
        criterion = {"name": str(item.get("name") or f"criterion-{index}"), "passed": passed}
        if not passed:
            criterion["gap"] = str(item.get("gap") or item.get("reason") or "未满足该条标准")
        criteria.append(criterion)
    return criteria


def parse_grader_response(content: Any) -> GraderResponse:
    """把模型输出解析为 GraderResponse，必要时做确定性纠偏。"""
    if not isinstance(content, str):
        content = str(content or "")
    try:
        data = json.loads(_strip_code_fence(content))
    except (ValueError, TypeError) as exc:
        raise GraderUnavailable(f"评分结果不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GraderUnavailable("评分结果不是 JSON 对象")

    criteria = _normalize_criteria(data.get("criteria"))
    verdict = _normalize_verdict(data.get("result"))
    # json_object 不强制枚举取值，模型可能给出 true/false 或近义词。
    # 用逐条判定结果纠偏，避免因为一个字段就把整次审查降级掉。
    if criteria:
        any_failed = any(not item["passed"] for item in criteria)
        if verdict is None:
            verdict = "needs_revision" if any_failed else "satisfied"
        elif verdict == "satisfied" and any_failed:
            verdict = "needs_revision"
        elif verdict == "needs_revision" and not any_failed:
            verdict = "satisfied"
    if verdict is None:
        raise GraderUnavailable("评分结果缺少合法的 result 字段")

    payload = {
        "result": verdict,
        "explanation": str(data.get("explanation") or "").strip() or "评分器未给出说明",
        "criteria": criteria,
    }
    try:
        return GraderResponse.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 -- 校验失败同样是“评分不可用”
        raise GraderUnavailable(f"评分结果不符合 GraderResponse: {exc}") from exc


class JsonModeRubricMiddleware(RubricMiddleware):
    """用 DeepSeek 支持的 json_object 完成 grader 的结构化输出。"""

    def _grader_messages(self, state, iteration: int, correction: str | None):
        messages = self._grader_input(state, iteration, correction)["messages"]
        return [SystemMessage(content=f"{self._system_prompt}\n\n{_JSON_INSTRUCTION}"), *messages]

    def _json_grader(self):
        model = self._model
        if not hasattr(model, "bind"):
            # 允许传入 provider:model 等字符串；此时退回框架默认实现。
            return None
        return model.bind(response_format={"type": "json_object"})

    def _invoke_grader(self, state, iteration: int, correction: str | None = None, *, context: object | None = None):
        if self._tools:
            # 评分器自带工具时仍走框架的 create_agent 实现。
            return super()._invoke_grader(state, iteration, correction, context=context)
        runnable = self._json_grader()
        if runnable is None:
            return super()._invoke_grader(state, iteration, correction, context=context)
        message = runnable.invoke(self._grader_messages(state, iteration, correction))
        return parse_grader_response(getattr(message, "content", message))

    async def _ainvoke_grader(self, state, iteration: int, correction: str | None = None, *, context: object | None = None):
        if self._tools:
            return await super()._ainvoke_grader(state, iteration, correction, context=context)
        runnable = self._json_grader()
        if runnable is None:
            return await super()._ainvoke_grader(state, iteration, correction, context=context)
        message = await runnable.ainvoke(self._grader_messages(state, iteration, correction))
        return parse_grader_response(getattr(message, "content", message))


def log_grader_skip(exc: BaseException | str) -> None:
    """审查降级只留一条日志，避免刷屏。"""
    if isinstance(exc, BaseException):
        detail = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
        label = type(exc).__name__
    else:
        detail, label = str(exc).strip()[:200], "grader_error"
    agent_logger.warning(f"审查器不可用，本轮跳过结果审查（不影响主流程与写工具审批）：{label}: {detail}")

"""把订单写入校验错误交还给模型，不触发审批，也不访问 ERP。"""
import json

from langchain_core.tools import tool


@tool
def report_write_validation_error(tool_name: str, error: str) -> str:
    """返回订单写入前的确定性校验错误。这是内部工具，不要主动调用。

    Args:
        tool_name: 被拦截的写工具名
        error: 校验错误说明
    """
    return json.dumps(
        {
            "code": 400,
            "validation_error": True,
            "tool_name": tool_name,
            "message": error,
            "data": None,
        },
        ensure_ascii=False,
    )

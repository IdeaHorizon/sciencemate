"""Read historical receipts without re-entering effectful tools."""
from core.context_view import get
from core.tool_registry import ToolDefinition, register_tool


async def read_tool_result(state, result_ref: str, offset: int = 0, limit: int = 16000, **_):
    try:
        return get(state).read_result(result_ref, offset=offset, limit=limit)
    except ValueError as exc:
        return dict(status="error", error=str(exc))


register_tool(
    ToolDefinition(
        name="read_tool_result",
        description=(
            "凭上下文压缩提示中的 result_ref 只读取回本 run 的历史工具回执，"
            "包括失败结果；不会重新执行原工具或提交作业。"
            "引用绑定原始记录的位置及摘要，worker 重启后仍有效。"
            "offset/limit 按字符分页，每页最多 16000 字符。"
            "历史回执不是当前状态；要知道作业现在怎样，仍需 remote_job status/wait。"
        ),
        replayable_read=True,
        parameters_schema=dict(
            type="object", additionalProperties=False,
            properties=dict(
                result_ref=dict(type="string"),
                offset=dict(type="integer", minimum=0, default=0),
                limit=dict(type="integer", minimum=1, maximum=16000, default=16000),
            ),
            required=["result_ref"],
        ),
    ),
    read_tool_result,
)

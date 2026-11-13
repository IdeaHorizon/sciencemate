"""Request a bounded literature dependency through the normal child dispatcher."""
from core.tool_registry import ToolDefinition, get_tool, register_tool


def _recover_literature_receipt(arguments, *, state, tool_call_id):
    # The child dispatcher owns the checkpoint, dispatch id and producer binding.
    recovery = get_tool("run_node").interrupted_recovery
    if recovery is not None:
        return recovery({"node_type": "literature"}, state=state, tool_call_id=tool_call_id)
    return None


async def _request_literature_evidence(state, research_question, mode="method_lookup",
                                       forward_artifact_ids=None):
    from shared.tools.run_node import _run_node_tool

    return await _run_node_tool(
        state, node_type="literature", user_note=research_question, forward_artifact_ids=forward_artifact_ids,
        node_inputs={"research_question": research_question, "mode": mode},
    )


register_tool(ToolDefinition(
    name="request_literature_evidence",
    description="实验中某个具体方法、参数或矛盾需要查论文时交给 Literature。返回原子节点回执，"
                "用 read_artifact 读取它实际交回的证据包。整项综述或批量全文核验应退给父调度器。",
    parameters_schema={
        "type": "object", "additionalProperties": False,
        "properties": {
            "research_question": {"type": "string", "minLength": 1,
                                  "description": "本次实验依赖的具体问题，可引用已有索引和缺口"},
            "mode": {"type": "string", "enum": ["method_lookup", "targeted_lookup", "contradiction_check"]},
            "forward_artifact_ids": {"type": "array", "items": {"type": "string"},
                                     "description": "本次查证要复用的已有产物 ID"},
        },
        "required": ["research_question"],
    },
    allowed_node_types=["experiment"],
    interrupted_recovery=_recover_literature_receipt,
), _request_literature_evidence)

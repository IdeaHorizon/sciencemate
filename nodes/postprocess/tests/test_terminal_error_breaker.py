from __future__ import annotations

from core.harness import NodeHarness
from core.loop_hooks import HookContext
from core.state import State
from nodes.postprocess.hooks import _postprocess_terminal_error_on_turn_end


def _context(
    tmp_path, result: dict, turn: int = 1, tool: str = "render_figure"
) -> HookContext:
    state = State.new("postprocess", tmp_path)
    context = HookContext(
        harness=NodeHarness(node_type="postprocess"),
        state=state,
        messages=[],
        turn=turn,
    )
    context.tool_call_records = [{"name": tool, "args": {}, "result": result}]
    return context


def test_first_terminal_error_injects_stop_instruction(tmp_path) -> None:
    context = _context(
        tmp_path,
        {"status": "error", "error": "clean_results requires upstream metadata: units"},
    )
    message = _postprocess_terminal_error_on_turn_end(context)
    assert message is not None
    assert "Do not retry" in message.content
    assert "_loop_terminal" not in context.state.hook_state


def test_second_terminal_error_sets_mechanical_breaker(tmp_path) -> None:
    context = _context(
        tmp_path,
        {"status": "error", "error": "clean_results requires upstream metadata: units"},
    )
    _postprocess_terminal_error_on_turn_end(context)
    context.turn = 2
    message = _postprocess_terminal_error_on_turn_end(context)
    assert message is None
    assert context.state.hook_state["_loop_terminal"]["requested_by"] == (
        "postprocess_terminal_error_breaker"
    )


def test_recoverable_argument_error_does_not_trigger_breaker(tmp_path) -> None:
    context = _context(
        tmp_path,
        {"status": "error", "error": "output_files must be an array of workspace-relative paths"},
    )
    assert _postprocess_terminal_error_on_turn_end(context) is None
    assert "_postprocess_terminal_error_count" not in context.state.hook_state


def test_single_clean_figure_closes_without_another_llm_tool_turn(tmp_path) -> None:
    # B 刀：提前收工只看机械事实 —— 唯一请求的 figure 记录铸出来了且
    # findings 为空；没有 quality_gate/status 合成判决。
    result = {
        "status": "success",
        "figure_id": "figure__one",
        "figure_hash": "sha256:" + "0" * 64,
        "findings": [],
        "files": [{"format": "png", "absolute_path": "/tmp/figure.png"}],
    }
    context = _context(tmp_path, result)
    context.state.hook_state["node_inputs"] = {
        "visual_requests": [{"request_id": "one"}]
    }
    assert _postprocess_terminal_error_on_turn_end(context) is None
    terminal = context.state.hook_state["_loop_terminal"]
    assert terminal["status"] == "completed"
    assert terminal["requested_by"] == "postprocess_success_finalizer"
    assert "figure__one" in terminal["final_text"]


def test_figure_with_open_findings_leaves_the_turn_to_the_agent(tmp_path) -> None:
    """有 findings 时不提前收工 —— 修不修归 agent 判断，框架不代答。"""
    result = {
        "status": "success",
        "figure_id": "figure__one",
        "findings": [
            {"collector": "OB-TEXT-BOUNDS", "message": "text extends past the canvas"}
        ],
        "files": [{"format": "png", "absolute_path": "/tmp/figure.png"}],
    }
    context = _context(tmp_path, result)
    context.state.hook_state["node_inputs"] = {
        "visual_requests": [{"request_id": "one"}]
    }
    assert _postprocess_terminal_error_on_turn_end(context) is None
    assert "_loop_terminal" not in context.state.hook_state


def test_multiple_requests_do_not_close_after_the_first_figure(tmp_path) -> None:
    result = {
        "status": "success",
        "figure_id": "figure__one",
        "findings": [],
        "files": [{"format": "png", "absolute_path": "/tmp/figure.png"}],
    }
    context = _context(tmp_path, result)
    context.state.hook_state["node_inputs"] = {
        "visual_requests": [{"request_id": "one"}, {"request_id": "two"}]
    }
    assert _postprocess_terminal_error_on_turn_end(context) is None
    assert "_loop_terminal" not in context.state.hook_state

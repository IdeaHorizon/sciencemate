"""curator 整合门禁必须凭"真整合过"，不是"子 run 返回了"（issue #229）。

qinp 2026-07-29 实测：Mode 1 curator 读到外部 survey artifact，但
  - 首次 scan_artifact_disagreements 返回 scanned_artifacts=[]（没扫到目标）
  - 之后只有 kb_overview / list_artifacts / search_kb 等 13 次只读调用
  - 无 KB 写入、无 proposal、最后 finish_reason=stop + 空 content
`summary.json` 仍是 status=completed / missing_required_outputs=[]，orchestrator
据此把 **全部** pending_curator_integrations 清空、所有 pending flow entry 的
curator_state 改成 done —— 整合门禁凭一次空跑就开了。

三层 fail-open：_curator 无 required_output/QC；#184 的 blank_stop 判据要求
"整轮无成功工具调用"（13 次只读正好绕过）；_finish_child 的 integration 分支
连 summary status 都不看且无条件全清。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import nodes._curator.hooks  # noqa: F401  注册 hook
from core.harness import NodeHarness
from core.state import State
from nodes._curator.hooks import build_integration_receipt
from shared.tools.run_node import _curator_integration_verdict, _finish_child

# 2026-08-19：curator 退出 post-producing flow 后，原本钉「flow 门禁怎么被 curator
# 解锁」的 5 条用例整体作废 —— 那道门连同它解锁的 curator_state 一起删了。本文件
# 保留的是**回执本身**的判据（跑了就得真跑过，#229），它与 curator 是不是流程一环
# 无关。新形态见 tests/test_curator_is_out_of_the_flow.py。

_TARGET = "survey_report__peer_assisted_bare_metal_provisioning_survey"


_CURATOR_HARNESS = NodeHarness(node_type="_curator", system_prompt="",
                               required_output_artifact_types=[])


def _state(node_type="_orchestrator") -> State:
    return State.new(node_type=node_type, base_dir=Path(tempfile.mkdtemp()),
                     project_id="p1")


# ── receipt 机械判据 ────────────────────────────────────────────────────────


def test_receipt_flags_unscanned_target():
    """复刻事故：读到了但 scan 返回空 scanned_artifacts → n_unintegrated=1。"""
    st = _state("_curator")
    ni = {"mode": "integration", "trigger_node": "literature",
          "artifact_ids": [_TARGET]}
    records = [
        {"name": "read_artifact", "args": {"artifact_id": _TARGET},
         "result": {"status": "error", "error": "not found in this run"}},
        {"name": "scan_artifact_disagreements", "args": {"artifact_ids": [_TARGET]},
         "result": {"status": "success", "scanned_artifacts": []}},   # ← 空！
        {"name": "read_external_artifact", "args": {"artifact_id": _TARGET},
         "result": {"status": "success"}},
        # 之后全是只读
        *[{"name": n, "args": {}, "result": {"status": "success"}}
          for n in ("kb_overview", "list_artifacts", "search_kb")],
    ]
    r = build_integration_receipt(st, ni, tool_records=records)
    assert r["applicable"] is True
    assert r["n_unintegrated"] == 1
    assert r["verdict"] == "incomplete_scan"
    assert _TARGET in r["artifacts_read"]          # 读到了
    assert _TARGET not in r["artifacts_scanned"]   # 但没扫到
    assert r["n_kb_writes"] == 0


def test_receipt_ok_when_read_and_scanned():
    st = _state("_curator")
    ni = {"mode": "integration", "artifact_ids": [_TARGET]}
    records = [
        {"name": "read_external_artifact", "args": {"artifact_id": _TARGET},
         "result": {"status": "success"}},
        {"name": "scan_artifact_disagreements", "args": {},
         "result": {"status": "success", "scanned_artifacts": [_TARGET]}},
        {"name": "create_claim", "args": {}, "result": {"status": "success"}},
    ]
    r = build_integration_receipt(st, ni, tool_records=records)
    assert r["n_unintegrated"] == 0
    assert r["verdict"] == "ok"
    assert r["n_kb_writes"] == 1


def test_receipt_legit_no_op_passes():
    """合法 no-op：扫全了、确认零候选 —— 门禁卡的是"没扫"，不是"没写"。"""
    st = _state("_curator")
    ni = {"mode": "integration", "artifact_ids": [_TARGET]}
    records = [
        {"name": "read_artifact", "args": {"artifact_id": _TARGET},
         "result": {"status": "success"}},
        {"name": "scan_artifact_disagreements", "args": {},
         "result": {"status": "success", "scanned_artifacts": [_TARGET]}},
    ]
    r = build_integration_receipt(st, ni, tool_records=records)
    assert r["n_unintegrated"] == 0
    assert r["verdict"] == "ok_no_op"
    assert "无候选" in r["reason"]


def test_receipt_not_applicable_for_dreaming():
    """dreaming / scheduled 模式不适用 → n_unintegrated=0，QC 不误伤。"""
    st = _state("_curator")
    for mode in ("dreaming", "scheduled"):
        r = build_integration_receipt(st, {"mode": mode}, tool_records=[])
        assert r["applicable"] is False
        assert r["n_unintegrated"] == 0


# ── QC：空跑不得判 completed ────────────────────────────────────────────────


# test_qc_fails_on_unscanned_target 已随 QC 层删除（2026-08-22）。

# test_qc_fails_closed_without_receipt 已随 QC 层删除（2026-08-22）。

def _child_summary(tmp: Path, *, status="completed", receipt: dict | None = None) -> dict:
    """造一份 curator 子 run 的 summary + transcript（含/不含 receipt）。"""
    d = tmp / "child"
    d.mkdir(parents=True, exist_ok=True)
    tp = d / "transcript.jsonl"
    lines = []
    if receipt is not None:
        lines.append(json.dumps({"event": "curator_integration_receipt", **receipt},
                                ensure_ascii=False))
    tp.write_text("\n".join(lines), encoding="utf-8")
    return {"run_id": "cur-1", "status": status, "state_dir": str(d),
            "node_type": "_curator", "turns": 6, "artifacts": []}


def _flow(state: State, *, producing_node="literature", artifact_ids=None,
          run_id="run-lit-1") -> dict:
    e = {"producing_node": producing_node, "producing_run_id": run_id,
         "artifact_ids": artifact_ids or [_TARGET],
         "review_state": "done", "curator_state": "pending",
         "decision_state": "pending"}
    state.hook_state.setdefault("pending_post_node_flow", []).append(e)
    return e


def test_verdict_rejects_noop(tmp_path):
    """A1：外部读取后没成功扫描 → 不放行。"""
    s = _child_summary(tmp_path, receipt={"applicable": True, "n_unintegrated": 1,
                                          "verdict": "incomplete_scan",
                                          "reason": "1 个目标未扫到"})
    ok, why, _ = _curator_integration_verdict(s, {"mode": "integration"})
    assert ok is False and "未扫" in why


def test_verdict_rejects_incomplete_child(tmp_path):
    s = _child_summary(tmp_path, status="incomplete",
                       receipt={"applicable": True, "n_unintegrated": 0})
    ok, why, _ = _curator_integration_verdict(s, {"mode": "integration"})
    assert ok is False and "completed" in why


def test_verdict_rejects_missing_receipt(tmp_path):
    s = _child_summary(tmp_path, receipt=None)
    ok, why, _ = _curator_integration_verdict(s, {"mode": "integration"})
    assert ok is False and "receipt" in why


def test_verdict_accepts_clean_run(tmp_path):
    s = _child_summary(tmp_path, receipt={"applicable": True, "n_unintegrated": 0,
                                          "verdict": "ok", "n_kb_writes": 2})
    ok, why, r = _curator_integration_verdict(s, {"mode": "integration"})
    assert ok is True and why == ""
    assert r["n_kb_writes"] == 2


def test_empty_artifact_ids_is_not_a_free_pass():
    """qinp 复核残留①（另一半）：artifact_ids=[] 曾生成 ok_no_op 白过门禁。
    空目标整合不了任何东西，必须判失败，且不能跟合法 no-op 混为一谈。"""
    from nodes._curator.hooks import build_integration_receipt
    st = _state("_curator")
    r = build_integration_receipt(
        st, {"mode": "integration", "trigger_node": "literature", "artifact_ids": []},
        tool_records=[{"name": "kb_overview", "args": {},
                       "result": {"status": "success"}}])
    assert r["verdict"] == "empty_targets"
    assert r["n_unintegrated"] == 1          # QC (<=0) 与门禁一起 fail-closed
    assert r["verdict"] != "ok_no_op"


def test_empty_targets_blocked_end_to_end(tmp_path):
    """端到端：空目标 curator run 不得放行任何 flow。"""
    st = _state()
    entry = _flow(st)
    s = _child_summary(tmp_path, receipt={"applicable": True, "n_unintegrated": 1,
                                          "verdict": "empty_targets",
                                          "reason": "没有传入 artifact_ids"})
    _finish_child(st, "_curator",
                  {"mode": "integration", "trigger_node": "literature",
                   "artifact_ids": []},
                  s, _CURATOR_HARNESS, None)
    assert entry["curator_state"] == "pending"


def test_dreaming_mode_untouched(tmp_path):
    """dreaming 分支行为不变（它本来就要求 completed）。"""
    st = _state()
    entry = _flow(st)
    s = _child_summary(tmp_path, receipt=None)
    _finish_child(st, "_curator", {"mode": "dreaming"}, s, _CURATOR_HARNESS, None)
    # integration 门禁不受 dreaming run 影响
    assert entry["curator_state"] == "pending"

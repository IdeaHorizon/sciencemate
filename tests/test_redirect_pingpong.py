"""redirect 踢皮球检测 + 结构文件校验门禁（issue #166 第 1、2 条）。

第 1 条背景：experiment 有硬门禁「前处理产物缺失必须 redirect_upstream: data」，
data 侧原本零 QC 可自由以「这是计算任务」退回 → 无限对踢，且框架**没有任何
redirect 账本**（redirect 靠 orchestrator LLM 读 prompt 执行），所以"我们已经
踢过一轮"在框架层不可见，永远不会仲裁。

第 2 条背景：结构文件校验器早就存在（_inspect_structure_file），但只挂在
recover_atomic_structure 恢复路径上，不是交付物门禁；加上 data 原本
quality_checks: []，坏 POSCAR 一路畅通到 reviewer（preview 里看不出格式错）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.state import State
from shared.tools.library.decision_package import (
    _record_redirect_edge,
    clear_redirect_pingpong,
    detect_redirect_pingpong,
)


def _make_state() -> State:
    return State.new(node_type="_orchestrator", base_dir=Path(tempfile.mkdtemp()))


# ── redirect 账本 / 往返检测 ────────────────────────────────────────────────


def test_single_redirect_is_not_pingpong() -> None:
    """A→B 单向 redirect 是完全正常的流程，绝不能误报。"""
    state = _make_state()
    _record_redirect_edge(state, "experiment", "data")
    assert detect_redirect_pingpong(state) is None
    assert detect_redirect_pingpong(state, "data") is None


def test_reverse_redirect_detected_as_pingpong() -> None:
    """B 又 redirect 回 A = 两边对归属没共识 → 判定踢皮球。

    注意正常流程里"上游产出后重跑下游"是**重跑**不是 redirect，所以反向
    redirect 边本身就是异常信号，不用等第二轮。
    """
    state = _make_state()
    _record_redirect_edge(state, "experiment", "data")
    _record_redirect_edge(state, "data", "experiment")

    pp = detect_redirect_pingpong(state)
    assert pp is not None
    assert pp["pair"] == ["data", "experiment"]
    assert len(pp["edges"]) == 2


def test_pingpong_scoped_to_requested_node() -> None:
    """只报与将要启动的节点相关的那一对，别的往返不影响本次。"""
    state = _make_state()
    _record_redirect_edge(state, "writing", "postprocess")
    _record_redirect_edge(state, "postprocess", "writing")

    assert detect_redirect_pingpong(state, "writing") is not None
    assert detect_redirect_pingpong(state, "data") is None      # 无关节点不受影响


def test_self_redirect_and_blank_ignored() -> None:
    state = _make_state()
    _record_redirect_edge(state, "data", "data")     # 自环无意义
    _record_redirect_edge(state, None, "data")
    _record_redirect_edge(state, "data", None)
    assert detect_redirect_pingpong(state) is None


def test_clear_removes_only_that_pair() -> None:
    state = _make_state()
    _record_redirect_edge(state, "experiment", "data")
    _record_redirect_edge(state, "data", "experiment")
    _record_redirect_edge(state, "writing", "postprocess")

    clear_redirect_pingpong(state, ["experiment", "data"])

    assert detect_redirect_pingpong(state, "experiment") is None
    remaining = state.hook_state["_redirect_ledger"]
    assert remaining == [{"from": "writing", "to": "postprocess",
                          "at": remaining[0]["at"]}]


def test_ledger_is_bounded() -> None:
    """账本有上限，长会话不会无限涨。"""
    state = _make_state()
    for i in range(40):
        _record_redirect_edge(state, f"n{i}", f"n{i+1}")
    assert len(state.hook_state["_redirect_ledger"]) == 20


def test_run_node_block_is_one_shot() -> None:
    """核心防呆：拦截会**消费掉**这一对边 —— 人工裁定后再起该节点不会被
    同一条规则再拦（否则就是我在 #151 造过的那种无出口 dead-end）。"""
    state = _make_state()
    _record_redirect_edge(state, "experiment", "data")
    _record_redirect_edge(state, "data", "experiment")

    first = detect_redirect_pingpong(state, "data")
    assert first is not None
    clear_redirect_pingpong(state, first["pair"])          # run_node 拦截时做的事

    assert detect_redirect_pingpong(state, "data") is None  # 第二次放行
    assert detect_redirect_pingpong(state, "experiment") is None

    # 但再对踢一轮 → 重新拦
    _record_redirect_edge(state, "experiment", "data")
    _record_redirect_edge(state, "data", "experiment")
    assert detect_redirect_pingpong(state, "data") is not None


# ── 结构文件校验门禁 ────────────────────────────────────────────────────────


_GOOD_POSCAR = """Si2
1.0
  5.43 0.00 0.00
  0.00 5.43 0.00
  0.00 0.00 5.43
Si
2
Direct
  0.00 0.00 0.00
  0.25 0.25 0.25
"""

# 声称 2 个原子但只给了 1 行坐标 —— 下游解析器会炸，正是 issue 报的症状
_BROKEN_POSCAR = """Si2
1.0
  5.43 0.00 0.00
  0.00 5.43 0.00
  0.00 0.00 5.43
Si
2
Direct
  0.00 0.00 0.00
"""


def _data_state() -> State:
    return State.new(node_type="data", base_dir=Path(tempfile.mkdtemp()))


def _latest_event(state: State, name: str) -> dict | None:
    import json
    if not state.transcript_path.exists():
        return None
    latest = None
    for line in state.transcript_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") == name:
            latest = e
    return latest


def test_structure_validation_passes_on_good_poscar() -> None:
    from nodes.data.hooks import validate_structure_outputs
    state = _data_state()
    (Path(state.root) / "POSCAR").write_text(_GOOD_POSCAR, encoding="utf-8")

    payload = validate_structure_outputs(state)
    assert payload["n_scanned"] == 1
    assert payload["n_invalid"] == 0
    assert _latest_event(state, "structure_file_validation")["n_invalid"] == 0


def test_structure_validation_catches_broken_poscar() -> None:
    """这条就是 e2e 实测的事故：格式错误的 POSCAR 必须被机械抓住。"""
    from nodes.data.hooks import validate_structure_outputs
    state = _data_state()
    (Path(state.root) / "POSCAR").write_text(_BROKEN_POSCAR, encoding="utf-8")

    payload = validate_structure_outputs(state)
    assert payload["n_invalid"] == 1
    ev = _latest_event(state, "structure_file_validation")
    assert ev["n_invalid"] == 1
    assert "POSCAR" in ev["invalid_files"][0]["path"]


def test_structure_validation_no_files_is_pass() -> None:
    """非结构类前处理任务（纯表格清洗等）不该被这条 QC 误伤。"""
    from nodes.data.hooks import validate_structure_outputs
    state = _data_state()
    (Path(state.root) / "table.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    payload = validate_structure_outputs(state)
    assert payload["n_scanned"] == 0 and payload["n_invalid"] == 0


# test_structure_qc_mechanical_pass_and_fail 已随 QC 层删除（2026-08-22）：structure_file_validation 事件仍由
# hook 落盘，reviewer 硬规则（n_invalid ≥ 1 → critical）继续消费它。

# test_structure_qc_fails_closed_without_event 已随 QC 层删除（2026-08-22）：structure_file_validation 事件仍由
# hook 落盘，reviewer 硬规则（n_invalid ≥ 1 → critical）继续消费它。

# test_data_qc_contract_is_satisfied_by_enabled_hooks 已随 QC 层删除（2026-08-22）：structure_file_validation 事件仍由
# hook 落盘，reviewer 硬规则（n_invalid ≥ 1 → critical）继续消费它。

def test_data_custom_loop_wraps_for_guaranteed_validation() -> None:
    """data 的 custom loop 有多条早退路径从不调 run_default（框架 on_end 不
    触发）—— run_loop 必须是包装器，在 finally 里兜底跑结构校验。"""
    import inspect
    from nodes.data import data_agent_loop

    src = inspect.getsource(data_agent_loop.run_loop)
    assert "_run_loop_impl" in src
    assert "validate_structure_outputs" in src
    assert "finally" in src

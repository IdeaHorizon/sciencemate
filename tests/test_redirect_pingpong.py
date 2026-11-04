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


def _review_structure(tmp_path, filename, content):
    from nodes.data.review.package_reviewer import review_preprocessing_package

    (tmp_path / filename).write_text(content, encoding="utf-8")
    return review_preprocessing_package(
        "materials", {filename: content}, {"package_dir": str(tmp_path)},
    )


def test_structure_validation_passes_on_good_poscar(tmp_path):
    review = _review_structure(tmp_path, "POSCAR", _GOOD_POSCAR)
    assert review["status"] == "pass"
    assert review["common_checks"]["structure_format:POSCAR"]


def test_structure_validation_catches_broken_poscar(tmp_path):
    review = _review_structure(tmp_path, "POSCAR", _BROKEN_POSCAR)
    assert review["status"] == "fail"
    assert any(issue["code"] == "invalid_structure_asset" and issue["file"] == "POSCAR"
               and issue["severity"] == "critical" for issue in review["issues"])


def test_structure_validation_no_files_is_pass(tmp_path):
    review = _review_structure(tmp_path, "table.csv", "a,b\n1,2\n")
    assert review["status"] == "pass"
    assert not any(key.startswith("structure_format:") for key in review["common_checks"])


@pytest.mark.asyncio
async def test_bad_structure_is_stopped_at_publication_before_model_approval(tmp_path, monkeypatch):
    from nodes.data.tools import execute_preprocessing_plan as pipeline
    from nodes.data.tools.package_publisher import package_paths

    state = State.new(node_type="data", base_dir=tmp_path / "runs")
    staging, final = package_paths(state, "")
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "POSCAR").write_text(_BROKEN_POSCAR, encoding="utf-8")

    async def cannot_approve(*args, **kwargs):
        pytest.fail("invalid native structure reached model approval")

    monkeypatch.setattr(pipeline, "review_delivery_against_authority", cannot_approve)
    result = await pipeline._review_and_publish_candidate(
        state, {}, {"staging_dir": str(staging), "final_dir": str(final),
                    "delivery_files": [{"path": "POSCAR"}]}, [],
    )
    assert result["status"] == "review_failed"
    assert not state.list_artifacts("dataset")
    assert not final.exists()
    assert (staging / "POSCAR").read_text() == _BROKEN_POSCAR
    assert "invalid_structure_asset" in Path(result["review_report"]).read_text()

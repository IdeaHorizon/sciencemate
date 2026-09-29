"""实验出了结果，Analysis 就欠一版新的 research_state —— 这条账没人记。

## 现场（E2E v23，2026-08-10 → 08-11）

    08-10 06:47  hypothesis 冻结预注册，出 research_state v1
                   completed_experiments: []
                   next_steps: "派 experiment 跑 LAMMPS…"、
                               "**分析拟合 Arrhenius 与 VFT，按预注册判定树裁决**"
    08-11 01:04  orchestrator 派发 experiment
    08-11 02:20  experiment 跑完：76 分钟、9 个真实 LAMMPS 模拟
    08-11 02:25  → _reviewer
    08-11 02:36  → _curator
    08-11 03:27  → _reviewer（重试）→ 进程死

    research_state 至今 **还是 v1**。

计划自己写着"下一步做分析"，实验也真的出了结果，然后**没有任何机制**提醒
orchestrator 该回 Analysis。回不回去，全靠模型自己想起来。

## 这条判断框架能机械做

义务账本（`core.obligations`）就是干这个的 —— "谁欠着什么、怎么了结"。它有
三个 collector（申诉 / 重复失败 / 未测 metric），**没有一条覆盖这件事**，
而算它需要的事实全都现成：

    最新 research_state 的 created_at   （账本 save 行写的，进 Git）
    完成的 experiment run 的 finished_at（run 账本）

判据用**时间戳**而不是 `completed_experiments` 这个列表字段：列表是产出方
自己写的（自证），时间戳是框架写的。列表仍然认 —— 作为额外的了结路径，
不作为唯一依据。

## 它只"看见"，不判质量

义务说的是"实验出结果之后 Analysis 没被调用过"，不是"分析做得好不好"。
后者是 reviewer 的事。机器负责看见，判决交给读得到产物的那一方。

夹具照现行记录模型：research_state / run_manifest 是节点目录下的原生文件，
版本、出处、登记时刻在工作区账本（`core/ledger`）里；同一身份连续 save 就是
连续版本，head 是最新版。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import obligations
from core.ledger import write_record
from core.project_workspace import _NODE_WORKSPACES as _DIRS
from core.run_history import RunRecord


class _State:
    def __init__(self, worktree: Path) -> None:
        self.project_worktree = worktree
        self.node_type = "_orchestrator"

    def append_transcript(self, event: str, **fields) -> None:
        pass


def _research_state(root: Path, version: int, created_at: str,
                    completed: list[str] | None = None) -> dict:
    """按版本原语落一版 research_state：同一身份再 save 一次就是下一版。"""
    return write_record(
        root, artifact_type="research_state", name="research_state",
        content=f"# Research State v{version}", directory=_DIRS["hypothesis"],
        metadata={
            "version": version,
            "verdict": "continue",
            "completed_experiments": completed or [],
            "hypotheses": [{"id": "H1", "status": "active"}],
        },
        produced_by_node_type="hypothesis", produced_by_run_id="r-hyp",
        created_at=created_at,
    )


def _experiment_run(run_id: str, finished_at: float, *,
                    status: str = "completed") -> RunRecord:
    return RunRecord(
        state_dir=Path("/nonexistent"),      # 这条账不读 run 目录，只看字段
        run_id=run_id,
        node_type="experiment",
        status=status,
        finished_at=finished_at,
    )


_STATE_CREATED_ISO = "2026-08-10T06:47:53.144057+00:00"
_LATER_ISO = "2026-08-11T05:00:00+00:00"


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / _DIRS["hypothesis"]).mkdir(parents=True)
    (root / _DIRS["experiment"]).mkdir(parents=True)
    return root


def test_a_finished_experiment_with_no_newer_analysis_is_owed(worktree: Path) -> None:
    """真实现场：research_state v1 早于实验，实验跑完了，没有 v2。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO)
    runs = [_experiment_run("1786410296-fc9b0b", _iso_epoch("2026-08-11T02:20:42+00:00"))]

    owed = obligations._collect_unanalyzed_results(_State(worktree), runs)
    hits = [o for o in owed if o.kind == obligations.KIND_UNANALYZED_RESULTS]

    assert hits, "实验出了结果、Analysis 没被调用过 —— 这条账必须记上"
    assert hits[0].owed_by == "hypothesis"
    assert "1786410296-fc9b0b" in json.dumps(hits[0].extra, ensure_ascii=False)
    assert hits[0].blocking, "结果没被分析过就收尾 = 做完实验直接去写论文"


def test_a_newer_research_state_discharges_it(worktree: Path) -> None:
    """Analysis 回来过了（出了更新的一版）→ 账清掉。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO)
    _research_state(worktree, 2, _LATER_ISO)
    runs = [_experiment_run("1786410296-fc9b0b", _iso_epoch("2026-08-11T02:20:42+00:00"))]

    owed = obligations._collect_unanalyzed_results(_State(worktree), runs)
    assert not [o for o in owed if o.kind == obligations.KIND_UNANALYZED_RESULTS]


def test_explicit_accounting_also_discharges_it(worktree: Path) -> None:
    """显式把 run_id 记进 completed_experiments 也算了结 —— 多一条路，不是唯一依据。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO, completed=["1786410296-fc9b0b"])
    runs = [_experiment_run("1786410296-fc9b0b", _iso_epoch("2026-08-11T02:20:42+00:00"))]

    owed = obligations._collect_unanalyzed_results(_State(worktree), runs)
    assert not [o for o in owed if o.kind == obligations.KIND_UNANALYZED_RESULTS]


def test_an_unfinished_experiment_is_not_owed_yet(worktree: Path) -> None:
    """还在跑 / 挂了的实验不算"出了结果"，别催。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO)
    runs = [_experiment_run("r-running", 0.0, status="running")]

    owed = obligations._collect_unanalyzed_results(_State(worktree), runs)
    assert not [o for o in owed if o.kind == obligations.KIND_UNANALYZED_RESULTS]


def test_no_research_state_at_all_is_not_this_obligation(worktree: Path) -> None:
    """连 v1 都没有 = 还没到 Analysis 这一步，是别的问题，别在这里吵。"""
    runs = [_experiment_run("r1", _iso_epoch("2026-08-11T02:20:42+00:00"))]
    owed = obligations._collect_unanalyzed_results(_State(worktree), runs)
    assert not [o for o in owed if o.kind == obligations.KIND_UNANALYZED_RESULTS]


def test_the_discharge_hint_names_the_node_and_the_run(worktree: Path) -> None:
    """指路必须具体到"调谁、带哪个 run 的结果" —— 泛泛的提醒等于没有。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO)
    runs = [_experiment_run("1786410296-fc9b0b", _iso_epoch("2026-08-11T02:20:42+00:00"))]

    hit = [o for o in obligations._collect_unanalyzed_results(_State(worktree), runs)
           if o.kind == obligations.KIND_UNANALYZED_RESULTS][0]
    assert "hypothesis" in hit.discharge_hint
    assert "1786410296-fc9b0b" in hit.discharge_hint
    assert "research_state" in hit.acceptance


def _iso_epoch(text: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(text).timestamp()


def test_it_is_actually_wired_into_collect(tmp_path: Path, monkeypatch) -> None:
    """走生产入口 `collect()`，不是只有直接调 collector 才работает.

    「机制存在但没接到路径」是本项目最常见的缺陷形状 —— 新加一条 collector
    也得自己受这一问：真实的 orchestrator state 上有 `project_worktree` 吗？
    `collect()` 会不会把它吞在 try/except 里？
    """
    root = tmp_path / "wt"
    (root / _DIRS["hypothesis"]).mkdir(parents=True)
    _research_state(root, 1, _STATE_CREATED_ISO)
    finished = _iso_epoch("2026-08-11T02:20:42+00:00")

    worktree_path, run_root = root, tmp_path / "runs" / "r"

    class _Full:
        project_worktree = worktree_path
        node_type = "_orchestrator"
        root = run_root
        project_id = "p"
        run_id = "cur"

    monkeypatch.setattr(
        obligations.run_history, "load_runs",
        lambda *a, **k: [_experiment_run("1786410296-fc9b0b", finished)],
    )
    kinds = [o.kind for o in obligations.collect(_Full())]
    assert obligations.KIND_UNANALYZED_RESULTS in kinds, "collect() 没走到这条账"

    # 而且它必须真的挡收尾 —— blocking 不只是个字段。终态闸的牙齿在消费端
    # （chat 读 blocking()），这里断言账进了 blocking 集合。
    blocked = obligations.blocking(obligations.collect(_Full()))
    assert blocked and any("hypothesis" in (o.owed_by or "") for o in blocked)


# ── 只有"有结论要裁决"的运行才欠这笔账（issue #413）──────────────────────────

def _run_manifest(root: Path, run_id: str, *, run_role: str, stage: str,
                  created_at: str = "2026-08-11T02:21:00+00:00") -> dict:
    """experiment 每次收尾写的那份 run_manifest（run_manifest.v1）。

    `requires_hypothesis_verdict` 就是它自己算的 `primary + simulation`
    —— 框架读它，不另造判据。每个 run 一份身份（`run_manifest_<run_id>`）；
    同一个 run 的起始版和收尾版是同一身份的两个版本，head 是后写的那版。
    """
    manifest = {
        "schema_version": "run_manifest.v1",
        "run_id": run_id,
        "run_role": run_role,
        "stage": stage,
        "analysis_eligible": run_role == "primary",
        "requires_hypothesis_verdict": run_role == "primary" and stage == "simulation",
        "status": "completed",
    }
    return write_record(
        root, artifact_type="run_manifest", name=f"run_manifest_{run_id}",
        content=json.dumps(manifest, ensure_ascii=False), directory=_DIRS["experiment"],
        metadata={"run_id": run_id, "run_role": run_role,
                  "analysis_eligible": manifest["analysis_eligible"]},
        produced_by_node_type="experiment", produced_by_run_id=run_id,
        created_at=created_at,
    )


def test_a_debug_run_does_not_owe_an_analysis(worktree: Path) -> None:
    """编译检查 / 环境诊断跑完了，但没有科学结论 —— 不该硬送去 Analysis。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO)
    run_id = "1786410296-debug1"
    _run_manifest(worktree, run_id, run_role="secondary", stage="debug")
    runs = [_experiment_run(run_id, _iso_epoch("2026-08-11T02:20:42+00:00"))]

    owed = obligations._collect_unanalyzed_results(_State(worktree), runs)
    assert not [o for o in owed if o.kind == obligations.KIND_UNANALYZED_RESULTS], \
        "secondary/debug 运行没有要裁决的结论，不该欠 Analysis"


def test_a_primary_simulation_still_owes_an_analysis(worktree: Path) -> None:
    """别为了放过调试运行把正式模拟也放过了。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO)
    run_id = "1786410296-fc9b0b"
    _run_manifest(worktree, run_id, run_role="primary", stage="simulation")
    runs = [_experiment_run(run_id, _iso_epoch("2026-08-11T02:20:42+00:00"))]

    hits = [o for o in obligations._collect_unanalyzed_results(_State(worktree), runs)
            if o.kind == obligations.KIND_UNANALYZED_RESULTS]
    assert hits and run_id in json.dumps(hits[0].extra, ensure_ascii=False)


def test_a_run_with_no_manifest_is_treated_conservatively(worktree: Path) -> None:
    """读不到声明的历史运行照旧欠账 —— 少做一次分析比多提醒一次贵得多。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO)
    runs = [_experiment_run("1786410296-old111", _iso_epoch("2026-08-11T02:20:42+00:00"))]

    hits = [o for o in obligations._collect_unanalyzed_results(_State(worktree), runs)
            if o.kind == obligations.KIND_UNANALYZED_RESULTS]
    assert hits, "没有 manifest 就当作要裁决，不能默认放过"


def test_the_final_manifest_wins_over_the_startup_one(worktree: Path) -> None:
    """起始那版 manifest（status=running）不能盖掉收尾那版的声明。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO)
    run_id = "1786410296-fc9b0b"
    _run_manifest(worktree, run_id, run_role="secondary", stage="debug",
                  created_at="2026-08-11T01:00:00+00:00")
    _run_manifest(worktree, run_id, run_role="primary", stage="simulation",
                  created_at="2026-08-11T02:30:00+00:00")
    runs = [_experiment_run(run_id, _iso_epoch("2026-08-11T02:20:42+00:00"))]

    hits = [o for o in obligations._collect_unanalyzed_results(_State(worktree), runs)
            if o.kind == obligations.KIND_UNANALYZED_RESULTS]
    assert hits, "后写的 manifest 说这是 primary simulation，账就得记上"


def test_only_the_analysable_run_is_listed_when_both_ran(worktree: Path) -> None:
    """同一批里既有正式模拟又有调试运行 —— 账上只该出现前者。"""
    _research_state(worktree, 1, _STATE_CREATED_ISO)
    _run_manifest(worktree, "1786410296-real11", run_role="primary", stage="simulation")
    _run_manifest(worktree, "1786410296-debug1", run_role="secondary", stage="debug",
                  created_at="2026-08-11T02:22:00+00:00")
    runs = [_experiment_run("1786410296-real11", _iso_epoch("2026-08-11T02:20:42+00:00")),
            _experiment_run("1786410296-debug1", _iso_epoch("2026-08-11T02:25:42+00:00"))]

    hits = [o for o in obligations._collect_unanalyzed_results(_State(worktree), runs)
            if o.kind == obligations.KIND_UNANALYZED_RESULTS]
    assert hits, hits
    listed = json.dumps(hits[0].extra, ensure_ascii=False)
    assert "1786410296-real11" in listed and "1786410296-debug1" not in listed, listed

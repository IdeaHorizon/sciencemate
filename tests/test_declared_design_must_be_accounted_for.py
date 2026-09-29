"""预注册声明了实验设计，就得有人交代兑现情况。

## 现场（E2E v23，2026-08-11）

冻结的预注册里写着：

    temperature_points: 7
    expected_params.temperature_points: [0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 1.0]
    n_replicas: 2

实际跑出来：7 个温度点都有生产日志，但**只有 0.45 / 0.50 / 0.55 有轨迹文件**
（`.lammpstrj`），另外 4 个既没有轨迹、日志里也没有 `c_msd` 列 —— 也就是说
**这 4 个点根本算不出 D_A**。预注册要 7 个点来区分 Arrhenius 和 VFT，
可用的只有 3 个，而且全挤在低温端。

没有任何一层提出这件事：

  · `_collect_unmeasured` 那条账读 `frozen_commitments()`，而它解析的是预注册
    **正文**里的 YAML metric 块 —— 这份预注册没写成那个形状，返回空，
    **义务永远不触发**
  · hypothesis（Analysis）本该拿结果对预注册判定 —— 但它这一轮**一次没跑过**
  · reviewer 审的是一条作业提交记录

## 这一条只做"看见"，不做判决

机器能机械知道的只有一件事：**预注册声明过一个设计，而没有任何一版
research_state 交代过它的兑现情况**。至于"3 个点够不够判定"，那是 Analysis
读得到数据才能下的判断 —— 硬编码成阈值就是又一个会凑会误伤的门（今晚下线
QC 判决层正是因为这个）。

所以义务的了结方式是：**Analysis 在 research_state 里如实写清哪些设计点实现了、
哪些没有**。框架负责让这件事没法被忘掉。

## 判据从已有字段现算

不要求节点在预注册里多写一份 `metrics` —— 多写一份就多一个会分叉的真相源。
`expected_params` 已经是机器可读的，直接拿它。

夹具照现行记录模型：预注册与 research_state 是 `plan/` 下的原生文件，冻结是
工作区账本（`core/ledger`）上带 `frozen_at` 的一行，版本由账本发。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import obligations
from core.ledger import workspace_store, write_record
from core.project_workspace import _NODE_WORKSPACES as _DIRS

_PLAN = _DIRS["hypothesis"]


class _State:
    def __init__(self, worktree: Path) -> None:
        self.project_worktree = worktree
        self.node_type = "_orchestrator"

    def append_transcript(self, event: str, **fields) -> None:
        pass


def _prereg_named(root: Path, name: str, *, expected_params: dict,
                  frozen_at: str, frozen: bool = True) -> str:
    """带真实冻结时间戳的预注册 —— 线上 freeze_artifact 写的就是这个形状
    （账本 freeze 行的 `frozen_at`）。返回 artifact id。"""
    rec = write_record(
        root, artifact_type="pre_registration", name=name,
        content="# H1\n自由文本，没有 YAML metric 块。", directory=_PLAN,
        metadata={"expected_params": expected_params},
        produced_by_node_type="hypothesis", produced_by_run_id="r-hyp",
        created_at=frozen_at,
    )
    if frozen:
        workspace_store(root).freeze(rec["id"], metadata_patch={}, by_node="hypothesis",
                                     by_run="r-hyp", frozen_at=frozen_at)
    return rec["id"]


def _prereg(root: Path, *, expected_params: dict, frozen: bool = True,
            frozen_at: str = "2026-08-10T00:00:00+00:00") -> str:
    """E2E v23 的时间线：预注册 08-10 冻结，research_state 08-11 才出。"""
    return _prereg_named(root, "X", expected_params=expected_params,
                         frozen=frozen, frozen_at=frozen_at)


def _research_state_at(root: Path, version: int, *, at: str,
                       design_accounting=None) -> None:
    """按版本原语落一版 research_state：同一身份再 save 一次就是下一版。"""
    meta = {"version": version, "verdict": "continue"}
    if design_accounting is not None:
        meta["design_accounting"] = design_accounting
    write_record(
        root, artifact_type="research_state", name="research_state",
        content=f"# Research State v{version}", directory=_PLAN, metadata=meta,
        produced_by_node_type="hypothesis", produced_by_run_id="r-hyp", created_at=at,
    )


def _research_state(root: Path, version: int, *, design_accounting=None) -> None:
    _research_state_at(root, version, at="2026-08-11T00:00:00+00:00",
                       design_accounting=design_accounting)


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / _PLAN).mkdir(parents=True)
    (root / _DIRS["experiment"]).mkdir(parents=True)
    return root


def test_a_declared_design_with_no_accounting_is_owed(worktree: Path) -> None:
    """真实现场：冻结预注册声明了 7×2，没有任何一版 research_state 交代过。"""
    _prereg(worktree, expected_params={
        "temperature_points": [0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 1.0],
        "n_replicas": 2, "n_particles": 1000,
    })
    _research_state(worktree, 1)

    owed = obligations._collect_unaccounted_design(_State(worktree), [])
    assert owed, "预注册声明了设计却没人交代兑现 —— 这条账必须记上"
    o = owed[0]
    assert o.owed_by == "hypothesis"
    assert "7" in o.what           # 7 个温度点
    assert "research_state" in o.acceptance


def test_accounting_in_research_state_discharges_it(worktree: Path) -> None:
    """Analysis 交代过了 → 账清掉。判决是它做的，框架只负责让它没法忘。"""
    _prereg(worktree, expected_params={
        "temperature_points": [0.45, 0.5, 0.55], "n_replicas": 2,
    })
    _research_state(worktree, 2, design_accounting={
        "temperature_points": "0.45/0.50 有轨迹可算 D_A；0.55 轨迹损坏，已重跑",
    })

    assert not obligations._collect_unaccounted_design(_State(worktree), [])


def test_an_unfrozen_prereg_is_not_a_commitment(worktree: Path) -> None:
    """没冻结的预注册还在改 —— 拿它当承诺会在设计期就吵。"""
    _prereg(worktree, expected_params={"temperature_points": [1, 2]}, frozen=False)
    _research_state(worktree, 1)

    assert not obligations._collect_unaccounted_design(_State(worktree), [])


def test_no_expected_params_means_nothing_to_account_for(worktree: Path) -> None:
    """没声明设计就没这笔账 —— 别凭空造一个谁也答不上的问题。"""
    _prereg(worktree, expected_params={})
    _research_state(worktree, 1)

    assert not obligations._collect_unaccounted_design(_State(worktree), [])


def test_it_does_not_judge_whether_the_data_is_enough(worktree: Path) -> None:
    """只说"没人交代过"，不说"3 个点不够" —— 那是 Analysis 读了数据才能判的。

    硬编码成阈值就是又一个会凑会误伤的门。
    """
    _prereg(worktree, expected_params={
        "temperature_points": [0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 1.0], "n_replicas": 2,
    })
    _research_state(worktree, 1)

    o = obligations._collect_unaccounted_design(_State(worktree), [])[0]
    text = o.what + o.acceptance + o.discharge_hint
    for word in ("不够", "不足", "至少", "必须达到"):
        assert word not in text, f"这条账在替 Analysis 下判断：{word}"


def test_it_is_wired_into_collect(worktree: Path, monkeypatch) -> None:
    """接线：走生产入口 collect()，不是只有直接调 collector 才算。"""
    _prereg(worktree, expected_params={"temperature_points": [1, 2, 3], "n_replicas": 2})
    _research_state(worktree, 1)

    run_root = worktree.parent / "runs" / "r"

    class _Full:
        project_worktree = worktree
        node_type = "_orchestrator"
        root = run_root
        project_id = "p"
        run_id = "cur"

    monkeypatch.setattr(obligations.run_history, "load_runs", lambda *a, **k: [])
    kinds = [o.kind for o in obligations.collect(_Full())]
    assert obligations.KIND_UNACCOUNTED_DESIGN in kinds


# ── 一份预注册一笔账（issue #414）────────────────────────────────────────────

def test_a_prereg_frozen_after_the_accounting_is_still_owed(worktree: Path) -> None:
    """#414 的现场：先冻 v1、交代过一次，之后又冻了 v2 —— v2 不能被那笔旧账清掉。

    一版 research_state 只可能交代它写作时已经存在的东西。
    """
    _prereg_named(worktree, "v1", expected_params={"temperature_points": [1, 2]},
                  frozen_at="2026-08-11T00:00:00+00:00")
    _research_state_at(worktree, 1, at="2026-08-11T06:00:00+00:00",
                       design_accounting={"temperature_points": "2/2 已实现"})
    _prereg_named(worktree, "v2", expected_params={"temperature_points": [1, 2, 3, 4]},
                  frozen_at="2026-08-12T00:00:00+00:00")

    owed = obligations._collect_unaccounted_design(_State(worktree), [])
    names = [o.extra.get("pre_registration") for o in owed]
    assert names == ["v2"], f"v2 是新冻的，旧账清不掉它；实际欠账：{names}"
    assert "v2" in owed[0].what, owed[0].what


def test_each_unaccounted_prereg_gets_its_own_line(worktree: Path) -> None:
    """两份都没交代 → 两笔账，各自点名。混成一笔就没人知道欠的是哪几份。"""
    _prereg_named(worktree, "v1", expected_params={"n_replicas": 2},
                  frozen_at="2026-08-11T00:00:00+00:00")
    _prereg_named(worktree, "v2", expected_params={"n_replicas": 4},
                  frozen_at="2026-08-12T00:00:00+00:00")

    owed = obligations._collect_unaccounted_design(_State(worktree), [])
    assert sorted(o.extra["pre_registration"] for o in owed) == ["v1", "v2"]
    # 多份时必须把"要点名"这条讲给模型，否则它写一笔笼统的账清不掉后面那份
    assert "点名" in owed[0].acceptance or "哪一份" in owed[0].discharge_hint


def test_naming_a_prereg_discharges_exactly_that_one(worktree: Path) -> None:
    """accounting 点了名 → 清那一份；没被点名的那份照旧欠着。"""
    _prereg_named(worktree, "v1", expected_params={"n_replicas": 2},
                  frozen_at="2026-08-11T00:00:00+00:00")
    _prereg_named(worktree, "v2", expected_params={"n_replicas": 4},
                  frozen_at="2026-08-12T00:00:00+00:00")
    _research_state_at(worktree, 1, at="2026-08-11T06:00:00+00:00",
                       design_accounting={"pre_registration": "v2",
                                          "n_replicas": "4/4 已实现"})

    owed = obligations._collect_unaccounted_design(_State(worktree), [])
    assert [o.extra["pre_registration"] for o in owed] == ["v1"]


def test_an_accounting_after_both_freezes_clears_both(worktree: Path) -> None:
    """别把账记严成解不开：写在两份都冻结之后的交代，两份都算清。"""
    _prereg_named(worktree, "v1", expected_params={"n_replicas": 2},
                  frozen_at="2026-08-11T00:00:00+00:00")
    _prereg_named(worktree, "v2", expected_params={"n_replicas": 4},
                  frozen_at="2026-08-12T00:00:00+00:00")
    _research_state_at(worktree, 3, at="2026-08-13T00:00:00+00:00",
                       design_accounting={"all": "逐项交代过"})

    assert not obligations._collect_unaccounted_design(_State(worktree), [])


def test_missing_timestamps_stay_lenient(worktree: Path) -> None:
    """缺时间戳时退回老口径 —— 这条义务 blocking，判严了会锁死项目。

    账本的 freeze 行总带 `frozen_at`；"缺"在这里指**读不出来**的时间戳
    （`_stamp` → 0 = 这份没有可用的时间信息），判据那边据此退回宽松口径。
    """
    _prereg(worktree, expected_params={"n_replicas": 2}, frozen_at="(unreadable)")
    _research_state(worktree, 1, design_accounting={"n_replicas": "2/2"})

    assert not obligations._collect_unaccounted_design(_State(worktree), [])

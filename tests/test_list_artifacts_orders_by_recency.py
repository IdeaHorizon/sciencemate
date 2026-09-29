"""list_artifacts 的返回顺序是契约：末位 = 最新。

回归来源（E2E v22，2026-08-09）：全仓 7 处调用方写 `artifacts[-1]` 表示"最新"，
而实现按文件名排序。真实碰撞是 experiment_log 的两份记录——

    experiment_log__Cooling_Rate_Tg_KA_LJ_v13_4rate_run   12:19:09  ← 真的
    experiment_log__LaTeX_Compilation_Fix_v13_4rate       12:18:31  ← 'L' > 'C'，赢了

writing 的机械门禁据此判定必需字段缺失，成稿被禁。

顺序来自账本行的 `created_at`（`core/ledger`），不来自文件名、也不来自 mtime。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.artifact_provenance import produced
from core.ledger import RecordStore
from core.state import State


def _store(state: State) -> RecordStore:
    return RecordStore(state.root / "artifacts", state.root / "records.jsonl")


def _write(state: State, artifact_id: str, created_at: str | None,
           artifact_type: str = "experiment_log") -> None:
    """直接落一条账本行（登记时刻由调用方给定）—— 模拟别的 run 登记过的记录。"""
    _store(state).save(
        artifact_id=artifact_id, artifact_type=artifact_type, name=artifact_id,
        content="", metadata={}, directory=state.root / "artifacts",
        created_at=created_at or "", provenance=produced("experiment", "r-x"),
        produced_by_node_type="experiment", produced_by_run_id="r-x",
        by_node="experiment", by_run="r-x",
    )


@pytest.fixture()
def state(tmp_path: Path) -> State:
    return State(run_id="r", node_type="writing", root=tmp_path)


def test_last_entry_is_newest_not_alphabetically_last(state: State) -> None:
    """v22 的真实碰撞：名字排后但时间更早的那份，不许占据末位。"""
    _write(state, "experiment_log__Cooling_Rate_Tg_KA_LJ_v13_4rate_run",
           "2026-08-09T12:19:09.138240+00:00")
    _write(state, "experiment_log__LaTeX_Compilation_Fix_v13_4rate",
           "2026-08-09T12:18:31.030253+00:00")

    got = state.list_artifacts("experiment_log")

    assert [r["id"] for r in got] == [
        "experiment_log__LaTeX_Compilation_Fix_v13_4rate",
        "experiment_log__Cooling_Rate_Tg_KA_LJ_v13_4rate_run",
    ]
    assert got[-1]["id"].endswith("v13_4rate_run")


def test_full_v22_experiment_log_family_orders_by_time(state: State) -> None:
    family = [
        ("experiment_log__Cooling_Rate_Tg_KA_LJ_run", "2026-08-08T01:25:37+00:00"),
        ("experiment_log__Cooling_Rate_Tg_KA_LJ_v11_lowT_run", "2026-08-08T17:56:07+00:00"),
        ("experiment_log__Cooling_Rate_Tg_KA_LJ_staging_recompile", "2026-08-09T11:19:28+00:00"),
        ("experiment_log__LaTeX_Compilation_Fix_v13_4rate", "2026-08-09T12:18:31+00:00"),
        ("experiment_log__Cooling_Rate_Tg_KA_LJ_v13_4rate_run", "2026-08-09T12:19:09+00:00"),
    ]
    for aid, ts in reversed(family):  # 写入（账本）顺序刻意与时间序相反
        _write(state, aid, ts)

    assert [r["id"] for r in state.list_artifacts("experiment_log")] == \
        [aid for aid, _ in family]


def test_missing_created_at_never_climbs_to_newest(state: State) -> None:
    """手写/半截的账本行缺 created_at —— 它排在最前，不许因坏数据当上'最新'。"""
    _write(state, "experiment_log__handwritten", None)
    _write(state, "experiment_log__real_run", "2026-08-09T12:19:09+00:00")

    got = state.list_artifacts("experiment_log")
    assert got[-1]["id"] == "experiment_log__real_run"


def test_same_timestamp_falls_back_to_stable_name_order(state: State) -> None:
    """同秒登记时顺序必须确定，否则门禁判定会随机漂移。"""
    ts = "2026-08-09T12:19:09+00:00"
    _write(state, "experiment_log__bbb", ts)
    _write(state, "experiment_log__aaa", ts)

    first = [r["id"] for r in state.list_artifacts("experiment_log")]
    second = [r["id"] for r in state.list_artifacts("experiment_log")]
    assert first == second == ["experiment_log__aaa", "experiment_log__bbb"]


def test_malformed_json_still_does_not_break_listing(state: State) -> None:
    """观察不该被坏数据打断（issue #299 的不变量，排序改造后仍成立）：
    账本里一行坏 JSON 只丢那一行。"""
    ledger = state.root / "records.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{not json\n", encoding="utf-8")
    _write(state, "experiment_log__ok", "2026-08-09T12:19:09+00:00")

    got = state.list_artifacts("experiment_log")
    assert [r["id"] for r in got] == ["experiment_log__ok"]

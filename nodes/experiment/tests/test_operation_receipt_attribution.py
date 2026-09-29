"""收据审计：归属判定先于内容裁决，枚举失败不是本 run 的账本事故。

#879 在 _operation_closure_receipt 里加了一串卫语句，其中前四处排在归属判定
**之前** —— 而 artifact 目录是**节点**作用域、跨 run 累积的，identity 十字段又
不含 run id。后果是：别的 run、别的 job 留下的一份坏文件，能把此后每一次
operation finalize 永久钉死。

节点规则原文：外 run 或其他 attempt 的合法收据在候选阶段过滤，不直接视为当前
run 被篡改；payload 与 producer 身份自相矛盾才是完整性错误。

这里钉住的不变量：
- artifacts 目录里一个**与本作业无关**的坏 json，不得让本 run 的收据审计失败；
- 被枚举成收据但读不动的记录，归属判不出来时仍按"可能是我的"硬拒（fail closed）
  —— 跳过它就可能重铸出第二份同 identity 终态收据；
- 解析流水线上各段的失败收成**一个**信封，读不动的位置放进 unreadable_stage。
"""
from __future__ import annotations

from pathlib import Path

from core.ledger import LEDGER_RELATIVE
from nodes.experiment.tools import resource_manager as manager
from test_operation_job_finalization import (
    _local_submission, _operation_receipt_payload, _save_operation_receipt,
    _shared_workspace_states,
)


def _write_unrelated_junk(state, *, into_ledger: bool = True):
    """与本作业无关的坏数据：记录目录里模型自己写的非记录文件，外加账本里的坏行。

    C1（2026-09-12）起枚举读的是账本行、坏行跳过，目录里多出来的文件不是记录。
    两种形状都造出来，钉住的仍是同一条：无关的坏数据不得左右本 run 的收据审计。
    """
    (state.records_dir / "my_notes.json").write_text("[1, 2, 3]", encoding="utf-8")
    (state.records_dir / "gbk_note.json").write_bytes("笔记".encode("gbk"))
    if into_ledger:
        ledger = Path(state.project_worktree) / LEDGER_RELATIVE
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write("[1, 2, 3]\n{ not json\n")


def test_an_unrelated_bad_json_does_not_dead_end_the_receipt_audit(tmp_path):
    """模型自己写的一份笔记不该让每一次 operation finalize 永久失败。

    C1 之前 core/state.py 的 list_artifacts 会被顶层非 dict、非 UTF-8 的 json 打断
    整份枚举；C1 起枚举读账本行、坏行跳过（core/ledger.py RecordStore.rows）。形状
    变了，要钉的不变量没变，所以两种坏数据都造。
    """
    submission = _local_submission()
    _first, state = _shared_workspace_states(tmp_path, submission)
    _write_unrelated_junk(state)

    resolved = manager._operation_closure_receipt(
        state, submission, "operation_completed")

    # 没有收据 → absent，而不是 audit_failed。
    assert resolved["status"] == "absent", resolved


def test_a_bad_json_does_not_hide_this_runs_own_receipt(tmp_path):
    """降级枚举必须仍然找得到本 run 自己那份收据，否则会重铸出第二份。"""
    submission = _local_submission()
    _first, state = _shared_workspace_states(tmp_path, submission)
    mine = _save_operation_receipt(
        state, submission, "my_receipt",
        _operation_receipt_payload(state, submission))
    _write_unrelated_junk(state)

    resolved = manager._operation_closure_receipt(
        state, submission, "operation_completed")

    assert resolved["status"] == "success", resolved
    assert resolved["artifact_id"] == mine["id"]


def test_an_unreadable_own_receipt_is_still_refused_in_one_envelope(tmp_path):
    """本 run 自己的收据读不动时仍硬拒（跳过会重铸），但只给一个信封。"""
    submission = _local_submission()
    _first, state = _shared_workspace_states(tmp_path, submission)
    mine = _save_operation_receipt(
        state, submission, "my_receipt",
        _operation_receipt_payload(state, submission))
    # C1 起正文就是原生文件：把它写坏，账本行照旧指名本 job。
    state.find_artifact_path(mine["id"]).write_text(
        "{ this is not json", encoding="utf-8")

    resolved = manager._operation_closure_receipt(
        state, submission, "operation_completed")

    assert resolved["status"] == "error", resolved
    assert resolved["error_code"] == "operation_closure_receipt_audit_failed"
    # 一个信封说清读不动的位置，不必逐段打地鼠。
    assert resolved["unreadable_stage"] == "payload_json"
    assert resolved["artifact_id"] == mine["id"]

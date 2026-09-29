"""Artifact 身份 + 版本原语（RFC 2026-08-18，落盘形状按 RFC 2026-09-12 §6）。

旧模型：文件身份 = `{type}__{slug(name)}.json`，名字即身份；冻结只说"不可变"
不说"如何修订"；报错同时禁止覆盖和换名 —— 模型被逼换名，身份碎裂成
`pre_registration__v1…v6`，六个消费方各自发明"哪份是当前的"（#414/#453/#395）。

新模型：路径即身份、head 即当前版本、修订走 amend（带理由、框架算 diff、
账本哈希链）。正文是原生文件（`plan/pre_registration__s.md`），事实在账本
（`core/ledger`）；冻结是账本上的一行，文件一个字节不动；闸的钉死 = 账本上
「冻结且未修订」的 head，修订后解除（冻结的字节在 git 历史 / run 内快照里）。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from core.ledger import LEDGER_RELATIVE, RecordStore, workspace_store, write_record
from core.state import State


@pytest.fixture()
def state(tmp_path: Path) -> State:
    """run 本地 State（没绑 worktree）：记录落 `<run>/artifacts/`，账本 `<run>/records.jsonl`。"""
    return State(run_id="r1", node_type="hypothesis", root=tmp_path / "run1")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    (root / "plan").mkdir()
    (root / "plan" / "README.md").write_text("# plan\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


@pytest.fixture()
def bound(tmp_path: Path) -> State:
    """绑了 Project worktree 的 State：记录落 `plan/`，账本 `.research/ledger/records.jsonl`。"""
    return State.new("hypothesis", tmp_path / "runs", project_worktree=_worktree(tmp_path))


def _freeze(state: State, artifact_id: str) -> Path:
    """最小化冻结（等价 freeze_artifact 的核心：账本一行 freeze，文件不动）。"""
    state.mark_frozen(artifact_id, {})
    return state.find_artifact_path(artifact_id)


# ── 身份与版本 ──────────────────────────────────────────────────────────────

def test_every_save_bumps_the_version_and_keeps_a_snapshot(state: State) -> None:
    r1 = state.save_artifact("survey_report", "lit", "v1 body", metadata={})
    r2 = state.save_artifact("survey_report", "lit", "v2 body", metadata={})
    assert (r1["version"], r2["version"]) == (1, 2)

    versions = state.artifact_versions(r1["id"])
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["content"] == "v1 body", "旧版必须还能整篇读回来"
    assert versions[1]["prev_content_hash"] == versions[0]["content_hash"], \
        "逐版内容哈希要连成链"


def test_undo_backup_points_at_the_version_snapshot(state: State) -> None:
    """.history 机制已删除 —— /undo 的补偿路径由 run 内版本快照接任。"""
    r1 = state.save_artifact("survey_report", "lit", "v1 body", metadata={})
    state.save_artifact("survey_report", "lit", "v2 body", metadata={})
    undo = state.hook_state["_last_artifact_overwrite"]
    assert undo["artifact_id"] == r1["id"] and undo["version"] == 1

    head = state.artifact_head(r1["id"])
    snapshot = RecordStore(state.root / "artifacts", state.root / "records.jsonl",
                           snapshot_dir=state.root / "versions").snapshot_path(
        replace(head, version=1))
    assert snapshot is not None and snapshot.exists() and "versions" in snapshot.parts
    assert snapshot.read_text(encoding="utf-8") == "v1 body"
    assert r1["id"] in snapshot.name

    restored = state.undo_last_overwrite()
    assert restored["status"] == "success" and restored["restored_version"] == 1
    assert state.read_artifact(r1["id"])["content"] == "v1 body"


def test_snapshots_are_invisible_to_list_artifacts(state: State) -> None:
    """快照是历史不是产物 —— 列举永远只见 head。"""
    state.save_artifact("survey_report", "lit", "v1", metadata={})
    state.save_artifact("survey_report", "lit", "v2", metadata={})
    ids = [a["id"] for a in state.list_artifacts("survey_report")]
    assert ids == ["survey_report__lit"], ids


# ── 冻结与修订 ──────────────────────────────────────────────────────────────

def test_a_frozen_version_cannot_be_silently_overwritten(state: State) -> None:
    r1 = state.save_artifact("pre_registration", "s", "v1", metadata={})
    _freeze(state, r1["id"])
    with pytest.raises(ValueError) as exc:
        state.save_artifact("pre_registration", "s", "v2", metadata={})
    msg = str(exc.value)
    assert "amendment_reason" in msg, "报错必须给出合法路径，不能只说不许"
    assert "不要换名另存" in msg, "必须堵住换名碎裂那条老路"


def test_amend_keeps_the_frozen_snapshot_and_logs_the_diff(state: State) -> None:
    r1 = state.save_artifact("pre_registration", "s", "H1 threshold 0.5",
                              metadata={"expected_params": {"n": 3}})
    path = _freeze(state, r1["id"])
    frozen_bytes = path.read_bytes()

    r2 = state.save_artifact("pre_registration", "s", "H1 threshold 0.7",
                              metadata={"expected_params": {"n": 7}},
                              amendment_reason="review 指出阈值缺乏依据")
    assert r2["version"] == 2

    head = state.read_artifact(r1["id"])
    assert not (head["metadata"] or {}).get("frozen"), "修订稿必须是未冻结草稿"
    assert head["amendment"]["from_version"] == 1
    assert head["amendment"]["reason"] == "review 指出阈值缺乏依据"
    assert path.read_text(encoding="utf-8") == "H1 threshold 0.7", "路径即身份：修订稿落同一路径"

    snap = state.latest_frozen_artifact(r1["id"])
    assert snap is not None and snap["version"] == 1
    assert snap["metadata"]["frozen"] is True, "冻结版必须原样保真"
    assert snap["content"].encode("utf-8") == frozen_bytes

    rows = [json.loads(line) for line in
            (state.root / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    amend = [r for r in rows if r.get("event") == "save" and r.get("amendment")]
    assert amend, "修订必须入账本"
    diff = amend[0]["amendment"]["diff"]
    assert diff["content_changed"] is True
    assert "expected_params" in diff["changed_metadata_keys"], \
        "差异是框架算的，不靠自觉申报"
    assert amend[0].get("prev_row_sha256"), "账本行间要有哈希链"


def test_the_gate_pin_moves_from_head_to_the_snapshot(bound: State) -> None:
    """amend 之后闸解除对 head 的钉死 —— 冻结的字节留在历史里（git / 快照），
    而修订稿是可改的草稿。"""
    r1 = bound.save_artifact("pre_registration", "s", "v1", metadata={})
    path = _freeze(bound, r1["id"])
    store = workspace_store(bound.project_worktree)
    relative = path.relative_to(bound.project_worktree).as_posix()

    pinned = store.pinned()
    assert pinned.get(relative) == bound.artifact_head(r1["id"]).sha256, "冻结后 head 被钉死"

    bound.save_artifact("pre_registration", "s", "v2", metadata={},
                        amendment_reason="修订")
    assert relative not in store.pinned(), "amend 后 head 解除钉死"
    frozen = bound.latest_frozen_artifact(r1["id"])
    assert frozen["version"] == 1 and frozen["content"] == "v1", "冻结版仍完整可读"
    assert frozen["content_hash"] == pinned[relative], "冻结的字节仍按哈希可核"


def test_latest_frozen_survives_a_pending_amendment(state: State) -> None:
    r1 = state.save_artifact("pre_registration", "s", "v1", metadata={})
    _freeze(state, r1["id"])
    state.save_artifact("pre_registration", "s", "v2 draft", metadata={},
                        amendment_reason="修订中")
    frozen = state.latest_frozen_artifact(r1["id"])
    assert frozen is not None and frozen["version"] == 1
    assert frozen["content"] == "v1"


def test_refreezing_the_amended_version_pins_the_new_head(bound: State) -> None:
    """修订 → 重新冻结：v2 成为现行承诺，v1 仍是被钉死的历史。"""
    r1 = bound.save_artifact("pre_registration", "s", "v1", metadata={})
    path = _freeze(bound, r1["id"])
    bound.save_artifact("pre_registration", "s", "v2", metadata={},
                        amendment_reason="修订")
    _freeze(bound, r1["id"])

    frozen = bound.latest_frozen_artifact(r1["id"])
    assert frozen["version"] == 2
    store = workspace_store(bound.project_worktree)
    relative = path.relative_to(bound.project_worktree).as_posix()
    assert store.pinned().get(relative) == frozen["content_hash"], "v2 冻结后 head 重新钉死"
    v1 = bound.artifact_versions(r1["id"])[0]
    assert v1["version"] == 1 and v1["content"] == "v1", "v1 仍完整在历史里"
    rows = [json.loads(line) for line in
            (bound.project_worktree / LEDGER_RELATIVE).read_text(encoding="utf-8").splitlines()]
    freezes = {int(r["version"]): r["sha256"] for r in rows
               if r.get("event") == "freeze" and r.get("id") == r1["id"]}
    assert freezes == {1: v1["content_hash"], 2: frozen["content_hash"]}, \
        "两次冻结都在账本上：v1 的冻结记录不因 v2 冻结而消失"


# ── research_state 唯一读取实现（RFC 2026-08-18 的收口）────────────────────

def test_the_reader_hands_back_the_source_path(tmp_path: Path) -> None:
    """记录和它的来源路径一起给出 —— 消费方不该自己再拼一次。

    收口时这里只回了 record，而简报那处仍按 `(version, path)` 用 `.relative_to`
    → AttributeError → 被外层 `except Exception: pass` 吞掉 → 整段"研究状态"
    从注入里静默消失。返回值形状是契约的一部分，钉住它。
    """
    from core import research_state_reader as rs

    write_record(tmp_path, artifact_type="research_state", name="research_state",
                 content="# RS v1", directory="plan",
                 metadata={"version": 1, "verdict": "continue"},
                 produced_by_node_type="hypothesis", produced_by_run_id="r-hyp",
                 created_at="2026-08-18T00:00:00+00:00")

    found = rs.latest_located(tmp_path)
    assert found is not None
    version, record, source = found
    assert version == 1
    assert record["metadata"]["verdict"] == "continue"
    assert source.is_file() and source.relative_to(tmp_path)


def test_an_unmigrated_workspace_is_loud_not_silent(tmp_path: Path) -> None:
    """旧式信封的碎片 = 这个项目是原语上线前建的。**必须吵**。

    静默的代价是错答案：义务账本会认为 Analysis 从没跑过、简报里整段研究状态
    消失，而没有任何东西报错。报错文案要带可直接执行的迁移命令。
    """
    import json as _json

    from core.research_state_reader import UnmigratedWorkspaceError, latest_located

    art = tmp_path / "plan" / "artifacts"
    art.mkdir(parents=True)
    (art / "research_state__v1.json").write_text(_json.dumps({
        "type": "research_state", "name": "v1",
        "metadata": {"version": 1, "verdict": "continue"},
    }, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(UnmigratedWorkspaceError) as exc:
        latest_located(tmp_path)
    message = str(exc.value)
    assert "research_state__v1.json" in message, "要指名是哪些文件"
    assert "migrate-records" in message
    assert "不要删除" in message


def test_a_migrated_workspace_reads_head_and_snapshots(tmp_path: Path) -> None:
    """新布局的形状：head 是当前版本（原生文件），历史版本在账本里。"""
    from core import research_state_reader as rs

    for version, verdict in ((1, "legacy"), (2, "continue")):
        write_record(tmp_path, artifact_type="research_state", name="research_state",
                     content=f"# RS v{version}", directory="plan",
                     metadata={"version": version, "verdict": verdict},
                     produced_by_node_type="hypothesis", produced_by_run_id="r-hyp")

    version, record, source = rs.latest_located(tmp_path)
    assert version == 2 and record["metadata"]["verdict"] == "continue"
    assert source.name == "research_state__research_state.md"
    assert source.read_text(encoding="utf-8") == "# RS v2"
    assert [rs.version_of(r) for r in rs.iter_records(tmp_path)] == [1, 2]

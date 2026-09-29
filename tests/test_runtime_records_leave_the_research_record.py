"""运行时记录随 run 生灭，不进研究记录。

一个真项目（2026-09-12 本机 Ising 课题）：各节点 artifacts/ 里 180 份信封，
89 份是 compression_log，外加 53 份 .versions 快照 —— 全进了 git、目录页、
收尾清单，还让派发闸的上游指纹每压缩一次就变一次。这些东西没有任何后续 run
读它们（逐类型 grep 核过），存在的意义只是事后审计这一个 run。

写入口按策略表的 `run_local` 一位路由：这类记录落 run 自己的目录
（`.research/runtime/runs/<run_id>/artifacts/`，gitignore 的），研究记录里
从此没有它们。这里绑的是**真 git 工作区**，按平台的目录形状来。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from core import catalog, dispatch_gate
from core.state import State
from shared.lib import artifact_policy

ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _worktree(tmp_path: Path) -> Path:
    """平台建出来的工作区形状：节点目录 + `.research/runtime/` 已 gitignore。"""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    (root / ".gitignore").write_text(".research/runtime/\n", encoding="utf-8")
    for directory in ("literature", "hypothesis", "experiment", "writing"):
        (root / directory).mkdir()
        (root / directory / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-q", "-m", "Initialize Project")
    return root


def _bound_state(root: Path, node_type: str = "experiment") -> State:
    # 平台把 run 目录放在工作区里的 `.research/runtime/runs/`（harness_sessions）。
    return State.new(
        node_type, root / ".research" / "runtime" / "runs",
        project_id="p1", project_worktree=root,
    )


def test_a_compression_log_lands_in_the_run_directory_not_the_record(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    state = _bound_state(root)

    saved = state.save_artifact("compression_log", "compression_turn_3", "压缩摘要")

    path = state.find_artifact_path(saved["id"])
    assert path is not None
    assert path.is_relative_to(state.root / "artifacts"), path
    assert not list((root / "experiments").glob("compression_log__*"))
    # 对 git 来说什么都没发生：它在 gitignore 的运行时目录里。
    assert _git(root, "status", "--porcelain") == ""


def test_a_real_artifact_still_lands_in_the_node_directory(tmp_path: Path) -> None:
    """对照：研究记录照旧进节点目录、照旧被 git 看见。"""
    root = _worktree(tmp_path)
    state = _bound_state(root, "hypothesis")

    state.save_artifact("pre_registration", "Q1", "# 预注册")

    assert (root / "plan" / "pre_registration__Q1.md").is_file()
    # -uall：不加的话 git 把整个未跟踪目录折叠成一行 `?? plan/`。
    assert "plan/pre_registration__Q1.md" in _git(root, "status", "--porcelain", "-uall")


def test_runtime_records_are_only_visible_when_asked_for_by_type(tmp_path: Path) -> None:
    """`list_artifacts()` 是"产出了什么"；运行时记录不是产出。指名要才给。"""
    root = _worktree(tmp_path)
    state = _bound_state(root)
    state.save_artifact("compression_log", "compression_turn_3", "压缩摘要")
    state.save_artifact("experiment_log", "run1", "# 实验记录")

    assert [a["type"] for a in state.list_artifacts()] == ["experiment_log"]
    assert [a["type"] for a in state.list_artifacts(own_only=True)] == ["experiment_log"]
    [entry] = state.list_artifacts("compression_log")
    assert entry["id"] == "compression_log__compression_turn_3"
    assert entry["owner_node"] == "experiment", \
        "归属来自账本的产出方，不从目录反推（运行时目录的祖父是 run_id，不是节点）"
    assert state.read_artifact("compression_log__compression_turn_3")["content"] == "压缩摘要"


def test_the_catalog_and_the_dispatch_fingerprint_do_not_see_runtime_records(tmp_path: Path) -> None:
    """目录页列的是研究记录；派发闸的上游指纹只该随上游**产出**变。

    此前每次上下文压缩都往 experiment/artifacts/ 落一份 → 指纹变了 →
    "上游没变就别重派"这道闸对压缩失效。
    """
    root = _worktree(tmp_path)
    state = _bound_state(root)
    state.save_artifact("experiment_log", "run1", "# 实验记录")
    before = dispatch_gate.upstream_fingerprint(root, "hypothesis")

    state.save_artifact("compression_log", "compression_turn_3", "压缩摘要")
    state.save_artifact("resource_profile", "probe", "{}")

    assert dispatch_gate.upstream_fingerprint(root, "hypothesis") == before
    assert {e.kind for e in catalog.build(root)} == {"experiment_log"}

    # 对照：真产出变了，指纹要变。
    state.save_artifact("experiment_log", "run2", "# 第二份")
    assert dispatch_gate.upstream_fingerprint(root, "hypothesis") != before


def test_undo_restores_a_runtime_record_where_it_lives(tmp_path: Path) -> None:
    """/undo 写回的位置由这个身份自己的账本决定，不按节点目录拼。

    否则对运行时记录（落 run 目录）撤销，会把旧版写进节点目录。
    """
    import chat as chat_mod

    root = _worktree(tmp_path)
    state = _bound_state(root)
    state.save_artifact("compression_log", "compression_turn_3", "第一版")
    state.save_artifact("compression_log", "compression_turn_3", "第二版")

    message = chat_mod._cmd_undo(state)

    assert "v1" in message, message
    assert state.read_artifact("compression_log__compression_turn_3")["content"] == "第一版"
    restored = state.find_artifact_path("compression_log__compression_turn_3")
    assert restored.is_relative_to(state.root / "artifacts"), restored
    assert not list((root / "experiments").glob("compression_log__*"))


# ── 策略表本身 ────────────────────────────────────────────────────────────

def test_run_local_implies_framework_internal() -> None:
    """运行时记录不可能是节点的交付物。"""
    for artifact_type in artifact_policy.run_local_types():
        assert artifact_policy.is_framework_internal(artifact_type), artifact_type


def test_every_run_local_type_has_a_writer() -> None:
    """策略表只登记真会落盘的类型。

    `resource_recommendation` 曾登记为 framework_internal 而全仓没有写者 ——
    一条谁也不会触发的策略，除了让人以为"有这么个东西"之外没有任何作用。
    """
    sources: list[str] = []
    for top in ("core", "shared", "nodes"):
        for path in (ROOT / top).rglob("*.py"):
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            sources.append(path.read_text(encoding="utf-8", errors="replace"))
    corpus = "\n".join(sources)
    for artifact_type in artifact_policy.run_local_types():
        pattern = re.compile(r"[\"']" + re.escape(artifact_type) + r"[\"']")
        assert pattern.search(corpus), f"{artifact_type} 在策略表里，但仓里没有任何写者"


def test_run_manifest_stays_in_the_record_because_a_later_run_reads_it() -> None:
    """framework_internal 答的是"谁写的"，run_local 答的是"要不要活过这个 run"。

    run_manifest 两问答案不同：框架在节点背后写它，但 core/obligations 按它判
    requires_hypothesis_verdict —— 它必须留在研究记录（工作区账本）里。
    """
    obligations = (ROOT / "core" / "obligations.py").read_text(encoding="utf-8")
    # 读者按类型从账本取全部 head（每个 run 一份身份），不 glob 文件名。
    assert 'iter_heads(worktree, artifact_type="run_manifest")' in obligations
    assert 'glob("run_manifest' not in obligations
    assert artifact_policy.is_framework_internal("run_manifest")
    assert not artifact_policy.is_run_local("run_manifest")
    # 早已删除的 .history 不该再被任何读者 glob（注释里提一嘴不算）。
    assert '".history"' not in obligations

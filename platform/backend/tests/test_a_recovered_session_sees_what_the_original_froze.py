"""恢复出来的会话看得到原会话已冻结的产物（#1186）。

## 现场

运行时丢了之后开的接续会话，看不到原会话已经冻结的产物（现场是 Q1 的预注册）。
08-10 起接续会话从原会话的分支头开出来，正文文件是带过来了 —— 但冻结的事实记在
工作区账本 `.research/ledger/records.jsonl` 里，而这本账**从不进会话分支**
（checkpoint 只提交节点目录）。新会话的 State 只读自己 worktree 的账，于是那份
预注册在它眼里只是一个文件：不知道是谁产的，也不知道已冻结。

## 裁决（wangd 2026-10-21）

从原会话分支头续接；续接前核一遍分支头与账本是否一致（worker 被杀后可能半截），
不一致就退回「从 base 新开，并把原会话已冻结的产物显式带过来」。

## 这里钉住的四件事（真 git 仓，tmp 里）

a. 分支头与账本一致 → 从分支头接，账本跟过来，新会话认得冻结件（commit 闸也认）。
b. 分支头缺一件账本钉死的冻结件 → 从原会话的 base 开，冻结件连账带过来，并记下为什么。
c. 原会话没有分支头 → 起点是**原会话的 base**，不是项目当前的 main。
d. 哪儿都找不回的冻结件 → 记成 unrecoverable，说出来（manifest / 提交 / 事件流），不悄悄少一件。

"新会话看得到"按新会话 State 读的那本账判：`workspace_store(新 worktree)` 正是
`State._worktree_store()` 返回的东西。
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.models.execution import ExecutionEvent, Run, SessionProjection
from app.models.project import Project
from app.models.user import User
from app.services import sessions as sessions_service
from app.services.harness_contract import ledger_module
from app.services.harness_sessions import HarnessSessionStaleError
from app.services.project_repository import (
    GitProjectRepository,
    RecoveryBaselineMissing,
    get_project_repository,
    run_in_repository_thread,
)

PROJECT = "p-recover"
SOURCE = "s-source"
CONTINUED = "s-continued"


def _repository(tmp_path: Path) -> GitProjectRepository:
    return GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")


def _source_session(repository: GitProjectRepository):
    project = repository.initialize_project(
        project_id=PROJECT, name="Recover", description=None,
        research_domain=None, owner_id="owner",
    )
    source = repository.ensure_session_workspace(
        project_id=PROJECT, session_id=SOURCE, base_commit=project.head_commit,
        title="source", created_by="owner",
    )
    return project, source


def _write(root: Path, name: str, body: str, *, frozen: bool = True) -> tuple[str, str]:
    """hypothesis 落一份预注册（原生文件 + 账本 save 行，`frozen` 再追一行 freeze）。"""
    ledger = ledger_module()
    record = ledger.write_record(
        root, artifact_type="pre_registration", name=name, content=body,
        directory="plan", produced_by_node_type="hypothesis", produced_by_run_id="run-src",
        frozen=frozen,
    )
    return record["id"], ledger.workspace_store(root).head(record["id"]).path


def _checkpoint(repository: GitProjectRepository, *, expected: str) -> str:
    checkpoint = repository.checkpoint_session_workspace(
        project_id=PROJECT, session_id=SOURCE, node_type="hypothesis", run_id="run-src",
        run_status="incomplete", workspace_prefix="plan", paths=["plan"],
        expected_head_commit=expected,
    )
    assert checkpoint.commit_sha
    return checkpoint.commit_sha


def _recover(repository: GitProjectRepository, source, *, head_hint: str | None):
    return repository.recover_session_workspace(
        project_id=PROJECT, source_session_id=SOURCE,
        source_base_commit=source.base_commit, source_head_commit=head_hint,
        session_id=CONTINUED, title="continued", created_by="owner",
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest_recovery(root: Path) -> dict:
    manifest = root / f".research/orchestration/sessions/{CONTINUED}.yaml"
    return yaml.safe_load(manifest.read_text(encoding="utf-8"))["recovery"]


def test_a_consistent_head_is_continued_and_the_continuation_knows_what_is_frozen(
    tmp_path: Path,
) -> None:
    """(a) 冻结件都在分支头上、哈希对得上 → 从分支头接；账本跟过来，新会话认得它冻结了。"""
    repository = _repository(tmp_path)
    _, source = _source_session(repository)
    source_root = Path(source.path)
    q1, rel = _write(source_root, "Q1", "# Q1 预注册\n")
    head = _checkpoint(repository, expected=source.head_commit)
    source_ledger = (source_root / ".research/ledger/records.jsonl").read_bytes()

    recovery = _recover(repository, source, head_hint=head)

    assert recovery.record["startedFrom"] == "source_head"
    assert recovery.record["why"] == "head_matches_ledger"
    assert recovery.workspace.base_commit == head
    assert recovery.record["frozen"] == [{
        "id": q1, "version": 1, "path": rel, "sha256": _sha("# Q1 预注册\n"),
        "outcome": "on_head",
    }]
    root = Path(recovery.workspace.path)
    continued = ledger_module().workspace_store(root)
    assert continued.head(q1).frozen, "接续会话的账里，Q1 必须是冻结的"
    record = continued.record(q1)
    assert record["content"] == "# Q1 预注册\n"
    assert record["produced_by_run_id"] == "run-src", "出处是原产出方，不是恢复动作"
    # 账是同一份工作的账：原样接过来，链不断。
    assert (root / ".research/ledger/records.jsonl").read_bytes() == source_ledger
    assert _manifest_recovery(root) == recovery.record

    # 「认得它冻结了」不只是读得到：新会话的 commit 闸也按这本账执法。
    (root / rel).write_text("# 接续会话悄悄改了预注册\n", encoding="utf-8")
    tampered = repository.checkpoint_session_workspace(
        project_id=PROJECT, session_id=CONTINUED, node_type="hypothesis", run_id="run-next",
        run_status="completed", workspace_prefix="plan", paths=["plan"],
        expected_head_commit=recovery.workspace.head_commit,
    )
    assert tampered.frozen_violations == ((rel, "restored"),)
    assert (root / rel).read_text(encoding="utf-8") == "# Q1 预注册\n"

    # 源分支是证据：一个字节不动。
    assert repository._git(source_root, "rev-parse", "HEAD") == head
    assert (source_root / ".research/ledger/records.jsonl").read_bytes() == source_ledger


def test_a_head_missing_a_frozen_artifact_falls_back_to_the_base_and_carries_it_over(
    tmp_path: Path,
) -> None:
    """(b) worker 冻结了 Q2、没等到 checkpoint 就被杀 —— 分支头对不上账本。

    退回原会话的 base；Q1、Q2 两件冻结件连账带过来，记录里写清楚为什么、从哪儿找回的。
    分支头上没冻结的东西不跟过来（裁决的代价），但它留在原会话分支上。
    """
    repository = _repository(tmp_path)
    _, source = _source_session(repository)
    source_root = Path(source.path)
    q1, rel1 = _write(source_root, "Q1", "# Q1\n")
    (source_root / "plan" / "scratch.md").write_text("没冻结的草稿\n", encoding="utf-8")
    head = _checkpoint(repository, expected=source.head_commit)
    q2, rel2 = _write(source_root, "Q2", "# Q2 被杀前刚冻结\n")   # 没有 checkpoint

    recovery = _recover(repository, source, head_hint=head)

    record = recovery.record
    assert record["startedFrom"] == "source_base"
    assert record["why"] == "head_disagrees_with_ledger"
    assert record["sourceHead"] == head
    assert recovery.workspace.base_commit == source.base_commit
    assert record["disagreements"] == [{
        "id": q2, "version": 1, "path": rel2, "sha256": _sha("# Q2 被杀前刚冻结\n"),
        "onHead": "missing",
    }]
    outcomes = {item["id"]: item for item in record["frozen"]}
    assert {item["outcome"] for item in outcomes.values()} == {"carried_over"}
    assert outcomes[q2]["from"] == "source_worktree"

    root = Path(recovery.workspace.path)
    continued = ledger_module().workspace_store(root)
    for artifact_id, body in ((q1, "# Q1\n"), (q2, "# Q2 被杀前刚冻结\n")):
        assert continued.head(artifact_id).frozen, f"{artifact_id} 带过来了却不是冻结的"
        assert continued.record(artifact_id)["content"] == body
        assert continued.record(artifact_id)["produced_by_node_type"] == "hypothesis"
    stamped = [row for row in continued.rows() if row.get("id") in {q1, q2}]
    assert stamped and all(row.get("recovered_from_session") == SOURCE for row in stamped), (
        "搬过来的账要说清楚它是从哪个会话搬来的"
    )
    assert not (root / "plan" / "scratch.md").exists()
    assert repository._git(repository.project_path(PROJECT), "show",
                           f"{head}:plan/scratch.md") == "没冻结的草稿"

    # 冻结件进了接续会话分支的提交（不是躺在工作区里等下一次 checkpoint 认领）。
    committed = repository._git(root, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert {rel1, rel2, f".research/orchestration/sessions/{CONTINUED}.yaml"} <= set(committed)
    message = repository._git(root, "log", "-1", "--format=%B")
    assert f"Recovered-From-Session: {SOURCE}" in message
    assert f"Session-ID: {CONTINUED}" in message, "没有它，下一次 checkpoint 会把这条提交判成越权"
    assert f"Frozen-Carried-Over: {q2}@v1 from source_worktree" in message
    assert _manifest_recovery(root) == record
    # 账本照例是未跟踪文件（和任何会话一样）；跟踪着的东西不许一开张就是脏的。
    assert repository._git(root, "status", "--porcelain", "--untracked-files=no") == "", (
        "接续会话一开张就带着没提交的跟踪改动"
    )


def test_a_frozen_version_newer_than_the_head_counts_as_a_disagreement(tmp_path: Path) -> None:
    """分支头上有这条路径、但字节是账本钉死之前的旧版 —— 同样是对不上。"""
    repository = _repository(tmp_path)
    _, source = _source_session(repository)
    source_root = Path(source.path)
    q1, rel = _write(source_root, "Q1", "# Q1 草稿\n", frozen=False)
    head = _checkpoint(repository, expected=source.head_commit)
    ledger = ledger_module()
    _write(source_root, "Q1", "# Q1 定稿\n", frozen=False)              # v2，没 checkpoint
    ledger.workspace_store(source_root).freeze(
        q1, metadata_patch={}, by_node="hypothesis", by_run="run-src",
    )

    recovery = _recover(repository, source, head_hint=head)

    assert recovery.record["startedFrom"] == "source_base"
    assert recovery.record["disagreements"][0]["onHead"] == "differs"
    root = Path(recovery.workspace.path)
    head_v2 = ledger.workspace_store(root).head(q1)
    assert (head_v2.version, head_v2.frozen) == (2, True)
    assert (root / rel).read_text(encoding="utf-8") == "# Q1 定稿\n"


def test_without_a_source_head_the_continuation_starts_from_the_source_base_not_main(
    tmp_path: Path,
) -> None:
    """(c) 原会话没有分支头 → 起点是它自己的 base。main 之后又往前走了，那是别人的工作。"""
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id=PROJECT, name="Recover", description=None,
        research_domain=None, owner_id="owner",
    )
    later_main = repository.commit_main_files(
        project_id=PROJECT, expected_main_commit=project.head_commit,
        files={"PROJECT.md": "# 后来别人改过的项目说明\n"}, message="later work on main",
        operation_id="op-later", actor_id="owner",
    )
    assert later_main != project.head_commit

    recovery = repository.recover_session_workspace(
        project_id=PROJECT, source_session_id=SOURCE,   # 这个会话从没开过工作区
        source_base_commit=project.head_commit, source_head_commit="",
        session_id=CONTINUED, title="continued", created_by="owner",
    )

    assert recovery.record["startedFrom"] == "source_base"
    assert recovery.record["why"] == "no_source_head"
    assert recovery.record["ledger"] == "missing"
    assert recovery.workspace.base_commit == project.head_commit
    root = Path(recovery.workspace.path)
    assert repository._git(root, "merge-base", "--is-ancestor", later_main, "HEAD",
                           check=False) == ""
    assert "后来别人改过" not in (root / "PROJECT.md").read_text(encoding="utf-8")


def test_a_base_that_is_not_in_the_repository_cannot_be_continued(tmp_path: Path) -> None:
    """要退回 base、而 base 在仓库里不存在 —— 说出来，不悄悄换成项目当前的 main。"""
    repository = _repository(tmp_path)
    repository.initialize_project(
        project_id=PROJECT, name="Recover", description=None,
        research_domain=None, owner_id="owner",
    )
    with pytest.raises(RecoveryBaselineMissing):
        repository.recover_session_workspace(
            project_id=PROJECT, source_session_id=SOURCE,
            source_base_commit="0" * 40, source_head_commit=None,
            session_id=CONTINUED, title="continued", created_by="owner",
        )
    assert not repository.session_path(PROJECT, CONTINUED).exists()


def test_a_frozen_artifact_that_survives_nowhere_is_named_not_dropped(tmp_path: Path) -> None:
    """(d) 冻结了、没 checkpoint、盘上的文件也没了 —— 正文哪儿都找不回。

    它照样记在接续会话的账上（承诺还在：要动它得走公开修订），并在 manifest 和
    恢复提交里被点名为 unrecoverable。
    """
    repository = _repository(tmp_path)
    _, source = _source_session(repository)
    source_root = Path(source.path)
    q1, rel = _write(source_root, "Q1", "# 只在盘上活过一会儿\n")
    (source_root / rel).unlink()

    recovery = _recover(repository, source, head_hint=source.head_commit)

    assert recovery.record["startedFrom"] == "source_base"
    lost = [item for item in recovery.record["frozen"] if item["outcome"] == "unrecoverable"]
    assert lost == [{
        "id": q1, "version": 1, "path": rel, "sha256": _sha("# 只在盘上活过一会儿\n"),
        "outcome": "unrecoverable",
    }]
    root = Path(recovery.workspace.path)
    continued = ledger_module().workspace_store(root)
    assert continued.head(q1).frozen, "找不回正文不等于这份承诺不存在"
    assert continued.record(q1) is None, "读不到就是读不到，不许拼出一份空正文"
    assert _manifest_recovery(root)["frozen"] == recovery.record["frozen"]
    message = repository._git(root, "log", "-1", "--format=%B")
    assert f"Frozen-Unrecoverable: {q1}@v1 {_sha('# 只在盘上活过一会儿' + chr(10))}" in message


# ── 走真入口：恢复这件事要留在新会话的事实流里 ─────────────────────────────


async def _people(db):
    user = User(
        id=str(uuid.uuid4()), email=f"u-{uuid.uuid4().hex[:6]}", hashed_password="x",
        display_name="u", role="researcher", institution_id="ieit",
    )
    project = Project(id=str(uuid.uuid4()), name="p", owner_id=user.id)
    db.add_all([user, project])
    await db.flush()
    return user, project


@pytest.mark.asyncio
async def test_the_recovery_is_told_in_the_continuation_event_stream(db_session, monkeypatch):
    """接续会话的事实流里有一条 `session.recovered`：从哪儿接的、冻结件各自落到了哪里。

    找回的、找不回的都要在里面 —— 找不回的那件尤其要让人看见。
    """
    user, project = await _people(db_session)
    repository = get_project_repository()
    status = await run_in_repository_thread(
        repository.initialize_project, project_id=str(project.id), name="p",
        description=None, research_domain=None, owner_id=user.id,
    )
    source_id = str(uuid.uuid4())
    source_ws = await run_in_repository_thread(
        repository.ensure_session_workspace, project_id=str(project.id), session_id=source_id,
        base_commit=status.head_commit, title="s", created_by=user.id,
    )
    source_root = Path(source_ws.path)
    kept, _ = _write(source_root, "Q1", "# 还在盘上\n")
    lost, lost_rel = _write(source_root, "Q2", "# 盘上也没了\n")
    (source_root / lost_rel).unlink()

    db_session.add_all([
        SessionProjection(
            tenant_id="t1", workspace_id="w", project_id=str(project.id), session_id=source_id,
            initiating_user_id=user.id, title="s", created_by_user_id=user.id,
            lifecycle_status="active", git_base_commit_sha=source_ws.base_commit,
            git_head_commit_sha=source_ws.head_commit,
        ),
        Run(
            id=str(uuid.uuid4()), tenant_id="t1", workspace_id="w", project_id=str(project.id),
            session_id=source_id, status="stale_unknown",
            summary={"executionKernel": "formal_harness", "staleReason": "harness_process_gone"},
        ),
    ])
    await db_session.flush()

    async def runtime_is_gone(*args, **kwargs):
        raise HarnessSessionStaleError("harness runtime is gone")

    monkeypatch.setattr(
        "app.services.harness_sessions.assert_conversation_runtime_available", runtime_is_gone
    )

    recovered, _, created = await sessions_service.recover_stale_session(
        db_session, user=user, project=project, source_session_id=source_id
    )

    assert created
    assert recovered.git_base_commit_sha == source_ws.base_commit
    event = await db_session.scalar(
        select(ExecutionEvent).where(
            ExecutionEvent.session_id == recovered.session_id,
            ExecutionEvent.kind == "session.recovered",
        )
    )
    assert event is not None, "恢复做了什么，没在接续会话的事实流里留下"
    assert event.payload["sourceSessionId"] == source_id
    assert event.payload["startedFrom"] == "source_base"
    outcomes = {item["id"]: item["outcome"] for item in event.payload["frozen"]}
    assert outcomes == {kept: "carried_over", lost: "unrecoverable"}

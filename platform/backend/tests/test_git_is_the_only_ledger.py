"""git 是唯一的版本账（RFC X1）。

## 删掉的是什么

`project_revisions / change_sets / change_items / merge_conflicts /
artifact_receipts` 五张表，加 `services/revisions.py`(683)、`api/v1/revisions.py`(503)、
`services/artifact_receipts.py`(324)。它们是一份**独立于 git 的版本账**：
resource_key 级的 manifest、逐条 ChangeItem、resource 级冲突检测、线性 revision_no。

而 `project_repository` 的 manifest 里白纸黑字写着 `authority: git`。两份版本账
必然分叉，且分叉时两边都不报错 —— 已经付过学费（E2E v22：会话分支领先 main 15
个提交、含重画好的四张图，publish 说成功，图没进库，因为 ChangeSet 被 checkpoint
消费掉了而 git 里的提交没人管）。

## 现在的定义

| 问题 | 答案 |
|---|---|
| 这一轮改了什么 | `git diff main...HEAD` |
| 有没有冲突 | 两边都动了同一个文件 |
| 发布 | 把会话路径回放到 main，一次线性提交 |
| 发布过没有 | main 上有没有带那条 trailer 的提交 |
| 证据（收据） | 发布提交上的 `Deliverable-Evidence:` trailer |
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect

from app.database import Base
from app.services.deliverable_publishing import DELIVERY_TRAILER, _publish_message
from app.services.session_changes import SessionChanges, changes_payload
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401
from app.services.project_repository import run_in_repository_thread

GONE = {
    "project_revisions", "change_sets", "change_items",
    "merge_conflicts", "artifact_receipts",
}


def test_the_second_version_account_is_gone() -> None:
    """五张表一张不剩。留一张就会有人再去读它，而没有人再维护它。"""
    assert not (GONE & set(Base.metadata.tables)), GONE & set(Base.metadata.tables)


def test_nothing_points_at_them_anymore() -> None:
    """指向那五张表的两列也走 —— 一个指向不存在的表的外键列，是最安静的那种谎。"""
    sessions = Base.metadata.tables["sessions"]
    projects = Base.metadata.tables["projects"]
    assert "base_revision_id" not in sessions.columns
    assert "current_revision_id" not in projects.columns


def test_the_delivery_message_carries_its_evidence() -> None:
    """收据跟着**它证明的那次提交**走。

    从前它们是 `artifact_receipts` 表里的行，而 `write_receipt` 早就同时写进
    git 了 —— 表是投影。跟着提交走比跟着表走更难失散：仓库在，证据就在。
    """
    message = _publish_message(
        "Continuous checkpoint for Run r1",
        {"report:结论": ["writing_validation", "review_approval"]},
    )
    lines = message.splitlines()
    assert lines[0] == "Continuous checkpoint for Run r1", "第一行仍然是给人看的"
    assert "Deliverable-Evidence: review_approval report:结论" in lines
    assert "Deliverable-Evidence: writing_validation report:结论" in lines


def test_no_evidence_means_no_trailers() -> None:
    """没有证据就别写一条空 trailer —— 一条内容为空的证据比没有证据更坏。"""
    assert _publish_message("just a checkpoint", {}) == "just a checkpoint"


@pytest.mark.asyncio
async def test_the_payload_the_ui_reads_is_all_from_git() -> None:
    """界面读的每一项都是 git 派生的。

    这是删掉那张表**没有代价**的原因：ChangeSet 行提供的 items / baseRevisionId /
    publishedRevisionId，界面一个都没在看。
    """
    class _Session:
        session_id = "s1"
        git_branch = None
        git_base_commit_sha = None

    payload = await changes_payload("p1", _Session())
    assert set(payload) == {
        "sessionId", "projectId", "gitBranch", "gitBaseCommitSha", "gitHeadCommitSha",
        "aheadBy", "behindBy", "patch", "additions", "deletions", "filesChanged",
        "worktreeClean", "patchTruncated", "changedPaths", "conflicts",
    }


def test_a_conflict_is_a_file_both_sides_touched() -> None:
    changes = SessionChanges(
        branch="session/x", base_commit="a" * 40, head_commit="b" * 40,
        ahead_by=2, behind_by=1, worktree_clean=True,
        changed_paths=("docs/report.md", "data/x.csv"),
        conflicting_paths=("docs/report.md",),
    )
    assert changes.files_changed == 2, "改了多少文件 = 名单长度，不读任何内容"
    assert changes.has_conflicts
    assert changes.conflicting_paths == ("docs/report.md",)


@pytest.mark.asyncio
async def test_the_session_change_endpoint_answers_from_git(runtime_client) -> None:
    client, _factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    session_id = (await client.post(
        f"/api/v1/projects/{project_id}/sessions", headers=_headers(token),
        json={"title": "git 是账本"},
    )).json()["id"]

    changes = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/change-set",
        headers=_headers(token),
    )
    assert changes.status_code == 200, changes.text
    body = changes.json()
    assert body["gitBranch"] == f"session/{session_id}"
    assert len(body["gitHeadCommitSha"]) == 40
    assert body["changedPaths"] == [], "刚建的会话相对 main 没有内容改动"
    assert body["conflicts"] == []

    conflicts = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/conflicts",
        headers=_headers(token),
    )
    assert conflicts.status_code == 200 and conflicts.json() == []


@pytest.mark.asyncio
async def test_publishing_nothing_is_a_409_not_an_empty_commit(runtime_client) -> None:
    """没有改动就没有可发布的东西。造一个空提交只会让历史里多一行噪音。"""
    client, _factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    session_id = (await client.post(
        f"/api/v1/projects/{project_id}/sessions", headers=_headers(token),
        json={"title": "空发布"},
    )).json()["id"]

    published = await client.post(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/publish",
        headers=_headers(token), json={"message": "nothing to say"},
    )
    assert published.status_code == 409, published.text


@pytest.mark.asyncio
async def test_resolving_with_the_project_version_actually_clears_the_conflict(
    runtime_client, tmp_path,
) -> None:
    """「用项目那一版」之后，冲突必须真的没了。

    2026-09-05 真机点验抓到的：冲突只按「两边都动过」判（三点 diff，相对分叉
    点），取回 main 那一版之后文件**仍然**出现在三点 diff 里 —— 它相对分叉点
    确实变了，但它和 main 已经一模一样。于是那条冲突永远解不掉，发布永远被拦。

    冲突的定义因此要带上后半句：两边都动过 **且此刻内容仍然不一样**。
    """
    from pathlib import Path

    from app.services.project_repository import get_project_repository

    client, _factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    session_id = (await client.post(
        f"/api/v1/projects/{project_id}/sessions", headers=_headers(token),
        json={"title": "冲突解决"},
    )).json()["id"]

    repo = get_project_repository()
    worktree = Path((await run_in_repository_thread(repo.session_status, project_id, session_id)).path)
    canonical = repo.project_path(project_id)

    (worktree / "SHARED.md").write_text("会话这边\n", encoding="utf-8")
    await run_in_repository_thread(repo._git, worktree, "add", "SHARED.md")
    (await run_in_repository_thread(repo._configure_identity, worktree))
    await run_in_repository_thread(repo._git, worktree, "commit", "-m", "session edit")

    (canonical / "SHARED.md").write_text("main 那边\n", encoding="utf-8")
    await run_in_repository_thread(repo._git, canonical, "add", "SHARED.md")
    (await run_in_repository_thread(repo._configure_identity, canonical))
    await run_in_repository_thread(repo._git, canonical, "commit", "-m", "main edit")

    conflicts = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/conflicts", headers=_headers(token)
    )
    assert [item["path"] for item in conflicts.json()] == ["SHARED.md"]

    resolved = await client.post(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/conflicts/resolve",
        headers=_headers(token), json={"path": "SHARED.md", "choice": "use_project"},
    )
    assert resolved.status_code == 200, resolved.text

    after = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/conflicts", headers=_headers(token)
    )
    assert after.json() == [], "解决完冲突还在 —— 那条冲突就永远解不掉"

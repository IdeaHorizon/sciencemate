"""会话改了什么、怎么发布 —— **答案全部来自 git**。

## 删掉的是什么（RFC X1）

`project_revisions / change_sets / change_items / merge_conflicts / artifact_receipts`
五张表，加 `services/revisions.py`(683) + `api/v1/revisions.py`(503) +
`services/artifact_receipts.py`(324)。它们是一份**独立于 git 的版本账**：
resource_key 级的 manifest、逐条 ChangeItem、resource 级的冲突检测、线性的
revision_no。

而 `project_repository` 的 manifest 里白纸黑字写着 `authority: git`。两份版本账
必然分叉，且分叉时两边都不报错 —— 已经付过学费：E2E v22 实测「分支领先 main
15 个 commit（含重画好的四张图），publish 说成功，图没进库」，因为 ChangeSet
被 checkpoint 消费掉了，而 git 里的提交没人管。

## 现在的定义

- **这一轮改了什么** = `git diff main...HEAD`（`repo.changed_paths` / `repo.diff`）
- **冲突** = 两边都动了同一个文件（会话这边改的 ∩ main 上后来改的）
- **发布** = 把会话的路径回放到 main 上，一次线性提交（`repo.publish_linear`）
- **发布过没有** = main 上有没有带那条 trailer 的提交（`repo.commit_with_trailer`）

界面上「main · r3」那个自增号也随之而去：它是第二份版本账的产物。取代它的是
main 的短 sha —— 一个**能拿去 `git show` 的**标识，而不是只在这张表里有意义的
序号。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact, ArtifactType, ArtifactVersion
from app.models.execution import SessionProjection
from app.models.project import Project
from app.models.user import User
from app.policies import has_project_capability

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionChanges:
    """一个会话相对 main 的**事实**。全部现算，没有一项存在表里，也没有一项读文件内容。

    补丁正文不在这里：它与工作区体量成正比，而这份事实挂在会话列表 5 秒一次的
    轮询上。2026-09-09 node20：一个把日志 tar 解成 9048 个文件的会话，这份"事实"
    每次要生成 1.83GB 的补丁再截到 200KB，35 秒一次，整台后端以此为量子停摆。
    要看补丁走 `read_session_patch`，按预算出。
    """

    branch: str | None
    base_commit: str | None
    head_commit: str | None
    ahead_by: int
    behind_by: int
    worktree_clean: bool
    changed_paths: tuple[str, ...]
    conflicting_paths: tuple[str, ...]

    @property
    def files_changed(self) -> int:
        return len(self.changed_paths)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicting_paths)


@dataclass(frozen=True, slots=True)
class SessionPatch:
    """给人看的补丁正文 —— 按预算读出来的那一段，计数只描述这一段。"""

    patch: str
    additions: int
    deletions: int
    files_in_patch: int
    truncated: bool


def _empty() -> SessionChanges:
    """工作区还没建起来（或已不可达）时的答案。

    返回"什么都没有"而不是抛：会话页要能打开。真的要发布时，publish 那条路
    自己会撞上并说清楚。
    """
    return SessionChanges(
        branch=None, base_commit=None, head_commit=None, ahead_by=0, behind_by=0,
        worktree_clean=True, changed_paths=(), conflicting_paths=(),
    )


def _read_session_changes(project_id: str, session: SessionProjection) -> SessionChanges:
    """同步实现 —— 只在仓库线程里跑（`_git` 在事件循环线程上会拒绝）。"""
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    if not session.git_branch:
        return _empty()
    repository = get_project_repository()
    try:
        workspace = repository.session_status(
            project_id, session.session_id, base_commit=session.git_base_commit_sha
        )
        ours = repository.changed_paths(project_id, session.session_id)
        theirs = repository.paths_changed_on_main_since(project_id, session.session_id)
        differing = set(repository.paths_differing_from_main(project_id, session.session_id))
    except ProjectRepositoryError as exc:
        logger.warning("cannot read session changes for %s: %s", session.session_id, exc)
        return _empty()
    return SessionChanges(
        branch=workspace.branch,
        base_commit=workspace.base_commit,
        head_commit=workspace.head_commit,
        ahead_by=workspace.ahead_by,
        behind_by=workspace.behind_by,
        worktree_clean=workspace.clean,
        changed_paths=tuple(ours),
        conflicting_paths=tuple(sorted(set(ours) & set(theirs) & differing)),
    )


async def read_session_changes(project_id: str, session: SessionProjection) -> SessionChanges:
    from app.services.project_repository import run_in_repository_thread

    return await run_in_repository_thread(_read_session_changes, project_id, session)


def _read_session_patch(project_id: str, session: SessionProjection) -> SessionPatch:
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )
    from app.services.redaction import DEFAULT_REDACTION_POLICY, MAX_LONG_TEXT_LENGTH

    if not session.git_branch:
        return SessionPatch("", 0, 0, 0, False)
    try:
        diff = get_project_repository().diff(project_id=project_id, session_id=session.session_id)
    except ProjectRepositoryError as exc:
        logger.warning("cannot read session patch for %s: %s", session.session_id, exc)
        return SessionPatch("", 0, 0, 0, False)
    redacted = DEFAULT_REDACTION_POLICY.sanitize(diff.patch, max_string_length=MAX_LONG_TEXT_LENGTH)
    return SessionPatch(
        patch=str(redacted.value),
        additions=diff.additions,
        deletions=diff.deletions,
        files_in_patch=diff.files_changed,
        truncated=diff.truncated,
    )


async def read_session_patch(project_id: str, session: SessionProjection) -> SessionPatch:
    from app.services.project_repository import run_in_repository_thread

    return await run_in_repository_thread(_read_session_patch, project_id, session)


async def changes_payload(project_id: str, session: SessionProjection) -> dict:
    """给前端的形状。字段名与从前的 ChangeSetOut 一致 —— 界面读的本来就全是
    git 派生的那几项（patch / ahead / behind / additions …）。这是**人打开
    改动面板**才走的路，补丁按预算出；会话列表和详情只要事实。"""
    changes = await read_session_changes(project_id, session)
    patch = await read_session_patch(project_id, session)
    return {
        "sessionId": session.session_id,
        "projectId": project_id,
        "gitBranch": changes.branch,
        "gitBaseCommitSha": changes.base_commit,
        "gitHeadCommitSha": changes.head_commit,
        "aheadBy": changes.ahead_by,
        "behindBy": changes.behind_by,
        "patch": patch.patch,
        "additions": patch.additions,
        "deletions": patch.deletions,
        "filesChanged": changes.files_changed,
        "worktreeClean": changes.worktree_clean,
        "patchTruncated": patch.truncated,
        "changedPaths": list(changes.changed_paths),
        "conflicts": [
            {"path": path, "status": "open"} for path in changes.conflicting_paths
        ],
    }


class PublishConflictsFoundError(RuntimeError):
    """两边动了同一个文件 —— 先决定用谁那一版。"""


async def publish_session(
    db: AsyncSession,
    *,
    project: Project,
    session: SessionProjection,
    user: User,
    message: str,
    change_key: str,
    expected_main_commit: str | None = None,
) -> str | None:
    """把会话的改动发布到 main，返回那次提交的 sha；没有改动返回 None。

    `change_key` 是幂等键（写进 `Change-Set-ID:` trailer）—— 同一个键发两次，
    第二次直接返回第一次那个 sha。这与交付幂等（`Delivered-Run:`）是同一种做法：
    判据写在 git 上，不写在表上。
    """
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    if not await has_project_capability(db, user, project, "publish_changes"):
        raise HTTPException(status_code=403, detail="Missing project capability: publish_changes")

    repository = get_project_repository()
    changes = await read_session_changes(str(project.id), session)
    if changes.has_conflicts:
        raise PublishConflictsFoundError(
            "Resolve conflicts before publishing: " + ", ".join(changes.conflicting_paths)
        )
    if not changes.changed_paths:
        return None

    main_head = (await run_in_repository_thread(repository.status, str(project.id))).head_commit
    if expected_main_commit and expected_main_commit != main_head:
        raise HTTPException(
            status_code=409, detail="Project head changed; refresh before publishing"
        )
    try:
        commit = await run_in_repository_thread(
            repository.publish_linear,
            project_id=str(project.id),
            session_id=session.session_id,
            expected_main_commit=main_head,
            paths=list(changes.changed_paths),
            message=message,
            change_set_id=change_key,
            actor_id=str(user.id),
        )
    except ProjectRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # 这个会话暂存的产物随这次发布转正。从前这一步是「把 ChangeItem 指到的
    # 版本翻成 published」；现在按会话找 —— 同一个事实，少一张中间表。
    candidates = (
        await db.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.session_id == session.session_id,
                ArtifactVersion.lifecycle_status == "candidate",
            )
        )
    ).scalars().all()
    for version in candidates:
        version.lifecycle_status = "published"
        version.git_commit_sha = commit
    await db.flush()
    return commit


async def resolve_conflict(
    db: AsyncSession,
    *,
    project: Project,
    session: SessionProjection,
    path: str,
    choice: str,
    user: User,
) -> dict:
    """两边都改了同一个文件时，决定用谁那一版。

    `use_project` 把 main 那一版取回会话工作区并提交。`use_proposed` **什么都
    不做** —— 发布本来就是把会话的路径回放到 main 上，会话那一版自然胜出；
    为了对称造一条空记录，只会多出一处会和事实分叉的状态。
    """
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    if not await has_project_capability(db, user, project, "publish_changes"):
        raise HTTPException(status_code=403, detail="Missing project capability: publish_changes")
    if choice not in {"use_project", "use_proposed"}:
        raise HTTPException(status_code=422, detail="choice must be use_project or use_proposed")

    changes = await read_session_changes(str(project.id), session)
    if path not in changes.conflicting_paths:
        raise HTTPException(status_code=404, detail="This path is not in conflict")

    if choice == "use_proposed":
        return {"path": path, "choice": choice, "headCommitSha": changes.head_commit}

    repository = get_project_repository()
    try:
        head = await run_in_repository_thread(
            repository.take_main_version, str(project.id), session.session_id, path
        )
    except ProjectRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.git_head_commit_sha = head
    await db.flush()
    return {"path": path, "choice": choice, "headCommitSha": head}


# ── 产物暂存 ─────────────────────────────────────────────────────────────────
#
# 从前这一步要先开一个 ChangeSet、再挂一条 ChangeItem，版本行上还留一个
# change_set_id 指回去。三层间接回答的是同一个问题：**这个会话给这个
# resource_key 出的最新一版是哪个**。版本行自己就带着会话与 resource_key ——
# 中间那两跳是那张表存在的理由，不是这个问题的答案。


async def stage_artifact_candidate(
    db: AsyncSession,
    *,
    project: Project,
    session: SessionProjection,
    user: User,
    artifact_id: str | None,
    name: str,
    artifact_type: str,
    resource_key: str | None,
    content: str,
    mime_type: str,
    description: str | None,
    resource_type: str = "artifact",
    source_run_id: str | None = None,
    owner_node: str | None = None,
) -> ArtifactVersion:
    if not await has_project_capability(db, user, project, "drive_session"):
        raise HTTPException(status_code=403, detail="Missing project capability: drive_session")
    if resource_type not in {"artifact", "project_doc", "project_config"}:
        raise HTTPException(
            status_code=422,
            detail="resourceType must be artifact, project_doc, or project_config; KB is excluded",
        )
    artifact = (
        await db.scalar(select(Artifact).where(Artifact.id == artifact_id).with_for_update())
        if artifact_id
        else None
    )
    if artifact_id and (not artifact or str(artifact.project_id) != str(project.id)):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if resource_type == "artifact":
        try:
            parsed_type = ArtifactType(artifact_type)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Unsupported artifactType") from exc
    else:
        parsed_type = ArtifactType.OTHER
    key = (resource_key or (f"artifact:{artifact.id}" if artifact else "")).strip()
    if not key and resource_type != "artifact":
        raise HTTPException(status_code=422, detail="resourceKey is required")
    encoded = content.encode()
    checksum = hashlib.sha256(encoded).hexdigest()
    # 这个 resource_key 在这个会话里上一次暂存的版本。从前要经 ChangeItem 转一手
    # （change_set → item → proposed_version_id）；版本行自己就带着会话与
    # resource_key，中间那一跳只是那张表存在的理由，不是这个问题的答案。
    previous = await db.scalar(
        select(ArtifactVersion)
        .where(
            ArtifactVersion.session_id == session.session_id,
            ArtifactVersion.resource_key == key,
        )
        .order_by(ArtifactVersion.version.desc())
        .limit(1)
        .with_for_update()
    ) if key else None
    if (
        previous
        and previous.lifecycle_status == "candidate"
        and previous.checksum == checksum
        and previous.git_commit_sha
    ):
        return previous
    if previous and artifact and previous.artifact_id != artifact.id:
        raise HTTPException(
            status_code=409,
            detail="resourceKey is already staged for another artifact",
        )
    if previous and not artifact:
        artifact = await db.scalar(
            select(Artifact).where(Artifact.id == previous.artifact_id).with_for_update()
        )
    # 「这一版是在改哪一版」从前查 manifest 里的 resource_key → version_id。
    # 现在按同一个 resource_key 在这个项目里的上一版算 —— 同一个事实，少一张表。
    base_version_id = previous.id if previous else None
    if not artifact and base_version_id:
        base_version = await db.get(ArtifactVersion, base_version_id)
        artifact = (
            await db.scalar(
                select(Artifact).where(Artifact.id == base_version.artifact_id).with_for_update()
            )
            if base_version
            else None
        )
    if not artifact:
        artifact = Artifact(
            project_id=project.id,
            name=name,
            type=parsed_type,
            description=description,
            mime_type=mime_type,
            current_version=0,
            extra_data={"_candidate_only": True},
        )
        db.add(artifact)
        await db.flush()
    key = key or f"artifact:{artifact.id}"
    if not key:
        raise HTTPException(status_code=422, detail="resourceKey cannot be empty")
    max_version = await db.scalar(
        select(func.max(ArtifactVersion.version)).where(ArtifactVersion.artifact_id == artifact.id)
    )
    version_id = str(uuid4())
    version = ArtifactVersion(
        id=version_id,
        artifact_id=artifact.id,
        version=int(max_version or 0) + 1,
        resource_key=key,
        session_id=session.session_id,
        lifecycle_status="candidate",
        created_by_user_id=user.id,
        size_bytes=len(encoded),
        checksum=checksum,
    )
    db.add(version)
    await db.flush()
    if previous and previous.lifecycle_status == "candidate":
        # 同一个 resource_key 在这个会话里又出了一版：上一版冻住，不再是候选。
        previous.lifecycle_status = "frozen"
    await db.flush()
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
    )

    repository = get_project_repository()
    repository_status = await run_in_repository_thread(
        repository.initialize_project,
        project_id=str(project.id),
        name=project.name,
        description=project.description,
        research_domain=project.research_domain,
        owner_id=project.owner_id,
    )
    # 会话从 main 的当前 head 分出去。从前这里读 base_revision.git_commit_sha ——
    # 那一行本身也是 git head 的一份拷贝（拷贝没有时还要补写回去）。
    base_commit = session.git_base_commit_sha or repository_status.head_commit
    workspace = await run_in_repository_thread(
        repository.ensure_session_workspace,
        project_id=str(project.id),
        session_id=session.session_id,
        base_commit=base_commit,
        title=session.title,
        created_by=user.id,
    )
    try:
        git_revision = await run_in_repository_thread(
            repository.write_artifact_revision,
            project_id=str(project.id),
            session_id=session.session_id,
            artifact_id=str(artifact.id),
            artifact_type=parsed_type.value,
            name=name,
            content=content,
            mime_type=mime_type,
            checksum=checksum,
            version=version.version,
            actor_id=user.id,
            change_set_id=str(session.session_id),
            expected_head_commit=session.git_head_commit_sha or workspace.head_commit,
            resource_type=resource_type,
            resource_key=key,
            source_run_id=source_run_id,
            owner_node=owner_node,
            source_attestation=None,
            artifact_contract=None,
        )
    except ProjectRepositoryError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "project_repository_write_failed", "message": str(exc)},
        ) from exc
    version.git_commit_sha = git_revision.commit_sha
    version.repository_path = git_revision.repository_path
    session.git_branch = workspace.branch
    session.git_base_commit_sha = workspace.base_commit
    session.git_head_commit_sha = git_revision.commit_sha
    session.git_worktree_path = workspace.path
    await db.flush()
    return version



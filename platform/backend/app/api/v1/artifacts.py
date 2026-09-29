"""Artifact management API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models.artifact import Artifact, ArtifactVersion
from app.models.project import Project
from app.models.user import User
from app.policies import visible_project_ids
from app.schemas.artifact import (
    ArtifactContentResponse,
    ArtifactCreate,
    ArtifactResponse,
    ArtifactVersionResponse,
)
from app.services.sessions import require_capability

# Per-project router (mounted at /projects/{project_id}/artifacts)
router = APIRouter()

# Global router (mounted at /artifacts)
global_router = APIRouter()


def _is_candidate_only(artifact: Artifact) -> bool:
    return bool((artifact.extra_data or {}).get("_candidate_only"))


# ─── Per-project endpoints ─────────────────────────────────────────────────


@router.post("/", response_model=ArtifactResponse, status_code=201)
async def create_artifact(
    project_id: str,
    data: ArtifactCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Artifact:
    await require_capability(db, user, project_id, "publish_changes")
    artifact = Artifact(
        project_id=project_id,
        name=data.name,
        type=data.type,
        description=data.description,
        mime_type=data.mime_type,
        extra_data=data.extra_data,
    )
    db.add(artifact)
    await db.flush()
    return artifact


@router.get("/", response_model=list[ArtifactResponse])
async def list_artifacts(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[Artifact]:
    query = select(Artifact).where(Artifact.project_id == project_id)
    query = query.order_by(Artifact.updated_at.desc())
    result = await db.execute(query)
    return [item for item in result.scalars().all() if not _is_candidate_only(item)]


@router.get("/{artifact_id}", response_model=ArtifactResponse)
async def get_artifact(
    project_id: str,
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
) -> Artifact:
    result = await db.execute(
        select(Artifact).where(
            Artifact.id == artifact_id, Artifact.project_id == project_id
        )
    )
    artifact = result.scalar_one_or_none()
    if not artifact or _is_candidate_only(artifact):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


@router.get("/{artifact_id}/content", response_model=ArtifactContentResponse)
async def get_artifact_content(
    project_id: str,
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """产物正文 —— 从 **git** 读，不从库里那份拷贝。

    库里曾经存着第二、第三份字节（`artifact_versions.content` 与
    `artifacts.extra_data["_content"]`），而 git 里那一份才是权威（路径即身份、
    head 即当前）。同一段内容存三处，就有三处会各自演化。
    """
    result = await db.execute(
        select(Artifact).where(
            Artifact.id == artifact_id, Artifact.project_id == project_id
        )
    )
    artifact = result.scalar_one_or_none()
    if not artifact or _is_candidate_only(artifact):
        raise HTTPException(status_code=404, detail="Artifact not found")

    version = await db.scalar(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact.id)
        .order_by(ArtifactVersion.version.desc())
        .limit(1)
    )
    content = ""
    if version is not None and version.repository_path:
        from app.services.project_repository import get_project_repository, run_in_repository_thread

        try:
            content = await run_in_repository_thread(
                get_project_repository().read_file_at_revision,
                artifact.project_id,
                version.repository_path,
                version.git_commit_sha or "HEAD",
            )
        except Exception:
            content = ""
    return {"content": content, "mime_type": artifact.mime_type}


@router.delete("/{artifact_id}", status_code=204)
async def delete_artifact(
    project_id: str,
    artifact_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await require_capability(db, user, project_id, "publish_changes")
    result = await db.execute(
        select(Artifact).where(
            Artifact.id == artifact_id, Artifact.project_id == project_id
        )
    )
    artifact = result.scalar_one_or_none()
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    await db.delete(artifact)
    await db.flush()


@router.get("/{artifact_id}/versions", response_model=list[ArtifactVersionResponse])
async def list_artifact_versions(
    project_id: str,
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[ArtifactVersion]:
    artifact = await db.scalar(
        select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.project_id == project_id,
        )
    )
    if not artifact or _is_candidate_only(artifact):
        raise HTTPException(status_code=404, detail="Artifact not found")
    result = await db.execute(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.desc())
    )
    return list(result.scalars().all())


# ─── Global endpoints ──────────────────────────────────────────────────────


@global_router.get("/", response_model=list[ArtifactResponse])
async def list_all_artifacts(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List all artifacts across all projects, with project_name attached."""
    project_ids = await visible_project_ids(db, user)
    result = await db.execute(
        select(Artifact, Project.name.label("project_name"))
        .join(Project, Artifact.project_id == Project.id)
        .where(Project.id.in_(project_ids))
        .order_by(Project.name, Artifact.type, Artifact.updated_at.desc())
    )
    rows = result.all()

    artifacts = []
    for artifact, project_name in rows:
        if _is_candidate_only(artifact):
            continue
        resp = ArtifactResponse.model_validate(artifact)
        resp.project_name = project_name
        artifacts.append(resp)

    return artifacts

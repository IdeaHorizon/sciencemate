"""Project management API endpoints."""

from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models.execution import SessionProjection, BLOCKER_RESOLVED_RUN_STATUSES
from app.models.project import (
    EntryType,
    Project,
    ProjectConfig,
    ProjectMembership,
    ProjectStatus,
)
from app.models.user import User
from app.policies import can_access_project, has_project_capability, my_project_ids
from app.schemas.project import (
    ProjectConfigUpdate,
    ProjectCreate,
    ProjectDetailResponse,
    ProjectResponse,
    ProjectUpdate,
)

router = APIRouter()


async def _project_response(db: AsyncSession, user: User, project: Project) -> dict:
    from app.services.sessions import effective_capabilities

    member_count = await db.scalar(
        select(func.count())
        .select_from(ProjectMembership)
        .where(
            ProjectMembership.project_id == project.id,
            ProjectMembership.removed_at.is_(None),
        )
    )
    active_session_count = await db.scalar(
        select(func.count())
        .select_from(SessionProjection)
        .where(
            SessionProjection.project_id == str(project.id),
            SessionProjection.lifecycle_status == "active",
        )
    )
    capabilities = await effective_capabilities(db, user, project, api_names=True)
    # 上面那几个计数/能力查询会 autoflush，server-side onupdate 让 updated_at
    # 过期。在异步边界内显式 refresh，别让 Pydantic 触发隐式 async IO。
    await db.refresh(project)
    from app.services import other_homes

    mine = project.owner_id == user.id or bool(await db.scalar(
        select(ProjectMembership.id).where(
            ProjectMembership.project_id == project.id,
            ProjectMembership.user_id == user.id,
            ProjectMembership.removed_at.is_(None),
        )))
    from app.policies import stewards_it

    return {
        **ProjectResponse.model_validate(project).model_dump(),
        "mine": mine,
        "can_archive": await stewards_it(db, user, project),
        "capabilities": capabilities,
        "member_count": int(member_count or 0),
        "active_session_count": int(active_session_count or 0),
        # 这个函数只画**本机库里**那些行，所以家永远是"这台机器"。桌面把别处的
        # 项目并进清单时会按那条连接重写这个字段 —— 组织服务器自己答这一句时也
        # 是对的：项目确实住在它身上。
        "home": other_homes.at_home_here(),
    }


@router.post("/", response_model=ProjectResponse, status_code=201)
async def create_project(
    data: ProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """建一个项目 —— 在本机，或者在某个组织里。

    家在**出生这一刻**挑，默认本机。挑了组织的话，项目是那台服务器建的：它的
    记录、仓库、会话从出生起就只有一份在那儿（`RFC_ORGANISATION_IS_A_PROJECT_HOME`
    A2）。不是"本机建完同步过去"—— 两处各一份就是两个真相源。
    """
    if data.home:
        from app.services import other_homes

        try:
            return await other_homes.create_it_elsewhere(
                data.home, data.model_dump(exclude={"home"}, mode="json"))
        except other_homes.ThereIsNoSuchHomeError as refused:
            raise HTTPException(status_code=refused.status, detail=refused.message) from refused

    from app.services.project_service import create_project

    result = await create_project(
        db=db,
        owner_id=user.id,
        name=data.name,
        description=data.description,
        research_domain=data.research_domain,
        entry_type=data.entry_type or EntryType.FUZZY_IDEA,
        operation_mode=data.operation_mode,
        reporting_level=data.reporting_level,
        preferred_model=data.preferred_model,
    )
    return await _project_response(db, user, result["project"])


@router.get("/", response_model=list[ProjectResponse])
async def list_projects(
    status: ProjectStatus | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """这个人看得见的项目 —— 本机的，加上每个组织里的。

    一份清单，按家分组（侧栏就是这么画的）。连不上的那台机器用上一次问到的名字，
    标着 `reachable: false` 灰着摆 —— 用户的项目不该因为一台机器关着就消失一半。

    **一台没连过任何组织的桌面上，这个函数和从前逐字一样**：没有连接就没有要问
    的人（`other_homes.the_ones_that_live_elsewhere` 返回空）。组织服务器自己也一样 ——
    它的数据根里没有连接清单。
    """
    from app.services import other_homes

    # 「我的项目」—— 管理员也一样；组里别的项目在组织页（`/organisation/projects`）。
    query = select(Project).where(Project.id.in_(my_project_ids(user)))
    if status:
        query = query.where(Project.status == status)
    query = query.order_by(Project.updated_at.desc())
    result = await db.execute(query)
    here = [
        {**await _project_response(db, user, item), "home": other_homes.at_home_here()}
        for item in result.scalars().all()
    ]
    elsewhere = await other_homes.the_ones_that_live_elsewhere()
    if status:
        elsewhere = [row for row in elsewhere if row.get("status") == status.value]
    # 本机的在前 —— 那是这台机器自己的东西，而且永远答得出来。
    return here + elsewhere


@router.get("/{project_id}", response_model=ProjectDetailResponse)
async def get_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(Project).options(selectinload(Project.config)).where(Project.id == project_id)
    result = await db.execute(query)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await can_access_project(db, user, project):
        raise HTTPException(status_code=403, detail="Not authorized to view this project")

    return {
        **await _project_response(db, user, project),
        "config": project.config,
    }


@router.get("/{project_id}/repository", response_model=dict)
async def get_project_repository_status(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await can_access_project(db, user, project):
        raise HTTPException(status_code=403, detail="Not authorized to view this project")
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    repository = get_project_repository()
    try:
        status = await run_in_repository_thread(repository.status, project_id)
    except ProjectRepositoryError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "project_repository_unavailable", "message": str(exc)},
        ) from exc
    return {
        "projectId": project_id,
        "repositoryId": project_id,
        "schemaVersion": 2,
        "authority": "git",
        "defaultBranch": "main",
        "branch": status.branch,
        "headCommitSha": status.head_commit,
        "clean": status.clean,
    }


async def _authorize_repository_read(
    db: AsyncSession,
    user: User,
    project_id: str,
    session_id: str | None,
) -> None:
    """读工作区文件的三道准入：项目在、看得见、会话属于这个项目。

    tree / file / raw 三条路由问的是同一个问题。抄三份的话，加固只会落在
    其中一份上，而另外两份看起来一样能跑 —— 这类分叉不会有任何一层报错。
    """
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await can_access_project(db, user, project):
        raise HTTPException(status_code=403, detail="Not authorized to view this project")
    if session_id:
        session = await db.scalar(
            select(SessionProjection).where(
                SessionProjection.project_id == project_id,
                SessionProjection.session_id == session_id,
            )
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")


@router.get("/{project_id}/catalog", response_model=dict)
async def get_project_catalog(
    project_id: str,
    session_id: str | None = Query(default=None, alias="sessionId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """这个项目产出了什么 —— **一个问题，一个答案**。

    ## 为什么加这条路由

    2026-09-09 之前，同一个问题有六个地方各自回答（Project files 扫 Git 树、
    Artifacts 面读 artifacts 表、交付物条读 deliverables 复制品、Research state
    读单个 artifact、会话变更面板读 change-set、收尾清单扫冻结账本）。同一篇
    论文可能出现在三处、各有各的名字，也可能一处都没有 —— 而分叉不报错。

    ## 判决全部来自 harness，后端一个字都不重算

    交付物是什么、哪些是框架内务、伴随文件怎么找 —— 全在 `core.catalog`。
    后端在这里做的只有两件事：鉴权，以及把工作区相对路径原样端出去
    （前端拿它去 `/repository/raw` 取字节，两边用的是同一个 worktree）。

    ⚠️ 每一项的 `files` 是**agent 写进 metadata 的数据**，按不可信输入处理：
    `core.catalog` 只放行"落在工作区里、盘上真的存在"的路径，绝对路径与
    工作区外的一律丢弃。这条不是格式讲究 —— 渲染层对外部 URL 是零点击外泄。
    """
    await _authorize_repository_read(db, user, project_id, session_id)

    from app.services.harness_contract import HarnessContractUnavailable
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    repository = get_project_repository()
    try:
        if session_id:
            workspace = await run_in_repository_thread(
                repository.session_status, project_id, session_id
            )
            root = workspace.path
        else:
            root = (await run_in_repository_thread(repository.status, project_id)).path
    except ProjectRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        from app.services.harness_contract import catalog_module

        catalog = catalog_module()
    except HarnessContractUnavailable as exc:
        # 缺 HARNESS_ROOT 时说清是**部署没配好**，不要回一个空目录 ——
        # 空目录看起来和"这个项目还没产出东西"一模一样。
        raise HTTPException(
            status_code=503,
            detail=f"研究产出这一层需要 harness checkout（HARNESS_ROOT）：{exc}",
        ) from exc

    entries = await run_in_repository_thread(catalog.build, root)
    grouped = catalog.by_tier(entries)
    return {
        "schemaVersion": 1,
        "sessionId": session_id,
        "entries": [entry.as_dict() for entry in entries],
        "counts": {tier: len(rows) for tier, rows in grouped.items()},
    }


@router.get("/{project_id}/repository/records", response_model=dict)
async def get_project_records(
    project_id: str,
    session_id: str | None = Query(default=None, alias="sessionId"),
    artifact_type: str | None = Query(default=None, alias="type"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """账本上的记录 head（类型 / 名字 / 路径 / 版本 / 冻结 / metadata）。

    研究记录是原生文件 + 一份账本（RFC 2026-09-12 §6）。正文走
    `/repository/raw?path=`，**事实**走这里：正文文件本身不带类型、版本、出处，
    也不带 research_state 那种结构化 metadata（假说裁决表）。界面此前按文件名
    正则去猜 head 在哪、再拆 JSON 信封拿 metadata —— 两个都不该由界面猜。

    判读全在 harness（`core.ledger`），后端只鉴权 + 端出去。旧布局（信封时代）
    的工作区回 `legacyLayout: true`，让界面说"这个项目是旧布局"而不是"还没有"。
    """
    await _authorize_repository_read(db, user, project_id, session_id)

    from app.services.harness_contract import HarnessContractUnavailable
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    repository = get_project_repository()
    try:
        if session_id:
            workspace = await run_in_repository_thread(
                repository.session_status, project_id, session_id
            )
            root = workspace.path
        else:
            root = (await run_in_repository_thread(repository.status, project_id)).path
    except ProjectRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        from app.services.harness_contract import ledger_module

        ledger = ledger_module()
    except HarnessContractUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"研究记录这一层需要 harness checkout（HARNESS_ROOT）：{exc}",
        ) from exc

    def _read() -> dict:
        legacy = ledger.old_layout_fragments(root)
        heads = ledger.workspace_store(root).heads()
        rows = []
        for head in heads.values():
            if artifact_type and head.artifact_type != artifact_type:
                continue
            rows.append({
                "id": head.artifact_id,
                "type": head.artifact_type,
                "name": head.name,
                "path": head.path,
                "version": head.version,
                "frozen": bool(head.frozen_version),
                "frozenVersion": head.frozen_version,
                "frozenAt": head.frozen_at,
                "createdAt": head.created_at,
                "producedByNodeType": head.produced_by_node_type,
                "producedByRunId": head.produced_by_run_id,
                "metadata": head.metadata,
            })
        rows.sort(key=lambda r: (r["createdAt"], r["id"]))
        return {"legacyLayout": bool(legacy), "records": rows}

    payload = await run_in_repository_thread(_read)
    return {"schemaVersion": 1, "sessionId": session_id, **payload}


@router.get("/{project_id}/repository/tree", response_model=dict)
async def get_project_repository_tree(
    project_id: str,
    session_id: str | None = Query(default=None, alias="sessionId"),
    path: str = Query(default="", max_length=1_000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """列举工作区里 `path` 那一层 —— 不传 path 就是根。

    schemaVersion 3 起是**一层一层**给的，不再是一个被静默切到 5000 条的平坦
    列表。`truncated` / `totalEntries` / `totalFiles` 是响应的一部分：到顶了要
    说出来，界面不许把"我只拿到这么多"显示成"一共就这么多"。
    """
    await _authorize_repository_read(db, user, project_id, session_id)
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    try:
        listing = await run_in_repository_thread(
            get_project_repository().file_tree,
            project_id,
            session_id=session_id,
            path=path,
        )
    except ProjectRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"schemaVersion": 3, "sessionId": session_id, **listing}


@router.get("/{project_id}/repository/file", response_model=dict)
async def get_project_repository_file(
    project_id: str,
    path: str = Query(min_length=1, max_length=1_000),
    session_id: str | None = Query(default=None, alias="sessionId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_repository_read(db, user, project_id, session_id)
    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    try:
        return await run_in_repository_thread(
            get_project_repository().read_worktree_file, project_id, path, session_id=session_id
        )
    except ProjectRepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/{project_id}/repository/raw",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def get_project_repository_raw(
    project_id: str,
    path: str = Query(min_length=1, max_length=1_000),
    session_id: str | None = Query(default=None, alias="sessionId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """工作区文件的**字节**出口 —— 图、PDF、以及任何非文本产出。

    在这条路由之前，节点产出的 PNG 和论文 PDF 在 API 层面就是取不到的：
    `/repository/file` 把内容解码成 UTF-8 字符串，二进制一律回 `content: null`。
    界面上因此只剩一个文件名。

    ## 三个响应头都不是装饰

    - `X-Content-Type-Options: nosniff` —— 我们按扩展名给类型（见 file_media），
      不许浏览器改主意去嗅探内容。
    - `Content-Security-Policy: sandbox` —— 这里送出的是 **agent 写的文件**。
      正常渲染路径上前端拿的是 blob（响应头不随 blob 走，所以不受影响），但
      直接访问这个 URL 时，sandbox 让它落进一个不透明源：即使内容是 HTML 或
      带脚本的 SVG，也够不着 API 源上的任何东西。生产环境前后端同源，少了
      这一条就是"任意文件 → 同源脚本执行"。
    - `Content-Disposition: inline` —— 目的是"打开看"，不是"下载"。文件名走
      RFC 5987，中文文件名才不会在这里变成乱码。
    """
    await _authorize_repository_read(db, user, project_id, session_id)
    from app.services.file_media import media_type_for
    from app.services.project_repository import (
        ProjectFileTooLargeError,
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    try:
        safe, body = await run_in_repository_thread(
            get_project_repository().read_worktree_bytes,
            project_id,
            path,
            session_id=session_id,
            max_bytes=settings.project_file_preview_max_bytes,
        )
    # 顺序要紧：TooLarge 是 ProjectRepositoryError 的子类，放在后面永远走不到，
    # "文件太大"就会被报成"文件不存在"。
    except ProjectFileTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "project_file_too_large",
                "message": str(exc),
                "sizeBytes": exc.size_bytes,
                "maxBytes": exc.max_bytes,
            },
        ) from exc
    except ProjectRepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    filename = PurePosixPath(safe).name
    return Response(
        content=body,
        media_type=media_type_for(safe),
        headers={
            "Content-Disposition": (
                f"inline; filename*=UTF-8''{quote(filename, safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            # 产出会被重跑覆盖（同一个 fig1.png 换了内容）。缓存住等于研究员
            # 看着旧图讨论新结果。
            "Cache-Control": "private, no-cache",
        },
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    data: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await has_project_capability(db, user, project, "manage_settings"):
        raise HTTPException(
            status_code=403,
            detail="Missing project capability: manage_settings",
        )

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(project, field, value)
    await db.flush()
    from app.services.project_record import commit_project_record

    await commit_project_record(
        db, project=project, user=user, message="project: update metadata"
    )
    return await _project_response(db, user, project)






@router.post("/{project_id}/archive", response_model=ProjectResponse)
async def archive_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """归档 = 冻结：只读、不再开会话、后台不再替它干活；什么都不丢，能恢复。负责人或组织管理员。"""
    from app.services import project_lifecycle

    project = await project_lifecycle.archive(db, user, project_id)
    return await _project_response(db, user, project)


@router.post("/{project_id}/restore", response_model=ProjectResponse)
async def restore_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """恢复一个归档的项目（负责人或组织管理员）。"""
    from app.services import project_lifecycle

    project = await project_lifecycle.restore(db, user, project_id)
    return await _project_response(db, user, project)


@router.patch("/{project_id}/config", response_model=dict)
async def update_project_config(
    project_id: str,
    data: ProjectConfigUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    proj_result = await db.execute(select(Project).where(Project.id == project_id))
    project = proj_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await has_project_capability(db, user, project, "manage_settings"):
        raise HTTPException(
            status_code=403,
            detail="Missing project capability: manage_settings",
        )

    result = await db.execute(select(ProjectConfig).where(ProjectConfig.project_id == project_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Project config not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(config, field, value)
    await db.flush()
    from app.services.project_record import commit_project_record

    await commit_project_record(
        db, project=project, user=user, message="project: update platform settings"
    )

    # ── 改完设置要**送到**正在跑的那一轮 ────────────────────────────────────
    #
    # 自主档（`operation_mode` + `autonomous_authorized_risk_classes`）不是一条
    # 事后生效的偏好，它决定"下一个决策点要不要停下来问人"。而 worker 手里只有
    # 一份**派发时**的快照，派发只在有人说话时发生 —— 一轮无人值守跑几十分钟、
    # 经过十几个决策点，中间一次派发都没有。
    #
    # 2026-08-23 会话 e46448f0 实测：人在一轮跑到一半时切成「连续」，19 分钟后
    # 那个 post-node 决策照样停下来问人；直到人手动答复（那才产生了一次派发），
    # 后面的决策点才开始自动放行。UI 上白纸黑字改了，落库也改了，就是没到现场。
    #
    # 落库仍然是权威（推不动的会话下次派发自然会带上），这里只负责让活着的
    # 那些**立刻**跟上。
    from app.services.harness_sessions import harness_session_manager
    from app.services.local_execution import (
        answer_pending_decisions_with_recommendation,
        project_autonomy_policy,
    )

    await db.commit()
    policy = await project_autonomy_policy(db, project)
    await harness_session_manager.broadcast_autonomy(str(project.id), policy, db=db)
    # 连续档还要多做一步：**已经**停在屏幕上的那张决策卡也替人点掉（推荐项）。
    # broadcast 只改变以后的决策点；停着的那张 worker 攥着 pause 在等一次
    # answer 派发，开关翻了它也不会自己走 —— 用户看到的就是"切了连续，卡还在"
    # （2026-08-24 实测）。判据与 harness 侧同源：连续 = 预授权覆盖全部类别。
    # 自主 / 连续都替人点掉已经停在屏幕上的那张（非高危；高危审批那条路不碰）。
    if policy.mode != "assisted":
        answer_pending_decisions_with_recommendation(str(project.id))
    return {"status": "updated"}


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a project and all associated data (cascade)."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await has_project_capability(db, user, project, "manage_settings"):
        raise HTTPException(
            status_code=403,
            detail="Missing project capability: manage_settings",
        )

    await db.execute(sql_delete(Project).where(Project.id == project_id))
    await db.commit()


# ── Intake Interview（2026-09-05 删）─────────────────────────────────────
#
# 两个端点（`intake/generate` / `intake/submit`）加一个 `app/core/intake.py`
# 访谈引擎。**前端零调用方**：`api.intakeGenerate` / `api.intakeSubmit` 定义了
# 但没有任何界面调它们。它们是后端**自己那份 LLM 客户端**（`app/llm/`）唯一
# 剩下的生产消费者 —— 平台只有一套 agent loop（harness），后端再持一份模型
# 接入就是第二处凭据、第二处 provider 路由、第二处重试策略，且两处会各自演化。
#
# 删而不是搬：一个没有调用方的功能，搬到桥上只是把没人走的路挪了个地方。
# 真需要访谈时，它属于 harness 的一个节点，不属于 App Server。

@router.get("/{project_id}/blockers", response_model=dict)
async def list_project_blockers(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """节点报告的阻塞（v2.1）。

    `report_blocker` 是新架构的一等机制：节点不自己硬扛，也不静默失败 ——
    它报事实 + 证据 + 需求，由调度器或人来决定怎么解。此前这个信号只以
    `run.blocked` 事件存在，用户要一条条翻 run 的事件流才看得到，等于看不到。

    阻塞是**项目级**的问题（"我这个项目现在卡在哪"），所以在这里做成项目级
    查询，而不是让前端逐个 run 拉事件流去拼 —— 那样既慢又会随 run 数增长。

    `stale`：报阻塞之后这个 run 又走到了终态且不是失败态 —— 说明它后来自己
    解开了或被替代了。仍然列出来（历史可审计），但标记出来，别让用户去处理
    一个已经不存在的问题。
    """
    from app.models.execution import ExecutionEvent, Run, RunStatus

    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await can_access_project(db, user, project):
        raise HTTPException(status_code=403, detail="Not authorized to view this project")

    rows = (
        await db.execute(
            select(ExecutionEvent, Run)
            .join(Run, Run.id == ExecutionEvent.run_id)
            .where(
                ExecutionEvent.project_id == project_id,
                ExecutionEvent.kind == "run.blocked",
            )
            .order_by(ExecutionEvent.occurred_at.desc())
            .limit(limit)
        )
    ).all()

    _RESOLVED = BLOCKER_RESOLVED_RUN_STATUSES
    blockers = []
    for event, run in rows:
        payload = event.payload if isinstance(event.payload, dict) else {}
        blockers.append({
            "id": payload.get("blockerId") or event.id,
            "runId": run.id,
            "sessionId": event.session_id,
            "reportingNode": payload.get("reportingNode") or run.node_type or "",
            "category": payload.get("category") or "other",
            "summary": payload.get("summary") or "",
            "requestedAction": payload.get("requestedAction") or "",
            "suggestedOwner": payload.get("suggestedOwner") or "",
            "retryableAfterChange": bool(payload.get("retryableAfterChange", True)),
            "evidencePaths": list(payload.get("evidencePaths") or []),
            "reportedAt": event.occurred_at.isoformat(),
            "runStatus": str(run.status),
            "stale": run.status in _RESOLVED,
        })
    return {"schemaVersion": 1, "projectId": project_id, "blockers": blockers}


@router.get("/{project_id}/jobs", response_model=dict)
async def get_project_jobs(
    project_id: str,
    session_id: str | None = Query(default=None, alias="sessionId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """这个项目跑过哪些受管作业、各自怎么结束的（#941）。

    ## 为什么加这条路由

    观测面此前答不出「作业的真实终态、退出状态、调度器作业号」—— 十九个路由
    里一个都没有。于是评测只能把这些维度记成 NOT_OBSERVABLE，或者更糟：
    从 chat 文案、catalog 里有没有产物、退出码摘要去**推断**。

    ## 判决全部来自 harness

    终态、是否还开着、超时比 —— 全在 `core.jobs.JobRecord` 上算好了。
    后端在这里只做鉴权和端出去，一个字不重算（同 `get_project_catalog`）。

    ## 读不出来要说读不出来

    `core.jobs.observe()` 在登记表存在但读不动时**抛**，这里翻成 503 ——
    **不回空清单**。空清单看起来和「这个项目一个作业都没跑过」一模一样，
    而那正是 #941 点名要避免的推断。登记表不存在则如实返回空：
    那是"确实还没有作业"，两件事必须分开。
    """
    await _authorize_repository_read(db, user, project_id, session_id)

    from app.services.harness_contract import HarnessContractUnavailable
    from app.services.project_homes import the_job_ledger_of
    from app.services.project_repository import run_in_repository_thread

    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    # 登记表在**项目的家**（`core.jobs._jobs_path` = state.project_root，
    # 即 `<projects_home>/<项目>/jobs.jsonl`）。
    # 这里从前读的是项目仓库 / 会话工作区的根 —— 那里没有任何人写这个文件，于是这条路由对每个项目
    # 都答「一个作业都没跑过」（2026-09-24 读出来的）；之后改成按人拼，因为那时一个项目几个人各有
    # 一份。现在项目层一个项目一份（`docs/RFC_PROJECT_HOME_20260924.md`）。`sessionId` 只用来鉴权。
    ledgers = [the_job_ledger_of(str(project.id))]

    try:
        from app.services.harness_contract import jobs_module

        jobs = jobs_module()
    except HarnessContractUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"作业观测面需要 harness checkout（HARNESS_ROOT）：{exc}",
        ) from exc

    def read_them() -> list:
        return [record for directory in ledgers for record in jobs.observe(directory)]

    try:
        records = await run_in_repository_thread(read_them)
    except jobs.JobsLedgerUnreadable as exc:
        # 读不动就说读不动。降级成空清单等于告诉调用方"没跑过作业"。
        raise HTTPException(
            status_code=503,
            detail=f"作业登记表读不出来（不是「没有作业」）：{exc}",
        ) from exc

    return {
        "jobs": [
            {
                "jobId": r.job_id,
                "purpose": r.purpose,
                "nodeType": r.node_type,
                "runId": r.run_id,
                "status": r.status,
                "schedulerJobId": r.scheduler_job_id,
                "pid": r.pid,
                "resources": r.resources,
                "startedAt": r.started_at or None,
                "lastCheckedAt": r.last_checked_at or None,
                "elapsedSeconds": r.elapsed_s,
                "etaSeconds": r.eta_s,
                "overrunRatio": r.overrun_ratio,
                "lastProgress": r.last_progress,
                "note": r.note,
            }
            for r in records
        ],
        "total": len(records),
    }

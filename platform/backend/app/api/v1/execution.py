"""Tenant-scoped read API for durable execution projections."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import get_current_user
from app.services.user_interface import language_for
from app.config import settings
from app.database import get_db
from app.models.execution import (
    Command,
    Decision,
    DecisionStatus,
    ExecutionEvent,
    Run,
    RunAttempt,
    RunStatus,
    SessionProjection,
    CANCELLABLE_RUN_STATUSES,
    STREAM_TERMINAL_RUN_STATUSES,
)
from app.models.project import Project
from app.models.user import User
from app.policies import can_access_project, has_project_capability, visible_project_ids
from app.schemas.execution import (
    DecisionAuthorityResponse,
    DecisionChoiceResponse,
    DecisionListResponse,
    DecisionRespondRequest,
    DecisionResponse,
    EventPageResponse,
    ExecutionEventResponse,
    RunAttemptResponse,
    RunDetailResponse,
    RunListResponse,
    RunResponse,
    RunUsageResponse,
)
from app.services.execution_ingest import (
    DecisionResponseRejectedError,
    ExecutionIngestService,
)
from app.services.execution_observers import (
    execution_event_revision,
    subscribe_run_transients,
    unsubscribe_run_transients,
    wait_for_execution_events,
)
from app.services import run_failures, run_liveness
from app.services.sse import sse_response


class CanonicalExecutionRoute(APIRoute):
    """Keep execution API failures inside the frozen ErrorResponse envelope."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def canonical_handler(request: Request):
            try:
                return await original_handler(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "Request validation failed.")
            except ResponseValidationError:
                return _error(
                    500,
                    "invalid_projection",
                    "Execution projection failed contract validation.",
                )
            except HTTPException as exc:
                code = {
                    401: "unauthorized",
                    403: "forbidden",
                    404: "not_found",
                    409: "conflict",
                    503: "service_unavailable",
                }.get(exc.status_code, "request_failed")
                detail = exc.detail
                message = (
                    detail
                    if isinstance(detail, str)
                    else detail.get("message", "Request failed.")
                    if isinstance(detail, dict)
                    else "Request failed."
                )
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"code": code, "message": message},
                    headers=exc.headers,
                )

        return canonical_handler


router = APIRouter(route_class=CanonicalExecutionRoute)


class CancelRunRequest(BaseModel):
    reason: str = Field(default="Cancelled by user", min_length=1, max_length=500)


def get_runtime_tenant_id() -> str:
    """Resolve the fixed Phase-0 tenant without accepting tenant input from a client."""
    tenant_id = settings.runtime_tenant_id.strip()
    if not tenant_id:
        raise HTTPException(
            status_code=503,
            detail="Execution tenant is not configured.",
        )
    return tenant_id


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "message": message})


def _encode_cursor(run: Run) -> str:
    body = json.dumps(
        {"createdAt": run.created_at.isoformat(), "id": run.id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
        created_at = datetime.fromisoformat(value["createdAt"])
        run_id = value["id"]
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("invalid cursor")
    return created_at, run_id


def _observed_summary(run: Run, observed_status: object, lang: str = "zh") -> object:
    """现算状态是 `stale_unknown` 且见证指明是**平台重启**时，把原因翻成用户可见
    的 `failure` —— 读时叠加，**不落盘**（D11：见证可持久化、判决不可以）。

    `summary.staleReason` 是见证（"某时刻观察到它没主了，原因是平台重启"，永真）；
    `failure` 是它此刻对用户的**说法**，随 `run_failures` 文案规则演化，所以每次读时
    从见证重算、绝不写回库。缺了这一步，后端明明在见证里写着 `app_server_restart`，
    前端却只读 `failure` —— 一个问题两个字段、从不相遇 → 掉进"这一轮没跑完 /
    Status unknown"的通用兜底（[[feedback_one_truth_source_per_question]]）。

    只认平台重启这一类：别的原因（真崩、上游拒绝…）在别处已有各自的落地路径，
    这里越俎代庖只会把"我不知道"伪装成一个具体诊断。
    """
    status_val = (
        observed_status.value if isinstance(observed_status, RunStatus)
        else str(observed_status or "")
    )
    summary = run.summary
    if status_val != RunStatus.STALE_UNKNOWN.value:
        return summary
    if not isinstance(summary, dict):
        return summary
    if summary.get("failure"):
        # 进程内那条路（优雅关闭时当场 describe）已写了更权威的 failure，别盖。
        return summary
    if summary.get("staleReason") != "app_server_restart":
        return summary
    failure = run_failures.describe(
        RuntimeError("worker lost across an app server restart"),
        reference=run.id,
        known_cause="app_server_restarted",
        lang=lang,
    ).as_record()
    return {**summary, "failure": failure}


def _run_view(run: Run, observed_status: object, summary: object, lang: str = "zh") -> dict:
    """这条 run 的局面 —— 与会话那一份出自**同一个** builder。

    两处各写一份就会分叉：会话顶栏说「被打断」而右栏那张卡还在转圈，正是
    2026-08-23 现场（右栏其实写好了「被打断」的纠偏，但 parentStatus 优先级
    更高，把它盖掉了）。
    """
    from app.services import execution_view as _view

    status_val = (
        observed_status.value if isinstance(observed_status, RunStatus)
        else str(observed_status or "")
    )
    record = summary if isinstance(summary, dict) else {}
    # ⚠️ 这里用的是 `build`，**不是** `build_session_view` —— 一条 run 的 view
    # 里没有 `answer`。答复入口是**会话**的属性；给历史 run 也发一份，就等于
    # 允许"从一条早就结束的 run 上渲染出一个能点的卡片"。那条 run 当时停在哪个
    # 问题上，由它自己的 `summary.pause` 如实记着，按**记录**呈现，永远只读。
    return _view.build(
        run,
        observed_status=status_val,
        failure=record.get("failure") if isinstance(record.get("failure"), dict) else None,
        live_runtime=_view.has_live_runtime(run.project_id, run.session_id),
        was_waiting_on=record.get("staleFromStatus"),
        lang=lang,
    )


def _run_response(run: Run, observed: dict[str, str], lang: str = "zh") -> RunResponse:
    """对外投影。`observed` 是 `run_liveness.observed_status_map` 现算的结果。

    ⚠️ 这个参数**必传**（2026-08-27 起）。它曾经有个 `= None` 默认值，
    而不传就是原样透传库里那一行 —— D11 之后那一行**不会**被改成
    stale_unknown，所以原样透传的是"它还在跑"这个可能早就不成立的断言。
    前端的 `runStatusById` / `LIVE_RUN_STATES` / 右栏的 `parentStatus` 全从
    这里取值（2026-08-23：右栏其实已经写好了"被打断"的纠偏，但 `parentStatus`
    优先级更高，正好把它盖掉了）。

    `status` 现算成 stale_unknown 的同时，`summary` 也要把见证里的重启原因翻成
    `failure` —— 两者是同一个现算结果的两面，不能只改状态、把原因留在用户够不到
    的 `staleReason` 里。
    """
    observed_status = observed[run.id]
    return RunResponse(
        id=run.id,
        tenant_id=run.tenant_id,
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        session_id=run.session_id,
        parent_run_id=run.parent_run_id,
        node_type=run.node_type,
        status=observed_status,
        usage=RunUsageResponse(
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            total_tokens=run.total_tokens,
            cost=float(run.cost) if run.cost is not None else None,
            currency=run.cost_currency,
            coverage=run.usage_coverage,
        ),
        retry_count=run.retry_count,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
        summary=_observed_summary(run, observed_status, lang),
        view=_run_view(run, observed_status, _observed_summary(run, observed_status, lang), lang),
    )


def _attempt_response(attempt: RunAttempt) -> RunAttemptResponse:
    return RunAttemptResponse.model_validate(attempt)


def _event_response(event: ExecutionEvent) -> ExecutionEventResponse:
    return ExecutionEventResponse(
        schema_version=event.schema_version,
        id=event.id,
        sequence=event.sequence,
        at=event.occurred_at,
        workspace_id=event.workspace_id,
        project_id=event.project_id,
        session_id=event.session_id,
        run_id=event.run_id,
        parent_run_id=event.parent_run_id,
        origin=event.origin,
        source=event.source,
        kind=event.kind,
        visibility=event.visibility,
        payload=event.payload,
    )


def _decision_response(decision: Decision) -> DecisionResponse:
    choices = [DecisionChoiceResponse.model_validate(choice) for choice in decision.choices]
    return DecisionResponse(
        id=decision.id,
        tenant_id=decision.tenant_id,
        workspace_id=decision.workspace_id,
        project_id=decision.project_id,
        session_id=decision.session_id,
        run_id=decision.run_id,
        attempt_no=decision.attempt_no,
        status=decision.status,
        subtype=decision.subtype,
        prompt=decision.prompt,
        context=decision.context,
        choices=choices,
        recommended_choice_id=decision.recommended_choice_id,
        selected_choice_id=decision.selected_choice_id,
        authority=DecisionAuthorityResponse(
            authority_type=decision.authority_type,
            authority_subjects=list(decision.authority_subjects),
            required_approval_count=decision.required_approval_count,
            action_set_version=decision.action_set_version,
            policy_snapshot_id=decision.policy_snapshot_id,
            expires_at=decision.expires_at,
        ),
        accepted_response_count=len(decision.accepted_responses or []),
        created_at=decision.created_at,
        updated_at=decision.updated_at,
        resolved_at=decision.resolved_at,
    )


async def _session_authorized(
    db: AsyncSession, *, tenant_id: str, session_id: str, user: User
) -> SessionProjection | None:
    session = await db.scalar(
        select(SessionProjection).where(
            SessionProjection.tenant_id == tenant_id,
            SessionProjection.session_id == session_id,
        )
    )
    if not session:
        return None
    # Narrow compatibility path for isolated projection tests and trusted internal callers.
    # Real authenticated User records always carry role and institution_id.
    if not hasattr(user, "role") or not hasattr(user, "institution_id"):
        return session
    if session.initiating_user_id == user.id:
        return session
    if session.project_id.startswith("global-"):
        return None
    project = await db.get(Project, session.project_id)
    if project and await can_access_project(db, user, project):
        return session
    return None


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    project_id: str | None = Query(None, alias="projectId"),
    session_id: str | None = Query(None, alias="sessionId"),
    status: RunStatus | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_runtime_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> RunListResponse | JSONResponse:
    query = select(Run).where(Run.tenant_id == tenant_id)
    if hasattr(user, "role") and hasattr(user, "institution_id"):
        authorized_project_ids = await visible_project_ids(db, user)
        # Durable Run projections keep project_id as a portable string, while the
        # projects table uses PostgreSQL UUID. Preserve the richer membership-aware
        # visibility query, but cast only at this compatibility boundary.
        authorized_project_string_ids = select(cast(Project.id, String)).where(
            Project.id.in_(authorized_project_ids)
        )
        own_session_ids = select(SessionProjection.session_id).where(
            SessionProjection.tenant_id == tenant_id,
            SessionProjection.initiating_user_id == user.id,
        )
        query = query.where(
            or_(
                Run.project_id.in_(authorized_project_string_ids),
                Run.session_id.in_(own_session_ids),
            )
        )
    if project_id:
        project = await db.get(Project, project_id)
        if not project or not await can_access_project(db, user, project):
            return _error(403, "forbidden", "Project is outside the authorized scope.")
        query = query.where(Run.project_id == project_id)
    if session_id:
        query = query.where(Run.session_id == session_id)
    if status:
        query = query.where(Run.status == status.value)
    if cursor:
        try:
            cursor_at, cursor_id = _decode_cursor(cursor)
        except ValueError:
            return _error(400, "invalid_cursor", "The runs cursor is invalid.")
        query = query.where(
            or_(Run.created_at < cursor_at, and_(Run.created_at == cursor_at, Run.id < cursor_id))
        )
    result = await db.execute(query.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1))
    runs = list(result.scalars().all())
    has_more = len(runs) > limit
    page = runs[:limit]
    observed = await run_liveness.observed_status_map(db, page)
    return RunListResponse(
        items=[_run_response(run, observed, language_for(user)) for run in page],
        next_cursor=_encode_cursor(page[-1]) if has_more and page else None,
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunDetailResponse,
)
async def get_run(
    run_id: str,
    user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_runtime_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> RunDetailResponse | JSONResponse:
    run = await db.scalar(select(Run).where(Run.tenant_id == tenant_id, Run.id == run_id))
    if not run:
        return _error(404, "not_found", "Run not found.")
    if not await _session_authorized(db, tenant_id=tenant_id, session_id=run.session_id, user=user):
        return _error(404, "not_found", "Run not found.")
    result = await db.execute(
        select(RunAttempt)
        .where(RunAttempt.tenant_id == tenant_id, RunAttempt.run_id == run_id)
        .order_by(RunAttempt.attempt_no)
    )
    event_count = await db.scalar(
        select(func.count())
        .select_from(ExecutionEvent)
        .where(ExecutionEvent.tenant_id == tenant_id, ExecutionEvent.run_id == run_id)
    )
    return RunDetailResponse(
        run=_run_response(run, await run_liveness.observed_status_map(db, [run]), language_for(user)),
        attempts=[_attempt_response(attempt) for attempt in result.scalars().all()],
        event_count=int(event_count or 0),
    )


@router.post("/runs/{run_id}/cancel", response_model=None)
async def cancel_run(
    run_id: str,
    payload: CancelRunRequest,
    user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_runtime_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> dict | JSONResponse:
    """Cancel the exact addressable local Harness process for a running Run."""
    from app.services.local_execution import cancel_local_run

    run = await db.scalar(select(Run).where(Run.tenant_id == tenant_id, Run.id == run_id))
    if not run or not await _session_authorized(
        db, tenant_id=tenant_id, session_id=run.session_id, user=user
    ):
        return _error(404, "not_found", "Run not found.")
    if run.status == RunStatus.CANCELLED.value:
        return {"runId": run.id, "status": RunStatus.CANCELLED.value}
    cancellable = {status.value for status in CANCELLABLE_RUN_STATUSES}
    if run.status not in cancellable:
        return _error(409, "conflict", f"Run is already terminal ({run.status}).")
    cancelled = await cancel_local_run(
        db,
        user=user,
        run=run,
        reason=payload.reason.strip(),
    )
    if not cancelled:
        return _error(
            409,
            "runtime_not_addressable",
            "The live runtime is not addressable by this user; its status was not falsified.",
        )
    return {"runId": run.id, "status": RunStatus.CANCELLED.value}


@router.get(
    "/sessions/{session_id}/events",
    response_model=EventPageResponse,
    response_model_exclude_none=True,
)
async def list_session_events(
    session_id: str,
    after_sequence: int = Query(0, alias="afterSequence", ge=0),
    limit: int = Query(200, ge=1, le=1000),
    run_id: str | None = Query(None, alias="runId", min_length=1),
    include_children: bool = Query(False, alias="includeChildren"),
    user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_runtime_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> EventPageResponse | JSONResponse:
    """一条 run 的事件；`includeChildren` 把它派出去的子节点也带上。

    ## 为什么需要这个开关（wangd 2026-08-11 试用）

        「这 literature 都结束了，然后开始 hypothesis 了，它还是在下面显示
          一大坨…按理说 literature 这一大坨运行的内容就应该是 over 了」

    会话视图按 `runId` 精确过滤，于是一轮里只看得到**编排器自己**的动作
    （197 条工具调用平铺成一坨 "Research activity"），literature / hypothesis
    真正做了什么，一条都取不回来 —— 它们是各自独立的 run。

    可"这一轮干了什么"在用户心里当然包含子节点做的事。按 run 切是**数据模型
    的内部划分**，不该原样泄漏成 UX。

    ## 为什么是一层不是递归

    子节点还能再派孙节点。这里只取直接子代：再往下就要递归查询，而 UI 上
    也没有对应的展示层级 —— 先把用得上的一层做对，需要更深时再说，别提前
    建一个没人走的通路。
    """
    if not await _session_authorized(db, tenant_id=tenant_id, session_id=session_id, user=user):
        return _error(404, "not_found", "Session not found.")
    if run_id is not None:
        matching_run = await db.scalar(
            select(Run.id).where(
                Run.tenant_id == tenant_id,
                Run.id == run_id,
                Run.session_id == session_id,
            )
        )
        if not matching_run:
            return _error(404, "not_found", "Run not found.")
    filters = [
        ExecutionEvent.tenant_id == tenant_id,
        ExecutionEvent.session_id == session_id,
        ExecutionEvent.sequence > after_sequence,
    ]
    if run_id is not None:
        filters.append(
            or_(ExecutionEvent.run_id == run_id, ExecutionEvent.parent_run_id == run_id)
            if include_children
            else ExecutionEvent.run_id == run_id
        )
    result = await db.execute(
        select(ExecutionEvent).where(*filters).order_by(ExecutionEvent.sequence).limit(limit + 1)
    )
    events = list(result.scalars().all())
    has_more = len(events) > limit
    page = events[:limit]
    next_sequence = page[-1].sequence if page else after_sequence
    return EventPageResponse(
        items=[_event_response(event) for event in page],
        after_sequence=after_sequence,
        next_after_sequence=next_sequence,
        has_more=has_more,
    )


@router.get("/sessions/{session_id}/observation", response_model=None)
async def get_session_observation(
    session_id: str,
    run_id: str | None = Query(None, alias="runId", min_length=1),
    after_sequence: int = Query(0, alias="afterSequence", ge=0),
    limit: int = Query(200, ge=1, le=1000),
    user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_runtime_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> dict | JSONResponse:
    """这次跑到底发生了什么 —— 让它可以被**机械证明**（#941）。

    六个维度，每个要么有值、要么显式 `unknown` / `unsupported`：产物的 active 视图
    与 supersession lineage、这一趟绑的预注册、run/attempt/job/closure 身份链、
    受管作业的终态与退出状态、清理状态、实际资源用量。

    **不建第二套状态**：产物读 `core.ledger`、任务绑定读 `core.task_contract`、
    run/attempt 读 runs 表、作业与清理读 durable events。

    **读不出来显式报错，不降成空结果**：工作区读不到、harness checkout 不在，
    都是 503 + 说清是哪一维；返回一个空列表会被读成"这次什么都没产出"。

    **分页不完整不能产生 PASS**：`complete=false` 时调用方手里的不是全貌。
    """
    if not await _session_authorized(db, tenant_id=tenant_id, session_id=session_id, user=user):
        return _error(404, "not_found", "Session not found.")

    from app.services import observation as obs

    runs = list((await db.scalars(
        select(Run)
        .where(Run.tenant_id == tenant_id, Run.session_id == session_id)
        .order_by(Run.created_at)
    )).all())
    if run_id is not None:
        runs = [r for r in runs if r.id == run_id or r.parent_run_id == run_id]
        if not runs:
            return _error(404, "not_found", "Run not found.")

    attempts_by_run: dict[str, list] = {}
    for attempt in (await db.scalars(
        select(RunAttempt)
        .where(RunAttempt.tenant_id == tenant_id,
               RunAttempt.run_id.in_([r.id for r in runs] or [""]))
        .order_by(RunAttempt.attempt_no)
    )).all():
        attempts_by_run.setdefault(attempt.run_id, []).append(attempt)

    # run_start 事件带着任务身份（#1080）—— 绑定从那里起，不从项目现状猜。
    run_start_by_run: dict[str, dict] = {}
    for event in (await db.scalars(
        select(ExecutionEvent)
        .where(ExecutionEvent.tenant_id == tenant_id,
               ExecutionEvent.session_id == session_id,
               ExecutionEvent.kind == "run.started")
        .order_by(ExecutionEvent.sequence)
    )).all():
        run_start_by_run[str(event.run_id)] = dict(event.payload or {})

    # 作业事件按页取：**分页不完整要说出来**。
    job_events = list((await db.scalars(
        select(ExecutionEvent)
        .where(ExecutionEvent.tenant_id == tenant_id,
               ExecutionEvent.session_id == session_id,
               ExecutionEvent.sequence > after_sequence,
               ExecutionEvent.kind.like("job.%"))
        .order_by(ExecutionEvent.sequence)
        .limit(limit + 1)
    )).all())
    has_more = len(job_events) > limit
    job_events = job_events[:limit]

    project_id = runs[0].project_id if runs else None
    deliverables: list[dict] | None = None
    task_bindings: dict[str, dict] = {}
    if project_id:
        from app.services.harness_contract import HarnessContractUnavailable
        from app.services.project_repository import (
            ProjectRepositoryError, get_project_repository, run_in_repository_thread,
        )

        try:
            from app.services.harness_contract import ledger_module, task_contract_module

            ledger = ledger_module()
            contracts = task_contract_module()
        except HarnessContractUnavailable as exc:
            return _error(503, "harness_unavailable",
                          f"产物与任务绑定这两维需要 harness checkout（HARNESS_ROOT）：{exc}")
        try:
            workspace = await run_in_repository_thread(
                get_project_repository().session_status, str(project_id), session_id)
            root = workspace.path
        except ProjectRepositoryError as exc:
            # **显式报错，不降成空**：空列表会被读成"这次什么都没产出"。
            return _error(503, "workspace_unavailable", f"读不到这个会话的工作区：{exc}")
        deliverables = await run_in_repository_thread(
            obs.deliverables_view, ledger, root)
        tasks_dir = root / "tasks"
        for run in runs:
            task_bindings[run.id] = obs.task_binding_view(
                contracts, tasks_dir, run_start_by_run.get(run.id))

    return {
        "schemaVersion": obs.SCHEMA_VERSION,
        "sessionId": session_id,
        # 分页不完整不能产生 PASS —— 调用方据此知道手里的不是全貌。
        "complete": not has_more,
        "afterSequence": after_sequence,
        "nextAfterSequence": job_events[-1].sequence if job_events else after_sequence,
        "deliverables": (
            deliverables if deliverables is not None
            else obs.unknown("这个会话还没有关联项目，读不到工作区账本")),
        "runs": [
            {
                "runId": run.id,
                "parentRunId": run.parent_run_id,
                "nodeType": run.node_type,
                "status": run.status,
                "attempts": [
                    {"attemptNo": int(a.attempt_no or 1), "status": a.status,
                     "startedAt": a.started_at, "endedAt": a.ended_at}
                    for a in attempts_by_run.get(run.id, [])
                ],
                "taskBinding": task_bindings.get(
                    run.id, obs.unknown("没有项目，读不到合同账本")),
                "resourceUsage": obs.resource_usage_view(run),
            }
            for run in runs
        ],
        "jobs": obs.job_view(job_events),
    }


@router.get("/sessions/{session_id}/events/stream", response_model=None)
async def stream_session_events(
    session_id: str,
    run_id: str = Query(..., alias="runId", min_length=1),
    after_sequence: int = Query(0, alias="afterSequence", ge=0),
    user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_runtime_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse | JSONResponse:
    """Replay and follow durable Run events without coupling Run life to SSE."""
    if not await _session_authorized(
        db, tenant_id=tenant_id, session_id=session_id, user=user
    ):
        return _error(404, "not_found", "Session not found.")
    matching_run = await db.scalar(
        select(Run.id).where(
            Run.tenant_id == tenant_id,
            Run.id == run_id,
            Run.session_id == session_id,
        )
    )
    if not matching_run:
        return _error(404, "not_found", "Run not found.")
    if db.bind is None:
        return _error(503, "service_unavailable", "Database is not available.")
    follower_sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    terminal_statuses = {status.value for status in STREAM_TERMINAL_RUN_STATUSES}

    async def event_generator():
        cursor = after_sequence
        revision = execution_event_revision()
        transient_queue = subscribe_run_transients(
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
        )

        def format_transient(event: dict) -> str:
            if event.get("type") == "token" and isinstance(event.get("text"), str):
                payload = {"runId": run_id, "text": event["text"]}
                return (
                    "event: token\n"
                    f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
                )
            payload = {
                "runId": run_id,
                "recovery": "assistant_message",
            }
            return (
                "event: token-gap\n"
                f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
            )

        try:
            while True:
                while not transient_queue.empty():
                    yield format_transient(transient_queue.get_nowait())

                async with follower_sessions() as follower_db:
                    events = list(
                        (
                            await follower_db.scalars(
                                select(ExecutionEvent)
                                .where(
                                    ExecutionEvent.tenant_id == tenant_id,
                                    ExecutionEvent.session_id == session_id,
                                    # 与分页读取同一条边界：这条 run 和它派出去
                                    # 的子节点。流里不带的话，子节点跑的事要等
                                    # 下一次全量拉取才出现 —— 正在跑的时候恰好
                                    # 是最需要看到它的时候。
                                    or_(
                                        ExecutionEvent.run_id == run_id,
                                        ExecutionEvent.parent_run_id == run_id,
                                    ),
                                    ExecutionEvent.sequence > cursor,
                                )
                                .order_by(ExecutionEvent.sequence)
                                .limit(200)
                            )
                        ).all()
                    )
                    run_status = await follower_db.scalar(
                        select(Run.status).where(
                            Run.tenant_id == tenant_id,
                            Run.id == run_id,
                            Run.session_id == session_id,
                        )
                    )
                    active_command = await follower_db.scalar(
                        select(Command.id).where(
                            Command.tenant_id == tenant_id,
                            Command.run_id == run_id,
                            # 「这条命令还没有结果」—— 读事实，不读状态词。
                            # 原先这里是 IN (accepted, queued, running)，而
                            # queued/running **从来没有被写过**：那是 == accepted
                            # 的三值拼法，而 accepted 的含义正是"还没收尾"。
                            Command.result.is_(None),
                            Command.error.is_(None),
                        )
                    )
                for event in events:
                    cursor = event.sequence
                    payload = _event_response(event).model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                    yield (
                        f"id: {cursor}\n"
                        "event: execution\n"
                        "data: "
                        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    )
                if len(events) == 200:
                    continue

                # A token may have arrived while the durable query was in flight.
                # Flush it before emitting the terminal boundary.
                while not transient_queue.empty():
                    yield format_transient(transient_queue.get_nowait())
                if run_status in terminal_statuses and active_command is None:
                    end = {
                        "runId": run_id,
                        "status": run_status,
                        "nextAfterSequence": cursor,
                    }
                    yield (
                        f"id: {cursor}\n"
                        "event: end\n"
                        f"data: {json.dumps(end, separators=(',', ':'))}\n\n"
                    )
                    break

                durable_wait = asyncio.create_task(
                    wait_for_execution_events(revision, timeout=15)
                )
                transient_wait = asyncio.create_task(transient_queue.get())
                activity_tasks = {durable_wait, transient_wait}
                done: set[asyncio.Task] = set()
                try:
                    done, _pending = await asyncio.wait(
                        activity_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in activity_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*activity_tasks, return_exceptions=True)
                if transient_wait in done:
                    yield format_transient(transient_wait.result())
                if durable_wait in done:
                    try:
                        revision = durable_wait.result()
                    except TimeoutError:
                        revision = execution_event_revision()
                        yield ": keepalive\n\n"
        finally:
            unsubscribe_run_transients(
                transient_queue,
                tenant_id=tenant_id,
                session_id=session_id,
                run_id=run_id,
            )

    return sse_response(event_generator(), release=db)


@router.get(
    "/sessions/{session_id}/decisions/current",
    response_model=DecisionListResponse,
    response_model_exclude_none=True,
)
async def list_current_decisions(
    session_id: str,
    user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_runtime_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> DecisionListResponse | JSONResponse:
    if not await _session_authorized(db, tenant_id=tenant_id, session_id=session_id, user=user):
        return _error(404, "not_found", "Session not found.")
    result = await db.execute(
        select(Decision)
        .where(
            Decision.tenant_id == tenant_id,
            Decision.session_id == session_id,
            Decision.status.in_(
                [DecisionStatus.PENDING.value, DecisionStatus.PARTIALLY_APPROVED.value]
            ),
            or_(Decision.expires_at.is_(None), Decision.expires_at > datetime.now(UTC)),
        )
        .order_by(Decision.created_at, Decision.id)
    )
    return DecisionListResponse(
        items=[_decision_response(decision) for decision in result.scalars().all()]
    )


@router.get(
    "/decisions/{decision_id}",
    response_model=DecisionResponse,
    response_model_exclude_none=True,
)
async def get_decision(
    decision_id: str,
    user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_runtime_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> DecisionResponse | JSONResponse:
    decision = await db.scalar(
        select(Decision).where(
            Decision.tenant_id == tenant_id,
            Decision.id == decision_id,
        )
    )
    if not decision:
        return _error(404, "not_found", "Decision not found.")
    if not await _session_authorized(
        db, tenant_id=tenant_id, session_id=decision.session_id, user=user
    ):
        return _error(404, "not_found", "Decision not found.")
    return _decision_response(decision)


@router.post(
    "/decisions/{decision_id}/responses",
    response_model=DecisionResponse,
    response_model_exclude_none=True,
)
async def respond_to_decision(
    decision_id: str,
    payload: DecisionRespondRequest,
    user: User = Depends(get_current_user),
    tenant_id: str = Depends(get_runtime_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> DecisionResponse | JSONResponse:
    """Record one authority response with an explicit projection CAS token."""
    decision = await db.scalar(
        select(Decision).where(
            Decision.tenant_id == tenant_id,
            Decision.id == decision_id,
        )
    )
    if not decision or not await _session_authorized(
        db, tenant_id=tenant_id, session_id=decision.session_id, user=user
    ):
        return _error(404, "not_found", "Decision not found.")
    project = await db.get(Project, decision.project_id)
    if not project:
        return _error(404, "not_found", "Decision not found.")
    if not await has_project_capability(db, user, project, "review_decision"):
        return _error(
            403,
            "decision_respond_forbidden",
            "Project role does not allow Decision responses.",
        )

    try:
        updated = await ExecutionIngestService().respond_to_decision(
            db,
            tenant_id=tenant_id,
            decision_id=decision_id,
            actor_user_id=user.id,
            choice_id=payload.choice_id,
            expected_accepted_response_count=payload.expected_accepted_response_count,
            has_decision_respond_capability=True,
            actor_authority_subjects={user.id},
        )
    except DecisionResponseRejectedError as exc:
        if exc.code in {"decision_respond_forbidden", "decision_authority_forbidden"}:
            return _error(403, exc.code, str(exc))
        if exc.code == "invalid_decision_choice":
            return _error(422, exc.code, str(exc))
        return _error(409, exc.code, str(exc))
    return _decision_response(updated)

"""Wire schemas for the frozen execution read API."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.execution import (
    AttemptStatus,
    DecisionAuthorityType,
    DecisionStatus,
    EventOrigin,
    EventVisibility,
    RunStatus,
)


def _camel_case(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ExecutionAPIModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel_case,
        extra="forbid",
        from_attributes=True,
        populate_by_name=True,
    )


class RunUsageResponse(ExecutionAPIModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float | None = Field(..., ge=0, allow_inf_nan=False)
    currency: str | None = None
    coverage: Literal["complete", "partial"]


class RunResponse(ExecutionAPIModel):
    id: str
    tenant_id: str
    workspace_id: str
    project_id: str
    session_id: str
    parent_run_id: str | None = None
    node_type: str | None = None
    status: RunStatus
    usage: RunUsageResponse
    retry_count: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    summary: dict[str, Any] | None = None
    #: 这条 run 的局面 —— 与会话那一份出自同一个 builder（execution_view.build）。
    #: `status` 是 13 值枚举的原始投影，留给尚未迁完的读点；客户端**只该读这个**。
    view: dict[str, Any] | None = None


class RunAttemptResponse(ExecutionAPIModel):
    id: str
    tenant_id: str
    workspace_id: str
    project_id: str
    session_id: str
    run_id: str
    attempt_no: int
    status: AttemptStatus
    worker_id: str | None = None
    lease_until: datetime | None = None
    heartbeat_at: datetime | None = None
    exit_reason: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None


class RunListResponse(ExecutionAPIModel):
    items: list[RunResponse]
    next_cursor: str | None = None


class RunDetailResponse(ExecutionAPIModel):
    run: RunResponse
    attempts: list[RunAttemptResponse]
    event_count: int = Field(ge=0)


class ExecutionEventSourceResponse(ExecutionAPIModel):
    raw_event: str | None = None
    file_ref: str | None = None
    byte_offset: int | None = None
    derived_from: list[str] | None = None


class ExecutionEventResponse(ExecutionAPIModel):
    schema_version: int = 1
    id: str
    sequence: int
    at: datetime
    workspace_id: str
    project_id: str
    session_id: str
    run_id: str | None = None
    parent_run_id: str | None = None
    origin: EventOrigin
    source: ExecutionEventSourceResponse
    kind: str
    visibility: EventVisibility
    payload: dict[str, Any]


class EventPageResponse(ExecutionAPIModel):
    items: list[ExecutionEventResponse]
    after_sequence: int
    next_after_sequence: int
    has_more: bool


class DecisionAuthorityResponse(ExecutionAPIModel):
    authority_type: DecisionAuthorityType
    authority_subjects: list[str]
    required_approval_count: int
    action_set_version: str = Field(min_length=1)
    policy_snapshot_id: str = Field(min_length=1)
    expires_at: datetime | None = None

    @field_validator("authority_subjects")
    @classmethod
    def subjects_must_be_unique(cls, value: list[str]) -> list[str]:
        if (
            not value
            or any(not subject.strip() for subject in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError("authoritySubjects must be non-empty and unique")
        return value

    @field_validator("action_set_version", "policy_snapshot_id")
    @classmethod
    def versions_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Decision authority versions cannot be blank")
        return value


class DecisionChoiceResponse(ExecutionAPIModel):
    choice_id: str
    label: str
    description: str | None = None
    consequence: str | None = None
    reversible: bool | None = None


class DecisionResponse(ExecutionAPIModel):
    id: str
    tenant_id: str
    workspace_id: str
    project_id: str
    session_id: str
    run_id: str
    attempt_no: int | None = None
    status: DecisionStatus
    subtype: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    choices: list[DecisionChoiceResponse]
    recommended_choice_id: str | None = None
    selected_choice_id: str | None = None
    #: 已经收到几份应答 —— 响应里的**计算值**（len(accepted_responses)），
    #: 不再是库里另存的一个计数器：一份事实两处记就会分叉。
    accepted_response_count: int
    authority: DecisionAuthorityResponse
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None


class DecisionListResponse(ExecutionAPIModel):
    items: list[DecisionResponse]


class DecisionRespondRequest(ExecutionAPIModel):
    """CAS-guarded response against one immutable Decision snapshot."""

    choice_id: str = Field(min_length=1, max_length=128)
    expected_accepted_response_count: int = Field(ge=0)

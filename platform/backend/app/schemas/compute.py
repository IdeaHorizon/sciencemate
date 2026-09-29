"""Honest local-development compute inventory contract."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

HealthStatus = Literal["online", "offline", "unknown"]


class HealthCheckOut(BaseModel):
    name: str
    status: HealthStatus
    detail: str


class HealthOut(BaseModel):
    status: HealthStatus
    checks: list[HealthCheckOut]


class CpuInventoryOut(BaseModel):
    status: HealthStatus
    logical_cores: int | None


class MemoryInventoryOut(BaseModel):
    status: HealthStatus
    total_bytes: int | None
    available_bytes: int | None


class StorageInventoryOut(BaseModel):
    status: HealthStatus
    total_bytes: int | None
    free_bytes: int | None


class GpuDeviceOut(BaseModel):
    id: str
    name: str
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    utilization_percent: int | None


class GpuInventoryOut(BaseModel):
    status: HealthStatus
    count: int | None
    devices: list[GpuDeviceOut]


class ComputeNodeOut(BaseModel):
    id: Literal["local-app-server"]
    name: str
    kind: Literal["local"]
    status: HealthStatus
    operating_system: str
    architecture: str
    cpu: CpuInventoryOut
    memory: MemoryInventoryOut
    storage: StorageInventoryOut
    gpu: GpuInventoryOut


class SchedulerOut(BaseModel):
    id: Literal["local-process"]
    kind: Literal["in_process"]
    status: HealthStatus
    supports_queue: Literal[False]
    queue_depth: None
    active_sessions: int


class ComputeCapacityOut(BaseModel):
    status: HealthStatus
    cpu_logical_cores: int | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    storage_total_bytes: int | None
    storage_free_bytes: int | None
    gpu_count: int | None
    gpu_memory_total_bytes: int | None
    gpu_memory_available_bytes: int | None


class RecentJobOut(BaseModel):
    id: str
    name: str
    status: Literal["queued", "running", "completed", "failed", "cancelled", "unknown"]
    submitted_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None


class RecentJobsOut(BaseModel):
    supported: Literal[False]
    items: list[RecentJobOut]


class ComputeInventoryOut(BaseModel):
    scope: Literal["local_development"]
    observed_at: datetime
    health: HealthOut
    nodes: list[ComputeNodeOut]
    schedulers: list[SchedulerOut]
    capacity: ComputeCapacityOut
    recent_jobs: RecentJobsOut
    limitations: list[str]

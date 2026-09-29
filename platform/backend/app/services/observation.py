"""只读观测面 —— 让「这次跑到底发生了什么」可以被**机械证明**（#941）。

## 为什么需要它

Experiment 的 UI benchmark 目前只能通过公开 API 可靠取得：project/session/run graph、
durable events、catalog 的 frozen/owner/producedByRunId，以及 repository 文件字节。
**证明不了**的有六样：产物的 active/superseded 最新有效视图；这一趟绑的是哪份预注册；
run/attempt/job/closure 的完整身份链；受管作业的真实终态与退出状态；cancel/finalize
之后物理清理做没做；实际资源用量。

于是 evaluator 只能把这些维度记成 `NOT_OBSERVABLE`，而**不能**从 chat 文案、catalog
里有没有这一条、退出码摘要或私有工作区去推断 PASS —— 从那些地方推出来的 PASS，和
"真的做到了"长得一模一样。

## 三条纪律

**一、复用现有权威，不建第二套状态。** 产物读 `core.ledger` 的 head（路径即身份、
head 即当前）；任务与预注册绑定读 `core.task_contract`（只追加、按 digest 精确取）；
run/attempt 读 runs / run_attempts 表；作业与清理读 durable events。这里一行状态都不存。

**二、读不出来要显式说 `unknown`，不许降成空。** 空结果和"这件事没发生"长得一样，
而它们是两回事。每个答不上来的维度都带一个 `unknown` 和一句为什么。

**三、分页不完整不能产生 PASS。** 每一页都带 `complete`：还有下一页时它是 `false`，
调用方据此知道自己手里的不是全貌。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: 一个维度答不上来时的形状。**它不是 `None`** —— 缺席要看得见。
UNKNOWN = "unknown"
UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Unavailable(Exception):
    """观测面自己读不出来 —— **显式报错，不降成空结果**。"""

    dimension: str
    reason: str

    def __str__(self) -> str:      # pragma: no cover - 供日志与 HTTP detail 用
        return f"{self.dimension}: {self.reason}"


def unknown(reason: str) -> dict[str, Any]:
    """某个维度这次答不上来 —— 说出来，并说清为什么。"""
    return {"state": UNKNOWN, "reason": reason}


def unsupported(reason: str) -> dict[str, Any]:
    """这台部署根本提供不了这个维度（不是"这次没取到"）。"""
    return {"state": UNSUPPORTED, "reason": reason}


def deliverables_view(ledger_module: Any, root: Any) -> list[dict[str, Any]]:
    """产物的 **active 视图** + supersession lineage + 内容 SHA-256 + 产出方。

    权威是 `core.ledger` 的 head：路径即身份、head 即当前。`retired` 的不算 active
    —— 此前调用方只能看 catalog 里"有没有这一条"，而那回答不了"它还是不是当前有效
    的那一份"。

    lineage 用 `saves`（版本升序的全部 save 行）逐版展开：每一版的 sha256 与它的
    前驱，于是"这一版把哪一版顶掉了"是读得出来的，不用去比时间戳猜。
    """
    store = ledger_module.workspace_store(root)
    heads = store.heads(include_retired=True)
    rows: list[dict[str, Any]] = []
    for head in heads.values():
        saves = list(getattr(head, "saves", None) or [])
        lineage = [
            {
                "version": int(item.get("version") or 0),
                "contentSha256": str(item.get("content_hash") or ""),
                "supersedes": str(item.get("prev_content_hash") or "") or None,
                "at": str(item.get("at") or ""),
            }
            for item in saves
        ]
        rows.append({
            "artifactId": head.artifact_id,
            "type": head.artifact_type,
            "name": head.name,
            "path": head.path,
            # active 与 superseded 是**两个不同的问题**，各答各的：
            # `active` 说的是这份身份还在不在（retired 即不在）；
            # `supersededVersions` 说的是它自己被改写过几次。
            "active": not bool(getattr(head, "retired", False)),
            "version": int(head.version or 0),
            "contentSha256": str(head.sha256 or ""),
            "frozen": bool(head.frozen),
            "frozenVersion": int(head.frozen_version or 0),
            "frozenSha256": str(head.frozen_sha256 or ""),
            "producedByNodeType": head.produced_by_node_type,
            "producedByRunId": head.produced_by_run_id,
            "lineage": lineage,
            "supersededVersions": max(0, len(lineage) - 1),
        })
    rows.sort(key=lambda r: (r["type"], r["artifactId"]))
    return rows


def task_binding_view(contract_module: Any, tasks_dir: Any,
                      run_start_payload: dict[str, Any] | None) -> dict[str, Any]:
    """这一趟绑的是哪份预注册 —— 从**派发合同**读，不从项目现状猜。

    身份在 `run_start` 事件上（`task_instance_uuid` + `task_contract_digest`，#1080），
    合同正文在只追加的合同账本里（按 digest 精确取，#1097）。两者缺一都答 `unknown`：
    「没绑」「绑了但读不出来」「还没安排」是三件事，压成一个空值就分不开了。
    """
    payload = dict(run_start_payload or {})
    uuid = str(payload.get("task_instance_uuid") or "")
    digest = str(payload.get("task_contract_digest") or "")
    if not uuid or not digest:
        return unknown(
            "这个 run 的 run_start 上没有任务身份 —— 它是 #1080 之前派发的，"
            "或者派发方没带 task_instance_uuid")
    log = contract_module.TaskContractLog(tasks_dir)
    revision = log.get(uuid, digest)
    if revision is None:
        return unknown(
            f"合同账本上找不到 {uuid[:12]}…@{digest[:12]}… —— 派发引用了一份不存在的授权")
    assignment = revision.assignment
    return {
        "state": "known",
        "taskInstanceUuid": uuid,
        "contractRevision": int(revision.revision),
        "contractDigest": revision.digest,
        "targetNode": revision.target_node,
        "intendedUse": revision.intended_use,
        # 三态照原样端出去：exact_bound / explicit_none / pending_assignment。
        # **缺席不等于「明确不绑」**，所以这里不做任何折叠。
        "preregAssignment": {
            "kind": revision.assignment_kind,
            **({"artifactId": assignment.artifact_id,
                "version": assignment.version,
                "contentHash": assignment.content_hash}
               if assignment is not None and assignment.artifact_id else {}),
            **({"reason": assignment.reason}
               if assignment is not None and assignment.reason else {}),
        },
    }


#: 受管作业的事实来自 durable events —— 平台手里**只有**这些，所以答案也只能到这里。
_JOB_EVENT_KINDS = ("job.submitted", "job.finished", "job.cancelled", "job.cleanup")


def job_view(events: list[Any]) -> list[dict[str, Any]]:
    """受管作业的身份链、终态、退出状态、stdout/stderr 引用、清理状态。

    **未知一律显式**：作业提交过但没有任何终态事件时，`terminal` 是
    `unknown("没有终态事件")`，而不是 `None`、也不是"还在跑" —— 后两种都会被读成
    一个确定的答案。清理同理：`cleanup` 只有在真有一条清理事件时才是 done。
    """
    by_job: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = dict(getattr(event, "payload", None) or {})
        job_id = str(payload.get("jobId") or payload.get("job_id") or "")
        if not job_id:
            continue
        row = by_job.setdefault(job_id, {
            "jobId": job_id,
            "runId": str(getattr(event, "run_id", "") or ""),
            "scheduler": payload.get("scheduler"),
            "terminal": unknown("没有终态事件"),
            "exitStatus": unknown("没有终态事件"),
            "stdoutRef": None,
            "stderrRef": None,
            "closureReceipt": None,
            "cleanup": unknown("没有清理事件"),
        })
        kind = str(getattr(event, "kind", ""))
        if payload.get("scheduler"):
            row["scheduler"] = payload["scheduler"]
        if payload.get("stdoutPath"):
            row["stdoutRef"] = str(payload["stdoutPath"])
        if payload.get("stderrPath"):
            row["stderrRef"] = str(payload["stderrPath"])
        if payload.get("closureId"):
            row["closureReceipt"] = {
                "closureId": str(payload["closureId"]),
                "contentHash": str(payload.get("contentHash") or "") or None,
            }
        if kind in {"job.finished", "job.cancelled"}:
            row["terminal"] = {"state": "known",
                               "value": str(payload.get("status") or kind.split(".")[-1])}
            row["exitStatus"] = (
                {"state": "known", "value": int(payload["exitCode"])}
                if isinstance(payload.get("exitCode"), int)
                else unknown("终态事件里没有退出码"))
        if kind == "job.cleanup":
            row["cleanup"] = {"state": "known",
                              "value": str(payload.get("status") or "done")}
    return sorted(by_job.values(), key=lambda r: r["jobId"])


def resource_usage_view(run: Any) -> dict[str, Any]:
    """实际资源用量。

    token 用量平台有（`usage.updated` 事件已折进 run）；**CPU / 内存没有** ——
    原生执行器不给 per-scope 计量，没有任何东西能诚实地回答它。所以它是
    `unsupported`（这台部署提供不了），不是 `unknown`（这次没取到）：两者对读的人
    是不同的下一步，一个该去查，一个不必。
    """
    return {
        "tokens": {
            "state": "known",
            "prompt": int(getattr(run, "prompt_tokens", 0) or 0),
            "completion": int(getattr(run, "completion_tokens", 0) or 0),
            "total": int(getattr(run, "total_tokens", 0) or 0),
        },
        "cpu": unsupported("原生执行器不提供 per-scope CPU 计量"),
        "memory": unsupported("原生执行器不提供 per-scope 内存计量"),
    }

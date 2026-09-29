"""观测面：要么有值，要么**显式说答不上来**（#941）。

## 为什么需要它

Experiment 的 UI benchmark 只能通过公开 API 可靠取得 project/session/run graph、
durable events、catalog 的 frozen/owner/producedByRunId 和文件字节。证明不了的六样：
产物的 active/superseded 最新有效视图、这一趟绑的预注册、run/attempt/job/closure
身份链、受管作业的真实终态与退出状态、cancel/finalize 后的物理清理、实际资源用量。

于是 evaluator 只能把它们记成 NOT_OBSERVABLE —— **而不能**从 chat 文案、catalog
里有没有这一条、退出码摘要或私有工作区推断 PASS。从那些地方推出来的 PASS，和
"真的做到了"长得一模一样。

## 判据

缺席必须看得见：`unknown`（这次没取到）与 `unsupported`（这台部署提供不了）是
两个答案，对读的人是两个不同的下一步。空列表 / None 一律不行 —— 它和"这件事没
发生"长得一样。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import observation as obs


# ── 缺席的形状 ─────────────────────────────────────────────────────────────


def test_unknown_and_unsupported_are_two_answers() -> None:
    missed = obs.unknown("这次没取到")
    cannot = obs.unsupported("这台部署提供不了")
    assert missed["state"] == "unknown" and missed["reason"]
    assert cannot["state"] == "unsupported" and cannot["reason"]
    assert missed["state"] != cannot["state"], (
        "「这次没取到」和「根本提供不了」压成一个值 —— 读的人不知道该不该去查")


def test_cpu_and_memory_are_unsupported_not_unknown() -> None:
    """原生执行器不给 per-scope 计量。说 unknown 会让人去查一个不存在的东西。"""
    usage = obs.resource_usage_view(
        SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    assert usage["tokens"]["total"] == 15
    assert usage["cpu"]["state"] == "unsupported"
    assert usage["memory"]["state"] == "unsupported"


# ── 产物：active 视图 + lineage + sha256 ───────────────────────────────────


class _Head:
    def __init__(self, artifact_id, *, retired=False, saves=None, version=2):
        self.artifact_id = artifact_id
        self.artifact_type = "clean_results"
        self.name = artifact_id
        self.path = f"experiment/{artifact_id}.md"
        self.version = version
        self.sha256 = "hash-v2"
        self.created_at = "2026-09-08T00:00:00+00:00"
        self.provenance = {}
        self.produced_by_node_type = "experiment"
        self.produced_by_run_id = "run-child"
        self.metadata = {}
        self.prev_sha256 = "hash-v1"
        self.frozen = True
        self.frozen_at = "2026-09-08T01:00:00+00:00"
        self.frozen_version = version
        self.frozen_sha256 = "hash-v2"
        self.retired = retired
        self.saves = saves if saves is not None else [
            {"version": 1, "content_hash": "hash-v1", "at": "t1"},
            {"version": 2, "content_hash": "hash-v2", "prev_content_hash": "hash-v1",
             "at": "t2"},
        ]


class _Ledger:
    def __init__(self, heads):
        self._heads = heads

    def workspace_store(self, root):
        del root
        return self

    def heads(self, *, include_retired=False):
        del include_retired
        return {h.artifact_id: h for h in self._heads}


def test_a_superseded_artifact_is_not_reported_as_active() -> None:
    rows = obs.deliverables_view(
        _Ledger([_Head("a_live"), _Head("a_retired", retired=True)]), root=None)
    live = next(r for r in rows if r["artifactId"] == "a_live")
    dead = next(r for r in rows if r["artifactId"] == "a_retired")
    assert live["active"] is True
    assert dead["active"] is False, "退役的产物被当成当前有效的那一份"


def test_the_lineage_says_which_version_superseded_which() -> None:
    """"这一版把哪一版顶掉了"要读得出来，不靠比时间戳猜。"""
    (row,) = obs.deliverables_view(_Ledger([_Head("a")]), root=None)
    assert row["contentSha256"] == "hash-v2"
    assert row["supersededVersions"] == 1
    assert row["lineage"][1]["supersedes"] == "hash-v1"
    assert row["lineage"][0]["supersedes"] is None


# ── 任务绑定：三态照原样端出去 ─────────────────────────────────────────────


class _Revision:
    def __init__(self, kind, assignment=None):
        self.revision = 2
        self.digest = "d2"
        self.target_node = "experiment"
        self.intended_use = "confirmatory"
        self.assignment_kind = kind
        self.assignment = assignment


class _Contracts:
    def __init__(self, revision):
        self._revision = revision

    def TaskContractLog(self, tasks_dir):  # noqa: N802 - 镜像真模块的名字
        del tasks_dir
        return self

    def get(self, uuid, digest):
        return self._revision if self._revision else None


def _binding(revision, payload):
    return obs.task_binding_view(_Contracts(revision), tasks_dir=None,
                                 run_start_payload=payload)


def test_a_run_without_task_identity_says_so() -> None:
    got = _binding(_Revision("exact_bound"), {})
    assert got["state"] == "unknown"
    assert "没有任务身份" in got["reason"]


def test_a_dangling_contract_is_not_reported_as_unbound() -> None:
    """合同读不出来 ≠ 没绑。压成一个空值，两件事就分不开了。"""
    got = _binding(None, {"task_instance_uuid": "u1", "task_contract_digest": "d2"})
    assert got["state"] == "unknown"
    assert "找不到" in got["reason"]


def test_pending_assignment_is_not_folded_into_explicit_none() -> None:
    """三态照原样端出去 —— 折叠等于替上游做了那个决定。"""
    payload = {"task_instance_uuid": "u1", "task_contract_digest": "d2"}
    pending = _binding(_Revision("pending_assignment"), payload)
    assert pending["preregAssignment"]["kind"] == "pending_assignment"

    bound = _binding(
        _Revision("exact_bound", SimpleNamespace(
            artifact_id="pre_registration__H1", version="2",
            content_hash="h", reason="")),
        payload)
    assert bound["preregAssignment"]["kind"] == "exact_bound"
    assert bound["preregAssignment"]["artifactId"] == "pre_registration__H1"

    none = _binding(
        _Revision("explicit_none", SimpleNamespace(
            artifact_id="", version="", content_hash="", reason="这趟只是装环境")),
        payload)
    assert none["preregAssignment"]["kind"] == "explicit_none"
    assert none["preregAssignment"]["reason"] == "这趟只是装环境"


# ── 作业：没有终态事件就不许看起来像有 ─────────────────────────────────────


def _event(kind, payload, run_id="run-child"):
    return SimpleNamespace(kind=kind, payload=payload, run_id=run_id)


def test_a_submitted_job_without_a_terminal_event_is_unknown() -> None:
    (row,) = obs.job_view([_event("job.submitted",
                                  {"jobId": "hf-job-1", "scheduler": "local"})])
    assert row["terminal"]["state"] == "unknown"
    assert row["exitStatus"]["state"] == "unknown"
    assert row["cleanup"]["state"] == "unknown", (
        "没有清理事件却报成已清理 —— 「未知」和「完成」必须分得开（验收第 3 条）")


def test_a_finished_job_carries_its_exit_status_and_refs() -> None:
    (row,) = obs.job_view([
        _event("job.submitted", {"jobId": "hf-job-1", "scheduler": "local",
                                 "stdoutPath": "jobs/a.out", "stderrPath": "jobs/a.err"}),
        _event("job.finished", {"jobId": "hf-job-1", "status": "exited", "exitCode": 0,
                                "closureId": "c1", "contentHash": "h1"}),
        _event("job.cleanup", {"jobId": "hf-job-1", "status": "done"}),
    ])
    assert row["terminal"] == {"state": "known", "value": "exited"}
    assert row["exitStatus"] == {"state": "known", "value": 0}
    assert row["stdoutRef"] == "jobs/a.out" and row["stderrRef"] == "jobs/a.err"
    assert row["closureReceipt"]["closureId"] == "c1"
    assert row["cleanup"] == {"state": "known", "value": "done"}


def test_a_terminal_without_an_exit_code_says_so() -> None:
    (row,) = obs.job_view([
        _event("job.submitted", {"jobId": "hf-job-1"}),
        _event("job.finished", {"jobId": "hf-job-1", "status": "exited"}),
    ])
    assert row["terminal"]["state"] == "known"
    assert row["exitStatus"]["state"] == "unknown"


def test_two_jobs_in_one_session_do_not_cross_wires() -> None:
    """验收第 1 条：同一 session 的跨 run/cross-attempt 收据不许串线。"""
    rows = obs.job_view([
        _event("job.submitted", {"jobId": "job-a"}, run_id="run-1"),
        _event("job.submitted", {"jobId": "job-b"}, run_id="run-2"),
        _event("job.finished", {"jobId": "job-b", "status": "exited", "exitCode": 3},
               run_id="run-2"),
    ])
    by_id = {r["jobId"]: r for r in rows}
    assert by_id["job-a"]["terminal"]["state"] == "unknown"
    assert by_id["job-b"]["exitStatus"]["value"] == 3
    assert by_id["job-a"]["runId"] == "run-1" and by_id["job-b"]["runId"] == "run-2"


# ── 这条路必须真的接上 ─────────────────────────────────────────────────────


def test_the_endpoint_exists_and_is_read_only() -> None:
    from app.api.v1.execution import router

    routes = {
        (getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", set()) or ())))
        for r in router.routes
    }
    observation = [p for p, m in routes if p.endswith("/observation")]
    assert observation, "观测面没有公开入口 —— 它只存在于代码里"
    methods = next(m for p, m in routes if p.endswith("/observation"))
    assert set(methods) <= {"GET", "HEAD"}, f"观测面不是只读的：{methods}"


def test_it_reads_the_existing_authorities_not_a_second_copy() -> None:
    """不建第二套状态：产物读 core.ledger，任务绑定读 core.task_contract。"""
    import inspect

    from app.api.v1 import execution

    src = inspect.getsource(execution.get_session_observation)
    assert "ledger_module" in src and "task_contract_module" in src
    assert "workspace_unavailable" in src and "harness_unavailable" in src, (
        "读不出来时降成了空结果 —— 空列表会被读成「这次什么都没产出」")
    assert '"complete"' in src, "分页不完整不能产生 PASS，那就得有一个说法"

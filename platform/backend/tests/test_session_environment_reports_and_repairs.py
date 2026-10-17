"""环境事实、修复提案、重复即升级、条幅接口（RFC_WORKER_START_IS_NOT_A_GATE §3.2 / §3.4 / §3.6，窄单 B / C）。

- 探针读得出所有者 / 权限 / 种类；读不出来写 null + 原因，不省略字段。
- 提案按表：权限形状的「读不到」→ 恢复数据根权限；真冲突 → 只有给管理员的话；别的 → 没有提案。
- 同一会话同一 code 连续第二次 → repeated=2、escalated、doctor 在；中间夹一次别的 code 从头数。
- GET 读活 worker 的报告，没有就读最近一条 run 记下的；POST 只认服务端重算的提案，没有的 409。
"""
from __future__ import annotations

# ruff: noqa: F811  （`runtime_client` 是借来的 pytest fixture，参数名遮住导入名是它的正常长相）
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import settings, the_data_root
from app.models.execution import Run, RunAttempt, SessionProjection
from app.models.model_backend import ModelBackendConfig
from app.models.project import Project
from app.models.user import User
from app.services import environment_facts as facts
from app.services import harness_sessions as hs
from app.services import session_environment as env
from tests.test_local_runtime_api import GROUP, _headers, _token, runtime_client  # noqa: F401


# ── 事实 ─────────────────────────────────────────────────────────────────────


def test_probe_reads_a_file_and_names_a_missing_one(tmp_path):
    target = tmp_path / "marker.json"
    target.write_text("{}", encoding="utf-8")
    got = facts.probe_path(target)
    assert got["exists"] is True and got["kind"] == "file"
    assert set(got) >= {"path", "exists", "kind", "owner", "mode", "acl", "reparse_tag", "error"}
    if sys.platform != "win32":
        assert got["mode"].startswith("0o") and f"uid {os.getuid()}" in got["owner"]
    missing = facts.probe_path(tmp_path / "nope")
    assert missing["exists"] is False and missing["kind"] == "missing" and missing["error"] is None
    assert facts.probe_path(None)["error"] == "no path"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="chmod 对 root / Windows 不生效")
def test_probe_keeps_the_denial_as_a_fact(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "inner").write_text("x", encoding="utf-8")
    locked.chmod(0)
    try:
        got = facts.probe_path(locked / "inner")
    finally:
        locked.chmod(0o700)
    assert got["exists"] is None and "PermissionError" in got["error"]


def test_token_names_this_process():
    token = facts.probe_token()
    assert token["user"]
    assert set(token) == {"user", "uid", "integrity", "error"}


# ── 提案 ─────────────────────────────────────────────────────────────────────


def test_a_permission_shaped_failure_proposes_resetting_the_data_root(tmp_path):
    got = facts.repair_proposal("home_marker_unreadable", error="PermissionError: [WinError 5] 拒绝访问。",
                                data_root=tmp_path)
    assert got["kind"] == "reset_data_root_permissions" and got["needs_consent"] is True
    assert str(tmp_path) in got["argv"] and got["command"]
    if sys.platform != "win32":
        assert got["argv"][0] == "chmod"


def test_a_conflict_has_no_command_only_advice(tmp_path):
    got = facts.repair_proposal("tenant_home_conflict", error="home_dir is already bound", data_root=tmp_path)
    assert got["kind"] == "needs_administrator" and got["argv"] is None


def test_a_non_permission_failure_has_no_proposal(tmp_path):
    assert facts.repair_proposal("home_marker_unreadable", error="JSONDecodeError: Expecting value",
                                 data_root=tmp_path) is None
    assert facts.repair_proposal("something_else", error="PermissionError", data_root=tmp_path) is None


def test_enrich_adds_facts_holders_token_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "own_holders", lambda p, s: [{"pid": 4242, "spawn_token": "abcd", "alive": False}])
    warning = {"type": "environment_warning", "code": "state_dir_unavailable", "severity": "degraded",
               "path": str(tmp_path / "state"), "error": "PermissionError: [Errno 13] Permission denied",
               "consequences": ["x"]}
    got = facts.enrich_warning(warning, project_id="p", session_id="s", data_root=tmp_path)
    assert got["facts"]["exists"] is False
    assert got["holders"][0]["pid"] == 4242
    assert got["token"]["user"]
    assert got["repair"]["kind"] == "reset_data_root_permissions"
    again = facts.enrich_warning(got, project_id="p", session_id="s", data_root=tmp_path)
    assert again is got


def test_a_hard_failure_with_a_path_becomes_a_fatal_event(tmp_path):
    exc = hs.HarnessSessionError("home_dir is already bound to a different identity",
                                 code="tenant_home_conflict", error_type="IdentityConflict",
                                 details={"path": str(tmp_path), "expected": {"tenant_id": "a"}})
    got = facts.hard_failure_warning(exc, project_id="p", session_id="s", data_root=tmp_path)
    assert got["severity"] == "fatal" and got["path"] == str(tmp_path)
    assert got["facts"]["kind"] == "dir"
    assert got["repair"]["kind"] == "needs_administrator"
    assert got["details"] == {"expected": {"tenant_id": "a"}}
    assert facts.hard_failure_warning(RuntimeError("x"), project_id="p", session_id="s", data_root=tmp_path) is None


# ── 重复即升级 + 接口 ─────────────────────────────────────────────────────────


async def _seed(db, *, run_id: str, session_id: str | None = None, failure_code: str | None = None,
                environment: dict | None = None) -> Run:
    project = await db.scalar(select(Project).where(Project.name == "Researcher Project"))
    user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
    backend = await db.scalar(select(ModelBackendConfig).where(
        ModelBackendConfig.scope_kind == "institution", ModelBackendConfig.provider == "demo"))
    if session_id is None:
        session_id = uuid4().hex
        db.add(SessionProjection(
            tenant_id=settings.runtime_tenant_id, workspace_id=GROUP, project_id=str(project.id),
            session_id=session_id, initiating_user_id=user.id, title=run_id,
            git_base_commit_sha="0" * 40, created_by_user_id=user.id, model_backend_id=backend.id,
            policy_snapshot_id="policy-snapshot-1", research_settings_snapshot_id="research-snapshot-1",
            research_settings_snapshot={"snapshot_id": "research-snapshot-1"},
            knowledge_read_watermark={"claim_sequence": 1},
        ))
    summary: dict = {"executionKernel": "formal_harness"}
    if failure_code:
        summary["failure"] = {"code": failure_code, "title": "t", "message": "m"}
    if environment is not None:
        summary["environment"] = environment
    run = Run(id=run_id, tenant_id=settings.runtime_tenant_id, workspace_id=GROUP, project_id=str(project.id),
              session_id=session_id, status="failed" if failure_code else "completed", summary=summary)
    db.add(run)
    await db.flush()
    return run


@pytest.mark.asyncio
async def test_the_same_code_twice_in_a_row_escalates_to_a_doctor(runtime_client) -> None:
    client, factory = runtime_client
    async with factory() as db:
        first = await _seed(db, run_id="run_env_1", failure_code="execution_failed")
        second = await _seed(db, run_id="run_env_2", session_id=first.session_id, failure_code="execution_failed")
        await db.commit()
        got = await env.environment_for_run(db, project_id=first.project_id, session_id=first.session_id,
                                            run_id=second.id, failure={"code": "execution_failed"})
    assert got["repeated"] == 2 and got["escalated"] is True
    assert got["doctor"]["data_root"]["path"] == str(the_data_root())
    assert got["doctor"]["token"]["user"]
    assert "holders" in got["doctor"]


@pytest.mark.asyncio
async def test_a_different_code_in_between_starts_the_count_over(runtime_client) -> None:
    client, factory = runtime_client
    async with factory() as db:
        first = await _seed(db, run_id="run_env_3", failure_code="upstream_unavailable")
        second = await _seed(db, run_id="run_env_4", session_id=first.session_id, failure_code="execution_failed")
        await db.commit()
        got = await env.environment_for_run(db, project_id=first.project_id, session_id=first.session_id,
                                            run_id=second.id, failure={"code": "execution_failed"})
    assert got is None, "第一次、又没有降级事实：没有环境那一节可写"


@pytest.mark.asyncio
async def test_a_third_time_counts_on_from_the_recorded_repeat(runtime_client) -> None:
    client, factory = runtime_client
    async with factory() as db:
        first = await _seed(db, run_id="run_env_5", failure_code="execution_failed",
                            environment={"warnings": [], "repeated": 2, "escalated": True})
        second = await _seed(db, run_id="run_env_6", session_id=first.session_id, failure_code="execution_failed")
        await db.commit()
        got = await env.environment_for_run(db, project_id=first.project_id, session_id=first.session_id,
                                            run_id=second.id, failure={"code": "execution_failed"})
    assert got["repeated"] == 3


@pytest.mark.asyncio
async def test_the_banner_reads_the_last_run_when_no_worker_is_alive_and_refuses_unknown_repairs(runtime_client, tmp_path) -> None:
    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    warning = {"type": "environment_warning", "code": "state_dir_unavailable", "severity": "degraded",
               "path": str(tmp_path / "runs"), "error": "PermissionError: [Errno 13] Permission denied",
               "consequences": ["这一轮的运行记录落在临时目录"]}
    async with factory() as db:
        run = await _seed(db, run_id="run_env_7", environment={
            "warnings": [warning], "source": "live", "repeated": 0, "escalated": False, "doctor": None})
        project_id, session_id = run.project_id, run.session_id
        await db.commit()

    got = await client.get(f"/api/v1/projects/{project_id}/sessions/{session_id}/environment", headers=_headers(token))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["source"] == "last_run" and body["recorded_source"] == "live" and body["runId"] == "run_env_7"
    assert [w["code"] for w in body["warnings"] if w["code"] != "data_root_permissions_changed"] == ["state_dir_unavailable"]

    refused = await client.post(f"/api/v1/projects/{project_id}/sessions/{session_id}/environment/repair",
                                headers=_headers(token), json={"code": "nothing_like_this"})
    assert refused.status_code == 409 and "nothing_like_this" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_a_repair_runs_the_server_side_proposal_and_reports_its_output(runtime_client, tmp_path, monkeypatch) -> None:
    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    warning = {"type": "environment_warning", "code": "state_dir_unavailable", "severity": "degraded",
               "path": str(tmp_path / "runs"), "error": "PermissionError: [Errno 13] Permission denied",
               "consequences": []}
    async with factory() as db:
        run = await _seed(db, run_id="run_env_8", environment={"warnings": [warning], "repeated": 0,
                                                              "escalated": False, "doctor": None})
        project_id, session_id = run.project_id, run.session_id
        await db.commit()
    ran: list[list[str]] = []

    def fake_run(argv):
        ran.append(argv)
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(env, "_run_argv", fake_run)
    done = await client.post(f"/api/v1/projects/{project_id}/sessions/{session_id}/environment/repair",
                             headers=_headers(token), json={"code": "state_dir_unavailable", "path": str(tmp_path / "runs")})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["returncode"] == 0 and body["stdout"] == "ok"
    assert ran and str(the_data_root()) in ran[0], "执行的是服务端按表重算的命令，不是前端回传的"
    assert body["command"] and "facts_after" in body

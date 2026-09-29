"""会话诊断包：人点一下，拿到的 zip 里就是说清「出了什么事」要的全部记录。

判据写在人会拿它做什么上：

- 会话派出去的**每一个** run 都在里面，不只是调度器那一个 —— 节点里出的错
  在子 run 的目录里（本机实测一个会话 12 个子 run，与调度器目录平级）。
- 同一时段的后端日志在里面，更早的不在；traceback 跟着它那条记录走。
- 模型的 key 一个字节都不出门，哪个文件里都没有。
- 组织服务器上，别人的日志不进包。
- 打完的临时文件不留在服务器上。
- worker 跑到一半死掉，它临终说的话进后端日志 —— 诊断包截日志就截得到它。
"""
from __future__ import annotations

# ruff: noqa: F811  （`runtime_client` 是借来的 pytest fixture，参数名遮住导入名是它的正常长相）
import asyncio
import io
import json
import logging
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import the_data_root
from app.models.execution import SessionProjection
from app.models.model_backend import ModelBackendConfig
from app.services import session_diagnostics as diagnostics
from app.services.model_backends import encrypt_api_key
from app.services.session_diagnostics import (
    session_runs_root,
    slice_backend_log,
    write_diagnostics_bundle,
)

from .test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401

#: 故意不长成 `sk-…`：抹掉它的必须是「按库里的原值抹」，不是形状兜底。
SECRET = "ds-live-7f3a9c2e5b8d1f40a6c3e9b2"


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S") + ",123"


def _log_line(moment: datetime, message: str) -> str:
    return f"timestamp={_stamp(moment)} | level=INFO | logger=app.test | message={message}\n"


async def _open_session(client: AsyncClient, token: str) -> tuple[str, str]:
    projects = await client.get("/api/v1/projects/", headers=_headers(token))
    assert projects.status_code == 200, projects.text
    project_id = projects.json()[0]["id"]
    created = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(token),
        json={"title": "诊断包"},
    )
    assert created.status_code == 201, created.text
    return project_id, created.json()["id"]


def _seed_runs(runs_root: Path, project_id: str, session_id: str) -> dict[str, Path]:
    orchestrator = runs_root / f"orchestrator__{project_id}__session__{session_id}"
    child = runs_root / "1788723286-9837f2"
    (orchestrator / "event_blobs").mkdir(parents=True)
    child.mkdir(parents=True)
    (orchestrator / "events.jsonl").write_text(
        json.dumps({"type": "started", "env": {"LLM_API_KEY": SECRET}}) + "\n", encoding="utf-8"
    )
    (orchestrator / "transcript.jsonl").write_text(
        '{"role": "user", "content": "开始"}\n', encoding="utf-8"
    )
    (orchestrator / "event_blobs" / "a43c.bin").write_bytes(b"\x00\x01blob")
    (child / "events.jsonl").write_text(
        '{"type": "error", "message": "节点里真正出的错"}\n', encoding="utf-8"
    )
    return {"orchestrator": orchestrator, "child": child}


def _members(payload: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.mark.asyncio
async def test_the_bundle_carries_every_run_the_logs_of_that_time_and_no_key(
    runtime_client, tmp_path, monkeypatch
) -> None:
    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)

    # 别人配的后端也算：抹的是这台后端握着的**每一把** key，不只是这个会话用的那把。
    someone = str(uuid4())
    async with factory() as db:
        db.add(ModelBackendConfig(
            scope_kind="personal", scope_id=someone, provider="openai_compatible",
            display_name="真 key", model="m", base_url="http://provider.invalid/v1",
            credential_source="encrypted", encrypted_api_key=encrypt_api_key(SECRET),
            created_by_user_id=someone,
        ))
        await db.commit()
        session = await db.scalar(
            select(SessionProjection).where(SessionProjection.session_id == session_id)
        )
        created_local = session.created_at.astimezone().replace(tzinfo=None)

    runs_root = session_runs_root(project_id, session_id)
    assert runs_root is not None, "建会话就该有工作树 —— 没有的话这条测试什么也没验"
    runs = _seed_runs(runs_root, project_id, session_id)
    # worker 的套接字这一类「不是普通文件」的东西：打不进 zip，也不该跟过去。
    # 用命名管道造它（套接字路径在这层嵌套目录下会超 104 字节的上限）。
    special = hasattr(os, "mkfifo")
    if special:
        os.mkfifo(runs["orchestrator"] / "w.sock")

    logs = the_data_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "backend.log").write_text(
        _log_line(created_local - timedelta(days=2), "两天前的事，不该进包")
        + _log_line(created_local + timedelta(seconds=5), "会话开始后出的错")
        + "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: 跟着上一条走\n"
        + _log_line(
            created_local + timedelta(seconds=6), f"GET /x Authorization=Bearer {'a' * 32}"
        ),
        encoding="utf-8",
    )
    (logs / "shell.log").write_text("READY http://127.0.0.1:1234/\n", encoding="utf-8")

    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    response = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/diagnostics",
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert f"session-diagnostics-{session_id[:8]}-" in response.headers["content-disposition"]

    members = _members(response.content)
    orchestrator = f"runs/{runs['orchestrator'].name}"
    for name in (
        f"{orchestrator}/events.jsonl",
        f"{orchestrator}/transcript.jsonl",
        f"{orchestrator}/event_blobs/a43c.bin",
        f"runs/{runs['child'].name}/events.jsonl",
        "logs/backend.log",
        "logs/shell.log",
        "manifest.json",
        "README.txt",
    ):
        assert name in members, f"{name} 应当在包里；实际：{sorted(members)}"
    assert "节点里真正出的错" in members[f"runs/{runs['child'].name}/events.jsonl"].decode()

    for name, data in members.items():
        assert SECRET.encode() not in data, f"模型 key 出现在 {name} 里"
    assert b"[REDACTED]" in members[f"{orchestrator}/events.jsonl"]

    backend_log = members["logs/backend.log"].decode()
    assert "会话开始后出的错" in backend_log
    assert "ValueError: 跟着上一条走" in backend_log, "traceback 要跟着它那条记录一起进包"
    assert "两天前的事" not in backend_log
    assert "a" * 32 not in backend_log, "Bearer 后面那串也要抹"

    manifest = json.loads(members["manifest.json"])
    assert manifest["session"]["id"] == session_id
    assert manifest["app"]["profile"] == "personal"
    assert manifest["redactions"] >= 2
    if special:
        assert f"{orchestrator}/w.sock" not in members
        assert {"path": f"{orchestrator}/w.sock", "reason": "不是普通文件"} in manifest["skipped"]

    assert list(scratch.iterdir()) == [], "打完的 zip 不许留在服务器的临时目录里"


@pytest.mark.asyncio
async def test_someone_who_cannot_see_the_session_cannot_download_it(runtime_client) -> None:
    client, _ = runtime_client
    owner = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, owner)
    outsider = await _token(client, "outsider@other.local")
    response = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/diagnostics",
        headers=_headers(outsider),
    )
    assert response.status_code in {403, 404}, response.text
    assert not response.content.startswith(b"PK"), "拒绝的时候不许把包也带出去"


def test_on_an_org_server_only_records_naming_this_session_leave(tmp_path) -> None:
    """组织服务器的日志是很多人共用的：只收点名这个会话或项目的记录，别的日志文件不收。"""
    now = datetime.now().replace(microsecond=0)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "backend.log").write_text(
        _log_line(now, "GET /api/v1/projects/proj-mine/sessions/sess-mine/events")
        + _log_line(now, "别人的会话 sess-theirs 出错了")
        + "Traceback: 别人的 traceback\n"
        + _log_line(now, "Harness worker for proj-mine/sess-mine exited mid-session")
        + "worker stderr 的续行\n",
        encoding="utf-8",
    )
    (logs / "shell.log").write_text("整台机器的壳日志\n", encoding="utf-8")

    bundle = write_diagnostics_bundle(
        project_id="proj-mine", session_id="sess-mine", session_facts={},
        session_created_at=None, runs_root=None, logs_dir=logs, secrets=(),
        shared_log=True, profile_label="org", app_version="0.5.3", destination_dir=tmp_path,
    )
    try:
        members = _members(bundle.path.read_bytes())
    finally:
        bundle.path.unlink()
    text = members["logs/backend.log"].decode()
    assert "sess-mine/events" in text
    assert "worker stderr 的续行" in text
    assert "sess-theirs" not in text and "别人的 traceback" not in text
    assert "logs/shell.log" not in members
    assert bundle.manifest["app"]["profile"] == "org"


def test_the_org_server_is_wired_to_filter_through_the_assembly(monkeypatch) -> None:
    """「日志是不是共用的」由装配层回答；组织档起的后端，打包时真的走过滤。"""
    from app.config import settings
    from app.services.session_diagnostics import build_for_session

    monkeypatch.setattr(settings, "profile", "org")
    now = datetime.now().replace(microsecond=0)
    logs = the_data_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "backend.log").write_text(
        _log_line(now, "GET /api/v1/projects/p-wired/sessions/s-wired/events")
        + _log_line(now, "别人的会话 s-other"),
        encoding="utf-8",
    )
    bundle = build_for_session(
        project_id="p-wired", session_id="s-wired", session_facts={},
        session_created_at=None, secrets=(),
    )
    try:
        text = _members(bundle.path.read_bytes())["logs/backend.log"].decode()
    finally:
        bundle.path.unlink()
    assert "s-wired/events" in text and "s-other" not in text
    assert bundle.manifest["app"]["profile"] == "org"


def test_an_oversized_log_keeps_its_tail_and_an_oversized_blob_is_named_not_packed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(diagnostics, "FILE_CAP_BYTES", 64)
    runs = tmp_path / "runs" / "orchestrator__p__session__s"
    runs.mkdir(parents=True)
    lines = [json.dumps({"n": index}) for index in range(40)]
    (runs / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (runs / "big.bin").write_bytes(b"\x00" * 1000)

    bundle = write_diagnostics_bundle(
        project_id="p", session_id="s", session_facts={}, session_created_at=None,
        runs_root=tmp_path / "runs", logs_dir=tmp_path / "no-logs", secrets=(),
        shared_log=False, profile_label="personal", app_version=None, destination_dir=tmp_path,
    )
    try:
        members = _members(bundle.path.read_bytes())
    finally:
        bundle.path.unlink()
    kept = members["runs/orchestrator__p__session__s/events.jsonl"].decode().splitlines()
    assert kept[-1] == lines[-1], "出事的地方通常在最后 —— 留的必须是尾巴"
    assert all(json.loads(line) for line in kept), "从一个完整行开始，每一行都读得懂"
    assert "runs/orchestrator__p__session__s/big.bin" not in members
    skipped = {entry["path"]: entry for entry in bundle.manifest["skipped"]}
    assert skipped["runs/orchestrator__p__session__s/big.bin"]["bytes"] == 1000


def test_the_log_slice_drops_the_oldest_records_when_it_is_too_big(tmp_path) -> None:
    now = datetime.now().replace(microsecond=0)
    log = tmp_path / "backend.log"
    log.write_text(
        "".join(_log_line(now, f"第 {index} 条") for index in range(100)), encoding="utf-8"
    )
    data, facts = slice_backend_log(
        log, since_local=now - timedelta(minutes=1), naming=None, cap_bytes=500
    )
    text = data.decode()
    assert "第 99 条" in text and "第 0 条" not in text
    assert facts["dropped_oldest_for_size"] > 0


@pytest.mark.asyncio
async def test_a_worker_that_dies_mid_session_leaves_its_reason_in_the_backend_log(caplog) -> None:
    """退出原因从前只拼进那一轮的失败信息；后端日志里没有，诊断包就截不到。"""
    from app.services.harness_runtime import _drain_stderr
    from app.services.harness_sessions import _ProjectHarnessSession

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        f"import sys; sys.stderr.write('ModuleNotFoundError: boom {SECRET}'); sys.exit(3)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    session = _ProjectHarnessSession(
        project_id="proj-1", session_id="sess-1", owner_user_id="u1",
        backend_id="b", backend_fingerprint="fp", platform_context_hash=None,
        process=process, stderr_task=asyncio.create_task(_drain_stderr(process.stderr)),
        provider_secrets=(SECRET,),
    )
    await process.wait()
    with caplog.at_level(logging.ERROR, logger="app.services.harness_sessions"):
        first = await session._process_exit_error()
        await session._process_exit_error()
    records = [r.getMessage() for r in caplog.records if "exited mid-session" in r.getMessage()]
    assert len(records) == 1, f"每次再问都会再算一遍，日志只该记一次；实际 {len(records)} 次"
    assert "proj-1/sess-1" in records[0]
    assert "exit code 3" in records[0]
    assert "ModuleNotFoundError: boom" in records[0]
    assert SECRET not in records[0]
    assert "ModuleNotFoundError" in str(first)


# ── 界面会抹的，包里也抹；只抹密钥，不剪证据 ──────────────────────────────────


def test_what_the_ui_would_hide_does_not_leave_in_the_bundle(tmp_path) -> None:
    """诊断包和界面入库用**同一个**「什么算密钥」（``app.services.redaction``）。

    审查时的探针：这些值曾经原样进了包 —— 包只认模型 key 的原值和 sk-/Bearer 两种形状，
    而界面入库那一层早就抹它们。运行目录里 worker 的 ``spawn_token`` 同理。
    """
    connection = "postgresql://alice:hunter2pass@db.internal:5432/research"
    aws = "AKIA" + "IOSFODNN7EXAMPL1"
    github = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
    spawn_token = "st-9f1e2d3c4b5a69788796a5b4c3d2e1f0"
    evidence = "/Users/someone/afs/project-worktrees/p/s/results/table.csv"
    runs = tmp_path / "runs" / "orchestrator__p__session__s"
    (runs / "event_blobs").mkdir(parents=True)
    (runs / "events.jsonl").write_text(
        json.dumps({"type": "tool_result", "env": {"DATABASE_URL": connection},
                    "output": f"aws={aws} gh={github}", "password": "p@ssw0rd-plain",
                    "cookie": "session=abcdef0123456789", "path": evidence}) + "\n",
        encoding="utf-8",
    )
    (runs / "activity.json").write_text(
        json.dumps({"pid": 4242, "spawn_token": spawn_token}, indent=2), encoding="utf-8")
    (runs / "notes.txt").write_text(f"模型写下的笔记\n{pem}\n", encoding="utf-8")
    (runs / "event_blobs" / "b1.bin").write_bytes(
        b"\x00\xff\xfe" + f"Authorization: Bearer {github}".encode() + b"\x80\x81")

    bundle = write_diagnostics_bundle(
        project_id="p", session_id="s", session_facts={}, session_created_at=None,
        runs_root=tmp_path / "runs", logs_dir=tmp_path / "no-logs", secrets=(),
        shared_log=False, profile_label="personal", app_version=None, destination_dir=tmp_path,
    )
    try:
        members = _members(bundle.path.read_bytes())
    finally:
        bundle.path.unlink()
    everything = b"\n".join(members.values())
    for secret in (connection, "hunter2pass", aws, github, "MIIEow", spawn_token,
                   "p@ssw0rd-plain", "session=abcdef0123456789"):
        assert secret.encode() not in everything, f"{secret!r} 出了门"
    blob = members["runs/orchestrator__p__session__s/event_blobs/b1.bin"]
    assert blob.startswith(b"\x00\xff\xfe") and blob.endswith(b"\x80\x81"), "二进制原样往返"
    # 只抹密钥，不做界面那套截断和路径缩写：诊断要的是原样的证据。
    events = members["runs/orchestrator__p__session__s/events.jsonl"].decode()
    assert evidence in events, "绝对路径被缩写了 —— 那是界面展示的事，诊断包要原样"
    assert '"type": "tool_result"' in events




async def test_a_second_bundle_while_one_is_building_is_told_to_wait(runtime_client) -> None:
    """同一时间只打一个：第二个直接 429，不去占仓库读写共用的线程池。"""
    from app.api.v1 import sessions as sessions_api

    client, _ = runtime_client
    owner = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, owner)
    assert sessions_api._DIAGNOSTICS_SLOT.acquire(blocking=False), "前提：没有别的在打"
    try:
        response = await client.get(
            f"/api/v1/projects/{project_id}/sessions/{session_id}/diagnostics",
            headers=_headers(owner),
        )
    finally:
        sessions_api._DIAGNOSTICS_SLOT.release()
    assert response.status_code == 429, response.text
    assert response.headers.get("retry-after")


def test_logs_count_toward_the_whole_bundle_cap(tmp_path, monkeypatch) -> None:
    """整包上限对运行记录和日志一视同仁 —— 日志曾经不计入。"""
    monkeypatch.setattr(diagnostics, "TOTAL_CAP_BYTES", 200)
    now = datetime.now().replace(microsecond=0)
    runs = tmp_path / "runs" / "orchestrator__p__session__s"
    runs.mkdir(parents=True)
    (runs / "events.jsonl").write_text("x" * 150 + "\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "backend.log").write_text(_log_line(now, "y" * 120), encoding="utf-8")

    bundle = write_diagnostics_bundle(
        project_id="p", session_id="s", session_facts={}, session_created_at=None,
        runs_root=tmp_path / "runs", logs_dir=logs, secrets=(),
        shared_log=False, profile_label="personal", app_version=None, destination_dir=tmp_path,
    )
    try:
        members = _members(bundle.path.read_bytes())
    finally:
        bundle.path.unlink()
    assert "logs/backend.log" not in members
    assert "logs/backend.log" in {entry["path"] for entry in bundle.manifest["skipped"]}
    assert sum(len(data) for name, data in members.items()
               if name not in {"manifest.json", "README.txt"}) <= 200


def test_one_log_record_bigger_than_the_cap_keeps_its_tail(tmp_path) -> None:
    now = datetime.now().replace(microsecond=0)
    log = tmp_path / "backend.log"
    log.write_text(_log_line(now, "开头" + "z" * 2000 + "结尾在这"), encoding="utf-8")
    data, _facts = slice_backend_log(
        log, since_local=now - timedelta(minutes=1), naming=None, cap_bytes=300)
    assert len(data) <= 300
    assert "结尾在这" in data.decode("utf-8", errors="replace")

"""worker 起来时持久化前提没成立：后端记下来、报出来，不把这一轮杀掉
（docs/RFC_WORKER_START_IS_NOT_A_GATE_20261016.md，窄单 A 的后端那半）。

三条：
1. `init` 请求带「这个家是不是单租户」（装配层答；worker 据此决定 marker 读不到时留家还是换家）。
2. ready 里的 capabilities / warnings 留在会话上（warning 补了事实与提案，worker 说的一个字不改），每条降级记一行 warning 日志。
3. 老 worker 不发这两个字段 → 读作「未报告」（None），不读作「全部成立」。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.services import harness_sessions as hs


class _Channel(hs._WorkerChannel):
    async def send_line(self, line: str) -> None:  # pragma: no cover - 不会被调到
        raise AssertionError("initialize 应经 _rpc_locked，这里被替身接管")

    async def read_line(self) -> bytes:  # pragma: no cover
        raise AssertionError("not used")


def _session() -> hs._ProjectHarnessSession:
    return hs._ProjectHarnessSession(
        project_id="project-x", session_id="session-x", owner_user_id="user-x",
        backend_id="backend-x", backend_fingerprint="fp", platform_context_hash=None,
        process=None, stderr_task=None, provider_secrets=[], channel=_Channel(),
    )


async def _noop(_event: dict) -> None:
    return None


async def _initialize(session, ready: dict, tmp_path: Path) -> dict:
    sent: dict = {}

    async def fake_rpc(payload, **kwargs):
        sent.update(payload)
        return ready

    session._rpc_locked = fake_rpc  # type: ignore[method-assign]
    await session.initialize(
        home_dir=tmp_path / "home", org_home=tmp_path / "org", projects_home=tmp_path / "projects",
        state_dir=tmp_path / "state", workspace_dir=tmp_path / "work",
        platform_context_snapshot=None, on_progress=_noop, on_protocol_event=_noop,
    )
    return sent


async def test_init_carries_the_edition_and_ready_capabilities_stay_on_the_session(tmp_path, caplog):
    session = _session()
    ready = {
        "type": "ready",
        "sandbox_protocol_version": hs.SANDBOX_PROTOCOL_VERSION,
        "capabilities": {"home_bound": False, "state_persistent": True},
        "warnings": [{
            "type": "environment_warning", "code": "home_marker_unreadable",
            "capability": "home_bound", "severity": "degraded",
            "path": str(tmp_path / "home" / ".platform-tenant-identity.json"),
            "error": "PermissionError: [WinError 5] 拒绝访问。",
            "consequences": ["无法核实这个家属于本租户"],
        }],
    }
    with caplog.at_level(logging.WARNING, logger=hs.logger.name):
        sent = await _initialize(session, ready, tmp_path)

    assert sent["op"] == "init"
    from app.assembly import the_worker_home_has_one_tenant

    assert sent["home_is_single_tenant"] is the_worker_home_has_one_tenant()
    assert isinstance(sent["home_is_single_tenant"], bool)
    assert session.start_report["capabilities"] == {"home_bound": False, "state_persistent": True}
    [kept] = session.start_report["warnings"]
    # worker 说的一个字不改；后端补上事实、持有者、令牌、提案（B 单：看不见就修不了）。
    for key, value in ready["warnings"][0].items():
        assert kept[key] == value
    assert set(kept) >= {"facts", "holders", "token", "repair", "data_root"}
    assert kept["facts"]["exists"] is False, "路径不存在也是事实，不是缺席"
    assert kept["repair"]["kind"] == "reset_data_root_permissions", "权限形状的读不到 → 恢复数据根权限的提案"
    logged = [r.getMessage() for r in caplog.records if "started degraded" in r.getMessage()]
    assert len(logged) == 1
    assert "home_marker_unreadable" in logged[0]
    assert "WinError 5" in logged[0]
    assert ".platform-tenant-identity.json" in logged[0]


async def test_an_old_worker_that_reports_nothing_reads_as_unreported_not_healthy(tmp_path):
    session = _session()
    await _initialize(session, {"type": "ready", "sandbox_protocol_version": hs.SANDBOX_PROTOCOL_VERSION}, tmp_path)
    assert session.start_report == {"capabilities": None, "warnings": None}


async def test_a_wrong_protocol_version_is_still_fatal(tmp_path, monkeypatch):
    session = _session()

    async def no_kill():
        return None

    monkeypatch.setattr(session, "_kill", no_kill)
    with pytest.raises(hs.HarnessSessionError, match="sandbox protocol"):
        await _initialize(session, {"type": "ready", "sandbox_protocol_version": 0,
                                    "capabilities": {"home_bound": True}}, tmp_path)
    assert session.start_report is None


async def test_a_private_runtime_root_is_trusted_only_from_the_temp_dir(tmp_path, monkeypatch):
    """worker 退到私有临时目录跑：ready 报的 runtime_root 进摄取的放行名单 —— 只认系统临时目录下的。"""
    import tempfile

    session = _session()
    private = Path(tempfile.mkdtemp(prefix="hf-state-")) / "runs"
    private.mkdir()
    await _initialize(session, {"type": "ready", "sandbox_protocol_version": hs.SANDBOX_PROTOCOL_VERSION,
                                "runtime_root": str(private), "capabilities": {"state_persistent": False},
                                "warnings": []}, tmp_path)
    assert session.private_runtime_root == private.resolve()
    elsewhere = _session()
    await _initialize(elsewhere, {"type": "ready", "sandbox_protocol_version": hs.SANDBOX_PROTOCOL_VERSION,
                                  "runtime_root": str(Path.home() / "definitely-not-the-temp-dir"), "capabilities": {"state_persistent": False},
                                  "warnings": []}, tmp_path)
    assert elsewhere.private_runtime_root is None
    normal = _session()
    await _initialize(normal, {"type": "ready", "sandbox_protocol_version": hs.SANDBOX_PROTOCOL_VERSION,
                               "runtime_root": str(private), "capabilities": {"state_persistent": True}, "warnings": []}, tmp_path)
    assert normal.private_runtime_root is None


def test_resolve_existing_treats_a_denied_stat_as_absent(tmp_path, monkeypatch):
    from app.services import session_runtime_paths as srp

    worktree = tmp_path / "wt"
    (worktree / ".research" / "runtime" / "runs").mkdir(parents=True)
    assert srp.resolve_existing(worktree, "runs") == worktree / ".research" / "runtime" / "runs"
    real_exists = Path.exists

    def denied(self, *a, **k):
        if "runtime" in str(self):
            raise PermissionError(5, "拒绝访问。", str(self))
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", denied)
    assert srp.resolve_existing(worktree, "runs") == worktree / ".research" / "runtime" / "runs", "读不到按不在：拿现行位置继续，不抛"


def test_ingest_approves_the_live_private_root_and_nothing_else(tmp_path, monkeypatch):
    import tempfile
    from app.services import harness_transcript_ingest as ingest

    private = Path(tempfile.mkdtemp(prefix="hf-state-")).resolve()
    monkeypatch.setattr(hs.harness_session_manager, "private_runtime_root", lambda p, s: private)
    monkeypatch.setattr(ingest, "_session_runtime_root", lambda p, s: tmp_path / "wt" / ".research" / "runtime" / "runs")
    assert ingest._approved_runtime_root(private / "orchestrator__p__session__s" / "transcript.jsonl", "p", "s") == private
    assert ingest._approved_runtime_root(tmp_path / "elsewhere" / "transcript.jsonl", "p", "s") is None
    monkeypatch.setattr(hs.harness_session_manager, "private_runtime_root", lambda p, s: None)
    assert ingest._approved_runtime_root(private / "transcript.jsonl", "p", "s") is None

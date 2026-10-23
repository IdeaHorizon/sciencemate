"""#1228 的后端三处：会话读者、一次性桥、诊断包。和 worker 同一条规则（`shared.lib.secret_values`）。

key 配成 ``"k"`` 时，三处各自做子串替换会把路径、code、日志里所有的 ``k`` 抹掉 —— 会话读者那一处让
`HarnessSessionError` 的原文和 `cause` 面目全非，诊断包里的路径对不上真实目录。
"""
from __future__ import annotations

from app.services import harness_bridge_once
from app.services.harness_sessions import _ProjectHarnessSession
from app.services.session_diagnostics import Redactor

PATH = r"C:\Users\k\afs\project-worktrees\hf-state-6k8tx30o\runs"
KEY = "Zq7vR2mXp9LwT4cN"


def _session(secrets: list[str]) -> _ProjectHarnessSession:
    return _ProjectHarnessSession(
        project_id="p", session_id="s", owner_user_id="u", backend_id="", backend_fingerprint="",
        platform_context_hash=None, process=None, stderr_task=None, provider_secrets=secrets,
    )


def test_the_session_reader_leaves_a_path_alone_for_a_one_letter_key() -> None:
    session = _session(["k", KEY])
    assert session._redact(f"home_marker_unreadable at {PATH}") == f"home_marker_unreadable at {PATH}"
    assert session._redact(f"Bearer-less {KEY} leaked") == "Bearer-less [REDACTED] leaked"


def test_the_one_shot_bridge_follows_the_same_rule() -> None:
    assert harness_bridge_once._redact(PATH, "k") == PATH
    assert harness_bridge_once._redact(f"x{KEY}y", KEY) == "x[REDACTED]y"


def test_the_diagnostics_bundle_follows_the_same_rule() -> None:
    data, count = Redactor(["k", KEY])(f"{PATH} {KEY}".encode())
    assert data.decode().startswith(PATH) and KEY.encode() not in data and count >= 1

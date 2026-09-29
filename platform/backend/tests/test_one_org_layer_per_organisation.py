"""org 层是**组织**的：个人档一台机器一个，组织档一个组织一个。

## 两次病例

**09-16（node20）**：org 层按用户 home 推（`state/users/<uid>/org`），12 个用户 12 个
私有 org 层，16 条晋升上来的 claim 散在其中 3 个人的目录里，互相看不见。当时改成
「一台安装一个」。

**09-24**：一台组织服务器上住着好几个组织（名录、项目、模型都按 `institution_id` 隔开），
而 org 层是整台一个 —— A 组织晋升上去的结论，B 组织的项目开题时就读到了。于是组织档上
按组织分：`<数据根>/organisations/<组织 id>/`。同一天还发现：09-16 那次只接到了 KB 桥，
**会话 worker 从来没被告知** org 层在哪，一直各自私有（见
`tests/test_the_worker_uses_its_organisations_knowledge.py`）。

## 判据

1. 个人档 `the_org_home()` 只由数据根决定；组织档必须说是哪个组织，两个组织两个目录，
   不说就拒（猜一个 = 把一个组织的知识写进另一个组织）。
2. 组织档上进程环境里**没有**一个可以被子进程继承的 org 层。
3. 每一个问 harness 的请求（KB 桥、算力授权、会话 worker）都带着**提问者所在组织**的
   那一个，子进程环境里也不捎带别的。
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from app.config import (
    HARNESS_ORG_HOME_VARIABLE,
    DataRootError,
    publish_the_data_root,
    settings,
    the_org_home,
)
from app.models.user import User
from app.services.instructions import harness_home_for


@pytest.fixture
def org_server(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(settings, "profile", "org")
    return tmp_path


def _someone_in(organisation: str) -> User:
    return User(id=f"user-in-{organisation}", email=f"a@{organisation}.test",
                institution_id=organisation, role="researcher")


def test_a_personal_install_has_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(settings, "profile", "personal")
    assert the_org_home() == tmp_path / "org"
    assert the_org_home("local") == tmp_path / "org"
    a, b = harness_home_for("user-a"), harness_home_for("user-b")
    assert the_org_home() not in (a / "org", b / "org")


def test_an_organisation_server_has_one_per_organisation(org_server: Path) -> None:
    a, b = the_org_home("Qx7-aa_9"), the_org_home("Zb2kLm")
    assert a != b
    assert a == org_server / "organisations" / "Qx7-aa_9"
    assert b.parent == a.parent


@pytest.mark.parametrize("asked", [None, "", "../other", "a/b", ".."])
def test_it_will_not_guess_which_organisation(org_server: Path, asked) -> None:
    with pytest.raises(DataRootError):
        the_org_home(asked)


def test_nothing_is_left_in_the_environment_to_inherit(org_server: Path, monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_FRAMEWORK_HOME", raising=False)
    monkeypatch.setenv(HARNESS_ORG_HOME_VARIABLE, str(org_server / "org"))
    publish_the_data_root()
    assert HARNESS_ORG_HOME_VARIABLE not in os.environ, (
        "组织档的进程环境里留着一个 org 层 —— 哪个子进程漏给了，就会读写它")


def test_a_personal_install_still_publishes_it(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.delenv("HARNESS_FRAMEWORK_HOME", raising=False)
    monkeypatch.delenv(HARNESS_ORG_HOME_VARIABLE, raising=False)
    publish_the_data_root()
    assert os.environ[HARNESS_ORG_HOME_VARIABLE] == str(tmp_path / "org")


@pytest.fixture
def what_the_bridge_is_asked(monkeypatch):
    """记下每一次问 harness 的请求和子进程环境，不真起桥。"""
    from app.services import harness_bridge_once

    asked: list[tuple[dict, dict]] = []

    async def remember(request, *, expect, child_env, **_):
        asked.append((request, child_env))
        return {"type": expect, "records": [], "stats": {}, "proposals": [],
                "proposal": {"id": "promo_x", "status": "declined"}, "grants": {}}

    monkeypatch.setattr(harness_bridge_once, "ask_once", remember)
    monkeypatch.setattr(settings, "harness_root", str(Path(__file__).resolve().parents[3]))
    # 环境里故意放一个别的：子进程不许把它捎过去。
    monkeypatch.setenv(HARNESS_ORG_HOME_VARIABLE, "/somewhere/else/org")
    return asked


@pytest.mark.asyncio
async def test_every_knowledge_question_names_the_askers_organisation(
        org_server: Path, what_the_bridge_is_asked) -> None:
    from app.services import harness_kb

    someone = _someone_in("OrgB")
    await harness_kb.query(someone, "", "claims", scope="org")
    await harness_kb.query(someone, "p1", "claims")
    await harness_kb.stats(someone, "", scope="org")
    await harness_kb.resolve_proposal(someone, "p1", "prop_1", decision="accepted", reasoning="理由写够五个字")
    await harness_kb.review_queue(someone)
    await harness_kb.decline(someone, "promo_x", reason="太宽")

    assert len(what_the_bridge_is_asked) == 6
    for request, child_env in what_the_bridge_is_asked:
        assert request["org_home"] == str(org_server / "organisations" / "OrgB"), request["op"]
        assert HARNESS_ORG_HOME_VARIABLE not in child_env, (
            f"{request['op']} 的子进程捎上了环境里的 org 层")


@pytest.mark.asyncio
async def test_compute_grants_are_the_askers_organisations(
        org_server: Path, what_the_bridge_is_asked) -> None:
    from app.services import compute_grants

    await compute_grants.read(_someone_in("OrgB"))

    [(request, child_env)] = what_the_bridge_is_asked
    assert request["org_home"] == str(org_server / "organisations" / "OrgB")
    assert HARNESS_ORG_HOME_VARIABLE not in child_env


@pytest.mark.asyncio
async def test_the_worker_is_told_in_its_init_request(tmp_path: Path) -> None:
    from app.services.harness_sessions import SANDBOX_PROTOCOL_VERSION, _ProjectHarnessSession

    session = _ProjectHarnessSession(
        project_id="p", session_id="c", owner_user_id="u", backend_id="b",
        backend_fingerprint="fp", platform_context_hash=None, process=None,
        stderr_task=None, provider_secrets=(), channel=object(), worker=object(),  # type: ignore[arg-type]
    )
    sent: list[dict] = []

    async def fake_rpc(payload: dict, **_: object) -> dict:
        sent.append(payload)
        return {"type": "ready", "sandbox_protocol_version": SANDBOX_PROTOCOL_VERSION}

    session._rpc_locked = fake_rpc  # type: ignore[method-assign]

    async def _noop(*_a, **_k):
        return None

    try:
        await session.initialize(
            home_dir=tmp_path / "home", org_home=tmp_path / "organisations" / "A",
            projects_home=tmp_path / "projects",
            state_dir=tmp_path / "state", workspace_dir=tmp_path / "ws",
            platform_context_snapshot=None, on_progress=_noop, on_protocol_event=_noop)
    except Exception:  # noqa: BLE001 —— ready 之后还有别的核对；这里只问请求里写了什么
        pass
    assert sent and sent[0]["op"] == "init"
    assert sent[0]["org_home"] == str(tmp_path / "organisations" / "A")


def test_the_spawn_names_the_speakers_organisation() -> None:
    """起 worker 的那一处，给的是**说话这个人所在组织**的 org 层。

    走真 spawn 要起一整个 worker；这里读调用本身（AST，不是字符串）：`initialize`
    的 `org_home=` 必须是 `the_org_home(user.institution_id)`。
    """
    source = (Path(__file__).resolve().parents[1] / "app/services/harness_sessions.py").read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "initialize"]
    assert calls, "找不到起 worker 时的 initialize 调用"
    for call in calls:
        given = next((kw.value for kw in call.keywords if kw.arg == "org_home"), None)
        assert isinstance(given, ast.Call) and getattr(given.func, "id", "") == "the_org_home", (
            "initialize 没带 org_home —— worker 会退回自己 home 底下的私有 org 层")
        assert ast.unparse(given.args[0]) == "user.institution_id"

"""会话的模型可以随时换 —— 锁定挡不住配错的后端，只挡住修正。

现场（2026-08-13，node20）：机构默认后端地址为空（发往公网 DeepSeek）配的
却是自建网关的 key，只会 401。一个会话连着两轮全废、零产出，而 UI 给出的
唯一解释是"会话中途不允许换模型，请开新会话"——被锁住的不是归因，是自救。

归因改由记录承担：每条 run 的 summary 里记着它**实际**用的 modelBackendId。
本文件守两件事：换得成，且换完之后两处真相（session 字段 / 送给 harness 的
platform_context_snapshot）不会分叉。
"""

import pytest
from fastapi import HTTPException

from app.models.execution import SessionProjection
from app.models.model_backend import ModelBackendConfig
from app.models.project import Project
from app.models.user import User
from app.schemas.session import SessionUpdate
from app.services import sessions as sessions_service


def _backend(backend_id: str, *, name: str, model: str) -> ModelBackendConfig:
    return ModelBackendConfig(
        id=backend_id,
        display_name=name,
        provider="deepseek",
        model=model,
        base_url="http://gateway/v1",
        scope_kind="institution",
        scope_id="ieit",
        credential_source="environment",
    )


def _session(backend: ModelBackendConfig, project: Project) -> SessionProjection:
    return SessionProjection(
        session_id="s1",
        project_id=str(project.id),
        model_backend_id=backend.id,
        platform_context_snapshot=sessions_service.build_platform_context_snapshot(
            project=project, session=None, backend=backend
        ),
    )


class _DB:
    async def flush(self) -> None:
        return None


@pytest.fixture
def fixtures():
    project = Project(id="p1", name="P")
    old = _backend("old", name="DeepSeek (environment)", model="deepseek-chat")
    new = _backend("new", name="DeepSeek · node20", model="deepseek-v4-pro")
    user = User(
        id="u1", email="u", hashed_password="x", display_name="u",
        role="researcher", institution_id="ieit",
    )
    return project, old, new, user


@pytest.mark.asyncio
async def test_switching_moves_both_the_field_and_the_snapshot(monkeypatch, fixtures):
    project, old, new, user = fixtures
    session = _session(old, project)
    monkeypatch.setattr(
        sessions_service, "get_visible_backend",
        lambda db, u, backend_id: _resolve({"old": old, "new": new}, backend_id),
    )

    returned = await sessions_service.set_session_model_backend(
        _DB(), user=user, project=project, session=session, backend_id="new"
    )

    assert returned.id == "new"
    assert session.model_backend_id == "new"
    # 快照是送给 harness 的那一份。只改字段不改快照 = UI 显示 A、harness 收 B。
    assert session.platform_context_snapshot["modelBackend"]["id"] == "new"
    assert session.platform_context_snapshot["modelBackend"]["model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_a_backend_outside_your_scope_is_refused(monkeypatch, fixtures):
    project, old, _new, user = fixtures
    session = _session(old, project)
    monkeypatch.setattr(
        sessions_service, "get_visible_backend",
        lambda db, u, backend_id: _resolve({"old": old}, backend_id),
    )

    with pytest.raises(HTTPException):
        await sessions_service.set_session_model_backend(
            _DB(), user=user, project=project, session=session, backend_id="someone-elses"
        )
    assert session.model_backend_id == "old"


def test_the_update_schema_carries_the_field():
    """PATCH 的载荷里得真有这个字段，否则前端发了也进不来。"""
    assert "model_backend_id" in SessionUpdate.model_fields


async def _resolve(catalog, backend_id):
    backend = catalog.get(backend_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="Model backend not found")
    return backend


def test_idle_session_swaps_the_worker_next_turn_not_next_round():
    """空闲会话换模型 = 下一条消息就换掉 worker，不存在"还要先浪费一轮"。

    界面那句话（前端 `modelSwitchNotice`）按这三档说话：空闲说"下一条消息就
    用它"，在飞说"这一轮仍用原来的模型"。话说了什么，这里就得真是什么 ——
    这个测试是那句话的凭据。

    worker 的模型定死在 spawn 时的环境变量里，所以"换模型"对活着的进程只有
    一个解法：换掉进程。闲着就换得掉。
    """
    from app.services.harness_sessions import worker_reuse_decision

    assert worker_reuse_decision(same_bindings=False, conversation_in_flight=False) == "respawn"
    # 绑定没变就别白杀进程（重开一次 = 重读 checkpoint）。
    assert worker_reuse_decision(same_bindings=True, conversation_in_flight=True) == "reuse"
    # 在飞的这一轮换不掉：进程已经带着旧绑定跑起来了，且 run 记录必须与
    # 真实执行的绑定一致。**但这不是拒收**（RFC D10 删除清单，2026-08-23）——
    # 从前它叫 `conflict`，调用方据此抛 session_busy，把一个完全合法的请求
    # （换了模型又说了句话）弹回去。现在叫 `defer`：这一轮照旧跑完，指纹
    # 留着旧的，下一次进来（那时空闲）自然 respawn。自愈，不需要一个"待换代"
    # 的标志位。
    assert worker_reuse_decision(same_bindings=False, conversation_in_flight=True) == "defer"
    # 停靠不是在飞：pause 呈递在 turn 边界之后，checkpoint 已落盘 ——
    # respawn 接续，不报错。2026-08-21 之前这里是 conflict，用户停靠中换完
    # 模型每条消息都撞"平台内部错误"，登出再登录反而能好。
    # 注意 paused 根本不再是这个判定的输入：它不该影响结论。

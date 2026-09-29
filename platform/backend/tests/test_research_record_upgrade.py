"""旧信封记录升到原生格式 —— 触发点在**派发前**，不在启动时。

这四条守的是同一件事：升级这件事的结果要送到撞墙的那个人手里。

第一版把它挂在 `main.lifespan`，被拒的工作区只在日志里留一句话，用户读到的
却是 2026-08-18 那版"没有迁移工具、新建一个项目重跑"的文案 —— 照着做就是把
自己的 Analysis 历史删掉（2026-09-15 现场，yuankk）。所以判据落在真入口上：
发一条消息，看它升没升、拒了的话用户读到的是不是迁移器的原话。
"""
import hashlib
import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.execution import SessionProjection
from app.services.harness_sessions import harness_session_manager
from app.services.project_repository import get_project_repository, run_in_repository_thread
from app.services.research_migration import RecordUpgradeBlocked, upgrade_records_before_use
from .test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401


def _old_release_checkpoint(repository, path, *, tamper: bool = False) -> str:
    """一份旧版本（信封时代）的冻结记录，提交进会话工作区；返回提交 sha。

    `tamper=True`：冻结登记之后再改信封字节 —— 迁移器必须以"冻结校验不符"拒绝。
    """
    artifact = path / "derivation/artifacts/derivation_log__proof.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": "derivation_log", "name": "proof", "version": 1,
              "content": "# 已有证明\n1 + 2 = 3\n",
              "metadata": {"frozen": True, "frozen_at": "2026-09-12T00:00:00Z"},
              "provenance": {}, "created_at": "2026-09-12T00:00:00Z"}
    artifact.write_text(json.dumps(record, ensure_ascii=False))
    (artifact.parent / ".frozen.jsonl").write_text(json.dumps({"action": "freeze", "version": 1,
        "artifact_id": artifact.stem, "path": artifact.relative_to(path).as_posix(),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}) + "\n")
    if tamper:
        record["metadata"]["frozen_at"] = "2026-09-13T00:00:00Z"
        artifact.write_text(json.dumps(record, ensure_ascii=False))
    repository._git(path, "add", "--all")
    repository._git(path, "commit", "-m", "Old release checkpoint")
    return repository._git(path, "rev-parse", "HEAD")


async def _session_with_legacy_records(client, factory, token, project, title, *, tamper=False):
    created = await client.post(f"/api/v1/projects/{project}/sessions", headers=_headers(token), json={"title": title})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    repository = get_project_repository()
    path = repository.session_path(project, session_id)
    head = await run_in_repository_thread(_old_release_checkpoint, repository, path, tamper=tamper)
    async with factory() as db:
        session = await db.scalar(select(SessionProjection).where(SessionProjection.session_id == session_id))
        session.git_head_commit_sha = head
        await db.commit()
    return session_id, path, head


async def _db_head(factory, session_id):
    async with factory() as db:
        session = await db.scalar(select(SessionProjection).where(SessionProjection.session_id == session_id))
        return session.git_head_commit_sha


async def _upgrade(factory, project, session_id, owner="whoever"):
    """按生产那样调：db 与 session 行在同一个会话里，回写的 head 由它自己提交。"""
    async with factory() as db:
        session = await db.scalar(select(SessionProjection).where(SessionProjection.session_id == session_id))
        return await upgrade_records_before_use(db, session, project_id=project, owner_user_id=owner)


async def _a_turn_that_would_run(client, token, tmp_path, monkeypatch):
    """把这一轮布置成"只要工作区读得了就能跑完"；返回 (project_id, 记录 turn 调用的 list)。"""
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    created = await client.post(
        "/api/v1/settings/model-backends", headers=_headers(token),
        json={"provider": "openai_compatible", "display_name": "Upgrade test", "model": "test-model",
              "base_url": "http://provider.invalid/v1", "api_key": "test-key"})
    assert created.status_code == 201, created.text
    assert (await client.post(f"/api/v1/settings/model-backends/{created.json()['id']}/default",
                              headers=_headers(token))).status_code == 200
    transcript = tmp_path / "orchestrator__upgrade" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_bytes(b"")
    calls: list[dict] = []

    async def fake_turn(**kwargs):
        calls.append(kwargs)
        return {"status": "completed", "run_id": "orchestrator__upgrade", "final_text": "done",
                "tokens_used": 3, "tokens_used_delta": 3, "transcript_path": str(transcript),
                "artifact_paths": [], "pause_event": None, "pause_pending_path": None,
                "_session_resumable": False}

    monkeypatch.setattr(harness_session_manager, "turn", fake_turn)
    monkeypatch.setattr(harness_session_manager, "paused_binding", lambda *_args: None)
    monkeypatch.setattr(harness_session_manager, "_sessions", {})
    project = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    return project, calls


def _events(response):
    return [json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines() if line.startswith("data: ")]


async def test_sending_a_message_upgrades_the_records_and_then_runs_the_turn(
    runtime_client, monkeypatch, tmp_path
):
    """真入口：一条消息就够。用户不必重启应用，也不必知道"迁移"这个词。"""
    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project, calls = await _a_turn_that_would_run(client, token, tmp_path, monkeypatch)
    session_id, path, previous_head = await _session_with_legacy_records(
        client, factory, token, project, "Existing release project")

    streamed = await client.post(
        f"/api/v1/chat/projects/{project}/stream", headers=_headers(token),
        json={"answer": {"kind": "text", "text": "接着做"}, "conversation_id": session_id})

    assert streamed.status_code == 200
    assert [event for event in _events(streamed) if event["type"] == "error"] == []
    assert len(calls) == 1, "升级完这一轮照跑 —— 这道闸不是拦路的，是开路的"
    assert (path / "derivation/derivation_log__proof.md").read_text() == "# 已有证明\n1 + 2 = 3\n"
    assert not (path / "derivation/artifacts/derivation_log__proof.json").exists()
    repository = get_project_repository()
    assert await _db_head(factory, session_id) != previous_head
    assert await _db_head(factory, session_id) == await run_in_repository_thread(
        repository._git, path, "rev-parse", "HEAD")
    records = await client.get(f"/api/v1/projects/{project}/repository/records",
                               params={"sessionId": session_id}, headers=_headers(token))
    assert records.status_code == 200, records.text
    assert "derivation_log__proof" in records.text


async def test_a_refused_upgrade_fails_this_turn_and_hands_over_the_migrator_reason(
    runtime_client, monkeypatch, tmp_path
):
    """迁移器对坏历史必须停下（永不伪造）—— 而"为什么停下"要出现在用户面前。

    2026-09-15 之前这句话只进服务端日志：启动时扫一遍、失败计进 `/health` 的
    一个计数，撞墙的人读到的是另一套文案。现在它就是这一轮的失败。
    """
    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project, calls = await _a_turn_that_would_run(client, token, tmp_path, monkeypatch)
    session_id, path, head = await _session_with_legacy_records(
        client, factory, token, project, "Tampered history", tamper=True)

    streamed = await client.post(
        f"/api/v1/chat/projects/{project}/stream", headers=_headers(token),
        json={"answer": {"kind": "text", "text": "接着做"}, "conversation_id": session_id})

    failure = next(event for event in _events(streamed) if event["type"] == "error")
    assert failure["code"] == "workspace_record_upgrade_failed"
    assert failure["retryable"] is False
    assert "Frozen envelope checksum mismatch" in failure["detail"], "迁移器的原话要留给运维"
    assert "Frozen envelope checksum mismatch" not in failure["message"], "但不是丢给用户读"
    assert "新建一个项目" not in failure["recovery"], "照这句做就是删掉自己的历史"
    assert "不要删除 artifacts" in failure["recovery"]
    assert calls == [], "读不了的工作区上不许起 worker"
    # 被拒的那个：Git、文件、DB head 一个字没动 —— 留给人修，不替它编历史。
    repository = get_project_repository()
    assert await run_in_repository_thread(repository._git, path, "rev-parse", "HEAD") == head
    assert (path / "derivation/artifacts/derivation_log__proof.json").is_file()
    assert await _db_head(factory, session_id) == head


async def test_a_live_worker_blocks_the_upgrade_instead_of_being_killed(runtime_client, monkeypatch):
    """攥着工作区的 worker 可能正跑着几小时的研究；空闲的也不杀 —— 挡回来。"""
    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    session_id, path, head = await _session_with_legacy_records(client, factory, token, project, "Held by a worker")

    class _IdleAdoptedWorker:
        alive = True
        paused = False
        conversation_in_flight = False
        terminated = 0

        async def terminate(self):
            self.terminated += 1

    worker = _IdleAdoptedWorker()
    monkeypatch.setattr(harness_session_manager, "_sessions",
                        {harness_session_manager._key(project, session_id): worker})

    with pytest.raises(RecordUpgradeBlocked, match="still holds this workspace"):
        await _upgrade(factory, project, session_id)

    assert worker.terminated == 0, "挡回来不是杀掉：空闲的 worker 也留给它自己退场"
    assert (path / "derivation/artifacts/derivation_log__proof.json").is_file()
    repository = get_project_repository()
    assert await run_in_repository_thread(repository._git, path, "rev-parse", "HEAD") == head
    assert await _db_head(factory, session_id) == head


async def test_a_stale_or_diverged_database_head_does_not_block_the_upgrade(runtime_client, monkeypatch):
    """DB 里那个 head 对不上，**不是**拒绝迁移的理由 —— 迁完它就被修好了。

    这道守卫（`Project HEAD changed outside migration`）上机量过：node20 上 19 个
    迁不动的工作区里 15 个倒在它手上，真正的坏历史只有 3 个。"DB head 对不上"
    是部署之后的常态（旧 worker 提交了、平台自己的旧提交没回写），11 个会话的
    DB head 连真实 HEAD 的祖先都不是 —— 这里用的就是那个形状。

    而它保护不了任何东西：迁移器在分离工作区里核验、打 backup_ref、只做 ff-merge，
    并且建在**真实 HEAD** 上。
    """
    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    monkeypatch.setattr(harness_session_manager, "_sessions", {})
    session_id, path, real_head = await _session_with_legacy_records(
        client, factory, token, project, "Head the database never caught up with")

    # 真实 HEAD 之外的一个 sha：同一个仓里存在，但不是 HEAD 的祖先（node20 上
    # 11 个会话就是这个形状）。
    repository = get_project_repository()
    stranger = await run_in_repository_thread(
        repository._git, path, "commit-tree", "-m", "别处的提交", f"{real_head}^{{tree}}")
    async with factory() as db:
        session = await db.scalar(select(SessionProjection).where(SessionProjection.session_id == session_id))
        session.git_head_commit_sha = stranger
        await db.commit()

    report = await _upgrade(factory, project, session_id)

    assert report and report["records"][0]["id"] == "derivation_log__proof"
    assert (path / "derivation/derivation_log__proof.md").is_file()
    migration_head = await run_in_repository_thread(repository._git, path, "rev-parse", "HEAD")
    assert migration_head not in {real_head, stranger}
    assert await _db_head(factory, session_id) == migration_head, "迁完把那份陈旧修好，而不是拿它当前提"
    # 迁完就没有旧信封了：下一轮一次 glob 就返回，连契约模块都不加载。
    assert await _upgrade(factory, project, session_id) is None


async def test_the_new_head_is_committed_before_the_turn_can_roll_it_back(
    runtime_client, monkeypatch, tmp_path
):
    """ff-merge 不可逆，它的记账就不能等别人的下一个 commit 顺手带上。

    磁盘上迁完、DB 跟着这一轮 rollback 的话，这个会话的 head 从此是错的，而
    checkpoint 权威拿它当 expected 且 fail-closed —— 后面每一次落盘都会被拒。

    顺带钉住"谁提交"这件事：让记账搭后面那句 `db.commit()` 的顺风车，等于把一次
    不可逆操作的记账托付给一段随时可能被重排的代码。
    """
    from app.services.execution_ingest import ExecutionIngestService

    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project, calls = await _a_turn_that_would_run(client, token, tmp_path, monkeypatch)
    session_id, path, previous_head = await _session_with_legacy_records(
        client, factory, token, project, "Turn dies right after the upgrade")

    async def ingest_that_dies(*_args, **_kwargs):
        raise RuntimeError("事实流写不进去")

    # 升级放行之后、这一轮任何一次 `db.commit()` 之前的第一件事就是摄取
    # `run_start`。让它炸在这里 —— 这就是那个窗口。
    monkeypatch.setattr(ExecutionIngestService, "ingest_raw_record", ingest_that_dies)

    streamed = await client.post(
        f"/api/v1/chat/projects/{project}/stream", headers=_headers(token),
        json={"answer": {"kind": "text", "text": "接着做"}, "conversation_id": session_id})

    assert next(event for event in _events(streamed) if event["type"] == "error")
    assert calls == [], "这一轮没跑起来 —— 正是要的那个窗口"
    repository = get_project_repository()
    migration_head = await run_in_repository_thread(repository._git, path, "rev-parse", "HEAD")
    assert migration_head != previous_head, "磁盘上迁完了"
    assert await _db_head(factory, session_id) == migration_head, "DB 不许跟着这一轮回滚"

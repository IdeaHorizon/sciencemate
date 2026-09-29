"""改动面板读到的那份响应，前后端钉在**同一批真样本**上。

## 为什么需要它（cuib 2026-09-14）

后端 `api/v1/revisions.py` 的十个端点被 `session_changes.py` 的三个取代，答案全部
改由 git 给；那份独立版本账（ChangeSet 行 / ChangeItem 行 / version id）连表一起
删了。前端的 `adaptSessionChangeSet` 留在原地，照旧要求 `items` / `id` / `status`
/ `createdAt` / `updatedAt` —— 后端一个都不发，于是**每个人、每个会话**展开改动
面板都是一句 `ChangeSet.items must be an array`。

它红着活了很久，是因为两边各自都绿：

- 后端 `test_the_payload_the_ui_reads_is_all_from_git` 断言的是**后端自己**的
  key 集合，还顺带写着「items / baseRevisionId 界面一个都没在看」—— 那句话是错的，
  而没有任何一处会发现它错了；
- 前端 `change-set-adapter.test.ts` 喂的是**手写 fixture**，而 fixture 是照着
  适配器自己的假设造的。

两份自洽的判据加起来，证明不了中间那条真实响应能被读懂。所以判据要落在**同一批
字节**上：这里用真实的端点产出它们，`features/sessions/lib/change-set-contract.test.ts`
用生产同一个适配器读它们。任何一边改了形状而另一边没跟上，两个测试里必有一个红。
（与 `platform/contracts/fixtures/chat-request/` 同一套办法，方向相反：那批是
请求体，前端生成后端认；这批是响应体，后端生成前端认。）

## 为什么比的是「形状」不是字节

sha、会话 id、时间戳每跑一次都不同。判据要问的是「字段还在不在、类型变没变」，
所以比的是 key → 类型名的递归投影。内容对不对由别的测试管（见
`test_git_is_the_only_ledger.py`）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services.project_repository import get_project_repository, run_in_repository_thread
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "session-change-set"
#: 置 1 重新生成 fixture（改了响应形状之后跑一次，然后把 diff 一起提交）。
UPDATING = os.getenv("AFS_UPDATE_CONTRACT_FIXTURES") == "1"


def shape(value: object) -> object:
    """key → 类型名的递归投影。数组取第一项的形状（空数组记成 []）。"""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return type(value).__name__


def _check(name: str, payload: dict) -> None:
    path = FIXTURES / f"{name}.json"
    if UPDATING:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return
    assert path.is_file(), (
        f"缺少契约样本 {path}。改了响应形状就跑一次 "
        "AFS_UPDATE_CONTRACT_FIXTURES=1 pytest tests/test_change_set_contract_fixtures.py"
    )
    recorded = json.loads(path.read_text(encoding="utf-8"))
    assert shape(payload) == shape(recorded), (
        f"change-set 响应形状变了，而 {path.name} 还是旧的。"
        "前端适配器读的就是这批样本 —— 更新它，让前端那条判据当场告诉你它还认不认。"
    )


@pytest.mark.asyncio
async def test_a_session_without_changes_matches_its_fixture(runtime_client) -> None:
    client, _factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    session_id = (await client.post(
        f"/api/v1/projects/{project_id}/sessions", headers=_headers(token),
        json={"title": "契约样本：干净会话"},
    )).json()["id"]

    response = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/change-set", headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    _check("clean-session", response.json())


@pytest.mark.asyncio
async def test_a_session_with_changes_and_a_conflict_matches_its_fixture(runtime_client) -> None:
    """一个真样本不够：空数组藏不住条目的形状，冲突那一支也从没被前端读过。"""
    client, _factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    session_id = (await client.post(
        f"/api/v1/projects/{project_id}/sessions", headers=_headers(token),
        json={"title": "契约样本：有改动有冲突"},
    )).json()["id"]

    repo = get_project_repository()
    worktree = Path(
        (await run_in_repository_thread(repo.session_status, project_id, session_id)).path
    )
    canonical = repo.project_path(project_id)

    (worktree / "SHARED.md").write_text("会话这边\n", encoding="utf-8")
    (worktree / "literature/notes.md").parent.mkdir(parents=True, exist_ok=True)
    (worktree / "literature/notes.md").write_text("只有会话动过\n", encoding="utf-8")
    await run_in_repository_thread(repo._configure_identity, worktree)
    await run_in_repository_thread(repo._git, worktree, "add", "SHARED.md", "literature/notes.md")
    await run_in_repository_thread(repo._git, worktree, "commit", "-m", "session edit")

    (canonical / "SHARED.md").write_text("main 那边\n", encoding="utf-8")
    await run_in_repository_thread(repo._configure_identity, canonical)
    await run_in_repository_thread(repo._git, canonical, "add", "SHARED.md")
    await run_in_repository_thread(repo._git, canonical, "commit", "-m", "main edit")

    response = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/change-set", headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changedPaths"], "这个样本的意义就是它非空"
    assert body["conflicts"], "冲突那一支也要有样本"
    _check("changed-with-conflict", body)

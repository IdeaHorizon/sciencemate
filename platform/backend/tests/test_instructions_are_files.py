"""指令是文件；文件只有一份；版本是 git（RFC X2 → X3）。

## X2 删掉的

三张表（`instruction_documents` / `instruction_versions` / `instruction_snapshots`）、
一套「起草 → 发布 → 版本」流程、一个 997 行的治理前端。它们回答的问题只有一个：
**这个会话看到的指令是什么。**

## X3 删掉的：那份抄件

X2 把答案搬进了会话行上的一列 JSON。那一列是文件的抄件，于是同一个问题有了
两个真相源 —— 而分叉时两边都不报错：

- 项目层：界面写 `<data_root>/projects/<id>/PROJECT.md`，agent 读**会话 worktree
  里的 `PROJECT.md`**。用户改完，agent 一个字都读不到。
- 个人层：写 `<data_root>/user/PROFILE.md`（路径里没有 user id，全机构一份），
  worker 的加载器读 `<harness home for this user>/user/PROFILE.md`。
- 那一列可以是 NULL（041 没 backfill），而跑轮那条路缺了就 raise —— node20 上
  99 个会话里 90 个因此一轮都发不出去。

所以现在没有抄件：平台读写的路径**就是 harness 读的那几个**，冻结交给 git。

## 这些判据钉的是什么

最关键的一条是 `test_the_platform_writes_where_the_harness_reads`：它不拿平台
的常量去比平台的常量（那只是在证明它跟自己一致），而是让 **harness 自己的
加载器**在平台算出来的 home 底下解析一次，两个路径必须是同一个文件。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services import instructions
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401

RESEARCHER = "researcher@atrium.local"


def _harness_resolved_paths(home: Path) -> tuple[Path, Path]:
    """让 harness **自己**在这个 home 下算一次个人层的两个文件。

    走的是 `core.directives_loader` 的真函数，不是抄一份路径表过来 ——
    抄件正是这次要删的东西。
    """
    import core.directives_loader as dl

    old = os.environ.get("HARNESS_FRAMEWORK_HOME")
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(home)
    try:
        return dl._profile_path(), dl._research_settings_path()
    finally:
        if old is None:
            os.environ.pop("HARNESS_FRAMEWORK_HOME", None)
        else:
            os.environ["HARNESS_FRAMEWORK_HOME"] = old


def test_the_platform_writes_where_the_harness_reads() -> None:
    """平台写的地方 = harness 读的地方，由 harness 的解析器当场判定。"""
    user_id = "user-abc"
    home = instructions.harness_home_for(user_id)
    profile, settings = _harness_resolved_paths(home)

    assert instructions.personal_instruction_path(user_id) == profile
    assert instructions.research_settings_instruction_path(user_id) == settings


def test_the_personal_layer_is_per_user() -> None:
    """个人层的路径里必须有 user id。

    从前没有：`<data_root>/user/PROFILE.md` 是**全机构一份**。org 档上一个人写
    的「只用中文回答」会进所有人的 system prompt，而没有任何一层会报错。
    """
    a = instructions.personal_instruction_path("user-a")
    b = instructions.personal_instruction_path("user-b")
    assert a != b
    assert "user-a" in str(a) and "user-b" in str(b)


def test_the_harness_home_has_one_definition() -> None:
    """worker 跑在哪个 home 下，和平台把指令写到哪个 home 下，是同一个函数算的。

    这两个各抄一行 `state_root / "users" / user.id` 的年代，就是"写一处读另一处"
    的出厂设置。
    """
    from app.services.harness_sessions import harness_home_for as from_sessions

    assert from_sessions is instructions.harness_home_for


def test_a_missing_file_is_empty_not_an_error() -> None:
    """没写过指令 = 没有指令，不是故障。

    这条语义是整层保持全函数的原因：缺失不再是一种能把会话卡死的状态。
    """
    assert instructions.read_instruction_file(
        instructions.personal_instruction_path("nobody")
    ) == ""


def test_writing_creates_the_directory() -> None:
    path = instructions.personal_instruction_path("user-mkdir")
    instructions.write_instruction_file(path, "# 只用中文回答\n")
    assert path.read_text(encoding="utf-8") == "# 只用中文回答\n"


def test_publishing_settings_is_idempotent_and_clears_when_empty() -> None:
    """平台维护的那半：内容没变不重写，空了就删掉，而不是留一份过期的。"""
    path = instructions.research_settings_instruction_path("user-settings")
    instructions.publish_research_settings("user-settings", "- Response language: zh-CN")
    first = path.stat().st_mtime_ns
    instructions.publish_research_settings("user-settings", "- Response language: zh-CN")
    assert path.stat().st_mtime_ns == first

    instructions.publish_research_settings("user-settings", "")
    assert not path.exists()


@pytest.mark.asyncio
async def test_editing_project_instructions_is_a_git_commit(runtime_client) -> None:
    """项目层：界面改的就是项目仓库里那个 `PROJECT.md`，一次改动 = 一次提交。

    从前这个端点写的是数据根底下一个同名文件 —— 没有历史，也到不了 agent 手里。
    """
    client, _factory = runtime_client
    token = await _token(client, RESEARCHER)
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]

    written = await client.put(
        f"/api/v1/projects/{project_id}/instructions",
        headers=_headers(token), json={"content": "# 走 API 写的"},
    )
    assert written.status_code == 200, written.text
    commit = written.json()["commit"]
    assert commit

    from app.services.project_repository import (
        get_project_repository,
        run_in_repository_thread,
    )

    # 仓库 I/O 不许跑在事件循环线程上 —— 测试也一样走那条唯一合法的路。
    on_disk = await run_in_repository_thread(
        get_project_repository().read_worktree_file, project_id, "PROJECT.md"
    )
    assert on_disk["content"] == "# 走 API 写的\n"

    read_back = await client.get(
        f"/api/v1/projects/{project_id}/instructions", headers=_headers(token),
    )
    assert read_back.json()["content"] == "# 走 API 写的\n"
    assert read_back.json()["commit"] == commit


@pytest.mark.asyncio
async def test_a_session_reads_its_own_branch_not_the_moving_main(runtime_client) -> None:
    """冻结是 git：会话看到的是**它自己分支上**那一份，主干后来改了不影响它。

    这正是那一列 JSON 想买的东西 —— 而 worktree 分支本来就已经提供了。
    """
    client, _factory = runtime_client
    token = await _token(client, RESEARCHER)
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]

    assert (await client.put(
        f"/api/v1/projects/{project_id}/instructions",
        headers=_headers(token), json={"content": "# 第一版"},
    )).status_code == 200

    created = await client.post(
        f"/api/v1/projects/{project_id}/sessions", headers=_headers(token),
        json={"title": "分支即冻结"},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    # 主干改了。
    assert (await client.put(
        f"/api/v1/projects/{project_id}/instructions",
        headers=_headers(token), json={"content": "# 第二版（改过了）"},
    )).status_code == 200

    effective = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/instructions",
        headers=_headers(token),
    )
    assert effective.status_code == 200, effective.text
    project_layer = effective.json()["layers"]["project"]
    assert "第一版" in project_layer["content"]
    assert "第二版" not in project_layer["content"]
    assert project_layer["source"] == "session_worktree"


@pytest.mark.asyncio
async def test_the_session_view_answers_even_without_a_snapshot(runtime_client) -> None:
    """041 之前建的会话也读得到 —— 因为没有"那一列"可缺了。

    从前这个端点对它们一律 404「这个会话没有冻结的指令」，而同一个空值在跑轮
    那条路上是硬 raise。同一个缺失、两处各编一个说法，两处都不指向真因。
    """
    client, _factory = runtime_client
    token = await _token(client, RESEARCHER)
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    session_id = (await client.post(
        f"/api/v1/projects/{project_id}/sessions", headers=_headers(token),
        json={"title": "没有快照"},
    )).json()["id"]

    payload = (await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/instructions",
        headers=_headers(token),
    ))
    assert payload.status_code == 200, payload.text
    assert set(payload.json()["layers"]) == {"project", "personal"}

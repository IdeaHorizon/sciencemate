"""数据根在两次启动之间被换掉 —— 平台必须停下来，不许带病服务。

## 2026-08-21 node20 实测

`project_worktree_root` 的默认值是相对路径，相对的是后端进程 cwd
（`current/platform/backend`，而 `current` 是指向 `releases/<sha>` 的软链）。
部署翻一次软链，科研数据的根就换一个地方。后果分三层，**每一层都没报错**：

1. 43 个既有会话在旧根下，新根下一个都没有 → 每发一条消息 0.1 秒内
   `Session Git worktree is not initialized`；用户看到的是"整个平台停了"。
2. 新会话在新根下**重新建**一套 Git 仓库 → 数据分裂两处，谁也不完整。
3. `/health/ready` 报 `"status":"ready"` —— 它检查库、检查 harness，
   就是没有一项问"我的数据还在我以为的地方吗"。

同一件事 8-19 发生过两次、8-20 被人手工 export 修回来、8-21 又来一次。

## 这些测试守什么

* 相对的根：在**配置层**就不合法（`data_root`），不给它"解析成某个东西"的机会。
* 配置的根 ≠ 库里记录的根：**启动闸拒绝启动**，并把两个根、搁浅数量、搬迁
  命令一起报出来。
* 判据本身（`stranded_by_root`）是纯函数，能被逐条变异打到。
"""
from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from app.config import DataRootError, data_root, settings
from app.main import _refuse_to_serve_where_the_data_is_not, stranded_by_root


class TestTheRootMustBeALocation:
    @pytest.mark.parametrize(
        "configured", ["data/project_worktrees", "./wt", "../shared/wt", "wt"]
    )
    def test_a_relative_root_is_refused(self, monkeypatch, configured):
        monkeypatch.setattr(settings, "project_worktree_root", configured)
        with pytest.raises(DataRootError) as raised:
            data_root("worktrees")
        assert configured in str(raised.value)

    def test_no_root_at_all_is_refused_with_the_variable_named(self, monkeypatch):
        monkeypatch.setattr(settings, "platform_data_root", "")
        monkeypatch.setattr(settings, "project_worktree_root", "")
        with pytest.raises(DataRootError) as raised:
            data_root("worktrees")
        assert "PLATFORM_DATA_ROOT" in str(raised.value), "报错必须点名要配哪个变量"

    def test_one_root_derives_all_four(self, monkeypatch):
        monkeypatch.setattr(settings, "platform_data_root", "/srv/data")
        for kind in ("repositories", "worktrees", "state", "sockets"):
            monkeypatch.setattr(
                settings,
                {"repositories": "project_repository_root",
                 "worktrees": "project_worktree_root",
                 "state": "harness_state_root",
                 "sockets": "harness_socket_root"}[kind],
                "",
            )
        assert data_root("repositories") == Path("/srv/data/project-repositories")
        assert data_root("worktrees") == Path("/srv/data/project-worktrees")
        assert data_root("state") == Path("/srv/data/harness-state")
        assert data_root("sockets") == Path("/srv/data/harness-sockets")

    def test_the_root_does_not_depend_on_the_current_directory(self, monkeypatch, tmp_path):
        """病根的充要条件：换个 cwd 必须还是同一个根。"""
        import os

        monkeypatch.setattr(settings, "platform_data_root", str(tmp_path / "d"))
        monkeypatch.setattr(settings, "project_worktree_root", "")
        original = Path.cwd()
        try:
            os.chdir(tmp_path)
            first = data_root("worktrees")
            deeper = tmp_path / "platform" / "backend"
            deeper.mkdir(parents=True, exist_ok=True)
            os.chdir(deeper)
            second = data_root("worktrees")
        finally:
            os.chdir(original)
        assert first == second


class TestTheStrandedPredicate:
    def test_paths_under_the_configured_root_are_not_stranded(self):
        configured = Path("/srv/data/project-worktrees")
        recorded = [f"/srv/data/project-worktrees/proj-{i}/sess-{i}" for i in range(3)]
        assert stranded_by_root(configured, recorded) == {}

    def test_paths_under_another_root_are_counted_by_that_root(self):
        configured = Path("/srv/data/project-worktrees")
        recorded = [
            "/rel/a5af2e8/backend/data/project_worktrees/p1/s1",
            "/rel/a5af2e8/backend/data/project_worktrees/p1/s2",
            "/srv/data/project-worktrees/p2/s3",
        ]
        assert stranded_by_root(configured, recorded) == {
            "/rel/a5af2e8/backend/data/project_worktrees": 2
        }

    def test_a_sibling_root_is_not_confused_with_the_configured_one(self):
        """前缀相同不算同一个根 —— `/srv/data-old/...` 不是 `/srv/data/...`。"""
        configured = Path("/srv/data/project-worktrees")
        assert stranded_by_root(configured, ["/srv/data-old/project-worktrees/p/s"]) == {
            "/srv/data-old/project-worktrees": 1
        }

    def test_nothing_recorded_is_not_stranded(self):
        assert stranded_by_root(Path("/srv/data/project-worktrees"), []) == {}
        assert stranded_by_root(Path("/srv/data/project-worktrees"), [None, ""]) == {}


class TestTheBootGate:
    """真跑启动闸，喂真表里的行 —— 只把"连哪个库"换成测试库，判据本身不替身。"""

    @pytest.fixture
    def gate_reads(self, db_engine, monkeypatch):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        import app.database as database

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        monkeypatch.setattr(database, "get_session_factory", lambda: factory)
        return factory

    @pytest.mark.asyncio
    async def test_it_refuses_when_the_recorded_root_is_elsewhere(
        self, db_session, gate_reads, monkeypatch, tmp_path
    ):
        await _insert_session(db_session, "/somewhere/else/project-worktrees/p1/s1")
        monkeypatch.setattr(settings, "platform_data_root", str(tmp_path / "data"))
        monkeypatch.setattr(settings, "project_worktree_root", "")

        with pytest.raises(RuntimeError) as raised:
            await _refuse_to_serve_where_the_data_is_not()
        message = str(raised.value)
        assert "/somewhere/else/project-worktrees" in message, "必须报出数据实际在哪"
        assert str(tmp_path / "data") in message, "必须报出配置指向哪"
        assert "relocate_project_data.py" in message, "必须给出合法的换根出口"

    @pytest.mark.asyncio
    async def test_it_starts_when_the_roots_agree(
        self, db_session, gate_reads, monkeypatch, tmp_path
    ):
        root = tmp_path / "data"
        await _insert_session(db_session, str(root / "project-worktrees" / "p1" / "s1"))
        monkeypatch.setattr(settings, "platform_data_root", str(root))
        monkeypatch.setattr(settings, "project_worktree_root", "")
        await _refuse_to_serve_where_the_data_is_not()  # 不抛 = 通过

    @pytest.mark.asyncio
    async def test_a_fresh_deployment_with_no_sessions_starts(
        self, gate_reads, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "platform_data_root", str(tmp_path / "data"))
        monkeypatch.setattr(settings, "project_worktree_root", "")
        await _refuse_to_serve_where_the_data_is_not()

    @pytest.mark.asyncio
    async def test_an_archived_session_does_not_block_startup(
        self, db_session, gate_reads, monkeypatch, tmp_path
    ):
        """归档的会话不再服务，它躺在老根下不构成"数据在别处"。"""
        await _insert_session(
            db_session, "/old/project-worktrees/p1/s9", archived=True
        )
        monkeypatch.setattr(settings, "platform_data_root", str(tmp_path / "data"))
        monkeypatch.setattr(settings, "project_worktree_root", "")
        await _refuse_to_serve_where_the_data_is_not()


async def _insert_session(db_session, worktree_path: str, *, archived: bool = False) -> None:
    """按真模型插一行 —— 这道闸读的就是这一列。"""
    from datetime import datetime

    from app.models.execution import SessionProjection

    db_session.add(
        SessionProjection(
            tenant_id="local-tenant",
            workspace_id="w1",
            project_id="proj-1",
            session_id=f"sess-{abs(hash(worktree_path)) % 10**8}",
            title="t",
            git_worktree_path=worktree_path,
            archived_at=datetime.now(UTC) if archived else None,
        )
    )
    await db_session.commit()

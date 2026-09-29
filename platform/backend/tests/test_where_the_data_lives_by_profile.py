"""数据在哪：个人档算得出来，组织档必须显式给。

个人档的根是用户 home 下一个固定目录（`~/.harness-framework`），不随部署改变，
所以给它一个默认值是安全的 —— 而且是必须的：一个刚下载的软件不该要求用户先
设一个环境变量。删掉那个目录 = 彻底卸载。

组织档一个都不猜。2026-08-21 丢过 43 个会话：相对路径的默认值跟着进程 cwd 走，
每次部署翻软链就换一个位置，而每一层拿到的都是一个"合法"的路径。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DataRootError, Settings, data_root, settings


def test_the_personal_profile_computes_its_own_root_and_database() -> None:
    personal = Settings(profile="personal", platform_data_root="", _env_file=None)
    assert personal.platform_data_root, "个人档必须自己算得出数据根"
    assert personal.database_url.startswith("sqlite+aiosqlite:///"), personal.database_url
    assert personal.platform_data_root in personal.database_url, (
        "库要落在数据根里 —— 删掉那个目录就等于卸载，库留在别处这条就不成立"
    )


def test_an_explicit_root_still_wins_in_the_personal_profile(tmp_path: Path) -> None:
    chosen = Settings(profile="personal", platform_data_root=str(tmp_path), _env_file=None)
    assert chosen.platform_data_root == str(tmp_path)
    assert str(tmp_path) in chosen.database_url


def test_an_explicit_database_is_never_overwritten(tmp_path: Path) -> None:
    given = Settings(
        profile="personal", platform_data_root=str(tmp_path),
        database_url="postgresql+asyncpg://u:p@example/db", _env_file=None,
    )
    assert given.database_url == "postgresql+asyncpg://u:p@example/db"


def test_the_org_profile_refuses_to_guess(monkeypatch) -> None:
    served = Settings(profile="org", platform_data_root="", _env_file=None)
    assert served.platform_data_root == ""
    assert served.database_url.startswith("postgresql")
    # 真正的判据在 data_root 上：算不出来就报错，并说清楚怎么修。
    monkeypatch.setattr(settings, "profile", "org")
    monkeypatch.setattr(settings, "platform_data_root", "")
    for kind in ("repositories", "worktrees", "state", "sockets"):
        monkeypatch.setattr(settings, {
            "repositories": "project_repository_root", "worktrees": "project_worktree_root",
            "state": "harness_state_root", "sockets": "harness_socket_root",
        }[kind], "")
        with pytest.raises(DataRootError) as excinfo:
            data_root(kind)
        assert "PLATFORM_DATA_ROOT" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_database_actually_opens_after_the_root_is_prepared(
    tmp_path: Path, monkeypatch,
) -> None:
    """最终判据：装配之后，个人档算出来的那个库真的连得上。

    2026-09-05 真机点验就是死在这里 —— 路径算得漂漂亮亮，目录一个都不存在，
    SQLite 报 `unable to open database file`，服务起不来。所有单测都绿，因为
    它们的根由 fixture 提前建好了。所以这条测试从一个**空目录**出发。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app import assembly
    from app.config import Settings

    root = tmp_path / "fresh-machine"
    fresh = Settings(profile="personal", platform_data_root=str(root), _env_file=None)
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "platform_data_root", fresh.platform_data_root)
    monkeypatch.setattr(settings, "database_url", fresh.database_url)
    for variable in ("project_repository_root", "project_worktree_root",
                     "harness_state_root", "harness_socket_root"):
        monkeypatch.setattr(settings, variable, "")

    assembly.prepare_the_data_root()

    engine = create_async_engine(fresh.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE probe (id TEXT)"))
    finally:
        await engine.dispose()
    assert (root / "db.sqlite").is_file()


def test_the_personal_profile_creates_the_root_it_named(tmp_path: Path, monkeypatch) -> None:
    """算得出路径 ≠ 那个路径存在。

    2026-09-05 真机点验：一台全新机器上 `~/.harness-framework/` 根本不存在，
    SQLite 于是报 `unable to open database file`，服务起不来 —— 而这正是
    「下载、打开、开跑」的第一步。所有单测都绿，因为它们的根由 fixture 建好了。
    """
    from app import assembly

    root = tmp_path / "brand-new"
    assert not root.exists()
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "platform_data_root", str(root))
    for variable in ("project_repository_root", "project_worktree_root",
                     "harness_state_root", "harness_socket_root"):
        monkeypatch.setattr(settings, variable, "")

    assembly.prepare_the_data_root()

    assert root.is_dir()
    for kind in ("repositories", "worktrees", "state", "sockets"):
        assert data_root(kind).is_dir(), kind
    # 只建目录，不写内容：一个刚打开还没做任何事的软件不该在磁盘上留下状态。
    assert not any(p.is_file() for p in root.rglob("*"))


def test_the_org_profile_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    """组织档的根由运维准备。应用替它建目录 = 把配错的根悄悄变成一个能用的根。"""
    from app import assembly

    root = tmp_path / "not-mine-to-make"
    monkeypatch.setattr(settings, "profile", "org")
    monkeypatch.setattr(settings, "platform_data_root", str(root))
    monkeypatch.setattr(settings, "secret_key", "a-real-secret-for-this-test")
    assembly.install(_AppStub())
    assert not root.exists()


class _AppStub:
    def __init__(self) -> None:
        self.dependency_overrides: dict = {}

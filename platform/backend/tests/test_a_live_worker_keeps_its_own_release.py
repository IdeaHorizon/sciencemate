"""换代不把代码从活 worker 脚下抽走（RFC 异步运行时 P0-5 / D8）。

## 为什么这条必须有测试锁着

部署布局是 `releases/<sha>/` + `current` 软链，`HARNESS_ROOT` 指向 `current`。
worker 起来之后还会**懒加载** import（很多模块是用到才 import 的）——如果它
的 sys.path/cwd 记的是那根软链，那么部署一翻，一个正跑到半路的 run 就会开始
加载**新版**代码，与它自己已经加载的旧版混在一起。

这种事故不会报错，只会让行为变得无法解释（"同一个 run 前后判据不一致"）。
钉住的机制是 `_paths()` 里的 `.resolve()` —— 一个删掉都不会有测试变红的
单词。所以在这里给它一条判据。

P0-1~P0-4 让 worker 活过后端重启，这条不变量因此才**真的**开始生效：
从前 worker 跟着后端一起死，根本活不到软链翻转。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.harness_sessions import _paths


def _fake_release(root: Path, name: str) -> Path:
    release = root / "releases" / name
    (release / "core").mkdir(parents=True)
    (release / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    return release


@pytest.fixture
def deploy_layout(tmp_path, monkeypatch):
    """releases/<sha> + current 软链 —— 与 deploy-node20.sh 同一套布局。"""
    from app.config import settings

    old = _fake_release(tmp_path, "sha-old")
    new = _fake_release(tmp_path, "sha-new")
    current = tmp_path / "current"
    current.symlink_to(old)

    monkeypatch.setattr(settings, "harness_root", str(current))
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path / "state"))

    worktree = tmp_path / "wt" / "p1" / "s1"
    worktree.mkdir(parents=True)

    class _Repo:
        def session_path(self, _project_id, _session_id):
            return worktree

    import app.services.project_repository as project_repository

    monkeypatch.setattr(project_repository, "get_project_repository", lambda: _Repo())
    return old, new, current


class _User:
    id = "user-1"


def test_the_spawn_root_is_the_real_release_not_the_symlink(deploy_layout):
    """spawn 时算出来的根必须是**真实 release 目录**。

    worker 的 cwd/PYTHONPATH 用的就是它 —— 是软链的话，懒加载 import 会在
    部署之后跨版本。
    """
    old, _new, current = deploy_layout
    root, *_ = _paths(_User(), "p1", "s1")
    assert root == old.resolve()
    assert root != current            # 软链本身不许流出去
    assert not root.is_symlink()


def test_flipping_current_does_not_change_an_already_resolved_root(deploy_layout):
    """**这条就是"活 run 不受部署影响"**：先算出来的那份路径不随软链改变。

    新 spawn 走新 release，老 worker 手里那份原地不动 —— 两代并存是特性，
    不是事故（注册表行的 code_version 让"谁跑的哪版"随时可查）。
    """
    old, new, current = deploy_layout
    pinned, *_ = _paths(_User(), "p1", "s1")

    current.unlink()
    current.symlink_to(new)           # 部署翻转

    assert pinned == old.resolve(), "已经交给 worker 的路径被换掉了"
    fresh, *_ = _paths(_User(), "p1", "s1")
    assert fresh == new.resolve(), "新 spawn 没走上新版本"


def test_an_invalid_release_is_refused_rather_than_half_started(deploy_layout, monkeypatch):
    """current 指向一个不完整的 release → 当场拒绝，别起半个进程。"""
    from app.config import settings

    _old, _new, current = deploy_layout
    broken = current.parent / "releases" / "sha-broken"
    broken.mkdir(parents=True)
    monkeypatch.setattr(settings, "harness_root", str(broken))

    from app.services.harness_sessions import HarnessSessionError

    with pytest.raises(HarnessSessionError):
        _paths(_User(), "p1", "s1")

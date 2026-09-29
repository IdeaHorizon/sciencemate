"""并发边界是 Session，不是 Project（v2.1 P4）。

Project v2 里 Session = 独立 worktree + 独立分支，两个 Session 本来就互不干扰。
锁如果是项目级的，同一项目的两个 Session 就被白白串行化 —— 多人协作直接卡死。
反过来，同一个 Session 被两个进程同时驱动会互相覆盖 conversation.json。

所以：同 Session 必须互斥，跨 Session 必须并行。
"""
from __future__ import annotations

import pathlib

import pytest

import platform_runtime as pr


def test_same_session_is_mutually_exclusive(tmp_path):
    root = tmp_path / "orchestrator__p1__session__s1"
    with pr._project_lock(root):
        with pytest.raises(pr.ProjectBusyError):
            with pr._project_lock(root):
                pass


def test_different_sessions_in_one_project_run_in_parallel(tmp_path):
    """同一项目的两个 Session 各有 worktree，串行化它们没有任何理由。"""
    a = tmp_path / "orchestrator__p1__session__s1"
    b = tmp_path / "orchestrator__p1__session__s2"
    with pr._project_lock(a), pr._project_lock(b):
        pass  # 两把锁同时持有即通过


def test_lock_is_released_after_the_block(tmp_path):
    root = tmp_path / "orchestrator__p1__session__s1"
    with pr._project_lock(root):
        pass
    with pr._project_lock(root):
        pass


def test_the_runtime_directory_name_carries_the_session():
    """锁的作用域由**目录名**决定，所以判据落在目录名上，不落在源码写法上。

    原来这条是拿 `inspect.getsource` 逐字匹配一行 f-string 的。那种写法有三个
    毛病（同一个仓库为它付过费）：把实现的写法冻在测试里、语义坏了照样绿、
    以及它自己会因为解析/重构而误报 —— 规则搬进共享契约模块的那一刻它就红了，
    而被测行为一个字没变。

    真正要守的是：同项目的两个 session 算出**不同**的目录，同一个 session
    算出**同一个**目录。
    """
    from core.worker_activity import session_dir_name

    assert session_dir_name("p1", "s1") != session_dir_name("p1", "s2")
    assert session_dir_name("p1", "s1") == session_dir_name("p1", "s1")
    assert "s1" in session_dir_name("p1", "s1")


def test_the_three_copies_of_the_layout_rule_agree():
    """命名规则有三份副本，这条把它们绑死。

    为什么有三份：worker 拿锁（`platform_runtime._project_lock`）、App Server
    找锁（`harness_sessions._session_lock_path`）、契约模块（正主）。前两处
    **故意不 import 契约** —— 它们是恢复链最底下的一步，挂到一次 import 上
    就是把"算得出这个文件名"变成"core / harness 根现在读得到吗"，而那两件事
    都会被一次配置抖动或一个测试的缓存污染掉（两者都实测发生过）。

    副本本身不是问题，**分叉时两边都不报错**才是。所以判据落在这里：
    写岔了当场红，而不是等到某天一个正在跑几小时研究的 worker 找不到自己的锁。
    """
    import inspect
    import re

    import platform_runtime as pr

    from core.worker_activity import ACTIVITY_FILENAME, LOCK_FILENAME, session_dir_name

    worker_side = re.search(r'lock_path = state_root / "([^"]+)"',
                            inspect.getsource(pr._project_lock))
    assert worker_side, "worker 侧的锁文件名不再是一个能被读出来的字面量"
    assert worker_side.group(1) == LOCK_FILENAME

    backend = (
        pathlib.Path(__file__).resolve().parents[1]
        / "platform" / "backend" / "app" / "services" / "harness_sessions.py"
    ).read_text(encoding="utf-8")
    assert f'_LOCK_FILENAME = "{LOCK_FILENAME}"' in backend
    assert f'_ACTIVITY_FILENAME = "{ACTIVITY_FILENAME}"' in backend
    assert (
        '_RUNTIME_DIR_TEMPLATE = "'
        + session_dir_name("{project_id}", "{session_id}")
        + '"'
    ) in backend


def test_both_processes_derive_the_same_lock_file(tmp_path):
    """worker 与 App Server 必须落在同一个文件上。

    这条从前靠"改任何一边都要同时改另一边"的注释来保证。规则现在只有一份，
    但**两个进程各自调它**这件事仍然要验 —— 调用错了一样会分叉，而分叉时
    两边都不报错：一边绑 A、一边找 B，找不到就当"没有 worker"，于是一个
    正在跑几小时研究的进程被判成尸体。
    """
    from core.worker_activity import activity_path, lock_path, session_dir_name

    root = tmp_path / session_dir_name("p1", "s1")
    with pr._project_lock(root):
        assert lock_path(root).is_file()
    # 活动文件与锁同目录、不同文件：一个答"谁拥有"，一个答"在不在干活"。
    assert activity_path(root) != lock_path(root)
    assert activity_path(root).parent == lock_path(root).parent


# ── 注册表行（RFC 异步运行时 P0-1）───────────────────────────────────────────

def test_lock_record_is_a_registry_row(tmp_path, monkeypatch):
    """锁记录同时是 worker 的注册表行：spawn_token（reattach 握手的身份凭证，
    pid 会复用、命令行会撞车）、code_version（活 run 跑的哪版代码，可对账）、
    protocol_version（drain 换代时对老 worker 只发老协议命令）。"""
    import json

    from core.worker_activity import lock_path

    monkeypatch.setenv("HARNESS_SPAWN_TOKEN", "tok-abc123")
    root = tmp_path / "orchestrator__p1__session__s1"
    with pr._project_lock(root):
        record = json.loads(lock_path(root).read_text(encoding="utf-8"))
    assert record["spawn_token"] == "tok-abc123"
    assert record["protocol_version"] == pr.PROTOCOL_VERSION
    # code_version 是增强字段：有 git 时是 sha，没有时空串，都不许炸。
    assert isinstance(record["code_version"], str)


def test_registry_row_without_token_is_still_valid(tmp_path, monkeypatch):
    """CLI/裸跑没有 HARNESS_SPAWN_TOKEN → 空串。字段是增强不是前提。"""
    import json

    from core.worker_activity import lock_path

    monkeypatch.delenv("HARNESS_SPAWN_TOKEN", raising=False)
    root = tmp_path / "orchestrator__p1__session__s1"
    with pr._project_lock(root):
        record = json.loads(lock_path(root).read_text(encoding="utf-8"))
    assert record["spawn_token"] == ""

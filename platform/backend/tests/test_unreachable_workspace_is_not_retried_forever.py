"""工作区已经不在磁盘上的 run，不该每 60s 重试一次、告警一次，直到永远。

## 现场（2026-08-24，本地库）

历史集成测试把一批 run 写进了真库。数据根后来从 `/private/tmp/p0-integration`
搬到 `~/harness-e2e/repo`，worktree 里的 `.git` 链接仍指着旧位置。交付对账每
一轮都去 `git rev-parse HEAD`，每一轮都是同一句 `fatal: not a git repository`：

    grep -c "delivery reconcile error" backend.log   → 4968

重试本身不贵，贵的是**真告警被埋在这 4968 条里没人看得见**。

## 判据锚在哪

两件事，分开锚：

1. `unreachable_workspace` —— 交付所需的路径链，第一个断在哪一环。**注意这批
   run 的仓库根是好的**（合法 git 仓库、还在原地），断的是 worktree 里那个
   `gitdir:` 指针。判据要是按第一直觉写成"仓库根不存在"，对真实病例一次都不
   触发，而测试照样全绿 —— 所以下面专门有一条用真实形状钉它。
2. 漏斗行为 —— 记过不可达证据的 run，下一轮**不出现在返回里**（⇒ 调度器不为
   它写日志）；那个路径一旦回来，同一条 run 无声地重新进漏斗。

变异任一处都该转红：探测链去掉 `linked_gitdir` 那一环、`delivery_block_holds`
改成恒 True（不再自愈）或恒 False（不再静音）、漏斗里的 `continue` 去掉。
"""
from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.deliverable_publishing import (
    delivery_block_holds,
    reconcile_pending_deliveries,
    unreachable_workspace,
)
from app.services.project_repository import get_project_repository


def _live_workspace(project_id: str = "proj-reach", session_id: str = "sess-reach"):
    """真建一个项目仓库 + session worktree（不用替身：被测的正是磁盘上的形状）。"""
    repo = get_project_repository()
    repo.initialize_project(
        project_id=project_id, name="Reach", description=None,
        research_domain=None, owner_id="owner",
    )
    repo.ensure_session_workspace(
        project_id=project_id, session_id=session_id, base_commit=None,
        title="Reach", created_by="owner",
    )
    return repo, project_id, session_id


# ── ① 探测链 ────────────────────────────────────────────────────────────────


def test_a_healthy_workspace_is_reachable():
    """路径链完好 → None ⇒ 失败另有原因，照旧重试、照旧告警。

    这条是静音机制的**边界**：没有它，一个恒返回 dict 的探测会让所有可重试
    失败都被静音，而症状是"交付再也不重试了"这种没人查得动的安静故障。
    """
    repo, pid, sid = _live_workspace()
    assert unreachable_workspace(repo, pid, sid) is None


def test_a_stale_gitdir_pointer_is_what_the_real_runs_look_like():
    """真实病例的形状：worktree 在、`.git` 在，但它指向的 gitdir 没了。

    这是 4968 条告警的那一批 run 的**原样**（数据根搬家后 `.git` 里的绝对路径
    还指着 `/private/tmp/p0-integration/...`）。仓库根和 worktree 目录此刻都
    健在 —— 把判据写成"仓库根不存在"的实现能通过前一条测试，却在这一条上转红。
    """
    repo, pid, sid = _live_workspace()
    worktree = repo.session_path(pid, sid)
    gone = Path("/private/tmp/does-not-exist-p0/repositories/x/.git/worktrees/y")
    (worktree / ".git").write_text(f"gitdir: {gone}\n", encoding="utf-8")

    found = unreachable_workspace(repo, pid, sid)
    assert found is not None, "gitdir 指针悬空 = 交付不可能成功，必须判为不可达"
    assert found["missing_path"] == str(gone)
    assert found["probe"] == "linked_gitdir"
    # 仓库根确实还在：证明这条判据不是靠"根没了"蒙对的
    assert repo.project_path(pid).exists()


def test_the_chain_reports_the_first_broken_link():
    """整条链每一环都验：仓库根 / worktree 目录 / worktree 的 `.git`。

    逐环扫，而不是列一张已知错误串的名单 —— 名单式护栏对下一种断法默认放行。
    """
    repo, pid, sid = _live_workspace("proj-chain", "sess-chain")
    worktree = repo.session_path(pid, sid)

    (worktree / ".git").unlink()
    assert unreachable_workspace(repo, pid, sid) == {
        "probe": "worktree_git_link", "missing_path": str(worktree / ".git"),
    }

    import shutil

    shutil.rmtree(worktree)
    assert unreachable_workspace(repo, pid, sid) == {
        "probe": "session_worktree", "missing_path": str(worktree),
    }

    shutil.rmtree(repo.project_path(pid))
    assert unreachable_workspace(repo, pid, sid) == {
        "probe": "repository_root", "missing_path": str(repo.project_path(pid)),
    }


# ── ② 记录是证据，判决现算 ──────────────────────────────────────────────────


def test_the_record_says_reported_the_probe_says_still_broken():
    """静音 = 报过了 **且** 现场重探仍不可达。判决不从记录里读。

    这条钉的是一个真栽过的坑：判据一度写成"记录里那个路径现在还缺不缺"。
    它对"数据根搬回来"成立，对**更可能发生的那种修复**却是反的 ——
    `git worktree repair` 把 `.git` 指回活着的 gitdir，那个悬空的旧路径永远不会
    回来，于是这条 run 被自己的历史证据永久关在门外。下面第二段就是那个形状：
    记录里的路径依旧不存在，但工作区已经好了 ⇒ 必须判定不再静音。
    """
    repo, pid, sid = _live_workspace("proj-hold", "sess-hold")
    worktree = repo.session_path(pid, sid)
    healthy_git = (worktree / ".git").read_text(encoding="utf-8")

    gone = "/private/tmp/does-not-exist-p0/never-coming-back"
    block = {"probe": "linked_gitdir", "missing_path": gone,
             "error": "ProjectRepositoryError: ...", "detected_at": "2026-08-24T00:00:00+00:00"}

    (worktree / ".git").write_text(f"gitdir: {gone}\n", encoding="utf-8")
    assert delivery_block_holds(block, repo, pid, sid) is True

    (worktree / ".git").write_text(healthy_git, encoding="utf-8")  # ← worktree repair
    assert not Path(gone).exists(), "记录里那个路径确实永远回不来 —— 这正是关键"
    assert delivery_block_holds(block, repo, pid, sid) is False, (
        "工作区已修好 ⇒ 必须解除静音，哪怕当初记下的那个路径永远不会回来"
    )


@pytest.mark.parametrize("block", [None, {}, {"missing_path": ""}, "not-a-dict"])
def test_a_missing_or_empty_record_never_blocks(block):
    """没有记录、记录残缺 ⇒ 不静音。默认必须是"照常重试"，静音得有据可查。

    工作区此刻**确实**不可达也一样：没报过就得先报一次（否则第一次故障就被
    悄悄吞掉，症状是交付无声地停了）。
    """
    repo, pid, sid = _live_workspace("proj-empty", "sess-empty")
    (repo.session_path(pid, sid) / ".git").write_text(
        "gitdir: /private/tmp/does-not-exist-p0/x\n", encoding="utf-8"
    )
    assert delivery_block_holds(block, repo, pid, sid) is False


# ── ③ 漏斗行为：吵一次，然后闭嘴；路径回来就自己解封 ────────────────────────


async def _funnel_fixture(db, *, run_id: str) -> tuple[str, str]:
    """一条能真正走进对账漏斗的 run（autonomous + root + 已完成 + 未 publish）。

    project / session / user 的 id 用**真 UUID**：这几列在库里就是 UUID 类型，
    拿 "proj-x" 这种自造串建出来的场景走不到被测那段代码（读行时就在类型转换
    上炸了），等于测了一个生产里不存在的形状。
    """
    from app.config import settings
    from app.models.execution import Run, RunStatus, SessionProjection
    from app.models.project import OperationMode, Project, ProjectConfig
    from app.models.user import User

    project_id, session_id, user_id = str(uuid4()), str(uuid4()), str(uuid4())
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="x",
                display_name="Blocked"))
    db.add(Project(id=project_id, name="Blocked", owner_id=user_id))
    db.add(ProjectConfig(project_id=project_id, operation_mode=OperationMode.AUTONOMOUS))
    db.add(SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=project_id, session_id=session_id, title="Blocked",
        created_by_user_id=user_id,
    ))
    db.add(Run(
        id=run_id, tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=project_id, session_id=session_id, parent_run_id=None,
        status=RunStatus.COMPLETED.value, ended_at=datetime.now(UTC) - timedelta(hours=1),
    ))
    await db.commit()
    return project_id, session_id


async def test_an_unreachable_run_is_reported_once_then_never_again(db_session):
    """核心行为：第一轮吵一次并落库，第二轮**一声不吭**。

    「只告警一次」在这里的机械含义是：第二轮的返回里根本没有这条 run（调度器
    只为返回里的 outcome 写日志）。变异漏斗里的 `continue` → 第二轮又出现 →
    转红。
    """
    rid = "run-funnel"
    pid, sid = await _funnel_fixture(db_session, run_id=rid)
    repo, _, _ = await asyncio.to_thread(_live_workspace, pid, sid)

    gone = Path("/private/tmp/does-not-exist-p0/gitdir")
    (repo.session_path(pid, sid) / ".git").write_text(f"gitdir: {gone}\n", encoding="utf-8")

    first = await reconcile_pending_deliveries(db_session)
    mine = [o for o in first if o["run_id"] == rid]
    assert len(mine) == 1, f"第一轮应如实回报一次（调度器据此发唯一那条告警）：{first}"
    assert mine[0]["unreachable"] == str(gone)
    assert mine[0]["probe"] == "linked_gitdir"
    assert mine[0]["error"], "唯一那条告警必须带上原始错误，否则它替代不了原来的日志"

    second = await reconcile_pending_deliveries(db_session)
    assert [o for o in second if o["run_id"] == rid] == [], (
        f"第二轮起必须完全安静，否则 4968 条告警照旧：{second}"
    )


async def test_the_evidence_is_persisted_on_the_run_row(db_session):
    """静音靠的是**落库的记录**，不是进程内的名单 —— 重启后照样安静。

    读一份新的 Run 行来验（不是内存里那个对象）：字段齐全到"只拿着这行记录的
    人能自己把判断重跑一遍" —— 缺的路径、断在哪一环、原始错误、什么时候发现的。
    """
    from app.models.execution import Run

    rid = "run-eviden"
    pid, sid = await _funnel_fixture(db_session, run_id=rid)
    repo, _, _ = await asyncio.to_thread(_live_workspace, pid, sid)
    gone = Path("/private/tmp/does-not-exist-p0/gitdir2")
    (repo.session_path(pid, sid) / ".git").write_text(f"gitdir: {gone}\n", encoding="utf-8")

    await reconcile_pending_deliveries(db_session)

    db_session.expire_all()
    row = await db_session.get(Run, rid)
    assert row.delivery_block is not None, "静音必须有据可查地落在库里"
    assert row.delivery_block["missing_path"] == str(gone)
    assert row.delivery_block["probe"] == "linked_gitdir"
    assert "ProjectRepositoryError" in row.delivery_block["error"]
    datetime.fromisoformat(row.delivery_block["detected_at"])  # 可解析的绝对时间
    assert delivery_block_holds(row.delivery_block, repo, pid, sid) is True


# ── ④ 那唯一一条告警本身 ────────────────────────────────────────────────────


def test_the_single_warning_says_what_it_is_and_how_it_ends(caplog):
    """既然一条 run 只吵这一次，这一句就得自带全部现场。

    锚在**内容**上而不是"发了一条 WARNING"上：缺哪个路径、断在哪一环、原始错误、
    以及它会自己解封（不然运维只知道"卡了"，不知道下一步做什么）。
    """
    import logging

    from app.services.delivery_scheduler import log_outcome

    with caplog.at_level(logging.WARNING, logger="app.services.delivery_scheduler"):
        log_outcome({
            "run_id": "run-x", "probe": "linked_gitdir",
            "unreachable": "/private/tmp/p0-integration/.../worktrees/w",
            "error": "ProjectRepositoryError: git rev-parse HEAD failed (128)",
        })

    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert "run-x" in line
    assert "/private/tmp/p0-integration/.../worktrees/w" in line, "得指名缺的是哪个路径"
    assert "linked_gitdir" in line, "得说清断在链子的哪一环"
    assert "git rev-parse HEAD failed (128)" in line, "原始错误不能丢"
    assert "reachable again" in line, "得说明它会自己解封，否则运维只能猜"


def test_a_retryable_error_still_gets_its_warning_every_pass(caplog):
    """静音只针对不可达。可重试的失败照旧每轮如实报 —— 别把降噪做成掩盖。"""
    import logging

    from app.services.delivery_scheduler import log_outcome

    with caplog.at_level(logging.WARNING, logger="app.services.delivery_scheduler"):
        log_outcome({"run_id": "run-y", "error": "HTTPException: revision conflict"})

    assert len(caplog.records) == 1
    assert "revision conflict" in caplog.records[0].getMessage()


async def test_a_repaired_workspace_re_enters_the_funnel(db_session):
    """自愈：`.git` 链接修好之后，同一条 run 重新被尝试 —— 不需要谁去解封。

    这条是"存证据不存判决"的**兑现**。变异 `delivery_block_holds` 成恒 True
    （= 写死判决）→ 转红。
    """
    rid = "run-heal"
    pid, sid = await _funnel_fixture(db_session, run_id=rid)
    repo, _, _ = await asyncio.to_thread(_live_workspace, pid, sid)

    worktree = repo.session_path(pid, sid)
    healthy_git = (worktree / ".git").read_text(encoding="utf-8")
    (worktree / ".git").write_text(
        "gitdir: /private/tmp/does-not-exist-p0/gitdir3\n", encoding="utf-8"
    )
    assert [o for o in await reconcile_pending_deliveries(db_session) if o["run_id"] == rid]
    assert [o for o in await reconcile_pending_deliveries(db_session) if o["run_id"] == rid] == []

    (worktree / ".git").write_text(healthy_git, encoding="utf-8")  # ← worktree repair 的效果
    again = [o for o in await reconcile_pending_deliveries(db_session) if o["run_id"] == rid]
    assert again, "工作区修好了 ⇒ 这条 run 必须重新被尝试，而不是被永久关在门外"
    assert "unreachable" not in again[0], f"已可达却仍判不可达：{again[0]}"

    from app.models.execution import Run

    db_session.expire_all()
    assert (await db_session.get(Run, rid)).delivery_block is None, (
        "已可达 ⇒ 那行记录必须抹掉，否则后来的人会把它当成「这条 run 还卡着」读"
    )

"""崩溃之后接着干：判据与起点。

## 现场（2026-08-10 实测）

一个跑了 1 小时 15 分的会话，已经 checkpoint 出 19 个产物、2 次真实提交。
子进程被杀之后：

  1. 点"恢复" → **409 session_not_recoverable**
  2. 另开新会话 → 那 19 个产物**一个都看不到**（它们在源会话分支上，
     而新会话从 project main 开分支）

两个独立缺陷，合起来的效果是：**几小时的真实研究搁浅在一条没人认领的分支上。**

## 缺陷一：恢复资格按"当初怎么死的"判

    App Server 自己崩（没发现）      → run 停在 stale_unknown → ✅ 可恢复
    子进程死掉、App Server 发现了     → run 记成 failed        → ❌ 不可恢复

**平台越是及时发现故障，用户越是恢复不了。** 判据反了。

这个文件里本来就有一段注释写着"恢复资格按'现在还能不能接着跑'判，不按'当初
怎么死的'判"，方向对，但落地时又落回了状态名单 —— 活性探针自己只扫
WAITING_HUMAN / STALE_UNKNOWN。同一个"硬编码枚举"的毛病，深了一层。

## 缺陷二：接续会话从**项目基线**开分支

checkpoint 机制的原话是"让有用的半成品留在 Session 分支上"。存了，而唯一的
续跑入口不读它 —— 又一次"机制存在但没接到路径"，且是最贵的一次。
"""
from __future__ import annotations

import inspect

from app.services import sessions as sessions_service


def _source() -> str:
    return inspect.getsource(sessions_service.recover_stale_session)


def test_recoverability_does_not_depend_on_how_it_died() -> None:
    """failed / incomplete / cancelled / stale_unknown 都要够得着恢复。

    用户关心的是"还有没有没干完的活"，不是"平台是怎么注意到的"。判据来自
    权威分类 UNFINISHED_RUN_STATUSES，不是又一份手写名单。
    """
    source = _source()
    assert "UNFINISHED_RUN_STATUSES" in source, (
        "恢复资格必须引用权威分类，覆盖'平台发现了'的那些死法"
    )


def test_a_live_runtime_still_blocks_recovery() -> None:
    """进程还活着就不许开分叉。

    ⚠️ 这条测试第一版写反了，写的是"不许查进程内注册表"，理由是多 worker 下
    它会误判。被既有用例 `test_live_runtime_is_not_recoverable` 当场证伪 ——
    **`failed` 不等于没有活运行时**：harness 会话进程跨轮保活，上一轮失败之后
    它照样活着等下一条消息。只看 run 状态就放行，单 worker 下**必然**多开分叉。

    两害相权：用注册表，多 worker 下可能多开一个（各有各的 worktree，不会两个
    驱动者写同一处）；不用，单 worker 下必然多开。所以用它，并把多 worker 的
    正解记着：给 session 加一个带心跳的 runtime 租约（共享事实），那是另一件事。
    """
    source = _source()
    assert "has_live_runtime" in source, "进程还活着时必须挡住恢复"
    assert "probe_was_inconclusive" in source, (
        "探针给出结论时以它为准，兜底判据不许覆盖它"
    )


def test_continuation_branches_from_the_dead_session_head() -> None:
    """接续会话的起点是源会话停下的地方，不是项目基线。

    否则 checkpoint 出来的半成品全部作废 —— 存了没人读，等于没存。
    """
    source = _source()
    assert "source.git_head_commit_sha" in source, (
        "接续必须从源会话 head 开分支，否则它看不到源会话已提交的工作"
    )
    assert "source_head or project_base_commit" in source, (
        "源会话没有 head（刚建就死）时才退回项目基线"
    )


def test_source_branch_is_left_untouched() -> None:
    """源分支保持原样：它是证据。新分支从它长出去，不是把它改掉。"""
    source = _source()
    for destructive in ("reset --hard", "branch -D", "push --force"):
        assert destructive not in source, f"恢复流程不许对源分支做 {destructive}"

"""算出来的 `missing_for_unattended` 必须真的被用来做决定（issue #798）。

它一直在被算、被记账、被交出去 —— 然后没有任何人读它做决定。一个算出来没人
消费的判据，等于那道防线不在场：自主档照样能开，作业照样在一台守不住写边界的
机器上跑，而"我们知道它守不住"只存在于一行日志里。
"""
from __future__ import annotations

import pytest

from app.services.unattended import judge_unattended


def test_a_machine_with_everything_just_runs() -> None:
    verdict = judge_unattended([], weak_resource_walls_are_acceptable=True)
    assert verdict.allowed and not verdict.reason and not verdict.note


@pytest.mark.parametrize("missing", ["write_boundary", "git_unwritable", "net_deny"])
def test_a_missing_boundary_is_refused_on_any_machine(missing: str) -> None:
    """不可恢复的三条，在**自己的电脑上也不让步**。

    缺了它们不是"弱一点"，是根本没有边界。判据不该因为"这是我自己的机器"
    就松口 —— 放着不管地跑等于把整台机器交给模型。
    """
    for own_machine in (True, False):
        verdict = judge_unattended([missing], weak_resource_walls_are_acceptable=own_machine)
        assert not verdict.allowed, (missing, own_machine)
        assert verdict.reason, "拒绝必须说清楚缺什么"
        assert "不会自己开下一轮" in verdict.reason, "拒绝要给出还能怎么办"


def test_weak_resource_walls_are_a_note_on_your_own_machine() -> None:
    verdict = judge_unattended(
        ["mem_cap", "pids_cap"], weak_resource_walls_are_acceptable=True
    )
    assert verdict.allowed
    assert "内存上限" in verdict.note and "进程数上限" in verdict.note
    assert not verdict.reason


def test_weak_resource_walls_stop_a_shared_executor() -> None:
    """共享执行器上跑飞会踩到别人 —— 同样的缺失，不同的判决。"""
    verdict = judge_unattended(
        ["mem_cap"], weak_resource_walls_are_acceptable=False
    )
    assert not verdict.allowed
    assert "影响别人" in verdict.reason


def test_an_unrecoverable_miss_beats_a_recoverable_one() -> None:
    """两种都缺时按重的判 —— 一句"注意资源占用"配不上"没有写边界"。"""
    verdict = judge_unattended(
        ["mem_cap", "net_deny"], weak_resource_walls_are_acceptable=True
    )
    assert not verdict.allowed
    assert "断不掉网络" in verdict.reason

"""worker 自报活动：三个概念各归各位（RFC 异步运行时 D10 + P0-6）。

被这个模块顶掉的那个错误形状：一把 operation lock 同时回答"谁拥有"、
"在不在干活"、"这句话能不能送达"，一个都答不对（2026-08-21 事故）。

这里守四件事：
  1. **写在盘上** —— 所以问它不需要它有空回答（跑轮中命令循环是堵住的）；
  2. **租约会衰减** —— 沉默太久读出 `unknown`，而 unknown 只呈现不判决；
  3. **停靠有自己的租约** —— 拿心跳量停靠，每一次正常停靠都会被读成 unknown；
  4. **回 idle 要清干净** —— 留着上一轮的 request_id 比没有更糟。
"""
from __future__ import annotations

import json
import time

import pytest

from core.worker_activity import (
    UNKNOWN,
    WORKING_LEASE_SECONDS,
    ActivityWriter,
    activity_path,
    lock_path,
    read_activity,
    session_dir_name,
)


def test_ownership_and_activity_are_different_files(tmp_path):
    """活动不能塞进锁文件。

    flock 挂在**那个 inode** 上，而原子改写只有 `os.replace` —— 它换的正是
    inode。锁就此失效，且两个进程都不会报错（一个还以为自己攥着，另一个
    抢得到）。所以它们必须是两个文件。
    """
    assert activity_path(tmp_path) != lock_path(tmp_path)


def test_a_missing_file_is_not_idle(tmp_path):
    """"没自报过"和"自报了空闲"是两件事，读侧必须分得出来。

    合并成一个值 = 老 worker（不写这个文件）会被读成"闲着"，而它可能正跑着
    一个几小时的研究。把它当尸体回收就是从这一步开始的。
    """
    assert read_activity(activity_path(tmp_path)).present is False


def test_what_it_is_doing_survives_without_it_answering(tmp_path):
    """自报落盘 = 不需要它有空回答。"""
    writer = ActivityWriter(activity_path(tmp_path), spawn_token="tok", command=["a", "b"])
    writer.set_state(
        "working",
        detail={"operation": "turn"},
        turn_id="turn-42",
        app_binding={"run_id": "run-7"},
    )
    activity = read_activity(activity_path(tmp_path))
    assert activity.present and activity.state == "working"
    assert activity.turn_id == "turn-42"
    assert activity.app_binding["run_id"] == "run-7"
    assert activity.command == ("a", "b")
    assert activity.occupied is True


def test_silence_while_working_decays_to_unknown(tmp_path):
    """干活就该有事件产出。静默超过租约 = 我们不知道了。

    ⚠️ 读出来的 `unknown` 是"不知道"，不是"死了" —— 本模块因此不提供任何
    "所以可以杀它"的判据。
    """
    path = activity_path(tmp_path)
    ActivityWriter(path).set_state("working")
    fresh = read_activity(path)
    assert fresh.state == "working"

    stale = read_activity(path, now=time.time() + WORKING_LEASE_SECONDS + 1)
    assert stale.declared == "working", "它说的话不许被改写"
    assert stale.state == UNKNOWN, "我们观察到的必须是'不知道'"
    assert stale.occupied is True, "不知道在不在跑时，正确行为是别去撞它"


def test_parking_is_measured_by_its_own_deadline_not_by_heartbeats(tmp_path):
    """停靠期间**本来就没有事件**。

    拿心跳去量它，每一次正常停靠（unattended 的复查间隔可以是 4 小时）都会
    被读成 unknown —— 这正是 2026-08-21 那句「平台内部错误」的上游。
    """
    path = activity_path(tmp_path)
    until = time.time() + 4 * 3600
    ActivityWriter(path).set_state("parked", detail={"until": until, "why": "recheck"})

    mid_nap = read_activity(path, now=time.time() + 3 * 3600)
    assert mid_nap.state == "parked", "睡到一半被读成 unknown"
    assert mid_nap.detail["why"] == "recheck", "沉默的原因必须说得出口"

    overslept = read_activity(path, now=until + 10_000)
    assert overslept.state == UNKNOWN, "该醒没醒，如实说不知道"


def test_waiting_for_a_human_may_be_silent_forever(tmp_path):
    """停在问题上的沉默是合法的：死活由"进程还在不在"回答，不由租约回答。"""
    path = activity_path(tmp_path)
    ActivityWriter(path).set_state("waiting_human", detail={"pause_id": "p1"})
    later = read_activity(path, now=time.time() + 30 * 24 * 3600)
    assert later.state == "waiting_human"
    assert later.detail["pause_id"] == "p1"


def test_going_idle_clears_the_turn_it_is_no_longer_running(tmp_path):
    """「从不更新的字段不是事实」：留着上一轮的 request_id，事后取证会读出
    一个早就结束的轮次 —— 比没有更糟。"""
    path = activity_path(tmp_path)
    writer = ActivityWriter(path)
    writer.set_state("working", turn_id="turn-1", app_binding={"run_id": "r1"})
    writer.set_state("idle")
    activity = read_activity(path)
    assert activity.state == "idle"
    assert activity.turn_id == ""
    assert activity.app_binding == {}


def test_a_departed_process_leaves_no_self_report(tmp_path):
    """进程没了，它的自报就该消失，而不是永远停在"它说它空闲"。"""
    path = activity_path(tmp_path)
    writer = ActivityWriter(path)
    writer.set_state("idle")
    writer.close()
    assert read_activity(path).present is False


def test_only_declarable_states_can_be_declared(tmp_path):
    """`unknown` 是**观察结果**，不是自报值。允许自报它就等于允许它撒谎。"""
    with pytest.raises(ValueError, match="unknown"):
        ActivityWriter(activity_path(tmp_path)).set_state("unknown")


def test_the_heartbeat_is_throttled_but_the_state_change_is_not(tmp_path, monkeypatch):
    """心跳挂在事件产出上，而事件一轮里有几万条 —— 不节流就是几万次写盘。
    状态变化不能节流：它是别人据以行动的事实。"""
    path = activity_path(tmp_path)
    writer = ActivityWriter(path)
    writer.set_state("working")
    first = json.loads(path.read_text(encoding="utf-8"))["heartbeat_at"]
    import core.worker_activity as activity
    from types import SimpleNamespace
    monkeypatch.setattr(activity, "time", SimpleNamespace(time=lambda: first + 0.1))
    writer.touch()
    assert json.loads(path.read_text(encoding="utf-8"))["heartbeat_at"] == first
    writer.touch(force=True)
    assert json.loads(path.read_text(encoding="utf-8"))["heartbeat_at"] > first


def test_a_half_written_file_never_reaches_the_reader(tmp_path):
    """读者没有锁，所以写必须是原子替换。读到半个 JSON 的后果不是"稍等再读"，
    是"这个 worker 看起来没自报过活动" = 接不回来。"""
    path = activity_path(tmp_path)
    writer = ActivityWriter(path)
    for index in range(50):
        writer.set_state("working", turn_id=f"turn-{index}")
        assert read_activity(path).present, "读到过一次残缺内容"


def test_the_session_directory_name_is_derived_once(tmp_path):
    """两个进程必须算出同一个目录名 —— 抄一份就会分叉，且分叉时两边都不报错。"""
    assert session_dir_name("p", "s") == session_dir_name("p", "s")
    assert session_dir_name("p", "s1") != session_dir_name("p", "s2")

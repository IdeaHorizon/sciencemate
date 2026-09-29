"""忙等的根因修复（E2E-4 实测事故的类级修法）。

现场：writing 在后台正常跑到 turn 27，orchestrator 无事可做，于是每 3-7 秒被
唤醒一次、每轮烧一次 200+ 消息的 LLM 调用只为说一句"检查 writing 进度" ——
10 分钟 47 轮、6.7M prompt tokens。副作用比浪费更糟：

  1. 被问了 47 次"要不要做点什么"之后，模型开始怀疑子节点卡死并去掐它；
  2. 等待时自然说同样的话，撞上一字不差重复检测 → 整个 continuous 被停掉。

根因不是阈值不对，而是**"等"这件事被交给了模型**。这里断言等待已经收回框架。
"""
from __future__ import annotations

import asyncio
import json
import time

import chat as chat_mod
from core import run_history
from core.bootstrap import bootstrap

bootstrap()

POLL_REPLY = "检查 writing 进度。\nCONTINUOUS_STATUS: continue"


def _state(tmp_path):
    st = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    st.hook_state.update(continuous_loop=True, continuous_phase="running")
    return st


def _inflight_child(base, name="1785313204-4fbe72", *, node_type="writing"):
    """在飞子 run：有 transcript、没 summary.json。"""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.jsonl").write_text(
        json.dumps({"event": "run_start", "node_type": node_type}) + "\n",
        encoding="utf-8")
    return d


def _child_appends(d, n=1):
    with (d / "transcript.jsonl").open("a", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"event": "llm_response", "turn": i}) + "\n")


def _finish_child(d, *, node_type="writing", project_id):
    (d / "summary.json").write_text(json.dumps({
        "node_type": node_type, "project_id": project_id,
        "status": "completed", "artifacts": [],
    }), encoding="utf-8")


def _fast(monkeypatch, *, first_budget=0.3):
    """把秒级常量压到亚秒，测试才跑得动。语义不变。

    注意 first_budget 现在是**默认等待**（agent 没设 check_in 时用），
    不再是"第一档阶梯" —— 阶梯已删，见 _CHILD_WAIT_DEFAULT_S 的说明。
    """
    monkeypatch.setattr(chat_mod, "_CHILD_POLL_S", 0.02)
    monkeypatch.setattr(chat_mod, "_CHILD_WAIT_DEFAULT_S", first_budget)
    monkeypatch.setattr(chat_mod, "_CHILD_WAIT_MAX_S", first_budget * 20)
    monkeypatch.setattr(chat_mod, "_CHILD_CHECK_IN_MIN_S", 0.01)
    # 续问退避也得压 —— 它原来是内联的魔法数字，压不到，一条测试因此实等
    # 31.75 秒（0.25/0.5/1/2/4/8/8/8）。语义不变：仍然是"翻倍、封顶"。
    monkeypatch.setattr(chat_mod, "_FOLLOWUP_BACKOFF_BASE_S", 0.002)
    monkeypatch.setattr(chat_mod, "_FOLLOWUP_BACKOFF_MAX_S", 0.05)


# ── 观测层：一个事实来源 ────────────────────────────────────────────────────

def test_child_activity_sees_inflight_and_its_writes(tmp_path):
    d = _inflight_child(tmp_path)
    act = run_history.child_activity(tmp_path, project_id=None)
    assert act.any_inflight and act.n_inflight == 1
    assert act.node_types == ("writing",)
    before = act.total_bytes
    _child_appends(d, 3)
    after = run_history.child_activity(tmp_path, project_id=None)
    assert after.total_bytes > before, "子节点在写 = 可观测"
    assert after.quiet_seconds(time.time()) < 5.0


def test_child_activity_ignores_finished_runs(tmp_path):
    d = _inflight_child(tmp_path)
    _finish_child(d, project_id=None)
    assert not run_history.child_activity(tmp_path, project_id=None).any_inflight


def test_unreadable_mtime_reports_infinite_quiet():
    """读不到 mtime → 倾向唤醒模型。

    反方向（当成刚写过）会让框架永久等一个已经死掉的子节点 —— 静默挂死比多叫
    一次模型坏得多。这是 fail-open 方向的显式断言。
    """
    act = run_history.ChildActivity(n_inflight=1, last_write_ns=0)
    assert act.quiet_seconds(time.time()) == float("inf")


# ── 退避：自身空转才等 ──────────────────────────────────────────────────────

def test_agent_sets_its_own_check_in():
    """等多久由 agent 在状态行里说了算 —— 框架不再写死阶梯。"""
    assert chat_mod._continuous_check_in(
        "CONTINUOUS_STATUS: continue check_in=3600") == (3600.0, None)
    # 没写 → 用默认
    assert chat_mod._continuous_check_in("CONTINUOUS_STATUS: continue") == (None, None)


def test_check_in_bounds_are_told_not_silently_applied():
    """越界要**如实告知**，不能悄悄改掉它设的值。"""
    secs, note = chat_mod._continuous_check_in(
        "CONTINUOUS_STATUS: continue check_in=5")
    assert secs is None and note and "忽略" in note, "太短要说明为什么忽略"

    secs, note = chat_mod._continuous_check_in(
        "CONTINUOUS_STATUS: continue check_in=999999")
    assert secs == chat_mod._CHILD_WAIT_MAX_S
    assert note and "截断" in note, "截断了必须告诉它"


def test_status_line_still_parses_without_check_in():
    """老格式必须照旧работать（别把既有契约改坏）。"""
    for line, want in (("CONTINUOUS_STATUS: continue", "continue"),
                       ("CONTINUOUS_STATUS: complete", "complete"),
                       ("CONTINUOUS_STATUS: blocked", "blocked")):
        assert chat_mod._continuous_status(line) == want
    assert chat_mod._continuous_status(
        "CONTINUOUS_STATUS: continue check_in=900") == "continue"


def test_no_inflight_child_means_no_waiting(tmp_path, monkeypatch):
    """没有在飞子节点 → 行为完全不变（改动的爆炸半径就这么大）。"""
    _fast(monkeypatch)
    st = _state(tmp_path)
    assert asyncio.run(chat_mod._wait_for_child_progress(st)) is None


def test_new_dispatch_is_never_blocked(tmp_path, monkeypatch):
    """它刚起了新 run → 立刻给它下一轮，别把并行派发压在等待后面。

    判据是**权威 run 状态**，不是文件指纹 —— 老版用指纹，结果把 orchestrator
    自己的上下文压缩（会写 memory candidates）当成项目进展，等待阶梯被反复清零，
    一分钟叫醒 4 次（E2E-4 实测）。
    """
    _fast(monkeypatch, first_budget=5.0)
    st = _state(tmp_path)
    _inflight_child(st.root.parent)
    asyncio.run(chat_mod._wait_for_child_progress(st))       # 建立基线
    _inflight_child(st.root.parent, name="1785399999-newrun",
                    node_type="experiment")                  # 又起一个
    t0 = time.time()
    assert asyncio.run(chat_mod._wait_for_child_progress(st)) is None
    assert time.time() - t0 < 0.3, "刚派发就该立刻拿到下一轮"


def test_own_housekeeping_does_not_reset_the_wait(tmp_path, monkeypatch):
    """orchestrator 自己写产物/压缩上下文**不算**项目进展，不该免掉等待。

    这条正是 E2E-4 那个 bug 的反向断言。
    """
    _fast(monkeypatch, first_budget=0.4)
    st = _state(tmp_path)
    _inflight_child(st.root.parent)
    asyncio.run(chat_mod._wait_for_child_progress(st))       # 建立基线
    st.save_artifact("scratchpad", "note", "自己的上下文压缩产物", {})
    t0 = time.time()
    w = asyncio.run(chat_mod._wait_for_child_progress(st))
    assert w is not None, "自己的housekeeping 不该免掉等待"
    assert time.time() - t0 >= 0.3, "该等还得等"


def test_waits_while_child_works_then_wakes_on_finish(tmp_path, monkeypatch):
    _fast(monkeypatch, first_budget=2.0)
    st = _state(tmp_path)
    d = _inflight_child(st.root.parent)

    async def _go():
        await chat_mod._wait_for_child_progress(st)          # 基线轮
        async def _finish_soon():
            await asyncio.sleep(0.15)
            _finish_child(d, project_id=st.project_id)
        task = asyncio.ensure_future(_finish_soon())
        w = await chat_mod._wait_for_child_progress(st)
        await task
        return w

    w = asyncio.run(_go())
    assert w and w["woke_on"] == "children_finished"
    assert w["waited_seconds"] < 1.5, "子节点一结束就该立刻醒，不等满预算"


def test_silence_alone_never_wakes_the_model(tmp_path, monkeypatch):
    """静默**不再**是唤醒理由 —— 它是信息的缺席，不是事件。

    E2E-5b 实测：agent 明说 check_in=1800，静默规则却每 ~4 分钟把它叫起来一次
    （5 次唤醒全是 child_went_quiet，平均间隔 236s）。两个机制在回答同一个问题
    （"没事发生时多久叫一次"），必然打架 —— 当时的修法冲动是加一条优先级规则，
    那是第四个补丁。删掉重复的那个才是根因。
    """
    _fast(monkeypatch, first_budget=0.4)
    st = _state(tmp_path)
    _inflight_child(st.root.parent)          # 建好就再也不写 → 一直静默
    asyncio.run(chat_mod._wait_for_child_progress(st))   # 建立基线（不等）
    w = asyncio.run(chat_mod._wait_for_child_progress(st))
    assert w and w["woke_on"] == "check_in_elapsed", \
        f"静默不该唤醒，实际 woke_on={w and w['woke_on']}"
    assert w["waited_seconds"] >= 0.3, "该等满自己的预算"
    # 静默作为**事实**仍要告诉它（供给事实，不替它判断）
    assert w["child_quiet_seconds"] is not None


def test_agent_check_in_is_not_overridden_by_silence(tmp_path, monkeypatch):
    """agent 说等 N 秒，静默不许把它提前叫起来 —— 它已经做过判断了。"""
    _fast(monkeypatch, first_budget=0.05)
    st = _state(tmp_path)
    _inflight_child(st.root.parent)
    st.hook_state["continuous_check_in_s"] = 1.0
    asyncio.run(chat_mod._wait_for_child_progress(st))   # 基线
    t0 = time.time()
    w = asyncio.run(chat_mod._wait_for_child_progress(st))
    assert time.time() - t0 >= 0.9, "agent 说 1 秒就该等够 1 秒"
    assert w["woke_on"] == "check_in_elapsed"


def test_user_stop_interrupts_the_wait(tmp_path, monkeypatch):
    """机械等待必须可打断，否则 /stop 要等 5 分钟才生效。"""
    _fast(monkeypatch, first_budget=3.0)
    st = _state(tmp_path)
    d = _inflight_child(st.root.parent)

    async def _go():
        await chat_mod._wait_for_child_progress(st)
        async def _keep_alive_then_stop():
            for _ in range(5):
                await asyncio.sleep(0.04)
                _child_appends(d)
            st.hook_state["continuous_phase"] = "aborted"
        task = asyncio.ensure_future(_keep_alive_then_stop())
        w = await chat_mod._wait_for_child_progress(st)
        await task
        return w

    w = asyncio.run(_go())
    assert w and w["woke_on"] == "user_stopped"
    assert w["waited_seconds"] < 2.0


# ── E2E-4 回放：接在真路径上 ────────────────────────────────────────────────

def test_replays_e2e4_busywait_no_longer_aborts(tmp_path, monkeypatch):
    """47 轮"检查 writing 进度"不再把 continuous 停掉。

    走 `_queue_continuous_followup` 真路径 —— 只测 `_wait_for_child_progress`
    的话，把那行接线摘掉测试照样全绿（今天已经因为这个吃过三次亏）。
    """
    _fast(monkeypatch, first_budget=0.05)
    st = _state(tmp_path)
    d = _inflight_child(st.root.parent)
    cs = chat_mod.ChatState()

    async def _go():
        for _ in range(10):
            _child_appends(d)          # writing 一直在正常干活
            ok = await chat_mod._queue_continuous_followup(
                st, cs, POLL_REPLY, reason="turn_finished")
            if not ok:
                return False
        return True

    assert asyncio.run(_go()) is True, "子节点正常工作时不该被判空转而停机"
    assert st.hook_state["continuous_phase"] == "running"
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "no_delta_repeat" not in tr, "一字不差检测不该咬到忙等自己的尾巴"
    assert "continuous_child_wait" in tr, "等待必须留痕"


def test_wait_note_tells_the_model_time_passed(tmp_path, monkeypatch):
    """框架静悄悄吞掉几分钟是不行的 —— 广告的和默认给的必须一致。"""
    _fast(monkeypatch, first_budget=0.05)
    st = _state(tmp_path)
    d = _inflight_child(st.root.parent)
    cs = chat_mod.ChatState()

    async def _go():
        for _ in range(3):
            _child_appends(d)
            await chat_mod._queue_continuous_followup(
                st, cs, POLL_REPLY, reason="turn_finished")
        out = []
        while not cs.input_queue.empty():
            out.append(cs.input_queue.get_nowait())
        return out

    prompts = asyncio.run(_go())
    assert any("框架已代你等待" in p for p in prompts)
    assert any("不消耗你的轮次" in p for p in prompts)
    assert any("writing" in p for p in prompts), "要指名在等谁"
    assert any("check_in=" in p for p in prompts), \
        "没设 check_in 时要就地教它怎么设（在它正要决定的那一刻）"


WAITING_REPLY = "Writing 在飞。不重复查进度。\nCONTINUOUS_STATUS: continue"


def test_waiting_on_a_live_child_is_not_stuck(tmp_path, monkeypatch):
    """子节点在飞时反复说"我在等"**不算卡死** —— 那正是我们要它做的。

    E2E-4 实测（07:45:53 / 07:45:56 / 07:46:15 / 07:46:33）：writing 在后台跑，
    orchestrator 正确地说"Writing 在飞。不重复查进度。"，一字不差三次 →
    被 no_delta_repeat 停机。而 writing **5 分钟后就跑完了**，停机纯属白挨一刀，
    项目从此空转 21.9 小时。

    症状是"模型重复"，根因是"框架在没事时反复问它" + "把正确的稳定状态当卡死"。
    """
    _fast(monkeypatch, first_budget=0.05)
    st = _state(tmp_path)
    d = _inflight_child(st.root.parent)
    cs = chat_mod.ChatState()

    async def _go():
        for _ in range(8):
            # 子节点静默（正卡在一次长工具调用里 —— 事故现场就是这样）
            ok = await chat_mod._queue_continuous_followup(
                st, cs, WAITING_REPLY, reason="turn_finished")
            if not ok:
                return False
        return True

    assert asyncio.run(_go()) is True, "子节点在飞时说'我在等'不该被判卡死停机"
    assert st.hook_state["continuous_phase"] == "running"
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "no_delta_repeat" not in tr


def test_still_stops_when_nothing_is_running(tmp_path, monkeypatch):
    """什么都没跑却原地重复 → 仍然该停（别把卡死检测整个废掉）。"""
    _fast(monkeypatch)
    st = _state(tmp_path)
    cs = chat_mod.ChatState()

    async def _go():
        for _ in range(6):
            ok = await chat_mod._queue_continuous_followup(
                st, cs, WAITING_REPLY, reason="turn_finished")
            if not ok:
                return False
        return True

    assert asyncio.run(_go()) is False, "无子节点在飞 + 一字不差 = 真卡死，该停"
    assert st.hook_state["continuous_phase"] == "aborted"


def test_agent_check_in_controls_how_long_we_wait(tmp_path, monkeypatch):
    """agent 说等多久就等多久 —— 框架不再写死阶梯。"""
    _fast(monkeypatch, first_budget=0.05)
    st = _state(tmp_path)
    _inflight_child(st.root.parent)
    cs = chat_mod.ChatState()

    async def _go():
        # 第一轮建立基线
        await chat_mod._queue_continuous_followup(
            st, cs, WAITING_REPLY, reason="turn_finished")
        t0 = time.time()
        await chat_mod._queue_continuous_followup(
            st, cs, "在等。\nCONTINUOUS_STATUS: continue check_in=1",
            reason="turn_finished")
        return time.time() - t0

    elapsed = asyncio.run(_go())
    assert elapsed >= 0.9, f"agent 说等 1 秒就该等够，实际 {elapsed:.2f}s"
    assert st.hook_state["continuous_check_in_s"] == 1.0

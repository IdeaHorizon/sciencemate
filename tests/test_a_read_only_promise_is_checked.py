"""用户说了"只读"，收尾时要拿真实事件对一遍（#973）。

现场：用户明说「不要修改文件」，这次 run 实际写了 **24 个** Project 文件，
而最终回复和独立审稿**双双称「全程只读」**，审稿还给了 0.92 通过。

机制早就在场 —— `observe_after_tool` 每次都产 `workspace_changed` 事件。
缺的是没有任何地方把观测和那句承诺放在一起比。

口径（2026-09-15 wangd 定）：**只算用户带进来的文件和源码**，
框架自己的 `.research/` 记账不算。
"""
from __future__ import annotations

from core.side_effect_contract import (
    declare_read_only,
    promised_read_only,
    reconcile,
)


class _State:
    node_type = "experiment"
    run_id = "r-1"

    def __init__(self, transcript=None):
        self.transcript = list(transcript or [])


def _changed(*paths):
    return {"event": "workspace_changed", "paths": list(paths)}


def test_the_24_file_contradiction_is_caught():
    """现场那一次：说了只读，改了 24 个用户文件。"""
    files = [f"analysis/step_{i}.py" for i in range(24)]
    st = _State([_changed(*files)])
    declare_read_only(st, quote="不要修改文件")

    r = reconcile(st)

    assert r.contradiction is True, "说了只读却改了 24 个文件，没被抓到"
    assert len(r.user_paths) == 24
    assert "24" in r.sentence() and "只读" in r.sentence()
    assert "analysis/step_0.py" in r.sentence(), "没指名到底改了哪些，人还得自己去翻"


def test_framework_bookkeeping_does_not_count():
    """口径：.research/ 下的记账不算「修改了用户的文件」。"""
    st = _State([_changed(".research/runtime/log.jsonl",
                          ".research/compression_log.md")])
    declare_read_only(st, quote="只读就好")

    r = reconcile(st)

    assert r.contradiction is False, "框架自己的记账被当成了改用户文件"
    assert r.user_paths == []
    assert len(r.internal_paths) == 2
    assert "确实没有改动用户的文件" in r.sentence()


def test_a_mixed_batch_reports_only_the_user_half():
    """混合改动：报用户那一半，记账那一半照数但不算矛盾来源。"""
    st = _State([_changed(".research/runtime/log.jsonl", "src/model.py")])
    declare_read_only(st)

    r = reconcile(st)

    assert r.contradiction is True
    assert r.user_paths == ["src/model.py"]
    assert r.internal_paths == [".research/runtime/log.jsonl"]


def test_no_promise_means_no_contradiction():
    """反作弊：没人要求只读时，改文件是正常工作，不许报矛盾。"""
    st = _State([_changed("src/model.py")])
    r = reconcile(st)
    assert r.promised_read_only is False
    assert r.contradiction is False
    assert r.sentence() == "", "没有承诺就不该有这句话，否则每次回复都多一行噪声"


def test_a_kept_promise_says_so():
    """反作弊：守住了也要说出来，否则上面几条可以靠「一律报矛盾」通过。"""
    st = _State([])
    declare_read_only(st, quote="别动文件")
    r = reconcile(st)
    assert r.contradiction is False
    assert "确实没有改动" in r.sentence()


def test_paths_are_deduplicated_across_events():
    """同一个文件被改两次是一个文件，不是两个 —— 数字要经得起看。"""
    st = _State([_changed("src/a.py"), _changed("src/a.py", "src/b.py")])
    declare_read_only(st)
    r = reconcile(st)
    assert r.user_paths == ["src/a.py", "src/b.py"]
    assert r.as_dict()["user_files_changed"] == 2


def test_the_promise_keeps_the_users_own_words():
    """报矛盾时要能引用用户原话，不能只说「你说过只读」。"""
    st = _State([])
    declare_read_only(st, quote="这次只看不改，不要动任何文件")
    assert promised_read_only(st).quote == "这次只看不改，不要动任何文件"
    assert reconcile(st).as_dict()["promise"]["quote"].startswith("这次只看不改")


def test_events_can_be_passed_in_directly():
    """调用方手里已经有事件时不必再走 state —— 平台侧读的是 DB 里的事件。"""
    st = _State([])
    declare_read_only(st)
    r = reconcile(st, [_changed("data/raw.csv")])
    assert r.contradiction is True and r.user_paths == ["data/raw.csv"]


# ── 接线：机制在场 ≠ 接到了路径 ──────────────────────────────────────────────


def _hook():
    import core.loop_hooks_builtin  # noqa: F401  （注册副作用）
    from core.loop_hooks import get_loop_hook

    return get_loop_hook("read_only_promise")


class _Ctx:
    def __init__(self, state):
        self.state = state


class _RecordingState(_State):
    def __init__(self, transcript=None):
        super().__init__(transcript)
        self.emitted = []

    def append_transcript(self, event, **payload):
        self.emitted.append({"event": event, **payload})


def test_the_hook_is_registered_on_before_finish():
    """写进 core/ 不等于接上了 —— 没注册到 on_before_finish 就永远不会跑。"""
    h = _hook()
    assert h is not None, "hook 没注册 —— 这一层不会在任何一轮被调用"
    assert h.on_before_finish is not None, (
        "挂错了钩子点：on_turn_start 够不到最后那一轮，正是它要管的那一轮")


def test_the_fact_reaches_the_model_before_it_writes():
    h = _hook()
    st = _RecordingState([_changed("src/model.py", "data/raw.csv")])
    declare_read_only(st, quote="不要修改文件")

    out = h.on_before_finish(_Ctx(st))

    assert out, "矛盾存在却什么都没递回去 —— 模型照样会说「未修改文件」"
    body = out[0].content
    assert "src/model.py" in body, "没指名改了哪些文件"
    assert "不要说" in body and "未修改文件" in body, (
        f"没有明确挡住那句话本身：{body}")
    assert any(e["event"] == "read_only_promise_contradicted" for e in st.emitted), (
        "没有留下可审计的事件 —— 事后无法证明框架确实提醒过")


def test_the_hook_stays_quiet_when_there_is_nothing_to_say():
    """反作弊：没承诺、或守住了，都不许插话 —— 否则每一轮都多一段噪声。"""
    h = _hook()
    assert h.on_before_finish(_Ctx(_RecordingState([_changed("src/a.py")]))) is None

    kept = _RecordingState([_changed(".research/log.jsonl")])
    declare_read_only(kept)
    assert h.on_before_finish(_Ctx(kept)) is None

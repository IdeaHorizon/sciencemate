"""用户说了"只读"，收尾时要拿真实事件按三类对一遍（#973）。

现场（2026-09-12）：用户明说「不要修改文件」，这次会话实际新增了 **24 个** Project
文件，而最终回复和独立审稿**双双称「未修改文件」**，审稿还给了 0.92 通过。

那 24 个全是 experiment / reviewer 自己目录里的证据。口径（wangd 2026-10-21 裁决）：
三类 —— 用户源文件 / 节点证据（按 workspace_prefix）/ `.research` 记账。只有用户源
文件构成矛盾；另外两类照数，但不许被说成「未修改文件」。
"""
from __future__ import annotations

from core.project_workspace import (
    BOOKKEEPING,
    NODE_EVIDENCE,
    USER_SOURCE,
    classify_project_path,
)
from core.side_effect_contract import (
    changed_paths,
    declare_read_only,
    end_read_only,
    promised_read_only,
    reconcile,
)


class _State:
    node_type = "experiment"
    run_id = "r-1"

    def __init__(self):
        self.hook_state: dict = {}


def _changed(*paths):
    return {"event": "workspace_changed", "paths": list(paths)}


# 2026-09-12 那 24 个文件的形状（目录按 2026-09-12 之后的名字：experiment→experiments）。
_INCIDENT_FILES = (
    ["experiments/runtime/environment_audit_report.md"]
    + [f"experiments/runtime/logs/{i:04d}.log" for i in range(1, 13)]
    + [f"experiments/artifacts/{name}.json" for name in (
        "raw_results", "clean_results", "experiment_log", "run_manifest", "repair_ledger")]
    + ["experiments/repro/run_manifest.json"]
    + [f"reviews/experiment_critique_{i}.json" for i in range(3)]
    + ["notes/session_orchestration.yaml", ".research/compression_log.md"]
)


# ── 三类判据（一个函数，所有调用方共用）──────────────────────────────────────


def test_each_node_prefix_is_node_evidence_not_a_user_file():
    """lujy 09-18 实测：旧口径把节点自己的产物算成「改动了 2 个用户文件」。"""
    for path in ("experiments/runtime/environment_audit_report.md",
                 "experiments/repro/run_manifest.json",
                 "reviews/x.json", "notes/a.md", "plan/pre_registration__Q1.md",
                 "figures/fig1.png", "paper/main.tex", "literature/survey.md",
                 "data/clean.csv", "MEMORY.md", "runs/extensions/foo/r1/out.txt"):
        assert classify_project_path(path) == NODE_EVIDENCE, path


def test_research_is_bookkeeping_and_everything_else_is_user_source():
    assert classify_project_path(".research/compression_log.md") == BOOKKEEPING
    for path in ("src/model.py", "README.md", "LITERATURE_REVIEW.md",
                 "experimentsX/a.py", "notes.md", "MEMORY.md.bak"):
        assert classify_project_path(path) == USER_SOURCE, path


# ── 对账 ────────────────────────────────────────────────────────────────────


def test_the_incident_is_node_evidence_not_a_contradiction():
    """现场那 24 个文件：没有一个是用户源文件 —— 不许误报矛盾，但必须照数。"""
    st = _State()
    declare_read_only(st, quote="不要修改文件")

    r = reconcile(st, [_changed(*_INCIDENT_FILES)])

    assert r.contradiction is False, "节点证据被当成了改用户文件（lujy 09-18 那个误报）"
    assert r.user_source == []
    assert len(r.node_evidence) == 23 and r.bookkeeping == [".research/compression_log.md"]
    assert r.evidence_by_prefix() == {"experiments": 19, "reviews": 3, "notes": 1}
    text = r.statement()
    assert "节点证据 23 个" in text and "experiments/ 19" in text
    assert "未修改" not in text and "没有新增或修改任何" not in text, (
        f"改了 24 个文件却出现了零修改的说法：{text}")


def test_a_user_source_write_under_read_only_is_a_contradiction():
    st = _State()
    declare_read_only(st, quote="不要修改文件")

    r = reconcile(st, [_changed("experiments/run.log", "src/model.py", ".research/x")])

    assert r.contradiction is True
    assert r.user_source == ["src/model.py"]
    assert r.node_evidence == ["experiments/run.log"]
    assert r.bookkeeping == [".research/x"]
    assert "src/model.py" in r.statement() and "⚠️" in r.statement()


def test_no_promise_means_no_contradiction_and_no_statement():
    """反作弊：没人要求只读时，改文件是正常工作，不许报矛盾，也不许多一行噪声。"""
    r = reconcile(_State(), [_changed("src/model.py")])
    assert r.promised_read_only is False
    assert r.contradiction is False
    assert r.statement() == ""


def test_a_kept_promise_with_nothing_written_says_zero_and_means_it():
    """守住了也要说出来 —— 零就说零，否则上面几条可以靠「一律报改动」通过。"""
    st = _State()
    declare_read_only(st)
    r = reconcile(st, [])
    assert r.contradiction is False and r.total == 0
    assert "没有新增或修改任何 Project 文件" in r.statement()


def test_every_event_that_carries_a_written_path_is_counted():
    """改动从四种事件来：本节点作用域 / 作用域外 / 交付投影 / 子 run 汇总。漏一种就少一类。"""
    events = [
        _changed("notes/a.md"),
        {"event": "workspace_out_of_scope_writes", "paths": ["REPORT.md"]},
        {"event": "deliverable_projected", "path": "LITERATURE_REVIEW.md"},
        {"event": "child_workspace_changed", "paths": ["experiments/x.log", "notes/a.md"]},
        {"event": "tool_call_end", "paths": ["ignored.txt"]},
    ]
    assert changed_paths(events) == [
        "notes/a.md", "REPORT.md", "LITERATURE_REVIEW.md", "experiments/x.log"]


def test_the_promise_keeps_the_users_words_and_is_not_overwritten():
    """承诺不能中途挪起点：第二次声明（同一请求里又一次只读派发）不覆盖第一次。"""
    st = _State()
    declare_read_only(st, quote="这次只看不改，不要动任何文件")
    declare_read_only(st, quote="另一句")
    assert promised_read_only(st).quote == "这次只看不改，不要动任何文件"
    assert reconcile(st, []).as_dict()["promise"]["quote"].startswith("这次只看不改")


def test_ending_the_promise_ends_it():
    st = _State()
    declare_read_only(st)
    assert end_read_only(st, reason="new_user_message") is True
    assert promised_read_only(st) is None
    assert end_read_only(st, reason="again") is False, "没有承诺时结束是空操作"


# ── 接线：机制在场 ≠ 接到了路径 ──────────────────────────────────────────────


def _hook():
    import core.loop_hooks_builtin  # noqa: F401  （注册副作用）
    from core.loop_hooks import get_loop_hook

    return get_loop_hook("read_only_promise")


class _Ctx:
    def __init__(self, state):
        self.state = state


class _RecordingState(_State):
    """事件经 append_transcript 进来，再按真实 transcript 的读法读回去。"""

    def __init__(self, tmp_path):
        super().__init__()
        self.root = tmp_path
        self.transcript_path = tmp_path / "transcript.jsonl"
        self.emitted: list[dict] = []

    def append_transcript(self, event, **payload):
        import json

        self.emitted.append({"event": event, **payload})
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, **payload}, ensure_ascii=False) + "\n")


def test_the_hook_is_registered_on_before_finish_and_enabled_on_the_orchestrator():
    """写进 core/ 不等于接上了 —— 09-18 lujy：没有任何节点启用它。"""
    from core.agent_loop import resolve_enabled_hook_names
    from core.loader import load_harness

    h = _hook()
    assert h is not None and h.on_before_finish is not None
    assert "read_only_promise" in resolve_enabled_hook_names(load_harness("_orchestrator"))


def test_the_fact_reaches_the_model_before_it_writes(tmp_path):
    st = _RecordingState(tmp_path)
    st.append_transcript("workspace_changed", paths=["src/model.py"])   # 声明之前：不算
    declare_read_only(st, quote="不要修改文件")
    st.append_transcript("workspace_out_of_scope_writes", paths=["data_raw.csv"])

    out = _hook().on_before_finish(_Ctx(st))

    assert out, "矛盾存在却什么都没递回去 —— 模型照样会说「未修改文件」"
    body = out[0].content
    assert "data_raw.csv" in body
    assert "src/model.py" not in body, "承诺之前的改动被算进来了 —— 起点丢了"
    assert "不要说" in body and "未修改文件" in body
    assert any(e["event"] == "read_only_promise_contradicted" for e in st.emitted)


def test_the_hook_stays_quiet_for_node_evidence(tmp_path):
    """收尾闸每个会话只放一次 —— 节点证据不许把它烧掉（closing_manifest 还要用）。"""
    st = _RecordingState(tmp_path)
    declare_read_only(st)
    st.append_transcript("child_workspace_changed", paths=list(_INCIDENT_FILES))
    assert _hook().on_before_finish(_Ctx(st)) is None

    nobody_asked = _RecordingState(tmp_path / "other")
    (tmp_path / "other").mkdir()
    nobody_asked.append_transcript("workspace_changed", paths=["src/a.py"])
    assert _hook().on_before_finish(_Ctx(nobody_asked)) is None

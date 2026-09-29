"""core.run_history —— run 历史唯一权威推导的回归。

这个模块是 2026-07-28 E2E-3 那批"机制接缝"缺陷的**根因修复**：当时 9 处代码
各自实现"扫兄弟 run 推导状态"，口径互不相同，于是两两打架。下面的测试分两类：

  1. 权威层自身的口径（时间序 / project 过滤 / 在飞 / 语义谓词）；
  2. **跨消费者一致性** —— 这类断言在统一之前根本写不出来，因为当时没有"同一个
     答案"这个概念。它们才是防回归的关键。
"""
from __future__ import annotations

import json
import os

import pytest

from core import run_history
from core.artifact_provenance import produced
from core.bootstrap import bootstrap
from core.ledger import RecordStore
from core.state import State

bootstrap()


def _mk(base, name, node_type, project_id, *, status="incomplete",
        failed=(), missing=(), artifacts=(), qc=None, category=None,
        in_flight=False, paused=False, mtime=None):
    d = base / name
    # 产物进这个 run 的 run 本地账本（原生文件 + records.jsonl），不是 JSON 信封
    store = RecordStore(d / "artifacts", d / "records.jsonl")
    for a in artifacts:
        store.save(
            artifact_id=a["id"], artifact_type=a["type"],
            name=a["id"].split("__", 1)[-1], content="x", metadata={},
            directory=d / "artifacts", created_at="2026-09-12T00:00:00+00:00",
            provenance=produced(node_type, name),
            produced_by_node_type=node_type, produced_by_run_id=name,
            by_node=node_type, by_run=name,
        )
    d.mkdir(parents=True, exist_ok=True)
    if paused:
        (d / "pause_pending.json").write_text("{}", encoding="utf-8")
    if in_flight:
        (d / "transcript.jsonl").write_text(
            json.dumps({"event": "run_start", "node_type": node_type,
                        "project_id": project_id}) + "\n", encoding="utf-8")
        return d
    (d / "summary.json").write_text(json.dumps({
        "node_type": node_type, "project_id": project_id, "status": status,
        "missing_required_outputs": list(missing) or list(failed),
        "artifacts": list(artifacts),
        "failure_category": category,
    }), encoding="utf-8")
    if mtime is not None:
        os.utime(d / "summary.json", (mtime, mtime))
    return d


# ── 1. 权威层口径 ───────────────────────────────────────────────────────────

def test_time_order_uses_mtime_not_directory_name(tmp_path):
    """取舍 1：run_id 的时间戳前缀是约定不是契约。

    PR#201 第一版按目录名比，把 writing-failed / writing-completed 判反了
    （'f' > 'c'）。名字与时间顺序相反时，必须听 mtime 的。
    """
    _mk(tmp_path, "zzz-early", "writing", "p", status="incomplete",
        missing=["manuscript"], mtime=1000)
    _mk(tmp_path, "aaa-late", "writing", "p", status="completed", mtime=2000)
    runs = run_history.load_runs(tmp_path, project_id="p")
    assert [r.run_id for r in runs] == ["aaa-late", "zzz-early"]
    assert run_history.latest(runs).is_completed


def test_project_filter_is_strict_none_matches_none(tmp_path):
    """取舍 2：recall 旧口径在 project_id=None 时**根本不过滤** → 跨项目泄漏。"""
    _mk(tmp_path, "1-a", "writing", "proj_a", missing=["m"])
    _mk(tmp_path, "1-b", "writing", "proj_b", missing=["m"])
    _mk(tmp_path, "1-n", "writing", None, missing=["m"])

    assert {r.run_id for r in run_history.load_runs(tmp_path, project_id="proj_a")} \
        == {"1-a"}
    # anon（None）只看得到同为 None 的，而不是"所有项目"
    assert {r.run_id for r in run_history.load_runs(tmp_path, project_id=None)} \
        == {"1-n"}


def test_self_run_always_excluded(tmp_path):
    _mk(tmp_path, "me", "writing", "p")
    _mk(tmp_path, "other", "writing", "p")
    runs = run_history.load_runs(tmp_path, project_id="p", exclude_run_id="me")
    assert [r.run_id for r in runs] == ["other"]


def test_in_flight_runs_are_first_class(tmp_path):
    """取舍 4：只有进度指纹认在飞 run，判定类逻辑把真进展当卡死。"""
    _mk(tmp_path, "2-done", "experiment", "p", status="completed")
    _mk(tmp_path, "2-live", "experiment", "p", in_flight=True)

    assert [r.run_id for r in run_history.load_runs(tmp_path, project_id="p")] \
        == ["2-done"], "默认不含在飞"
    with_live = run_history.load_runs(tmp_path, project_id="p",
                                      include_in_flight=True)
    live = [r for r in with_live if r.in_flight]
    assert [r.run_id for r in live] == ["2-live"]
    assert live[0].status is None and live[0].node_type == "experiment"


def test_in_flight_without_readable_project_still_visible(tmp_path):
    """在飞 run 的 project_id 可能读不到 —— 不能因此当成"别的项目的"丢掉。"""
    d = tmp_path / "3-live"
    d.mkdir()
    (d / "transcript.jsonl").write_text("not json\n", encoding="utf-8")
    runs = run_history.load_runs(tmp_path, project_id="p", include_in_flight=True)
    assert [r.run_id for r in runs] == ["3-live"]


def test_was_evaluated_distinguishes_crash_from_clean(tmp_path):
    """catch-all 收尸（status=error）与中途停靠（blocked）都没走到终态评估。"""
    _mk(tmp_path, "4-crashed", "writing", "p", status="error")
    _mk(tmp_path, "4-parked", "writing", "p", status="blocked")
    _mk(tmp_path, "4-judged", "writing", "p", status="incomplete",
        missing=["manuscript"])
    runs = {r.run_id: r for r in run_history.load_runs(tmp_path, project_id="p")}
    assert not runs["4-crashed"].was_evaluated
    assert not runs["4-parked"].was_evaluated
    assert runs["4-judged"].was_evaluated


def test_orphaned_runs_ignores_project_and_finds_both_kinds(tmp_path):
    _mk(tmp_path, "5-done", "writing", "p", status="completed")
    _mk(tmp_path, "5-half", "writing", "p", in_flight=True)
    _mk(tmp_path, "5-paused", "writing", "other", in_flight=True, paused=True)
    _mk(tmp_path, "5-self", "writing", "p", in_flight=True)
    got = {r.run_id: r for r in run_history.orphaned_runs(tmp_path, "5-self")}
    assert set(got) == {"5-half", "5-paused"}
    assert got["5-paused"].has_pause and not got["5-half"].has_pause


# ── 2. 判定原语 ─────────────────────────────────────────────────────────────

def test_consecutive_failures_breaks_on_upstream_success(tmp_path):
    """E2E-3 死锁：计数只在本节点自己成功时清零 → 退回上游补齐后无法重试。"""
    for i, ts in enumerate((100, 200, 300)):
        _mk(tmp_path, f"6-w{i}", "writing", "p", missing=["manuscript"], mtime=ts)
    runs = run_history.load_runs(tmp_path, project_id="p")
    assert run_history.consecutive_failures(runs, "writing")["count"] == 3

    _mk(tmp_path, "6-curator", "_curator", "p", status="completed", mtime=400)
    runs = run_history.load_runs(tmp_path, project_id="p")
    assert run_history.consecutive_failures(
        runs, "writing", break_on_other_producing_success=True) is not None, \
        "系统节点成功不算上游状况改变"

    _mk(tmp_path, "6-exp", "experiment", "p", status="completed", mtime=500)
    runs = run_history.load_runs(tmp_path, project_id="p")
    assert run_history.consecutive_failures(
        runs, "writing", break_on_other_producing_success=True) is None, \
        "上游 producing 节点成功 → 状况已变，必须放行重试"


def test_consecutive_failures_ignores_infra_categories(tmp_path):
    """外因失败（协议抽风 / 框架门禁）**无条件**不进卡死统计。

    #426 起不再依赖调用方传对 ignore_failure_categories —— obligations 的
    `_collect_repeated_failure` 就没传，两个消费方曾经口径不一致。
    """
    for i, ts in enumerate((100, 200, 300, 400)):
        _mk(tmp_path, f"7-p{i}", "postprocess", "p", missing=["clean_results"],
            category="provider_tool_call_protocol_error", mtime=ts)
    runs = run_history.load_runs(tmp_path, project_id="p")
    # 不传参也剔除（唯一真相源在 EXTERNAL_FAILURE_CATEGORIES）
    assert run_history.consecutive_failures(runs, "postprocess") is None
    # 真实失败照常计数
    for i, ts in enumerate((500, 600)):
        _mk(tmp_path, f"7-q{i}", "postprocess", "p", missing=["clean_results"],
            mtime=ts)
    runs = run_history.load_runs(tmp_path, project_id="p")
    assert run_history.consecutive_failures(runs, "postprocess")["count"] == 2


def test_best_attempt_prefers_progress_then_evaluated(tmp_path):
    """挑走得最远的那次，不是最近那次；且"0 条失败"不能靠没被评估过白捡。"""
    _mk(tmp_path, "8-good", "writing", "p", missing=["one_thing"], mtime=100,
        artifacts=[{"id": "manuscript__g", "type": "manuscript"},
                   {"id": "plan__g", "type": "writing_preflight_plan"}])
    _mk(tmp_path, "8-capped", "writing", "p", status="error", mtime=200,
        artifacts=[{"id": "plan__c", "type": "writing_preflight_plan"}])
    runs = run_history.load_runs(tmp_path, project_id="p")
    best = run_history.best_attempt(runs, "writing",
                                    ["manuscript", "writing_preflight_plan"])
    assert best.run_id == "8-good"

    # 产出数相同时，被评估过的赢
    _mk(tmp_path, "8-x", "hypothesis", "p", missing=["c"], mtime=100,
        artifacts=[{"id": "h__x", "type": "hypothesis_set"}])
    _mk(tmp_path, "8-y", "hypothesis", "p", status="error", mtime=200,
        artifacts=[{"id": "h__y", "type": "hypothesis_set"}])
    runs = run_history.load_runs(tmp_path, project_id="p")
    assert run_history.best_attempt(runs, "hypothesis",
                                    ["hypothesis_set"]).run_id == "8-x"


def test_system_node_streak_stops_at_producing(tmp_path):
    _mk(tmp_path, "9-w", "writing", "p", status="completed", mtime=100)
    for i, ts in enumerate((200, 300, 400)):
        _mk(tmp_path, f"9-c{i}", "_curator", "p", status="completed", mtime=ts)
    runs = run_history.load_runs(tmp_path, project_id="p")
    assert run_history.system_node_streak(runs, {"_curator", "_reviewer"}) == 3


# ── 3. 跨消费者一致性（统一之前写不出来的断言）────────────────────────────

@pytest.mark.asyncio
async def test_baseline_note_and_tool_resolve_identically(tmp_path, monkeypatch):
    """note 广告哪一次，read_own_prior_attempt 默认就读哪一次。

    PR#198 只改了 note 的选择、工具默认没跟着改 —— 节点照着 note 里的 id 去读，
    连报两次"这个 run 里没有该 artifact"。统一之后这在结构上不可能：两边都调
    _baseline_run。
    """
    import shared.tools.run_node as rn

    class _H:
        required_output_artifact_types = ["manuscript"]
    monkeypatch.setattr("core.loader.load_harness", lambda nt, *a, **k: _H())

    _mk(tmp_path, "a-good", "writing", "p_x", missing=["c"], mtime=100,
        artifacts=[{"id": "manuscript__good", "type": "manuscript"}])
    _mk(tmp_path, "b-capped", "writing", "p_x", status="error", mtime=200,
        artifacts=[{"id": "plan__meh", "type": "writing_preflight_plan"}])

    st_o = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_x")
    note = rn._revision_baseline_note(st_o, "writing")
    assert note["run_id"] == "a-good"

    st_w = State.new(node_type="writing", base_dir=tmp_path, project_id="p_x")
    got = await rn._read_own_prior_attempt(st_w, artifact_id="manuscript__good")
    assert got["status"] == "success"
    assert got["from_run_id"] == note["run_id"]


def test_all_consumers_agree_on_latest_run(tmp_path):
    """同一份磁盘状态，各消费者对"最近一次 writing"必须给同一个答案。

    E2E-3 现场：终态门禁信 transcript（看到失败那次），修订基线信磁盘（看到成功
    那次），两边各说各话，项目永远关不掉。
    """
    import chat
    import shared.tools.run_node as rn

    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_y")
    base = st.root.parent
    _mk(base, "w-failed", "writing", "p_y", missing=["manuscript"], mtime=100)
    # 父只记下了失败那次（成功那次跑完时父进程已经没了）
    st.append_transcript("subagent_call_end", child_node_type="writing",
                         child_run_id="w-failed", child_status="incomplete")
    _mk(base, "w-ok", "writing", "p_y", status="completed", mtime=200,
        artifacts=[{"id": "manuscript__ok", "type": "manuscript"}])

    runs = run_history.load_runs(base, project_id="p_y", exclude_run_id=st.run_id)
    assert run_history.latest(runs, node_type="writing").run_id == "w-ok"
    # 终态门禁：不再被隐形的成功 run 卡住
    assert chat._continuous_unresolved_terminal_producers(st) == []
    # 派发拦截：最近一次成功 → 不算卡死
    assert rn._repeated_failure_for(st, "writing") is None


def test_recall_no_longer_leaks_across_projects_for_anon_state(tmp_path):
    """旧 recall 口径：project_id=None 时不过滤 → 读到所有项目的失败记录。"""
    from core.recall import recall

    _mk(tmp_path, "leak-a", "writing", "someone_elses_project",
        failed=["their_check"], mtime=100)
    st = State.new(node_type="writing", base_dir=tmp_path, project_id=None)
    res = recall(st, "写 manuscript")
    assert res.prior_failed_checks == [], "anon state 不得读到别的项目的失败记录"


# ── 4. 平台无关性（macOS 全过 / Linux 容器全挂那次的钉子）──────────────────

@pytest.fixture
def coarse_mtime(monkeypatch):
    """模拟文件系统时间戳粒度粗到"同一秒内创建的文件 mtime 相同"。

    Linux 容器的实际行为。第一版把 mtime 当排序主键，本地 macOS 5/5 全过、
    CI 2/2 全挂 —— 正确性挂在了文件系统上。
    """
    orig = run_history._mtime
    monkeypatch.setattr(run_history, "_mtime", lambda p: float(int(orig(p))))


def test_ordering_survives_coarse_filesystem_timestamps(tmp_path, coarse_mtime):
    """mtime 全打平时，run_id 的时间戳前缀必须还能定出正确顺序。

    刻意用**数值序与字典序相反**的一对（位数不同）：`999999999` 数值上早于
    `1000000000`，字典序却大。字符串比较会判反，解析成数字才对 —— 这就是
    "前缀是时间不是字符串"的意思。
    """
    _mk(tmp_path, "999999999-early", "writing", "p", missing=["manuscript"])
    _mk(tmp_path, "1000000000-late", "writing", "p", status="completed")
    runs = run_history.load_runs(tmp_path, project_id="p")
    assert [r.run_id for r in runs] == ["1000000000-late", "999999999-early"]
    assert run_history.latest(runs, node_type="writing").is_completed


def test_terminal_gate_reconciliation_is_not_a_time_comparison(tmp_path,
                                                               coarse_mtime):
    """对账靠"transcript 没见过"，不靠"谁的时间戳大" —— 后者要求跨来源可比。

    第一版就是拿 mtime 跨来源比：粒度一粗两边相等，磁盘上那次成功的 run 就又
    被忽略了，孤儿 bug 原样复发。
    """
    import chat

    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_g")
    base = st.root.parent
    _mk(base, "1785000001-w", "writing", "p_g", missing=["manuscript"])
    st.append_transcript("subagent_call_end", child_node_type="writing",
                         child_run_id="1785000001-w", child_status="incomplete")
    assert [r["run_id"] for r in chat._continuous_unresolved_terminal_producers(st)] \
        == ["1785000001-w"]

    # 成功那次只在磁盘上（父进程没来得及记）——即便 mtime 完全相等也必须认
    _mk(base, "1785000002-w", "writing", "p_g", status="completed")
    assert chat._continuous_unresolved_terminal_producers(st) == []


def test_transcript_recorded_run_is_not_superseded_by_older_disk_run(tmp_path,
                                                                     coarse_mtime):
    """反向：transcript 见过的 run 不该被它同样见过的另一次覆盖。

    否则名字字典序大的那次会赢（'writing-failed' > 'writing-completed'），
    这正是 PR#201 第一版按名字比时踩的坑。
    """
    import chat

    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_h")
    base = st.root.parent
    _mk(base, "writing-failed", "writing", "p_h", missing=["manuscript"])
    st.append_transcript("subagent_call_end", child_node_type="writing",
                         child_run_id="writing-failed", child_status="incomplete")
    _mk(base, "writing-completed", "writing", "p_h", status="completed")
    st.append_transcript("subagent_call_end", child_node_type="writing",
                         child_run_id="writing-completed", child_status="completed")
    assert chat._continuous_unresolved_terminal_producers(st) == []

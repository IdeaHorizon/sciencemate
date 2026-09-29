"""崩溃的旧尝试不许顶掉成功的新尝试（E2E-6 2026-08-04 实测：项目关不掉）。

## 现场

writing 跑了两次：
  1785801560  status=error（ReadTimeout 崩溃）—— 父来不及写 subagent_call_end
  1785806808  status=completed（transcript 有完整记录）

手稿早已冻结入 deliverables、reviewer/curator/decision 全 PROCEED，项目实质完成。
但闭环判据说 writing 未闭环，orchestrator 只能报 blocked —— 它拒绝改
summary.json 造假（做得完全对），于是一个做完的项目永远关不掉。

## 根因

`_reconcile_with_disk` 规则①（孤儿 run 修复，为 E2E-3 那个"成功却对父隐形"的
writing run 加的）是**无条件取代**：

    unseen = [磁盘上有、transcript 没见过的 run]
    latest[node_type] = unseen[0]

它的前提是"没被 transcript 见过 ⇒ 父停止记录之后才落盘 ⇒ 它更新"。
**这个前提对崩溃的子 run 不成立**：子 run 抛异常死掉时父同样来不及写终态事件，
于是崩溃那次也"没被见过"，反过来顶掉了后面成功的那次。

## 修法

孤儿只有**确实更新**才配取代。时间源用 run_id 的时间戳前缀 —— 与
`RunRecord.order_key` 同一个来源，精确且与文件系统无关（这不违背"不做跨来源
时间比较"：那条防的是拿 mtime 比，run_id 前缀是创建时刻本身）。
"""
from __future__ import annotations

import json

from core import closure
from core.bootstrap import bootstrap

bootstrap()


def _mk_run(base, run_id, node_type, status, **extra):
    d = base / run_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_id, "node_type": node_type, "status": status,
               "project_id": "p",
               "missing_required_outputs": [], "quality_check_results": []}
    payload.update(extra)
    (d / "summary.json").write_text(json.dumps(payload), encoding="utf-8")
    return d


class _State:
    """最小 state：closure 只用 root / project_id / run_id / transcript_path。"""

    def __init__(self, root, events):
        self.root = root
        self.project_id = "p"
        self.run_id = root.name
        root.mkdir(parents=True, exist_ok=True)
        self.transcript_path = root / "transcript.jsonl"
        self.transcript_path.write_text(
            "\n".join(json.dumps(e) for e in events) + ("\n" if events else ""),
            encoding="utf-8")
        self.hook_state = {}


def _scan(state, node="writing"):
    latest, started, seen = closure._scan_child_run_events(
        state, frozenset({node}))
    closure._reconcile_with_disk(
        state, latest, seen_run_ids=seen, probe_node_types=frozenset({node}))
    return latest


# ── 现场回放 ────────────────────────────────────────────────────────────────

def test_replays_e2e6_crashed_attempt_must_not_override_later_success(tmp_path):
    """E2E-6 原形：崩溃的第一次（更早）不许顶掉成功的第二次。"""
    base = tmp_path / "runs"
    orch = base / "orchestrator__p"
    _mk_run(base, "1785801560-f2546d", "writing", "error")        # 崩溃，无事件
    _mk_run(base, "1785806808-8359ee", "writing", "completed")    # 成功，有事件

    st = _State(orch, [
        {"event": "subagent_call_start", "child_node_type": "writing",
         "child_run_id": "1785806808-8359ee", "at": "2026-08-04T03:00:00"},
        {"event": "subagent_call_end", "child_node_type": "writing",
         "child_run_id": "1785806808-8359ee", "child_status": "completed",
         "at": "2026-08-04T03:40:00"},
    ])
    latest = _scan(st)
    assert latest["writing"].run_id == "1785806808-8359ee", \
        "更早的崩溃 run 顶掉了更晚的成功 run —— 项目将永远关不掉"
    assert latest["writing"].status == "completed"
    assert latest["writing"].resolved is True


def test_project_can_close_with_a_crashed_sibling(tmp_path):
    """闭环判据层面：有一具崩溃尸体不该让项目挂着。"""
    base = tmp_path / "runs2"
    orch = base / "orchestrator__p"
    _mk_run(base, "1785801560-dead", "writing", "error")
    _mk_run(base, "1785806808-good", "writing", "completed")
    st = _State(orch, [
        {"event": "subagent_call_end", "child_node_type": "writing",
         "child_run_id": "1785806808-good", "child_status": "completed",
         "at": "2026-08-04T03:40:00"},
    ])
    latest = _scan(st)
    unresolved = [a for a in latest.values() if not a.resolved]
    assert unresolved == [], f"仍有未闭环项：{unresolved}"


# ── 别把 E2E-3 那个孤儿修复削弱了 ───────────────────────────────────────────

def test_newer_invisible_success_still_replaces_older_failure(tmp_path):
    """E2E-3 原形必须继续有效：成功却对父隐形的**更新**孤儿要能救回项目。

    现场：writing run 跑满 46 轮、12 项 QC 全绿、PDF 编好，但父进程在它写下
    subagent_call_end 之前就没了 → 父记录的仍是前一次失败的。
    """
    base = tmp_path / "runs3"
    orch = base / "orchestrator__p"
    _mk_run(base, "1785222000-0acefd", "writing", "incomplete",
            missing_required_outputs=["manuscript"])
    _mk_run(base, "1785222389-e08990", "writing", "completed")   # 更新、隐形

    st = _State(orch, [
        {"event": "subagent_call_end", "child_node_type": "writing",
         "child_run_id": "1785222000-0acefd", "child_status": "incomplete",
         "at": "2026-07-28T01:00:00"},
    ])
    latest = _scan(st)
    assert latest["writing"].run_id == "1785222389-e08990", \
        "更新的隐形成功 run 必须仍能取代 —— 否则 E2E-3 那个修复就废了"
    assert latest["writing"].resolved is True


def test_orphan_used_when_transcript_has_nothing(tmp_path):
    """transcript 完全没记过这个节点 → 磁盘上那次照用（没有可比对象）。"""
    base = tmp_path / "runs4"
    orch = base / "orchestrator__p"
    _mk_run(base, "1785300000-only", "writing", "completed")
    st = _State(orch, [])
    latest = _scan(st)
    assert latest["writing"].run_id == "1785300000-only"


# ── 时间序必须借用唯一权威，不许自造 ────────────────────────────────────────

def test_ordering_borrows_run_history_authority():
    """比较用 `RunRecord.order_key`，不自己实现第二套时间序。

    第一版我自己拿 run_id 前缀比、解析不出就放弃 —— 于是 run_id 没有时间戳前缀的
    场景（既有测试用 `w-ok` / `w-failed` 这种名字）退化成"永不取代"，把 E2E-3 那个
    孤儿修复又弄坏了。自造第二套时间序，就是在制造这个模块要消灭的那种口径漂移。
    """
    import inspect
    src = inspect.getsource(closure._is_newer_run)
    assert "order_key" in src, "时间序必须走 RunRecord.order_key"
    assert "split(" not in src, "不许自己解析 run_id 前缀"


def test_baseline_missing_on_disk_lets_disk_win():
    """transcript 提到但磁盘上没有的 run → 无从比较，磁盘那份胜出。

    磁盘是唯一事实来源；一条只存在于事件里的 run 不该挡住真实落盘的那次。
    """
    class _Rec:
        order_key = (1.0, 1.0, "x")
    assert closure._is_newer_run(_Rec(), None) is True


def test_order_key_decides_both_directions():
    class _Rec:
        def __init__(self, k): self.order_key = k
    newer, older = _Rec((200.0, 0, "b")), _Rec((100.0, 0, "a"))
    assert closure._is_newer_run(newer, older) is True
    assert closure._is_newer_run(older, newer) is False

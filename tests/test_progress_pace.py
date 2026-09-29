"""progress 必须给时间尺度 —— 不给，调度器只能猜"卡住了"。

E2E-4 实测：writing 节点被连续 cancel **10 次**，每次理由都是
"llm_request stuck for extended period"。而 `runtime_control(action='progress')`
只返回事件名 + HH:MM:SS，**没有任何时间尺度** —— "extended period" 是它猜的。
真实情况：机器 load 130，writing 在编译 LaTeX，等两三分钟完全正常。

根因不是"调度器没耐心"，是没给它判断快慢所需的信息。所以修法是**补信息**，
不是加机械拦截（wangd: "别光加规矩，看是不是现有的内容导致的"）。
"""
from __future__ import annotations

import json
import time

from core.bootstrap import bootstrap

bootstrap()


def _events(*specs):
    """specs: (相对现在的秒数, 事件名)。"""
    import datetime as dt
    now = time.time()
    return [{"event": name,
             "at": dt.datetime.fromtimestamp(now - ago).astimezone().isoformat()}
            for ago, name in specs]


def test_pace_reports_wait_and_typical_gap():
    from shared.tools.library.runtime_control import _pace_summary

    pace = _pace_summary(_events(
        (300, "tool_call"), (240, "tool_result"),
        (180, "llm_request"), (120, "llm_response"),
        (60, "tool_call"), (45, "llm_request")))
    assert pace["last_event"] == "llm_request"
    assert 40 <= pace["seconds_since_last_event"] <= 50
    assert pace["typical_gap_seconds"] > 0
    assert pace["max_gap_seconds"] >= pace["typical_gap_seconds"]


def test_empty_or_unparsable_events_do_not_crash():
    from shared.tools.library.runtime_control import _pace_summary

    assert _pace_summary([]) == {}
    assert _pace_summary([{"event": "x", "at": "不是时间"}]) == {}


def test_waiting_on_model_is_called_out_explicitly(tmp_path, monkeypatch):
    """最后一条是 llm_request → 必须**明说**这是在等模型、不是卡死。

    只测 _pace_summary 的话，把 _peek_child_progress 里那段接线摘掉测试照样全绿。
    所以这里走真正的工具入口。
    """
    import asyncio

    import shared.tools.library.runtime_control as rc
    from core.state import State

    st = State.new(node_type="writing", base_dir=tmp_path, project_id="p_pace")
    with st.transcript_path.open("w", encoding="utf-8") as fh:
        for e in _events((200, "tool_call"), (150, "tool_result"),
                         (100, "llm_response"), (30, "llm_request")):
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    monkeypatch.setattr(rc, "find_child_state", lambda rid: st)

    res = asyncio.run(rc._peek_child_progress(state=st, child_run_id=st.run_id))
    assert res["status"] == "success"
    assert res["waiting_on_model"] is True, "这个状态必须机械标出，不能让它自己推断"
    assert res["pace"]["seconds_since_last_event"] > 0
    note = res["note"]
    assert "不是卡死" in note, "必须直说，否则它还是会猜"
    assert "typical_gap" in json.dumps(res, ensure_ascii=False) or "中位数" in note


def test_not_waiting_gives_plain_elapsed(tmp_path, monkeypatch):
    """最后一条不是 llm_request → 不该硬说"在等模型"。"""
    import asyncio

    import shared.tools.library.runtime_control as rc
    from core.state import State

    st = State.new(node_type="writing", base_dir=tmp_path, project_id="p_pace2")
    with st.transcript_path.open("w", encoding="utf-8") as fh:
        for e in _events((90, "llm_request"), (20, "tool_result")):
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    monkeypatch.setattr(rc, "find_child_state", lambda rid: st)

    res = asyncio.run(rc._peek_child_progress(state=st, child_run_id=st.run_id))
    assert res["waiting_on_model"] is False
    assert "不是卡死" not in res["note"]


def test_orchestrator_harness_documents_cancel_boundary():
    """cancel 的边界必须写在它每轮都读得到的地方。"""
    from pathlib import Path

    import yaml

    raw = Path("nodes/_orchestrator/harness.yaml").read_text(encoding="utf-8")
    yaml.safe_load(raw)          # 保证没写坏
    assert "waiting_on_model" in raw
    assert "不是\"我觉得它慢\"的开关" in raw
    assert "typical_gap_seconds" in raw

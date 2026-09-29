"""未了结的义务 —— 一个概念收敛掉散落的"还欠什么"。

背景（wangd）："调度器的 harness 如果一点一点增加补丁那就成屎山了，得根上思考。"
`_continuous_followup` 当时已经长出 5 段拼接的 recovery 文本 + 3 处终态检查，
每段一个临时扫描函数。它们说的是同一件事：**某个节点欠着某样东西**。

关键设计：**申诉的状态从 run 历史推，不养状态机**。被点名的节点在申诉之后成功
跑过一次 = 了结。这样 cancel 路径丢字段、谁忘了标记都不会让账变脏
（E2E-4 实测：cancel 路径确实丢过申诉，我为此单独打过一个补丁）。
"""
from __future__ import annotations

import json

from core import obligations as obl
from core.bootstrap import bootstrap
from core.state import State

bootstrap()


def _run(base, name, node_type, project_id, *, status="completed",
         appeals=None, failed=(), missing=()):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": node_type, "project_id": project_id, "status": status,

        "missing_required_outputs": list(missing) or list(failed),
        "upstream_rework_requests": list(appeals or []),
        "artifacts": [],
    }), encoding="utf-8")
    return d


APPEAL = {
    "requested_by_node": "writing",
    "upstream_node": "experiment",
    "missing": "experiment_log 缺 execution_parameters（random_state / CV 设置）",
    "acceptance": "metadata 里能追溯到 Methods 要写的每个参数",
    "blocking": True,
}


def _state(tmp_path, pid="p_obl"):
    return State.new(node_type="_orchestrator", base_dir=tmp_path, project_id=pid)


def test_open_appeal_becomes_an_obligation(tmp_path):
    st = _state(tmp_path)
    _run(st.root.parent, "1900000001-w", "writing", st.project_id,
         status="incomplete", appeals=[APPEAL])
    got = obl.collect(st)
    assert len(got) == 1
    o = got[0]
    assert o.kind == obl.KIND_APPEAL
    assert o.owed_by == "experiment" and o.claimed_by == "writing"
    assert "execution_parameters" in o.what
    assert o.blocking is True
    assert "run_node" in o.discharge_hint


def test_appeal_discharged_by_later_success_no_flag_needed(tmp_path):
    """被点名节点在申诉**之后**成功跑过 → 自动了结。不需要谁去标记。"""
    st = _state(tmp_path)
    base = st.root.parent
    _run(base, "1900000001-w", "writing", st.project_id,
         status="incomplete", appeals=[APPEAL])
    assert len(obl.collect(st)) == 1
    _run(base, "1900000002-e", "experiment", st.project_id, status="completed")
    assert obl.collect(st) == []


def test_earlier_success_does_not_discharge(tmp_path):
    """申诉**之前**的成功不算数 —— 那次显然没补这个缺口。"""
    st = _state(tmp_path)
    base = st.root.parent
    _run(base, "1900000001-e", "experiment", st.project_id, status="completed")
    _run(base, "1900000002-w", "writing", st.project_id,
         status="incomplete", appeals=[APPEAL])
    assert len(obl.collect(st)) == 1


def test_failed_later_run_does_not_discharge(tmp_path):
    """被点名节点跑了但没成功 → 账还在。"""
    st = _state(tmp_path)
    base = st.root.parent
    _run(base, "1900000001-w", "writing", st.project_id,
         status="incomplete", appeals=[APPEAL])
    # 新语义（QC 层删除）：incomplete 必须带契约缺口才算失败 —— 零信号的
    # incomplete 会被现算平反。没成功 = 缺了该交的东西。
    _run(base, "1900000002-e", "experiment", st.project_id,
         status="incomplete", missing=["clean_results"])
    assert len(obl.collect(st)) == 1


def test_repeated_failure_surfaces_with_three_exits(tmp_path):
    st = _state(tmp_path)
    for i in range(3):
        _run(st.root.parent, f"1900001{i:03d}-w", "writing", st.project_id,
             status="incomplete", failed=["manuscript_no_unverified_details"])
    got = [o for o in obl.collect(st) if o.kind == obl.KIND_REPEATED_FAILURE]
    assert len(got) == 1
    hint = got[0].discharge_hint
    assert "退回上游" in hint and "降级产物" in hint and "blocked" in hint


def test_duplicate_appeals_collapse(tmp_path):
    """同一个节点欠同一样东西，提十次也只算一条账（否则又是没有增量的重复）。"""
    st = _state(tmp_path)
    for i in range(4):
        _run(st.root.parent, f"1900002{i:03d}-w", "writing", st.project_id,
             status="incomplete", appeals=[APPEAL])
    assert len([o for o in obl.collect(st) if o.kind == obl.KIND_APPEAL]) == 1


def test_render_is_one_section_and_empty_when_clean(tmp_path):
    assert obl.render([]) == ""
    st = _state(tmp_path)
    _run(st.root.parent, "1900003001-w", "writing", st.project_id,
         status="incomplete", appeals=[APPEAL])
    text = obl.render(obl.collect(st))
    assert text.count("未了结的义务") == 1, "只能有一段，不是每种来源拼一段"
    assert "experiment" in text and "execution_parameters" in text


# ── 让步（判决拆除批 0）：blocking 义务的申报式出口 ─────────────────────────
#
# 终态门禁的牙齿在消费端（chat 读 blocking()），这里测的是唯一的收口：
# collect() 出口处的让步比对。走**真 appeal 路径**，不打替身。

def test_a_concession_lifts_blocking_but_the_debt_stays_visible(tmp_path):
    """让步解除拦截力，但账**不消失**：照渲染（🤝）、extra 带完整让步记录。"""
    from core import concessions as cc

    st = _state(tmp_path)
    _run(st.root.parent, "1900004001-w", "writing", st.project_id,
         status="incomplete", appeals=[APPEAL])
    before = obl.collect(st)
    assert obl.blocking(before), "前置：该账必须先是 blocking"
    o = obl.blocking(before)[0]
    kh = cc.obligation_key_hash(o.kind, o.owed_by, o.what)
    # 渲染必须把让步码送到调用方（契约必须送到调用方）
    assert kh in obl.render(before)

    rec = cc.record(st, key_hash=kh, kind=o.kind, owed_by=o.owed_by,
                    what=o.what, reason="上游平台能力缺席，带账继续出降级稿")
    assert rec is not None

    after = obl.collect(st)
    assert not obl.blocking(after), "让步后不再拦截 complete"
    conceded = [x for x in after if x.extra.get("conceded")]
    assert len(conceded) == 1, "账必须还在，不许消失"
    text = obl.render(after)
    assert "🤝" in text and "已让步" in text and "待终审" in text


def test_wrong_key_hash_gets_the_legal_values(tmp_path):
    """让步码不对时报错必须列出全部合法取值（不逼模型猜）。"""
    import asyncio

    import shared.tools  # noqa: F401
    from core.tool_registry import execute

    st = _state(tmp_path)
    _run(st.root.parent, "1900004002-w", "writing", st.project_id,
         status="incomplete", appeals=[APPEAL])
    res = asyncio.run(execute("concede_obligation", st,
                              key_hash="deadbeef", reason="理由"))
    assert res["status"] == "error"
    real = obl.blocking(obl.collect(st))[0]
    from core import concessions as cc
    assert cc.obligation_key_hash(real.kind, real.owed_by, real.what) in res["error"]


def test_concede_tool_end_to_end_and_reason_is_semantic_not_length(tmp_path):
    """走真工具入口：空 reason 拒（语义必需），一个字的 reason 放行（无字数闸）。"""
    import asyncio

    import shared.tools  # noqa: F401
    from core import concessions as cc
    from core.tool_registry import execute

    st = _state(tmp_path)
    _run(st.root.parent, "1900004003-w", "writing", st.project_id,
         status="incomplete", appeals=[APPEAL])
    o = obl.blocking(obl.collect(st))[0]
    kh = cc.obligation_key_hash(o.kind, o.owed_by, o.what)

    empty = asyncio.run(execute("concede_obligation", st, key_hash=kh, reason="  "))
    assert empty["status"] == "error"

    ok = asyncio.run(execute("concede_obligation", st, key_hash=kh, reason="略"))
    assert ok["status"] == "success"
    assert not obl.blocking(obl.collect(st))
    # 让步是项目级事实：新 run（同一 worktree/根）照样看得见
    st2 = _state(tmp_path, pid=st.project_id)
    st2.root = st.root
    assert not obl.blocking(obl.collect(st2))


def test_project_scope_is_respected(tmp_path):
    """别的项目的申诉不进本项目的账。"""
    st = _state(tmp_path)
    _run(st.root.parent, "1900005001-x", "writing", "other_project",
         status="incomplete", appeals=[APPEAL])
    assert obl.collect(st) == []


def test_complete_is_rejected_while_blocking_obligation_open(tmp_path):
    """端到端：挂着 blocking 义务时 CONTINUOUS_STATUS: complete 必须被驳回。

    E2E-4 实测：data 节点申诉后没人跟进，orchestrator 改道绕开，诉求悬到最后
    没有下文 —— 提了没人管等于没提。只测 collect/terminal_block 的话，把
    chat.py 里那段接线摘掉测试照样全绿。

    申诉方刻意用 `data`（也正是 E2E-4 的现场），**不是** `writing`：终态门禁里的
    producer 判据只覆盖 writing（见 core/closure.py 的 CONTINUOUS_TERMINAL），
    拿一个 incomplete 的 writing run 当夹具会先撞上那道门 —— 于是本测试想验的
    "义务门禁真的接上了" 反而被遮住，测什么都过。
    """
    import chat as chat_mod

    st = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    st.hook_state.update(continuous_loop=True, continuous_phase="running")
    _run(st.root.parent, "1900006001-d", "data", st.project_id,
         status="incomplete", appeals=[APPEAL])
    prompt, _ = chat_mod._continuous_followup(
        st, "都做完了\nCONTINUOUS_STATUS: complete", reason="turn_finished")
    assert prompt is not None, "还欠着账就不能收尾"
    assert st.hook_state["continuous_phase"] != "complete"
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "open_blocking_obligations" in tr

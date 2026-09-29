"""v3.7 L1+L2：用户原始输入的不可变锚 + 逐字注入。

回归依据（E2E-3 实测构念漂移，两处都发生在纯散文转述链上）：
  - 用户 `V0 无法验证 ~ V3 可确定性验证` → prereg `V0 Deterministic ~ V3 Human`
    （刻度整个反转）
  - 用户 `低难度**且**可验证`（两条件）→ 只测可验证性（一个维度消失）
根因：节点只见到 orchestrator 转述的 research_question，从未见过原文。
"""
from __future__ import annotations

from pathlib import Path

from core.bootstrap import bootstrap
from core.context_engine import build_messages
from core.loader import load_harness
from core.research_intake import load_intake, record_intake, render_intake_section
from core.state import State

bootstrap()

_ORIG = "可验证性等级：V0 无法验证 ~ V3 可确定性验证；H1：40-70% 步骤属于低难度可验证操作"


def _state(tmp_path: Path, pid="p_intake") -> State:
    return State.new(node_type="hypothesis", base_dir=tmp_path, project_id=pid)


def test_intake_recorded_verbatim(tmp_path):
    st = _state(tmp_path)
    record_intake(st.project_root, _ORIG)
    rec = load_intake(st.project_root)
    assert rec["original_text"] == _ORIG, "必须逐字保存，不得规范化/截断"


def test_intake_never_overwritten(tmp_path):
    """L2：谁都不能静默改写目标 —— 后续输入只能追加 amendment。"""
    st = _state(tmp_path, "p_amend")
    record_intake(st.project_root, _ORIG)
    record_intake(st.project_root, "改成只做 airline 域")
    rec = load_intake(st.project_root)
    assert rec["original_text"] == _ORIG, "原始目标必须永远保留"
    assert len(rec["amendments"]) == 1
    assert rec["amendments"][0]["text"] == "改成只做 airline 域"


def test_repeat_same_text_not_recorded_twice(tmp_path):
    """进程重启/重放同一条输入不该被当成新指令。"""
    st = _state(tmp_path, "p_dup")
    record_intake(st.project_root, _ORIG)
    record_intake(st.project_root, _ORIG)
    assert load_intake(st.project_root)["amendments"] == []


def test_intake_injected_verbatim_into_every_node(tmp_path):
    """L1：原文进入**每个**节点的 system prompt，而不只是 orchestrator。"""
    st = _state(tmp_path, "p_inject")
    record_intake(st.project_root, _ORIG)
    for node in ("literature", "hypothesis", "experiment", "writing", "_reviewer"):
        s = State.new(node_type=node, base_dir=tmp_path, project_id="p_inject")
        msgs = build_messages(load_harness(node), s,
                              {"research_question": "orchestrator 的转述版本"})
        sys_text = msgs[0].content or ""
        assert "V0 无法验证" in sys_text, f"{node} 看不到原文"
        assert "以本段为准" in sys_text, f"{node} 缺少权威性声明"


def test_paraphrase_marked_subordinate_to_original(tmp_path):
    """转述必须被明确标为次级 —— 漂移正是从"只见转述"开始的。"""
    st = _state(tmp_path, "p_auth")
    record_intake(st.project_root, _ORIG)
    md = render_intake_section(st.project_root)
    assert "唯一权威表述" in md
    assert "仅供参考" in md
    # 纪律条款要点名两类实测漂移
    assert "刻度" in md and "静默改写" in md


def test_no_intake_no_section(tmp_path):
    st = _state(tmp_path, "p_none")
    assert render_intake_section(st.project_root) is None


def test_reviewer_must_diff_against_original():
    """光有原文不够 —— reviewer 必须被要求拿它做对照，否则可见性不咬合。"""
    h = load_harness("_reviewer")
    prompt = h.system_prompt
    assert "用户原始研究输入" in prompt
    assert "静默改写" in prompt


def test_decision_note_is_rendered_as_user_words_with_its_source(tmp_path):
    """#761：HITL 决策附言是用户的话，进权威原文段；来源单独标注，不冒充开题原文。

    现场：课题负责人在 REVISE 附言里给了「中位数 AMI 提升 ≥0.02」，节点如实引用，
    reviewer 对账时只看得到开题 brief → 判 phantom。核验面必须看得到这条通道。
    """
    st = _state(tmp_path, "p_note")
    record_intake(st.project_root, _ORIG)
    record_intake(st.project_root, "中位数 AMI 提升 ≥0.02", source="decision_note")
    md = render_intake_section(st.project_root)
    assert "中位数 AMI 提升 ≥0.02" in md
    assert "用户决策附言 #1" in md
    assert "用户后续指令 #1" not in md      # 来源分得清

"""pause payload 在传输中不许掉字段 —— 判据不是"这几个字段还在"，是"任何字段都还在"。

写死一张字段清单去断言，正是被反复警告过的名单式护栏：下一个新字段照样默默消失，
而测试全绿。所以这里用**未知字段**做判据 —— 它代表"以后会加的那些"。

两次实测代价：
  v0.4.3      漏拷 metadata        → auto-approve 永远选 options[0]=PROCEED，无视 reviewer 推荐
  2026-08-19  漏拷 option_details  → 选项的身份在第一跳就死，UI 只能拿文案当 id
"""
from __future__ import annotations

from core.pause import PauseEvent


def _tool_payload() -> dict:
    """decision_package 那种带结构化选项的 pause payload。"""
    return {
        "question": "Post-node decision for hypothesis",
        "context": "……决策包正文……",
        "options": ["RETRY CURATOR INTEGRATION", "REVISE"],
        "option_details": [
            {"id": "retry_curator", "label": "RETRY CURATOR INTEGRATION",
             "description": "重跑整合", "recommended": True},
            {"id": "revise", "label": "REVISE", "description": "重跑 producer",
             "recommended": False},
        ],
        "offer_id": "r_prod:p1234abcd:oabcdef12",
        "decision_id": "r_prod:p1234abcd",
        "asking_node_type": "_orchestrator",
        "asking_run_id": "run_x",
        "metadata": {"type": "decision_package", "recommended_option_index": 0},
        # 代表"将来会加的字段"。名单式测试永远看不见它。
        "a_field_nobody_has_written_yet": {"deep": ["value"]},
    }


def test_every_field_survives_construction_and_roundtrip():
    ev = PauseEvent.from_payload(_tool_payload(), pending_tool_call_id="call_1")
    out = ev.to_dict()
    for key, value in _tool_payload().items():
        assert key in out, f"字段 {key!r} 在构造/序列化中被丢掉了"
        assert out[key] == value, f"字段 {key!r} 的值变了"
    assert out["pending_tool_call_id"] == "call_1"


def test_structured_choices_keep_their_identity():
    """选项的 id 必须活着 —— 这是 UI 能回传 choice_id 而不是文案的前提。"""
    ev = PauseEvent.from_payload(_tool_payload())
    assert ev.choice_ids() == ("retry_curator", "revise")
    assert ev.offer_id == "r_prod:p1234abcd:oabcdef12"
    assert ev.option_details()[0]["recommended"] is True


def test_named_fields_still_normalize():
    """命名字段是规范化视图：缺失时回退，不是原样透传。"""
    ev = PauseEvent.from_payload(
        {"question": "q"}, default_node_type="hypothesis", default_run_id="run_y"
    )
    assert ev.asking_node_type == "hypothesis"
    assert ev.asking_run_id == "run_y"
    assert ev.options == []
    assert ev.option_details() == []
    assert ev.choice_ids() == ()


def test_free_text_pause_has_no_choices():
    """没有结构化选项的 pause（自由文本提问）不该凭空长出 id。"""
    ev = PauseEvent.from_payload({"question": "还要继续吗？", "options": ["是", "否"]})
    assert ev.options == ["是", "否"]
    assert ev.choice_ids() == ()      # 纯文案数组没有身份，别假装有
    assert ev.offer_id == ""

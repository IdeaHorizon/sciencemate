"""孤儿 DSML parameter：invoke 开标签被吞时的恢复（issue #267，lujy 实测）。

现场（zju deepseek-v4-pro，**有的 project 会有的不会**）：屏幕上满是
`<｜DSML｜parameter name=…>` 残渣，工具完全调不起来。原文形态：

    <｜DSML｜parameter name="node_type" string="true">hypothesis
    <｜DSML｜parameter name="node_inputs" string="false">{...}
                                   ← 这里本该有个 invoke 开标签，被吞了
    <｜DSML｜invoke name="run_node">
    <｜DSML｜parameter name="node_type" string="true">_curator

前两个 parameter 没有前置 invoke。旧实现只沿 invoke 开标签迭代，这段：
  - 没恢复成调用（那半个意图丢了）
  - 也没被剥掉 → 原样当叙述文本显示（用户看到的残渣）
  - 且 `protocol_leak=False` → 框架当正常回复**不重试**
模型于是以为自己发了两个调用，实际只跑了后半个。"有的会有的不会" =
取决于开标签有没有被吞。

处理原则：**不猜工具名硬恢复**（猜错就是拿错参数跑错节点），而是判定为未解析
markup → protocol_leak → 重试，让后端重发完整的；同时把残段剥干净不上屏。
"""
from __future__ import annotations

from core.tool_call_recovery import recover_tool_calls

_ORPHAN = '''<｜DSML｜parameter name="node_type" string="true">hypothesis
<｜DSML｜parameter name="node_inputs" string="false">{"research_question": "盐浓度"}

<｜DSML｜invoke name="run_node">
<｜DSML｜parameter name="node_type" string="true">_curator
<｜DSML｜parameter name="node_inputs" string="false">{"mode": "dreaming"}
<｜DSML｜parameter name="background" string="false">true
'''


def test_orphan_params_trigger_retry_not_silent_pass():
    """核心：孤儿参数段必须判协议失败 → 上层重试。

    旧行为 protocol_leak=False：框架当正常回复，模型永远不知道自己没调成。
    """
    r = recover_tool_calls(_ORPHAN, [])
    assert r.protocol_leak is True
    # 不允许"只跑后半个"——宁可整轮重来，也不要拿半个意图去执行
    assert r.tool_calls == []


def test_orphan_markup_never_reaches_the_screen():
    """残渣不上屏（lujy 看到的就是这个）。"""
    r = recover_tool_calls(_ORPHAN, [])
    body = r.content or ""
    assert "DSML" not in body
    assert "parameter name" not in body
    assert body.strip() == ""


def test_normal_invoke_not_broken():
    """不误伤：规范（含不闭合）的 invoke 照常恢复。"""
    ok = ('<｜DSML｜invoke name="run_node">\n'
          '<｜DSML｜parameter name="node_type" string="true">hypothesis\n'
          '<｜DSML｜parameter name="node_inputs" string="false">{"q": 1}\n')
    r = recover_tool_calls(ok, [])
    assert r.protocol_leak is False
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0]["function"]["name"] == "run_node"
    assert '"node_type": "hypothesis"' in r.tool_calls[0]["function"]["arguments"]


def test_prose_mentioning_parameter_untouched():
    """不误伤：正文里提到 "parameter" 这个词不算 markup。"""
    text = "我建议把 parameter name 改成别的，这样更清楚。"
    r = recover_tool_calls(text, [])
    assert r.protocol_leak is False
    assert r.content == text


def test_prose_before_valid_invoke_is_kept():
    """正文 + 正常调用：叙述保留，调用恢复。"""
    c = ('好的，我来起 hypothesis 节点做这个研究。\n'
         '<｜DSML｜invoke name="run_node">\n'
         '<｜DSML｜parameter name="node_type" string="true">hypothesis\n')
    r = recover_tool_calls(c, [])
    assert len(r.tool_calls) == 1
    assert "好的，我来起 hypothesis 节点" in (r.content or "")

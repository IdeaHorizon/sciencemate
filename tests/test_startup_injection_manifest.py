"""启动注入清单（#737，抄 CC SubAgent 的「拿到/拿不到什么是文档化清单」）。

跨节点交接失败史（experiment 读不到 prereg、#621）的共同根因：子侧启动
上下文没有契约，缺了不报错。守护判据两个方向都锁：
  - 清单说有的，prompt 里必须真有（防"标记了没 append"）
  - prompt 里有的关键段，清单必须列出（防"append 了没标记"——那清单就是
    半改造固化的谎言）
"""
from __future__ import annotations

import json

import pytest

from core.bootstrap import bootstrap
from core.context_engine import build_messages
from core.harness import NodeHarness
from core.state import State


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield


def _events(state, name):
    lines = [json.loads(ln) for ln in
             state.transcript_path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return [e for e in lines if e.get("event") == name]


#: 通道名 → 该通道在 prompt 里的机械指纹（在场判据，两个方向共用一份）
_SYSTEM_FINGERPRINTS = {
    "system_prompt": None,                    # 内容自由，无固定指纹
    "rules": "## 硬约束",
    "guidelines": "## 建议",
    "blocker_rights": "## 🆘 当前节点解决不了的问题",
    "expected_outputs": "## 你必须产出这些 artifact",
    "kb_heuristic": "## 💡 KB 使用启发式",
}
_USER_FINGERPRINTS = {
    "node_inputs": "## 节点输入",
    "upstream_artifacts": "## 可用的上游 artifact",
    "required_outputs_reminder": "## 提醒：必须产出的 artifact 类型",
}


def _both_directions(manifest_channels, prompt_text, fingerprints):
    for channel, fp in fingerprints.items():
        if fp is None:
            continue
        in_manifest = channel in manifest_channels
        in_prompt = fp in prompt_text
        assert in_manifest == in_prompt, (
            f"{channel}: 清单说{'有' if in_manifest else '无'}、"
            f"prompt 里{'有' if in_prompt else '无'} —— 两边分叉了")


def _harness(**kw):
    kw.setdefault("node_type", "literature")
    kw.setdefault("system_prompt", "你是文献节点")
    kw.setdefault("tools", ["list_artifacts"])
    return NodeHarness(**kw)


def test_manifest_matches_prompt_both_directions(tmp_path):
    h = _harness(rules=["规则一"], guidelines=["建议一"],
                 expected_outputs={"survey_report": "综述"},
                 required_outputs=["survey_report"])
    state = State.new("literature", tmp_path / "r1", project_id="p")
    msgs = build_messages(h, state, {"topic": "MLIP", "prereg_id": "pr_1"})

    manifest = state.hook_state["_startup_injection_manifest"]
    _both_directions(manifest["system"], msgs[0].content, _SYSTEM_FINGERPRINTS)
    _both_directions(manifest["user"], msgs[1].content, _USER_FINGERPRINTS)

    # 节点输入的**键**在契约里 —— "prereg 送没送到"从此是清单问题
    assert manifest["node_input_keys"] == ["prereg_id", "topic"]
    assert "node_inputs" in manifest["user"]


def test_manifest_lands_in_transcript(tmp_path):
    h = _harness()
    state = State.new("literature", tmp_path / "r2", project_id="p")
    build_messages(h, state, {"q": "x"})
    ev = _events(state, "startup_injection_manifest")
    assert len(ev) == 1
    assert ev[0]["node_input_keys"] == ["q"]
    assert "system_prompt" in ev[0]["system"]
    assert "blocker_rights" in ev[0]["system"]     # 非 _ 节点的基本权利


def test_the_manifest_carries_which_instructions_this_turn_read(tmp_path):
    """「这一轮读到的指令是哪一份」跟着这一轮走（RFC X3）。

    从前这个答案在会话行上冻着一列 JSON：它记的是建会话那一刻的事，与本轮无关，
    而真正到达模型的项目层早就被 worktree 里的文件覆盖掉了 —— 一个回答不了自己
    那个问题的记录。证据要由产生它的那一轮给出。
    """
    import core.directives_loader as dl

    h = _harness()
    state = State.new("literature", tmp_path / "r-dig", project_id="p")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "PROJECT.md").write_text("# 项目层\n\n只用中文\n", encoding="utf-8")
    state.project_worktree = worktree

    build_messages(h, state, {})
    digests = state.hook_state["_startup_injection_manifest"]["instruction_digests"]
    expected = dl._digest((worktree / "PROJECT.md").read_text(encoding="utf-8"))
    assert digests["project"] == expected
    # 没有个人层文件 = 没有指纹，而不是一串假的。
    assert digests["personal"] is None


def test_absent_channels_are_absent_not_guessed(tmp_path):
    """裸 state 没绑项目：宪法/局面/directives 不该出现在清单里 ——
    清单是事实记录，不是模板。"""
    h = _harness()
    state = State.new("literature", tmp_path / "r3", project_id="p")
    build_messages(h, state, {})
    m = state.hook_state["_startup_injection_manifest"]
    # （research_situation 不在此列：State.new 带 project_id 时局面段现算注入，
    #  这是设计内行为 —— 本用例第一版把它列进去，被真实行为纠正。）
    # org_directives 随组织层一起删了（RFC X3）：断言一个**不可能存在**的通道
    # 不在清单里，等于什么都没断言。换成真的会缺席的那个 —— 裸 state 没有
    # PROFILE.md，个人层这一段就该不在。
    for ch in ("memory_constitution", "profile_directives", "user_intake_verbatim"):
        assert ch not in m["system"], ch
    assert "node_inputs" not in m["user"]


def test_orchestrator_gets_no_producing_only_channels(tmp_path):
    """系统节点（_ 前缀）没有 blocker 权利段 / KB 启发式 —— 按节点类型裁剪
    是契约的一部分（CC 按角色砍 CLAUDE.md 的同款判断）。"""
    h = NodeHarness(node_type="_orchestrator", system_prompt="调度",
                    tools=["list_artifacts"])
    state = State.new("_orchestrator", tmp_path / "r4", project_id="p")
    msgs = build_messages(h, state, {})
    m = state.hook_state["_startup_injection_manifest"]
    assert "blocker_rights" not in m["system"]
    assert "kb_heuristic" not in m["system"]
    _both_directions(m["system"], msgs[0].content, _SYSTEM_FINGERPRINTS)

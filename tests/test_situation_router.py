"""局面路由：调度器该由框架告诉它"现在处于哪种局面"，而不是自己翻大 prompt。

## 现场（2026-08-17，英国饮食文化开题）

新项目第一条消息，orchestrator 按训练先验规划了 literature → hypothesis →
writing 老管线并直接起了 literature。v2.1 的"开题入口是 hypothesis(Analysis)"
写在 prompt 第 200 行上下 —— 没竞争过第 89 行的老示例和 run_node 描述里的
过期节点名单。同一局面下模型每次都要重新从十几个段落里推断"我现在该干什么"。

## 机制

局面（fresh / flow_debt / in_research）是**机械可判**的：盘上有没有
research_state、hook_state 里有没有收尾欠账、有没有后台子节点在跑。
situation_router 在每条用户消息前判一次，**覆盖式**注入一条局面消息：

  - 覆盖不追加：旧局面消息先移除，防注入在长 session 里堆积；
  - 局面没变且旧消息还在 → 整体 no-op，保 byte-identity（驻定重放 / cache）；
  - 只在 turn 1 注入 —— 工具轮之间不动 messages，不打扰 prompt 前缀缓存。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import bootstrap

bootstrap()

from core.harness import NodeHarness                            # noqa: E402
from core.ledger import write_record                            # noqa: E402
from core.llm import LLMMessage                                 # noqa: E402
from core.loop_hooks import HookContext                         # noqa: E402
from core.loop_hooks_builtin import (                           # noqa: E402
    _SITUATION_MARKER,
    _situation_router_on_turn_start,
)
from core.state import State                                    # noqa: E402


def make_state(td: Path, node_type: str = "_orchestrator") -> State:
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(td)
    return State.new(node_type=node_type, base_dir=td)


def make_ctx(state: State, messages: list | None = None, turn: int = 1) -> HookContext:
    return HookContext(
        harness=NodeHarness(node_type=state.node_type),
        state=state, messages=messages if messages is not None else [], turn=turn,
    )


def test_only_the_orchestrator_gets_routed() -> None:
    """局面路由是主入口的机制 —— producing 节点有自己的定向层，别重复注入。"""
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td), node_type="experiment")
        assert _situation_router_on_turn_start(make_ctx(state)) is None


def test_tool_rounds_do_not_reshuffle_messages() -> None:
    """turn>1 不注入 —— 工具轮之间动 messages 会打掉本轮的 prompt 前缀缓存。"""
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        assert _situation_router_on_turn_start(make_ctx(state, turn=2)) is None


def test_a_fresh_project_routes_to_hypothesis_not_literature() -> None:
    """没有 research_state 的项目：开题入口是 hypothesis(Analysis)。

    这是本次实测事故的直接回归：模型先铺了一轮 landscape 综述。
    局面消息必须把"别先替 Analysis 跑 literature"送到眼前。
    """
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        result = _situation_router_on_turn_start(make_ctx(state))
        assert result is not None and len(result) == 1
        body = result[0].content
        assert body.startswith(_SITUATION_MARKER)
        assert "fresh" in body
        assert "hypothesis" in body
        assert "landscape" in body          # 明说别先铺综述
        assert "import_artifact" in body    # 外部材料的合法出口也在场


def test_flow_debt_outranks_everything() -> None:
    """收尾三步欠着时，局面 = flow_debt —— 指向 🚨 提醒，不再自由发挥。"""
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        state.hook_state["pending_post_node_flow"] = [{
            "producing_node": "experiment",
            "producing_run_id": "run_abc",
            "review_state": "pending",
            "curator_state": "pending",
            "decision_state": "pending",
            "artifact_ids": ["experiment_log__x"],
        }]
        result = _situation_router_on_turn_start(make_ctx(state))
        assert result is not None
        body = result[0].content
        assert "flow_debt" in body
        assert "run_abc" in body
        assert "POST-PRODUCING FLOW" in body


def test_a_project_with_research_state_routes_by_verdict() -> None:
    """research_state 在盘上 → 局面 = in_research，给绝对路径 + verdict。"""
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        worktree = Path(td) / "wt"
        worktree.mkdir()
        # research_state 是 Analysis 的原生文件 + 工作区账本一行
        record = write_record(worktree, artifact_type="research_state",
                              name="research_state", content="# research state",
                              directory="plan", metadata={"verdict": "continue"},
                              produced_by_node_type="hypothesis",
                              produced_by_run_id="r-hyp")
        state.project_worktree = worktree

        result = _situation_router_on_turn_start(make_ctx(state))
        assert result is not None
        body = result[0].content
        assert "in_research" in body
        assert "verdict=continue" in body
        # 地图必须给能直接粘进工具的坐标（E2E v22 教训）：绝对路径，指向 head 文件
        assert record["id"] == "research_state__research_state"
        assert str((worktree / "plan" / "research_state__research_state.md").resolve()) in body


def test_reinjection_replaces_instead_of_appending() -> None:
    """局面变了 → 旧局面消息被移除再注入新的；长 session 里不堆积。"""
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        messages: list[LLMMessage] = []
        first = _situation_router_on_turn_start(make_ctx(state, messages))
        assert first is not None
        messages.extend(first)          # run_loop 会 append 注入结果
        messages.append(LLMMessage(role="user", content="开始研究"))

        # 局面变化：欠下收尾三步
        state.hook_state["pending_post_node_flow"] = [{
            "producing_node": "hypothesis", "producing_run_id": "r9",
            "review_state": "pending", "curator_state": "pending",
            "decision_state": "pending", "artifact_ids": [],
        }]
        second = _situation_router_on_turn_start(make_ctx(state, messages))
        assert second is not None
        markers = [m for m in messages
                   if m.role == "system"
                   and (m.content or "").startswith(_SITUATION_MARKER)]
        assert markers == []            # 旧的已被移除（新的由 run_loop append）
        assert "flow_debt" in second[0].content


def test_an_unchanged_situation_is_a_byte_identical_noop() -> None:
    """局面没变且旧消息还在 → 返回 None、不动 messages —— 驻定重放靠这条。"""
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        messages: list[LLMMessage] = []
        first = _situation_router_on_turn_start(make_ctx(state, messages))
        assert first is not None
        messages.extend(first)
        before = [(m.role, m.content) for m in messages]

        assert _situation_router_on_turn_start(make_ctx(state, messages)) is None
        assert [(m.role, m.content) for m in messages] == before


def test_orchestrator_harness_enables_the_router() -> None:
    from core.loader import load_harness

    assert "situation_router" in load_harness("_orchestrator").loop_hooks

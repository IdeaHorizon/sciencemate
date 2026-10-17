"""坏掉的 node_inputs 不能冒充"没给"（#1165）。

实测（FVCOM，2026-09-28）：父节点第一次派发带了完整 experiment_spec，因
background 被拒；重试时整包重写参数，node_inputs 变成一个末尾少 `}` 的 JSON
字符串。run_node 解析失败就 `node_inputs = None`，输入契约检查以
`if node_inputs …` 为前提整段跳过，派发处 None 再变 `{}` —— 子节点拿着空输入
起来，编了一个没人指定的版本，finish 因"没有原始任务文本"被拒五次。

判据：解析不了、或解析出来不是对象 → 明确报错、**零子 run、零派发记录**；
合法对象与合法 JSON 对象字符串照常派发，子节点收到的是同一个对象。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import bootstrap
from core.state import State

bootstrap()

from shared.tools import run_node as rn   # noqa: E402


def _parent(tmp_path: Path) -> State:
    s = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_inputs")
    s.hook_state["_callable_nodes"] = ["*"]
    return s


def _transcript_kinds(state: State) -> list[str]:
    path = state.root / "transcript.jsonl"
    if not path.exists():
        return []
    kinds = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            kinds.append(rec.get("type") or rec.get("kind") or rec.get("event"))
    return kinds


@pytest.fixture
def calls(tmp_path, monkeypatch):
    seen: list[dict] = []

    async def fake_execute(**kwargs):
        seen.append(kwargs)
        sd = tmp_path / f"child_{len(seen)}"
        (sd / "artifacts").mkdir(parents=True, exist_ok=True)
        return {
            "run_id": f"run_child_{len(seen)}", "status": "completed",
            "node_type": kwargs.get("node_type") or "_curator",
            "turns": 1, "tokens_used": 1, "artifacts": [],
            "missing_required_outputs": [], "state_dir": str(sd),
            "final_text_preview": "done",
        }

    monkeypatch.setattr("core.executor.execute_node", fake_execute)
    return seen


# FVCOM 现场那一份的形状：只剩 execution_params，末尾少一个 `}`。
_TRUNCATED = '{"execution_params": {"ref": "release-v4.4.12", "jobs": 8}'


@pytest.mark.asyncio
@pytest.mark.parametrize("raw, code", [
    (_TRUNCATED, "node_inputs_invalid_json"),
    ("{mode: dreaming}", "node_inputs_invalid_json"),
    ("[1, 2]", "node_inputs_not_object"),
    ("3", "node_inputs_not_object"),
    ('"dreaming"', "node_inputs_not_object"),
    ("null", "node_inputs_not_object"),
    (["mode", "dreaming"], "node_inputs_not_object"),
    (7, "node_inputs_not_object"),
])
async def test_malformed_inputs_start_nothing(tmp_path, calls, raw, code):
    parent = _parent(tmp_path)
    out = await rn._run_node_tool(parent, node_type="_curator",
                                  user_note="测试派发", node_inputs=raw)

    assert out["status"] == "error"
    assert out["error_code"] == code
    assert "没有启动子节点" in out["error"]
    # 零子 run：execute_node 一次都没被调
    assert calls == []
    # 零派发记录：没有任何"已经开始派"的痕迹，只有这次拒绝本身
    kinds = _transcript_kinds(parent)
    assert "subagent_call_start" not in kinds
    assert "run_node_inputs_rejected" in kinds


@pytest.mark.asyncio
async def test_truncated_json_names_the_position(tmp_path, calls):
    """报错要带上解析器说的位置 —— 调用方据此知道是截断，不是键名不对。"""
    parent = _parent(tmp_path)
    out = await rn._run_node_tool(parent, node_type="_curator",
                                  user_note="测试派发", node_inputs=_TRUNCATED)
    assert f"char {len(_TRUNCATED)}" in out["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    {"mode": "dreaming"},
    json.dumps({"mode": "dreaming"}),
])
async def test_object_and_object_string_both_reach_the_child(tmp_path, calls, raw):
    parent = _parent(tmp_path)
    out = await rn._run_node_tool(parent, node_type="_curator",
                                  user_note="测试派发", node_inputs=raw)
    assert out["status"] == "success", out
    assert len(calls) == 1
    assert calls[0]["node_inputs"].get("mode") == "dreaming"


@pytest.mark.asyncio
async def test_omitted_inputs_are_still_omitted_not_rejected(tmp_path, calls):
    """缺省不是坏输入：省略允不允许归正式契约判，不归这道形状检查。"""
    parent = _parent(tmp_path)
    out = await rn._run_node_tool(parent, node_type="_curator", user_note="测试派发")
    assert out["status"] == "success", out
    assert len(calls) == 1

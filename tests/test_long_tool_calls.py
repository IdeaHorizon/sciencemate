"""A live tool owns its result, with visible supervision and no detached calls."""
from __future__ import annotations

import asyncio
import json

import pytest

from core import tool_registry as tr
from core.bootstrap import bootstrap
from core.state import State

bootstrap()


@pytest.fixture()
def slow_tool():
    """注册一个可控时长的假工具，用完摘掉。"""
    name = "_t_slow"

    async def _slow(*, state, seconds: float = 0.05, **_):
        await asyncio.sleep(seconds)
        return {"status": "success", "slept": seconds}

    tr._REGISTRY.tools[name] = tr.ToolDefinition(
        name=name, description="test", parameters_schema={
            "type": "object", "properties": {"seconds": {"type": "number"}}})
    tr._REGISTRY.executors[name] = _slow
    yield name
    tr._REGISTRY.tools.pop(name, None)
    tr._REGISTRY.executors.pop(name, None)


def _state(tmp_path):
    return State.new(node_type="experiment", base_dir=tmp_path / "runs",
                     project_id="p_long")


def _events(st, name):
    p = st.transcript_path
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
            if x.strip() and f'"{name}"' in x]


# ── 保留参数：agent 自己声明检查点 ─────────────────────────────────────────

def test_retired_handback_is_not_advertised():
    for tool in list(tr._REGISTRY.tools.values()):
        props = tr.to_openai_schema(tool)["function"]["parameters"].get("properties", {})
        assert tr.RESERVED_CHECK_AFTER not in props


def test_reserved_param_never_reaches_the_tool(tmp_path, slow_tool):
    """框架保留参数必须在派发前摘掉 —— 否则严格签名的工具会炸。"""
    seen = {}

    async def _strict(*, state, seconds=0.01):        # 故意不接 **kwargs
        seen["keys"] = {"state", "seconds"}
        return {"status": "success"}

    tr._REGISTRY.executors[slow_tool] = _strict
    res = asyncio.run(tr.execute(slow_tool, _state(tmp_path),
                                 seconds=0.01, check_after_seconds=99))
    assert res["status"] == "success", res
    assert seen, "工具必须被真正调用到（参数没摘干净会 TypeError）"


@pytest.mark.parametrize("raw", [None, "garbage", -5, 0, 0.001])
def test_retired_handback_never_detaches_the_call(tmp_path, slow_tool, raw):
    state = _state(tmp_path)
    result = asyncio.run(tr.execute(slow_tool, state, seconds=0.03, check_after_seconds=raw))
    assert result == {"status": "success", "slept": 0.03}
    assert not state.hook_state.get("injected_messages")
    assert not _events(state, "tool_handed_back")


def test_fast_tool_returns_normally(tmp_path, slow_tool):
    """快命令照常同步返回，不走交还路径（爆炸半径 = 0）。"""
    st = _state(tmp_path)
    res = asyncio.run(tr.execute(slow_tool, st, seconds=0.01,
                                 check_after_seconds=30))
    assert res["status"] == "success"
    assert not (st.hook_state.get("injected_messages") or [])
    assert not _events(st, "tool_handed_back")


def test_zero_means_never_hand_back(tmp_path, slow_tool):
    """agent 说"别打断我" → 一直等到底。"""
    st = _state(tmp_path)
    res = asyncio.run(tr.execute(slow_tool, st, seconds=0.3,
                                 check_after_seconds=0))
    assert res["status"] == "success"
    assert not _events(st, "tool_handed_back")


def test_late_error_returns_to_the_original_call(tmp_path, slow_tool):
    async def boom(*, state, **_):
        await asyncio.sleep(0.03)
        raise RuntimeError("late boom")

    tr._REGISTRY.executors[slow_tool] = boom
    state = _state(tmp_path)
    result = asyncio.run(tr.execute(slow_tool, state, check_after_seconds=0.001))
    assert result["status"] == "error" and "late boom" in result["error"]
    assert not state.hook_state.get("injected_messages")


# ── 监督方仍看得见（长跑留痕）─────────────────────────────────────────────

def test_long_running_marks_are_recorded(tmp_path, slow_tool, monkeypatch):
    """"这次调用已经跑了多久"是**只有框架知道**的事实，bash 查不出来。"""
    monkeypatch.setattr(tr, "_LONG_RUNNING_MARKS_S", (0.05, 0.15))
    st = _state(tmp_path)
    asyncio.run(tr.execute(slow_tool, st, seconds=0.3, check_after_seconds=0))
    marks = _events(st, "tool_long_running")
    assert marks, "长调用必须留痕（监督方靠它区分'节点在想' vs '卡在同一次调用')"
    assert marks[0]["tool_name"] == slow_tool


def test_marks_stop_once_the_tool_returns(tmp_path, slow_tool, monkeypatch):
    monkeypatch.setattr(tr, "_LONG_RUNNING_MARKS_S", (0.05, 0.1, 0.15))
    st = _state(tmp_path)
    asyncio.run(tr.execute(slow_tool, st, seconds=0.01, check_after_seconds=0))
    assert not _events(st, "tool_long_running")


# ── 零新工具：runtime_control 没长胖 ──────────────────────────────────────

def test_no_new_tools_were_added():
    """判断作业健康用 agent 自己的 shell —— 框架不为此加工具。

    （tail_file / workspace_recent_files 曾被加进来，是把"缺信息"误诊成
    "缺能力"：orchestrator 白名单里本来就有 run_bash 和 read_file。）

    2026-08-04 新增 `jobs`，**过的是同一道判据而不是绕开它**：

      - `tail_file` 当年该被拒，因为 `run_bash` 就能 tail —— 那是**能力**问题，
        而 agent 早有那个能力。
      - `jobs` 读的是**框架必须替它持有的状态**：一张跨 run 存活的作业登记表
        （谁在跑 / 占什么算力 / 预计多久 / 超期多少）。bash 拿不到这个 ——
        它连"上一个 run 起的作业还活着吗"都答不了，因为那个 run 已经没了。

    判据仍然是那句：**agent 自己能做的，框架不代劳；只有框架才能持有的状态，
    才配一个入口。** 见 core/jobs.py 的模块文档（E2E-7 实测现场）。

    `answer` 同样只交还框架持有的当前 pause 身份与用户答复，不恢复循环；
    恢复仍由原暂停驱动器负责，shell 无法代替这次交接。
    """
    from shared.tools.library import runtime_control as rc
    assert "tail_file" not in rc._RC_ACTIONS
    assert set(rc._RC_ACTIONS) == {
        "inject", "cancel", "list_active", "progress", "jobs", "answer"}


def test_every_action_param_is_declared_in_schema():
    """新增 action 的参数必须进 schema，否则模型根本传不进来（实测栽过）。"""
    from shared.tools.library import runtime_control as rc

    spec = tr._REGISTRY.tools["runtime_control"]
    assert set(spec.parameters_schema["properties"]["action"]["enum"]) == \
        set(rc._RC_ACTIONS)


def test_every_action_is_documented():
    from shared.tools.library import runtime_control as rc

    desc = tr._REGISTRY.tools["runtime_control"].description
    for action in rc._RC_ACTIONS:
        assert f"`{action}`" in desc, f"action {action} 没写进工具说明"

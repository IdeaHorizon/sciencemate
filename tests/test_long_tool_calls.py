"""长工具调用：到点把**轮次**还给 agent，命令不动。

E2E-5 实测：experiment 起 vLLM，一次 tool_call 挂了 15.5 小时。agent 全程没拿回
控制权 —— agent loop 是"想 → 调工具 → 等结果 → 再想"，`await` 不返回就没有下
一轮。它不是放弃了，是被冻住了。

设计（与 wangd 讨论定稿 2026-07-31）：

  - **"多久算异常"是领域知识，只有 agent 知道**（`pip install` 半分钟没动静就该
    看，LAMMPS 弛豫六小时才正常，HPC 排队几天也正常）。所以由它在调用时用
    `check_after_seconds` 自己声明，框架不猜统一阈值。
  - 到点框架**只把轮次还回去**，底下调用一点没动、继续跑；跑完结果走
    `injected_messages`（后台子节点那条现成通道）自动推回。
  - **零个新工具。** 判断作业健不健康靠看外部世界（tail 日志 / ps / curl 端口），
    那些 agent 自己的 shell 全能干；框架只提供 bash 拿不到的那一件 ——
    "框架自己这个还没返回的调用最终返回了什么"。
"""
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
    tr._PENDING.clear()


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

def test_check_after_is_injected_into_every_tool_schema():
    """一处注入，所有工具（含同事的、MCP 的）自动获得，谁都不用改。"""
    for tool in list(tr._REGISTRY.tools.values())[:25]:
        props = tr.to_openai_schema(tool)["function"]["parameters"].get(
            "properties", {})
        assert tr.RESERVED_CHECK_AFTER in props, f"{tool.name} 没拿到保留参数"


def test_injection_never_overwrites_a_tools_own_param():
    t = tr.ToolDefinition(name="x", description="d", parameters_schema={
        "type": "object",
        "properties": {tr.RESERVED_CHECK_AFTER: {"type": "string",
                                                 "description": "自家的"}}})
    got = tr.to_openai_schema(t)["function"]["parameters"]["properties"]
    assert got[tr.RESERVED_CHECK_AFTER]["description"] == "自家的"


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


@pytest.mark.parametrize("raw,is_default", [
    (None, True), ("垃圾", True), (-5, False), (0, False),
])
def test_bad_values_never_block_the_call(raw, is_default):
    """参数写错不该导致工具不执行 —— 当没填处理。"""
    kwargs = {} if raw is None else {tr.RESERVED_CHECK_AFTER: raw}
    got = tr._pop_check_after(kwargs)
    assert (got == tr._MAX_HANDBACK_S) is is_default
    assert tr.RESERVED_CHECK_AFTER not in kwargs


# ── 到点交还控制权（不是中止）─────────────────────────────────────────────

def test_hands_back_without_touching_the_call(tmp_path, slow_tool):
    """到检查点：agent 拿回轮次，**命令继续跑**，跑完结果自动送达。"""
    st = _state(tmp_path)

    async def go():
        res = await tr.execute(slow_tool, st, seconds=0.6,
                               check_after_seconds=0.1)
        assert res["status"] == "running", res
        assert "没有被中断" in res["note"] and "也没有失败" in res["note"]
        assert "不要因为没拿到返回值就重跑" in res["note"], "必须防它重跑"
        job = res["job_id"]
        assert job in tr._PENDING, "调用必须还活着"
        await asyncio.sleep(0.9)                      # 等它自己跑完
        return job

    job = asyncio.run(go())
    injected = st.hook_state.get("injected_messages") or []
    assert injected, "跑完的结果必须自动推给 agent（走现成的 injected_messages）"
    body = injected[-1]["content"]
    assert job in body and "已返回" in body
    assert injected[-1]["source"] == "pending_tool_call"
    assert job not in tr._PENDING, "跑完要出表"
    assert _events(st, "tool_handed_back"), "交还控制权要留痕"


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


def test_tool_error_after_handback_is_delivered(tmp_path, slow_tool):
    """交还之后工具才出错 —— 错误也得送到，不能吞。"""
    st = _state(tmp_path)

    async def _boom(*, state, **_):
        await asyncio.sleep(0.3)
        raise RuntimeError("late boom")

    tr._REGISTRY.executors[slow_tool] = _boom

    async def go():
        res = await tr.execute(slow_tool, st, check_after_seconds=0.05)
        assert res["status"] == "running"
        await asyncio.sleep(0.7)

    asyncio.run(go())
    body = (st.hook_state.get("injected_messages") or [{}])[-1].get("content", "")
    assert "late boom" in body and "出错" in body


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
    """
    from shared.tools.library import runtime_control as rc
    assert "tail_file" not in rc._RC_ACTIONS
    assert set(rc._RC_ACTIONS) == {
        "inject", "cancel", "list_active", "progress", "jobs"}


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

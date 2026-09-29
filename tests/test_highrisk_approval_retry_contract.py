"""高危审批的后半条契约必须送到模型手上。

框架的设计是：高危工具调用先返回一个 pause，人回答"批准"后 hook 给这条命令
打一个**一次性、绑定逐字参数文本**的确认标记，然后 **模型必须自己用完全相同
的参数把那个工具再调一次**，标记才会被消费、调用才会真的执行。

后半句以前只写在 `_highrisk_confirm_on_turn_start` 的 docstring 里。E2E v19
真实故障（session af9e6a9c 的 execution_events）：

    574  tool.started    submit_job  dry_run=false, job_name=ka_lj_slow_rep1
    577  tool.completed  → status=pause（等审批）
    581  session.message user: "批准"
    582  run.resumed
    585  tool.started    job_status  job_id="ka_lj_slow_rep1"   ← 没重调 submit_job
    586  tool.failed     "invalid local process group"
    591  tool.started    submit_job  job_name=ka_lj_slow_rep1_v2  ← 改了名字
    594  tool.completed  → status=pause（通行证绑的是旧文本，又弹一次）
    602/605/608         run_node / safe_run_bash / check_external_job_health 全试了
    613  request_human_input "submit_job local scheduler 无法启动进程"

调度器完全是好的（同机实测 dry_run=false 能起进程、返回真 PID）。模型的结论
是从框架给它的信息里能推出的最合理结论 —— 框架漏讲了半条契约。

这里回放整条链路，不只断言文案。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from core.sandbox import availability

from core.llm import LLMMessage
from core.loop_hooks import HookContext
from core.loop_hooks_builtin import _highrisk_confirm_on_turn_start
from core.state import State
from shared.lib import dangerous_commands as _dc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nodes" / "experiment"))
from tools.path_roles import experiment_output_dir  # noqa: E402
from tools.resource_manager import (  # noqa: E402
    _cancel_sync,
    _local_container_status,
    _submit_job,
)

HARMLESS = "echo STARTED; sleep 2; echo FINISHED"

#: 回放整条审批链的载体**必须是会弹卡的那一种提交**。
#:
#: 原来的载体是 `scheduler="local"` + `HARMLESS`。#897（「生命周期归属」与「要不要
#: 问人」是两件事）之后那一路不再弹卡：本地受管作业还在同机 cgroup 里，却顶着
#: 「真实外部作业提交」的说法每次问人，文案与事实不符。于是载体一走，这几条就不是
#: 变红那么简单 —— 它们会去验一个**不再存在的局面**。
#:
#: 换成「本地 + 高危命令」：那一路照旧弹卡（类别写明含高危命令）。这里要回放的契约
#: ——通行证绑逐字参数、必须重调同一个工具、拒绝要说话——与「哪种提交触发卡片」
#: 无关，所以换载体不动任何一条判据。
#:
#: 命令本身选得可以真跑：`rm -rf` 的目标是本用例自己 runtime 目录下的一个相对路径，
#: 且 `echo STARTED/FINISHED` 原样保留 —— #793 之后那三条真起进程的用例解封时，
#: 它们的断言不用跟着改。
HIGHRISK = "echo STARTED; rm -rf ./approval-probe-scratch; sleep 2; echo FINISHED"


def _state(tmp_path: Path) -> State:
    state = State(run_id="r1", node_type="experiment", root=tmp_path / "runstate")
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Replay the high-risk approval retry contract end to end.",
    }
    return state


async def _managed_lifecycle_ready(state: State) -> None:
    """把 state 推到"真实执行动作可以发生"的那一步。

    Experiment 的受管执行生命周期（PR#771）要求真实执行先冻结 scope 与执行
    路线：没有它们，`submit_job` 在 `experiment_scope_classification_required` /
    `execution_route_required` 就返回了，**根本到不了**这里要回放的那个审批
    pause —— 于是这条契约看起来是绿的，其实一次都没被验过。
    """
    if state.hook_state.get("_highrisk_test_route_ready"):
        return
    from tools.execution_route import _declare_execution_route
    from tools.run_contract import _classify_experiment_scope

    classified = await _classify_experiment_scope(
        state, scope="operation", operation_category="other",
        reason="Replay the framework high-risk approval contract.")
    assert classified["status"] == "success", classified
    declared = await _declare_execution_route(state, route={
        "schema_version": 2,
        "goal": "回放高危审批契约",
        "evidence_refs": ["test:highrisk-approval-retry"],
        "steps": [{
            "id": "probe", "goal": "提交一条无害的受管作业", "after": [],
            "action": {"tool": "submit_job", "program": "echo"},
            "effects": ["process_tree", "external_job"],
            "workdir_role": "run_root", "expected_outputs": [],
        }],
    })
    assert declared["status"] == "success", declared
    state.hook_state["_highrisk_test_route_ready"] = True


def _writable(state: State) -> Path:
    """workdir 必须落在节点声明过的可写角色里，否则先被路径门挡下。"""
    return Path(experiment_output_dir(state, "runtime", create=True))


def _ctx(state: State, answer: str | None) -> HookContext:
    messages: list[LLMMessage] = []
    if answer is not None:
        messages.append(LLMMessage(role="tool", content=answer, tool_call_id="c1"))
    return HookContext(harness=None, state=state, messages=messages, turn=1)


@pytest.fixture()
def trusted_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit-only approval tests need an immutable identity, not a Docker launch."""
    from core import sandbox

    monkeypatch.setattr(sandbox, "trusted_image_id", lambda: "sha256:test-sandbox")


async def _submit(state: State, workdir: Path, job_name: str) -> dict:
    await _managed_lifecycle_ready(state)
    return await _submit_job(
        state=state, command=HIGHRISK, scheduler="local", job_name=job_name,
        workdir=str(workdir), dry_run=False, route_step_id="probe",
    )


def _cleanup_submission(result: dict) -> None:
    if result.get("status") != "success" or not result.get("container_runtime_id"):
        return
    _cancel_sync(
        "local", str(result["job_id"]), None,
        container_runtime_id=str(result["container_runtime_id"]),
    )
    control = result.get("sandbox_control_dir")
    if control:
        from core.sandbox import cleanup_control_dir

        cleanup_control_dir(str(control))


# ── 整条链路回放 ────────────────────────────────────────────────────────────

# 这三条走 submit_job scheduler=local 的真作业链路。本地作业的 Docker 容器随 PR C
# 一起没了（wangd 09-04：Docker 一行不留），原生的 detached 作业 API 还没有 ——
# issue #793。在那之前这三条不跑：跑了只会撞 prepare_launch(detached=True) 的
# SandboxUnavailable，那不是审批链的判据。
requires_sandbox = pytest.mark.skip(
    reason="local job containers were removed (PR C); native detached jobs pending (#793)")


@pytest.mark.production_sandbox
@requires_sandbox
@pytest.mark.asyncio
async def test_approve_then_identical_retry_actually_runs(tmp_path: Path) -> None:
    """批准 → hook 告诉模型重调 → 逐字重调 → 真的起进程。这条通了才算修好。"""
    state = _state(tmp_path)
    workdir = _writable(state)
    first = await _submit(state, workdir, "probe")
    assert first["status"] == "pause", first

    injected = _highrisk_confirm_on_turn_start(_ctx(state, "批准"))
    assert injected, "批准之后 hook 什么都没跟模型说 —— 这正是 v19 的死因"

    second = await _submit(state, workdir, "probe")
    try:
        assert second["status"] == "success", second
        assert second.get("dry_run") is False
        assert str(second.get("job_id", "")).startswith("hf-"), f"没拿到受管容器标识：{second.get('job_id')}"
        assert len(str(second.get("container_runtime_id", ""))) == 64

        out = Path(second["stdout_path"])
        for _ in range(40):
            if out.exists() and "FINISHED" in out.read_text():
                break
            time.sleep(0.25)
        assert "STARTED" in out.read_text(), "进程根本没跑起来"
    finally:
        _cleanup_submission(second)


@pytest.mark.asyncio
async def test_the_injected_message_says_to_call_the_same_tool_again(
    tmp_path: Path, trusted_sandbox: None,
) -> None:
    """光"有消息"不够 —— 得说清是"重调同一个工具"，否则模型照样会去查状态。"""
    state = _state(tmp_path)
    await _submit(state, _writable(state), "probe")

    text = "\n".join(m.content or "" for m in _highrisk_confirm_on_turn_start(_ctx(state, "批准")))

    assert "submit_job" in text, "没点名要重调哪个工具"
    assert "重新调用" in text or "重调" in text
    # 反面也必须讲：模型的默认假设就是"批准了 = 已经执行了"
    assert "不会" in text and "执行" in text, f"没否掉「批准即执行」的默认假设：{text}"


@pytest.mark.asyncio
async def test_changing_any_argument_voids_the_approval_and_says_so(
    tmp_path: Path, trusted_sandbox: None,
) -> None:
    """通行证绑定逐字文本 —— 这是对的（批的是那一条命令），但必须讲出来。

    v19 里模型批准后把 job_name 改成 _v2，于是又被挡了一次，它以为是调度器坏了。
    """
    state = _state(tmp_path)
    await _submit(state, _writable(state), "probe")
    text = "\n".join(m.content or "" for m in _highrisk_confirm_on_turn_start(_ctx(state, "批准")))
    assert "参数" in text and ("作废" in text or "失效" in text), text

    renamed = await _submit(state, _writable(state), "probe_v2")
    assert renamed["status"] == "pause", "改了参数还放行 —— 审批就形同虚设了"


@pytest.mark.asyncio
async def test_denial_tells_the_model_not_to_retry(
    tmp_path: Path, trusted_sandbox: None,
) -> None:
    """拒绝时也要说话。以前是静默的，模型只能靠再撞一次墙才知道。"""
    state = _state(tmp_path)
    await _submit(state, _writable(state), "probe")

    injected = _highrisk_confirm_on_turn_start(_ctx(state, "先别跑"))
    assert injected, "拒绝之后什么都不说，模型只会原样重试"
    text = "\n".join(m.content or "" for m in injected)
    assert "没有" in text or "未" in text

    again = await _submit(state, _writable(state), "probe")
    assert again["status"] == "pause", "拒绝之后居然放行了"


def test_the_replay_carrier_is_actually_a_high_risk_command() -> None:
    """载体之所以弹卡，必须**是因为它真的高危** —— 不是因为碰巧还没人收窄这条路。

    这一条把"为什么会弹卡"钉在明处：上面那几条回放整条审批链的用例全靠载体产生
    pause，而载体停止弹卡的方式有两种 —— 分类器不再认这条命令（那是分类器的事），
    或者这一路的确认策略又变了（那是策略的事）。哪一种都该在这里先说出来。
    """
    assert _dc.match_high_risk(HIGHRISK) is not None, (
        f"载体 {HIGHRISK!r} 已经不被判为高危 —— 上面几条回放审批链的用例"
        "会因为「根本没有卡」而失去被测对象"
    )


def test_hook_stays_quiet_when_there_is_no_pending_ask(tmp_path: Path) -> None:
    """没有待批的东西就别往上下文里塞 —— 每轮都注入等于噪音。"""
    assert _highrisk_confirm_on_turn_start(_ctx(_state(tmp_path), "批准")) is None


# ── 契约要在 pause 当时就送到，而不只是批准之后 ──────────────────────────

def test_pause_payload_carries_a_model_facing_contract(tmp_path: Path) -> None:
    payload = _dc.build_pause_payload(
        _state(tmp_path), tool="submit_job", text="x", category="真实外部作业提交",
        preview="scheduler=local",
    )
    contract = payload.get("approval_contract") or ""
    assert "submit_job" in contract and ("重新调用" in contract or "重调" in contract), contract
    # 给人的那份不能被改坏
    assert "批准" in payload["pause_event"]["context"]


# ── 传错 job_id 时要说清合法值 ─────────────────────────────────────────────

def test_local_job_status_error_names_the_valid_id_shape() -> None:
    err = _local_container_status("ka_lj_slow_rep1")["stderr"]

    assert "ka_lj_slow_rep1" in err, "没回显它到底传了什么"
    assert "submit_job" in err, "没说合法值从哪儿来"
    assert "hf-" in err and "PID" in err


def test_a_host_pid_is_never_a_local_job_identity() -> None:
    import os
    assert _local_container_status(str(os.getpgrp()))["ok"] is False


def test_local_job_status_rejects_a_reused_container_name(monkeypatch) -> None:
    from core import sandbox

    monkeypatch.setattr(sandbox, "inspect_container", lambda _name: {
        "exists": True,
        "running": True,
        "managed": True,
        "id": "b" * 64,
        "kind": "job",
        "namespace": "harness",
    })
    result = _local_container_status("hf-" + "a" * 20, "c" * 64)
    assert result["ok"] is False
    assert "不匹配" in result["stderr"]


def test_local_job_status_rejects_an_unmanaged_same_name_container(monkeypatch) -> None:
    from core import sandbox

    monkeypatch.setattr(sandbox, "inspect_container", lambda _name: {
        "exists": True,
        "running": True,
        "managed": False,
        "id": "b" * 64,
    })
    result = _local_container_status("hf-" + "a" * 20, "b" * 64)
    assert result["ok"] is False
    assert "受管标签" in result["stderr"]


def test_local_job_status_accepts_the_namespace_bound_identity(monkeypatch) -> None:
    from core import sandbox

    monkeypatch.setenv("HARNESS_SANDBOX_NAMESPACE", "node20")
    monkeypatch.setattr(sandbox, "inspect_container", lambda _name: {
        "exists": True,
        "running": False,
        "managed": True,
        "id": "b" * 64,
        "kind": "job",
        "namespace": "node20",
    })

    result = _local_container_status("hf-node20-" + "a" * 20, "b" * 64)

    assert result["ok"] is True
    assert result["stdout"] == "NOT_RUNNING"


# ── 界面印的选项，后端必须接得住（issue #422）────────────────────────────────

def _envelope(response: str, node: str = "experiment") -> str:
    """resume 回填给模型的那个信封 —— hook 从 messages 里读到的就是它。

    见 core/agent_loop.py::resume_loop。此前测试直接喂裸文本 "批准"，于是
    "判定读的是整条 JSON 而不是人说的那句话" 这个缺陷从来没被看见。
    """
    import json as _json
    return _json.dumps(
        {"status": "success", "response": response, "asked_by": node},
        ensure_ascii=False)


@pytest.mark.production_sandbox
@requires_sandbox
@pytest.mark.asyncio
async def test_ui_prints_an_option_index_and_the_backend_honours_it(tmp_path: Path) -> None:
    """CLI 印 `[1] 批准执行`，人输入 `1` —— 这一路必须真的放行。

    issue #422 现场：`1` 被判未批准 → 不发通行证 → hook 告诉模型"重调也会被挡"
    → 模型再没重调 submit_job。序号从 payload 自己的 options 里算，选项顺序
    将来改了这条测试跟着改，不会假绿。
    """
    state = _state(tmp_path)
    workdir = _writable(state)
    first = await _submit(state, workdir, "probe")
    assert first["status"] == "pause", first

    options = first["pause_event"]["options"]
    index = str(options.index(_dc.APPROVE_OPTION) + 1)

    _dc.record_highrisk_answer(state, index, options)
    injected = _highrisk_confirm_on_turn_start(_ctx(state, _envelope(index)))
    assert injected, f"人按界面输入 {index}，框架仍判未批准"
    assert "重新调用" in "\n".join(m.content or "" for m in injected)

    second = await _submit(state, workdir, "probe")
    try:
        assert second["status"] == "success", second
    finally:
        _cleanup_submission(second)


@pytest.mark.asyncio
async def test_the_deny_option_index_still_denies(
    tmp_path: Path, trusted_sandbox: None,
) -> None:
    """认序号不能变成"什么数字都放行" —— [2] 拒绝必须仍然是拒绝。"""
    state = _state(tmp_path)
    first = await _submit(state, _writable(state), "probe")
    options = first["pause_event"]["options"]
    index = str(options.index(_dc.DENY_OPTION) + 1)

    _dc.record_highrisk_answer(state, index, options)
    injected = _highrisk_confirm_on_turn_start(_ctx(state, _envelope(index)))
    assert "没有" in "\n".join(m.content or "" for m in injected)
    again = await _submit(state, _writable(state), "probe")
    assert again["status"] == "pause", "选了拒绝还放行"


@pytest.mark.production_sandbox
@requires_sandbox
@pytest.mark.asyncio
async def test_free_text_approval_survives_the_json_envelope(tmp_path: Path) -> None:
    """没有记账的路径（老 resume）也得判对：判定要先拆信封再匹配。

    `^yes\\b` 这类锚定规则以前对着 `{"status": ...}` 串首匹配，永远不成立。
    """
    state = _state(tmp_path)
    workdir = _writable(state)
    await _submit(state, workdir, "probe")

    injected = _highrisk_confirm_on_turn_start(_ctx(state, _envelope("yes")))
    assert injected and "重新调用" in "\n".join(m.content or "" for m in injected)
    second = await _submit(state, workdir, "probe")
    try:
        assert second["status"] == "success", second
    finally:
        _cleanup_submission(second)


def test_the_human_facing_text_names_the_index_the_ui_will_print(tmp_path: Path) -> None:
    """给人的说明和界面渲染必须是同一份选项表推出来的，不能各写各的。"""
    payload = _dc.build_pause_payload(
        _state(tmp_path), tool="submit_job", text="x", category="真实外部作业提交",
        preview="scheduler=local",
    )
    pe = payload["pause_event"]
    assert pe["options"] == _dc.HIGHRISK_OPTIONS
    index = pe["options"].index(_dc.APPROVE_OPTION) + 1
    assert f"回复 {index}" in pe["context"], pe["context"]


def test_denial_wording_is_not_swallowed_by_a_loose_approve_word(tmp_path: Path) -> None:
    """选项优先于关键词：选了"拒绝"就是拒绝，不进宽松词表。"""
    assert _dc.looks_like_approval(_envelope(_dc.DENY_OPTION)) is False
    assert _dc.looks_like_approval(_envelope(_dc.APPROVE_OPTION)) is True
    assert _dc.looks_like_approval(_envelope("先别跑")) is False
    assert _dc.looks_like_approval(_envelope("")) is False

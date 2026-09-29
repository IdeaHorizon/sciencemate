"""guard 拦截的判重与证据提取回归（2026-07-31，run 1785469523-90f86c）。

该 run 里节点对同一个 /etc 目标反复提交安装命令，40 余轮没有任何收敛信号。
排查出两个 hook 共用同一个根因：**失败正文没人读**。

_diagnose_failure 与 _short_error 都只读 result["stderr_tail"]/["stdout_tail"]，
而 guard 拦截 / 高危 gate / 工具层错误的正文在 result["error"] 里。后果：
  - repeated_error_detector 拿到空串 → 指纹 confidence=low → is_hard=False
    → 永不进硬计数 → "2 次换方向 / 3 次禁止重试" 的升级阶梯完全不触发；
  - execution_control 的 evidence 恒为 "(no stderr/stdout tail)"。

另一个独立缺陷：_component_from_text 取整条命令的最后一个 token 当组件名，
实测注入过 head / x86_64-linux-gnu / hea / Chec / cu 这类无意义值。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment import hooks as H
from nodes.experiment.tools import safe_bash as sb


@pytest.fixture
def state(monkeypatch):
    home = Path(tempfile.mkdtemp(prefix="hf-blk-"))
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(home))
    base = home / "projects" / "p" / "runs"
    base.mkdir(parents=True, exist_ok=True)
    return State.new(node_type="experiment", base_dir=base, project_id="p")


def _guard_record(cmd: str = "echo x | sudo tee /etc/OpenCL/vendors/nvidia.icd",
                  target: str = "/etc", scope: str = "protected_path") -> dict:
    result = sb._scope_guard_error(
        cmd, scope, f"write op mentions protected root: {target}", target)
    return {"name": "safe_run_bash", "args": {"cmd": cmd}, "result": result}


def test_guard_block_carries_structured_blocker():
    """判重靠结构化字段，不靠正则解析中文错误正文。"""
    result = _guard_record()["result"]
    assert result["blocker"] == {
        "kind": "scope_guard", "scope": "protected_path", "target": "/etc"}


def test_guard_block_is_hard_countable(state):
    """guard 拦截必须够格进入硬计数 —— 否则升级阶梯永远不触发。"""
    diag = H._diagnose_failure(_guard_record(), state)

    assert diag is not None
    assert diag["is_hard"] is True
    assert diag["counter_key"].endswith("scope_guard|protected_path|/etc")
    assert diag["focus"]["primary_error"]


def test_same_target_escalates_on_second_attempt(state):
    """同一 (scope, target) 第 2 次出现就必须强制换方向。"""
    from core.loop_hooks import HookContext

    def ctx(turn: int):
        c = HookContext.__new__(HookContext)
        c.state, c.turn, c.tool_call_records, c.messages = state, turn, [_guard_record()], []
        return c

    H.repeated_error_detector_on_turn_end(ctx(1))
    assert H.repeated_error_detector_on_turn_start(ctx(2)) is None, "第 1 次不该升级"

    H.repeated_error_detector_on_turn_end(ctx(2))
    out = H.repeated_error_detector_on_turn_start(ctx(3))
    assert out, "第 2 次必须注入强制换方向"
    assert "第 **2 次**" in out[0].content


def test_different_targets_do_not_merge(state):
    """不同目标是不同 blocker，不该互相累计。"""
    from core.loop_hooks import HookContext

    def ctx(turn: int, rec: dict):
        c = HookContext.__new__(HookContext)
        c.state, c.turn, c.tool_call_records, c.messages = state, turn, [rec], []
        return c

    H.repeated_error_detector_on_turn_end(ctx(1, _guard_record(target="/etc")))
    H.repeated_error_detector_on_turn_end(
        ctx(2, _guard_record(cmd="touch /usr/lib/x", target="/usr")))
    assert H.repeated_error_detector_on_turn_start(ctx(3, _guard_record())) is None


def test_short_error_reads_error_field():
    """evidence 行不该再是 "(no stderr/stdout tail)"。"""
    result = _guard_record()["result"]
    evidence = H._short_error(result)

    assert evidence != "(no stderr/stdout tail)"
    assert "/etc" in evidence
    # 只要摘要段，不要把整段标准改写指引灌进上下文
    assert "标准改写模式" not in evidence
    assert len(evidence) <= 220


def test_error_text_prefers_real_stderr():
    """有 stderr_tail 时仍以它为准（不回退 error）。"""
    assert H._result_error_text(
        {"status": "error", "stderr_tail": "undefined reference to `foo'",
         "error": "wrapper text"}) == "undefined reference to `foo'"


@pytest.mark.parametrize("cmd", [
    "which git 2>&1; which make 2>&1; which g++ 2>&1; g++ --version 2>&1 | head -2",
    "ls /usr/lib/x86_64-linux-gnu",
    "cat /etc/OpenCL/vendors/nvidia.icd",
])
def test_component_not_guessed_from_last_token(cmd):
    """命令最后一个词跟"哪个组件坏了"无关 —— 推不出就返回 None。"""
    assert H._component_from_text("", cmd) is None


def test_component_still_derived_from_build_log():
    """log 路径仍是组件名的正当来源。"""
    assert H._component_from_text("/x/bld/atm.bldlog", "make -j8") == "atm"


@pytest.mark.parametrize("cmd,is_probe", [
    ("which nvidia-smi", True), ("g++ --version", True), ("clinfo", True),
    ("man make", True), ("make -j8", False), ("./configure --prefix=/x", False),
])
def test_probe_commands_excluded_from_issue_ledger(cmd, is_probe):
    """探查失败是结论不是故障：`which nvidia-smi` 非 0 只说明没装。"""
    assert H._is_probe_cmd(cmd) is is_probe

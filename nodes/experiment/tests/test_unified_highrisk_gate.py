"""统一高危门测试（v0.11.1，采纳 core review）。

验收点：safe_run_bash / safe_execute_python 的高危授权语义与框架
dangerous_commands（PR #97）完全一致——普通模式 pause、bypass 直跑、
fixture allowlist 事前预授权、批准一次性消费；节点补充 pattern（框架清单
没有的 kill -9 等）也走同一套流程；EXPERIMENT_ALLOW_HIGHRISK_BASH 已无效。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

_NODE_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _NODE_DIR.parents[1]
sys.path.insert(0, str(_NODE_DIR))   # tools.* 可 import

_spec = importlib.util.spec_from_file_location(
    "experiment_safe_bash_under_test", _NODE_DIR / "tools" / "safe_bash.py")
_sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sb)

from shared.lib import dangerous_commands as _danger   # noqa: E402
from core.state import State                            # noqa: E402
from nodes.experiment.tools import run_contract as _run_contract  # noqa: E402


def _make_state() -> State:
    return State.new(node_type="experiment",
                     base_dir=Path(tempfile.mkdtemp(prefix="hf-gate-")))


def _bind_operation_inputs(state: State) -> None:
    """Bind public safe-tool tests to the declared non-scientific request."""
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Exercise the unified high-risk gate regression.",
        "stage": "diagnostic",
    }


def _gate(state, text, mode="shell", kind="bash"):
    return _sb._unified_highrisk_gate(
        state, text, mode=mode,
        tool=f"safe_run_bash" if kind == "bash" else "safe_execute_python",
        kind=kind)


def test_normal_mode_pauses_on_framework_pattern() -> None:
    state = _make_state()
    res = _gate(state, "sudo rm -rf /scratch/x")
    assert res is not None and res["status"] == "pause"
    assert "高危" in res["pause_event"]["question"]


def test_node_extra_pattern_also_pauses() -> None:
    """kill -9 在框架清单里没有，节点补充清单命中 → 同样 pause 而非放行。"""
    state = _make_state()
    res = _gate(state, "kill -9 12345")
    assert res is not None and res["status"] == "pause"


def test_allowlist_preauth_passes_with_audit() -> None:
    state = _make_state()
    state.hook_state["highrisk_bash_allowlist"] = ["sudo apt install libxml"]
    res = _gate(state, "sudo apt install libxml-libxml-perl")
    assert res is None   # 放行
    import json
    events = [json.loads(l) for l in
              state.transcript_path.read_text().splitlines() if l.strip()]
    allowed = [e for e in events if e.get("event") == "highrisk_bash_allowed"]
    assert len(allowed) == 1 and "allowlist" in allowed[0]["authorized_by"]


def test_confirmed_is_consumed_once() -> None:
    state = _make_state()
    cmd = "sudo systemctl restart myapp"
    _danger.mark_confirmed(state, cmd)
    assert _gate(state, cmd) is None                     # 第一次：批准放行
    res2 = _gate(state, cmd)
    assert res2 is not None and res2["status"] == "pause"  # 第二次：重新问


def test_bypass_mode_runs_directly(monkeypatch) -> None:
    state = _make_state()
    monkeypatch.setattr(_danger, "BYPASS_ENABLED", True)
    try:
        assert _gate(state, "sudo rm -rf /x") is None
    finally:
        monkeypatch.setattr(_danger, "BYPASS_ENABLED", False)


def test_env_var_no_longer_authorizes(monkeypatch) -> None:
    """旧 EXPERIMENT_ALLOW_HIGHRISK_BASH 整 run 授权已删除 → 设了也照样 pause。"""
    state = _make_state()
    monkeypatch.setenv("EXPERIMENT_ALLOW_HIGHRISK_BASH", "1")
    res = _gate(state, "sudo rm -rf /scratch/x")
    assert res is not None and res["status"] == "pause"


def test_python_kind_uses_code_preview_event() -> None:
    state = _make_state()
    res = _gate(state, "import shutil; shutil.rmtree('/data')",
                mode="python", kind="python")
    assert res is not None and res["status"] == "pause"
    import json
    events = [json.loads(l) for l in
              state.transcript_path.read_text().splitlines() if l.strip()]
    pend = [e for e in events
            if e.get("event") == "highrisk_python_blocked_pending_confirm"]
    assert len(pend) == 1 and "shutil" in pend[0]["code_preview"]


def test_python_highrisk_pause_explains_static_subprocess_and_write_facts() -> None:
    """审批首屏必须有可核查动作摘要，不能只给 Python 源码前 300 字。"""
    state = _make_state()
    code = """import os, subprocess
base = '/project/workspace/experiment'
build = os.path.join(base, 'runtime', 'build')
os.makedirs(build, exist_ok=True)
subprocess.run(['grep', '-n', 'dmpar', os.path.join(base, 'src', 'WRF', 'arch', 'configure.defaults')])
"""
    result = _gate(state, code, mode="python", kind="python")
    assert result is not None and result["status"] == "pause"
    context = result["pause_event"]["context"]
    assert "审批摘要（由代码静态提取，非 LLM 自述）" in context
    assert "grep -n dmpar '<动态参数>'" in context
    assert "os.makedirs" in context
    assert "未静态证明子进程的全部副作用" in context


def test_bypass_leaves_no_stale_confirmation(monkeypatch) -> None:
    """回归（core review 残留风险）：bypass 模式下不预标记 mark_confirmed——
    内层门 bypass 分支不 consume，标记会残留；/bypass off 后同一 code
    不得凭残留标记免确认放行。"""
    state = _make_state()
    code = "import shutil; shutil.rmtree('/data')"

    # bypass 开：统一门放行；模拟 _safe_execute_python 的预标记守卫逻辑
    monkeypatch.setattr(_danger, "BYPASS_ENABLED", True)
    assert _gate(state, code, mode="python", kind="python") is None
    if not _danger.bypass_enabled() and _danger.match_high_risk(code, mode="python"):
        _danger.mark_confirmed(state, code)   # 守卫下不应执行

    # bypass 关：同一 code 必须重新 pause，而不是凭残留标记放行
    monkeypatch.setattr(_danger, "BYPASS_ENABLED", False)
    assert not _danger.is_confirmed(state, code), "bypass 期间不得残留确认标记"
    res = _gate(state, code, mode="python", kind="python")
    assert res is not None and res["status"] == "pause"


def test_bypass_cannot_skip_execution_scope_classification(
    monkeypatch, tmp_path,
) -> None:
    state = _make_state()
    target = tmp_path / "unclassified.txt"
    monkeypatch.setattr(_danger, "BYPASS_ENABLED", True)
    try:
        result = asyncio.run(_sb._safe_write_file(state, str(target), "x"))
    finally:
        monkeypatch.setattr(_danger, "BYPASS_ENABLED", False)

    assert result["reason"] == "experiment_scope_classification_required"
    assert target.exists() is False


def test_bypass_only_overrides_soft_environment_scope(
    monkeypatch,
) -> None:
    """bypass 只跳过精确环境授权，不改变路径有效性事实。"""
    state = _make_state()
    _bind_operation_inputs(state)
    classified = asyncio.run(_run_contract._classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="验证 bypass 仅适用于环境类软边界。",
    ))
    assert classified["status"] == "success"
    writes = []

    async def fake_writer(_state, path, content, create_dirs=True, **_kw):
        writes.append((path, content, create_dirs))
        return {"status": "success"}

    monkeypatch.setattr(_sb, "_orig_write_file", fake_writer)
    monkeypatch.setattr(_danger, "BYPASS_ENABLED", True)
    try:
        result = asyncio.run(_sb._safe_write_file(
            state, "/etc/hf-experiment-soft-scope.conf", "x"))
    finally:
        monkeypatch.setattr(_danger, "BYPASS_ENABLED", False)

    assert result["status"] == "success"
    assert writes == [("/etc/hf-experiment-soft-scope.conf", "x", True)]
    import json
    events = [json.loads(line) for line in
              state.transcript_path.read_text().splitlines() if line.strip()]
    assert any(event.get("event") == "scope_guard_write_file_bypassed"
               for event in events)


def test_bypass_cannot_write_framework_state_or_source_baseline(
    monkeypatch, tmp_path,
) -> None:
    state = _make_state()
    _bind_operation_inputs(state)
    classified = asyncio.run(_run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="other",
        reason="验证框架状态和源码基线是不可绕过的完整性边界。"))
    assert classified["status"] == "success"
    baseline = tmp_path / "source-baseline"
    baseline.mkdir()
    state.hook_state["path_roles"] = {
        "source_baseline_root": {
            "path": str(baseline), "writable": False,
        },
    }
    called = False

    async def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "success"}

    monkeypatch.setattr(_sb, "_orig_write_file", forbidden)
    targets = [
        Path(state.root) / "artifacts" / "poison.json",
        baseline / "poison.c",
    ]
    monkeypatch.setattr(_danger, "BYPASS_ENABLED", True)
    try:
        results = [asyncio.run(_sb._safe_write_file(state, str(target), "x"))
                   for target in targets]
    finally:
        monkeypatch.setattr(_danger, "BYPASS_ENABLED", False)

    assert [result["blocker"]["scope"] for result in results] == [
        "protected_framework_state", "protected_source_tree"]
    assert called is False
    assert all(not target.exists() for target in targets)


def test_bypass_cannot_cross_baseline_via_bash_or_python(
    monkeypatch, tmp_path,
) -> None:
    state = _make_state()
    _bind_operation_inputs(state)
    classified = asyncio.run(_run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="other",
        reason="验证 Bash 和 Python 共用不可绕过的源码边界。"))
    assert classified["status"] == "success"
    baseline = tmp_path / "source-baseline"
    baseline.mkdir()
    state.hook_state["path_roles"] = {
        "source_baseline_root": {
            "path": str(baseline), "writable": False,
        },
    }
    run_root = _sb.experiment_output_dir(state, "runtime", create=True)
    executed = False

    async def forbidden(*_args, **_kwargs):
        nonlocal executed
        executed = True
        return {"status": "success"}

    monkeypatch.setattr(_sb, "_exec_and_log", forbidden)
    python_code = (
        "from pathlib import Path\n"
        f"Path({str(baseline / 'python.txt')!r}).write_text('x')"
    )
    monkeypatch.setattr(_danger, "BYPASS_ENABLED", True)
    try:
        bash_result = asyncio.run(_sb._safe_run_bash(
            state, f"touch {baseline / 'bash.txt'}", cwd=str(run_root)))
        python_result = asyncio.run(_sb._safe_execute_python(
            state, python_code, cwd=str(run_root)))
    finally:
        monkeypatch.setattr(_danger, "BYPASS_ENABLED", False)

    assert bash_result["blocker"]["scope"] == "protected_source_tree"
    assert python_result["blocker"]["scope"] == "protected_source_tree"
    assert executed is False
    assert not (baseline / "bash.txt").exists()
    assert not (baseline / "python.txt").exists()

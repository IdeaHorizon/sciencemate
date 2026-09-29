from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.tool_registry import get_tool
from nodes.experiment.tools import execution_route, safe_bash
from nodes.experiment.tools.execution_route import (
    enforce_execution_route,
    execution_route_block,
)


_NODE = Path(__file__).resolve().parents[1]
_FALSE_ROUTE_GUIDANCE = (
    "任何写入",
    "Python 必传",
    "只读诊断随时可用",
    "只要本次会写",
    "会写文件时必须有对应的冻结路线步骤",
    "路线要求只来自",
    "safe_execute_python 的只读诊断照常可用",
)


def _rules() -> str:
    config = yaml.safe_load((_NODE / "harness.yaml").read_text(encoding="utf-8"))
    return "\n".join(config["rules"])


def _assert_python_description_does_not_overclaim(description: str) -> None:
    assert (
        "不得根据这段描述推定未识别的 Python 效果已经受路线保护。"
        in description
    )
    sentences = {part.strip() for part in description.split("。") if part.strip()}
    assert "未识别的 Python 效果已经受路线保护" not in sentences


def _effectful_decision(tool: str, **updates: object) -> dict[str, object]:
    decision: dict[str, object] = {
        "decision": "route_not_required",
        "tool": tool,
        "policy": "low_risk_effectful",
        "read_only": False,
        "dry_run": False,
        "observed_effects": ["workspace_write"],
        "effective_effects": ["workspace_write"],
        "scope_required": True,
        "scope_status": "classified",
        "scope_mode": "operational",
    }
    decision.update(updates)
    return decision


def test_registered_execution_tool_descriptions_tell_the_actual_route_boundary():
    bash_description = get_tool("safe_run_bash").description
    python_description = get_tool("safe_execute_python").description

    assert (
        "是否需要路线以本次调用返回的机械判定和 next_action 为准，"
        "不要靠命令名称清单猜测。"
        in bash_description
    )
    assert "当前冻结路线已有与本次命令入口对应的就绪步骤时" in bash_description
    assert "解析结果要求显式绑定时传 route_step_id" in bash_description

    assert (
        "是否需要新路线以本次调用返回的 reason 与提示为准。"
        in python_description
    )
    assert (
        "当前冻结路线已有与 safe_execute_python 对应的就绪步骤时，"
        "真实计算必须传该步骤的 route_step_id"
        in python_description
    )
    assert "只读诊断改用 safe_run_bash，不会消耗该步骤" in python_description
    assert "不要把诊断绑定到该步骤上，否则会把步骤记成已完成" in python_description
    assert (
        "直接写出的 tarfile/zipfile extractall 或 extract，以及 "
        "shutil.unpack_archive，会被识别为 environment_change"
        in python_description
    )
    assert "动态调用、别名和其他未识别的 Python 效果" in python_description
    assert "需要 shell 效果分析的动作使用 safe_run_bash" in python_description
    _assert_python_description_does_not_overclaim(python_description)

    for name, description in (
        ("safe_run_bash", bash_description),
        ("safe_execute_python", python_description),
    ):
        for false_claim in _FALSE_ROUTE_GUIDANCE:
            assert false_claim not in description, (name, false_claim)


def test_harness_rules_tell_the_actual_route_boundary():
    rules = _rules()
    assert "工具返回需要路线时" in rules
    assert "不要靠命令名称清单猜测" in rules
    assert "safe_run_bash 以本次命令入口匹配步骤" in rules
    assert "safe_execute_python 的对应就绪步骤只承接真实计算" in rules
    assert "只读诊断改用 `safe_run_bash`，不会消耗该步骤" in rules
    assert "不要把诊断绑定到步骤上，否则会把步骤记成已完成" in rules
    assert "有界且被机械判定为 low_risk_effectful 的 run-local 写入通常无需新建路线" in rules
    assert (
        "直接写出的 `tarfile`/`zipfile` `extractall` 或 `extract`，以及 "
        "`shutil.unpack_archive` 会被识别为 `environment_change`"
        in rules
    )
    assert "动态调用、别名和其他 Python 未识别效果" in rules
    assert "Python 未识别效果不因此自动获得路线保护" in rules
    assert "改用 `safe_run_bash`" in rules
    for false_claim in _FALSE_ROUTE_GUIDANCE:
        assert false_claim not in rules, false_claim


def test_python_unpack_rejection_guides_route_declaration_and_retry_without_completing_other_step(
    tmp_path,
    monkeypatch,
):
    from nodes.experiment.tests.test_route_shadow_wiring import _state

    state = _state(tmp_path)
    runtime = state.root / "outputs" / "experiment" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)

    spawned: list[str] = []

    async def fake_exec(_state, command, **_kwargs):
        spawned.append(command)
        return {
            "status": "success",
            "returncode": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)
    code = "import tarfile\ntarfile.open('src.tgz').extractall('src')"
    rejected = asyncio.run(safe_bash._safe_execute_python(
        state,
        code,
        cwd=str(runtime),
    ))

    assert rejected["status"] == "error"
    assert rejected["reason"] == "execution_route_required"
    assert rejected["blocker"]["node_action"] == "investigate_then_declare_or_amend_route"
    assert "declare_execution_route" in rejected["error"]
    assert spawned == []

    route = {
        "schema_version": 2,
        "goal": "解包源码并保留另一条独立步骤",
        "evidence_refs": ["test:python-unpack-route"],
        "steps": [
            {
                "id": "unpack",
                "goal": "用 Python 解包源码",
                "after": [],
                "action": {"tool": "safe_execute_python", "program": "python"},
                "effects": ["environment_change", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": [],
            },
            {
                "id": "unrelated",
                "goal": "另一条尚未执行的步骤",
                "after": [],
                "action": {"tool": "safe_run_bash", "program": "echo"},
                "effects": ["workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": [],
            },
        ],
    }
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=route,
    ))
    assert declared["status"] == "success", declared

    retried = asyncio.run(safe_bash._safe_execute_python(
        state,
        code,
        cwd=str(runtime),
        route_step_id="unpack",
    ))

    assert retried["status"] == "success", retried
    assert len(spawned) == 1
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["unpack"]["state"] == "verified"
    assert snapshot["steps"]["unrelated"]["state"] == "pending"
    description = get_tool("safe_execute_python").description
    assert "shutil.unpack_archive" in description
    _assert_python_description_does_not_overclaim(description)


@pytest.mark.parametrize(
    "code",
    [
        "import tarfile\ntarfile.open('src.tgz').extractall('src')",
        "import tarfile\ntarfile.open('src.tgz').extract('member', 'src')",
        "import zipfile\nzipfile.ZipFile('src.zip').extractall('src')",
        "import zipfile\nzipfile.ZipFile('src.zip').extract('member', 'src')",
        "import shutil\nshutil.unpack_archive('src.tgz', 'src')",
    ],
)
def test_python_archive_unpack_apis_require_route_without_spawning(
    tmp_path,
    monkeypatch,
    code,
):
    from nodes.experiment.tests.test_route_shadow_wiring import _state

    state = _state(tmp_path)
    runtime = state.root / "outputs" / "experiment" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    spawned = False

    async def forbidden_exec(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success", "returncode": 0}

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_exec)

    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        code,
        cwd=str(runtime),
    ))

    assert result["status"] == "error"
    assert result["reason"] == "execution_route_required"
    assert spawned is False


def test_bare_extract_and_import_aliases_keep_the_existing_python_fallback(
    tmp_path,
    monkeypatch,
):
    from nodes.experiment.tests.test_route_shadow_wiring import _state

    state = _state(tmp_path)
    runtime = state.root / "outputs" / "experiment" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    spawned: list[str] = []

    async def fake_exec(_state, command, **_kwargs):
        spawned.append(command)
        return {"status": "success", "returncode": 0}

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)
    for code in (
        "df.c.str.extract(r'(x)')",
        "import shutil as archive_tools\narchive_tools.unpack_archive('a.tgz', 'src')",
    ):
        result = asyncio.run(safe_bash._safe_execute_python(
            state,
            code,
            cwd=str(runtime),
        ))
        assert result["status"] == "success", (code, result)

    assert len(spawned) == 2


def test_python_unpack_uses_requirements_environment_change_projection():
    unpack = safe_bash._python_route_action(
        code="import shutil\nshutil.unpack_archive('a.tgz', 'src')",
        execution_stage="diagnostic",
        requirements=None,
    )
    requirements = safe_bash._python_route_action(
        code="print('diagnostic')",
        execution_stage="diagnostic",
        requirements=["numpy"],
    )

    assert unpack["observed_effects"] == requirements["observed_effects"] == [
        "environment_change",
        "workspace_write",
    ]
    assert "process_tree" not in unpack["observed_effects"]
    assert execution_route._policy_for_effects(
        set(unpack["observed_effects"]),
        read_only=False,
    ) == "guarded_process"


def test_legacy_dependencies_sentinel_matches_no_real_provisioning_action():
    legacy = {
        "route_type": "official_build_system",
        "activities": {
            "uses_source": True,
            "compile": True,
            "run": True,
            "manage_dependencies": True,
        },
        "path_roles": {
            "source_baseline_root": "/tmp/source",
            "build_root": "/tmp/build",
            "run_root": "/tmp/run",
        },
        "env_domains": {
            "compiler": {"selected": "gcc", "probes": ["gcc --version"]},
            "build_discovery": {
                "selected": "cmake",
                "probes": ["cmake --version"],
            },
        },
        "expected_artifacts": [{"path": "/tmp/build/app"}],
        "cache_invalidation": {"clean_on_toolchain_change": True},
    }
    normalized = execution_route.normalize_declared_route(legacy)
    assert normalized["valid"] is True, normalized
    dependency_step = normalized["route"]["steps"][0]
    assert dependency_step["id"] == "legacy_dependencies"
    assert dependency_step["action"] == {
        "tool": "safe_run_bash",
        "program": "legacy:legacy_dependencies",
        "evidence_refs": [
            "gcc --version",
            "cmake --version",
            "legacy_expected:/tmp/build/app",
        ],
    }

    commands = (
        "pip install numpy",
        "python -m pip install numpy",
        "uv sync",
        "./configure",
        "tar -xzf src.tgz",
    )
    for command in commands:
        action = safe_bash._bash_route_action(
            command,
            execution_stage="diagnostic",
        )
        assert {"environment_change", "process_tree"} <= set(
            action["observed_effects"]
        ), (command, action)
        assert execution_route._tool_program_matches_step(
            action,
            dependency_step,
        ) is False


def test_bash_unpack_route_requirement_is_unchanged(tmp_path, monkeypatch):
    from nodes.experiment.tests.test_route_shadow_wiring import _state

    state = _state(tmp_path)
    runtime = state.root / "outputs" / "experiment" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    spawned = False

    async def forbidden_exec(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success", "returncode": 0}

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_exec)
    result = asyncio.run(safe_bash._safe_run_bash(
        state,
        "tar -xzf src.tgz",
        cwd=str(runtime),
    ))

    assert result["status"] == "error"
    assert result["reason"] == "execution_route_required"
    assert spawned is False


def test_ready_python_route_step_rejects_unbound_python_and_guidance_redirects_diagnosis(
    tmp_path,
    monkeypatch,
):
    from nodes.experiment.tests.test_route_shadow_wiring import (
        _single_step_route,
        _state,
    )

    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="safe_execute_python",
            program="python",
            role="run_root",
            effects=["workspace_write"],
        ),
    ))
    assert declared["status"] == "success"

    async def fake_exec(*_args, **_kwargs):
        return {"status": "success", "stdout_tail": "", "stderr_tail": ""}

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)
    result = asyncio.run(safe_bash._safe_execute_python(state, "print('diag')"))

    assert result["status"] == "error"
    assert result["reason"] == "execution_route_step_id_required"
    description = get_tool("safe_execute_python").description
    assert "真实计算必须传该步骤的 route_step_id" in description
    assert "只读诊断改用 safe_run_bash，不会消耗该步骤" in description
    assert "不要把诊断绑定到该步骤上，否则会把步骤记成已完成" in description


@pytest.mark.parametrize(
    ("warning", "reason", "python_error", "bash_error"),
    [
        (
            "transcript_tail_unterminated",
            "route_transcript_tail_unwritable",
            "transcript 尾部存在未完成 JSONL，继续追加会把步骤开始/结果收据"
            "写进损坏行；真实执行已在物化和启动前拒绝。safe_execute_python "
            "在此状态下也会被拒；若只需只读诊断，请改用 safe_run_bash；"
            "请由 framework owner 对账并修复追加日志。",
            "transcript 尾部存在未完成 JSONL，继续追加会把步骤开始/结果收据"
            "写进损坏行；真实执行已在物化和启动前拒绝。只读诊断仍可继续，"
            "请由 framework owner 对账并修复追加日志。",
        ),
        (
            "transcript_unreadable",
            "route_transcript_unreadable",
            "路线事件历史不可读取或存在身份冲突；真实执行已在物化和启动前"
            "拒绝。safe_execute_python 在此状态下也会被拒；若只需只读诊断，"
            "请改用 safe_run_bash；请由 framework owner 对账。",
            "路线事件历史不可读取或存在身份冲突；真实执行已在物化和启动前"
            "拒绝。只读诊断仍可继续，请由 framework owner 对账。",
        ),
        (
            "duplicate_attempt_identity",
            "route_event_history_invalid",
            "路线事件历史不可读取或存在身份冲突；真实执行已在物化和启动前"
            "拒绝。safe_execute_python 在此状态下也会被拒；若只需只读诊断，"
            "请改用 safe_run_bash；请由 framework owner 对账。",
            "路线事件历史不可读取或存在身份冲突；真实执行已在物化和启动前"
            "拒绝。只读诊断仍可继续，请由 framework owner 对账。",
        ),
    ],
)
def test_damaged_route_history_points_python_to_bash_without_lying_about_access(
    warning,
    reason,
    python_error,
    bash_error,
):
    python_block = execution_route_block(
        _effectful_decision(
            "safe_execute_python",
            decision="invalid_event_history",
            history_warning=warning,
        ),
        phase="pre_materialization",
    )
    bash_block = execution_route_block(
        _effectful_decision(
            "safe_run_bash",
            decision="invalid_event_history",
            history_warning=warning,
        ),
        phase="pre_materialization",
    )

    assert python_block is not None
    assert python_block["reason"] == reason
    assert python_block["error"] == python_error

    assert bash_block is not None
    assert bash_block["reason"] == reason
    assert bash_block["error"] == bash_error


@pytest.mark.parametrize(
    ("tool", "expected_error"),
    [
        (
            "safe_execute_python",
            "路线事件历史无法安全读取或追加，真实动作已在目录物化和"
            "进程/作业启动前拒绝；safe_execute_python 在此状态下也会被拒；"
            "若只需只读诊断，请改用 safe_run_bash。",
        ),
        (
            "safe_run_bash",
            "路线事件历史无法安全读取或追加，真实动作已在目录物化和"
            "进程/作业启动前拒绝；只读诊断仍可继续。",
        ),
    ],
)
def test_enforcement_history_degradation_guidance_matches_actual_tool_access(
    monkeypatch,
    tool,
    expected_error,
):
    monkeypatch.setattr(
        execution_route,
        "_current_blocking_event_history_warning",
        lambda _state: "transcript_unreadable",
    )
    action = {
        "tool": tool,
        "program": "python" if tool == "safe_execute_python" else "echo",
        "read_only": False,
        "observed_effects": ["workspace_write"],
    }
    result = enforce_execution_route(
        SimpleNamespace(),
        action,
        _effectful_decision(tool),
        phase="pre_materialization",
    )

    assert result is not None
    assert result["reason"] == "route_transcript_unreadable"
    assert result["error"] == expected_error


def test_unclassified_python_points_to_bash_for_read_only_diagnosis():
    python_block = execution_route_block(
        _effectful_decision(
            "safe_execute_python",
            scope_status="absent",
            scope_mode=None,
        ),
        phase="pre_materialization",
    )
    bash_block = execution_route_block(
        _effectful_decision(
            "safe_run_bash",
            scope_status="absent",
            scope_mode=None,
        ),
        phase="pre_materialization",
    )

    assert python_block is not None
    assert python_block["reason"] == "experiment_scope_classification_required"
    assert "classify_experiment_scope" in python_block["error"]
    assert "safe_run_bash" in python_block["error"]

    assert bash_block is not None
    assert bash_block["reason"] == "experiment_scope_classification_required"
    assert "classify_experiment_scope" in bash_block["error"]
    assert "safe_run_bash" not in bash_block["error"]

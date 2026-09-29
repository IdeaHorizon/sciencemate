from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

from core.state import State
from core.tool_registry import _REGISTRY
from nodes.experiment.tools import execution_route, resource_manager, safe_bash
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _bind_fixture_scope(state: State) -> None:
    state.hook_state.setdefault("node_inputs", {
        "fixture": "route_shadow_wiring",
        "requested_work": "验证受管路线 shadow、绑定与执行入口接线。",
        "prereg_assignment": {
            "kind": "none",
            "reason": "该夹具仅验证工具到路线执行入口的机械接线。",
        },
    })
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="路线 shadow 测试的真实效果路径必须绑定稳定的上游测试输入。",
    ))
    assert classified["status"] == "success", classified


def _state(tmp_path: Path, *, bind_scope: bool = True) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="route-shadow-project",
    )
    if bind_scope:
        _bind_fixture_scope(state)
    state.hook_state["build_resource_preflight"] = {
        "status": "success",
        "decision": "build_mode_feasible",
        "runtime_resource_policy": "fixed",
        "artifact_id": "build_resource_plan__route-shadow-test",
        "requested_resources": {
            "total_cpus": 2,
            "memory_gb": 2,
            "walltime_minutes": 5,
        },
    }
    return state


def _recording_executor(observed: dict, result: dict | None = None):
    async def fake_exec(
        _state,
        cmd,
        timeout=600,
        cwd=None,
        resource_profile=None,
        *,
        sandbox_profile="bash",
        sandbox_write_targets=None,
        child_env=None,
        sandbox_roots=None,
        execution_action=None,
        execution_decision=None,
        execution_route_binding=None,
    ):
        observed.update(
            cmd=cmd,
            timeout=timeout,
            cwd=cwd,
            resource_profile=resource_profile,
            sandbox_profile=sandbox_profile,
            sandbox_write_targets=sandbox_write_targets,
            child_env=child_env,
            sandbox_roots=sandbox_roots,
            execution_action=execution_action,
            execution_decision=execution_decision,
            execution_route_binding=execution_route_binding,
        )
        return dict(result or {
            "status": "success",
            "returncode": 0,
            "stdout_tail": "ok",
            "stderr_tail": "",
        })

    return fake_exec


def test_safe_run_bash_shadow_observes_without_changing_success(tmp_path, monkeypatch):
    state = _state(tmp_path)
    observed: list[dict] = []
    executor_call: dict = {}
    monkeypatch.setattr(
        execution_route,
        "record_execution_route_shadow",
        lambda _state, action, _decision: observed.append(action),
    )

    monkeypatch.setattr(
        safe_bash, "_exec_and_log", _recording_executor(executor_call),
    )

    result = asyncio.run(safe_bash._safe_run_bash(state, "pwd"))

    assert result["status"] == "success"
    assert observed[0]["tool"] == "safe_run_bash"
    assert observed[0]["program"] == "pwd"
    assert observed[0]["read_only"] is True
    assert executor_call["sandbox_profile"] == "bash"
    assert executor_call["sandbox_write_targets"] == []
    assert executor_call["sandbox_roots"] is not None
    assert executor_call["resource_profile"] is None
    assert executor_call["execution_action"]["tool"] == "safe_run_bash"
    assert executor_call["execution_decision"]["tool"] == "safe_run_bash"


def test_safe_execute_python_shadow_observes_without_changing_success(tmp_path, monkeypatch):
    state = _state(tmp_path)
    observed: list[dict] = []
    executor_call: dict = {}
    monkeypatch.setattr(
        execution_route,
        "record_execution_route_shadow",
        lambda _state, action, _decision: observed.append(action),
    )

    monkeypatch.setattr(
        safe_bash, "_exec_and_log", _recording_executor(executor_call),
    )

    result = asyncio.run(safe_bash._safe_execute_python(state, "print('ok')"))

    assert result["status"] == "success"
    assert observed[0]["tool"] == "safe_execute_python"
    assert observed[0]["program"] == "python"
    assert executor_call["sandbox_profile"] == "python"
    assert executor_call["sandbox_write_targets"] == []
    assert executor_call["sandbox_roots"] is not None
    assert executor_call["execution_action"]["tool"] == "safe_execute_python"
    assert executor_call["execution_decision"]["tool"] == "safe_execute_python"
    assert executor_call["resource_profile"] is None
    assert isinstance(executor_call["child_env"], dict)


def test_submit_job_dry_run_shadow_observes_without_changing_success(tmp_path, monkeypatch):
    state = _state(tmp_path)
    observed: list[dict] = []
    monkeypatch.setattr(
        execution_route,
        "record_execution_route_shadow",
        lambda _state, action, _decision: observed.append(action),
    )

    result = asyncio.run(resource_manager._submit_job(
        state,
        command="echo ok",
        scheduler="local",
        dry_run=True,
        stage="diagnostic",
    ))

    assert result["status"] == "success"
    assert observed[0]["tool"] == "submit_job"
    assert observed[0]["program"] == "echo"
    assert observed[0]["dry_run"] is True


def test_legacy_stage_hint_does_not_choose_cwd_or_resource_strength(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    observed: dict = {}

    monkeypatch.setattr(
        safe_bash, "_exec_and_log", _recording_executor(observed),
    )

    result = asyncio.run(safe_bash._safe_run_bash(
        state,
        "echo ok > result.txt",
        stage="toolchain_build",
    ))

    runtime = state.root / "outputs" / "experiment" / "runtime"
    assert result["status"] == "success", result
    assert observed["cwd"] == str(runtime)
    assert observed["sandbox_profile"] == "bash"
    assert observed["sandbox_write_targets"]
    assert observed["sandbox_roots"] is not None
    assert observed["resource_profile"] is None


def test_legacy_simulation_stage_alone_cannot_create_scientific_identity(tmp_path):
    state = _state(tmp_path)

    assert safe_bash._is_formal_scientific_action(
        state,
        execution_stage="simulation",
        route_decision={"decision": "route_unavailable", "policy": "low_risk_effectful"},
    ) is False
    action = resource_manager._submission_route_action(
        "echo solver",
        stage="simulation",
        dry_run=False,
    )
    assert "scientific_execution" not in action["observed_effects"]


def test_stage_is_runtime_compatibility_only_and_not_llm_visible():
    for tool_name in ("safe_run_bash", "safe_execute_python", "submit_job"):
        definition = _REGISTRY.tools[tool_name]
        properties = (definition.parameters_schema or {}).get("properties") or {}
        assert "stage" not in properties, tool_name


def test_shadow_failure_is_advisory_and_does_not_block_safe_run_bash(tmp_path, monkeypatch):
    state = _state(tmp_path)
    executor_call: dict = {}

    def broken_shadow(_state, _action, _decision):
        raise RuntimeError("shadow-only failure")

    monkeypatch.setattr(
        execution_route,
        "record_execution_route_shadow",
        broken_shadow,
    )

    monkeypatch.setattr(
        safe_bash, "_exec_and_log", _recording_executor(executor_call),
    )

    result = asyncio.run(safe_bash._safe_run_bash(state, "pwd"))

    assert result["status"] == "success"
    assert executor_call["sandbox_profile"] == "bash"
    assert executor_call["sandbox_roots"] is not None
    assert executor_call["resource_profile"] is None


def test_env_and_mutating_git_commands_are_never_route_read_only():
    mutating = [
        "env MAKEFLAGS=-j16 make",
        "env bash -c 'touch result.txt'",
        "git branch -D old",
        "git branch new-branch",
        "git remote set-url origin https://example.invalid/repo.git",
        "git tag v1",
    ]
    for command in mutating:
        assert safe_bash._is_read_only_shell_command(command) is False, command

    assert safe_bash._is_major_build("env MAKEFLAGS=-j16 make") is True
    assert safe_bash._route_observed_program(
        "env MAKEFLAGS=-j16 make") == "make"


def test_ast_route_projection_unwraps_static_shell_structures_once():
    quote = chr(34)
    expected = (
        (f"bash -c {quote}touch /x{quote}", "touch"),
        ("(cd /b && touch x)", "touch"),
        ("f(){ cd /b; touch x; }; f", "touch"),
        ("builtin echo x", "echo"),
        (f"eval {quote}touch /x{quote}", "touch"),
    )
    for command, program in expected:
        projection = safe_bash._project_bash_route(command)
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic")
        assert projection == {
            "program": program,
            "unknown_entry": False,
        }
        assert action["observed_effects"] == ["workspace_write"]


def test_route_projection_keeps_one_payload_and_trusted_pipeline_sidecar():
    assert safe_bash._project_bash_route(
        "make -j2 2>&1 | tee build.log"
    )["program"] == "make"
    assert safe_bash._project_bash_route(
        "./compile em_real 2>&1 | /usr/bin/tee build.log"
    )["program"] == "./compile"
    assert safe_bash._project_bash_route(
        "make | python uploader.py"
    )["program"] == "compound:make+python"


def test_untrusted_pipeline_sidecar_cannot_be_collapsed(
    tmp_path, monkeypatch,
):
    fake_tee = tmp_path / "tee"
    fake_tee.touch()
    fake_tee.chmod(0o755)
    original_which = safe_bash.shutil.which
    monkeypatch.setattr(
        safe_bash.shutil,
        "which",
        lambda name: str(fake_tee) if name == "tee" else original_which(name),
    )

    projection = safe_bash._project_bash_route("make | tee build.log")

    assert projection["unknown_entry"] is True
    assert projection["program"] == "compound:make+tee"


def test_transparent_shell_wrapper_must_resolve_to_trusted_root(tmp_path):
    fake_shell = tmp_path / "bash"
    fake_shell.touch()
    fake_shell.chmod(0o755)
    quote = chr(34)

    projection = safe_bash._project_bash_route(
        f"{fake_shell} -c {quote}touch /x{quote}")

    assert projection["program"] == "touch"
    assert projection["unknown_entry"] is True


def test_same_entrypoint_compound_is_not_collapsed_to_one_route_action():
    assert safe_bash._route_observed_program(
        "cmake -S src -B . && cmake --build ."
    ) == "compound:cmake+cmake"
    assert safe_bash._route_observed_program(
        "bash -c 'cmake -S src -B . && cmake --build .'"
    ) == "compound:cmake+cmake"


def test_git_query_forms_remain_route_read_only():
    queries = [
        "git status --short",
        "git branch --list 'release-*'",
        "git remote -v",
        "git remote get-url origin",
        "git tag --list 'v*'",
        "git submodule status",
    ]
    for command in queries:
        assert safe_bash._is_read_only_shell_command(command) is True, command


def test_strict_capability_probe_never_binds_an_execution_step():
    probes = [
        "make --version",
        "cmake --version",
        "ninja --help",
        "gcc --version",
        "curl --version",
        "git --version",
    ]
    for command in probes:
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic")
        assert action["read_only"] is True, command
        assert action["observed_effects"] == [], command

    # 这些形式会读取/展开项目规则，不能因名字像 probe 就绕过路线。
    assert safe_bash._is_read_only_shell_command("make -n") is False
    assert safe_bash._is_read_only_shell_command("make help") is False


def _fake_probe_tool(directory: Path, name: str,
                     body: str = "echo 4.9.2") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def test_capability_probe_trusts_toolchain_entry_run_cannot_influence(
        tmp_path, monkeypatch):
    """conda/spack 场景：入口不在本 run 可写根内 → 只读探测豁免 + 身份钉住。"""
    state = _state(tmp_path)
    toolbox = tmp_path / "toolbox"
    tool = _fake_probe_tool(toolbox, "nc-config")
    monkeypatch.setenv("PATH", f"{toolbox}:/usr/bin:/bin")

    action = safe_bash._bash_route_action(
        "nc-config --version", execution_stage="diagnostic", state=state)
    assert action["read_only"] is True
    assert action["observed_effects"] == []

    pin = state.hook_state[safe_bash._ROUTE_ENTRY_IDENTITY_KEY]["nc-config"]
    assert pin["revoked"] is False
    assert pin["identity"]["realpath"] == str(tool.resolve())
    events = [
        json.loads(line) for line in
        state.transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event["event"] == "route_entry_identity_pinned"
        and event["program"] == "nc-config"
        for event in events
    )


def test_probe_entry_inside_run_writable_root_is_rejected(
        tmp_path, monkeypatch):
    """绝不放宽：入口落在本 run 可写根内 → 无只读豁免，仍是未知入口。"""
    state = _state(tmp_path)
    bindir = Path(state.root) / "planted-bin"
    _fake_probe_tool(bindir, "nc-config")
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")

    action = safe_bash._bash_route_action(
        "nc-config --version", execution_stage="diagnostic", state=state)
    assert action["read_only"] is False
    assert "unknown_executable" in action["observed_effects"]
    assert safe_bash._is_read_only_shell_command(
        "nc-config --version", state) is False


def test_identity_override_and_wrapper_never_get_probe_exemption(
        tmp_path, monkeypatch):
    """绝不放宽：身份覆盖前缀与 wrapper 形式都拿不到只读探测豁免。"""
    state = _state(tmp_path)
    toolbox = tmp_path / "toolbox"
    _fake_probe_tool(toolbox, "nc-config")
    monkeypatch.setenv("PATH", f"{toolbox}:/usr/bin:/bin")

    assert safe_bash._is_read_only_shell_command(
        "PATH=/tmp nc-config --version", state) is False
    assert safe_bash._is_read_only_shell_command(
        "env nc-config --version", state) is False
    assert safe_bash._is_read_only_shell_command(
        "bash -c 'nc-config --version'", state) is False


def test_dangerous_semantics_keep_their_classification(tmp_path, monkeypatch):
    """绝不放宽：sbatch/ssh/rm 等语义判定不因入口可信而变成只读。"""
    state = _state(tmp_path)
    toolbox = tmp_path / "toolbox"
    for name in ("sbatch", "ssh"):
        _fake_probe_tool(toolbox, name)
    monkeypatch.setenv("PATH", f"{toolbox}:/usr/bin:/bin")

    for command in ("sbatch job.sh", "ssh host uname", "rm -rf sub"):
        assert safe_bash._is_read_only_shell_command(
            command, state) is False, command
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic", state=state)
        assert action["read_only"] is False, command


def test_trusted_entry_identity_change_revokes_exemption(
        tmp_path, monkeypatch):
    """入口身份钉住：同 run 内同名入口任一属性变化 → 撤销豁免且不恢复。"""
    state = _state(tmp_path)
    toolbox = tmp_path / "toolbox"
    tool = _fake_probe_tool(toolbox, "nc-config")
    monkeypatch.setenv("PATH", f"{toolbox}:/usr/bin:/bin")

    first = safe_bash._bash_route_action(
        "nc-config --version", execution_stage="diagnostic", state=state)
    assert first["read_only"] is True

    original = tool.read_text(encoding="utf-8")
    tool.write_text(f"{original}echo tampered payload\n", encoding="utf-8")
    tool.chmod(0o755)

    second = safe_bash._bash_route_action(
        "nc-config --version", execution_stage="diagnostic", state=state)
    assert second["read_only"] is False
    pin = state.hook_state[safe_bash._ROUTE_ENTRY_IDENTITY_KEY]["nc-config"]
    assert pin["revoked"] is True
    events = [
        json.loads(line) for line in
        state.transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event["event"] == "route_entry_identity_revoked"
        and event["program"] == "nc-config"
        for event in events
    )

    # 改回原内容也不恢复：撤销是本 run 内的保守终态。
    tool.write_text(original, encoding="utf-8")
    tool.chmod(0o755)
    third = safe_bash._bash_route_action(
        "nc-config --version", execution_stage="diagnostic", state=state)
    assert third["read_only"] is False


def test_missing_probe_entry_requires_uninfluenced_path_lookup(
        tmp_path, monkeypatch):
    """解析不到的裸名探测只在 PATH 查找不受本 run 可写根影响时豁免。"""
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    action = safe_bash._bash_route_action(
        "hf-not-installed-tool --version",
        execution_stage="diagnostic", state=state)
    assert action["read_only"] is True

    bindir = Path(state.root) / "planted-bin"
    bindir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    influenced = safe_bash._bash_route_action(
        "hf-other-missing-tool --version",
        execution_stage="diagnostic", state=state)
    assert influenced["read_only"] is False


def test_absent_path_entry_probe_stays_reviewable_without_route(
        tmp_path, monkeypatch):
    """自证闭环解锁：宿主机上不存在的绝对入口，纯只读探测不需要冻结路线。

    活体来源：镜像自带 ``/usr/local/bin/python3.12`` 在宿主机上不存在，
    ``strict`` resolve 失败 → 入口判不可信 → 只读探测被 execution_route_required
    拦死，而它恰恰是复核"目标到底在不在"这一前提所需的手段。
    """
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    absent = tmp_path / "image-root" / "usr" / "local" / "bin" / "python3.12"
    assert not absent.exists()

    for command in (
        f"{absent} --version",
        f"{absent} --help",
        f"{absent} -V",
    ):
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic", state=state)
        assert action["read_only"] is True, command
        assert action["observed_effects"] == [], command

    # 只读命令作用于路径实参：入口是裸名 ls/stat/file，早已由可信系统根快路径
    # 放行，本改动前后都免路线（这里钉住"没被本次收窄改坏"）。
    for command in (
        f"ls -la {absent}",
        f"stat {absent}",
        f"file {absent}",
    ):
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic", state=state)
        assert action["read_only"] is True, command
        assert action["observed_effects"] == [], command


def test_absent_path_entry_probe_unblocks_the_compound_ls_then_probe_form(
        tmp_path, monkeypatch):
    """活体证据里 ``ls`` 那一半的实际成因：与绝对入口探测同属一条复合命令。

    单发的 ``ls -la <绝对路径>`` 入口是裸名 ls，在本改动前就已免路线；真正
    被拦的是 ``ls -la <路径> && <路径> --version`` 这类复合形态——只读判定
    要求**每个** segment 的入口都可信，绝对入口那一段不可信就把整条命令拖成
    真实执行动作。本改动让该 segment 通过后，整条复核命令才免路线。
    """
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    absent = tmp_path / "image-root" / "usr" / "local" / "bin" / "python3.12"

    for command in (
        f"ls -la {absent} && {absent} --version",
        f"ls -la {absent}; {absent} --version",
    ):
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic", state=state)
        assert action["read_only"] is True, command
        assert action["observed_effects"] == [], command

    # 复合形态里只要掺进真实执行段，整条仍必须匹配路线。
    blocked = safe_bash._bash_route_action(
        f"ls -la {absent} && {absent} script.py",
        execution_stage="diagnostic", state=state)
    assert blocked["read_only"] is False
    assert "unknown_executable" in blocked["observed_effects"]


def test_absent_path_entry_exemption_never_covers_glob_entries(
        tmp_path, monkeypatch):
    """不变量：含未展开元字符的入口不进新豁免——shell 会把它展开回可写根。

    ``_absent_path_entry_outside_run_writable_roots`` 按**字面串**与可写根
    比对，而 payload 最终交给 ``/bin/bash -c``：``<runs>/*/bin/ls`` 字面上不含
    run root（``_path_inside_roots`` 恒 False），展开后却正是本 run 自己写进
    可写根的可执行文件。
    """
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    run_root = Path(state.root)
    runs_root = run_root.parent
    planted_bin = run_root / "bin"
    for name in ("ls", "python3.12", "solver"):
        _fake_probe_tool(planted_bin, name, body="echo owned")
    assert sorted(
        path.name for path in planted_bin.glob("*")
    ) == ["ls", "python3.12", "solver"]

    for command in (
        f"{runs_root}/*/bin/ls -la /etc",
        f"{runs_root}/*/bin/python3.12 --version",
        f"{runs_root}/?*/bin/solver --version",
        f"{runs_root}/[a-z]*/bin/solver --version",
        f"{runs_root}/{{a,b}}/bin/solver --version",
        f"{runs_root}/~x/bin/solver --version",
        f"{runs_root}/$RUN/bin/solver --version",
    ):
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic", state=state)
        assert action["read_only"] is False, command
        assert "unknown_executable" in action["observed_effects"], command

    # 同样落在 run 可写根内的字面路径也仍然不豁免（谓词本身没被绕开）。
    assert safe_bash._is_read_only_shell_command(
        f"{planted_bin}/python3.12 --version", state) is False


def test_absent_path_entry_exemption_requires_capability_flag_argv(
        tmp_path, monkeypatch):
    """不变量：豁免只认"参数恰为单个 capability flag"，不认入口 basename。

    否则"解析不到的绝对入口 + 任意 argv"只要 basename 叫 ls/cat/grep/… 就被
    判只读——那是把 argv 形状门整个让掉。
    """
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    image_bin = tmp_path / "image-root" / "opt" / "image" / "bin"

    for command in (
        f"{image_bin}/ls -la /etc",
        f"{image_bin}/grep -r secret /etc",
        f"{image_bin}/cat /etc/passwd",
        f"{image_bin}/which python",
        f"{image_bin}/test -f /etc/passwd",
        f"{image_bin}/uname -a",
    ):
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic", state=state)
        assert action["read_only"] is False, command
        assert "unknown_executable" in action["observed_effects"], command

    # 同一入口换成单个 capability flag 才免路线。
    assert safe_bash._is_read_only_shell_command(
        f"{image_bin}/ls --version", state) is True


def test_absent_path_entry_exemption_never_covers_real_execution(
        tmp_path, monkeypatch):
    """不变量：真实执行形态拿不到"入口解析不到"的只读豁免。"""
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    absent = tmp_path / "image-root" / "usr" / "local" / "bin" / "python3.12"

    for command in (
        f"{absent}",                        # 无 flag，真实执行
        f"{absent} -c 'print(1)'",          # 带参数，真实执行
        f"{absent} script.py",
        f"{absent} --version > out.txt",    # 重定向副作用
        f"{absent} --version | tee log",    # 管道副作用
        f"{absent} --version && make",      # 复合动作
    ):
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic", state=state)
        assert action["read_only"] is False, command
        assert "unknown_executable" in action["observed_effects"], command


def test_absent_path_entry_inside_run_writable_root_is_never_exempt(
        tmp_path, monkeypatch):
    """不变量：run 可写根内的入口即使当前不存在也不豁免（本 run 能造出它）。"""
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    planted = Path(state.root) / "planted-bin" / "python3.12"
    assert not planted.exists()

    action = safe_bash._bash_route_action(
        f"{planted} --version", execution_stage="diagnostic", state=state)
    assert action["read_only"] is False
    assert "unknown_executable" in action["observed_effects"]
    assert safe_bash._is_read_only_shell_command(
        f"{planted} --version", state) is False

    # 相对路径入口按 cwd 解析（通常就是可写根），一律不豁免。
    assert safe_bash._is_read_only_shell_command(
        "./hf-absent-solver --version", state) is False


def test_absent_path_entry_wrapper_and_identity_override_stay_rejected(
        tmp_path, monkeypatch):
    """不变量：wrapper 与身份覆盖形态拿不到绝对路径缺失入口的只读豁免。"""
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    absent = tmp_path / "image-root" / "usr" / "local" / "bin" / "python3.12"

    for command in (
        f"env {absent} --version",
        f"bash -c '{absent} --version'",
        f"PATH=/tmp {absent} --version",
        f"LD_PRELOAD=/tmp/x.so {absent} --version",
    ):
        assert safe_bash._is_read_only_shell_command(
            command, state) is False, command


def test_absent_path_entry_probe_pins_missing_identity(tmp_path, monkeypatch):
    """身份钉住按调用路径分键：记 missing，同一路径上入口一出现即撤销豁免。"""
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    toolbox = tmp_path / "image-root" / "usr" / "local" / "bin"
    absent = toolbox / "hf-probe-tool"
    pin_key = f"path:{absent}"

    assert safe_bash._bash_route_action(
        f"{absent} --version", execution_stage="diagnostic",
        state=state)["read_only"] is True
    pins = state.hook_state[safe_bash._ROUTE_ENTRY_IDENTITY_KEY]
    assert pin_key in pins
    assert "hf-probe-tool" not in pins  # 绝对路径入口不占用裸名键
    assert pins[pin_key]["identity"] == {"realpath": None, "missing": True}
    assert pins[pin_key]["revoked"] is False

    _fake_probe_tool(toolbox, "hf-probe-tool")
    assert safe_bash._bash_route_action(
        f"{absent} --version", execution_stage="diagnostic",
        state=state)["read_only"] is False
    assert state.hook_state[
        safe_bash._ROUTE_ENTRY_IDENTITY_KEY][pin_key]["revoked"] is True


def test_absent_path_entry_pin_never_revokes_a_same_basename_real_entry(
        tmp_path, monkeypatch):
    """不变量：缺失绝对入口的身份记录不得污染同名真实入口的记录。

    pin 键若只用 basename，``/usr/local/bin/python3.12``（缺失）与 conda
    ``python3.12``（真实）会互相撤销对方的豁免，且 ``revoked`` 永不复位——
    本 run 内该程序名的 capability 探测全部失效，正是本轮要解开的那类闭环。
    """
    toolbox = tmp_path / "toolbox"
    real = _fake_probe_tool(toolbox, "python3.12", body="echo 3.12.1")
    absent = tmp_path / "image-root" / "usr" / "local" / "bin" / "python3.12"
    monkeypatch.setenv("PATH", f"{toolbox}:/usr/bin:/bin")

    def probe(state, command):
        return safe_bash._bash_route_action(
            command, execution_stage="diagnostic", state=state)["read_only"]

    # 顺序 A：真实绝对入口 → 缺失绝对入口 → 裸名，三者互不影响。
    forward = _state(tmp_path / "fwd")
    assert probe(forward, f"{real} --version") is True
    assert probe(forward, f"{absent} --version") is True
    assert probe(forward, "python3.12 --version") is True

    # 顺序 B：缺失绝对入口在先，同样不撤销随后的真实入口。
    reverse = _state(tmp_path / "rev")
    assert probe(reverse, f"{absent} --version") is True
    assert probe(reverse, "python3.12 --version") is True
    assert probe(reverse, f"{real} --version") is True

    for state in (forward, reverse):
        pins = state.hook_state[safe_bash._ROUTE_ENTRY_IDENTITY_KEY]
        assert all(
            pin["revoked"] is False for pin in pins.values()
        ), pins
        assert pins["python3.12"]["identity"]["realpath"] == str(real)
        assert pins[f"path:{absent}"]["identity"] == {
            "realpath": None, "missing": True}


def test_absent_path_entry_probe_clears_the_route_gate_end_to_end(
        tmp_path, monkeypatch):
    """接到活体链路上：已冻结 v2 路线下，探测拿到 route_not_required 并放行。

    投影层断言（``_bash_route_action``）只覆盖 read_only 标签；这里把同一个
    action 交给 ``resolve_execution_context`` / ``enforce_execution_route``，
    钉住短路真的发生在路线门上，且真实执行形态照旧被 execution_route_required
    拦下。
    """
    state = _state(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="make",
            role="build_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"

    absent = tmp_path / "image-root" / "usr" / "local" / "bin" / "python3.12"
    probe = safe_bash._bash_route_action(
        f"{absent} --version", execution_stage="diagnostic", state=state)
    probe_action = {
        "tool": "safe_run_bash",
        "program": probe["program"],
        "read_only": probe["read_only"],
        "observed_effects": probe["observed_effects"],
        "workdir_roles": ["managed_source_root"],
        "dry_run": False,
    }
    decision = execution_route.resolve_execution_context(state, probe_action)
    assert decision["decision"] == "route_not_required"
    assert decision["policy"] == "read_only"
    assert execution_route.enforce_execution_route(
        state, probe_action, decision, phase="pre_materialization") is None

    executed = safe_bash._bash_route_action(
        f"{absent} -c 'print(1)'", execution_stage="diagnostic", state=state)
    exec_action = dict(probe_action, program=executed["program"],
                       read_only=executed["read_only"],
                       observed_effects=executed["observed_effects"])
    exec_decision = execution_route.resolve_execution_context(
        state, exec_action)
    assert exec_decision["decision"] != "route_not_required"
    blocked = execution_route.enforce_execution_route(
        state, exec_action, exec_decision, phase="pre_materialization")
    assert blocked is not None
    assert blocked["reason"] == "execution_route_required"


# 活体原文：/var/tmp/hf-review/evidence/airsea/*/transcript.jsonl 里被
# execution_route_blocked（resolver_decision=route_action_mismatch，
# resolver_policy=guarded_unknown_effect）拦下的 python3.12 复核命令。六条
# **全部**带 `2>&1` / `2>/dev/null` / `| head` / `| grep`，因此在收窄"只读探测"
# 判据之前，一律先被"整条命令不得含任何重定向或管道"这一关判成非只读，
# 440acfa0 的路径形态入口豁免根本轮不到生效。禁止改写成简化形态。
_LIVE_READ_ONLY_PROBES = [
    'ls -la /usr/local/bin/python3.12 /home/lujy/miniconda3/bin/python 2>&1;'
    ' /usr/local/bin/python3.12 --version 2>&1;'
    ' /home/lujy/miniconda3/bin/python --version 2>&1',
    'ls -la /usr/local/bin/python3* 2>&1; echo "---";'
    ' which python3.12 python3 2>&1; echo "---";'
    ' /usr/local/bin/python3.12 --version 2>&1; echo "---";'
    ' ls -la /usr/bin/python3* 2>&1',
    'ls -la /usr/local/bin/python3.12 2>&1; echo "---";'
    ' ls -la /usr/local/bin/python3* 2>&1; echo "---";'
    ' which python3.12 python3 2>&1; echo "---";'
    ' /usr/local/bin/python3.12 --version 2>&1',
    'ls -la /usr/local/bin/ | grep -i python; echo "---";'
    ' which python3 python3.12 python 2>&1; echo "---";'
    ' /usr/bin/python3 --version 2>&1',
    'echo "=== python3.12 ===" && ls -la /usr/local/bin/python3.12 2>/dev/null;'
    ' echo "=== runtime/acquired ===" && ls -la /home/lujy/.harness-framework'
    '/projects/airsea-paper-local/workspace/experiment/runtime/acquired/'
    ' 2>/dev/null; echo "=== runtime ===" && ls -la /home/lujy/'
    '.harness-framework/projects/airsea-paper-local/workspace/experiment'
    '/runtime/ 2>/dev/null | head -60',
    'echo "=== check features.csv / match_log.json exist ===";'
    ' ls -la /home/lujy/.harness-framework/projects/airsea-paper-local'
    '/workspace/experiment/runtime/features.csv /home/lujy/.harness-framework'
    '/projects/airsea-paper-local/workspace/experiment/runtime/match_log.json'
    ' 2>&1; echo "=== python3.12 check ===";'
    ' ls -la /usr/local/bin/python3.12 2>&1',
]

# 同一批活体记录里真正执行 python 的命令：必须**继续**被路线门要求。
_LIVE_EXECUTING_COMMANDS = [
    '/usr/local/bin/python3.12 -c "import sys; print(sys.version)";'
    ' echo "---sklearn---";'
    ' /usr/local/bin/python3.12 -c "import sklearn" 2>&1 | head -5',
    '/usr/local/bin/python3.12 -c "import numpy" > runtime/env_probe.json'
    ' 2>&1; echo "exit=$?" >> runtime/env_probe.json;'
    ' cat runtime/env_probe.json',
    '/usr/local/bin/python3.12 /home/lujy/.harness-framework/projects'
    '/airsea-paper-local/workspace/experiment/runtime/probe_nc.py'
    ' > /home/lujy/.harness-framework/projects/airsea-paper-local/workspace'
    '/experiment/runtime/probe_out.txt 2>&1',
]


def test_live_blocked_probe_transcript_commands_are_route_read_only(
        monkeypatch):
    """活体六条复核命令逐条恢复只读裁决，程序投影与活体事件逐字一致。"""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for command in _LIVE_READ_ONLY_PROBES:
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic")
        assert action["read_only"] is True, command
        assert action["observed_effects"] == [], command

    # 两条活体事件记下了 program 字段，钉住"走的是同一条投影链"。
    assert safe_bash._bash_route_action(
        _LIVE_READ_ONLY_PROBES[0], execution_stage="diagnostic",
    )["program"] == (
        "compound:ls+/usr/local/bin/python3.12"
        "+/home/lujy/miniconda3/bin/python")
    assert safe_bash._bash_route_action(
        _LIVE_READ_ONLY_PROBES[1], execution_stage="diagnostic",
    )["program"] == (
        "compound:ls+echo+which+echo+/usr/local/bin/python3.12+echo+ls")

    for command in _LIVE_EXECUTING_COMMANDS:
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic")
        assert action["read_only"] is False, command
        assert "workspace_write" in action["observed_effects"], command


def test_live_probe_command_clears_the_route_gate_end_to_end(
        tmp_path, monkeypatch):
    """把活体原文接到路线门上：route_not_required 且 enforce 放行。"""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="make",
            role="build_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"

    probe = safe_bash._bash_route_action(
        _LIVE_READ_ONLY_PROBES[0], execution_stage="diagnostic", state=state)
    probe_action = {
        "tool": "safe_run_bash",
        "program": probe["program"],
        "read_only": probe["read_only"],
        "observed_effects": probe["observed_effects"],
        "workdir_roles": ["managed_source_root"],
        "dry_run": False,
    }
    decision = execution_route.resolve_execution_context(state, probe_action)
    assert decision["decision"] == "route_not_required"
    assert decision["policy"] == "read_only"
    assert execution_route.enforce_execution_route(
        state, probe_action, decision, phase="pre_materialization") is None

    executed = safe_bash._bash_route_action(
        _LIVE_EXECUTING_COMMANDS[0], execution_stage="diagnostic", state=state)
    exec_action = dict(probe_action, program=executed["program"],
                       read_only=executed["read_only"],
                       observed_effects=executed["observed_effects"])
    exec_decision = execution_route.resolve_execution_context(
        state, exec_action)
    assert exec_decision["decision"] != "route_not_required"
    blocked = execution_route.enforce_execution_route(
        state, exec_action, exec_decision, phase="pre_materialization")
    assert blocked is not None
    assert blocked["reason"] == "execution_route_required"


def test_benign_stderr_redirection_never_widens_to_write_effects(monkeypatch):
    """只放行纯 stderr 合流/丢弃与只读管道；写副作用形态一律仍非只读。"""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    allowed = [
        "ls -la /usr/local/bin/python3.12 2>&1",
        "ls -la /usr/local/bin/python3.12 2>/dev/null",
        "ls -la /usr/local/bin/python3.12 2>&-",
        "ls -la /usr/local/bin/python3.12 2>>/dev/null",
        "ls -la /usr/local/bin 2>&1 | head -5",
        "ls -la /usr/local/bin | grep -i python",
        "cat /etc/hostname | wc -l",
        "/usr/local/bin/python3.12 --version 2>&1",
    ]
    for command in allowed:
        assert safe_bash._is_read_only_shell_command(command) is True, command

    rejected = [
        # 写重定向：目标是文件，无论 stdout 还是 stderr。
        "ls -la /x > out.txt",
        "ls -la /x >> out.txt",
        "ls -la /x &> out.txt",
        "ls -la /x 2> err.txt",
        "ls -la /x 2>&1 >> log.txt",
        "ls -la /x 2>/dev/null > out.txt",
        "/usr/local/bin/python3.12 --version 2>&1 > out.txt",
        # 命令替换/进程替换：可含任意副作用。
        "echo $(rm -rf /tmp/x)",
        "ls `rm -rf /tmp/y`",
        "diff <(ls) <(ls)",
        # 管道里只要有一段不是只读，整条就不是只读。
        "ls | tee f",
        "ls -la /x 2>&1 | tee f",
        "ls | xargs rm",
        "ls -la /x 2>&1 | sh",
        "cat f | bash",
        "ls |& cat",
        # 输入重定向/heredoc 不在放行范围。
        "cat < in.txt",
        "ls <<< abc",
        # 真实执行动作仍必须匹配路线。
        "/usr/local/bin/python3.12 2>&1",
        "/usr/local/bin/python3.12 -c \"print(1)\" 2>&1",
        "/usr/local/bin/python3.12 script.py 2>&1",
        # wrapper / 身份覆盖不吃只读豁免。
        "env /usr/local/bin/python3.12 --version 2>&1",
        "bash -c \"/usr/local/bin/python3.12 --version\" 2>&1",
        "PATH=/tmp/evil /usr/local/bin/python3.12 --version 2>&1",
        "LD_PRELOAD=/tmp/x.so /usr/local/bin/python3.12 --version 2>&1",
        # echo/printf 的只读豁免只认可信裸名入口。
        "./echo hi 2>&1",
        "/tmp/echo hi 2>&1",
        "sudo echo hi 2>&1",
    ]
    for command in rejected:
        assert safe_bash._is_read_only_shell_command(command) is False, command


def test_benign_stderr_pattern_needs_both_word_boundaries(monkeypatch):
    """挖掉的必须是**整个** stderr 重定向，不能是写重定向的中间片段。

    无边界的模式会把 ``2>&1``/``2>/dev/null`` 从词中间抠走，剩余串里不再有
    ``>``，于是一条真实创建文件的写重定向被判成只读。两侧形态都在真实 bash
    里验证过会落盘。
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # 左边界：``2`` 粘在前一个词尾上时它不是 fd —— bash 读作词 ``y2`` 加
    # ``>&1results.txt``，``>&`` 后跟非数字即把 stdout+stderr 一起重定向到
    # 文件 ``1results.txt``（实跑确认该文件被创建）。
    assert safe_bash._is_read_only_shell_command(
        "grep x f y2>&1results.txt") is False
    assert safe_bash._is_read_only_shell_command(
        "ls /etc/hostname a2>&1b") is False
    assert safe_bash._is_read_only_shell_command(
        "cat /etc/hostname x2>&1out") is False
    # 右边界：``/dev/null`` 后跟非词字符时目标是 /dev 下的真实文件，不是丢弃。
    assert safe_bash._is_read_only_shell_command(
        "ls -la /usr 2>/dev/null.log") is False
    assert safe_bash._is_read_only_shell_command(
        "ls -la /usr 2>>/dev/null-x") is False
    assert safe_bash._is_read_only_shell_command(
        "ls -la /usr 2>/dev/null,y") is False
    assert safe_bash._is_read_only_shell_command(
        "ls -la /usr 2>/dev/null@z") is False
    # 边界收紧不得动到活体里的正常写法。
    assert safe_bash._is_read_only_shell_command("ls -la /usr 2>&1") is True
    assert safe_bash._is_read_only_shell_command(
        "ls -la /usr 2>/dev/null; echo ok") is True
    assert safe_bash._is_read_only_shell_command(
        "ls -la /usr 2>/dev/null | head -60") is True


def test_background_operator_never_rides_along_as_read_only(monkeypatch):
    """单个 ``&`` 不是切分点：后台段必须让整条命令判非只读。"""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for command in [
        "ls 2>/dev/null & rm -rf /home/lujy/important",
        "ls | head & rm -rf /home/lujy/important",
        "ls 2>&1 & rm -rf /x",
        "ls & rm -rf /x",
        "ls -la /usr &",
    ]:
        assert safe_bash._is_read_only_shell_command(command) is False, command


def test_quoted_shell_metacharacters_are_not_read_as_structure(monkeypatch):
    """引号内的元字符是字面量：既不放行副作用，也不误判管道。"""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert safe_bash._is_read_only_shell_command('echo "a|b"') is True
    assert safe_bash._is_read_only_shell_command('grep -i "2>&1" f') is True
    assert safe_bash._is_read_only_shell_command("echo 'x > y'") is True
    # 双引号里的 $() 照样展开，必须继续按命令替换拒绝。
    assert safe_bash._is_read_only_shell_command('echo "$(rm -rf /tmp/x)"') is False
    # 引号不配对时不能提升为可信只读。
    assert safe_bash._is_read_only_shell_command('ls "unterminated') is False


def test_route_required_refusal_names_reviewable_read_only_forms():
    """判据不成立时，拒绝文案必须指出可复核的替代只读形式与其前提。"""
    blocked = execution_route.execution_route_block({
        "decision": "route_action_mismatch",
        "tool": "safe_run_bash",
        "policy": "guarded_unknown_effect",
        "effective_effects": ["unknown_executable", "workspace_write"],
    })
    assert blocked["reason"] == "execution_route_required"
    message = blocked["error"] if "error" in blocked else json.dumps(
        blocked, ensure_ascii=False)
    # 兜底形式 + 豁免的三条前提 + 别反复重试（同签名会升级为 exhausted）。
    for hint in (
        "--version", "ls -l", "stat ",
        "绝对路径", "可写根", "重复重试",
    ):
        assert hint in message, hint


def test_static_command_lookup_is_route_read_only_but_command_execution_is_not():
    probes = [
        "command -v lmp",
        "command -V lmp_mpi",
        "command -v lmp lmp_serial lmp_mpi lammps",
        "command -v lmp-mpi lmp+mpi lmp@candidate",
    ]
    for command in probes:
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic")
        assert action["read_only"] is True, command
        assert action["observed_effects"] == [], command

    rejected = [
        "command lmp --help",
        "command -p -v lmp",
        "command -v $LMP",
        "PATH=/tmp command -v lmp",
        "command -v lmp >/tmp/probe",
    ]
    for command in rejected:
        assert safe_bash._is_read_only_shell_command(command) is False, command

    # 无写重定向的 printf/echo 只往 stdout 写字节：给探测各段打标题不构成
    # 副作用，因此不再把整条命令拖成真实执行动作（活体探测普遍是这个形状）。
    # 写重定向与命令替换仍单独拒绝，见下方两条。
    assert safe_bash._is_read_only_shell_command(
        "printf solver=; command -v lmp; printf done") is True
    assert safe_bash._is_read_only_shell_command(
        "printf solver=; command -v lmp > /tmp/probe") is False
    assert safe_bash._is_read_only_shell_command(
        "printf $(command -v lmp)") is False


def test_redirection_and_pipeline_are_never_promoted_to_route_read_only():
    commands = [
        "ls >/tmp/x",
        "cat a>/tmp/x",
        "cat a |tee /tmp/x",
        "cat a|tee /tmp/x",
        "cat <input.txt",
    ]
    for command in commands:
        assert safe_bash._is_read_only_shell_command(command) is False, command


def _single_step_route(*, tool: str, program: str, role: str, effects: list[str]) -> dict:
    return {
        "schema_version": 2,
        "goal": "执行一个受管测试步骤",
        "evidence_refs": ["test:route-wiring"],
        "steps": [{
            "id": "execute",
            "goal": "执行并取得受管收据",
            "after": [],
            "action": {"tool": tool, "program": program},
            "effects": effects,
            "workdir_role": role,
            "expected_outputs": [],
        }],
    }


def test_route_selects_build_cwd_and_writes_bound_before_spawn(tmp_path, monkeypatch):
    from shared.lib import dangerous_commands as danger

    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="make",
            role="build_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"
    observed: dict = {}
    submit_signature = inspect.signature(resource_manager._submit_sync)

    def fake_submit(*args, **kwargs):
        call = submit_signature.bind_partial(*args, **kwargs).arguments
        events = [
            json.loads(line)
            for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        observed["bound_before_spawn"] = any(
            event.get("event") == "route_step_bound" for event in events
        )
        observed["cwd"] = call["workdir"]
        observed["guarded"] = (
            call.get("precomputed_build_limits") is not None
        )
        return {
            "status": "success", "scheduler": "local", "dry_run": False,
            "job_name": "experiment_job",
            "job_id": "hf-harness-route-cwd",
            "container_runtime_id": "a" * 64,
            "submission_nonce": "route-cwd",
        }

    monkeypatch.setattr(resource_manager, "_submit_sync", fake_submit)
    danger.set_bypass_mode(True)
    try:
        result = asyncio.run(resource_manager._submit_job(
            state,
            command="make -j2",
            scheduler="local",
            dry_run=False,
        ))
    finally:
        danger.set_bypass_mode(False)

    expected = state.root / "outputs" / "experiment" / "build"
    assert result["status"] == "success"
    assert observed == {
        "bound_before_spawn": True,
        "cwd": str(expected),
        "guarded": True,
    }
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["execute"]["state"] == "in_progress"
    assert snapshot["route_state"] == "in_progress"


def test_torn_transcript_blocks_build_before_directory_or_spawn(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="make",
            role="build_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write(chr(123) + chr(34) + "event" + chr(34) + ":")
    broken_bytes = state.transcript_path.read_bytes()
    spawned = []

    def forbidden(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("torn transcript must block before spawn")

    monkeypatch.setattr(resource_manager, "_submit_sync", forbidden)

    result = asyncio.run(resource_manager._submit_job(
        state, command="make -j2", scheduler="local", dry_run=False,
    ))

    assert result["reason"] == "route_transcript_tail_unwritable"
    assert result["blocker"]["suggested_owner"] == "framework"
    assert spawned == []
    assert not (state.root / "outputs" / "experiment" / "build").exists()
    assert state.transcript_path.read_bytes() == broken_bytes


def test_torn_transcript_blocks_python_before_scratch_or_spawn(
    tmp_path,
    monkeypatch,
):
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
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write(chr(123) + chr(34) + "event" + chr(34) + ":")
    broken_bytes = state.transcript_path.read_bytes()
    spawned = []

    async def forbidden(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("torn transcript must block before Python spawn")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)

    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        "print('must not run')",
        route_step_id="execute",
    ))

    assert result["reason"] == "route_transcript_tail_unwritable"
    assert "safe_run_bash" in result["error"]
    assert "只读诊断仍可继续" not in result["error"]
    assert spawned == []
    runtime = state.root / "outputs" / "experiment" / "runtime"
    assert not runtime.exists()
    assert not (runtime / ".python-scratch").exists()
    assert state.transcript_path.read_bytes() == broken_bytes


def test_torn_transcript_blocks_submit_before_script_intent_or_scheduler(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="echo",
            role="run_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"
    with state.transcript_path.open("a", encoding="utf-8") as stream:
        stream.write(chr(123) + chr(34) + "event" + chr(34) + ":")
    broken_bytes = state.transcript_path.read_bytes()
    submitted = []

    def forbidden(*_args, **_kwargs):
        submitted.append(True)
        raise AssertionError("torn transcript must block before submit")

    monkeypatch.setattr(resource_manager, "_submit_sync", forbidden)
    monkeypatch.setattr(
        "shared.lib.dangerous_commands.bypass_enabled", lambda: True,
    )

    result = asyncio.run(resource_manager._submit_job(
        state,
        command="echo run",
        scheduler="local",
        dry_run=False,
    ))

    assert result["reason"] == "route_transcript_tail_unwritable"
    assert submitted == []
    assert not (state.root / "outputs" / "experiment" / "runtime").exists()
    assert state.list_artifacts("external_submission_intent") == []
    assert state.list_artifacts("job_submission") == []
    assert list(state.root.rglob("*.sh")) == []
    assert state.transcript_path.read_bytes() == broken_bytes


def test_build_logging_pipeline_binds_one_make_route_step(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="make",
            role="build_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"
    result = asyncio.run(resource_manager._submit_job(
        state,
        command="make -j2 2>&1 | tee build.log",
        scheduler="local",
        dry_run=True,
    ))

    assert result["status"] == "success", result
    assert result["command"] == "make -j2 2>&1 | tee build.log"
    assert result["resource_guard"]["timeout_s"] is None
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["execute"]["state"] == "pending"


def test_explicit_canonical_lazy_build_root_is_materialized_before_spawn(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="make",
            role="build_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"
    build_root = state.root / "outputs" / "experiment" / "build"
    assert not build_root.exists()
    result = asyncio.run(resource_manager._submit_job(
        state,
        command="make -j2",
        scheduler="local",
        workdir=str(build_root),
        dry_run=True,
        route_step_id="execute",
    ))

    assert result["status"] == "success", result
    assert build_root.is_dir()


def test_missing_explicit_build_role_is_never_auto_materialized(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path, bind_scope=False)
    build_root = state.root / "caller-owned-build"
    state.hook_state["node_inputs"] = {
        "fixture": "route_shadow_wiring",
        "requested_work": "验证显式 caller-owned build_root 不会被自动物化。",
        "path_roles": {
            "build_root": {
                "path": str(build_root),
                "writable": True,
            },
        },
    }
    _bind_fixture_scope(state)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="make",
            role="build_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"
    result = asyncio.run(resource_manager._submit_job(
        state,
        command="make -j2",
        scheduler="local",
        workdir=str(build_root),
        dry_run=True,
        route_step_id="execute",
    ))

    assert result["reason"] == "required_workdir_unavailable"
    assert not build_root.exists()


def test_major_build_without_route_is_blocked_before_spawn(tmp_path, monkeypatch):
    state = _state(tmp_path)
    spawned = False

    async def forbidden_exec(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success"}

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_exec)
    monkeypatch.setattr(safe_bash, "_build_gate", lambda *_args, **_kwargs: None)

    result = asyncio.run(safe_bash._safe_run_bash(state, "make -j2"))

    assert result["status"] == "error"
    assert result["reason"] == "execution_route_managed_lifecycle_required"
    assert spawned is False


def test_route_process_tree_effect_guards_unrecognized_official_wrapper(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="official-wrapper",
            role="build_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"
    build_root = state.root / "outputs" / "experiment" / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    wrapper = build_root / "official-wrapper"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    observed: dict = {}

    monkeypatch.setattr(
        safe_bash, "_exec_and_log", _recording_executor(observed),
    )
    monkeypatch.setattr(safe_bash, "_build_gate", lambda *_args, **_kwargs: None)

    result = asyncio.run(safe_bash._safe_run_bash(
        state, "./official-wrapper", stage="diagnostic"))

    assert result["status"] == "error"
    assert result["reason"] == "execution_route_required"
    assert safe_bash._is_major_build("./official-wrapper") is False
    assert observed == {}


def test_frozen_v2_make_route_is_not_revalidated_as_legacy_contract(
    tmp_path, monkeypatch,
):
    """v2 已由 canonical resolver 授权后，不能再要求旧 route_type/activities。

    configure-first 工程在 configure 前没有可执行 DAG。旧调用链会因此落入
    ``_build_contract_gate``，再把合法 v2 当成 legacy build contract 校验，
    最终以缺 ``route_type``/``activities`` 拒绝同一个已匹配步骤。
    """
    state = _state(tmp_path)
    source_root = tmp_path / "configure-first-source"
    source_root.mkdir()
    (source_root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\nproject(route_gate)\n",
        encoding="utf-8",
    )
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="make",
            role="build_root",
            effects=["workspace_write", "process_tree", "external_job"],
        ),
    ))
    assert declared["status"] == "success"
    state.save_artifact("platform_profile", "test_platform", "{}")
    state.save_artifact("source_recon", "test_recon", "{}")

    # 组合出真实的 fallback 分支：build gate 开启，但 configure 前 DAG 尚不可得。
    # 这也是分阶段 rollout / benchmark 能出现的合法能力组合。
    monkeypatch.setattr(
        safe_bash,
        "bench_enabled",
        lambda capability: capability in {"build_gate", "recon"},
    )
    monkeypatch.setattr(safe_bash, "_infer_source_path", lambda *_a, **_k: str(source_root))
    monkeypatch.setattr(safe_bash, "_has_matching_source_recon", lambda *_a, **_k: True)
    monkeypatch.setattr(safe_bash, "_latest_build_graph", lambda *_a, **_k: None)

    result = asyncio.run(resource_manager._submit_job(
        state,
        command="make -j2",
        scheduler="local",
        dry_run=True,
    ))

    assert result["status"] == "success", result
    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert not any(event.get("event") == "build_contract_invalid" for event in events)


def test_v2_route_owns_dependencies_even_when_build_graph_is_actionable(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    source_root = tmp_path / "src"
    build_root = tmp_path / "build"
    source_root.mkdir()
    build_root.mkdir()
    for artifact_type in ("platform_profile", "source_recon", "declared_route"):
        state.save_artifact(artifact_type, f"test_{artifact_type}", "{}")
    graph = {
        "status": "extracted", "actionable": True, "dag_id": "cmake-dag",
        "source_root": str(source_root), "build_root": str(build_root),
        "nodes": {
            "lib": {"deps": [], "outputs": [str(build_root / "lib.a")]},
            "app": {"deps": ["lib"], "outputs": [str(build_root / "app")]},
        },
    }
    state.hook_state["provisioned_build_state"] = {
        "dag_id": "cmake-dag",
        "nodes": {
            "lib": {"state": "planned", "deps": [],
                    "blocked_by": [], "outputs": [str(build_root / "lib.a")]},
            "app": {"state": "planned", "deps": ["lib"],
                    "blocked_by": ["lib"], "outputs": [str(build_root / "app")]},
        },
    }
    monkeypatch.setattr(
        safe_bash, "bench_enabled",
        lambda capability: capability in {"build_gate", "recon", "provision_first"},
    )
    monkeypatch.setattr(safe_bash, "_infer_source_path", lambda *_a, **_k: str(source_root))
    monkeypatch.setattr(safe_bash, "_infer_build_root", lambda *_a, **_k: str(build_root))
    monkeypatch.setattr(safe_bash, "_has_matching_source_recon", lambda *_a, **_k: True)
    monkeypatch.setattr(safe_bash, "_latest_build_graph", lambda *_a, **_k: graph)

    result = safe_bash._build_gate(
        state, "cmake --build . --target app", cwd=str(build_root),
        route_decision={
            "decision": "matched_ready_step", "authoritative": True,
            "route_ref": {"artifact_id": "route", "version": 1},
            "route_step_id": "build_app",
        },
    )

    assert result is None


def test_legacy_graph_without_observable_outputs_cannot_hard_block(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    source_root = tmp_path / "legacy-src"
    build_root = tmp_path / "legacy-build"
    source_root.mkdir()
    build_root.mkdir()
    for artifact_type in ("platform_profile", "source_recon"):
        state.save_artifact(artifact_type, f"test_{artifact_type}", "{}")
    graph = {
        "status": "extracted", "actionable": True, "dag_id": "empty-output-dag",
        "source_root": str(source_root), "build_root": str(build_root),
        "nodes": {
            "lib": {"deps": [], "outputs": []},
            "app": {"deps": ["lib"], "outputs": []},
        },
    }
    state.hook_state["provisioned_build_state"] = {
        "dag_id": "empty-output-dag",
        "nodes": {
            "lib": {"state": "planned", "deps": [], "blocked_by": [], "outputs": []},
            "app": {"state": "planned", "deps": ["lib"],
                    "blocked_by": ["lib"], "outputs": []},
        },
    }
    monkeypatch.setattr(
        safe_bash, "bench_enabled",
        lambda capability: capability in {"build_gate", "recon", "provision_first"},
    )
    monkeypatch.setattr(safe_bash, "_infer_source_path", lambda *_a, **_k: str(source_root))
    monkeypatch.setattr(safe_bash, "_infer_build_root", lambda *_a, **_k: str(build_root))
    monkeypatch.setattr(safe_bash, "_has_matching_source_recon", lambda *_a, **_k: True)
    monkeypatch.setattr(safe_bash, "_latest_build_graph", lambda *_a, **_k: graph)

    assert safe_bash._build_gate(
        state, "cmake --build . --target app", cwd=str(build_root),
        route_decision={"decision": "matched_legacy_step", "authoritative": False},
    ) is None


def test_legacy_fallback_route_still_uses_legacy_contract_gate(tmp_path, monkeypatch):
    """兼容路线没有 v2 权威授权，旧 schema 校验不能随 v2 修复一起被删掉。"""
    state = _state(tmp_path)
    for artifact_type in ("platform_profile", "source_recon", "declared_route"):
        state.save_artifact(artifact_type, f"test_{artifact_type}", "{}")
    marker = {
        "status": "error",
        "blocker": {"kind": "invalid_build_contract"},
        "error": "legacy contract rejected",
    }
    monkeypatch.setattr(
        safe_bash,
        "bench_enabled",
        lambda capability: capability in {"build_gate", "recon"},
    )
    monkeypatch.setattr(safe_bash, "_infer_source_path", lambda *_a, **_k: str(tmp_path))
    monkeypatch.setattr(safe_bash, "_has_matching_source_recon", lambda *_a, **_k: True)
    monkeypatch.setattr(safe_bash, "_latest_build_graph", lambda *_a, **_k: None)
    monkeypatch.setattr(safe_bash, "_build_contract_gate", lambda _state: marker)

    result = safe_bash._build_gate(
        state,
        "make -j2",
        route_decision={
            "decision": "matched_legacy_step",
            "authoritative": False,
        },
    )

    assert result is marker


def test_unknown_effectful_driver_uses_strong_bounded_executor(
    tmp_path, monkeypatch,
):
    """未知 effectful 入口仍必须进入同一个有界 RunAttempt executor。"""
    state = _state(tmp_path)
    asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="safe_run_bash",
            program="custom-driver",
            role="run_root",
            effects=["workspace_write"],
        ),
    ))
    run_root = state.root / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True, exist_ok=True)
    driver = run_root / "custom-driver"
    driver.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    driver.chmod(0o755)
    observed: dict = {}

    async def forbidden_builtin(*_args, **_kwargs):
        raise AssertionError("非只读动作不得进入 builtin communicate 分支")

    monkeypatch.setattr(
        safe_bash, "_exec_and_log", _recording_executor(observed),
    )
    monkeypatch.setattr(safe_bash, "_orig_run_bash", forbidden_builtin)
    monkeypatch.setattr(safe_bash, "bench_enabled", lambda _cap: False)

    result = asyncio.run(safe_bash._safe_run_bash(
        state,
        "./custom-driver --emit-fast",
        stage="diagnostic",
    ))

    assert result["status"] == "success", result
    assert observed["sandbox_profile"] == "bash"
    assert observed["sandbox_write_targets"] == []
    assert observed["sandbox_roots"] is not None
    assert observed["resource_profile"] is None
    assert observed["cmd"] == "./custom-driver --emit-fast"


def test_bash_census_failure_does_not_rewrite_physical_route_success(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="safe_run_bash",
            program="custom-driver",
            role="run_root",
            effects=["workspace_write"],
        ),
    ))
    assert declared["status"] == "success"
    run_root = state.root / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True, exist_ok=True)
    driver = run_root / "custom-driver"
    driver.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    driver.chmod(0o755)
    physical = {
        "status": "success",
        "returncode": 0,
        "stdout_tail": "payload succeeded",
        "stderr_tail": "",
    }
    monkeypatch.setattr(
        safe_bash,
        "_exec_and_log",
        _recording_executor({}, {
            "status": "error",
            "error_code": "execution_action_census_persistence_failed",
            "execution_action_phase": "spawn_observation",
            "safe_to_retry": False,
            "execution_outcome": physical,
        }),
    )
    monkeypatch.setattr(safe_bash, "bench_enabled", lambda _cap: False)

    result = asyncio.run(safe_bash._safe_run_bash(
        state, "./custom-driver --emit-fast", route_step_id="execute"))

    assert result["status"] == "error"
    assert result["execution_outcome"]["status"] == "success"
    assert result["execution_outcome"]["route_attempt"]["outcome"] == "success"
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["execute"]["state"] == "verified"
    assert snapshot["route_state"] == "complete"


def test_bash_route_receipt_failure_preserves_physical_outcome(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="safe_run_bash",
            program="custom-driver",
            role="run_root",
            effects=["workspace_write"],
        ),
    ))
    assert declared["status"] == "success"
    run_root = state.root / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True, exist_ok=True)
    driver = run_root / "custom-driver"
    driver.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    driver.chmod(0o755)
    physical = {
        "status": "success",
        "returncode": 0,
        "stdout_tail": "payload succeeded",
        "stderr_tail": "",
    }
    monkeypatch.setattr(
        safe_bash, "_exec_and_log", _recording_executor({}, physical),
    )
    monkeypatch.setattr(safe_bash, "bench_enabled", lambda _cap: False)
    monkeypatch.setattr(
        execution_route,
        "finish_route_step_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("route receipt unavailable")
        ),
    )

    result = asyncio.run(safe_bash._safe_run_bash(
        state, "./custom-driver --emit-fast", route_step_id="execute"))

    assert result["status"] == "error"
    assert result["reason"] == "route_outcome_persistence_failed"
    assert result["execution_outcome"]["status"] == "success"
    assert result["execution_outcome"]["stdout_tail"] == "payload succeeded"
    assert result["do_not_repeat_action"] is True
    assert result["safe_to_retry"] is False
    assert result["route_attempt_binding"]["route_step_id"] == "execute"
    assert result["route_attempt_binding"]["attempt_id"].startswith("route-")


def test_missing_route_output_is_reported_as_action_failure(tmp_path, monkeypatch):
    state = _state(tmp_path)
    route = _single_step_route(
        tool="safe_run_bash",
        program="produce-result",
        role="run_root",
        effects=["workspace_write"],
    )
    route["steps"][0]["expected_outputs"] = ["result.dat"]
    asyncio.run(execution_route._declare_execution_route(state, route=route))
    run_root = state.root / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True, exist_ok=True)
    entry = run_root / "produce-result"
    entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    entry.chmod(0o755)

    async def fake_exec(*_args, **_kwargs):
        return {
            "status": "success",
            "returncode": 0,
            "stdout_tail": "PROGRAM_OK\n",
            "stderr_tail": "",
            "log_path": "/managed/logs/run.log",
            "log_sha256": "a" * 64,
        }

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)

    result = asyncio.run(safe_bash._safe_run_bash(
        state, "./produce-result", stage="diagnostic"))

    assert result["status"] == "error"
    assert result["reason"] == "route_expected_outputs_missing"
    assert result["execution_receipt"]["returncode"] == 0
    assert result["execution_receipt"]["stdout_tail"] == "PROGRAM_OK\n"
    assert result["execution_receipt"]["log_sha256"] == "a" * 64
    attempt = result["route_attempt"]
    assert attempt["attempt_id"].startswith("route-")
    assert attempt["route_step_id"] == "execute"
    assert attempt["failure_class"] == "expected_outputs_missing"
    recovery = result["recovery_context"]
    assert recovery["attempt_id"] == attempt["attempt_id"]
    assert recovery["suggested_failure_class"] == "expected_output"
    assert recovery["recovery_basis_template"]["attempt_id"] == (
        attempt["attempt_id"])


def test_route_binding_write_failure_blocks_before_spawn(tmp_path, monkeypatch):
    state = _state(tmp_path)
    asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="safe_execute_python",
            program="python",
            role="run_root",
            effects=["workspace_write"],
        ),
    ))
    original_append = state.append_transcript
    spawned = False

    def fail_binding(event, **payload):
        if event == "route_step_bound":
            raise OSError("disk unavailable")
        return original_append(event, **payload)

    async def forbidden_python(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success"}

    monkeypatch.setattr(state, "append_transcript", fail_binding)
    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_python)

    result = asyncio.run(safe_bash._safe_execute_python(
        state, "print('must not spawn')", route_step_id="execute"))

    assert result["reason"] == "route_binding_persistence_failed"
    assert spawned is False


def test_safe_execute_python_writes_managed_route_receipt(tmp_path, monkeypatch):
    state = _state(tmp_path)
    executor_call: dict = {}
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

    monkeypatch.setattr(
        safe_bash, "_exec_and_log", _recording_executor(executor_call),
    )

    result = asyncio.run(safe_bash._safe_execute_python(
        state, "print('route-python-ok')", route_step_id="execute"))

    assert result["status"] == "success"
    assert result["route_attempt"]["outcome"] == "success"
    assert result["route_attempt"]["receipt_persisted"] is True
    assert executor_call["sandbox_profile"] == "python"
    assert executor_call["sandbox_write_targets"] == []
    assert executor_call["sandbox_roots"] is not None
    assert executor_call["resource_profile"] is None
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["execute"]["state"] == "verified"
    assert snapshot["route_state"] == "complete"


def test_python_census_failure_does_not_rewrite_physical_route_success(
    tmp_path, monkeypatch,
):
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
    physical = {
        "status": "success",
        "returncode": 0,
        "stdout_tail": "payload succeeded",
        "stderr_tail": "",
    }
    monkeypatch.setattr(
        safe_bash,
        "_exec_and_log",
        _recording_executor({}, {
            "status": "error",
            "error_code": "execution_action_census_persistence_failed",
            "execution_action_phase": "terminal",
            "safe_to_retry": False,
            "execution_outcome": physical,
        }),
    )

    result = asyncio.run(safe_bash._safe_execute_python(
        state, "print('route-python-ok')", route_step_id="execute"))

    assert result["status"] == "error"
    assert result["execution_outcome"]["status"] == "success"
    assert result["execution_outcome"]["route_attempt"]["outcome"] == "success"
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["execute"]["state"] == "verified"
    assert snapshot["route_state"] == "complete"


def test_python_route_receipt_failure_preserves_physical_outcome(
    tmp_path, monkeypatch,
):
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
    physical = {
        "status": "success",
        "returncode": 0,
        "stdout_tail": "payload succeeded",
        "stderr_tail": "",
    }
    monkeypatch.setattr(
        safe_bash, "_exec_and_log", _recording_executor({}, physical),
    )
    monkeypatch.setattr(
        execution_route,
        "finish_route_step_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("route receipt unavailable")
        ),
    )

    result = asyncio.run(safe_bash._safe_execute_python(
        state, "print('route-python-ok')", route_step_id="execute"))

    assert result["status"] == "error"
    assert result["reason"] == "route_outcome_persistence_failed"
    assert result["execution_outcome"]["status"] == "success"
    assert result["execution_outcome"]["stdout_tail"] == "payload succeeded"
    assert result["do_not_repeat_action"] is True
    assert result["safe_to_retry"] is False
    assert result["route_attempt_binding"]["route_step_id"] == "execute"
    assert result["route_attempt_binding"]["attempt_id"].startswith("route-")


def test_unbound_python_cannot_bypass_a_declared_python_step(tmp_path, monkeypatch):
    state = _state(tmp_path)
    spawned = False

    async def forbidden_python(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return {"status": "success"}

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_python)
    asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="safe_execute_python",
            program="python",
            role="run_root",
            effects=["workspace_write"],
        ),
    ))

    result = asyncio.run(safe_bash._safe_execute_python(
        state, "print('diagnostic-only')"))

    assert result["status"] == "error"
    assert result["reason"] == "execution_route_step_id_required"
    assert spawned is False
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["execute"]["state"] == "pending"
    assert not any(
        json.loads(line).get("event") == "route_step_bound"
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def test_real_submit_without_route_is_blocked_before_scheduler_call(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    submitted = False

    def forbidden_submit(*_args, **_kwargs):
        nonlocal submitted
        submitted = True
        return {"status": "submitted", "job_id": "must-not-exist"}

    monkeypatch.setattr(resource_manager, "_submit_sync", forbidden_submit)

    result = asyncio.run(resource_manager._submit_job(
        state,
        command="echo should-not-submit",
        scheduler="local",
        dry_run=False,
        stage="diagnostic",
    ))

    assert result["status"] == "error"
    assert result["reason"] == "execution_route_required"
    assert submitted is False


def test_submit_job_dry_run_never_completes_a_route_step(tmp_path):
    state = _state(tmp_path)
    asyncio.run(execution_route._declare_execution_route(
        state,
        route=_single_step_route(
            tool="submit_job",
            program="echo",
            role="run_root",
            effects=["workspace_write", "external_job"],
        ),
    ))

    result = asyncio.run(resource_manager._submit_job(
        state,
        command="echo ok",
        scheduler="local",
        dry_run=True,
        stage="diagnostic",
    ))

    assert result["status"] == "success"
    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["execute"]["state"] == "pending"


def _trust_control_plane_entries(monkeypatch):
    original_which = safe_bash.shutil.which
    trusted = {"kubectl", "docker", "podman", "ssh", "pdsh"}
    monkeypatch.setattr(
        safe_bash.shutil,
        "which",
        lambda name: (
            "/usr/bin/true"
            if Path(str(name)).name in trusted
            else original_which(name)
        ),
    )


def test_control_plane_queries_are_route_read_only_and_known(monkeypatch):
    _trust_control_plane_entries(monkeypatch)
    commands = (
        "kubectl get pods",
        "kubectl auth can-i create pods",
        "kubectl config current-context",
        "docker ps",
        "ssh -V",
        "pdsh -V",
    )

    for command in commands:
        projection = safe_bash._project_bash_route(command)
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic")

        assert projection["unknown_entry"] is False, command
        assert action["read_only"] is True, command
        assert action["observed_effects"] == [], command


def test_external_control_actions_are_known_but_owned_as_external_jobs(
    monkeypatch,
):
    _trust_control_plane_entries(monkeypatch)
    commands = (
        "kubectl exec pod/solver -- ./solver",
        "kubectl proxy",
        "ssh login-node ./solver",
    )

    for command in commands:
        projection = safe_bash._project_bash_route(command)
        action = safe_bash._bash_route_action(
            command, execution_stage="diagnostic")

        assert projection["unknown_entry"] is False, command
        assert action["read_only"] is False, command
        assert "external_job" in action["observed_effects"], command
        assert "unknown_executable" not in action["observed_effects"], command


def test_control_plane_queries_reach_public_executor(tmp_path, monkeypatch):
    _trust_control_plane_entries(monkeypatch)
    state = _state(tmp_path)
    calls = []

    async def fake_executor(_state, cmd, **kwargs):
        calls.append((cmd, kwargs))
        return {
            "status": "success",
            "returncode": 0,
            "stdout_tail": "query ok",
            "stderr_tail": "",
        }

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_executor)
    monkeypatch.setattr(safe_bash, "_orig_run_bash", fake_executor)
    commands = (
        "kubectl get pods",
        "kubectl auth can-i create pods",
        "ssh -V",
    )

    for command in commands:
        result = asyncio.run(safe_bash._safe_run_bash(state, command))
        assert result["status"] == "success", (command, result)

    assert len(calls) == len(commands)


def test_legacy_graph_with_observable_outputs_is_recorded_not_blocked(
    tmp_path,
    monkeypatch,
) -> None:
    state = _state(tmp_path)
    source_root = tmp_path / "observable-src"
    build_root = tmp_path / "observable-build"
    source_root.mkdir()
    build_root.mkdir()
    expected_library = str(build_root / "libsolver.a")
    for artifact_type in ("platform_profile", "source_recon"):
        state.save_artifact(artifact_type, f"observable_{artifact_type}", "{}")
    graph = {
        "status": "extracted",
        "actionable": True,
        "dag_id": "observable-output-dag",
        "source_root": str(source_root),
        "build_root": str(build_root),
        "nodes": {
            "lib": {"deps": [], "outputs": [expected_library]},
            "app": {"deps": ["lib"], "outputs": [str(build_root / "app")]},
        },
    }
    state.hook_state["provisioned_build_state"] = {
        "dag_id": "observable-output-dag",
        "nodes": {
            "lib": {
                "state": "planned",
                "deps": [],
                "blocked_by": [],
                "outputs": [expected_library],
            },
            "app": {
                "state": "planned",
                "deps": ["lib"],
                "blocked_by": ["lib"],
                "outputs": [str(build_root / "app")],
            },
        },
    }
    monkeypatch.setattr(
        safe_bash,
        "bench_enabled",
        lambda capability: capability in {
            "build_gate", "recon", "provision_first",
        },
    )
    monkeypatch.setattr(
        safe_bash, "_infer_source_path", lambda *_a, **_k: str(source_root)
    )
    monkeypatch.setattr(
        safe_bash, "_infer_build_root", lambda *_a, **_k: str(build_root)
    )
    monkeypatch.setattr(
        safe_bash, "_has_matching_source_recon", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        safe_bash, "_latest_build_graph", lambda *_a, **_k: graph
    )

    result = safe_bash._build_gate(
        state,
        "cmake --build . --target app",
        cwd=str(build_root),
        route_decision={
            "decision": "matched_legacy_step",
            "authoritative": False,
        },
    )

    # 判决拆除（sb:3341 删，2026-08-31）：构建前置闸整体降为 warning —— 同函数
    # 下方自认 DAG 模型不可靠，实测后果是弱模型绕行 execute_python。留痕不拦，
    # 构建系统自己会拒绝缺前置。防线从「墙」变成「账」。
    assert result is None
    events = state.transcript_path.read_text(encoding="utf-8")
    assert "build_gate_prereq_warning" in events
    assert "lib" in events


def test_static_submit_sequence_accepts_only_linear_direct_payload():
    command = (
        "cmake -S /src -B /build && "
        "cmake --build /build && "
        "/build/hf_toolchain_smoke > /run/smoke_stdout.txt 2>&1 && "
        "sha256sum /src/CMakeLists.txt"
    )

    sequence, reason = safe_bash._static_submit_program_sequence(command)

    assert reason is None
    assert sequence == [
        "cmake", "cmake", "/build/hf_toolchain_smoke", "sha256sum",
    ]
    action = resource_manager._submission_route_action(
        command, stage="toolchain_build", dry_run=False,
        route_step_id="build_and_run",
    )
    assert action["program"] == "cmake"
    assert action["program_sequence"] == sequence

    for rejected in (
        "sh ./run_smoke.sh",
        "cmake -S /src -B /build || cmake --build /build",
        "cmake --build /build | tee /build/build.log",
        "(cmake --build /build)",
        "f(){ cmake --build /build; }; f",
    ):
        observed, rejection = safe_bash._static_submit_program_sequence(rejected)
        assert observed is None, rejected
        assert rejection, rejected




# ── E-3：成功自我纠偏的路线拒绝不计入终局门禁证据（route_block_resolution）──
# core/executor._gate_block_evidence 按 tool_calls 里 result.blocker.kind 计数，
# 同类 ≥3 次即终局记 gate_block_evidence。瞬时拒绝被后续成功的
# route_step_bound + route_step_outcome 解决后，不得再作为未解决阻断计入；
# 从未成功解决的照旧计入。拒绝 result 与 execution_route_blocked 事件一律经
# 真实 enforce_execution_route 产生，保证测试数据与生产 blocker 结构一致；
# hook 调用经注册表 + run_on_end 真实派发，不直接调函数。


def _blocked_record_via_enforce(
    state: State, step_id: str | None = None, program: str = "make",
) -> dict:
    """经生产 enforce_execution_route 产生拒绝 result + blocked 事件。

    step_id=None 走"未声明路线的高后果动作"拒绝（execution_route_required，
    blocker 无 route_step_id）；否则走绑定 mismatch
    （execution_route_step_binding_mismatch）。
    """
    action = {"tool": "safe_run_bash", "program": program,
              "observed_effects": ["process_tree"]}
    if step_id is None:
        decision = {"decision": "route_action_mismatch",
                    "policy": "formal_scientific_execution"}
    else:
        decision = {"decision": "route_step_binding_mismatch",
                    "route_step_id": step_id,
                    "policy": "formal_scientific_execution"}
    block = execution_route.enforce_execution_route(state, action, decision)
    assert block is not None and block["route_blocked"] is True
    return {"name": "safe_run_bash", "args": {"command": program},
            "result": block}


def _ok_record() -> dict:
    return {"name": "safe_run_bash", "args": {"command": "make"},
            "result": {"status": "success", "returncode": 0}}


def _append_success(state: State, step_id: str, attempt_id: str,
                    outcome: str = "success") -> None:
    state.append_transcript(
        "route_step_bound", attempt_id=attempt_id, route_step_id=step_id,
    )
    state.append_transcript(
        "route_step_outcome", attempt_id=attempt_id, route_step_id=step_id,
        outcome=outcome,
    )


def _dispatch_route_block_resolution(state: State, records: list[dict]) -> None:
    """按生产路径派发：注册表取 hook，经 run_on_end 调其 on_end。"""
    from types import SimpleNamespace

    from core.loop_hooks import HookContext, get_loop_hook, run_on_end
    import nodes.experiment.hooks  # noqa: F401 —— 触发 register_loop_hook

    hook = get_loop_hook("route_block_resolution")
    assert hook is not None and hook.on_end is not None
    ctx = HookContext(harness=None, state=state, messages=[], turn=9)
    asyncio.run(run_on_end([hook], ctx, SimpleNamespace(tool_calls=records)))


def test_route_block_resolution_hook_is_enabled_in_experiment_harness():
    """hook 必须在 agent_loop 实际启用集里 —— 只注册不启用 = on_end 永不派发。"""
    from core.agent_loop import resolve_enabled_hook_names
    from core.loader import load_harness
    from core.loop_hooks import get_loop_hook, list_hooks
    import nodes.experiment.hooks  # noqa: F401 —— 触发 register_loop_hook

    harness = load_harness("experiment")
    enabled = resolve_enabled_hook_names(harness)
    assert "route_block_resolution" in enabled
    hook = get_loop_hook("route_block_resolution")
    assert hook is not None and hook in list_hooks(enabled)
    # hooks 模块可能以两个 import 身份被加载（tests 的 sys.path 差异），
    # 函数对象不必 is 同一个 —— 校验注册的 on_end 确为本修复的实现即可。
    assert hook.on_end is not None
    assert hook.on_end.__name__ == "route_block_resolution_on_end"


def test_route_block_resolved_by_later_success_leaves_no_gate_evidence(tmp_path):
    """mismatch→后续成功 ⇒ 不进 gate_block_evidence，run 不因它 blocked。"""
    from core.executor import _gate_block_evidence

    state = _state(tmp_path)
    records = [_blocked_record_via_enforce(state, step_id=f"step-{i}")
               for i in (1, 2, 3)]
    for i in (1, 2, 3):
        _append_success(state, f"step-{i}", f"route-a{i}")
    records.append(_ok_record())

    _dispatch_route_block_resolution(state, records)

    assert _gate_block_evidence(records) is None
    for r in records[:3]:
        assert "blocker" not in r["result"]          # 不再作为未解决门禁证据
        assert r["result"]["resolved_blocker"]["kind"] == (
            "execution_route_step_binding_mismatch")  # 原始拒绝事实保留
    events, _warnings = execution_route._read_transcript_events(state)
    resolved = [e for e in events
                if e.get("event") == "execution_route_block_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["resolved"] == {"step-1": 1, "step-2": 1, "step-3": 1}
    blocked = [e for e in events if e.get("event") == "execution_route_blocked"]
    assert len(blocked) == 3                         # 已冻结事件一字不改


def test_route_block_without_later_success_still_counts(tmp_path):
    """mismatch→无后续成功 ⇒ 照旧计入终局门禁证据。"""
    from core.executor import _gate_block_evidence

    state = _state(tmp_path)
    records = [_blocked_record_via_enforce(state, step_id=f"step-{i}")
               for i in (1, 2, 3)]
    records.append(_ok_record())

    _dispatch_route_block_resolution(state, records)

    gate = _gate_block_evidence(records)
    assert gate is not None
    assert gate["kind"] == "execution_route_step_binding_mismatch"
    assert gate["count"] == 3
    for r in records[:3]:
        assert r["result"]["blocker"]["kind"] == (
            "execution_route_step_binding_mismatch")
    events, _warnings = execution_route._read_transcript_events(state)
    assert not any(e.get("event") == "execution_route_block_resolved"
                   for e in events)


def test_new_route_block_after_success_is_not_forgiven(tmp_path):
    """成功之后新出现、且再无后续成功的拒绝，不被之前的成功消解。"""
    state = _state(tmp_path)
    records = [_blocked_record_via_enforce(state, step_id="step-1")
               for _ in range(2)]
    _append_success(state, "step-1", "route-a1")
    records.append(_blocked_record_via_enforce(state, step_id="step-1"))

    _dispatch_route_block_resolution(state, records)

    assert "blocker" not in records[0]["result"]
    assert "blocker" not in records[1]["result"]
    assert records[2]["result"]["blocker"]["kind"] == (
        "execution_route_step_binding_mismatch")


def test_stepless_route_block_resolved_by_later_bound_success(tmp_path):
    """先跑命令被拒（无 route_step_id 可引用）→ 声明路线后成功 ⇒ 消解。

    execution_route_required / route_step_id_required 等拒绝的 blocker 没有
    route_step_id（拒绝时步骤还不存在），纠偏后的重试携带新 step_id、
    action_signature 随之改变 —— 消解证据是"其后出现了任一成功绑定并执行
    的路线步骤"。
    """
    from core.executor import _gate_block_evidence

    state = _state(tmp_path)
    records = [_blocked_record_via_enforce(state, program=p)
               for p in ("make", "cmake", "gmake")]
    assert records[0]["result"]["blocker"]["kind"] == "execution_route_required"
    assert records[0]["result"]["blocker"]["route_step_id"] is None
    _append_success(state, "step-1", "route-a1")
    records.append(_ok_record())

    _dispatch_route_block_resolution(state, records)

    assert _gate_block_evidence(records) is None
    for r in records[:3]:
        assert "blocker" not in r["result"]
        assert r["result"]["resolved_blocker"]["kind"] == (
            "execution_route_required")
    events, _warnings = execution_route._read_transcript_events(state)
    resolved = [e for e in events
                if e.get("event") == "execution_route_block_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["resolved"] == {"": 3}


def test_stepless_route_block_without_later_success_still_counts(tmp_path):
    """无 route_step_id 的拒绝、从未声明路线成功 ⇒ 照旧计入。"""
    from core.executor import _gate_block_evidence

    state = _state(tmp_path)
    records = [_blocked_record_via_enforce(state, program=p)
               for p in ("make", "cmake", "gmake")]
    records.append(_ok_record())

    _dispatch_route_block_resolution(state, records)

    gate = _gate_block_evidence(records)
    assert gate is not None
    assert gate["kind"] == "execution_route_required"
    assert gate["count"] == 3


def test_late_reconciled_outcome_does_not_forgive_blocks_after_bind(tmp_path):
    """消解界是成功尝试的 bound 事件序：崩溃恢复迟到补写的 outcome 不得
    赦免 bind 之后新出现且从未纠偏的拒绝。"""
    state = _state(tmp_path)
    records = [_blocked_record_via_enforce(state, step_id="step-1")]
    state.append_transcript(
        "route_step_bound", attempt_id="route-a1", route_step_id="step-1",
    )
    # bind 之后（如崩溃后）新出现的同 step 拒绝
    records.append(_blocked_record_via_enforce(state, step_id="step-1"))
    # 崩溃恢复对账迟到补写的成功 outcome —— 事件序晚于真实成功时刻
    state.append_transcript(
        "route_step_outcome", attempt_id="route-a1", route_step_id="step-1",
        outcome="submitted",
    )

    _dispatch_route_block_resolution(state, records)

    assert "blocker" not in records[0]["result"]     # bind 前的拒绝被消解
    assert records[1]["result"]["blocker"]["kind"] == (
        "execution_route_step_binding_mismatch")     # bind 后的照旧计入

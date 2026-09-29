"""受控工作目录回归测试（2026-08-18）。

事故：agent 想在本 run 的 run_root 下编译一个最小 CUDA probe。第一条
「建目录 + 写源码 + 编译 + 运行」复合命令被当时的 exec_preflight 拦下（二进制
尚不存在），但拦截是整条的 —— mkdir 也没执行。agent 误以为目录已建好，改发
「只编译」，于是 `cd <不存在目录>` 失败，shell **继续**在继承的 cwd 里写源码、
调 nvcc、`ls`，末尾 `ls` 返回 0，工具上报 success。产物落在错误目录，声明工作
目录与实际工作目录脱节，provenance 断裂。

不变量：
    required_workdir_valid = false ⇒ side_effects = 0 ⇒ status != success

两道防线（顺序即优先级）：
  ① `cwd` 参数是唯一权威工作目录，目录不存在时子进程根本不 spawn；
  ② 命令文本里语句开头的 `cd` 被改写成进不去就 exit 73，其失败不再能被
     后续语句的成功退出码掩盖。
不加全局 `set -e`：grep 无匹配、which 缺失等在诊断命令里是正常语义。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from core import sandbox
from core.state import State
from nodes.experiment.tools import safe_bash as sb
from nodes.experiment.tools.run_contract import _classify_experiment_scope


requires_sandbox = pytest.mark.skipif(
    not sandbox.availability()[0],
    reason="mandatory Docker sandbox is unavailable",
)


@pytest.fixture
def state(tmp_path, monkeypatch) -> State:
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    base = tmp_path / "home" / "projects" / "proj-wd" / "runs"
    base.mkdir(parents=True, exist_ok=True)
    st = State.new(node_type="experiment", base_dir=base, project_id="proj-wd")
    run_root = sb.experiment_output_dir(st, "runtime", create=True)
    st.hook_state["path_roles"] = {
        "experiment_root": str(st.root),
        "run_root": str(run_root),
    }
    st.hook_state["node_inputs"] = {
        "fixture": "required_workdir",
        "requested_work": "验证受管 shell 与 Python 的声明工作目录边界。",
    }
    classified = asyncio.run(_classify_experiment_scope(
        st,
        scope="operation",
        operation_category="environment_probe",
        reason="工作目录回归中的执行尝试必须绑定稳定的上游测试输入。",
    ))
    assert classified["status"] == "success", classified
    try:
        yield st
    finally:
        manifest = getattr(st, "sandbox_manifest", None)
        if isinstance(manifest, dict) and manifest.get("attempt_id"):
            attempt_id = str(manifest["attempt_id"])
            evicted = sandbox.evict_state_attempt(st)
            leaked = [
                row for row in sandbox.list_attempt_instances()
                if row.get("attempt_id") == attempt_id
            ]
            assert evicted or not leaked, (
                "production RunAttempt still exists after teardown: " + attempt_id
            )


def _run(state: State, cmd: str, cwd: str | None = None) -> dict:
    if cwd is None:
        cwd = str(_run_root(state))
    return asyncio.run(sb._exec_and_log(state, cmd, timeout=60, cwd=cwd))


def _run_root(state: State) -> Path:
    return Path(state.hook_state["path_roles"]["run_root"])


# ── 1. cd 失败不得执行后续命令 ───────────────────────────────────────────────

@requires_sandbox
def test_failed_cd_does_not_run_following_commands(state, tmp_path):
    missing = tmp_path / "no-such-run-root"
    probe = _run_root(state) / "should_not_exist_wd_probe"
    assert not probe.exists()
    res = _run(state, f"cd {missing}\ntouch {probe.name}")
    assert res["status"] == "error"
    assert res.get("reason") == sb.WORKDIR_UNAVAILABLE
    assert not probe.exists(), "cd 失败后仍产生了副作用"


# ── 2. cd 失败后尾部命令成功也必须失败 ───────────────────────────────────────

@requires_sandbox
def test_failed_cd_is_not_masked_by_trailing_success(state, tmp_path):
    """事故现场的最小形态：末尾命令返回 0，整体退出码曾因此变成 0。"""
    res = _run(state, f"cd {tmp_path / 'missing'}\ntrue")
    assert res["status"] == "error"
    assert res["returncode"] != 0
    assert res.get("reason") == sb.WORKDIR_UNAVAILABLE


@requires_sandbox
def test_failed_cd_before_build_and_ls_reports_error(state, tmp_path):
    """完整复现：cd 失败 → 编译 → ls 成功。ls 的 0 不得代表"在声明目录里构建成功"。"""
    res = _run(
        state,
        f"cd {tmp_path / 'gpu_probe'}\n"
        "echo 'int main(){return 0;}' > probe.c\n"
        "echo '--- compile exit=$? ---'\n"
        "ls -la .")
    assert res["status"] == "error"
    assert not (_run_root(state) / "probe.c").exists()


# ── 3. 受控 build 的产物必须落在 run_root，而不是继承 cwd ───────────────────

@requires_sandbox
def test_declared_workdir_receives_build_artifacts(state):
    run_root = _run_root(state)
    res = _run(state, "printf '#!/bin/sh\\nexit 0\\n' > probe.sh && chmod +x probe.sh",
               cwd=str(run_root))
    assert res["status"] == "success", res
    binary = run_root / "probe.sh"
    assert binary.is_file()
    assert Path(os.path.realpath(binary)).is_relative_to(run_root.resolve())
    assert not (Path(os.getcwd()) / "probe.sh").exists()
    assert res["workdir"] == str(run_root)
    assert res["workdir_authority"] == "cwd_param"


# ── 4. 编译与执行分离：先造产物，再单独执行已存在的产物 ─────────────────────

@requires_sandbox
def test_build_then_execute_as_two_calls(state):
    run_root = _run_root(state)

    async def _both() -> tuple[dict, dict]:
        # 同一 event loop 里连做两次：state 上的取消 Event 绑定首个 loop，
        # 两次 asyncio.run 会让第二次命令被误判为 cancelled（测试设施约束，
        # 与被测语义无关）。
        build = await sb._exec_and_log(
            state, "printf '#!/bin/sh\\necho ran\\n' > probe.sh "
                   "&& chmod +x probe.sh && ls probe.sh",
            timeout=60, cwd=str(run_root))
        assert build["status"] == "success", build
        attempt_id = sandbox.parse_manifest(state.sandbox_manifest).attempt_id
        # 第二次调用前，产物已经存在且路径可验证 —— 诊断因此无话可说。
        assert sb._exec_target_diagnosis("./probe.sh", cwd=str(run_root)) is None
        run = await sb._exec_and_log(
            state, "./probe.sh", timeout=60, cwd=str(run_root))
        assert sandbox.parse_manifest(state.sandbox_manifest).attempt_id == attempt_id
        return build, run

    build, run = asyncio.run(_both())
    assert run["status"] == "success", run
    assert "ran" in run["stdout_tail"]
    # 声明路径与实际执行路径一致 —— 审计读 workdir，不反解命令文本。
    assert run["workdir"] == str(run_root)


def test_exec_diagnosis_states_that_preceding_statements_ran():
    """判决拆除·第三波（sb:1855 降格）：不再事前拦整条命令；bash rc=127 之后的
    诊断文案必须点明前置语句（mkdir/heredoc）**已生效**，否则 agent 会带着
    「什么都没跑」的错误前提重做一遍。"""
    diagnosis = sb._exec_target_diagnosis("mkdir -p out && ./out/probe && echo done")
    assert diagnosis is not None and diagnosis["kind"] == "exec_target_missing"
    assert "已生效" in diagnosis["hint"] and "mkdir" in diagnosis["hint"]


# ── 5. cwd 指向不存在目录：shell 根本没启动，零副作用 ───────────────────────

def test_missing_cwd_never_spawns_a_shell(state, tmp_path, monkeypatch):
    spawned: list[tuple] = []

    async def _no_spawn(*args, **kw):
        spawned.append(args)
        raise AssertionError("工作目录不可用时不得启动子进程")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)
    missing = _run_root(state) / "not-created-yet"
    res = _run(state, "touch side_effect.txt", cwd=str(missing))
    assert res["status"] == "error"
    assert res["reason"] == sb.WORKDIR_UNAVAILABLE
    assert spawned == []
    assert not missing.exists(), "cwd 不得被本次调用顺带创建"


def test_missing_cwd_rejected_before_any_gate(state):
    """入口级校验：_safe_run_bash 同样在任何门之前拒绝，不去打扰人批准。"""
    missing = _run_root(state) / "absent"
    res = asyncio.run(sb._safe_run_bash(state, "ls", cwd=str(missing)))
    assert res["status"] == "error"
    assert res["blocker"]["kind"] == sb.WORKDIR_UNAVAILABLE


# ── 6. 显式 cwd 才是权威；文本 cd 失败不得记成成功 ──────────────────────────

@requires_sandbox
def test_explicit_cwd_stays_authoritative_when_inline_cd_fails(state, tmp_path):
    run_root = _run_root(state)
    res = _run(state, f"cd {tmp_path / 'elsewhere'}\ntouch stray.txt\ntrue",
               cwd=str(run_root))
    assert res["status"] != "success"
    assert res["reason"] == sb.WORKDIR_UNAVAILABLE
    # 权威工作目录仍是显式 cwd，不因命令文本里的 cd 改变归属
    assert res["workdir"] == str(run_root)
    assert res["workdir_authority"] == "cwd_param"
    assert not (run_root / "stray.txt").exists()
    assert not (Path(os.getcwd()) / "stray.txt").exists()


@requires_sandbox
def test_full_log_header_records_actual_workdir(state):
    run_root = _run_root(state)
    res = _run(state, "pwd", cwd=str(run_root))
    assert res["status"] == "success"
    log = Path(res["log_path"]).read_text(encoding="utf-8")
    assert f"# workdir: {run_root}" in log
    assert "# workdir_authority: cwd_param" in log
    assert res["stdout_tail"].strip() == str(run_root)


# ── 加固的边界：不能改坏本来就写对的命令 ────────────────────────────────────

def test_hardening_leaves_explicit_fallback_and_heredoc_alone():
    """`cd x || fallback` 是显式容错；heredoc 正文是数据，不是要执行的语句。"""
    assert sb._harden_leading_cd("cd /a || mkdir -p /a")[1] == []
    assert sb._harden_leading_cd("cd -")[1] == []
    body = "cat > s.sh <<'EOF'\ncd /inside\nEOF\nbash s.sh"
    assert sb._harden_leading_cd(body) == (body, [])


@requires_sandbox
def test_no_global_set_e_ordinary_nonzero_probes_still_continue(state):
    """诊断命令里 grep 无匹配、which 缺失是正常语义，不得中断整条命令。"""
    run_root = _run_root(state)
    res = _run(state, "echo hay > f.txt\ngrep needle f.txt\n"
                      "which definitely-not-a-real-binary\necho reached-the-end",
               cwd=str(run_root))
    assert "reached-the-end" in res["stdout_tail"]


# ── 7. 两个执行工具的 cwd 语义必须一致 ──────────────────────────────────────

def test_python_and_bash_agree_on_missing_workdir(state):
    """同一件事在两个工具上不得反应相反。

    此前 safe_execute_python 走框架 _execute_python，那里 `workspace.mkdir(
    parents=True, exist_ok=True)` 把不存在的 cwd 静默建出来。后果有两层：
    ① 规则说"目录不存在先单独 mkdir -p"，对 python 是空话；
    ② cwd 打错一个字母不会被发现 —— 目录被建出来，代码正常跑完，产物落进
       一个谁都没打算创建的目录。声明和实际仍然一致，所以不是本次那种
       fail-open，但"系统不提醒你搞错了"是同一类毛病。
    """
    missing = _run_root(state) / "typo-dir"
    res = asyncio.run(sb._safe_execute_python(state, "print(1)", cwd=str(missing)))
    assert res["status"] == "error"
    assert res["reason"] == sb.WORKDIR_UNAVAILABLE
    assert not missing.exists(), "工作目录不得被本次调用顺带创建"

    bash_res = asyncio.run(sb._safe_run_bash(state, "true", cwd=str(missing)))
    assert bash_res["blocker"]["kind"] == res["blocker"]["kind"]


@requires_sandbox
def test_python_runs_in_declared_existing_workdir(state):
    run_root = _run_root(state)
    res = asyncio.run(sb._safe_execute_python(
        state, "import os; print(os.getcwd())", cwd=str(run_root)))
    assert res["status"] == "success", res
    assert str(run_root) in res.get("stdout", "") + res.get("stdout_tail", "")



@requires_sandbox
def test_safe_tools_ignore_legacy_stage_and_default_to_run_root(state):
    async def run():
        bash = await sb._safe_run_bash(state, "pwd", stage="toolchain_build")
        python = await sb._safe_execute_python(
            state, "import os; print(os.getcwd())", stage="toolchain_build")
        return bash, python

    runtime = sb.experiment_output_dir(state, "runtime", create=False)
    bash, python = asyncio.run(run())
    assert bash["status"] == "success", bash
    assert str(runtime) in bash.get("stdout_tail", "")
    assert python["status"] == "success", python
    assert str(runtime) in python.get("stdout", "") + python.get("stdout_tail", "")



def test_default_workdir_derivation_failure_never_starts_an_executor(state, monkeypatch):
    started: list[str] = []

    def broken_default(*_args, **_kwargs):
        raise RuntimeError("path derivation failed")

    async def unexpected_bash(*_args, **_kwargs):
        started.append("bash")
        raise AssertionError("bash executor must not start")

    async def unexpected_python(*_args, **_kwargs):
        started.append("python")
        raise AssertionError("python executor must not start")

    monkeypatch.setattr(sb, "experiment_output_dir", broken_default)
    monkeypatch.setattr(sb, "_orig_run_bash", unexpected_bash)
    monkeypatch.setattr(sb, "_exec_and_log", unexpected_python)

    async def run():
        return (
            await sb._safe_run_bash(state, "echo must-not-run"),
            await sb._safe_execute_python(state, "print(1)"),
        )

    bash, python = asyncio.run(run())
    assert bash["reason"] == sb.DEFAULT_WORKDIR_UNAVAILABLE
    assert python["reason"] == sb.DEFAULT_WORKDIR_UNAVAILABLE
    assert started == []

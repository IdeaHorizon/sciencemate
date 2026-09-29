"""Experiment Bash 路径效果守卫回归测试。

背景：2026-07-10 / 07-13 meiyu e2e 两次误拦，共同根因两个：
  1. 项目级 workspace 必须以 run_root 明确声明；声明后即使框架 home 嵌在
     git 仓库里也不应落到 "git working tree" 兜底拦截；
  2. 命令内自定义变量（WORKDIR="/abs"; cd "$WORKDIR"）expandvars 展不开
     → 路径解析回退到 cwd，写目标归类到错误的树。
误拦的次生危害已实测：agent 为绕过拦截去掉 cd，wrf.exe 在 /tmp 启动秒崩。

本文件锁住：白名单优先、变量展开、数据领地不按 source tree 归因、
真源码树保护不回退、拦截消息含 cd 指引。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from core.state import State
from core.harness import NodeHarness
from core.llm import LLMMessage
from core.loop_hooks import HookContext
from core.loop_hooks_builtin import _highrisk_confirm_on_turn_start
from nodes.experiment.tools import safe_bash as sb
from nodes.experiment.tools.path_roles import collect_path_roles
from nodes.experiment.tools.execution_route import _declare_execution_route
from nodes.experiment.tools.run_contract import _classify_experiment_scope
from core import sandbox as _core_sandbox
from shared.lib import dangerous_commands


def _non_tmp_base() -> Path:
    """/tmp 之外的可写测试根：优先 HF_TEST_NON_TMP_DIR（sandbox/CI 里 /home
    可能只读），否则 ~/.cache。"""
    override = os.getenv("HF_TEST_NON_TMP_DIR")
    base = Path(override) if override else Path.home() / ".cache"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _mkdtemp_non_tmp(prefix: str) -> Path:
    import tempfile as _tf
    try:
        return Path(_tf.mkdtemp(prefix=prefix, dir=str(_non_tmp_base())))
    except OSError:
        pytest.skip("无 /tmp 之外的可写目录（设 HF_TEST_NON_TMP_DIR 以启用本测试）")


@pytest.fixture(autouse=True)
def _home_outside_tmp(_isolate_harness_home, monkeypatch):
    """conftest 把 HARNESS_FRAMEWORK_HOME 放 /tmp 下，而 /tmp 是无条件白名单，
    会让本文件所有 workspace/数据领地断言被 /tmp 放行假通过。挪到 /tmp 之外。"""
    import shutil
    home = _mkdtemp_non_tmp("hf-sg-test-")
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(home))
    yield
    shutil.rmtree(home, ignore_errors=True)


requires_sandbox = pytest.mark.skipif(
    not _core_sandbox.availability()[0],
    reason="mandatory Docker sandbox is unavailable")


def _mk_state(project_id: str = "proj-x") -> State:
    home = Path(os.environ["HARNESS_FRAMEWORK_HOME"])
    base = home / "projects" / project_id / "runs"
    base.mkdir(parents=True, exist_ok=True)
    st = State.new(node_type="experiment", base_dir=base, project_id=project_id)
    st.hook_state["node_inputs"] = {
        "experiment_focus": "Exercise the declared safe-bash boundary regression.",
        "stage": "diagnostic",
    }
    classified = asyncio.run(_classify_experiment_scope(
        st,
        scope="operation",
        operation_category="other",
        reason="Exercise the declared safe-bash boundary without scientific conclusions.",
    ))
    assert classified["status"] == "success", classified
    return st


def _workspace(st: State) -> Path:
    ws = Path(st.project_root) / "workspace" / "pilot" / "WRF" / "CTRL"
    ws.mkdir(parents=True, exist_ok=True)
    st.hook_state["path_roles"] = {
        "experiment_root": str(ws.parent),
        "run_root": str(ws),
    }
    return ws


# ── 误报回归 1：项目级 workspace 是白名单，git 嵌套不影响 ────────────────────

def test_project_workspace_write_allowed():
    st = _mk_state()
    ws = _workspace(st)
    res = sb._bash_path_effects_guard(
        st,
        f'rm -f "{ws}/rsl.error.0000" "{ws}/rsl.out.0000"',
        cwd=str(ws),
        allow_authorization=False,
    )
    assert res is None


def test_project_workspace_allowed_even_inside_git_repo():
    """测试部署形态：HARNESS_FRAMEWORK_HOME 整个嵌在一个 git 仓库里。"""
    home = Path(os.environ["HARNESS_FRAMEWORK_HOME"])
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    # autouse 夹具会在测试后回收整个隔离目录；这里不再手工递归删除 .git，
    # 避免测试自身复制生产中需要阻止的危险命令模式。
    st = _mk_state()
    ws = _workspace(st)
    res = sb._bash_path_effects_guard(
        st,
        f"cd {ws} && rm -f rsl.error.0000 && touch wrf.stdout",
        cwd=str(ws),
        allow_authorization=False,
    )
    assert res is None


def test_framework_data_outside_whitelist_still_blocked_with_correct_reason():
    """project_root 下 workspace 以外（kb 账本等）仍拦，但归因不再是 source tree。"""
    st = _mk_state()
    kb = Path(st.project_root) / "kb_claims.jsonl"
    scope, reason = sb._classify_write_path(str(kb), st)
    assert scope != "safe"
    assert scope != "protected_source_tree"


def test_project_run_rejects_declared_external_build_root():
    """A declared role cannot turn a home-directory tree into a project asset."""
    st = _mk_state()
    # The project-worktree boundary is what makes this a project containment test.
    st.workspace_root = Path(st.project_root) / "workspace" / "experiments"
    external = _mkdtemp_non_tmp("hf-external-build-")
    st.hook_state["path_roles"] = {"build_root": str(external)}
    target = external / "CMakeCache.txt"

    scope, _ = sb._classify_write_path(str(target), st)
    assert scope == "outside_project_workspace"

    blocked = sb._bash_path_effects_guard(
        st, f"touch {target}", allow_authorization=False)
    assert blocked["status"] == "error"
    assert blocked["blocker"]["scope"] == "outside_project_workspace"


# ── 误报回归 2：命令内变量展开 ───────────────────────────────────────────────

def test_cmd_var_expansion_basic():
    out = sb._expand_cmd_vars('WORKDIR="/a/b"\ncd "$WORKDIR"\nrm -f "$WORKDIR/x"')
    assert 'cd "/a/b"' in out and '"/a/b/x"' in out


def test_cmd_var_expansion_chained_and_braced():
    out = sb._expand_cmd_vars('A=/a\nB="$A/b"\ntouch "${B}/f"')
    assert '"/a/b/f"' in out


def test_cmake_relative_source_is_not_inferred_as_build_directory(tmp_path):
    source = tmp_path / "gromacs-2024.6"
    build = tmp_path / "gromacs-2024.6-build"
    source.mkdir()
    build.mkdir()
    st = _mk_state()

    inferred = sb._infer_source_path(
        f"cd {build} && cmake ../gromacs-2024.6 -DGMX_MPI=off", st)

    assert inferred == str(source.resolve())


def test_cmake_build_resolves_canonical_source_inside_harness_tree():
    st = _mk_state("source-context")
    nested = (
        Path(os.environ["HARNESS_FRAMEWORK_HOME"])
        / "node4-experiment"
        / "harness-framework"
        / "nodes"
        / "experiments"
        / "fixtures"
    )
    source = nested / "source"
    build = Path(st.root) / "outputs" / "experiment" / "build"
    source.mkdir(parents=True)
    build.mkdir(parents=True)
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\n",
        encoding="utf-8",
    )
    (build / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={source}\n",
        encoding="utf-8",
    )
    st.hook_state["node_inputs"] = {
        "path_roles": {
            "source_baseline_root": {
                "path": str(source),
                "writable": False,
            },
            "build_root": {
                "path": str(build),
                "writable": True,
            },
        },
    }

    inferred = sb._infer_source_path(
        f"cmake --build {build}",
        st,
        cwd=str(build),
    )

    assert sb._is_harness_dir(str(source)) is True
    assert inferred == str(source.resolve())
    assert sb._infer_build_root(
        f"cmake --build {build}", st, cwd=str(build)
    ) == str(build.resolve())


def test_ambiguous_canonical_sources_do_not_fall_back_to_build_root():
    st = _mk_state("ambiguous-source-context")
    root = Path(st.root) / "context"
    source_a = root / "source-a"
    source_b = root / "source-b"
    build = root / "build"
    for path in (source_a, source_b, build):
        path.mkdir(parents=True)
    st.hook_state["node_inputs"] = {
        "path_roles": {
            "source_baseline_root": [
                {"path": str(source_a), "writable": False},
                {"path": str(source_b), "writable": False},
            ],
            "build_root": {"path": str(build), "writable": True},
        },
    }

    inferred = sb._infer_source_path(
        f"make -C {build}",
        st,
        cwd=str(build),
    )

    assert inferred == ""
    assert inferred != str(build.resolve())


def test_common_build_tool_source_operands_do_not_become_build_roots(tmp_path):
    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    st = _mk_state()

    commands = (
        f"cd {build} && cmake -S ../source -B .",
        f"cd {build} && meson setup . ../source",
        f"cd {build} && ../source/configure --prefix=/opt/example",
        f"cd {build} && python ../source/setup.py build",
    )
    for command in commands:
        assert sb._infer_source_path(command, st) == str(source.resolve())


def test_workdir_var_paths_remain_inside_declared_run_root():
    """后台启动由静态门另行拒绝；路径层只验证 WORKDIR 解析。"""
    st = _mk_state()
    ws = _workspace(st)
    cmd = (
        f'WORKDIR="{ws}"\n'
        'cd "$WORKDIR"\n'
        'rm -f rsl.error.0000 rsl.out.0000\n'
        'nohup env LD_LIBRARY_PATH="$WORKDIR/lib:/opt/conda/lib" '
        '/opt/wrf/main/wrf.exe > "$WORKDIR/wrf.stdout" 2> "$WORKDIR/wrf.stderr" &'
    )
    assert sb._bash_path_effects_guard(
        st, cmd, cwd=str(ws), allow_authorization=False) is None


# ── 保护不回退 ───────────────────────────────────────────────────────────────

def test_true_git_source_tree_still_blocked():
    # 不能用 pytest tmp_path：/tmp 是无条件白名单，会假通过
    import shutil
    src = _mkdtemp_non_tmp("hf-sg-src-")
    try:
        st = _mk_state()
        subprocess.run(["git", "init", "-q", str(src)], check=True)
        (src / "solver.f90").write_text("! src\n")
        res = sb._bash_path_effects_guard(
            st,
            f'sed -i "s/a/b/" "{src}/solver.f90"',
            allow_authorization=False,
        )
        assert res["status"] == "error"
        assert res["blocker"]["scope"] == "protected_source_tree"
    finally:
        shutil.rmtree(src, ignore_errors=True)


def test_global_env_still_blocked():
    st = _mk_state()
    res = sb._bash_path_effects_guard(
        st, "rm -f /usr/lib/libfoo.so", allow_authorization=False)
    assert res["status"] == "error"
    assert res["blocker"]["scope"] == "protected_global_env"


def test_error_message_gives_cd_guidance():
    msg = sb._scope_guard_error("cmd", "protected_source_tree", "r", "/x")["error"]
    assert "cd" in msg and "symlink" in msg


# ── exec_target_diagnosis：rc=127/126 之后的可执行目标诊断（判决拆除·第三波，
#    sb:1855/1868 降格：前身 exec_preflight 是事前拦整条命令的预测失败墙）────────

def test_exec_diagnosis_missing_binary():
    r = sb._exec_target_diagnosis("cd /tmp && ./solver.exe", cwd="/tmp")
    assert r is not None and r["kind"] == "exec_target_missing"
    assert r["target"] == "./solver.exe" and r["cwd"] == "/tmp"
    # 与旧墙相反：文案必须如实说前置语句已生效，别再让 agent 重做 mkdir/heredoc
    assert "已生效" in r["hint"] and "未生效" not in r["hint"]


def test_exec_diagnosis_mpirun_target_checked():
    r = sb._exec_target_diagnosis("mpirun -np 4 /nonexistent/bin/solver")
    assert r is not None and r["resolved"] == "/nonexistent/bin/solver"


def test_exec_diagnosis_mpi_option_paths_not_mistaken_for_target():
    """--hostfile/-machinefile 的路径值是选项参数，不是被启动程序。"""
    st = _mk_state()
    ws = _workspace(st)
    hosts = ws / "hosts"
    hosts.write_text("localhost\n")          # 存在但无执行位——误当目标会误诊
    app = ws / "app.exe"
    app.write_text("#!/bin/sh\n")
    app.chmod(0o755)
    assert sb._exec_target_diagnosis(
        f'cd "{ws}" && mpirun --hostfile {hosts} -np 4 ./app.exe') is None
    assert sb._exec_target_diagnosis(
        f'cd "{ws}" && mpirun -machinefile {hosts} ./app.exe') is None
    r = sb._exec_target_diagnosis(
        f'cd "{ws}" && mpirun --hostfile {hosts} -np 4 ./missing.exe')
    assert r is not None and r["target"] == "./missing.exe"


def test_exec_diagnosis_relative_path_with_slash_checked():
    """bin/solver 这类不带 ./ 前缀的相对路径 argv0 同样是文件系统路径。"""
    st = _mk_state()
    ws = _workspace(st)
    r = sb._exec_target_diagnosis(f'cd "{ws}" && bin/solver --input x.in')
    assert r is not None and r["kind"] == "exec_target_missing"
    (ws / "bin").mkdir()
    (ws / "bin" / "solver").write_text("#!/bin/sh\n")
    (ws / "bin" / "solver").chmod(0o755)
    assert sb._exec_target_diagnosis(f'cd "{ws}" && bin/solver --input x.in') is None


def test_exec_diagnosis_resolves_cd_and_vars():
    st = _mk_state()
    ws = _workspace(st)
    (ws / "ok.sh").write_text("#!/bin/sh\n")
    (ws / "ok.sh").chmod(0o755)
    cmd = f'W="{ws}"\ncd "$W"\nnohup ./ok.sh > out.log 2>&1 &'
    assert sb._exec_target_diagnosis(cmd) is None
    cmd2 = f'W="{ws}"\ncd "$W"\nnohup ./missing.exe > out.log 2>&1 &'
    r = sb._exec_target_diagnosis(cmd2)
    assert r is not None and r["cwd"] == str(ws)


def test_exec_diagnosis_not_executable():
    st = _mk_state()
    ws = _workspace(st)
    (ws / "data.bin").write_text("x")
    r = sb._exec_target_diagnosis(f'cd "{ws}" && ./data.bin')
    assert r is not None and r["kind"] == "exec_target_not_executable"


def test_exec_diagnosis_ignores_bare_names_and_reads():
    # 裸命令名走 PATH，不检查；读操作路径参数不是 argv0，不检查
    assert sb._exec_target_diagnosis("ls /nonexistent/path") is None
    assert sb._exec_target_diagnosis("grep foo /no/such/file") is None


def test_exec_diagnosis_regression_wrf_style_missing_binary():
    """回归锚点（2026-07-13 meiyu e2e）：nohup 启动 HPC 二进制时因未 cd 到
    运行目录、目标不存在而静默秒崩——现在由 bash rc=127 拒绝后附诊断。"""
    st = _mk_state()
    ws = _workspace(st)
    cmd = (f'WORKDIR="{ws}"\n'
           'nohup env LD_LIBRARY_PATH="$WORKDIR/lib" ./wrf.exe '
           '> "$WORKDIR/wrf.stdout" 2> "$WORKDIR/wrf.stderr" &')
    r = sb._exec_target_diagnosis(cmd, cwd="/tmp")
    assert r is not None and r["target"] == "./wrf.exe" and r["cwd"] == "/tmp"


def test_missing_exec_target_is_not_refused_before_the_command_runs(monkeypatch):
    """降格判据（不依赖沙箱那半）：本地 bash 路径上不再有事前预检墙。

    从前 `_exec_preflight_bash` 会在这里拦下整条命令；判决拆除·第三波把它降格成
    rc=127/126 之后的 `exec_diagnosis`。这一半不需要 Docker，所以在任何环境都跑：
    墙加回去 —— 结果会带 exec_preflight_blocked 事件或 exec_preflight 文案 —— 即转红。
    诊断内容本身在下面 requires_sandbox 那条里连着真实 rc 一起验。
    """
    st = _mk_state()
    ws = sb.experiment_output_dir(st, "runtime", create=True)

    diagnosis = sb._exec_target_diagnosis("mkdir -p out && ./solver.exe", cwd=str(ws))

    assert diagnosis["kind"] == "exec_target_missing"
    assert diagnosis["target"] == "./solver.exe"
    # 事前拦截的那条通道确实没了：本地 bash 路径零调用方。
    import inspect
    body = inspect.getsource(sb._safe_run_bash)
    assert "_exec_preflight_bash" not in body, (
        "本地 bash 路径又出现了事前 exec 预检 —— 那正是被降格掉的那堵墙")


@requires_sandbox
def test_missing_target_runs_and_rc127_carries_diagnosis(monkeypatch):
    """降格判据（需要真沙箱）：命令照跑（spawn 被调用），bash 的 rc=127 之后同一份
    诊断挂在结果与 transcript 上。墙加回去本测试即转红。

    受管生命周期下 `_exec_and_log` 会先做 RunAttempt 能力预检，没有 Docker 时它在
    sandbox_manifest_unavailable 就返回了 —— 这条断言的前提根本不成立，所以按本仓
    既有惯例 skip 掉，而不是让它在无 Docker 的机器上假红。
    """
    st = _mk_state()
    ws = sb.experiment_output_dir(st, "runtime", create=True)
    spawned: list[tuple] = []

    async def fake_spawn(*args, **kwargs):
        spawned.append(args)
        return "done", 127, b"", b"bash: ./solver.exe: No such file or directory\n"

    monkeypatch.setattr(sb, "spawn_and_wait", fake_spawn)
    res = asyncio.run(sb._exec_and_log(st, "mkdir -p out && ./solver.exe", timeout=30, cwd=str(ws)))

    assert spawned, f"命令必须真的被派去执行，而不是事前拦下；实际返回 {res}"
    assert res["status"] == "error" and res["returncode"] == 127
    assert res["exec_diagnosis"]["kind"] == "exec_target_missing"
    assert res["exec_diagnosis"]["target"] == "./solver.exe"
    events = [json.loads(line) for line in st.transcript_path.read_text().splitlines() if line.strip()]
    assert any(e.get("event") == "exec_target_diagnosis" and e.get("returncode") == 127 for e in events)
    assert not any(e.get("event") == "exec_preflight_blocked" for e in events)



# ─────────────────────────────────────────────────────────────────────────────
# build gate 命令位判定（2026-07-31 回归）
#
# run 1785469523-90f86c：`which git; which make; which g++; g++ --version`
# 被判成"正在编译"并要求先声明 build_root，节点因此卡在探查阶段。根因是旧
# _MAJOR_BUILD_RE 全串搜关键词，不区分 make 处于命令位还是参数位。
# 同一次排查还发现两个反向漏判：`g\+\+\b` 的 \b 在 `+` 后跟空格时永不成立，
# 所以 g++/nvc++ 的编译命令从来没被 gate 认出来过；`-o` 那条 alternative 的
# 编译器清单漏了 nvcc/nvc/nvc++/mpicc/mpicxx。
# ─────────────────────────────────────────────────────────────────────────────

_NOT_BUILD = [
    # 原始 bug：探查命令被判成构建
    'which git 2>&1; which make 2>&1; which g++ 2>&1; g++ --version 2>&1 | head -2',
    "which make",
    "man make",
    'echo "make sure to check"',
    "ls /usr/lib/x86_64-linux-gnu | grep make",
    "apt-cache search cmake",
    "cat Makefile",
    'find / -name "*make*"',
    'echo "gcc -c foo.c"',
    # 版本/能力探查
    "make --version",
    "make -n",
    "cmake --version",
    "ninja --version",
    "g++ --version",
    "nvcc -V",
    "gcc -dumpmachine",
    # 恰好带 -c / -o 但根本不是编译器
    "ls -o",
    "rm -c file",
]

_IS_BUILD = [
    "make", "make -j8", "gmake -j4 all", "ninja -j8", "ninja -v",
    "make help", "ninja help",
    "cd build && make -j8", "cmake .. && make -j8",
    "sudo make install", "time make -j4", "nice -n 10 make",
    "CC=gcc make", "/usr/bin/make -j2", "./case.build",
    "cmake --build . -j8",
    "make -j8 | tee build.log", "make 2>&1 | tail -20",
    # 旧正则漏判：g++ / nvc++ 的 \b 永不成立
    "g++ -c a.cpp", "g++ -o app a.cpp", "g++ -O2 -c a.cpp -o a.o",
    "nvc++ -o app a.cpp",
    # 旧正则漏判：-o alternative 缺 nvcc / mpicc
    "nvcc -arch=sm_90 -o app a.cu", "mpicc -o solver main.c",
    "gcc -c foo.c", "nvcc -c a.cu",
]


@pytest.mark.parametrize("cmd", _NOT_BUILD)
def test_probe_and_mention_commands_are_not_builds(cmd):
    """命令位之外出现 make/g++ 只是文本，不该触发 build gate。"""
    assert sb._is_major_build(cmd) is False


@pytest.mark.parametrize("cmd", _IS_BUILD)
def test_real_builds_still_detected(cmd):
    """命令位上的构建工具/编译器必须仍被 gate 认出来。"""
    assert sb._is_major_build(cmd) is True


def test_bash_dash_c_does_not_bypass_build_gate():
    """`bash -c "make"` 不能成为绕过 build gate 的后门。"""
    assert sb._is_major_build('bash -c "make -j8"') is True
    assert sb._is_major_build("sh -c 'ninja -j4'") is True
    # 但内嵌的仍然只是探查时不算构建
    assert sb._is_major_build('bash -c "make --version"') is False


def test_major_build_must_run_inside_build_root_not_source_tree(tmp_path):
    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    st = _mk_state()
    st.hook_state["path_roles"] = {
        "managed_source_root": str(source),
        "build_root": str(build),
    }

    in_source = sb._activity_path_role_guard(st, f"cd {source} && make -j2")
    assert in_source is not None
    assert "protected_source_tree" in in_source["error"]

    # 标准 out-of-source 形式可以从父目录调用；输出目录仍明确是 build_root。
    assert sb._activity_path_role_guard(
        st, f"cd {tmp_path} && cmake -S {source} -B {build}") is None
    assert sb._activity_path_role_guard(st, f"make -C {build} -j2") is None
    assert sb._activity_path_role_guard(st, f"make -C {source} -j2") is not None
    assert sb._activity_path_role_guard(st, f"cd {build} && cmake ../source") is None


def test_in_tree_build_requires_both_trusted_worktree_role_and_matching_route(tmp_path):
    baseline = tmp_path / "baseline"
    worktree = tmp_path / "worktree"
    baseline.mkdir()
    worktree.mkdir()
    st = _mk_state()
    st.hook_state["path_roles"] = {
        "source_baseline_root": str(baseline),
        "source_worktree_root": str(worktree),
    }
    route_decision = {
        "decision": "matched_ready_step",
        "authoritative": True,
        "declared_workdir_role": "source_worktree_root",
        "workdir_role_observed": True,
    }

    assert sb._activity_path_role_guard(
        st,
        "make -j2",
        cwd=str(worktree),
    ) is not None
    assert sb._activity_path_role_guard(
        st,
        "make -j2",
        cwd=str(worktree),
        route_decision=route_decision,
    ) is None
    assert sb._activity_path_role_guard(
        st,
        "make -j2",
        cwd=str(baseline),
        route_decision={**route_decision, "declared_workdir_role": "source_baseline_root"},
    ) is not None


def test_default_build_root_removes_bootstrap_deadlock():
    """首次编译不应因 agent 尚未重复声明默认 build_root 而停转。

    回归 run 1785469523-90f86c：探查阶段被误判为 build，随后第一条真实
    编译又因缺 build_root 被拦。现在每个 experiment 自动获得 run-local
    build_root；显式角色仍可覆盖它。
    """
    st = _mk_state()
    roles = collect_path_roles(st)
    build_root = next(
        Path(r.path) for r in roles if r.role == "build_root" and r.writable)
    assert sb._activity_path_role_guard(
        st, "make -j8", cwd=str(build_root)) is None


def test_build_without_output_selector_cannot_run_in_unrelated_cwd(tmp_path):
    """存在 build_root 不能授权 make 在任意当前目录启动。

    回归 WRF 事故链：入口初始化失败后脚本留在 frame 目录继续 ``make -i``，
    若这里只检查“某处有 build_root”，递归 make 仍会真正启动。
    """
    unrelated = tmp_path / "frame"
    unrelated.mkdir()
    st = _mk_state()

    blocked = sb._activity_path_role_guard(
        st, "make -j2", cwd=str(unrelated))

    assert blocked is not None
    assert "invalid_build_root" in blocked["error"]


def test_scope_guard_error_without_state_stays_silent():
    """纯构造用法（单测/无 state）不发事件，也不炸。"""
    err = sb._scope_guard_error("cmd", "protected_source_tree", "r", "/x")
    assert err["status"] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# 依赖安装路径：项目 run 必须将环境安装留在 workspace 内，
# 不得再把 ~/.local 作为跨项目共享前缀。
# ─────────────────────────────────────────────────────────────────────────────

def test_user_home_block_points_at_project_local_environment():
    """拦用户目录时明确要求在项目的 run_root/build_root 内安装。"""
    target = str(Path.home() / ".local/share/OpenCL/vendors/nvidia.icd")
    err = sb._scope_guard_error(
        f"echo libcuda.so > {target}", "unknown_absolute",
        "absolute path is not declared writable", target)["error"]

    assert "run_root" in err and "build_root" in err
    assert "不要安装到 `~/.local`" in err
    assert "平台管理员" in err


def test_non_home_block_has_no_dependency_hint():
    """普通源码树拦截不该被塞进依赖安装引导。"""
    err = sb._scope_guard_error(
        "rm -rf /srv/src", "protected_source_tree", "immutable", "/srv/src")["error"]
    assert "📦" not in err


@pytest.mark.parametrize("path", ["/", "/usr", "/etc", "/var", str(Path.home())])
def test_system_roots_cannot_be_declared_writable_dependency_root(path):
    """可写 dependency_root 不能变成绕过 guard 的万能后门。"""
    st = _mk_state()
    st.hook_state["path_roles"] = {
        "dependency_root": {"path": path, "writable": True}}
    roles = [r for r in collect_path_roles(st) if r.role == "dependency_root"]
    assert roles and roles[0].writable is False


def test_project_run_rejects_declared_home_dependency_root():
    """项目 run 不得把 ~/.local 变成共享的可写依赖安装前缀。"""
    st = _mk_state()
    # An unbound sandbox is deliberately run-local; bind this test to a project workspace.
    st.workspace_root = Path(st.project_root) / "workspace" / "experiments"
    dep = Path.home() / ".local"
    st.hook_state["path_roles"] = {
        "dependency_root": {"path": str(dep), "writable": True}}

    scope, _ = sb._classify_write_path(str(dep / "share/OpenCL/vendors/nvidia.icd"), st)
    assert scope == "outside_project_workspace"
    # 声明用户目录不得顺带打开系统目录
    assert sb._classify_write_path("/etc/OpenCL/vendors/nvidia.icd", st)[0] != "safe"


# ── 人工批准桥接：scope_guard 不再把可授权的环境路径变成永久 error ────────

def _approve_scope_pause(
    st: State, command: str, *, tool: str = "safe_run_bash",
) -> None:
    """模拟 pause_driver resume 后的 always-on highrisk_confirm hook。"""
    ctx = HookContext(
        harness=NodeHarness(node_type="experiment", system_prompt="test"),
        state=st, turn=2,
        messages=[
            LLMMessage(role="tool", tool_call_id="scope-1",
                       name=tool, content="批准执行一次"),
        ],
    )
    _highrisk_confirm_on_turn_start(ctx)


def test_full_safe_run_bash_consumes_scope_and_highrisk_grants_once(monkeypatch):
    """端到端工具顺序：scope pause → 人答复 → 一次执行，不实际碰 /etc。"""
    st = _mk_state()
    command = "sudo tee /etc/harness-test-opencl-full.icd"
    route = {
        "schema_version": 2,
        "goal": "验证环境路径授权与高危授权只消费一次",
        "evidence_refs": ["operator-provided-regression-case"],
        "steps": [{
            "id": "environment_change",
            "goal": "执行已明确授权的单次环境变更",
            "after": [],
            "action": {"tool": "safe_run_bash", "program": "sudo"},
            "effects": ["workspace_write"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }
    declared = asyncio.run(_declare_execution_route(st, route=route))
    assert declared["status"] == "success"
    calls: list[str] = []

    async def fake_exec(state, cmd, timeout=600, cwd=None, **_kwargs):
        calls.append(cmd)
        return {"status": "success", "stdout_tail": "fake", "stderr_tail": ""}

    monkeypatch.setattr(sb, "_exec_and_log", fake_exec)
    monkeypatch.setattr(sb._te, "bash_analyzer_unavailable_reason", lambda: None)
    monkeypatch.setattr(sb._te, "looks_backgrounded", lambda cmd: False)
    first = asyncio.run(sb._safe_run_bash(st, command))
    assert first["status"] == "pause" and not calls

    _approve_scope_pause(st, command)
    second = asyncio.run(sb._safe_run_bash(st, command))
    assert second["status"] == "success" and calls == [command]
    assert dangerous_commands.is_confirmed(st, command) is False



def test_safe_write_file_requires_scope_before_writer_or_parent_creation(
    tmp_path,
    monkeypatch,
):
    st = State.new("experiment", tmp_path)
    target = tmp_path / "not-created" / "sentinel.txt"
    calls = []

    async def forbidden(*_args, **_kwargs):
        calls.append(1)
        return {"status": "success"}

    monkeypatch.setattr(sb, "_orig_write_file", forbidden)
    result = asyncio.run(sb._safe_write_file(st, str(target), "x"))

    assert result["reason"] == "experiment_scope_classification_required"
    assert calls == []
    assert target.parent.exists() is False


def test_safe_write_file_uses_same_path_authorization(monkeypatch):
    st = _mk_state()
    target = "/etc/harness-test-write-file.conf"

    async def fake_write(state, path, content, create_dirs=True, **kwargs):
        return {"status": "success", "path": path}

    monkeypatch.setattr(sb, "_orig_write_file", fake_write)
    first = asyncio.run(sb._safe_write_file(st, target, "x"))
    assert first["status"] == "pause"

    _approve_scope_pause(st, f"write_file(path={target!r})")
    second = asyncio.run(sb._safe_write_file(st, target, "x"))
    assert second["status"] == "success"
    assert asyncio.run(sb._safe_write_file(st, target, "x"))["status"] == "pause"


def test_python_scope_guard_does_not_offer_unenforceable_global_authorization():
    st = _mk_state()
    code = (
        "from pathlib import Path\n"
        "Path(\"/etc/harness-test-python.conf\").write_text(\"x\")"
    )

    blocked = sb._scope_guard_python(st, code)

    assert blocked is not None and blocked["status"] == "error"
    assert blocked["blocker"]["scope"] == "protected_global_env"
    assert "pause_event" not in blocked


def test_python_scope_guard_does_not_treat_subprocess_argv_as_write_target():
    """A read-only child-process probe must not become a /usr write request."""
    st = _mk_state()
    code = (
        "import subprocess\n"
        "subprocess.run(['/usr/bin/python', '--version'], check=False)"
    )
    assert sb._scope_guard_python(st, code) is None
    highrisk = sb._unified_highrisk_gate(
        st, code, mode="python", tool="safe_execute_python", kind="python")
    assert highrisk is not None and highrisk["status"] == "pause"


def test_python_scope_guard_does_not_treat_open_read_mode_as_write():
    st = _mk_state()
    assert sb._scope_guard_python(
        st, "with open('/etc/hosts', 'r') as f:\n    print(f.read())") is None


def test_python_scope_guard_detects_literal_direct_global_write():
    st = _mk_state()
    code = (
        "from pathlib import Path\n"
        "Path(\"/etc/harness-test.conf\").write_text(\"x\")"
    )

    blocked = sb._scope_guard_python(st, code)

    assert blocked is not None and blocked["status"] == "error"
    assert blocked["blocker"]["target"] == "/etc/harness-test.conf"


def test_python_scope_guard_detects_path_open_write_mode():
    st = _mk_state()
    code = (
        "from pathlib import Path\n"
        "Path(\"/etc/harness-test.conf\").open(\"w\")"
    )

    blocked = sb._scope_guard_python(st, code)

    assert blocked is not None and blocked["status"] == "error"


def test_external_cwd_capability_is_obtainable_through_public_hitl(
    monkeypatch,
):
    home = Path(os.environ["HARNESS_FRAMEWORK_HOME"])
    state = State.new("experiment", home / "unbound-runs")
    external = _mkdtemp_non_tmp("hf-external-cwd-")
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(external), "writable": True},
    }
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Exercise the declared external bash cwd capability regression.",
        "stage": "diagnostic",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="Exercise the declared external bash cwd capability without scientific conclusions.",
    ))
    assert classified["status"] == "success", classified
    from core import project_workspace, sandbox

    monkeypatch.setattr(
        project_workspace, "validate_tool_cwd",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("outside")),
    )
    monkeypatch.setattr(
        sandbox, "model_tool_roots",
        lambda _state: ([Path(state.root)], []),
    )
    calls = []

    async def fake_exec(_state, cmd, **_kwargs):
        calls.append(cmd)
        return {"status": "success", "stdout_tail": str(external)}

    monkeypatch.setattr(sb, "_exec_and_log", fake_exec)
    first = asyncio.run(sb._safe_run_bash(
        state, "pwd", cwd=str(external), timeout=10,
    ))
    assert first["status"] == "pause", first
    assert first["pause_event"]["metadata"]["scope"] == (
        "path_capability_required"
    )
    assert calls == []

    _approve_scope_pause(state, "pwd")
    second = asyncio.run(sb._safe_run_bash(
        state, "pwd", cwd=str(external), timeout=10,
    ))
    assert second["status"] == "success", second
    assert calls == ["pwd"]
    assert str(external.resolve()) in state.hook_state[
        "_approved_subprocess_write_roots"
    ]


def test_external_python_cwd_capability_is_obtainable_through_public_hitl(
    monkeypatch,
) -> None:
    home = Path(os.environ["HARNESS_FRAMEWORK_HOME"])
    state = State.new("experiment", home / "unbound-python-runs")
    external = _mkdtemp_non_tmp("hf-external-python-cwd-")
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(external), "writable": True},
    }
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Exercise the declared external Python cwd capability regression.",
        "stage": "diagnostic",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="Exercise the declared external Python cwd capability without scientific conclusions.",
    ))
    assert classified["status"] == "success", classified
    from core import project_workspace, sandbox

    monkeypatch.setattr(
        project_workspace, "validate_tool_cwd",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("outside")),
    )
    monkeypatch.setattr(
        sandbox, "model_tool_roots",
        lambda _state: ([Path(state.root)], []),
    )
    calls = []

    async def fake_exec(_state, command, **_kwargs):
        calls.append(command)
        return {
            "status": "success",
            "stdout_tail": "ok",
            "stderr_tail": "",
            "returncode": 0,
        }

    monkeypatch.setattr(sb, "_exec_and_log", fake_exec)
    code = """print("ok")"""
    first = asyncio.run(sb._safe_execute_python(
        state, code, cwd=str(external), timeout=10,
    ))

    assert first["status"] == "pause", first
    assert first["pause_event"]["metadata"]["scope"] == (
        "path_capability_required"
    )
    assert first["pause_event"]["metadata"]["tool"] == "safe_execute_python"
    assert "工具：safe_execute_python" in first["pause_event"]["context"]
    assert calls == []

    _approve_scope_pause(state, code, tool="safe_execute_python")
    second = asyncio.run(sb._safe_execute_python(
        state, code, cwd=str(external), timeout=10,
    ))

    assert second["status"] == "success", second
    assert len(calls) == 1
    assert str(external.resolve()) in state.hook_state[
        "_approved_subprocess_write_roots"
    ]


def test_python_source_pause_names_the_registered_public_tool(
    tmp_path,
) -> None:
    state = _mk_state("python-source-tool-name")
    worktree = tmp_path / "source-worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    target = worktree / "model.py"
    state.hook_state["path_roles"] = {
        "source_worktree_root": {
            "path": str(worktree),
            "writable": True,
        },
    }
    code = (
        "from pathlib import Path\n"
        f"""Path({str(target)!r}).write_text("changed")"""
    )

    first = sb._scope_guard_python(state, code)

    assert first is not None and first["status"] == "pause"
    assert first["pause_event"]["metadata"]["tool"] == "safe_execute_python"
    assert "工具：safe_execute_python" in first["pause_event"]["context"]

    _approve_scope_pause(state, code, tool="safe_execute_python")
    assert sb._scope_guard_python(state, code) is None


@pytest.mark.parametrize(
    "command",
    [
        "git -c diff.external=/tmp/hf-sentinel diff --ext-diff",
        "git log --ext-diff -p",
        "git show --textconv HEAD",
        "rg --pre /tmp/hf-sentinel needle .",
        "rg --pre=/tmp/hf-sentinel needle .",
    ],
)
def test_public_bash_blocks_embedded_process_launchers_before_spawn(
    monkeypatch,
    command,
) -> None:
    state = _mk_state("embedded-launcher")
    calls = []

    async def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("内嵌启动器必须在任何执行入口前拒绝")

    monkeypatch.setattr(sb, "_exec_and_log", forbidden)
    monkeypatch.setattr(sb, "_orig_run_bash", forbidden)

    result = asyncio.run(sb._safe_run_bash(state, command, timeout=10))

    assert result["status"] == "error"
    assert result["blocker"]["kind"] == "embedded_process_launcher"
    assert calls == []


def test_framework_guard_exception_blocks_payload_even_with_bypass(monkeypatch):
    """框架守卫崩了必须 fail-closed，且 bypass 打不开它。

    合并说明：本用例原先打的是本地 bash 路径上的 exec 预检；判决拆除·第三波把
    那道预测失败墙降格成了事后诊断（rc=127/126 附 exec_diagnosis），所以它在
    这条路上已经不存在。守的那条不变量没变，改打同一位置仍在的 build gate ——
    `_framework_guard_failure` 是同一个包裹器。（提交链上的 exec 预检仍在，
    见 test_resource_manager 的 payload 预检用例。）
    """
    state = _mk_state("exec-preflight-failure")
    runtime = sb.experiment_output_dir(state, "runtime", create=True)
    spawned = []

    def broken_guard(*_args, **_kwargs):
        raise RuntimeError("build gate crashed")

    async def forbidden_executor(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("payload must not start after exec preflight failure")

    monkeypatch.setattr(dangerous_commands, "bypass_enabled", lambda: True)
    monkeypatch.setattr(sb, "_build_gate", broken_guard)
    monkeypatch.setattr(sb, "_exec_and_log", forbidden_executor)

    result = asyncio.run(sb._safe_run_bash(
        state, "echo exec-check", cwd=str(runtime)))

    assert result["status"] == "error"
    assert result["reason"] == "build_gate_unavailable"
    assert result["blocker"]["guard"] == "build_gate"
    assert result["blocker"]["source"] == "build_gate"
    assert result["blocker"]["bypass_allowed"] is False
    assert spawned == []

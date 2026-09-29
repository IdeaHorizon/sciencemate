"""runtime ABI/toolchain consistency preflight 测试（experiment 节点本地）。

定位（2026-07-13 设计定稿）：抓 缺库 / 混 MPI / 错环境 启动，非形式化证明。
判据分层：编译档案对账 > soname×launcher --version 族判定 > 前缀差异（warning）。
关键约束回归：ldd 必须在命令将用的环境（行内 LD_LIBRARY_PATH）下跑；仅 ELF。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import platform
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import sandbox
from core.state import State
from nodes.experiment.tools import safe_bash as sb
from nodes.experiment.tools.run_contract import _classify_experiment_scope
from shared.lib import dangerous_commands


def _preflight_sync(*args, **kwargs):
    """同步壳：链路随执行器分档转 async（探针经咽喉 spawn_and_wait）。"""
    return asyncio.run(sb._runtime_abi_preflight_bash(*args, **kwargs))


def _probe_sync(*args, **kwargs):
    return asyncio.run(sb._sandbox_probe(*args, **kwargs))


requires_sandbox = pytest.mark.skipif(
    not sandbox.availability()[0],
    reason="mandatory Docker sandbox is unavailable",
)


# ── 纯解析函数 ───────────────────────────────────────────────────────────────

LDD_MISSING = """\
\tlinux-vdso.so.1 (0x00007fff)
\tlibadios2_fortran_mpi.so.2.11 => not found
\tlibmpi.so.12 => /opt/mpich-4.2/lib/libmpi.so.12 (0x00007f1)
\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f2)
"""

LDD_CLEAN_OPENMPI = """\
\tlibmpi.so.40 => /opt/openmpi-5.0/lib/libmpi.so.40 (0x00007f1)
\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f2)
"""


def test_parse_ldd_missing_and_libmpi():
    missing, libmpi = sb._parse_ldd(LDD_MISSING)
    assert missing == ["libadios2_fortran_mpi.so.2.11"]
    assert libmpi.endswith("libmpi.so.12")


def test_family_from_soname():
    assert sb._family_from_soname("/opt/openmpi/lib/libmpi.so.40") == "openmpi"
    assert sb._family_from_soname("/opt/mpich/lib/libmpi.so.12") == "mpich"
    assert sb._family_from_soname("/opt/intel/impi/lib/libmpi.so") == "intel"
    assert sb._family_from_soname("/x/libfoo.so") is None


def test_family_from_version_output():
    assert sb._family_from_version("mpirun (Open MPI) 5.0.3") == "openmpi"
    assert sb._family_from_version("HYDRA build details:\n  Version: 4.2") == "mpich"
    assert sb._family_from_version("Intel(R) MPI Library for Linux") == "intel"
    assert sb._family_from_version("unknown launcher") is None


def test_cmd_ld_library_path_extraction():
    cmd = sb._expand_cmd_vars(
        'W="/data/ws"\nenv LD_LIBRARY_PATH="$W/lib:/opt/x/lib" ./solver.exe')
    assert sb._cmd_ld_library_path(cmd) == "/data/ws/lib:/opt/x/lib"
    assert sb._cmd_ld_library_path("./solver.exe") is None


def test_install_prefix_strips_bin_lib():
    assert sb._install_prefix("/opt/openmpi-5.0/bin/mpirun") == "/opt/openmpi-5.0"
    assert sb._install_prefix("/opt/mpich/lib/libmpi.so.12") == "/opt/mpich"


def test_is_elf(tmp_path):
    script = tmp_path / "x.sh"
    script.write_text("#!/bin/sh\n")
    assert not sb._is_elf(str(script))       # 脚本不是 ELF —— 各平台都成立


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason=("系统二进制是 ELF 只在 Linux 成立：macOS 的 /bin/ls 是 Mach-O "
            "(0xcafebabe)，_is_elf 正确地返 False。ABI preflight 本身面向 HPC "
            "Linux；这里只跳过'系统二进制必为 ELF'这条平台相关断言。"),
)
def test_is_elf_on_real_system_binary():
    assert sb._is_elf("/bin/ls" if os.path.exists("/bin/ls") else "/usr/bin/ls")


# ── 集成（monkeypatch 子进程层）─────────────────────────────────────────────

def _mk_state() -> State:
    home = Path(os.environ["HARNESS_FRAMEWORK_HOME"])
    base = home / "projects" / "proj-abi" / "runs"
    base.mkdir(parents=True, exist_ok=True)
    return State.new(node_type="experiment", base_dir=base, project_id="proj-abi")


def _fake_elf(d: Path, name: str) -> Path:
    p = d / name
    p.write_bytes(b"\x7fELF" + b"\x00" * 12)
    p.chmod(0o755)
    return p


@pytest.fixture()
def ws(tmp_path):
    return tmp_path


def test_missing_lib_warns_with_hint_and_lets_reality_refuse(ws, monkeypatch):
    """判决拆除（sb:2093 删，2026-08-31，专审二）：预测失败降为 warning。

    缺库如实写进 runtime_preflight_warning（含「单个目录」领域 hint），命令放行，
    链接器自己拒绝。"""
    st = _mk_state()
    binary = _fake_elf(ws, "solver.exe")
    monkeypatch.setattr(sb, "_run_ldd", lambda state, b, lp, cwd: LDD_MISSING)
    r = _preflight_sync(st, f'cd "{ws}" && ./solver.exe')
    assert r is None
    tr = (Path(st.root) / "transcript.jsonl").read_text()
    assert "runtime_preflight_warning" in tr
    assert "libadios2_fortran_mpi.so.2.11" in tr
    assert "单个目录" in tr
    assert str(binary) or True


def test_ldd_uses_command_env(ws, monkeypatch):
    """行内 env LD_LIBRARY_PATH 必须传给 ldd —— 合法补库启动不得误拦。"""
    st = _mk_state()
    _fake_elf(ws, "solver.exe")
    seen = {}

    def fake_ldd(state, binary, ld_path, cwd):
        seen["ld_path"] = ld_path
        return LDD_CLEAN_OPENMPI
    monkeypatch.setattr(sb, "_run_ldd", fake_ldd)
    cmd = f'W="{ws}"\ncd "$W"\nenv LD_LIBRARY_PATH="$W/lib" ./solver.exe'
    assert _preflight_sync(st, cmd) is None
    assert seen["ld_path"] == f"{ws}/lib"


def test_mixed_mpi_family_warns_not_blocks(ws, monkeypatch):
    st = _mk_state()
    _fake_elf(ws, "solver.exe")
    launcher = _fake_elf(ws, "mpirun")
    monkeypatch.setattr(sb, "_run_ldd", lambda state, b, lp, cwd: LDD_MISSING.replace(
        "libadios2_fortran_mpi.so.2.11 => not found",
        "libfoo.so => /lib/libfoo.so (0x1)"))       # mpich 链接、无缺库
    monkeypatch.setattr(sb, "_resolve_launcher", lambda n: str(launcher))
    monkeypatch.setattr(sb, "_launcher_version_output",
                        lambda state, l, cwd: "mpirun (Open MPI) 5.0.3")
    r = _preflight_sync(st, f'cd "{ws}" && mpirun -np 4 ./solver.exe')
    # 判决拆除（sb:2121 删）：混族降为 warning；最坏后果由超时熔断（A 类）接管。
    assert r is None
    tr = (Path(st.root) / "transcript.jsonl").read_text()
    assert "runtime_preflight_warning" in tr and "不同族" in tr


def test_same_family_different_prefix_warns_not_blocks(ws, monkeypatch):
    st = _mk_state()
    _fake_elf(ws, "solver.exe")
    launcher = _fake_elf(ws, "mpirun")
    monkeypatch.setattr(sb, "_run_ldd", lambda state, b, lp, cwd: LDD_CLEAN_OPENMPI)
    monkeypatch.setattr(sb, "_resolve_launcher", lambda n: str(launcher))
    monkeypatch.setattr(sb, "_launcher_version_output",
                        lambda state, l, cwd: "mpirun (Open MPI) 5.0.3")
    assert _preflight_sync(
        st, f'cd "{ws}" && mpirun -np 4 ./solver.exe') is None
    tr = (Path(st.root) / "transcript.jsonl").read_text()
    assert "runtime_preflight_warning" in tr


def test_build_archive_mismatch_warns_not_blocks(ws, monkeypatch):
    """判决拆除（sb:2109 删）：编译档案不一致降为 warning，命令放行。"""
    st = _mk_state()
    st.hook_state["build_mpi_prefix"] = "/opt/openmpi-5.0"
    _fake_elf(ws, "solver.exe")
    launcher = _fake_elf(ws, "mpirun")
    monkeypatch.setattr(sb, "_run_ldd", lambda state, b, lp, cwd: LDD_CLEAN_OPENMPI)
    monkeypatch.setattr(sb, "_resolve_launcher", lambda n: str(launcher))
    r = _preflight_sync(
        st, f'cd "{ws}" && mpirun -np 4 ./solver.exe')
    assert r is None
    tr = (Path(st.root) / "transcript.jsonl").read_text()
    assert "runtime_preflight_warning" in tr and "编译档案" in tr


def test_host_verifiable_roots_scope_skips_image_internal_targets(
    ws, monkeypatch,
):
    """E-14 回归：提交链给出受管挂载点后，范围外目标不做宿主机视角 ldd。

    镜像自带路径的 ABI 事实在镜像里；宿主机上同名文件（若存在）是另一个
    二进制，拿它的 ldd 结果下结论既可能误拦也可能误放。"""
    st = _mk_state()
    outside = ws / "image-view"
    outside.mkdir()
    _fake_elf(outside, "solver.exe")
    called: list[str] = []
    monkeypatch.setattr(
        sb, "_run_ldd",
        lambda state, b, lp, cwd: called.append(b) or LDD_MISSING)

    managed = ws / "managed"
    managed.mkdir()
    assert _preflight_sync(
        st, f'{outside / "solver.exe"}',
        host_verifiable_roots=[str(managed)]) is None
    assert called == []

    # 同一目标落在受管挂载点内时，**探针照跑**（收窄的是判定范围不是探测范围）。
    # 判决拆除 sb:2093/2109/2121 后本函数恒返回 None，缺库改记 warning：
    # host_verifiable_roots 的作用因此变成「决定要不要花代价探这个目标」，
    # 而不是「决定拦不拦」。用 called 来验范围，比用返回值更贴合拆除后的语义。
    _fake_elf(managed, "solver.exe")
    assert _preflight_sync(
        st, f'{managed / "solver.exe"}',
        host_verifiable_roots=[str(managed)]) is None
    assert called == [str(managed / "solver.exe")]
    events = st.transcript_path.read_text(encoding="utf-8")
    assert "runtime_preflight_warning" in events
    assert "libadios2_fortran_mpi.so.2.11" in events

    # 不给 host_verifiable_roots 时全盘按宿主机判定：探针照样落到镜像外目标上。
    called.clear()
    assert _preflight_sync(st, f'{outside / "solver.exe"}') is None
    assert called == [str(outside / "solver.exe")]

    # 范围算不出来（空列表）时 fail-closed 回宿主机口径 —— 探针照跑，不静默跳过。
    # （拆除后"fail-closed"的含义从"拦住"变成"仍然探、仍然记"，防线是账不是墙。）
    called.clear()
    assert _preflight_sync(
        st, f'{managed / "solver.exe"}', host_verifiable_roots=[]) is None
    assert called == [str(managed / "solver.exe")]


def test_exec_preflight_host_verifiable_roots_scope(ws):
    """`_exec_preflight_bash` 的同构开关：范围外目标不做存在性/执行位判定。"""
    managed = ws / "managed"
    managed.mkdir()
    absent_outside = ws / "image-view" / "python3.12"
    absent_inside = managed / "solver"

    assert sb._exec_preflight_bash(
        f"{absent_outside} run.py",
        host_verifiable_roots=[str(managed)]) is None
    blocked = sb._exec_preflight_bash(
        f"{absent_inside} --case x", host_verifiable_roots=[str(managed)])
    assert blocked is not None and "可执行目标不存在" in blocked["error"]
    # 默认（None）＝全盘按宿主机判定，本地 bash 主路径行为不变。
    assert sb._exec_preflight_bash(f"{absent_outside} run.py") is not None

    # 镜像自带的系统树：范围里有没有它都不判（E-14 的活体形态）。
    image_argv0 = f"/usr/local/bin/python3.12-{uuid.uuid4().hex}"
    assert not os.path.exists(image_argv0)
    for roots in ([str(managed)], [str(managed), "/usr"], []):
        assert sb._exec_preflight_bash(
            f"{image_argv0} run.py", host_verifiable_roots=roots) is None
    # 空范围＝范围不可知：fail-closed 回宿主机口径，不是把守卫关掉。
    blocked_empty = sb._exec_preflight_bash(
        f"{absent_inside} --case x", host_verifiable_roots=[])
    assert blocked_empty is not None
    assert "可执行目标不存在" in blocked_empty["error"]


def test_non_elf_and_bare_names_skipped(ws, monkeypatch):
    st = _mk_state()
    (ws / "run.sh").write_text("#!/bin/sh\n")
    (ws / "run.sh").chmod(0o755)
    called = []
    monkeypatch.setattr(sb, "_run_ldd",
                        lambda state, b, lp, cwd: called.append(b) or LDD_CLEAN_OPENMPI)
    assert _preflight_sync(st, f'cd "{ws}" && ./run.sh') is None
    assert _preflight_sync(st, "make -j4 && ls bin/") is None
    assert called == []   # 非 ELF / 裸命令名从未触发 ldd


def _probe_test_state(tmp_path: Path) -> tuple[State, Path]:
    state = State.new("experiment", tmp_path / "probe-state")
    runtime = sb.experiment_output_dir(state, "runtime", create=True)
    state.hook_state["path_roles"] = {
        "experiment_root": str(state.root),
        "run_root": str(runtime),
    }
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Exercise the runtime ABI preflight regression.",
        "stage": "diagnostic",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="environment_probe",
        reason="Exercise the declared runtime ABI preflight without scientific conclusions.",
    ))
    assert classified["status"] == "success", classified
    return state, runtime


def test_sandbox_probe_uses_only_exact_scratch_as_writable_root(
    tmp_path,
    monkeypatch,
):
    state, runtime = _probe_test_state(tmp_path)
    seen = {}
    cleaned = []

    def fake_prepare(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return SimpleNamespace(
            argv=["attempt-request-client"],
            cleanup=lambda: cleaned.append(True),
        )

    def fake_run(argv, **kwargs):
        seen["request_argv"] = argv
        seen["request_kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="probe-ok\n", stderr="")

    monkeypatch.setattr(sb, "_ensure_hardened_attempt_manifest", lambda _state: object())
    # 本测试钉的是 image 路径的 prepare 契约（roots 计算与两条路径共用）；
    # 原生后端的真跑覆盖在 test_sandbox_probe_real_hardened_ldd_is_read_only。
    from core import isolation
    monkeypatch.setattr(
        isolation, "attempt_capability",
        lambda backend=None: {"backend": "image", "image_id": "x" * 64,
                              "security_profile": "hardened"})
    monkeypatch.setattr(sandbox, "prepare_attempt_command", fake_prepare)
    monkeypatch.setattr(sb.subprocess, "run", fake_run)

    result = _probe_sync(state, ["ldd", "/bin/true"], str(runtime))

    assert result == "probe-ok\n"
    scratch = (runtime / ".abi-probe-scratch").resolve()
    assert seen["writable_roots"] == [scratch]
    assert scratch not in seen["readonly_roots"]
    assert runtime.resolve() in seen["readonly_roots"]
    assert Path(state.root).resolve() in seen["readonly_roots"]
    assert seen["limits"].walltime_seconds == 15
    assert seen["request_kwargs"]["timeout"] == 20
    assert seen["request_kwargs"]["check"] is False
    assert seen["environment"]["TMPDIR"] == str(scratch)
    assert "TMPDIR=" + str(scratch) in seen["argv"]
    assert "TMP=" + str(scratch) in seen["argv"]
    assert "TEMP=" + str(scratch) in seen["argv"]
    assert seen["argv"][0] == "/usr/bin/env"
    assert cleaned == [True]


def test_sandbox_probe_semantic_role_without_capability_cannot_expand_roots(
    tmp_path,
    monkeypatch,
):
    state = State.new("experiment", tmp_path / "probe-state")
    semantic_only = tmp_path / "semantic-only"
    semantic_only.mkdir()
    state.hook_state["path_roles"] = {
        "experiment_root": str(state.root),
        "run_root": str(semantic_only),
    }
    prepared = []

    def forbidden_prepare(*_args, **_kwargs):
        prepared.append(True)
        raise AssertionError("unauthorized probe must not prepare an Attempt request")

    monkeypatch.setattr(sb, "_ensure_hardened_attempt_manifest", lambda _state: object())
    monkeypatch.setattr(sandbox, "prepare_attempt_command", forbidden_prepare)

    result = _probe_sync(state, ["ldd", "/bin/true"], str(semantic_only))

    assert result["status"] == "error"
    assert result["reason"] == "path_capability_required"
    assert result["blocker"]["guard"] == "runtime_abi_probe"
    assert result["blocker"]["source"] == "bash_sandbox_roots"
    assert result["blocker"]["bypass_allowed"] is False
    assert prepared == []


def test_sandbox_probe_rejects_portable_profile_before_request(
    tmp_path,
    monkeypatch,
):
    state, runtime = _probe_test_state(tmp_path)
    prepared = []

    def portable(_state):
        raise RuntimeError(
            "hardened_sandbox_profile_required: portable keeps broad mounts writable"
        )

    def forbidden_prepare(*_args, **_kwargs):
        prepared.append(True)
        raise AssertionError("portable profile must not prepare an Attempt request")

    monkeypatch.setattr(sb, "_ensure_hardened_attempt_manifest", portable)
    monkeypatch.setattr(sandbox, "prepare_attempt_command", forbidden_prepare)

    result = _probe_sync(state, ["ldd", "/bin/true"], str(runtime))

    assert result["status"] == "error"
    assert result["reason"] == "hardened_sandbox_profile_required"
    assert result["blocker"]["source"] == "attempt_manifest"
    assert result["blocker"]["bypass_allowed"] is False
    assert prepared == []


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_sandbox_probe_request_failure_is_closed_and_cleaned(
    tmp_path,
    monkeypatch,
    failure,
):
    state, runtime = _probe_test_state(tmp_path)
    cleaned = []

    monkeypatch.setattr(sb, "_ensure_hardened_attempt_manifest", lambda _state: object())
    # 同上一测试：钉 image 路径验证失败收口契约（两条路径共用同一收口）。
    from core import isolation
    monkeypatch.setattr(
        isolation, "attempt_capability",
        lambda backend=None: {"backend": "image", "image_id": "x" * 64,
                              "security_profile": "hardened"})
    monkeypatch.setattr(
        sandbox,
        "prepare_attempt_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            argv=["attempt-request-client"],
            cleanup=lambda: cleaned.append(True),
        ),
    )

    def failed_request(_argv, **_kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                cmd=["attempt-request-client"], timeout=20)
        return SimpleNamespace(
            returncode=9,
            stdout="",
            stderr="trusted launcher failed",
        )

    monkeypatch.setattr(sb.subprocess, "run", failed_request)

    result = _probe_sync(state, ["ldd", "/bin/true"], str(runtime))

    assert result["status"] == "error"
    assert result["reason"] == "runtime_abi_probe_unavailable"
    assert result["blocker"]["bypass_allowed"] is False
    assert result["blocker"]["source"] in {
        "attempt_request_timeout",
        "attempt_request_exit",
    }
    assert cleaned == [True]


def test_runtime_abi_checker_exception_blocks_payload_even_with_bypass(
    tmp_path,
    monkeypatch,
):
    state, runtime = _probe_test_state(tmp_path)
    spawned = []

    def broken_abi(*_args, **_kwargs):
        raise RuntimeError("abi checker crashed " + "x" * 800)

    async def forbidden_executor(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("payload must not start after ABI checker failure")

    monkeypatch.setattr(dangerous_commands, "bypass_enabled", lambda: True)
    monkeypatch.setattr(sb, "_runtime_abi_preflight_bash", broken_abi)
    monkeypatch.setattr(sb, "_exec_and_log", forbidden_executor)

    result = asyncio.run(sb._safe_run_bash(
        state, "echo abi-check", cwd=str(runtime)))

    assert result["status"] == "error"
    assert result["reason"] == "runtime_abi_preflight_unavailable"
    assert result["blocker"]["guard"] == "runtime_abi_preflight"
    assert result["blocker"]["bypass_allowed"] is False
    assert len(result["blocker"]["detail"]) <= 401
    assert spawned == []


@pytest.fixture
def real_probe_state(tmp_path):
    state, runtime = _probe_test_state(tmp_path)
    try:
        yield state, runtime
    finally:
        manifest = getattr(state, "sandbox_manifest", None)
        if isinstance(manifest, dict) and manifest.get("attempt_id"):
            attempt_id = str(manifest["attempt_id"])
            evicted = sandbox.evict_state_attempt(state)
            leaked = [
                row for row in sandbox.list_attempt_instances()
                if row.get("attempt_id") == attempt_id
            ]
            assert evicted or not leaked, (
                "production ABI probe Attempt still exists: " + attempt_id
            )


@requires_sandbox
def test_sandbox_probe_real_hardened_ldd_is_read_only(real_probe_state):
    state, runtime = real_probe_state
    sentinel = runtime / "probe-must-not-write"
    assert not sentinel.exists()

    result = _probe_sync(
        state,
        ["/usr/bin/ldd", "/bin/true"],
        str(runtime),
    )

    assert isinstance(result, str), result
    assert "libc.so" in result
    # PR C 后 "hardened" 的定义即「逐命令写边界」：原生后端逐命令构造墙，
    # manifest v4 的 profile 恒为 hardened（PR B 短暂用过 "native" 一词，已并回）。
    frozen = sandbox.parse_manifest(state.sandbox_manifest)
    assert frozen.security_profile == "hardened"
    assert not sentinel.exists()

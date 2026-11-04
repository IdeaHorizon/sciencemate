"""执行目标（RFC_WINDOWS_EXECUTION_TARGETS D1/D2/D9）：命令在本机还是在 WSL 发行版里跑。

## 现场

#1263（lujy，10-28/29）：Windows 个人版的受管命令跑在 Low IL 下，``wsl.exe`` 一律
``Wsl/E_ACCESSDENIED``；用户想在本机 Ubuntu 里跑，产品没有路。方案：目标由人选，worker
（Medium IL）经 ``wsl.exe`` 进发行版，墙在对面（linux 后端）。

这里钉住四件事：写法与谁说了算；选了到不了的目标**绝不回落本机**；路径与 argv 怎么翻；
冻进 attempt 的「谁来守」带着目标。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import execution_target as et
from core import isolation
from core.isolation import CommandSpec, Invariant, IsolationContractError
from core.isolation.wsl import WslBackend


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(et.ENV, raising=False)
    et._reset_for_tests()
    isolation._reset_for_tests()
    yield
    et._reset_for_tests()
    isolation._reset_for_tests()


# ── 写法与谁说了算 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [None, "", "local", "  local "])
def test_absent_or_local_means_this_machine(raw) -> None:
    assert et.parse(raw) == et.LOCAL


@pytest.mark.parametrize("raw,spec", [("wsl:Ubuntu-24.04", "wsl:Ubuntu-24.04"),
                                      ("ssh:hpc_login.1", "ssh:hpc_login.1")])
def test_named_targets_round_trip(raw, spec) -> None:
    assert et.parse(raw).spec == spec


@pytest.mark.parametrize("raw", ["wsl:", "wsl", "wsl:a b", "wsl:../etc", "docker:x", "wsl:x;rm",
                                 "local:x", 3])
def test_bad_spellings_are_refused_not_read_as_local(raw) -> None:
    with pytest.raises(et.ExecutionTargetError):
        et.parse(raw)


def test_platform_declaration_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(et.ENV, "wsl:FromEnv")
    assert et.current().spec == "wsl:FromEnv"
    et.declare("wsl:Declared")
    assert et.current().spec == "wsl:Declared"
    # None = 这条请求没谈目标：保持原样，不清掉
    et.declare(None)
    assert et.current().spec == "wsl:Declared"
    et.declare("local")
    assert et.current().is_local


def test_a_misspelt_environment_target_is_an_error(monkeypatch) -> None:
    monkeypatch.setenv(et.ENV, "wsl")
    with pytest.raises(et.ExecutionTargetError):
        et.current()


# ── 到不了的目标绝不回落本机 ───────────────────────────────────────────────────


def test_ssh_target_is_refused_instead_of_running_here() -> None:
    et.declare("ssh:hpc")
    with pytest.raises(IsolationContractError, match="not implemented yet"):
        isolation.select_backend()


def test_the_throat_reports_spawn_failed_for_an_unreachable_target(tmp_path) -> None:
    et.declare("ssh:hpc")
    state = SimpleNamespace(kill_event=None, append_transcript=lambda *a, **k: None)
    from shared.lib.cancellable_subprocess import spawn_and_wait

    status, rc, _out, err = asyncio.run(spawn_and_wait(
        "/bin/sh", "-c", f"echo ran > {tmp_path}/ran", state=state, timeout=10,
        writable_roots=[tmp_path]))
    assert status == "spawn_failed" and rc is None
    assert b"not implemented yet" in err
    assert not (tmp_path / "ran").exists(), "命令在本机跑了 —— 目标到不了时绝不能回落"


def test_wsl_target_off_windows_has_no_wall_and_says_why() -> None:
    et.declare("wsl:Ubuntu-24.04")
    backend = isolation.select_backend()
    assert isinstance(backend, WslBackend)
    if __import__("sys").platform != "win32":
        assert backend.capabilities() == frozenset()
        assert "Windows host" in backend.unavailable_reason
        spec = CommandSpec(argv=("/bin/true",), cwd="/", writable_roots=(Path("/tmp"),))
        with pytest.raises(IsolationContractError, match="write boundary"):
            backend.prepare(spec, state=None)


# ── 冻进 attempt 的「谁来守」 ─────────────────────────────────────────────────


def test_attempt_capability_freezes_the_target() -> None:
    frozen = isolation.attempt_capability(target="wsl:Ubuntu-24.04")
    assert frozen == {"backend": "wsl:Ubuntu-24.04"}
    local = isolation.attempt_capability(target="local")["backend"]
    assert ":" not in local and local == isolation.native_backend_name()
    et.declare("wsl:Ubuntu-24.04")
    assert isolation.attempt_capability() == {"backend": "wsl:Ubuntu-24.04"}


def test_the_enforcement_record_names_the_target() -> None:
    native = isolation.select_backend()
    assert isolation.enforcement_record(native).as_event()["target"] == "local"


# ── 路径与 argv ────────────────────────────────────────────────────────────────


def _ready_backend(monkeypatch, tmp_path: Path) -> WslBackend:
    backend = WslBackend("Ubuntu-24.04")
    backend._probed = True
    backend._mount_prefix = "/mnt/"
    backend._entry_linux = "/mnt/c/afs/core/isolation/_wsl_entry.py"
    backend._report = {"caps": ["write_boundary", "git_unwritable", "net_deny", "walltime",
                                "group_kill"], "net_scope": "all"}
    return backend


def test_windows_paths_map_into_the_distro(monkeypatch, tmp_path) -> None:
    b = _ready_backend(monkeypatch, tmp_path)
    assert b.to_linux(r"C:\Users\a b\run") == "/mnt/c/Users/a b/run"
    assert b.to_linux("D:/data/x.h5") == "/mnt/d/data/x.h5"
    assert b.to_linux("C:\\") == "/mnt/c"
    assert b.to_linux(r"\\wsl.localhost\Ubuntu-24.04\home\lulu") == "/home/lulu"
    assert b.to_linux(r"\\wsl$\ubuntu-24.04\opt") == "/opt"
    with pytest.raises(IsolationContractError):
        b.to_linux(r"\\wsl.localhost\Debian\home")
    with pytest.raises(IsolationContractError):
        b.to_linux("relative/path")


def test_managed_shell_and_interpreter_become_the_distros_own(monkeypatch, tmp_path) -> None:
    import sys

    import shared.lib.shell as shell

    b = _ready_backend(monkeypatch, tmp_path)
    monkeypatch.setattr(shell, "bash_shell", lambda: r"C:\Program Files\Git\usr\bin\bash.exe")
    monkeypatch.setattr(shell, "posix_shell", lambda: r"C:\Program Files\Git\usr\bin\bash.exe")
    monkeypatch.setattr(sys, "executable", r"D:\ScienceMate\Resources\python\python.exe")
    body = 'echo "C:\\keep\\me" > out.txt'
    assert b._linux_argv((r"C:\Program Files\Git\usr\bin\bash.exe", "-o", "pipefail", "-c", body)) \
        == ["/bin/bash", "-o", "pipefail", "-c", body], "命令正文一个字不动"
    assert b._linux_argv((r"D:\ScienceMate\Resources\python\python.exe", r"C:\run\train.py")) \
        == ["python3", "/mnt/c/run/train.py"]
    assert b._linux_argv(("make", "-j4")) == ["make", "-j4"]


def test_prepare_hands_over_a_spec_file_not_a_command_line(monkeypatch, tmp_path) -> None:
    b = _ready_backend(monkeypatch, tmp_path)
    original = b.to_linux

    def to_linux(path):  # 本机的临时目录不是 Windows 路径：测试里把它也翻过去
        text = str(path)
        return f"/mnt/x{text}" if text.startswith("/") else original(path)

    monkeypatch.setattr(b, "to_linux", to_linux)
    limits = SimpleNamespace(memory_bytes=1 << 30, pids=64, cpus=2.0, walltime_seconds=30)
    spec = CommandSpec(argv=("make",), cwd=r"C:\run\exp", writable_roots=(Path(r"C:\run\exp"),),
                       readonly_roots=(Path(r"C:\data"),), limits=limits)
    launch = b.prepare(spec, state=None)
    try:
        assert launch.argv[1:6] == ["-d", "Ubuntu-24.04", "--cd", "/", "--exec"]
        assert launch.argv[-2] == "run" and launch.cwd_in_argv
        assert launch.stop_argv[-2] == "kill"
        handed = json.loads((launch.scratch_dir / "spec.json").read_text(encoding="utf-8"))
        assert handed["argv"] == ["make"] and handed["network"] is False
        assert handed["cwd"] == "/mnt/c/run/exp"
        assert handed["writable"] == ["/mnt/c/run/exp"] and handed["readonly"] == ["/mnt/c/data"]
        assert handed["limits"]["memory_bytes"] == 1 << 30
        assert handed["ident"] == launch.stop_argv[-1]
        assert not any(k.upper().endswith("API_KEY") for k in launch.env)
    finally:
        launch.cleanup()
    assert not launch.scratch_dir.exists()


def test_capabilities_follow_the_probe_and_need_the_write_boundary(monkeypatch, tmp_path) -> None:
    b = _ready_backend(monkeypatch, tmp_path)
    assert Invariant.NET_DENY in b.capabilities()
    b._report = {"caps": ["net_deny", "walltime"], "reason": "bubblewrap is missing"}
    assert b.capabilities() == frozenset()


# ── 开局说明 ───────────────────────────────────────────────────────────────────


def test_the_orientation_says_commands_run_in_the_distro() -> None:
    from core import execution_context_facts as ecf

    snapshot = {"backend": "wsl", "enforced": ["write_boundary", "net_deny", "group_kill"],
                "missing_for_unattended": ["mem_cap", "pids_cap"], "missing_for_attended": []}
    target = {"target": "wsl:Ubuntu-24.04", "distro": "Ubuntu 24.04.3 LTS",
              "kernel": "6.6.87.2-microsoft-standard-WSL2", "python": "3.12.3",
              "mount_prefix": "/mnt/"}
    text = ecf.render_section(ecf.gather(snapshot=snapshot, platform="win32", target=target,
                                         os_text="Windows-11"))
    assert "`wsl:Ubuntu-24.04`" in text and "Ubuntu 24.04.3 LTS" in text
    assert "/bin/bash" in text and "python3" in text
    assert "/mnt/c/" in text and "interop" in text
    assert "不可达" not in text, "Windows 本机那条「wsl.exe 不可达」不能出现在 WSL 目标上"


def test_the_orientation_says_an_unreachable_target_will_not_fall_back() -> None:
    from core import execution_context_facts as ecf

    snapshot = {"backend": None, "enforced": [], "missing_for_unattended": [],
                "missing_for_attended": [], "unavailable_reason": "x"}
    text = ecf.render_section(ecf.gather(
        snapshot=snapshot, platform="win32", os_text="Windows-11",
        target={"target": "wsl:Gone", "error": "distro 'Gone' is not usable"}))
    assert "`wsl:Gone`" in text and "到不了" in text and "不会悄悄改在本机跑" in text


def test_the_orientation_says_a_wall_that_cannot_stand_up_is_unreachable() -> None:
    """9800x3d 实测：发行版在、但没装 bubblewrap —— 开局说明得直说「起不来、为什么、怎么修」，
    不能只列一串缺的不变量（那一行模型读不出结论）。"""
    from core import execution_context_facts as ecf

    snapshot = {"backend": "wsl", "enforced": [], "missing_for_unattended": ["write_boundary"],
                "missing_for_attended": ["write_boundary"]}
    target = {"target": "wsl:Ubuntu-24.04", "write_boundary": False, "distro": "Ubuntu 24.04.3 LTS",
              "reason": "bubblewrap is missing in this distro; Fix: sudo apt install bubblewrap"}
    text = ecf.render_section(ecf.gather(snapshot=snapshot, platform="win32", target=target,
                                         os_text="Windows-11"))
    assert "到不了" in text and "sudo apt install bubblewrap" in text
    assert "interop" not in text, "墙没立起来时不该再念那几条「在发行版里怎么用」"


def test_distro_paths_map_back_to_windows_for_write_judges(monkeypatch, tmp_path) -> None:
    b = _ready_backend(monkeypatch, tmp_path)
    assert b.to_windows("/mnt/c/Users/a b/run/out.txt") == r"C:\Users\a b\run\out.txt"
    assert b.to_windows("/mnt/d") == "D:\\"
    assert b.to_windows("/home/lulu/x") is None and b.to_windows("/mnt/wsl/x") is None
    assert b.to_windows(b.to_linux(r"C:\run\exp")) == r"C:\run\exp"

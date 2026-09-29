"""载荷没起来，得由墙的最内层说出来 —— 咽喉据此报 spawn_failed。

## 现场（2026-09-15，node20，release d852a6f）

PATH 缺 ``~/.local/bin``（latexmk 住那儿），writing 编译走 systemd-run → Landlock 启动器 →
``os.execvp("latexmk")``。启动器自己起来了，exec 才 ENOENT：它以 Python traceback +
非零码退出。咽喉看到的是 ``status=done, rc≠0`` —— 和"latexmk 跑完拒绝了稿子"**一模一样**，
于是 `_compile_latex` 报「LaTeX 编译失败 / 按 stderr_tail 改源码」，stderr_tail 里是一段
``_landlock_exec.py line 163 os.execvp … FileNotFoundError``。PR#915 要消掉的误归属，原样回来。

bwrap（``bwrap: execvp x: …``，rc 1）、sandbox-exec（``execvp() of 'x' failed``，rc 71，
本机实测）同病：它们自己 execvp 载荷失败，只留一行方言 stderr 和一个普通非零码。

## 修法

每条隔离链的**最内层**都是我们自己的代码（Landlock 启动器 / ``_exec.py`` 垫片 / win32
启动器）。exec 不成，它按 ``core.isolation.LAUNCHER_ERROR_MARKER`` 的约定在 stderr 说一句
``HARNESS_ISOLATION_ERROR exec_failed:…``；咽喉 ``spawn_and_wait`` 读到那一行就报
``spawn_failed``（returncode None —— 载荷一次都没跑，没有退出码可言）。

## 这套测试钉什么

* **真起启动器**（不 stub、不比字符串）：给它一个不存在的 argv0，它必须说那一句；
  给它一个存在的，它必须原样 exec。启动器 ``-I`` 跑在墙内、不 import core，标记是各写
  一份的字面量 —— 一致性只能靠真起一次让 ``launcher_refusal`` 读到。
* **真走咽喉 + 真后端**：argv0 不存在 → ``("spawn_failed", None)``；存在 → ``("done", 0)``
  （对照组，[[feedback_baseline_can_vanish]]）；载荷**自己**以 127 退出 → 仍是
  ``("done", 127)``（垫片不许把载荷的退出码冒充成"没起来"）。

变异判据：把 ``_exec.py`` 里的 try/except 拆掉（裸 execvp），darwin / bwrap 那两条红；
把咽喉里 ``launcher_refusal`` 那段删掉，全部红；把 darwin.py 里 ``exec_shim_argv()`` 拿掉，
darwin 上"不存在的 argv0"那条红。
"""
from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from core import isolation
from core.isolation import Invariant, LAUNCHER_ERROR_MARKER, launcher_refusal, select_backend
from shared.lib.cancellable_subprocess import spawn_and_wait

_ISOLATION_DIR = Path(isolation.__file__).parent
_EXEC_SHIM = _ISOLATION_DIR / "_exec.py"
_LANDLOCK_EXEC = _ISOLATION_DIR / "_landlock_exec.py"
_POSIX = os.name == "posix"
#: 一个在任何机器上都不该存在的裸命令名。
_NO_SUCH = "hf-no-such-command-3f9c1a"


def _launch(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, capture_output=True, timeout=30, check=False)


# ── 1. 启动器本体：exec 不成要说那一句 ───────────────────────────────────────────

@pytest.mark.skipif(not _POSIX, reason="_exec.py 只进 bwrap / seatbelt 链（POSIX）")
def test_exec_shim_reports_a_payload_it_could_not_exec():
    out = _launch([sys.executable, "-I", "-B", str(_EXEC_SHIM), "--", _NO_SUCH, "--flag"])
    assert out.returncode == 127, (out.returncode, out.stderr)
    line = launcher_refusal(out.stderr)
    assert line is not None, f"垫片没按约定说话，咽喉读不到：{out.stderr!r}"
    assert line.startswith(f"{LAUNCHER_ERROR_MARKER} exec_failed:{_NO_SUCH}:ENOENT:"), line
    assert b"Traceback" not in out.stderr, "traceback 是给我们看的，不是给下游归属用的"


@pytest.mark.skipif(not _POSIX, reason="_exec.py 只进 bwrap / seatbelt 链（POSIX）")
def test_exec_shim_execs_a_real_payload_verbatim():
    # 对照组：存在的命令必须原样 exec —— argv、退出码、stdout 一个不改。
    out = _launch([sys.executable, "-I", "-B", str(_EXEC_SHIM), "--",
                   sys.executable, "-c", "import sys; print('argv', sys.argv[1:]); sys.exit(3)",
                   "a", "b c"])
    assert out.returncode == 3, out.stderr
    assert out.stdout.strip() == b"argv ['a', 'b c']"
    assert launcher_refusal(out.stderr) is None


@pytest.mark.skipif(not _POSIX, reason="_exec.py 只进 bwrap / seatbelt 链（POSIX）")
def test_exec_shim_reports_a_script_whose_interpreter_is_missing(tmp_path: Path):
    # "在 PATH 上、却起不来"的真实形状：shebang 指的解释器不在。execvp 给的也是 ENOENT，
    # 而它**不是**"这台机器没装工具"—— 下游靠 which 再分，这里只负责说"没起来"。
    script = tmp_path / "latexmk"
    script.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    out = _launch([sys.executable, "-I", "-B", str(_EXEC_SHIM), "--", str(script)])
    assert out.returncode == 127
    line = launcher_refusal(out.stderr)
    assert line is not None and f"exec_failed:{script}:ENOENT" in line, out.stderr


def _landlock_abi() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    out = _launch([sys.executable, "-I", "-B", str(_LANDLOCK_EXEC), "abi"])
    try:
        return int(out.stdout.strip() or 0)
    except ValueError:
        return 0


@pytest.mark.skipif(_landlock_abi() < 1, reason="Landlock 启动器只在有 Landlock 的内核上能跑")
def test_landlock_launcher_reports_a_payload_it_could_not_exec(tmp_path: Path):
    """node20 那次的真身：启动器自己起来了、关进了规则集，exec 载荷才 ENOENT。"""
    import json

    roots = json.dumps([str(tmp_path)])
    argv = [sys.executable, "-I", "-B", str(_LANDLOCK_EXEC), "--network", "allow", roots, "--"]
    out = _launch([*argv, _NO_SUCH])
    assert out.returncode == 127, (out.returncode, out.stderr)
    line = launcher_refusal(out.stderr)
    assert line is not None, f"Landlock 启动器 exec 失败没按约定说话：{out.stderr!r}"
    assert line.startswith(f"{LAUNCHER_ERROR_MARKER} exec_failed:{_NO_SUCH}:ENOENT:"), line
    assert b"Traceback" not in out.stderr

    # 对照：同一条启动器、存在的载荷 → 原样 exec，退出码是载荷的。
    ok = _launch([*argv, sys.executable, "-c", "raise SystemExit(5)"])
    assert ok.returncode == 5, ok.stderr
    assert launcher_refusal(ok.stderr) is None


# ── 2. 咽喉 + 真后端：那一句要变成 spawn_failed ────────────────────────────────

class _State:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.kill_event = None

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append({"event": event_type, **payload})


@pytest.fixture()
def native_wall(monkeypatch, tmp_path: Path):
    """这台机器上真能守写边界的原生后端；守不住就跳过（不是失败：这里测的是归属）。"""
    name = isolation.native_backend_name()
    if name is None:
        pytest.skip(f"no native backend for {sys.platform}")
    monkeypatch.setenv(isolation.EXECUTOR_ENV, name)
    isolation._reset_for_tests()
    backend = select_backend(name)
    if Invariant.WRITE_BOUNDARY not in backend.capabilities():
        isolation._reset_for_tests()
        pytest.skip(f"native backend {name} cannot enforce the write boundary here: "
                    f"{getattr(backend, 'unavailable_reason', '')}")
    yield tmp_path
    isolation._reset_for_tests()


def _run(*argv: str, root: Path):
    return asyncio.run(spawn_and_wait(
        *argv, state=_State(), timeout=60, cwd=str(root), writable_roots=[root]))


def test_a_missing_argv0_is_spawn_failed_through_the_real_wall(native_wall: Path):
    status, rc, _out, err = _run(_NO_SUCH, "--version", root=native_wall)
    assert (status, rc) == ("spawn_failed", None), (status, rc, err)
    assert launcher_refusal(err) is not None, err
    assert b"Traceback" not in err, "咽喉交出去的 stderr 不该是一段 Python traceback"


def test_a_real_payload_still_runs_through_the_same_wall(native_wall: Path):
    # 对照组：最内层多了一层垫片之后，正常命令必须照旧跑通、退出码照旧是它自己的。
    status, rc, out, err = _run(sys.executable, "-c", "print('ok')", root=native_wall)
    assert (status, rc) == ("done", 0), err
    assert out.strip() == b"ok"


def test_a_payload_that_exits_127_itself_is_not_called_a_spawn_failure(native_wall: Path):
    # 127 是 shell 的"command not found"，也是我们启动器的失败码 —— 但判据不是退出码，
    # 是启动器那一句话。载荷自己退 127，那就是它自己的事。
    status, rc, _out, err = _run(sys.executable, "-c", "raise SystemExit(127)", root=native_wall)
    assert (status, rc) == ("done", 127), err
    assert launcher_refusal(err) is None


@pytest.mark.skipif(not _POSIX, reason="shebang 是 POSIX 的事")
def test_a_script_with_a_missing_interpreter_is_spawn_failed(native_wall: Path):
    script = native_wall / "tool"
    script.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    status, rc, _out, err = _run(str(script), root=native_wall)
    assert (status, rc) == ("spawn_failed", None), (status, rc, err)
    assert launcher_refusal(err) is not None


# ── 3. 契约本身 ───────────────────────────────────────────────────────────────

def test_launcher_refusal_only_reads_a_line_that_starts_with_the_marker():
    assert launcher_refusal(b"") is None
    assert launcher_refusal(b"latexmk: ! Undefined control sequence.\n") is None
    # 载荷输出里**提到**这个词不算 —— 只认行首。
    assert launcher_refusal(b"echo HARNESS_ISOLATION_ERROR is a marker\n") is None
    line = f"{LAUNCHER_ERROR_MARKER} exec_failed:latexmk:ENOENT:No such file or directory"
    assert launcher_refusal(f"bwrap noise\n{line}\n".encode()) == line
    assert launcher_refusal(line) == line

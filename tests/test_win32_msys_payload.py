r"""Windows：原生进程起 MSYS bash，要替它做 login shell 才做的两件事（#1203 #1204）。

载荷是 Git for Windows 的 ``usr\bin\bash.exe``（模型的 shell、本地作业脚本）时：

* **它自己的 ``/usr/bin`` 上 PATH，只给这个子进程**（#1203）。作业脚本里的 ``mkdir``、
  ``cut``、``find``、``sort`` 都在那里；全局 PATH 上没有它，脚本退 127。也不许放进全局：
  后端里按 PATH 找 ``find``/``sort`` 的会拿到 MSYS 版。
* **命令行按 Cygwin 规则加引号**（#1204）。MSYS 运行时自己把命令行切回 argv，
  ``list2cmdline`` 交过去，``printf '%s\n' 'a\\b'`` 打出的是 ``a\b``，静默。

前半段在任何平台上都跑：走真入口（``Win32Backend.prepare`` → ``_win32_exec.main``），
只把 Windows 系统调用换成替身、截下交给 ``CreateProcessAsUserW`` 的那行命令行。
后半段只在 Windows 真机上跑：真 Low-IL 墙、真 MSYS bash，断言落在载荷真打出来的字节上。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import isolation
from core.isolation import CommandSpec, Invariant, _win32_exec, select_backend
from core.isolation._native import payload_environment
from core.isolation.win32 import Win32Backend
from shared.lib import shell


@pytest.fixture(autouse=True)
def _the_launcher_chdir_stays_in_this_test():
    """启动器的 ``run()`` 会 ``os.chdir(spec["cwd"])``（#943：它自己进 cwd）。这里在进程内
    直接调它的 ``main()``，那一下 chdir 改的是**整个 pytest worker** 的当前目录 —— 同一个
    worker 里后面拿相对路径读仓库文件的测试全红（CI 上 gw4 红了 3 条）。跑完放回去。"""
    before = os.getcwd()
    yield
    os.chdir(before)


def _fake_git(tmp_path: Path) -> Path:
    """Git for Windows 的布局：``usr/bin`` 里是 MSYS 程序（与 msys-2.0.dll 同目录），
    ``cmd`` 里是原生的 git.exe。返回 bash.exe。"""
    usr_bin = tmp_path / "Git" / "usr" / "bin"
    usr_bin.mkdir(parents=True)
    (usr_bin / "msys-2.0.dll").write_bytes(b"")
    (usr_bin / "bash.exe").write_bytes(b"")
    (tmp_path / "Git" / "cmd").mkdir()
    (tmp_path / "Git" / "cmd" / "git.exe").write_bytes(b"")
    return usr_bin / "bash.exe"


def _prepare(argv: tuple[str, ...], tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    launch = Win32Backend().prepare(
        CommandSpec(argv=argv, cwd=str(work), writable_roots=(work,)), state=None)
    return launch


def isolation_cleanup(launch) -> None:
    import shutil

    if launch.scratch_dir is not None:
        shutil.rmtree(launch.scratch_dir, ignore_errors=True)


def _spec_of(launch) -> dict:
    run = launch.argv.index("run")
    return json.loads(launch.argv[run + 1])


@pytest.fixture()
def windows(monkeypatch):
    """在非 Windows 上也验判据与接线：强制 shell 走 Windows 分支（同 test_shell_resolver）。"""
    monkeypatch.setattr(shell, "_WINDOWS", True)


# ── 判据：什么算 MSYS 程序 ─────────────────────────────────────────────────────


def test_an_msys_program_is_recognised_by_the_runtime_beside_it(tmp_path, windows) -> None:
    bash = _fake_git(tmp_path)
    assert shell.msys_runtime_bin(str(bash)) == str(bash.parent)
    # 原生的 git.exe 旁边没有 MSYS 运行时；WSL 的 bash.exe 也没有 —— 都不是。
    assert shell.msys_runtime_bin(str(tmp_path / "Git" / "cmd" / "git.exe")) is None
    assert shell.msys_runtime_bin(r"C:\Windows\System32\bash.exe") is None
    assert shell.msys_runtime_bin(sys.executable) is None


def test_posix_has_no_msys_programs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(shell, "_WINDOWS", False)
    bash = _fake_git(tmp_path)
    assert shell.msys_runtime_bin(str(bash)) is None


# ── #1203：MSYS 的 /usr/bin 只进这个子进程的 PATH ─────────────────────────────


def test_msys_bash_gets_its_own_usr_bin_right_after_the_interpreter(tmp_path, windows) -> None:
    bash = _fake_git(tmp_path)
    before = os.environ.get("PATH", "")
    launch = _prepare((str(bash), "-c", "mkdir -p out"), tmp_path)
    try:
        path = launch.env["PATH"].split(os.pathsep)
        # 解释器目录仍在最前（模型敲的 python3 是带依赖的那个），紧跟着是 MSYS 的 /usr/bin，
        # 排在继承来的 Windows PATH（System32 的 FIND.EXE / SORT.EXE）之前。
        assert path[:2] == [str(Path(sys.executable).parent), str(bash.parent)]
        assert path[2:] == [p for p in before.split(os.pathsep) if p]
        # 只给这个子进程：本进程（后端）的 PATH 一个字不动。
        assert os.environ.get("PATH", "") == before
        assert str(bash.parent) not in before.split(os.pathsep)
        assert _spec_of(launch)["msys"] is True
    finally:
        isolation_cleanup(launch)


def test_a_native_payload_keeps_the_path_it_always_had(tmp_path, windows) -> None:
    launch = _prepare((sys.executable, "-c", "pass"), tmp_path)
    try:
        expected = payload_environment(None, scratch_dir=launch.scratch_dir)["PATH"]
        assert launch.env["PATH"] == expected
        assert _spec_of(launch)["msys"] is False
    finally:
        isolation_cleanup(launch)


# ── #1204：交给 CreateProcessAsUserW 的命令行 ──────────────────────────────────


class _Kernel32:
    """``_spawn_low_and_wait`` 用到的 kernel32 —— 全部成功、退出码 0。"""

    def __getattr__(self, name):
        return lambda *args, **kwargs: 1


class _Advapi32:
    def __init__(self) -> None:
        self.command_lines: list[str] = []

    def CreateProcessAsUserW(self, token, app, cmdline, *rest) -> int:  # noqa: N802
        assert app is None, "程序由命令行第一个 token 决定"
        self.command_lines.append(ctypes.wstring_at(ctypes.addressof(cmdline)))
        return 1


def _launcher_command_line(launch, monkeypatch) -> str:
    """把 prepare 交出的启动器 argv 原样喂给启动器的 main()，截下它起载荷用的命令行。"""
    advapi = _Advapi32()
    monkeypatch.setattr(_win32_exec, "_win32", lambda: (_Kernel32(), advapi))
    monkeypatch.setattr(_win32_exec, "_icacls", lambda path, level: None)
    monkeypatch.setattr(_win32_exec, "_make_job", lambda k, **limits: None)
    monkeypatch.setattr(_win32_exec, "_low_il_primary_token", lambda a: None)
    script = next(i for i, part in enumerate(launch.argv) if part.endswith("_win32_exec.py"))
    assert _win32_exec.main(launch.argv[script:]) == 0
    assert len(advapi.command_lines) == 1
    return advapi.command_lines[0]


# 右边这几行在 Windows 真机（Git for Windows bash 5.3）上原样交给 CreateProcess 验过：bash 收到
# 的那个参数与左边的命令一字不差。下面 Windows 那段的 round-trip 测试是同一个判据的真跑。
_MODEL_COMMANDS = {
    r"""printf '%s\n' 'a\\b'""": r'''"printf '%s\\n' 'a\\\\b'"''',
    r"""python3 -c 'import re, sys; print(re.sub(r"\\d", "#", sys.argv[1]))' 'a\d1'""":
        r'''"python3 -c 'import re, sys; print(re.sub(r\"\\\\d\", \"#\", sys.argv[1]))' 'a\\d1'"''',
    r"""printf '%s\n' "C:\\Users\\fcbay\\x y\\data.csv" *.txt""":
        r'''"printf '%s\\n' \"C:\\\\Users\\\\fcbay\\\\x y\\\\data.csv\" *.txt"''',
}


@pytest.mark.parametrize("command", sorted(_MODEL_COMMANDS))
def test_the_launcher_quotes_the_model_command_the_way_msys_splits_it(
    command, tmp_path, windows, monkeypatch
) -> None:
    bash = _fake_git(tmp_path)
    launch = _prepare((str(bash), "-o", "pipefail", "-c", command), tmp_path)
    try:
        line = _launcher_command_line(launch, monkeypatch)
    finally:
        isolation_cleanup(launch)
    head = subprocess.list2cmdline([str(bash)])
    assert line == f'{head} "-o" "pipefail" "-c" {_MODEL_COMMANDS[command]}'
    # 旧路（MSVC 规则）交出的那一行，正是真机上丢反斜杠的那一行。
    assert line != subprocess.list2cmdline([str(bash), "-o", "pipefail", "-c", command])


def test_a_native_payload_still_gets_the_msvc_command_line(tmp_path, windows, monkeypatch) -> None:
    payload = (sys.executable, "-c", r"print('a\\b')", 'say "hi"')
    launch = _prepare(payload, tmp_path)
    try:
        line = _launcher_command_line(launch, monkeypatch)
    finally:
        isolation_cleanup(launch)
    assert line == subprocess.list2cmdline(list(payload))


# ── Windows 真机：真 Low-IL 墙、真 MSYS bash ───────────────────────────────────

_on_windows = sys.platform == "win32"
_caps = select_backend("win32").capabilities() if _on_windows else frozenset()
windows_only = pytest.mark.skipif(
    not _on_windows or Invariant.WRITE_BOUNDARY not in _caps,
    reason="needs the win32 backend on a real Windows host",
)


class _State:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.kill_event = None

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append({"event": event_type, **payload})


@pytest.fixture()
def win32_backend(monkeypatch):
    monkeypatch.setenv(isolation.EXECUTOR_ENV, "win32")
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


def _model_shell(command: str, work: Path) -> tuple[str, int | None, bytes, bytes]:
    """模型的 shell 那条路：safe_bash 交给咽喉的就是 ``bash -o pipefail -c <命令>``。"""
    from shared.lib.cancellable_subprocess import spawn_and_wait

    return asyncio.run(spawn_and_wait(
        shell.bash_shell(), "-o", "pipefail", "-c", command, state=_State(), timeout=60,
        cwd=str(work), writable_roots=[work], readonly_roots=[],
    ))


@windows_only
def test_backslashes_reach_the_model_shell_byte_exact(tmp_path, win32_backend) -> None:
    work = tmp_path / "work"
    work.mkdir()
    data = work / "in.txt"
    data.write_text("from a windows path\n", encoding="utf-8")
    escaped = str(data).replace("\\", "\\\\\\\\")   # bash 双引号里 \\\\ → \\，python 里 \\ → \
    cases = {
        r"printf '%s\n' 'a\\b'": "a\\\\b\n",
        r'''python3 -c 'import re, sys; print(re.sub(r"\\d", "#", sys.argv[1]))' 'a\d1' ''':
            "a#1\n",
        f'''python3 -c "print(open('{escaped}', encoding='utf-8').read(), end='')"''':
            "from a windows path\n",
        r"printf '%s\n' '\\fileserver\share\data.csv'": "\\\\fileserver\\share\\data.csv\n",
    }
    for command, expected in cases.items():
        status, rc, out, err = _model_shell(command, work)
        assert (status, rc) == ("done", 0), (command, err.decode(errors="replace"))
        assert out.decode("utf-8").replace("\r\n", "\n") == expected, command


@windows_only
def test_every_argument_reaches_msys_bash_unchanged(tmp_path) -> None:
    """启动器的引号规则对真 MSYS 运行时逐条 round-trip（引号、盘符、通配、@文件、中文、换行）。"""
    (tmp_path / "rsp").write_text("RESP_X RESP_Y", encoding="utf-8")
    (tmp_path / "a1").write_text("", encoding="utf-8")
    args = [
        "", "a b", "a\\b", "a\\\\b", "C:\\a\\b", "C:\\a\\", "\\\\server\\share", "\"", "a\"b",
        "\\\"", "C:\\a\"b", "@rsp", "~", "~/x", "{a,b}", "*", "a?", "[a]", "'", "it's",
        "x\ny", "\t", "中文\\\\路径", "$HOME", "%PATH%", "a&b|c<d>e^f", "-o",
    ]
    argv = [shell.bash_shell(), "-c", 'printf "%s\\0" "$@"', "_", *args]
    out = subprocess.run(_win32_exec._command_line(argv, msys=True), cwd=tmp_path,
                         capture_output=True, check=True).stdout
    assert out.split(b"\0")[:-1] == [a.encode("utf-8") for a in args]


@windows_only
def test_a_job_script_finds_coreutils_without_them_on_the_global_path(
    tmp_path, win32_backend, monkeypatch
) -> None:
    """本地作业的形状：``bash <脚本>``。全局 PATH 上没有 Git 的 usr\\bin（box 的默认 PATH、
    安装好的应用都是这样），脚本里的 coreutils 仍然找得到，而且是 MSYS 那一份。"""
    from shared.lib.cancellable_subprocess import spawn_and_wait

    global_path = [p for p in os.environ.get("PATH", "").split(os.pathsep)
                   if p and not p.lower().rstrip("\\").endswith("\\usr\\bin")]
    monkeypatch.setenv("PATH", os.pathsep.join(global_path))
    work = tmp_path / "work"
    work.mkdir()
    script = work / "job.sh"
    script.write_bytes(
        b"set -eo pipefail\n"
        b"mkdir -p out/sub\n"
        b"printf 'b:2\\na:1\\n' | sort | cut -d: -f1 > out/sub/names\n"
        b"find out -name names\n"
        b"cat out/sub/names\n"
        b"command -v find\n"
        b"command -v sort\n"
        b"sort --version | head -n 1\n"
        b"find --version | head -n 1\n"
    )
    status, rc, out, err = asyncio.run(spawn_and_wait(
        shell.bash_shell(), str(script), state=_State(), timeout=60,
        cwd=str(work), writable_roots=[work], readonly_roots=[],
    ))
    text = out.decode("utf-8", errors="replace").replace("\r\n", "\n")
    assert (status, rc) == ("done", 0), err.decode("utf-8", errors="replace")
    lines = text.splitlines()
    assert lines[:5] == ["out/sub/names", "a", "b", "/usr/bin/find", "/usr/bin/sort"]
    assert "GNU coreutils" in lines[5] and "GNU findutils" in lines[6]
    # 只进了那个子进程：后端自己的 PATH 上仍然没有它。
    assert os.environ["PATH"] == os.pathsep.join(global_path)

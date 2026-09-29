"""墙拒掉了程序的真家，就得同时给它一个 —— ``core.isolation._native.payload_home``。

缘起（2026-09-23，一台干净的 Windows）：随包 tectonic 每次编译 5 秒就 ``os error 5``。
它编译前第一件事是在 ``%LOCALAPPDATA%\\TectonicProject\\Tectonic\\formats`` 建目录，这个
位置它经系统接口（Known Folder）问出来，不看任何环境变量；Low-IL 的墙写不进真
``%LOCALAPPDATA%``。开发机上那个目录早在第一次在墙外跑 tectonic 时就建好了，所以这个
缺陷在开发机上从来没露面。之前给它指的 ``TECTONIC_CACHE_DIR`` 只盖住了包缓存。

修法不按程序点名：平台自带、自成一体的程序声明 ``home="own"``，墙给它一个完整的家（按本平台
原生约定，fontconfig 的字体缓存、tectonic 的 formats 都在那里），判据按**本平台的原生约定**：
程序往操作系统说的那个缓存位置写，东西要落进持久缓存；家里的其余东西每条命令都是新的。

反过来那一半同样要钉住：用户的程序（默认 ``home="host"``）要看见用户真实的家。第一版把家
一律换掉，elan 按 ``~/.elan`` 找不到 Lean 工具链，check_lean 当场全挂。

这些测试真起进程、真过墙（本机的原生后端），不 mock 墙。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from core import isolation
from core.isolation import Invariant, select_backend
from core.isolation._native import cache_scratch, payload_environment
from shared.lib.cancellable_subprocess import spawn_and_wait

NATIVE = isolation.native_backend_name()


class _State:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.kill_event = None

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append({"event": event_type, **payload})


@pytest.fixture(autouse=True)
def _native(monkeypatch):
    if NATIVE is not None:
        monkeypatch.setenv(isolation.EXECUTOR_ENV, NATIVE)
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


def _backend_ready() -> bool:
    if NATIVE is None:
        return False
    try:
        backend = select_backend(NATIVE)
    except isolation.IsolationContractError:
        return False
    return Invariant.WRITE_BOUNDARY in backend.capabilities()


live = pytest.mark.skipif(
    not _backend_ready(),
    reason=f"native backend {NATIVE} cannot enforce the write boundary on this host",
)

# 载荷：按本平台的约定问「我的缓存目录在哪」—— Windows 走 Known Folder（和 tectonic
# 一样，不看 LOCALAPPDATA 变量），macOS 是 ~/Library/Caches，其余是 ~/.cache —— 往里写，
# 再往家里的配置区写，并报告它看见了什么。
_PAYLOAD = r"""
import json, os, pathlib, sys
tag = sys.argv[1]
home = pathlib.Path(os.path.expanduser("~"))
if sys.platform == "win32":
    import ctypes, uuid
    f = ctypes.windll.shell32.SHGetKnownFolderPath
    f.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    p = ctypes.c_wchar_p()
    hr = f(uuid.UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le, 0, None, ctypes.byref(p))
    assert hr == 0, hex(hr & 0xFFFFFFFF)
    native_cache = pathlib.Path(p.value)
elif sys.platform == "darwin":
    native_cache = home / "Library" / "Caches"
else:
    native_cache = home / ".cache"
mine = native_cache / "hf-the-wall-gives-a-home"
mine.mkdir(parents=True, exist_ok=True)
(mine / tag).write_text(tag)
config = home / ".config" / "hf-the-wall-gives-a-home"
seen_config = sorted(p.name for p in config.iterdir()) if config.is_dir() else []
config.mkdir(parents=True, exist_ok=True)
(config / tag).write_text(tag)
print(json.dumps({
    "home": str(home),
    "native_cache": str(native_cache),
    "seen_cache": sorted(p.name for p in mine.iterdir()),
    "seen_config": seen_config,
}))
"""


def _run_payload(tag: str, root: Path) -> dict:
    state = _State()
    status, rc, out, err = asyncio.run(spawn_and_wait(
        sys.executable, "-c", _PAYLOAD, tag,
        state=state, timeout=120, cwd=str(root), writable_roots=[root], sandbox_home="own",
    ))
    assert status == "done" and rc == 0, (
        f"墙里的程序写不进它自己的家：status={status} rc={rc}\n{err.decode(errors='replace')[-800:]}")
    return json.loads(out.decode("utf-8").strip().splitlines()[-1])


@pytest.fixture()
def persistent_marker_dir():
    cache = cache_scratch()
    assert cache is not None, "持久缓存建不出来 —— 墙给的家没有能跨命令留下东西的地方"
    target = cache / "hf-the-wall-gives-a-home"
    shutil.rmtree(target, ignore_errors=True)
    yield target
    shutil.rmtree(target, ignore_errors=True)


@live
def test_a_programs_cache_lands_where_the_os_says_and_outlives_the_command(
        tmp_path: Path, persistent_marker_dir: Path) -> None:
    first = _run_payload("first", tmp_path)
    second = _run_payload("second", tmp_path)

    # 缓存按平台约定写进去了，并且**跨命令留下来** —— tectonic 首编 82 秒、之后 0.4 秒靠的就是这个。
    assert "first" in second["seen_cache"], (
        "上一条命令写进缓存的东西，下一条看不见 —— 每次编译都要从零下载")
    assert (persistent_marker_dir / "first").is_file(), (
        "缓存没落进持久缓存（或者命令结束清理 scratch 时顺着链接把它删了）")

    # 家本身每条命令都是新的：配置 / 数据不跨命令、不跨项目留（否则就是一条下钩子的通道）。
    assert second["seen_config"] == [], f"上一条命令的配置留到了下一条：{second['seen_config']}"
    assert first["home"] != second["home"]

    # 不是宿主的真家。
    real_home = Path(os.path.expanduser("~")).resolve()
    for seen in (first, second):
        assert Path(seen["home"]).resolve() != real_home


@live
def test_the_home_goes_away_with_the_command(tmp_path: Path, persistent_marker_dir: Path) -> None:
    seen = _run_payload("only", tmp_path)
    assert not Path(seen["home"]).exists(), "命令结束后它的家还在 —— 私有 scratch 没收"
    assert (persistent_marker_dir / "only").is_file()


@live
def test_the_ledger_says_how_far_the_home_goes(tmp_path: Path) -> None:
    record = isolation.enforcement_record(select_backend(NATIVE)).as_event()
    assert record["payload_home"] in {"container", "env_only"}
    if sys.platform != "win32":
        # POSIX 上家就是环境变量指过去的目录，没有别的间接层。
        assert record["payload_home"] == "container"


def test_the_hosts_home_vocabulary_is_replaced_not_merged(monkeypatch, tmp_path: Path) -> None:
    """宿主环境里指向真家的那些变量（含 XDG_*）要被**整套换掉**，漏一个就有程序写回真家。"""
    real = tmp_path / "the-real-home"
    for name in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                 "XDG_STATE_HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR"):
        monkeypatch.setenv(name, str(real / name.lower()))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = payload_environment(None, scratch_dir=scratch, home="own")
    home = scratch / "home"
    expected = ["HOME", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME"]
    expected += (["USERPROFILE", "APPDATA", "LOCALAPPDATA"] if sys.platform == "win32"
                 else ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"])
    for name in expected:
        assert Path(env[name]).is_relative_to(scratch), f"{name} 仍指向宿主：{env[name]}"
    assert Path(env["HOME"]) == home


def test_a_callers_explicit_choice_still_wins(tmp_path: Path) -> None:
    """``CommandSpec.environment`` 显式给的最后叠上去 —— 那是调用方自己的决定和后果。"""
    env = payload_environment({"HOME": "/chosen/by/caller"}, scratch_dir=tmp_path, home="own")
    assert env["HOME"] == "/chosen/by/caller"


# ── 用户的程序看见用户的家 ─────────────────────────────────────────────────────

_READS_ITS_TOOLCHAIN = (
    "import os, pathlib\n"
    "print(pathlib.Path(os.path.expanduser('~'), '.hf-toolchain', 'stable').read_text())\n"
)


@pytest.fixture()
def a_users_home_with_a_toolchain(monkeypatch, tmp_path):
    """elan 的形状：工具链装在用户家里，程序按 ~ 去找。"""
    home = tmp_path / "the-users-home"
    (home / ".hf-toolchain").mkdir(parents=True)
    (home / ".hf-toolchain" / "stable").write_text("lean-4.x", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE" if sys.platform == "win32" else "HOME", str(home))
    return home


@live
def test_a_users_program_still_finds_what_the_user_installed(tmp_path, a_users_home_with_a_toolchain):
    status, rc, out, err = asyncio.run(spawn_and_wait(
        sys.executable, "-c", _READS_ITS_TOOLCHAIN,
        state=_State(), timeout=120, cwd=str(tmp_path), writable_roots=[tmp_path],
    ))
    assert status == "done" and rc == 0, (
        "用户的程序在墙里找不到用户装在家里的东西（elan 找不到 Lean 就是这样）：\n"
        + err.decode(errors="replace")[-600:])
    assert out.decode().strip() == "lean-4.x"


@live
def test_a_platform_program_does_not_depend_on_the_users_home(tmp_path, a_users_home_with_a_toolchain):
    """反面：住墙给的家的程序看不见用户家里的东西 —— 它在每台机器上的行为都一样。"""
    status, rc, _out, _err = asyncio.run(spawn_and_wait(
        sys.executable, "-c", _READS_ITS_TOOLCHAIN,
        state=_State(), timeout=120, cwd=str(tmp_path), writable_roots=[tmp_path],
        sandbox_home="own",
    ))
    assert status == "done" and rc != 0


def test_host_is_the_default_and_keeps_the_users_home(monkeypatch, tmp_path: Path) -> None:
    real = tmp_path / "the-users-home"
    monkeypatch.setenv("HOME", str(real))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = payload_environment(None, scratch_dir=scratch)
    assert env["HOME"] == str(real)
    assert Path(env["TMPDIR"]).is_relative_to(scratch)
    assert env["XDG_CACHE_HOME"] == str(cache_scratch())


def test_an_unknown_home_is_a_contract_error(tmp_path: Path) -> None:
    with pytest.raises(isolation.IsolationContractError, match="valid values: host, own"):
        isolation.CommandSpec(argv=("x",), cwd=str(tmp_path), writable_roots=(tmp_path,),
                              home="mine")

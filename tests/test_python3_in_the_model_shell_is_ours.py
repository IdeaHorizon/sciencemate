"""模型在沙箱 shell 里敲 `python3`，必须是带着这个项目依赖的那一个。

## 这条测试为什么存在

2026-09-07 真机实测（Mac 安装包，第三轮 Ising 课题）：模型在 `run_bash` 里敲

    python3 -c "import numpy, matplotlib; print(...)"

拿到的是**系统 python**（macOS 上是 Xcode 3.9，没有 numpy），于是它开始满硬盘找：

    find ~/.harness-framework -maxdepth 6 -name "python*" | head -40
    === find python with numpy ===

这不是模型笨，是我们把它扔进了一个 `python3` 不指向项目解释器的 shell。
virtualenv 解决的正是这件事：把自己的 bin 放在 PATH 最前面。

## 与 #827 的关系

同一条链上有三处「用哪个 Python」：

| 谁敲的命令 | 怎么定 |
|---|---|
| 框架自己 spawn（`python_exec`） | `the_interpreter_for_model_code()` —— #827 |
| 节点写死的绝对路径（`safe_execute_python`） | 同上 —— #827 |
| **模型自己敲的 `python3`** | 改不了它敲什么，只能让那个名字在这个 shell 里指对东西 —— 本条 |

## Windows：目录放对了，名字不在

POSIX 上每种解释器布局都自带 `python3`，把目录放到 PATH 最前面就够了。Windows 上
没有一种带它（venv / python.org / uv 独立 CPython 都只有 `python.exe`），于是
`python3` 落到 `WindowsApps` 里应用商店的桩：2026-09-23 真机实测，模型 shell 里
`python3 -c ...` 一个字不打印、退出码 49。`python3_is_python` 在同一个目录里补一个
指向 `python.exe` 的硬链接 —— 也就是 POSIX venv 里 `python3 -> python` 那条链。

随包应用的解释器目录就是安装目录：装在 `C:\\Program Files` 下时运行时写不进。所以
打包器**打包时**就用同一个函数把 `python3.exe` 放进 `Resources/python/`，并列进
`INSTALL_INVENTORY`（装完缺件检查读它）；装完自检在安装位置经沙箱真敲一次 `python3`。

## POSIX venv：目录放错了，名字对

放到 PATH 最前面的曾经是 `Path(sys.executable).resolve().parent`。venv 的
`bin/python` 是指向基础解释器的链接，一跟就走出了 venv：2026-09-23 本机实测（uv 建的
`.venv`），模型 shell 里 `python3` 的 `sys.prefix` 是 uv 那份 CPython，`import numpy`
直接 ModuleNotFoundError。而这里原来的判据全是绿的 —— `samefile` 也跟着链接走，基础
解释器与 venv 的 `python` 是同一个 inode。所以判据改落在**模型要的那件事**上：它起来
的那个 `python3` 的 `sys.prefix` 就是我们的（依赖在那里），而且要在一个自己造的 venv
里验一次 —— CI 跑在 Debian 的系统 python 上，不是 venv，跟不跟链接一个样。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.isolation import _native
from core.isolation._native import payload_environment, python3_is_python


def test_the_interpreter_bin_comes_first(tmp_path: Path) -> None:
    """跑着 harness 的那个解释器**被起的那个目录**在 PATH 最前面。

    "在 PATH 里"不够 —— 系统 python 也在，而先找到谁是**顺序**决定的。也不是它顺着
    符号链接走到的目录：venv 的 ``bin/python`` 是链接，走出去就不是 venv 了。
    """
    env = payload_environment(None, scratch_dir=tmp_path)
    first = env["PATH"].split(os.pathsep)[0]
    assert first == str(Path(sys.executable).parent)


def test_the_rest_of_the_path_survives(tmp_path: Path) -> None:
    """只往前面加，不替换 —— 模型还要用 ls / grep / git 这些。"""
    original = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    env = payload_environment(None, scratch_dir=tmp_path)
    got = env["PATH"].split(os.pathsep)
    assert got[1:] == original or set(original).issubset(set(got))
    assert len(got) >= len(original)


def test_python3_in_that_path_is_the_one_that_has_our_dependencies(tmp_path: Path) -> None:
    """按这个 PATH 找到的 `python3`，就在这个进程被起的那个目录里。

    判据落在**解析结果**上，不落在"路径字符串对不对" —— 后者可以拼对而
    `python3` 这个名字在那个目录里根本不存在（比如只有 `python3.14`）。比的是目录
    而不是 ``samefile(sys.executable)``：venv 的 ``python3`` 与它链到的基础解释器是
    同一个文件，文件相同说明不了找到的是不是 venv 里那个。
    """
    import shutil

    env = payload_environment(None, scratch_dir=tmp_path)
    found = shutil.which("python3", path=env["PATH"])
    assert found is not None, "这个 PATH 上根本找不到 python3"
    assert Path(found).parent.samefile(Path(sys.executable).parent)


def _model_shell_python3(env: dict[str, str], code: str) -> str:
    """在模型的那个 shell 里敲 ``python3 -c <code>``，交回它打印的东西。"""
    from shared.lib.shell import bash_shell

    out = subprocess.run([bash_shell(), "-c", f'python3 -c "{code}"'], env=env,
                         capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0, (out.returncode, out.stderr)
    return out.stdout


def test_python3_typed_in_the_model_shell_runs_this_interpreter(tmp_path: Path) -> None:
    """模型不是在 ``shutil.which`` 里敲命令，是在 shell 里 —— Windows 上是 MSYS bash，
    它不认 PATHEXT，按自己的规矩拼 ``.exe``。判据落在那个 shell 真起来的进程上：
    它的 ``sys.prefix`` 就是这个进程的 —— 依赖装在那里，模型要的是这个。修之前在
    Windows 上这里什么都打印不出来（商店桩，退出码 49）；在 POSIX venv 里打印的是
    基础解释器的 prefix（``sys.executable`` 与它 ``samefile``，那一句抓不到）。
    """
    printed = _model_shell_python3(payload_environment(None, scratch_dir=tmp_path),
                                   "import sys; print(sys.executable); print(sys.prefix)")
    executable, prefix = printed.splitlines()
    assert Path(executable).samefile(sys.executable)
    assert Path(prefix).samefile(sys.prefix), (prefix, sys.prefix)


def test_python3_in_a_venv_is_the_venv_not_the_interpreter_it_links_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自己造一个真 venv、装一个只有它有的包，就当 harness 是从它起的。

    上一条只在"跑测试的这个解释器本身是 venv"时分得出对错；CI 跑在 Debian 的系统
    python 上，``/usr/bin/python3`` 链到同目录的 ``python3.11``，跟不跟链接一个样。
    这一条不看宿主：POSIX 上 venv 的 ``bin/python`` 总是指向基础解释器的链接。
    Windows 上 venv 的 ``python.exe`` 是启动器，这一条验的是那边补上的 ``python3.exe``。
    """
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                   check=True, capture_output=True, timeout=120)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    (Path(purelib) / "only_in_this_venv.py").write_text("")

    monkeypatch.setattr(sys, "executable", str(python))
    printed = _model_shell_python3(payload_environment(None, scratch_dir=tmp_path),
                                   "import only_in_this_venv, sys; print(sys.prefix)")
    assert Path(printed.strip()).samefile(venv), printed


def _interpreter_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "Scripts"
    bin_dir.mkdir()
    (bin_dir / "python.exe").write_bytes(b"MZ interpreter")
    return bin_dir


def test_the_name_is_the_same_file_as_python(tmp_path: Path) -> None:
    bin_dir = _interpreter_dir(tmp_path)
    python3_is_python(bin_dir)
    assert (bin_dir / "python3.exe").samefile(bin_dir / "python.exe")
    assert sorted(p.name for p in bin_dir.iterdir()) == ["python.exe", "python3.exe"]


def test_a_name_that_is_already_right_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每条命令都走到这里 —— 已经对了就不写盘。

    判据落在"有没有写"上，不落在 inode 上：重建一个硬链接 inode 不变，看不出来；
    而 Windows 上替换一个正被别的命令跑着的 ``python3.exe`` 会失败。
    """
    bin_dir = _interpreter_dir(tmp_path)
    python3_is_python(bin_dir)

    def must_not_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("名字已经对了，不该再写")

    monkeypatch.setattr(_native.os, "link", must_not_write)
    monkeypatch.setattr(_native.shutil, "copy2", must_not_write)
    monkeypatch.setattr(_native.os, "replace", must_not_write)
    python3_is_python(bin_dir)
    assert (bin_dir / "python3.exe").samefile(bin_dir / "python.exe")


def test_a_stale_name_is_replaced(tmp_path: Path) -> None:
    """解释器换过版本：旧的 ``python3.exe`` 指向的是上一个 python，得换成这一个。"""
    bin_dir = _interpreter_dir(tmp_path)
    (bin_dir / "python3.exe").write_bytes(b"MZ the previous interpreter")
    python3_is_python(bin_dir)
    assert (bin_dir / "python3.exe").samefile(bin_dir / "python.exe")


def test_without_hard_links_a_copy_still_gives_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exFAT 的 U 盘没有硬链接，而便携目录允许整个拷走。"""
    bin_dir = _interpreter_dir(tmp_path)

    def no_links(*_args: object, **_kwargs: object) -> None:
        raise OSError("hard links not supported")

    monkeypatch.setattr(_native.os, "link", no_links)
    python3_is_python(bin_dir)
    alias = bin_dir / "python3.exe"
    assert alias.read_bytes() == (bin_dir / "python.exe").read_bytes()
    before = alias.stat().st_ino
    python3_is_python(bin_dir)
    assert alias.stat().st_ino == before, "逐字节相同的拷贝也算对，不该每条命令重拷"


def test_an_unwritable_interpreter_dir_does_not_fail_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行时补不上名字，也不把整条命令拒掉。

    这**不是**「写不进就算了」的产品判据：随包应用的名字打包时就在（下面
    ``test_the_package_ships_python3_beside_python``），不靠运行时写安装目录。这里守的
    只是运行时这段在只读目录里的行为。
    """
    bin_dir = _interpreter_dir(tmp_path)

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(_native.os, "link", denied)
    monkeypatch.setattr(_native.shutil, "copy2", denied)
    python3_is_python(bin_dir)
    assert sorted(p.name for p in bin_dir.iterdir()) == ["python.exe"]


def test_payload_environment_gives_the_name_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接线：Windows 上 ``payload_environment`` 放到 PATH 最前面的那个目录里，
    ``python3.exe`` 在。任何宿主上都跑得到这条（CI 在 Linux）。"""
    bin_dir = _interpreter_dir(tmp_path)
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python.exe"))
    monkeypatch.setattr(_native, "_WINDOWS", True)
    env = payload_environment(None, scratch_dir=tmp_path)
    assert env["PATH"].split(os.pathsep)[0] == str(bin_dir)
    assert (bin_dir / "python3.exe").samefile(bin_dir / "python.exe")


def test_payload_environment_touches_nothing_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX 的解释器目录自带 ``python3``，这里一个文件都不该多出来。"""
    bin_dir = _interpreter_dir(tmp_path)
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python.exe"))
    monkeypatch.setattr(_native, "_WINDOWS", False)
    payload_environment(None, scratch_dir=tmp_path)
    assert sorted(p.name for p in bin_dir.iterdir()) == ["python.exe"]


def test_an_unknown_interpreter_puts_nothing_in_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sys.executable`` 可以是空串（Python 取不到自己的路径时）。``Path("").parent``
    是 ``.`` —— 放到最前面，工作目录里模型写的任何文件都能顶替 ``python3``、``ls``。"""
    monkeypatch.setattr(sys, "executable", "")
    env = payload_environment(None, scratch_dir=tmp_path)
    original = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    assert env["PATH"].split(os.pathsep) == original


def _packager():
    """按路径加载 Windows 打包器（它在 scripts/package/ 下、不在包里）。"""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "package" / "build_windows_app.py"
    spec = importlib.util.spec_from_file_location("_build_windows_app_python3", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_package_ships_python3_beside_python(tmp_path: Path) -> None:
    """打包时就放好：装完即在，不靠运行时往安装目录写；缺件检查也认它。"""
    packager = _packager()
    runtime = _interpreter_dir(tmp_path)
    packager.give_python_its_python3_name(runtime)
    assert (runtime / "python3.exe").read_bytes() == (runtime / "python.exe").read_bytes()
    assert "Resources/python/python3.exe" in packager.INSTALL_INVENTORY


def test_the_packager_stops_when_it_cannot_place_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行时写不进就算了（命令照样起）；打包时写不进就停 —— 包是给所有用户的。"""
    packager = _packager()
    runtime = _interpreter_dir(tmp_path)

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(_native.os, "link", denied)
    monkeypatch.setattr(_native.shutil, "copy2", denied)
    with pytest.raises(SystemExit):
        packager.give_python_its_python3_name(runtime)


def test_the_packager_places_the_name_with_the_runtime_function() -> None:
    """一个问题一个答案：打包器补名字用的就是运行时那个函数（不是另抄一份），而且
    ``install_the_runtime`` 真调了它。"""
    import ast

    packager = _packager()
    assert Path(packager.python3_is_python.__code__.co_filename).samefile(_native.__file__)
    source = Path(packager.__file__).read_text(encoding="utf-8")
    install = next(node for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.FunctionDef) and node.name == "install_the_runtime")
    calls = {node.func.id for node in ast.walk(install)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "give_python_its_python3_name" in calls


def test_the_install_check_reads_python3s_prefix(tmp_path: Path) -> None:
    """装完自检的判定：落到包内解释器、名字是包里带的才算过。"""
    packager = _packager()
    runtime = _interpreter_dir(tmp_path)
    python = runtime / "python.exe"
    (runtime / "python3.exe").write_bytes(python.read_bytes())  # 安装器解出来的独立文件
    packager.python3_lands_on_the_packaged_interpreter(
        {"rc": 0, "out": str(runtime), "err": ""}, python)
    with pytest.raises(SystemExit):  # 应用商店的桩：rc=49，一个字不打印
        packager.python3_lands_on_the_packaged_interpreter({"rc": 49, "out": "", "err": ""}, python)
    with pytest.raises(SystemExit):  # 别的解释器
        packager.python3_lands_on_the_packaged_interpreter(
            {"rc": 0, "out": str(tmp_path), "err": ""}, python)
    (runtime / "python3.exe").unlink()
    os.link(python, runtime / "python3.exe")  # 运行时补写的样子
    with pytest.raises(SystemExit):
        packager.python3_lands_on_the_packaged_interpreter(
            {"rc": 0, "out": str(runtime), "err": ""}, python)


def test_the_install_check_probe_runs_here(tmp_path: Path) -> None:
    """装完自检的探针只在 Windows 打包机上跑、一趟十分钟。在这里先真跑一次：它自己
    起得来，两条载荷都经沙箱出来，``python3`` 那条报的是这个解释器的 prefix。"""
    from core import isolation
    from core.isolation import Invariant

    try:
        backend = isolation.select_backend()
    except isolation.IsolationContractError as exc:
        pytest.skip(f"这台机器没有可用的沙箱后端：{exc}")
    if Invariant.WRITE_BOUNDARY not in backend.capabilities():
        pytest.skip("这台机器的沙箱守不住写边界")
    packager = _packager()
    probe = tmp_path / "payload_probe.py"
    probe.write_text(packager._PAYLOAD_PROBE, encoding="utf-8")
    harness = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, str(probe), str(harness), str(tmp_path / "work")],
                         capture_output=True, text=True, timeout=300, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert packager.PAYLOAD_MARKER in report["out"], report
    typed = report["python3"]
    assert typed["rc"] == 0, typed
    assert Path(typed["out"].splitlines()[-1]).samefile(sys.prefix), typed


def test_callers_can_still_override(tmp_path: Path) -> None:
    """调用方显式给的环境变量仍然最后生效（含 PATH）。"""
    env = payload_environment({"PATH": "/only/this"}, scratch_dir=tmp_path)
    assert env["PATH"] == "/only/this"


def test_secrets_are_still_stripped(tmp_path: Path) -> None:
    """加 PATH 不能顺手把"减去凭据"那件事弄丢。"""
    from core.secrets import is_secret_name

    env = payload_environment(None, scratch_dir=tmp_path)
    assert not [name for name in env if is_secret_name(name)]

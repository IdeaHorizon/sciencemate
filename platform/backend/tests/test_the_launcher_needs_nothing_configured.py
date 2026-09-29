"""一条命令就能起来：启动器不需要任何前置配置。

个人版的验收线是「安装到第一条回复 ≤ 5 分钟」。在此之前起这套东西要装
Postgres、跑 alembic、设四个环境变量、起后端、另起一个前端 dev server、再自己
敲地址 —— 每一步都是一次放弃的机会。这条测试钉住"这些前提一个都不剩"。
"""
from __future__ import annotations

import ast
import os
import re
import socket
import sys
from pathlib import Path

import pytest

from app import launcher


def test_the_data_root_is_the_same_answer_the_app_gives(monkeypatch, tmp_path: Path) -> None:
    """启动器和后端必须对「数据在哪」给同一个答案。

    两处各算一遍就是两个真相源：启动器建好一个根、后端往另一个根里写，而两边
    都不报错。08-21 那次 43 个会话丢失就是同一形状。

    这条判据以前比对**两个函数的返回值**（`launcher.default_data_root()` 和
    `Settings(...)`）—— 那只能证明两份抄件此刻相等，证明不了以后不分叉；
    2026-09-07 真机上它们就分叉了（配了 PLATFORM_DATA_ROOT，横幅印的还是
    `~/.harness-framework`）。现在启动器没有自己的答案可比，只有一个。
    """
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "elsewhere"))
    from app.config import Settings

    assert not hasattr(launcher, "default_data_root"), (
        "启动器又有了自己那份「数据在哪」—— 问 `app.config.the_data_root()`"
    )
    assert Settings(profile="personal", platform_data_root="", _env_file=None).platform_data_root \
        == str(tmp_path / "elsewhere")


def test_a_free_port_is_picked_when_none_is_given() -> None:
    """默认不写死端口：个人电脑上 8000 十有八九已经被占了，而"端口被占"是最不该
    让用户自己去查的一类失败。"""
    port = launcher.pick_a_free_port()
    assert 1024 < port < 65536
    with socket.socket() as probe:  # 真的空着
        probe.bind(("127.0.0.1", port))


def test_an_occupied_port_is_reported_not_silently_swapped() -> None:
    """给了端口就用给的。悄悄换一个 = 用户按自己记住的地址打不开。"""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        occupied = taken.getsockname()[1]
        with pytest.raises(OSError):
            launcher.pick_a_free_port(occupied)


def test_the_static_ui_is_found_where_the_build_puts_it(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("STATIC_UI_ROOT", raising=False)
    built = Path(launcher.__file__).resolve().parents[2] / "frontend" / "out"
    found = launcher.find_static_ui()
    if built.is_dir():
        assert found == built
    else:  # 没构建过就说没构建过，不假装有
        assert found is None

    explicit = tmp_path / "somewhere"
    explicit.mkdir()
    monkeypatch.setenv("STATIC_UI_ROOT", str(explicit))
    assert launcher.find_static_ui() == explicit


def test_doctor_reports_the_four_things_that_go_wrong(monkeypatch) -> None:
    """doctor 要能回答"东西在哪、这台机器守得住什么"，而不是抛异常。

    它自己**不许**制造失败：一个报告工具在报告不出来的时候崩掉，等于把用户
    从"有一个问题"送到"有两个问题"。
    """
    facts = dict(launcher.describe_environment())
    assert set(facts) == {"profile", "data root", "database", "static UI",
                          "pdf engine", "harness", "version", "git", "biber", "model shell",
                          "pdf", "sandbox"}
    # 「pdf engine」只说找到了哪个文件；能不能真出 PDF 是「pdf」那一行（实测记录）。
    # 文件在而出不了 PDF 的机器实测过（2026-09-23）。
    assert facts["pdf"].startswith("PDF 排版"), facts["pdf"]
    assert facts["profile"] in {"personal", "org"}
    assert facts["data root"]
    # 模型的 shell 必须报出来（哪怕报"解析不出"也行，不能没有这一行）——experiment
    # 节点每条命令都靠它，自检里不现形就要等真跑课题才炸。仓库里跑：harness 在、
    # `shared.lib.shell` import 得到，POSIX 上就是 `/bin/bash`。
    assert facts["model shell"], "doctor 没报模型的 shell（experiment 节点靠它跑命令）"
    if sys.platform != "win32":
        assert facts["model shell"] == "/bin/bash"


def test_the_harness_is_found_without_being_configured(monkeypatch) -> None:
    """harness 指不到 = 服务起得来、消息发得出、**回复永远不来**。

    这条失败没有任何症状指向病因：`/health/ready` 是 200，界面打得开，项目建得
    出。2026-09-05 写验收脚本时实测到的就是这个 —— 一条消息发出去，然后什么都
    没发生。所以"harness 在哪"不能留给用户去配。
    """
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    found = launcher.find_the_harness()
    assert found is not None, "从仓库里跑都找不到 harness，那装成包更找不到"
    assert (found / "core" / "agent_loop.py").is_file()


def test_a_harness_root_that_points_nowhere_is_not_an_answer(monkeypatch, tmp_path) -> None:
    """指错了就是没指到。

    判据是 `core/agent_loop.py` 在不在 —— 与 `/health/ready` 用的同一条
    （`main.py`）。两处各写一条判据，就会出现"启动器说找到了、就绪检查说没配"
    这种两边都不报错的分叉。
    """
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    assert launcher.find_the_harness() is None


def test_starting_wires_the_harness_but_never_overrides_a_choice(monkeypatch) -> None:
    """`start` 把找到的 harness 接上；已经配过的一律不动。"""
    import sys
    import types

    served: dict = {}
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: served.update({"app": app, **kwargs})
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    monkeypatch.delenv("HARNESS_BRIDGE_ENABLED", raising=False)

    assert launcher.main(["start", "--no-browser"]) == 0
    assert served["app"] == "app.main:app"
    root = launcher.find_the_harness()
    assert os.environ["HARNESS_ROOT"] == str(root)
    assert os.environ["HARNESS_BRIDGE_ENABLED"] == "true"

    monkeypatch.setenv("HARNESS_BRIDGE_ENABLED", "false")
    assert launcher.main(["start", "--no-browser"]) == 0
    assert os.environ["HARNESS_BRIDGE_ENABLED"] == "false"


# ── 壳没了，后端也要收摊 ────────────────────────────────────────────────────


def test_the_parent_being_gone_is_a_change_not_a_number() -> None:
    """判据是"父进程变了"，不是"父进程是不是 1"。

    父进程一死，孩子被过继给 init/launchd，`getppid()` 于是变了。写死 `== 1`
    在过继给别人的平台上就永远不成立，而那种失败是**静默**的：孤儿照样活着。
    """
    assert launcher.the_parent_is_gone(4242, 1) is True
    assert launcher.the_parent_is_gone(4242, 9999) is True
    assert launcher.the_parent_is_gone(4242, 4242) is False


def test_the_backend_stops_itself_when_the_shell_dies() -> None:
    """父进程没了 → 自己收摊。

    2026-09-06 真机实测：把桌面壳 pkill 掉之后后端活了下来，一次清出 9 个孤儿，
    每个都还占着数据根和一个端口。壳里的 applicationWillTerminate 只在 AppKit
    正常退出时跑，而用户按的「强制退出」不走那条路。
    """
    import threading

    died = threading.Event()
    ppids = iter([1234, 1234, 5678])
    thread = launcher.die_when_the_parent_does(
        original_ppid=1234,
        get_ppid=lambda: next(ppids),
        on_death=died.set,
        interval=0.01,
    )
    assert died.wait(timeout=5), "父进程都没了，后端还赖着不走"
    thread.join(timeout=5)


def test_a_live_parent_is_left_alone() -> None:
    """父进程还在就什么都不做 —— 这条守着"别自己把自己杀了"。"""
    import threading

    died = threading.Event()
    launcher.die_when_the_parent_does(
        original_ppid=1234, get_ppid=lambda: 1234,
        on_death=died.set, interval=0.01,
    )
    assert not died.wait(timeout=0.3)


def test_the_shell_asks_for_it() -> None:
    """桌面壳起后端时必须带上这个开关 —— 不带的话上面两条判据一条都用不上。

    判据落在壳的源码上：它是**唯一**会被强制退出的父进程，而它与启动器分属两种
    语言、两个构建产物，除了这条扫盘没有别的东西能把它们钉在一起。
    """
    shell = Path(launcher.__file__).resolve().parents[3] / "platform/desktop/mac/Shell.swift"
    if not shell.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("Shell.swift 不在了")
    # 先把注释剥掉再扫。**这一步不是洁癖**：解释这道闸的那段注释里就写着
    # `--exit-with-parent` 这几个字，不剥的话闸会匹配到自己的注释 —— 把开关
    # 从参数表里删掉，它照样绿（2026-09-06 变异实测，本仓库同形错误第三次）。
    code = "\n".join(
        line.split("//", 1)[0] for line in shell.read_text(encoding="utf-8").splitlines()
    )
    assert "--exit-with-parent" in code, (
        "壳没有要求后端跟着自己一起死 —— 强制退出会留下孤儿"
    )


# ── 应用自带 git ────────────────────────────────────────────────────────────


def test_a_bundled_git_is_found_next_to_the_runtime(monkeypatch, tmp_path: Path) -> None:
    """随包那份 git 找得到；没有就说没有（开发机上用系统的）。"""
    monkeypatch.delenv("HARNESS_GIT", raising=False)
    explicit = tmp_path / "bin" / "git"
    explicit.parent.mkdir(parents=True)
    explicit.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_GIT", str(explicit))
    assert launcher.find_the_bundled_git() == explicit

    monkeypatch.setenv("HARNESS_GIT", str(tmp_path / "nowhere" / "git"))
    assert launcher.find_the_bundled_git() is None, "指不到的路径不算数"


def test_starting_puts_the_bundled_git_first_on_path(monkeypatch, tmp_path: Path) -> None:
    """`start` 把它放到 PATH **最前面**。

    后端与 harness worker 都是 `"git"` 从 PATH 里找 —— 一处接线两边都拿到。
    放在后面等于没放：开发机上系统 git 先被找到，而没装 Command Line Tools 的
    机器上 `/usr/bin/git` 是个会弹框失败的 shim（这条判据要守的就是那台机器）。
    """
    import sys
    import types

    served: dict = {}
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: served.update({"app": app})
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    bundled = tmp_path / "bin" / "git"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_GIT", str(bundled))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert launcher.main(["start", "--no-browser"]) == 0

    first = os.environ["PATH"].split(os.pathsep)[0]
    assert first == str(bundled.parent), f"PATH 第一项是 {first}"


def test_doctor_says_which_git_it_will_use(monkeypatch) -> None:
    """doctor 要说出用的是哪一份 git —— 装错的时候这是唯一能自己看出来的地方。"""
    facts = dict(launcher.describe_environment())
    assert "git" in facts and facts["git"]


def test_the_bundled_layout_is_the_one_the_packager_builds(monkeypatch, tmp_path: Path) -> None:
    """随包那份 git 的位置，钉在**打包脚本真的放的地方**。

    这条是为一次真机失败写的：`find_the_bundled_git` 原来从 `__file__` 往上数
    目录，数错一层，于是随包的 git 永远找不到、悄悄回落到系统 shim —— 而当时
    所有测试都走显式 `HARNESS_GIT` 那条分支，没有一条碰到这里。

    判据因此**造出打包脚本产出的那个布局**（`Resources/python` 与
    `Resources/git` 并排），再问它找不找得到。
    """
    import sys

    resources = tmp_path / "Resources"
    (resources / "python" / "bin").mkdir(parents=True)
    git = resources / "git" / "bin" / "git"
    git.parent.mkdir(parents=True)
    git.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.delenv("HARNESS_GIT", raising=False)
    monkeypatch.setattr(sys, "prefix", str(resources / "python"))

    assert launcher.find_the_bundled_git() == git


def test_the_bundled_git_finder_accepts_the_windows_exe_name(monkeypatch, tmp_path: Path) -> None:
    """Windows 上随包的是 `git.exe` —— 找法要认这个名字。

    以前 finder 只找无后缀的 `git`，Windows 上（`git.exe`）永远找不到、悄悄回落
    系统 git —— 装完的应用在没装 Git for Windows 的机器上「新建项目」当场失败。
    找法不依赖 `os.name`，所以在 POSIX 宿主上也能确定性地验 `.exe` 分支。
    """
    import sys

    resources = tmp_path / "Resources"
    (resources / "python" / "bin").mkdir(parents=True)
    bindir = resources / "git" / "bin"
    bindir.mkdir(parents=True)
    monkeypatch.delenv("HARNESS_GIT", raising=False)
    monkeypatch.setattr(sys, "prefix", str(resources / "python"))

    win = bindir / "git.exe"
    win.write_text("MZ", encoding="utf-8")
    assert launcher.find_the_bundled_git() == win

    win.unlink()
    posix = bindir / "git"
    posix.write_text("#!/bin/sh\n", encoding="utf-8")
    assert launcher.find_the_bundled_git() == posix

    posix.unlink()
    assert launcher.find_the_bundled_git() is None


def test_the_packager_and_the_launcher_agree_on_where_git_goes() -> None:
    """打包脚本写进去的位置，与启动器去找的位置，是同一处。

    两处各写一份路径就会分叉，而分叉的表现是"包里带了一份没人调的 git"。
    """
    packager = (Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_mac_app.py")
    if not packager.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("打包脚本不在了")
    source = packager.read_text(encoding="utf-8")
    assert '"Contents" / "Resources" / "git"' in source, (
        "打包脚本把 git 放到了别处，而启动器仍按 Resources/git 去找"
    )


def test_the_windows_packager_puts_tectonic_where_the_finder_looks() -> None:
    """Windows 打包脚本把 tectonic 放的位置，与 launcher 去找的位置，是同一处。

    `find_the_bundled_tectonic` 找 `<sys.prefix.parent>/tectonic/bin/tectonic.exe`
    （便携包里 `sys.prefix` = `Resources/python`，故 `Resources/tectonic/bin/`）。
    两处各写一份就会"装是装进去了、finder 去别处找、装完的应用出不了 PDF"——
    正是 build_mac_app 那条 git 判据要挡的同一类。
    """
    packager = (Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_windows_app.py")
    if not packager.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("Windows 打包脚本不在了")
    source = packager.read_text(encoding="utf-8")
    assert '"tectonic" / "bin"' in source, (
        "Windows 打包脚本把 tectonic 放到了别处，而 finder 仍按 tectonic/bin 去找"
    )


def test_the_windows_packager_puts_biber_where_the_finder_looks() -> None:
    """biber 同 tectonic：放的位置与 `find_the_bundled_biber` 找的位置是同一处。

    平台模板的参考文献走 biblatex/biber，tectonic 从 PATH 调它；装进去了却放错地方，
    干净机器上每篇论文都卡在参考文献（2026-09-23 真机：``error: program not found``）。
    """
    packager = (Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_windows_app.py")
    if not packager.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("Windows 打包脚本不在了")
    source = packager.read_text(encoding="utf-8")
    assert '"biber" / "bin" / "biber.exe"' in source, (
        "Windows 打包脚本把 biber 放到了别处，而 finder 仍按 biber/bin 去找")
    assert '"Resources/biber/bin/biber.exe"' in source, "装完清单里没有 biber"


def test_the_windows_packager_puts_git_where_the_finder_looks() -> None:
    """Windows 打包脚本把 git 放的位置，与 launcher 去找的位置，是同一处。

    `find_the_bundled_git` 找 `<sys.prefix.parent>/git/bin/git.exe`（便携包里
    `sys.prefix` = `Resources/python`，故 `Resources/git/bin/git.exe`）。打包脚本取
    PortableGit（有 `bin/git.exe`）解到 `Resources/git/` 并校验 `bin/git.exe`——两处对齐，
    否则「装是装进去了、finder 去别处找、干净机器上没 git」。
    """
    packager = (Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_windows_app.py")
    if not packager.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("Windows 打包脚本不在了")
    source = packager.read_text(encoding="utf-8")
    assert '"bin" / "git.exe"' in source, (
        "Windows 打包脚本没把 git 放/校验在 git/bin/git.exe，而 finder 仍按那儿找"
    )


def test_the_windows_packager_puts_the_model_shell_where_the_resolver_looks() -> None:
    """打包脚本随包的 bash，落在模型的 shell（`_windows_bash`）去找的那一处。

    experiment 节点跑的每条实验命令都走 `shared.lib.shell.bash_shell()`；Windows 上那是
    `_windows_bash` 的首选——随包布局 `<sys.prefix.parent>/git/usr/bin/bash.exe`（便携包里
    `Resources/git/usr/bin/bash.exe`）。resolver 那一端「首选随包 bash」由
    `tests/test_shell_resolver.py::test_windows_prefers_the_bundled_git_bash` 钉住；这里钉
    **打包端**——脚本取 PortableGit 解到 `Resources/git/` 后，必须成对校验 `usr/bin/bash.exe`
    真在（`bin/git.exe` 在不等于 bash 在：MinGit 只有 git 没有 bash）。两端对齐，才不会
    「git 校验过了、bash 悄悄缺席」——干净机器上没系统 Git for Windows 时，那份随包 bash
    是模型唯一的 shell，缺了 experiment 节点当场没 bash（或裸 `bash.exe` 落进 WSL 启动器）。
    """
    packager = (Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_windows_app.py")
    if not packager.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("Windows 打包脚本不在了")
    source = packager.read_text(encoding="utf-8")
    assert '"usr" / "bin" / "bash.exe"' in source, (
        "Windows 打包脚本没把随包 bash 放/校验在 git/usr/bin/bash.exe，而模型的 shell"
        "（_windows_bash 首选）仍按那儿找——干净机器上 experiment 节点会没 bash"
    )


def test_the_windows_packager_configures_the_bundled_git_for_byte_exact_content() -> None:
    """随包 git 必须显式关掉 `autocrlf`（+ 开 `longpaths`），写进它的**系统配置**。

    平台每个 Project 都是 git 仓库，attempt 沙箱靠 `git worktree add` / `git checkout` 从
    提交态签出工作树。Git for Windows 默认 `core.autocrlf=true`（真机实测系统 git 就是 true），
    签出时把 LF→CRLF：提交的 shell 脚本签出成 `#!/bin/bash\r`，在 MSYS bash 里当场
    `bad interpreter: /bin/bash^M`；数据 /.tex 按字节读也被污染。平台内容跨平台字节精确，
    CRLF 转换一律是错——所以打包时必须把随包 git 的 `core.autocrlf` 显式设 false（不靠
    PortableGit 碰巧的默认、也不靠只保 .json/.yaml 的 .gitattributes——脚本 /.tex /.py 漏在外）。
    设在 `--system` ＝这份 git 的所有仓库统一生效。顺带 `core.longpaths=true`（RFC §11 机制）
    挡 %LOCALAPPDATA%\\afs 深路径的 `Filename too long`。
    """
    packager = (Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_windows_app.py")
    if not packager.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("Windows 打包脚本不在了")
    source = packager.read_text(encoding="utf-8")
    assert '"config", "--system", cfg_key, cfg_value' in source, (
        "随包 git 的配置不是写进 --system（系统配置）——换个写法会让它只对某一个仓库生效，"
        "attempt 沙箱 worktree/别的仓库照样 autocrlf=true"
    )
    assert '("core.autocrlf", "false")' in source, (
        "打包脚本没显式关掉随包 git 的 autocrlf——签出的脚本会带 CRLF、在 MSYS bash 里炸"
    )
    assert '("core.longpaths", "true")' in source, (
        "打包脚本没给随包 git 开 longpaths——%LOCALAPPDATA%\\afs 深路径 git 操作会 Filename too long"
    )
    # 光「读回为真」不够：值可能落在包外的 %PROGRAMDATA%\Git\config（本机 git 也读它、读回
    # 照样真），装到别的机器上就没了。打包脚本必须用 `--show-origin` 钉死配置文件在**包内**。
    assert '"--show-origin"' in source and "is_relative_to(target" in source, (
        "打包脚本没校验随包 git 的配置落在包内——可能写进了 %PROGRAMDATA%、装到别处就丢了"
    )


# ── 打包脚本的下载：超时 + 重试 + 完整性（行为测试，跨平台可跑）─────────────────

def _load_packager():
    """按路径加载打包脚本（它在 scripts/package/ 下、不在包里，不能直接 import）。"""
    import importlib.util
    packager = (Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_windows_app.py")
    if not packager.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("Windows 打包脚本不在了")
    spec = importlib.util.spec_from_file_location("_build_windows_app_undertest", packager)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    """假的 urlopen 返回值：既是上下文管理器，又能被 shutil.copyfileobj 读。"""

    def __init__(self, data: bytes, length=None):
        self._data = data
        self._pos = 0
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
        else:
            chunk = self._data[self._pos:self._pos + n]
            self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_that(*, fail_times: int, data: bytes = b"payload", length=None):
    """前 fail_times 次抛（瞬时失败），之后返回 data；length 可单独给以造「截断」。"""
    calls = {"n": 0}

    def _open(url, timeout=None):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise OSError("boom")
        return _FakeResp(data, length=length)

    return _open, calls


def test_packager_download_succeeds_without_retry(monkeypatch, tmp_path):
    mod = _load_packager()
    opener, calls = _urlopen_that(fail_times=0, data=b"hello-bytes")
    monkeypatch.setattr("urllib.request.urlopen", opener)
    dest = tmp_path / "f.bin"
    mod.download("http://x/f", dest, attempts=4, timeout=1)
    assert dest.read_bytes() == b"hello-bytes"
    assert calls["n"] == 1, "一次成功不该重试"


def test_packager_download_retries_transient_then_succeeds(monkeypatch, tmp_path):
    mod = _load_packager()
    opener, calls = _urlopen_that(fail_times=2, data=b"ok")
    monkeypatch.setattr("urllib.request.urlopen", opener)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)  # 别真睡退避
    dest = tmp_path / "f.bin"
    mod.download("http://x/f", dest, attempts=4, timeout=1)
    assert dest.read_bytes() == b"ok"
    assert calls["n"] == 3, "该在第 3 次成功（前两次瞬时失败被重试掉）"


def test_packager_download_gives_up_after_attempts(monkeypatch, tmp_path):
    mod = _load_packager()
    opener, calls = _urlopen_that(fail_times=99)
    monkeypatch.setattr("urllib.request.urlopen", opener)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    with pytest.raises(SystemExit):
        mod.download("http://x/f", tmp_path / "f.bin", attempts=3, timeout=1)
    assert calls["n"] == 3, "该恰好试 attempts 次就放弃（不是无限重试）"
    assert not (tmp_path / "f.bin").exists(), "失败不该把半截文件留给下一步"


def test_packager_download_rejects_truncated(monkeypatch, tmp_path):
    mod = _load_packager()
    # Content-Length 说 100、实际只 2 字节 → 截断，必须判失败（否则半截文件下一步解压才炸）
    opener, calls = _urlopen_that(fail_times=0, data=b"ab", length=100)
    monkeypatch.setattr("urllib.request.urlopen", opener)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    with pytest.raises(SystemExit):
        mod.download("http://x/f", tmp_path / "f.bin", attempts=2, timeout=1)
    assert calls["n"] == 2, "截断该被判失败并重试到用尽"
    assert not (tmp_path / "f.bin").exists(), "截断的半截文件不该留下"


def test_the_windows_packager_builds_the_static_export() -> None:
    """`--with-ui` 打的界面必须是**静态导出**（`output: export`），不是 dev server。

    next.config 靠 `PLATFORM_STATIC_EXPORT` 切成 `output: export`（产出 `out/`，由后端
    自己 serve、`find_static_ui()` 的落点）。打包脚本 build 界面时不带这个环境变量，
    产出的是普通 server bundle、没有 `out/index.html`——装完的应用只有 API、没界面。
    """
    packager = (Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_windows_app.py")
    if not packager.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("Windows 打包脚本不在了")
    source = packager.read_text(encoding="utf-8")
    assert "PLATFORM_STATIC_EXPORT" in source, (
        "Windows 打包脚本 build 界面没带 PLATFORM_STATIC_EXPORT —— 出的不是静态导出"
    )


def test_the_windows_packager_writes_a_relocatable_backend_json() -> None:
    """backend.json 的 executable 必须是**相对路径**（壳按相对自己目录解析），不是绝对
    构建路径——否则装到 %LOCALAPPDATA% / 拷到别处，路径指回构建机、后端起不来。这条
    是为一个真 bug 写的：#881 前 backend.json 写的是 `str(python)`（绝对），装完自检又只
    `--no-launch` 查文件在、没从新位置真起壳，漏了整包不可搬走。
    """
    packager = (Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_windows_app.py")
    if not packager.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip("Windows 打包脚本不在了")
    source = packager.read_text(encoding="utf-8")
    assert "python.relative_to(app)" in source, (
        "backend.json 的 executable 不是相对路径——装到别处 / 拷走后后端起不来"
    )
    assert '"executable": str(python),' not in source, (
        "backend.json 又写回了绝对 str(python)——整包不可搬走"
    )


def test_the_installer_stub_and_packager_agree_on_the_magic() -> None:
    """自解压 Setup.exe 的 footer 魔数，桩（C#）与打包器（py）必须逐位一致。

    单文件安装器＝`stub.exe ‖ payload.zip ‖ footer(offset, MAGIC)`。桩靠 MAGIC 认出
    「这是拼过 payload 的真安装包」。两处各写一份 int、改一处忘了另一处 → 装好的
    Setup.exe 认不出自己的 payload、安装当场失败。用扫盘把这两份钉在一起。
    """
    import re

    root = Path(launcher.__file__).resolve().parents[3]
    packager = root / "scripts" / "package" / "build_windows_app.py"
    stub = root / "platform" / "desktop" / "windows" / "InstallerStub.cs"
    if not (packager.is_file() and stub.is_file()):  # pragma: no cover - 仓库结构变了
        pytest.skip("Windows 打包脚本 / 安装器桩不在了")

    def hexval(text: str, pattern: str) -> int | None:
        m = re.search(pattern, text)
        return int(m.group(1), 16) if m else None

    py_magic = hexval(packager.read_text(encoding="utf-8"),
                      r"INSTALLER_MAGIC\s*=\s*(0x[0-9A-Fa-f]+)")
    cs_magic = hexval(stub.read_text(encoding="utf-8"),
                      r"MAGIC\s*=\s*(0x[0-9A-Fa-f]+)")
    assert py_magic is not None, "打包器里找不到 INSTALLER_MAGIC"
    assert cs_magic is not None, "桩里找不到 MAGIC"
    assert py_magic == cs_magic, f"footer 魔数分叉：py={py_magic:#x} cs={cs_magic:#x}"


def test_a_bundled_tectonic_is_found_next_to_the_runtime(monkeypatch, tmp_path: Path) -> None:
    """随包那份 tectonic 找得到；没有就说没有（开发机上用系统 latexmk）。"""
    monkeypatch.delenv("HARNESS_TECTONIC", raising=False)
    explicit = tmp_path / "bin" / "tectonic"
    explicit.parent.mkdir(parents=True)
    explicit.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_TECTONIC", str(explicit))
    assert launcher.find_the_bundled_tectonic() == explicit

    monkeypatch.setenv("HARNESS_TECTONIC", str(tmp_path / "nowhere" / "tectonic"))
    assert launcher.find_the_bundled_tectonic() is None, "指不到的路径不算数"


def test_the_bundled_tectonic_layout_matches_the_git_convention(monkeypatch, tmp_path: Path) -> None:
    """随包 tectonic 的位置，钉在与 git 并排的那个布局（`Resources/tectonic/bin`）。

    和 git 那条同一个教训：别从 `__file__` 往上数目录（数错一层就静默找不到、
    悄悄回落系统），锚在 `sys.prefix`（跑着我们的解释器）旁边。
    """
    import sys

    resources = tmp_path / "Resources"
    (resources / "python" / "bin").mkdir(parents=True)
    tectonic = resources / "tectonic" / "bin" / "tectonic"
    tectonic.parent.mkdir(parents=True)
    tectonic.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.delenv("HARNESS_TECTONIC", raising=False)
    monkeypatch.setattr(sys, "prefix", str(resources / "python"))

    assert launcher.find_the_bundled_tectonic() == tectonic


def test_the_bundled_tectonic_finder_accepts_the_windows_exe_name(monkeypatch, tmp_path: Path) -> None:
    """随包二进制在 Windows 上叫 `tectonic.exe`、POSIX 上叫 `tectonic` —— 两个都认。

    这条守的是**真机那一侧**（本战役目标就是 Windows）。找法不依赖 `os.name`，所以
    在 POSIX 宿主上也能确定性地验 `.exe` 分支：文件系统只看文件名，造一个叫
    `tectonic.exe` 的空文件即可。
    """
    import sys

    resources = tmp_path / "Resources"
    (resources / "python" / "bin").mkdir(parents=True)
    bindir = resources / "tectonic" / "bin"
    bindir.mkdir(parents=True)
    monkeypatch.delenv("HARNESS_TECTONIC", raising=False)
    monkeypatch.setattr(sys, "prefix", str(resources / "python"))

    win = bindir / "tectonic.exe"
    win.write_text("MZ", encoding="utf-8")  # 名字对就够了
    assert launcher.find_the_bundled_tectonic() == win

    # POSIX 无后缀名的那份也认。
    win.unlink()
    posix = bindir / "tectonic"
    posix.write_text("#!/bin/sh\n", encoding="utf-8")
    assert launcher.find_the_bundled_tectonic() == posix

    # 都没有 → None。
    posix.unlink()
    assert launcher.find_the_bundled_tectonic() is None


def test_starting_puts_the_bundled_tectonic_on_path(monkeypatch, tmp_path: Path) -> None:
    """`start` 把随包 tectonic 放到 PATH 上 —— 编译那一步（在 harness worker 里）
    才 `shutil.which("tectonic")` 找得到。放不上＝装完的应用出不了 PDF。"""
    import sys
    import types

    served: dict = {}
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: served.update({"app": app})
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    bundled = tmp_path / "bin" / "tectonic"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_TECTONIC", str(bundled))
    monkeypatch.delenv("HARNESS_GIT", raising=False)  # 只测 tectonic 那条 prepend
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert launcher.main(["start", "--no-browser"]) == 0

    first = os.environ["PATH"].split(os.pathsep)[0]
    assert first == str(bundled.parent), f"PATH 第一项是 {first}"


def test_starting_puts_the_bundled_biber_on_path(monkeypatch, tmp_path: Path) -> None:
    """`start` 把随包 biber 放到 PATH 上 —— tectonic 编到参考文献那步从 PATH 调它。"""
    import sys
    import types

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: None
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    bundled = tmp_path / "biber" / "bin" / "biber"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "find_the_bundled_biber", lambda: bundled)
    monkeypatch.setattr(launcher, "find_the_bundled_tectonic", lambda: None)
    monkeypatch.setattr(launcher, "find_the_bundled_git", lambda: None)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert launcher.main(["start", "--no-browser"]) == 0
    assert os.environ["PATH"].split(os.pathsep)[0] == str(bundled.parent)


_PACKAGERS = Path(__file__).resolve().parents[3] / "scripts" / "package"


def _load_the_packager_module(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"afs_test_{name}", _PACKAGERS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("packager, gate", [
    ("build_windows_app.py", "prove_the_installed_app_makes_the_platforms_pdf"),
    ("build_mac_app.py", "prove_the_app_makes_the_platforms_pdf"),
])
def test_the_installer_self_check_makes_the_platforms_pdf(packager: str, gate: str) -> None:
    """装完自检必须在**安装位置**真编一次平台的 PDF，判据是「编出来了」，不是「文件在」。

    2026-09-23：tectonic.exe 在，可干净 Windows 上每次 5 秒 os error 5，随包也没有 biber；
    自检当时只看 doctor 的「pdf engine」有没有值。这条是 Linux 上跑的**机械闸**，守的是
    「那段自检还在、有人调、走的是平台自己的实测（不另写编译命令）、编不过就红」。
    """
    source = (_PACKAGERS / packager).read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == gate), None)
    assert node is not None, f"{packager}：装完自检里那条「在安装位置真编一次 PDF」不见了"
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert gate in called, f"{packager}：写了自检却没人调"
    body = ast.get_source_segment(source, node) or ""
    assert 'record.get("works")' in body and "SystemExit" in body, "编不过也不红"
    assert "tex_toolchain.PDF_PROBE" in body, "没用两个打包器共用的那份探针，另写了一份"
    probe = _load_the_packager_module("tex_toolchain").PDF_PROBE
    assert "prepare_the_environment" in probe, "没走用户那条路接环境（随包 tectonic/biber 上 PATH）"
    assert "pdf_toolchain.measure" in probe, "没用平台自己的实测，另写了一份编译命令"


def test_the_mac_self_check_proves_the_bundled_tex_not_the_build_machines() -> None:
    """打包机装着 MacTeX：自检的 PATH 里留着它，编译器选择先挑 latexmk，自检就只证明了
    打包机能出 PDF。PATH 只留系统目录、HOME 是空的，并且认准编它的是**包里的**那两件。"""
    source = (_PACKAGERS / "build_mac_app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "prove_the_app_makes_the_platforms_pdf")
    body = ast.get_source_segment(source, node) or ""
    assert '"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"' in body, "PATH 里可能留着打包机的 TeX"
    assert "texbin" not in body
    assert '"HOME": str(work / "home")' in body, "不是空的家：第一次取宏包那条路没被验到"
    assert "startswith(str(bundled))" in body, "没认准编它的是包里的 tectonic / biber"


def test_each_packager_bundles_both_halves_of_the_tex_pair() -> None:
    """tectonic 与 biber 是一对（biber 版本对着 tectonic 宏包里的 biblatex）：每个平台两件都
    钉了哈希，每个打包器两件都放 —— 放了 tectonic 没放 biber，每篇论文卡在参考文献。"""
    toolchain = _load_the_packager_module("tex_toolchain")
    for (platform, name), asset in toolchain.ASSETS.items():
        assert re.fullmatch(r"[0-9a-f]{64}", asset.sha256), (platform, name)
    platforms = {"build_windows_app.py": "windows", "build_mac_app.py": "macos"}
    for packager, platform in platforms.items():
        assert {(platform, "tectonic"), (platform, "biber")} <= set(toolchain.ASSETS)
        tree = ast.parse((_PACKAGERS / packager).read_text(encoding="utf-8"))
        placed = {n.args[1].value for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "place" and len(n.args) >= 2
                  and isinstance(n.args[0], ast.Constant) and n.args[0].value == platform
                  and isinstance(n.args[1], ast.Constant)}
        assert placed == {"tectonic", "biber"}, f"{packager} 放的是 {sorted(placed)}"


def test_the_mac_packager_puts_tex_where_the_finder_looks() -> None:
    """Mac 包里 tectonic / biber 的落点，与 `_bundled_binary` 找的
    `<sys.prefix.parent>/<名>/bin/<名>`（``sys.prefix`` = `Resources/python`）是同一处。"""
    source = (_PACKAGERS / "build_mac_app.py").read_text(encoding="utf-8")
    assert 'resources / "tectonic" / "bin" / "tectonic"' in source
    assert 'resources / "biber" / "bin" / "biber"' in source
    assert 'resources = app / "Contents" / "Resources"' in source
    tree = ast.parse(source)
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {n.func.id for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "place_the_tex_toolchain" in called, "写了放 TeX 的那步却没人调"


def test_the_installer_self_check_runs_a_real_model_command() -> None:
    """装完自检必须在**安装位置**真跑一条模型命令，而且判据落在**载荷的 stdout** 上。

    ## 这条闸守的是什么

    自检曾经只查「文件在 / doctor 说得出 / health 200 / GET / 有 HTML」——一条模型命令
    都不跑。可用户装完第一件事就是提课题，那要走 `select_backend().prepare()` →
    Low-IL 令牌 → `CreateProcessAsUser` → 标准句柄透传这一整条，**在安装位置**从来没被
    自检走过（2026-09-10：一个把安装目录权限改坏的操作，没有任何一层出声，症状要等
    真课题跑到 experiment 才现形）。

    判据必须落在 stdout 上不是返回码：Low-IL 下句柄没透传时进程照样 rc=0 而 print 全丢
    （#861 踩过），只看 rc 会放过它。

    这条测试是 Linux 上跑的**机械闸** —— 打包器只在 Windows 上跑，所以它守的是
    「那段自检还在、而且还在断言 stdout」，不是自检本身的行为。
    """
    root = Path(__file__).resolve().parents[3]
    packager = (root / "scripts" / "package" / "build_windows_app.py").read_text(encoding="utf-8")
    tree = ast.parse(packager)

    gate = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and n.name == "prove_a_model_command_runs_from_the_installed_app"),
        None,
    )
    assert gate is not None, "装完自检里那条「真跑一条模型命令」不见了"

    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(gate) if isinstance(node, ast.Call)
    }
    assert "SystemExit" in {
        getattr(n.exc.func, "id", "") for n in ast.walk(gate)
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
    }, "载荷跑不出来必须让构建失败，不能只打印一行"

    # 判据落在载荷的 stdout 上（报告里的 "out"），不是只看返回码
    source = ast.get_source_segment(packager, gate) or ""
    assert 'report.get("out"' in source or 'report["out"]' in source, \
        "判据要落在载荷的 stdout 上——Low-IL 下句柄没透传时 rc 照样是 0"

    # 而且它必须真的被安装器自检调用（写了没接线等于没有）
    installer = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "prove_the_installer_works"),
        None,
    )
    assert installer is not None
    wired = {
        getattr(node.func, "id", "") for node in ast.walk(installer)
        if isinstance(node, ast.Call)
    }
    assert "prove_a_model_command_runs_from_the_installed_app" in wired, \
        "这条闸没有被安装器自检调用——写了没接线等于没有"


def _windows_shell_source() -> str:
    root = Path(__file__).resolve().parents[3]
    return (root / "platform" / "desktop" / "windows" / "Shell.cs").read_text(encoding="utf-8")


def _windows_shell_code() -> str:
    """Shell.cs 去掉整行注释后的**代码**。

    「壳里不许再出现 X」这种判据必须扫代码：解释「为什么不许有 X」的注释里一定
    会写出 X。2026-09-10 踩过两次（另一次是 DLL 放处那条扫到了 docstring 里的
    "Resources"）—— 判据扫到散文，就是在回答另一个问题。
    """
    return "\n".join(line for line in _windows_shell_source().splitlines()
                     if not line.lstrip().startswith("//"))


def _windows_packager_source() -> str:
    root = Path(__file__).resolve().parents[3]
    return (root / "scripts" / "package" / "build_windows_app.py").read_text(encoding="utf-8")


def _installer_code() -> str:
    """InstallerStub.cs 去掉整行注释后的代码 —— 同 `_windows_shell_code` 的理由。"""
    root = Path(__file__).resolve().parents[3]
    text = (root / "platform" / "desktop" / "windows" / "InstallerStub.cs").read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith(("//", "///")))


def test_the_installer_never_takes_apart_a_working_install() -> None:
    r"""安装器不许在有能用的新东西之前，拆掉唯一能用的旧东西。

    2026-09-10 真机：应用开着时重跑 Setup.exe，`Directory.Delete(installDir, true)`
    删到一半撞上被占用的 WebView2 DLL 抛异常，留下一个 `Resources\tectonic` 已经
    没了的安装 —— 起得来、产不出 PDF、用户不知道为什么。而这正是升级的主路
    （自更新只换 harness 与界面，壳变了必须重装）。

    判据落在**顺序**上：解压的目标不是 installDir 而是 staging；旧目录只许被
    `Move` 走（可回退），不许被 `Delete`；而且「有没有实例在跑」要在解压**之前**问。

    事务化之后（每次安装一个独立的 `.new-<id>` / `.old-<id>`、按安装目录取 Mutex、
    换不上去就把旧的挪回来）判据不变，只是多了几件要看的事：锁要在动手之前拿；
    验新的（staging 里有 ScienceMate.exe）要在挪旧的之前；换新失败要有回滚那一步；
    解压的成员要拒绝绝对路径 / 盘符·ADS / `..`，且只许 `CreateNew`（不覆盖任何已有文件）。
    """
    code = _installer_code()
    assert "Directory.Delete(installDir" not in code, (
        "安装器又直接删安装目录了 —— 删到一半就是一个坏掉的安装")
    assert '".new-"' in code and "staging" in code, "没有 staging 目录 —— 那就是原地解压"
    # 判的是「谁被挪到哪」，不是挪它的那个函数叫什么名字。2026-09-21 那次把裸
    # `Directory.Move` 换成了带重试的包装（杀软会攥着刚写完的文件），顺序一点没变，
    # 判据却整条塌了 —— 按写法去扫，改写法就当没这道闸。
    def _step(pattern: str, what: str) -> int:
        found = re.search(pattern, code)
        assert found is not None, f"找不到{what}"
        return found.start()

    def _moves(source: str, target: str, what: str) -> int:
        return _step(rf"\w+\(\s*(?:IoPath\()?{source}\)?\s*,\s*(?:IoPath\()?{target}\b", what)

    extract = _step(r"ExtractPackage\(\s*tmpZip\s*,\s*staging\b", "把包解到 staging 那一步")
    verify_new = code.index("File.Exists(IoPath(stagedExe))")
    retire_old = _moves("installDir", "retired", "把旧安装挪走那一步")
    switch = _moves("staging", "installDir", "把新安装挪上去那一步")
    rollback = _moves("retired", "installDir", "换新失败把旧的挪回来那一步")
    assert extract < verify_new < retire_old < switch < rollback, (
        "顺序错了：应当 解到 staging → 验 staging 里的 exe → 把旧的 Move 走 → 把新的 Move 上去 → 失败则把旧的挪回来")
    lock = code.index("new Mutex(")
    assert lock < code.index("RunningPidIn(installDir)") < extract, (
        "锁和「有没有实例在跑」都得问在解压之前 —— 那时用户已经等了半分钟，而且 Move 一定被锁挡住")
    assert "MainModule.FileName" in code, (
        "按进程名认「它在跑」会把别处的另一份安装误报成本机这一份")
    # 解压成员的边界：不许逃出 staging，不许覆盖已有文件。
    member = code[code.index("private static void ExtractPackage"):extract]
    assert "Path.IsPathRooted(relative)" in member and "IndexOf(':')" in member, "没拒绝绝对路径 / 盘符·ADS 成员"
    assert "target.StartsWith(root" in member, "没拒绝 `..` 逃出安装目录的成员"
    assert "FileMode.CreateNew" in member, "解压会覆盖已有文件 —— 应当只许 CreateNew"


def test_the_reinstall_gate_is_wired_and_checks_both_things() -> None:
    """那条闸要装在壳还活着的时候，而且两件事都得断言：说了话 + 没弄坏。"""
    tree = ast.parse(_windows_packager_source())
    gate = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name == "prove_a_reinstall_never_breaks_a_working_install"), None)
    assert gate is not None, "没有「开着的时候重装」这条闸"
    src = ast.unparse(gate)
    assert "returncode" in src, "没断言安装器说了话（退出码）"
    assert "INSTALL_INVENTORY" in src, "没断言已装好的那份还完整"
    wired = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                             and n.name == "prove_the_installer_works"))
    assert "prove_a_reinstall_never_breaks_a_working_install" in wired, "写了没接线等于没有"
    # 必须排在「把壳停掉」之前 —— 壳都关了，验的就不是「开着的时候重装」了。
    # 停壳这件事换过写法（`shell.terminate()` → `stop_the_shell(shell)`），所以按
    # **做这件事的任何形式**去找，而不是钉死一个拼法。
    stops = [m.start() for m in re.finditer(r"(?:stop|terminate|kill)\w*\(\s*shell\b"
                                            r"|shell\.(?:terminate|kill)\(", wired)]
    assert stops, "这条闸的前提没了：整个自检里没有「把壳停掉」那一步"
    assert wired.index("prove_a_reinstall_never_breaks_a_working_install") < min(stops), \
        "这条闸排到了关壳之后 —— 它验的场景就不存在了"


def test_the_windows_shell_shows_its_own_window_not_a_browser_tab() -> None:
    """Windows 壳必须把界面显示在**自己的窗口**里 —— 和 mac 的 WKWebView 同一个答案。

    V1 只打印 URL、拉起默认浏览器，理由是「无头 SSH 上窗口画不出也验不了」。
    **那个理由是错的**（2026-09-10 实测：headless SSH 上窗口照样建得出、`EnumWindows`
    找得到）。同事装完看到一个浏览器标签页，不是一个软件。

    这条闸守的是「别再退回浏览器」：壳必须引 WebView2 并建 Form。
    """
    shell = _windows_shell_source()
    assert "Microsoft.Web.WebView2.WinForms" in shell, "壳没有内嵌 WebView2"
    assert "new WebView2()" in shell, "壳没有建 WebView2 控件"
    assert "Application.Run(" in shell, "壳没有自己的消息循环 —— 那就不是一个窗口应用"


def test_stdin_is_not_a_stop_signal_for_the_shell() -> None:
    """壳不许再把 stdin 当收摊信号 —— 一行都不许留。

    2026-09-10 真机对照（同一份源码、同一种起法）：
      监听 stdin EOF → 双击起来 2.08s 自己退，exit 0（同事看到「窗口一闪就没了」）
      删掉那几行     → 75s 还活着

    `Console.IsInputRedirected` 回答不了「上游有没有人管着我」：**没有控制台时它
    也返回 true**（stdin 句柄为 NULL，不是字符设备）。所以正解不是把守卫写细，
    而是把这条路删掉 —— 能让壳收摊的只剩「窗口被关」和「后端退出」两件用户看得
    见的事。要在脚本里停掉壳，杀 pid 就行，产品不为夹具留分支。
    """
    shell = _windows_shell_code()
    assert "Console.In.ReadLine" not in shell, (
        "壳又开始读 stdin 了 —— 双击起来的窗口会刚开就关（真机实测 2.08s）")
    assert "Console.IsInputRedirected" not in shell, (
        "又拿 IsInputRedirected 当守卫了 —— 它在「没有控制台」时同样是 true，"
        "恰好在双击这个场景失效")


def test_the_shell_writes_what_it_says_to_a_log_file() -> None:
    """没有控制台的进程必须有地方说话，否则它出事时一个字都留不下。

    「双击 2 秒自己退」能活到发布前一刻，就是因为 winexe 的 `Console.Write*` 掉进
    虚空。日志得在**做任何事之前**就接上 —— 接晚了，早退的那条路照样是哑的。
    """
    shell = _windows_shell_code()
    assert "AlsoLogToAFile" in shell, "壳不写日志 —— 双击起来出任何事都查不了"
    assert "shell.log" in shell, "日志没有落到 shell.log"
    body = shell[shell.index("private static int Main("):]
    first = body.index("AlsoLogToAFile();")
    for later in ("LoadConfig", "CreateKillOnCloseJob", "ShowTheInterface"):
        assert first < body.index(later), f"日志接得比 {later} 还晚 —— 早退那条路仍然是哑的"


def test_the_install_check_starts_the_shell_the_way_a_user_does() -> None:
    """自检起壳的方式必须和用户双击一致：没有控制台、三个标准句柄都不给。

    这是「双击 2 秒自己退」当初躲过全部自检的原因：自检用 `stdin=PIPE` 起壳，
    父进程一直攥着管道写端，壳的 stdin 永远不 EOF。**同事没有那个父进程。**
    夹具比产品多给一样东西，测的就是另一个场景。

    判据落在真实的调用参数上（AST），不是措辞。
    """
    tree = ast.parse(_windows_packager_source())
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "start_the_shell_the_way_a_user_does"), None)
    assert fn is not None, "自检没有「按用户的方式起壳」这一步"
    call = next((n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == "subprocess.Popen"), None)
    assert call is not None, "它没真去起进程"
    kwargs = {k.arg: ast.unparse(k.value) for k in call.keywords}
    for handle in ("stdin", "stdout", "stderr"):
        assert kwargs.get(handle) == "subprocess.DEVNULL", (
            f"{handle} 不是 DEVNULL（是 {kwargs.get(handle)!r}）—— "
            "留着管道就等于自检替壳养了一个用户没有的父进程")
    assert "DETACHED_PROCESS" in kwargs.get("creationflags", ""), (
        "没有 DETACHED_PROCESS —— 壳会继承调用方的控制台，双击时它没有")

    wired = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                             and n.name == "prove_the_installer_works"))
    assert "start_the_shell_the_way_a_user_does" in wired, "写了没接线等于没有"
    assert "subprocess.PIPE" not in wired, "自检又给壳接管道了"


def test_the_install_check_proves_the_shell_keeps_running() -> None:
    """光「起来了」不算 —— 得过一会儿它还在。

    真机上壳是**先就绪、再自己退**的：READY 打了、/health 200 了、窗口也画出来了，
    2 秒后没了。任何只看「起来没起来」的判据都会给绿灯。
    """
    tree = ast.parse(_windows_packager_source())
    gate = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name == "prove_the_shell_outlives_its_launcher"), None)
    assert gate is not None, "没有「壳得活着」这条闸"
    src = ast.unparse(gate)
    assert "poll()" in src, "它没去看进程还在不在"
    assert "SystemExit" in src, "壳死了它不判红 —— 那这条闸不存在"
    wired = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                             and n.name == "prove_the_installer_works"))
    assert "prove_the_shell_outlives_its_launcher" in wired, "写了没接线等于没有"


def test_the_install_check_reads_only_this_launch_from_the_shell_log() -> None:
    """日志是追加的：不带起点去读，会读到上一次启动的 READY 和一个早关了的端口。"""
    tree = ast.parse(_windows_packager_source())
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "read_the_shell_log_since"), None)
    assert fn is not None, "读壳日志没有起点参数"
    assert "seek" in ast.unparse(fn), "它没从 offset 读起 —— 会看到上一次启动说的话"
    wired = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                             and n.name == "prove_the_installer_works"))
    assert "st_size" in wired, "起壳前没记下日志已有多长 —— 那个 offset 从哪来"


def test_the_packager_puts_the_webview2_dlls_where_the_shell_looks() -> None:
    """随包 DLL 的「放处」必须等于壳的「找处」——同 git / tectonic 那两条闸。

    .NET 按 exe 所在目录探测程序集、原生 loader 也按同目录找，所以三个 DLL 要和
    `ScienceMate.exe` 并排。放到 `Resources/` 下就得给壳加探测配置 —— 那是给一件
    本来不需要配置的事发明配置，而配置会和代码分叉。
    """
    packager = _windows_packager_source()
    tree = ast.parse(packager)
    place = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "place_the_webview2_sdk"), None)
    assert place is not None, "打包器不随包 WebView2 SDK —— 干净机器上壳编不出/起不来"
    # 扫**这件事**，不扫措辞：注释里为了解释「为什么不放 Resources/」也会出现那个词。
    # 判据落在真实的路径表达式上（`app / …` 而不是 `resources / …`）。
    roots = {
        node.left.id
        for node in ast.walk(place)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        and isinstance(node.left, ast.Name)
    }
    assert "app" in roots, f"DLL 没放在壳旁边（app 目录）；实际用的根：{roots}"
    assert "resources" not in roots, "放进 Resources/ 了 —— 壳在那儿找不到"
    # 编壳时必须引用它们，且是 winexe（双击不闪控制台）
    assert "/target:winexe" in packager, "壳还是 target:exe —— 双击会闪一个黑框"
    assert "Microsoft.Web.WebView2.Core.dll" in packager


def test_the_install_check_proves_a_window_actually_appears() -> None:
    """装完自检必须断言**窗口真的出现了**，而且要被安装器自检调用。

    「窗口画不出来也验不了」曾经是我拿来换掉产品形态的理由。判据一直在
    （`EnumWindows` 按 pid 找标题），这条闸把它钉住，免得下次又用同一个借口。
    """
    packager = _windows_packager_source()
    tree = ast.parse(packager)
    gate = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "prove_the_window_appears"), None)
    assert gate is not None, "装完自检没有窗口判据"
    source = ast.get_source_segment(packager, gate) or ""
    assert "EnumWindows" in source and "SHELL_WINDOW_TITLE" in source
    assert any(isinstance(n, ast.Raise) for n in ast.walk(gate)), "没窗口必须让构建失败"

    installer = next((n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "prove_the_installer_works"), None)
    wired = {getattr(node.func, "id", "") for node in ast.walk(installer)
             if isinstance(node, ast.Call)}
    assert "prove_the_window_appears" in wired, "写了没接线等于没有"

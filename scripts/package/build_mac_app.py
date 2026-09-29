"""打一个 Mac 安装包：`.app` 拖进"应用程序"，双击出窗口。

## 判据

装完之后，一台**没装过 Python、没装过 Node、没装过 uv** 的 Mac 上双击图标就能
用。所以这个脚本的活是：把这套东西运行时需要的每一样东西都塞进 `.app` 里。

    ScienceMate.app/Contents/
      MacOS/ScienceMate              Swift 壳（窗口 + webview + 起后端）
      Resources/python/              独立 CPython + 全部依赖 + 本平台的包
      Resources/git/                 随包 git（新建项目要它）
      Resources/tectonic/bin/        随包 PDF 引擎 + 参考文献（没装 MacTeX 的 Mac 也出得了 PDF）
      Resources/biber/bin/
      Resources/AppIcon.icns
      Info.plist

## 为什么 harness 是"拷贝目录"而不是"pip 安装成包"

harness 的 `core/ shared/ nodes/` 里有 **700 多个非 .py 文件**（节点 spec 的
yaml、提示词的 md、论文模板的 tex），而它的 `pyproject.toml` 没有配
`package-data` —— CI 用的是 `pip install -e .`（可编辑安装，直接指着源码目录），
所以从来没人撞上这件事。真的 `pip install .` 会把这 700 个文件全丢掉，装出一个
能 import、但一跑就找不到自己 spec 的 harness。

拷贝目录还有一个好处：`.app` 里的布局跟一个 checkout 逐字一样（`cwd=root` +
`PYTHONPATH=root`），而那是这套东西**唯一被真正跑过**的配置。

依赖列表不抄第二份：从 harness 自己的 `pyproject.toml` 读。

## 用法

    python3 scripts/package/build_mac_app.py            # 全套，出 .app 和 .dmg
    python3 scripts/package/build_mac_app.py --skip-ui  # 界面已经构建过就跳过
"""
from __future__ import annotations

import argparse
import hashlib
import tempfile
import socket
import contextlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path


def _sibling(name: str):
    """按文件路径装载同目录的模块，**不碰 sys.path、不依赖包名**。

    仓库根和 platform/backend 各有一个叫 `scripts` 的包。谁先被 import，`sys.modules`
    里的 `scripts` 就是谁 —— 之后用包名 import 兄弟模块会解析到**错的那个包**。按路径
    装载绕开整件事。

    历史注记：这里原本还有第二条理由 —— 后端测试的扫盘 helper 会把打包器当模块
    `exec` 一遍来问"构建产物写在哪"，于是打包器任何导入期副作用都会让**每一条扫盘闸
    一起报错**（2026-09-08 实测：全量 8 红、单跑全绿）。那条路已随 #878 删除：扫盘语料
    改成问 `git ls-files`，`dist/` 由 `.gitignore` 排除，没人再 exec 打包器。
    """
    import importlib.util

    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"afs_package_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

payload_release = _sibling("payload_release")
git_tracked = _sibling("git_tracked")
# tectonic 与 biber 是一对（版本、下载地址、哈希、装完自检的探针）：两个打包器读同一份。
tex_toolchain = _sibling("tex_toolchain")

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "platform" / "backend"
FRONTEND = REPO / "platform" / "frontend"
SHELL = REPO / "platform" / "desktop" / "mac"
DIST = REPO / "dist"

APP_NAME = "ScienceMate"
BINARY_NAME = "ScienceMate"
#: 与 `credential_key._KEYCHAIN_SERVICE` 是同一个串（钥匙串条目按它取）。
BUNDLE_ID = "com.ieit.sciencemate"
PYTHON_VERSION = "3.14"

#: harness 里要随包走的**包**。`platform_runtime` 是 worker 的入口，
#: `core/ shared/ nodes/` 是它用到的全部包。`tests/ scripts/ docs/` 不在其中
#: —— 装机的人不跑它们。
HARNESS_PACKAGES = ("core", "shared", "nodes")

#: worker 的入口模块。仓库根上还有别的顶层模块（`chat.py` 等），它们**不写在
#: 这里** —— 见 `git_tracked.top_level_modules_reachable_from()`：清单是从 import
#: 里推出来的，不是手写的（三个打包器同一个）。
HARNESS_ENTRY = "platform_runtime.py"


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in command[:6])}{' …' if len(command) > 6 else ''}")
    return subprocess.run(command, check=True, **kwargs)


def step(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


# ─────────────────────────────────────────────────────── 1. 界面


def build_the_interface(skip: bool) -> Path:
    out = FRONTEND / "out"
    if skip and out.is_dir():
        print(f"  跳过（已有 {out}）")
        return out
    step("构建界面（静态导出）")
    # 每次都 `npm ci`，不问 node_modules 在不在。「在」只说明装过，不说明装的是
    # 这次 package-lock 要的那套 —— 2026-09-16 打 0.5.0 时构建机上留着 0.4.6 的
    # node_modules，界面里新加的 remark/katex 一族全 `Module not found`，构建死在
    # 一半。`npm ci` 本来就是幂等的，一分钟换一个不会撒谎的判据。
    run(["npm", "ci", "--no-audit", "--no-fund"], cwd=FRONTEND)
    run(["npm", "run", "build"], cwd=FRONTEND,
        env={**os.environ, "PLATFORM_STATIC_EXPORT": "1"})
    if not (out / "index.html").is_file():
        raise SystemExit("界面构建完了但没有 out/index.html —— 静态导出没生效")
    return out


# ─────────────────────────────────────────────────────── 2. 装配 wheel


def stage_into_the_package(interface: Path) -> None:
    """把界面和 harness 放到**启动器会去找的地方**。

    这两个位置不是这里定的，是 `app/launcher.py` 的 `find_static_ui()` /
    `find_the_harness()` 定的（它们各自的第二个来源就叫"随包分发"）。两处各写
    一个路径的话，改一处就会出现"装是装进去了、但它去别处找"这种谁都不报错的
    分叉。
    """
    step("把界面和 harness 装进包里")
    staged_ui = BACKEND / "app" / "static_ui"
    staged_harness = BACKEND / "app" / "harness"
    for target in (staged_ui, staged_harness):
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(interface, staged_ui)
    staged_harness.mkdir(parents=True)
    # 只拷 git 跟踪着的（见 `git_tracked`）：整棵 copytree 会把开发机上 `nodes/`、`shared/`
    # 里的 `.DS_Store` 之类一起装进包。
    shipped = git_tracked.copy_the_tracked_files(REPO, [*HARNESS_PACKAGES, HARNESS_ENTRY],
                                                 staged_harness)
    for package in HARNESS_PACKAGES:
        if not any(name.startswith(f"{package}/") for name in shipped):
            raise SystemExit(f"harness 少了 {package}/ —— 装出来的包跑不了 worker")
    if HARNESS_ENTRY not in shipped:
        raise SystemExit(f"harness 少了 {HARNESS_ENTRY}（git 没跟踪它）—— 装出来的包跑不了 worker")
    modules = git_tracked.carry_the_top_level_modules(REPO, staged_harness)
    if modules:
        print(f"  顺带需要的顶层模块：{', '.join(sorted(modules))}")
    marker = staged_harness / "core" / "agent_loop.py"
    if not marker.is_file():
        raise SystemExit("harness 装进去了但没有 core/agent_loop.py —— 启动器认不出它")
    ui_files = sum(1 for _ in staged_ui.rglob("*") if _.is_file())
    harness_files = sum(1 for _ in staged_harness.rglob("*") if _.is_file())
    print(f"  界面 {ui_files} 个文件，harness {harness_files} 个文件")


def unstage_from_the_package() -> None:
    """把装配进去的东西撤走。见 `main()` 里那段注释。"""
    for name in ("static_ui", "harness",
                 payload_release.VENDOR_DIR, payload_release.SERVER_BUNDLE_DIR):
        shutil.rmtree(BACKEND / "app" / name, ignore_errors=True)


def uv() -> str:
    """uv 在哪 —— 问 `payload_release`，**要用的那一刻才问**。

    Mac 上 uv 一直在 PATH 里，所以这里裸写 `"uv"` 从没出过事；而共用的装配层在
    Windows 构建机上就是这么 `WinError 2` 的（2026-09-21 打 0.5.2 专业版）。

    写成函数而不是模块级常量，是因为**这个文件会被测试 import**，而跑测试的机器
    不是构建机：在 import 那一刻去找 uv，等于让「没装 uv」变成「这个模块 import 不了」。
    第一版就是常量，CI 当场红给我看。
    """
    return payload_release.the_uv_executable()


def build_the_wheel() -> Path:
    step("打 wheel")
    out = DIST / "wheel"
    shutil.rmtree(out, ignore_errors=True)
    run([uv(), "build", "--wheel", "-o", str(out)], cwd=BACKEND)
    wheels = sorted(out.glob("*.whl"))
    if not wheels:
        raise SystemExit("没打出 wheel")
    wheel = wheels[-1]
    # 光"打出来了"不算数：ignore 规则、hatch 的文件选择都可能把刚放进去的东西
    # 又筛掉，而那种失败要等到双击图标才现形。
    import zipfile

    names = zipfile.ZipFile(wheel).namelist()
    for needed in ("app/harness/core/agent_loop.py", "app/static_ui/index.html"):
        if needed not in names:
            raise SystemExit(f"wheel 里没有 {needed} —— 装出来的包会缺半边")
    print(f"  {wheel.name}（{len(names)} 个文件，{wheel.stat().st_size // 1024} KB）")
    return wheel


# ─────────────────────────────────────────────────────── 3. 运行时


def harness_dependencies() -> list[str]:
    """harness 运行时要的第三方包 —— 从它自己的 pyproject 读，不抄第二份。"""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def standalone_python() -> Path:
    """一份可以整个搬走的 CPython。

    用 uv 下的那套 python-build-standalone：它就是为"拷到别处也能跑"造的。
    系统自带的 python3 不行 —— 那是用户机器上的东西，版本、位置、有没有都不
    归我们管，而"装个应用还要先装 Python"正是这一版要消灭的前提。
    """
    step(f"取一份独立的 CPython {PYTHON_VERSION}")
    run([uv(), "python", "install", PYTHON_VERSION])
    # `uv python find` 会优先返回**项目自己的 .venv**（仓库根一 `uv sync` 就有一个同版本
    # 的），而那份搬不走。两条都会把它拽回 .venv：cwd 在项目里、以及 `uv run` 设的
    # `VIRTUAL_ENV`。所以问的时候两条都断：cwd 换到非项目目录 + 剥掉激活变量 —— 与
    # build_windows_app 同一处理（2026-09-16 打 0.5.0 时 Mac 也撞上了）。
    away_env = {k: v for k, v in os.environ.items()
                if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX")}
    with tempfile.TemporaryDirectory() as away:
        found = subprocess.run([uv(), "python", "find", PYTHON_VERSION], check=True,
                               capture_output=True, text=True, cwd=away, env=away_env).stdout.strip()
    executable = Path(found)
    # `<root>/bin/python3` → root。uv 也可能指到系统 python（那种没有 root），
    # 所以这里要认出来并说清楚。
    root = executable.parent.parent
    if not (root / "lib").is_dir() or "uv" not in str(root):
        raise SystemExit(
            f"uv 给的是系统 Python（{executable}），它搬不走。\n"
            f"先跑：uv python install {PYTHON_VERSION}")
    print(f"  {root}")
    return root


#: 随包分发的 git。**用户不该为了打开一个应用先去装 Xcode。**
#:
#: 平台的每个 Project 仓库都是 git 仓库（init / commit / worktree / diff /
#: show-ref / rev-parse 全在用），而 macOS 上的 `/usr/bin/git` 只是一个 shim：
#: 没装 Command Line Tools 时它弹一个"要安装开发者工具吗"的系统框然后失败。
#: 目标用户是同事不是开发者，他们的 Mac 大概率没装 —— 那样"新建项目"这个
#: 装完后的第一个动作就当场失败，而我们所有自测都在装着 CLT 的开发机上做。
#:
#: 编的是**只做本地仓库**的那一份：`NO_CURL / NO_OPENSSL / NO_EXPAT` —— 我们
#: 从不从应用里走网络推拉，去掉之后二进制只依赖系统自带的
#: libSystem / libz / libiconv，可以整个搬走。
GIT_VERSION = "2.47.1"
GIT_SOURCE = f"https://mirrors.edge.kernel.org/pub/software/scm/git/git-{GIT_VERSION}.tar.gz"

#: worker 与后端都要用到的那些 git 操作。装完之后逐条真跑一遍 —— "编出来了"
#: 和"它能干我们要它干的事"是两件事。
_GIT_MUST_DO = ("init", "commit", "worktree", "diff", "show-ref", "rev-parse")


def megabytes_on_disk(root: Path) -> int:
    """目录真实占用（MB）。

    **硬链接只算一次** —— git 的 libexec 里 143 个命令是同一个 inode 的硬链接，
    按文件逐个相加会把 19 MB 报成 598 MB，然后让人以为哪里出了问题。
    """
    seen: set[int] = set()
    total = 0
    for file in root.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        info = file.stat()
        if info.st_ino in seen:
            continue
        seen.add(info.st_ino)
        total += info.st_size
    return total // 1024 // 1024


def build_a_portable_git(app: Path) -> Path:
    """编一份可以整个搬走的 git，放进 .app。"""
    step(f"编一份随包分发的 git {GIT_VERSION}")
    target = app / "Contents" / "Resources" / "git"
    work = DIST / "git-src"
    tarball = DIST / f"git-{GIT_VERSION}.tar.gz"
    if not tarball.is_file():
        run(["curl", "-sSL", "--max-time", "300", "-o", str(tarball), GIT_SOURCE])
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    run(["tar", "xzf", str(tarball), "-C", str(work), "--strip-components=1"])
    shutil.rmtree(target, ignore_errors=True)
    run(["make", "-j8", f"prefix={target}",
         "NO_CURL=1", "NO_OPENSSL=1", "NO_GETTEXT=1", "NO_TCLTK=1",
         "NO_PERL=1", "NO_PYTHON=1", "NO_EXPAT=1", "install"],
        cwd=work, capture_output=True)
    binary = target / "bin" / "git"
    if not binary.is_file():
        raise SystemExit("git 没编出来")
    _prove_the_git_works(binary)
    print(f"  {binary}（{megabytes_on_disk(target)} MB）")
    return binary


def _prove_the_git_works(binary: Path) -> None:
    """在**没有开发者工具**的环境里，把我们真正用到的操作各跑一遍。

    `DEVELOPER_DIR` 指到一个不存在的地方 = 模拟一台没装 Xcode 的机器；`PATH`
    收成系统目录 = 保证跑的是这一份、不是开发机上碰巧有的另一个 git。
    """
    import subprocess
    import tempfile

    env = {"DEVELOPER_DIR": "/nonexistent", "PATH": "/usr/bin:/bin",
           "HOME": tempfile.mkdtemp(prefix="git-probe-home-")}
    scratch = Path(tempfile.mkdtemp(prefix="git-probe-"))
    repo = scratch / "repo"
    author = ["-c", "user.email=probe@local", "-c", "user.name=probe"]

    def git(*args, cwd=repo):
        done = subprocess.run([str(binary), *args], cwd=str(cwd), env=env,
                              capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise SystemExit(
                f"随包 git 跑不了 `git {' '.join(args)}`：{done.stderr.strip()[:200]}")
        return done.stdout.strip()

    repo.mkdir(parents=True)
    git("init", "-q", ".")
    git(*author, "commit", "-q", "--allow-empty", "-m", "genesis")
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    git("add", "f.txt")
    git(*author, "commit", "-q", "-m", "one")
    git("worktree", "add", "-q", str(scratch / "wt"), "HEAD")
    if not (scratch / "wt" / "f.txt").is_file():
        raise SystemExit("随包 git 的 worktree 没建出内容")
    if git("diff", "--name-only", "HEAD~1", "HEAD") != "f.txt":
        raise SystemExit("随包 git 的 diff 不对")
    git("rev-parse", "--git-common-dir")
    shutil.rmtree(scratch, ignore_errors=True)
    print(f"  验过：{', '.join(_GIT_MUST_DO)}（在没有开发者工具的环境里）")


def download(url: str, dest: Path) -> None:
    """抓到 dest：HTTP 错误当场失败、瞬时失败重试；截断由调用方按哈希收货抓住。"""
    run(["curl", "-fsSL", "--retry", "4", "--retry-all-errors", "--max-time", "900",
         "-o", str(dest), url])


def place_the_tex_toolchain(app: Path) -> None:
    """随包 PDF 引擎 tectonic + 参考文献 biber → ``Resources/{tectonic,biber}/bin/``。

    launcher 的 ``_bundled_binary`` 找的就是这两处，并把它们接上 PATH。没装 MacTeX 的 Mac
    以前出不了 PDF —— writing 写完、最后一步编译才发现。版本与哈希、为什么是一对：见
    ``tex_toolchain``。签名在 ``sign()`` 里统一做（按 Mach-O 扫盘，这两件也在内）。
    """
    step(f"取 tectonic {tex_toolchain.TECTONIC_VERSION} + biber {tex_toolchain.BIBER_VERSION}"
         "（PDF 引擎与参考文献）")
    resources = app / "Contents" / "Resources"
    tex_toolchain.place("macos", "tectonic", resources / "tectonic" / "bin" / "tectonic", download)
    tex_toolchain.place("macos", "biber", resources / "biber" / "bin" / "biber", download)


def install_the_runtime(app: Path, wheel: Path) -> Path:
    step("把运行时装进 .app")
    source = standalone_python()
    target = app / "Contents" / "Resources" / "python"
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, symlinks=True)
    # uv 在它管的那份 Python 里放了一张 `EXTERNALLY-MANAGED`，写着"这份由 uv
    # 管理，别改它"。那句话对**原件**是对的（改了会影响机器上所有用它的项目），
    # 对这份拷贝不成立：它已经在 .app 里，只属于这一个应用。留着它的后果是任何
    # 安装器都拒绝往里装东西，包括我们自己这一步。
    (target / "lib" / f"python{PYTHON_VERSION}" / "EXTERNALLY-MANAGED").unlink(missing_ok=True)
    python = target / "bin" / "python3"
    # 用 uv 往那份 Python 里装，不走 `ensurepip` + `pip`。
    #
    # 理由一：uv 不要求目标环境里先有 pip（实测 ensurepip 在这份独立构建里自举
    # 失败）。理由二：uv 是**构建机**的工具，不是用户机器的 —— 装完之后
    # `.app` 里只有 CPython 和一堆包，跟 uv 再无关系。
    packages = [str(wheel), *harness_dependencies()]
    run([uv(), "pip", "install", "--python", str(python), "--quiet", *packages])
    # 装完立刻问一句：这三样东西在不在。缺了的话现在报，比双击之后报好。
    check = subprocess.run(
        [str(python), "-c",
         "import app.launcher as L, json;"
         "print(json.dumps({'harness': str(L.find_the_harness()),"
         " 'ui': str(L.find_static_ui())}))"],
        check=True, capture_output=True, text=True, cwd=str(target))
    facts = json.loads(check.stdout)
    for name, value in facts.items():
        if value in ("None", ""):
            raise SystemExit(f"装进去的运行时找不到 {name}")
        print(f"  {name}: …{value[-60:]}")
    return python


# ─────────────────────────────────────────────────────── 4. 壳与图标


def build_the_shell(app: Path) -> None:
    step("编译壳")
    binary = app / "Contents" / "MacOS" / BINARY_NAME
    binary.parent.mkdir(parents=True, exist_ok=True)
    run(["swiftc", "-O", "-target", "arm64-apple-macos13.0",
         "-framework", "AppKit", "-framework", "WebKit",
         str(SHELL / "Shell.swift"), "-o", str(binary)])


def draw_the_icon(app: Path) -> None:
    """图标。

    没有图标的应用在 Dock 里是一张白纸，看起来像装坏了 —— 而这是用户看到的
    第一样东西。用 CoreGraphics 现画，不往仓库里塞二进制。
    """
    step("画图标")
    source = SHELL / "MakeIcon.swift"
    iconset = DIST / "AppIcon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    tool = DIST / "makeicon"
    run(["swiftc", "-O", "-framework", "AppKit", str(source), "-o", str(tool)])
    run([str(tool), str(iconset)])
    icns = app / "Contents" / "Resources" / "AppIcon.icns"
    icns.parent.mkdir(parents=True, exist_ok=True)
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)])


def write_the_plist(app: Path) -> None:
    plist = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": BINARY_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": payload_release.the_version(),
        "CFBundleVersion": payload_release.the_version(),
        "LSMinimumSystemVersion": "13.0",
        # 界面和后端都在本机，走的是明文 http://127.0.0.1 —— ATS 默认不放行。
        # 只开 loopback 这一条例外，不是整个关掉 ATS。
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        # 这是个有窗口的正经应用，不是后台代理。
        "LSUIElement": False,
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))


# ─────────────────────────────────────────────────────── 5. 签名与 dmg


#: Mach-O 魔数（32/64 位、两种字节序，外加 fat 二进制的四种）。用来认"这个文件
#: 是不是原生代码"——按扩展名认不出来：`.so`、无后缀的 `bin/git-*`、`dylib`、
#: `python3` 本体各长各样，而 site-packages 里的 `.so` 才是大头。
MACH_O_MAGICS = {bytes.fromhex(value) for value in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe",
    "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}


def sign(app: Path) -> None:
    """临时签名（ad-hoc），**从最深的原生二进制往外签**。

    没有开发者账号就签不出能分发的名字，但**不签**的话，带原生二进制的 app 在
    Apple Silicon 上会被直接杀掉（不是"警告一下"，是起不来）。ad-hoc 签名让它
    在本机能跑；发给别人经浏览器下载仍会被 Gatekeeper 拦（对方要走系统设置 → 隐私与
    安全性 → 仍要打开；或改走 install.sh 不经浏览器，那就不拦），要彻底
    干净得走开发者账号 + 公证。

    不用 `--deep`：Apple 自己说它"只在应急时用"，而且它对**嵌套但不是标准 bundle
    布局**的东西（我们的 `Resources/python/lib/.../*.so`、`Resources/git/libexec/*`）
    覆盖不全 —— 漏签的那个 `.so` 要到运行时 dlopen 才崩。所以按 Mach-O 魔数扫盘、
    路径深的先签、最后签 bundle 本身（外层签名要把内层的签名一起算进去，顺序反了
    外层立刻失效）。

    验证是**硬闸**：`--deep --strict` 不过就当场失败。原来这里只把 codesign 的最后
    一行打出来就算完，签坏了也只是多一行看不懂的输出。
    """
    step("临时签名")
    # **不开 hardened runtime**（`--options runtime`）。它只为公证而存在，而 ad-hoc 签名
    # 本来就公证不了；开了却有实打实的代价：hardened runtime 带库校验 —— 进程只肯加载
    # 与自己同一 Team ID 签的库，而 ad-hoc 签名没有 Team ID。于是包内的 python3 一
    # dlopen `site-packages/pydantic_core/*.so` 就死：`mapping process and mapped file
    # (non-platform) have different Team IDs`（2026-09-16 打 0.5.0 时装完自检抓到的）。
    # 0.4.6 没撞上纯属侥幸：那时 `--deep` 根本没签到 Resources/python 里的解释器，
    # 它没有 hardened runtime，也就没有库校验。有了开发者证书再开：那时要配套
    # entitlements（disable-library-validation / allow-unsigned-executable-memory）。
    common = ["codesign", "--force", "--sign", "-", "--timestamp=none"]
    natives = []
    for path in sorted(app.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) in MACH_O_MAGICS:
                natives.append(path)
    # **一个 inode 只签一次，签完把同伴重新挂回去。**
    #
    # git 的 `libexec/git-core/` 里 167 个命令是**同一个 inode 的硬链接**（上游 git
    # 就这么装）。`codesign --force` 是"重写这个文件"，于是签第二个的时候链接断开，
    # 167 个命令各自变成一份 4.2 MB 的实体 —— 2026-09-16 打 0.5.0 时实测：
    # `Resources/git` 从 21 MB 涨到 599 MB，dmg 从 245 MB 涨到 530 MB。同事的下载量
    # 翻了一倍，而没有任何东西报错。（0.4.6 没这个问题，因为那时是一条
    # `codesign --deep`，它根本没逐个重写 libexec 里的文件。）
    #
    # 签完再 `os.link` 回去是安全的：它们本来就是同一个二进制，上游 git 让它们共用
    # 一份身份，签名自然也该是同一份。
    groups: dict[int, list[Path]] = {}
    for path in natives:
        groups.setdefault(path.stat().st_ino, []).append(path)
    # 这里不走 `run()`：嵌套的原生二进制有上千个，一行一条命令会把构建日志淹掉。
    relinked = 0
    for members in groups.values():
        first, rest = members[0], members[1:]
        subprocess.run([*common, str(first)], check=True, capture_output=True)
        for twin in rest:
            twin.unlink()
            os.link(first, twin)
            relinked += 1
    run([*common, str(app)], capture_output=True)
    verify_the_signature(app)
    print(f"  {len(natives)} 个原生二进制（{len(groups)} 个 inode）+ bundle 本身已签并验过"
          + (f"，{relinked} 个硬链接已挂回" if relinked else ""))


def verify_the_signature(app: Path) -> None:
    """签名还成立吗 —— 失败就抬走。

    包装好之后还会有人往 bundle 里写东西（最典型的是包内验收跑起来时子解释器写进去
    的 `.pyc`）。bundle 里多一个字节，签名就不再成立，而这件事在打包机上一声不响：
    要等同事双击才变成"应用已损坏"。所以验收前后各验一次，而且是 `--strict`。
    """
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        capture_output=True)


def make_the_dmg(app: Path, edition: str = "personal") -> Path:
    step("打 dmg")
    staging = DIST / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    # `ditto` 而不是 `cp -R`：**cp 不保留硬链接**。git 的 libexec 里 143 个命令
    # 是同一个 inode，`cp -R` 会把它们展开成 143 份实体副本 —— 实测 dmg 因此
    # 从 213 MB 涨到 499 MB，比它装的那个 .app（409 MB）还大。
    run(["ditto", str(app), str(staging / app.name)])
    os.symlink("/Applications", staging / "Applications")
    # 专业版的 dmg 名带 -Pro：两种发行的桌面包内容几乎一样，名字是人分得清它们的唯一办法。
    suffix = "-Pro" if edition == "pro" else ""
    dmg = DIST / f"{APP_NAME.replace(' ', '-')}{suffix}-{payload_release.the_version()}-arm64.dmg"
    dmg.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", str(staging),
         "-ov", "-format", "UDZO", str(dmg)], capture_output=True)
    print(f"  {dmg}（{dmg.stat().st_size // 1024 // 1024} MB）")
    return dmg


# ─────────────────────────────────────────────────── 6. 发布目录（不买证书也能发）

#: 收尾说给打包的人听的话。2026-09-08 改：原来写「右键 →「打开」」—— macOS 15 起
#: 这条捷径没了，照着做的人会以为包坏了。现在写的是**真实的**两条路：不经浏览器
#: （零提示），或者经浏览器（四步）。签名证书以后买了再把四步删掉。
HOW_TO_INSTALL = """
发布目录已装配好（dmg + SHA256SUMS + install.sh + 可自更新的载荷 + manifest）：
  {release}
发到 Forgejo：  FORGEJO_TOKEN=user:token python3 scripts/package/publish_release.py
（或整个目录放内网 HTTP / 共享盘）。**安装和自更新读的是同一个地址。**

同事怎么装（第一条没有任何提示）：
  A. 一条命令 —— macOS：  curl -fsSL {url}/install.sh | sh
                Windows：  irm {url}/install.ps1 | iex
     两条都不经浏览器，于是文件不带来源标记：Gatekeeper / SmartScreen 根本不会去
     问它签没签名。共享盘拷过来也一样。
  B. 浏览器下 dmg → 拖进「应用程序」→ 第一次打开会被拦（未签名）：
     系统设置 → 隐私与安全性 → 拉到底「仍要打开」→ 再确认一次。每台机器一次。
     Windows 浏览器下 Setup.exe → 「Windows 已保护你的电脑」→ 更多信息 → 仍要运行。
"""


#: 一条「别处打好的发布件」：名字 + 它的 SHA-256。字节不用过来，清单里那一行就够了。
FOREIGN_ENTRY = re.compile(r"^(?P<name>[^\s/\\]+)=(?P<sha256>[0-9a-f]{64})$")


def a_foreign_asset(text: str) -> tuple[str, str]:
    """解析 `--windows-installer 名字=sha256`。只认小写 64 位十六进制 —— 少一位、大写、
    带路径，都是拷贝时抄错了，当场拒绝比把错哈希写进清单强。"""
    found = FOREIGN_ENTRY.match(text.strip())
    if not found:
        raise SystemExit(f"--windows-installer 要写成 名字=64位小写sha256，不是 {text!r}")
    return found.group("name"), found.group("sha256")


def write_the_wire(release: Path) -> Path:
    """这一版组织服务器对外的「线」（`app/pro/org_wire.py`）—— 随发布件一起传上去。

    下一次发布时 `publish_release.py` 拿它跟那一版比：线断了而协议号没动，拒绝发布。
    仓库里那张快照（tests/pro/contracts/org_wire.json）管的是开发中的每个 PR；这一张管的是
    **真的发出去、装在别人机器上的那一版** —— 兼容承诺是对它许的。

    在装配之后跑（`app.main` 要 import 得起来），在 `assemble_the_release_dir` 之前落盘
    （它会进 SHA256SUMS，回读时一并核对）。用的是跑这个打包器的解释器 —— 打包本来就
    必须用后端 venv 的 python。
    """
    out = release / "org_wire.json"
    subprocess.run([sys.executable, "-m", "app.pro.org_wire", "--write", str(out)], cwd=BACKEND, check=True)
    return out


def assemble_the_release_dir(dmg: Path, release_url: str,
                             foreign: dict[str, str] | None = None) -> Path:
    """把「发出去要用的东西」放进同一个目录：dmg、校验和、安装脚本、载荷、manifest。

    一个目录一个 URL：install.sh 从这里拿 dmg，装好的应用从这里拿更新。两件事一个
    地址，就不会出现"安装包更新了、更新源没跟上"这种谁都不报错的分叉。

    SHA256SUMS 覆盖目录里每一个会被下载的文件 —— install.sh 核对 dmg，自更新自己
    核对载荷（manifest 里有），这里再列一遍是给人用 `shasum -c` 核对的。

    `foreign` 是**在别的机器上打好、不会经过这台机器**的发布件（Windows 的 Setup.exe
    在 Windows 构建机上生、在 Forgejo 所在的那台 PC 上传，369 MB 没有理由先拖到 Mac
    再拖回去 —— 2026-09-21 真拖过一次：scp 退出 0 却只传了 49 MB）。清单需要的只是
    它的哈希，所以只收哈希；文件要是碰巧在本地，就核一遍它没在撒谎。
    """
    step("装配发布目录")
    release = DIST / "release"
    release.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dmg, release / dmg.name)

    template = (Path(__file__).resolve().parent / "install.sh").read_text(encoding="utf-8")
    # 整行替换，只替换赋值那两行。占位符若出现在别处（注释、判断），那是模板的错，
    # 不是这里该将就的 —— 所以替换后再查一次没有残留。
    script = (template.replace('DEFAULT_URL="__RELEASE_URL__"', f'DEFAULT_URL="{release_url.strip().rstrip("/")}"')
                      .replace('DEFAULT_DMG="__DMG_NAME__"', f'DEFAULT_DMG="{dmg.name}"'))
    for token in ("__RELEASE_URL__", "__DMG_NAME__"):
        if token in script.replace("'__RELEASE''_URL__'", ""):
            raise SystemExit(f"install.sh 模板里 {token} 出现在赋值行之外 —— 替换后会留下残留")
    (release / "install.sh").write_text(script, encoding="utf-8")
    (release / "install.sh").chmod(0o755)

    # Windows 那条同一个道理、同一个形状：`irm …/install.ps1 | iex` 不经浏览器，
    # 于是文件不带网络来源标记，SmartScreen 不会去问它签没签名。
    # 它只需要烧一个地址 —— 安装器叫什么由发布目录的 SHA256SUMS 说了算（一个真相源）。
    ps1 = (Path(__file__).resolve().parent / "install.ps1").read_text(encoding="utf-8")
    ps1_baked = ps1.replace("$DefaultUrl = '__RELEASE_URL__'",
                            f"$DefaultUrl = '{release_url.strip().rstrip('/')}'")
    if "__RELEASE_URL__" in ps1_baked.replace("('__RELEASE' + '_URL__')", ""):
        raise SystemExit("install.ps1 模板里 __RELEASE_URL__ 出现在赋值行之外 —— 替换后会留下残留")
    (release / "install.ps1").write_text(ps1_baked, encoding="utf-8")

    lines = []
    for path in sorted(release.iterdir()):
        # 两个安装脚本不进清单：它们的内容会在发布到 Forgejo 那一刻被改写成指向
        # 那个 release 的地址，清单里记下现在这一份的哈希只会立刻过期。
        if path.is_file() and path.name not in ("SHA256SUMS", "install.sh", "install.ps1"):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}")
    for name, digest in sorted((foreign or {}).items()):
        local = release / name
        if local.is_file():
            actual = hashlib.sha256(local.read_bytes()).hexdigest()
            if actual != digest:
                raise SystemExit(f"{name} 就在发布目录里，可它的哈希是 {actual[:12]}…，"
                                 f"不是你给的 {digest[:12]}… —— 两边有一个是错的，别发")
            continue                      # 本地有，上面那个循环已经列过它
        lines.append(f"{digest}  {name}")
        print(f"  {name}：不在这台机器上，按给的哈希列进清单")
    (release / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for path in sorted(release.iterdir()):
        print(f"  {path.name}（{path.stat().st_size // 1024} KB）")
    if not release_url:
        print("  install.sh 里暂无地址：publish_release.py 发到 Forgejo 时会改成那个 release 的下载地址；"
              "不经 Forgejo 直接放内网时请传 --release-url，或装的人 export AFS_RELEASE_URL")
    return release


def the_extras_for_the_payload(app: Path, windows_shell_dir: str | None) -> dict[str, Path]:
    """载荷第二个归档要带的东西：Mac 壳（从刚编好的 .app 里取）+ Windows 壳（从传过来的目录取）。

    ## 为什么 Windows 的壳是「传过来」的

    Windows 壳只能在 Windows 上编（in-box csc），而发布目录在 Mac 上装配。所以 Windows
    打包器把壳（exe + 3 个 DLL）连同一份 `SHA256SUMS` 落到 `dist/shell-windows/`，和
    Setup.exe 一起 scp 过来；这里**先核那份 SHA256SUMS 再收** —— 传坏了当场拒绝，
    不让半截的壳进到签过名的载荷里去。

    没给 `--windows-shell-dir` 就只带 Mac 壳：这不是错误（比如只发 Mac），但会印出来。
    """
    extras: dict[str, Path] = {}
    mac_dir = DIST / "shell-macos"
    shutil.rmtree(mac_dir, ignore_errors=True)
    mac_dir.mkdir(parents=True)
    binary = app / "Contents" / "MacOS" / BINARY_NAME
    if not binary.is_file():
        raise SystemExit(f"Mac 壳还没编出来：{binary} —— 得先 build_the_shell 再打载荷")
    shutil.copy2(binary, mac_dir / BINARY_NAME)
    extras["shell/macos"] = mac_dir

    if windows_shell_dir:
        win = Path(windows_shell_dir).expanduser().resolve()
        sums = win / "SHA256SUMS"
        if not sums.is_file():
            raise SystemExit(f"{win} 里没有 SHA256SUMS —— 那不是 Windows 打包器导出的壳目录")
        # 核对单是 Windows 上写的。`splitlines()` 切 CRLF、`strip()` 吃 \r，所以这里本来就
        # 认 CRLF —— 2026-09-12 真机撞上的只是 Mac 上的 `shasum -c`（它不认），装配端没坏过。
        # 别再往这里加一层 strip：变异证明那是死代码（M8）。
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, _, name = line.strip().partition("  ")
            target = win / name
            if not target.is_file():
                raise SystemExit(f"Windows 壳目录缺 {name}")
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise SystemExit(f"Windows 壳文件 {name} 传坏了（sha256 对不上）—— 不收")
        # SHA256SUMS 自己不进归档：它是运输途中的核对单，不是壳的一部分。
        clean = DIST / "shell-windows-verified"
        shutil.rmtree(clean, ignore_errors=True)
        shutil.copytree(win, clean, ignore=shutil.ignore_patterns("SHA256SUMS"))
        extras["shell/windows"] = clean
        print(f"  Windows 壳已核对并收进载荷（{win}）")
    else:
        print("  ⚠️ 没给 --windows-shell-dir：这份载荷只带 Mac 壳，Windows 用户这次拿不到壳的更新")

    extras["app"] = the_backend_app_for_the_payload()
    return extras


#: 装配时打包器放进 `BACKEND/app`、**要随载荷的 `app/` 一起走**的东西：专业版的 ssh 库与服务器包
#: （`payload_release.stage_the_pro_only_things` —— 自更新上来的安装只能从 `app/` 拿到它们）。
#: 它们是构建产物、不在 git 里，所以按名字点出来；`app/` 里除此之外只收 git 跟踪的文件。
#: 界面和 harness 也是装配进去的，但它们走载荷的第一个归档（各自是一个更新单元），不在这里。
BUILT_INTO_THE_BACKEND_APP = (payload_release.VENDOR_DIR, payload_release.SERVER_BUNDLE_DIR)


def the_backend_app_for_the_payload() -> Path:
    """载荷第二个归档里的 `app/`：客户端启动时把它接过来（`launcher.hand_over_to_the_payload_app`）。

    = `platform/backend/app` 里 git 跟踪着的文件（去掉测试，见 `git_tracked`）+
    `BUILT_INTO_THE_BACKEND_APP`。不是"装配态的 `BACKEND/app` 整棵拷出来再按名字排除" ——
    那样工作树里有什么（`.DS_Store`、没提交的草稿模块）更新里就有什么。

    要在 `stage_the_pro_only_things` 之后、`unstage_from_the_package` 之前调：那时专业版的
    两样东西才在 `BACKEND/app` 里。
    """
    source = BACKEND / "app"
    if not (source / "launcher.py").is_file():
        raise SystemExit(f"{source} 里没有 launcher.py —— 那不是后端的 app 包")
    staged = DIST / "app-for-the-payload"
    shutil.rmtree(staged, ignore_errors=True)
    under = source.relative_to(REPO).as_posix()
    git_tracked.copy_the_tracked_files(REPO, [under], staged, under=under)
    built = [name for name in BUILT_INTO_THE_BACKEND_APP if (source / name).is_dir()]
    for name in built:
        shutil.copytree(source / name, staged / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = sum(1 for _ in staged.rglob("*") if _.is_file())
    print(f"  后端 app/ 收进载荷（{files} 个文件：git 跟踪的"
          + "".join(f" + {name}/" for name in built) + "）")
    return staged


# ─────────────────────────────────────────────────────── main


def prove_the_package_works(app: Path, edition: str = "personal") -> None:
    """用**包里那个 python** 起一次，让验收脚本走完整条路。

    2026-09-07：我把一个首页 404 的包装进了 /Applications，然后才发现 —— 界面
    没接上，API 却全好，`doctor` 还把界面路径报得清清楚楚。包是"由一堆单独都对
    的东西装起来的"，装完是不是还对，只有真起一次才知道。

    验收脚本本来就支持 `--url`（验一个已经在跑的实例），所以这里不另写一份判据
    ——两份判据分叉的时候，两边都不会报错。
    """
    python = app / "Contents" / "Resources" / "python" / "bin" / "python3"
    home = Path(tempfile.mkdtemp(prefix="package-check-"))
    port = _a_free_port()
    step(f"装完之后验一次（端口 {port}）")
    # 后端日志接**文件**而不是 PIPE：没人读的 PIPE 在日志写满缓冲区那一刻把后端卡住，
    # 于是验收超时，报出来的是"走不通验收"——指着一个假原因。落盘之后失败还留得下证据。
    log_path = home.parent / f"{home.name}.log"
    log_handle = log_path.open("wb")
    succeeded = False
    server = subprocess.Popen(
        # -B / PYTHONDONTWRITEBYTECODE：**签好的 bundle 一个字节都不能再动**。包里那个
        # python 一旦往 `Resources/` 写 .pyc，签名当场失效，而这里没有任何东西会报错 ——
        # 要等同事双击看到"应用已损坏"。不加 -I：isolated 会把这里设的 PYTHON* 一起关掉。
        [str(python), "-B", "-m", "app.launcher", "start", "--port", str(port), "--no-browser"],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PLATFORM_DATA_ROOT": str(home),
             "HARNESS_FRAMEWORK_HOME": str(home)},
        stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        done = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "acceptance" / "personal_smoke.py"),
             "--url", f"http://127.0.0.1:{port}"],
            capture_output=True, text=True, timeout=420,
        )
        print(done.stdout.strip())
        if done.stderr.strip():
            print(done.stderr.strip())
        if done.returncode != 0:
            raise SystemExit(
                f"\n❌ 这个包起来之后走不通验收（exit {done.returncode}）。"
                "\n包已经在 dist/ 里，但**别装它** —— 先看上面那行说的是哪一步。")
        # 发行是读 `Resources/edition.json` 得来的：放错一层就静默变成个人版。问包自己。
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/capabilities", timeout=10) as r:
            reported = json.load(r).get("edition")
        if reported != edition:
            raise SystemExit(f"\n❌ 包报的发行是 {reported!r}，要打的是 {edition!r} —— edition.json 没落在解释器旁边")
        print(f"  ✅ 包自报发行 = {reported}")
        payload_release.the_package_carries_no_pro_edition(app, edition)
        succeeded = True
    finally:
        import signal

        import psutil

        try:
            workers = psutil.Process(server.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            workers = []
        # terminate() 只杀 launcher 这一个进程；它起的 server/worker 还活着，占着端口和
        # 数据根。整组一起收（Popen 用了 start_new_session，所以 pid 就是进程组 id）。
        if server.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(server.pid, signal.SIGTERM)
        with contextlib.suppress(Exception):
            server.wait(timeout=20)
        log_handle.close()
        # 「答完了」不等于「收得了摊」。后端退出之后 worker 要自己退干净，否则装出去的
        # 包会在同事机器上留一堆跑着的进程 —— 这件事只有在这里等一等才看得见。
        deadline = time.monotonic() + 80
        while workers and time.monotonic() < deadline:
            workers = [p for p in workers if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
            if workers:
                time.sleep(0.2)
        leaked = [p.pid for p in workers]
        for worker in workers:
            with contextlib.suppress(psutil.NoSuchProcess):
                worker.kill()
        if succeeded and not leaked:
            print("  ✅ 后端退出后，验收起的 worker 已自行退干净")
            shutil.rmtree(home, ignore_errors=True)
            log_path.unlink(missing_ok=True)
        else:
            # 失败时把日志尾巴打出来、数据根留着：没有证据的失败只能重跑一次再猜。
            print(log_path.read_text("utf-8", errors="replace")[-16000:])
            print(f"自检失败，证据保留：{home} ; {log_path}")
        if leaked:
            raise SystemExit(f"❌ 后端退出 80 秒后还有 worker 活着：{leaked}")


def prove_the_app_makes_the_platforms_pdf(app: Path) -> None:
    """装完自检：用包里的 python、**只有随包的 TeX**、一个全新用户的家，真编一次平台的 PDF。

    打包机装着 MacTeX：PATH 上有 latexmk，编译器选择先挑它 —— 那样自检只证明了打包机能出
    PDF。所以 PATH 只留系统目录（随包的 tectonic / biber 由 launcher 接上去，和用户双击时
    一样），HOME 是空的：缓存从零开始，证明「第一次编译先取宏包」这条路在墙里走得通（墙
    断网，取包只在框架自己的样本上联网；2026-09-24 本机空缓存约 4 分钟）。判据落在产物上：
    样本编出了 PDF，而且编它的是**包里的**那两件。
    """
    step("装完自检：只用随包的 TeX、在一个全新用户的家里，真编一次平台的 PDF")
    python = app / "Contents" / "Resources" / "python" / "bin" / "python3"
    bundled = (app / "Contents" / "Resources").resolve()
    work = Path(tempfile.mkdtemp(prefix="mac-pdf-check-")).resolve()
    probe = work / "pdf_probe.py"
    probe.write_text(tex_toolchain.PDF_PROBE, encoding="utf-8")
    (work / "home").mkdir()
    env = {
        "HOME": str(work / "home"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        # 签好的 bundle 一个字节都不能再动（见 prove_the_package_works）。
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    passed = False
    try:
        proc = subprocess.run(
            [str(python), "-B", str(probe), str(work / "data")],
            env=env, capture_output=True, text=True, timeout=1800)
        record = tex_toolchain.read_the_probe_record(proc.stdout)
        if not record:
            raise SystemExit(
                "❌ 装完自检：PDF 实测探针自己没跑起来 —— 先怀疑自检，别急着怪产品。\n"
                f"stdout={proc.stdout[-1500:]!r}\nstderr={proc.stderr[-1500:]!r}")
        tools = record.get("identity") or {}
        foreign = [f"{name}={tools.get(name)}" for name in ("binary", "biber")
                   if not str(tools.get(name) or "").startswith(str(bundled))]
        if foreign:
            raise SystemExit(
                f"❌ 装完自检：编译用的不是包里的那两件（{'；'.join(foreign)}）—— "
                "这次实测证明不了同事的 Mac 能出 PDF")
        if not record.get("works"):
            raise SystemExit(
                f"❌ 装完自检：装好的应用出不了平台的 PDF —— {record.get('reason')!r}\n"
                "   用户会在 writing 最后一步撞上它。")
        passed = True
        print(f"  ✅ 只用随包的 tectonic + biber 编出了平台的 PDF（空缓存起步，{record.get('seconds')}s）")
    finally:
        if passed:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"自检失败，现场保留：{work}")


def _a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-ui", action="store_true", help="界面已构建过就不再构建")
    parser.add_argument("--no-dmg", action="store_true", help="只出 .app")
    parser.add_argument("--skip-check", action="store_true",
                        help="不跑装完之后那次验收（只在明知会失败、就是要拿包去调试时用）")
    parser.add_argument("--release-url", default="",
                        help="发布目录将来放在哪（如 http://<内网主机>/afs）。烧进 install.sh；"
                             "不给也能打包，装的人得自己 export AFS_RELEASE_URL")
    parser.add_argument("--windows-shell-dir", default="",
                        help="Windows 打包器导出的壳目录（dist/shell-windows，含 SHA256SUMS）。"
                             "给了就把 Windows 壳打进更新载荷的附加归档；不给只带 Mac 壳")
    parser.add_argument("--windows-installer", action="append", default=[], metavar="名字=sha256",
                        help="Windows 安装器留在打它的那台机器上，这里只收它的哈希写进 SHA256SUMS。"
                             "可重复。install.ps1 靠这份清单认出安装器叫什么")
    parser.add_argument("--edition", choices=("personal", "pro"), default="personal",
                        help="哪种发行（EXEC_PLAN_TWO_EDITIONS §1）：只差 edition.json 与自更新源")
    parser.add_argument("--update-source", default="",
                        help="烧进包里的自更新源（仓库网址）。专业版必给（它的更新在私有仓库）；"
                             "个人版留空 = 代码里的出厂值（公开仓库）")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        raise SystemExit("这个脚本只打 Mac 包。")
    # 个人版是开源发行：只从导出的公开树打（见 payload_release 里那条的来历）。
    payload_release.the_personal_edition_is_built_from_the_public_tree(REPO, args.edition)
    # 能不能签，第一分钟就说，别等五分钟编完 git 再死在签名那步。
    # 有发布私钥 = 这台是发布机 = 必须签得出来；没私钥 = 未签名路径，不需要库。
    release_keys = _sibling("release_keys")

    if release_keys.private_key_path().is_file():
        try:
            import cryptography  # noqa: F401
        except ImportError:
            raise SystemExit(
                f"这台机器有发布私钥（{release_keys.private_key_path()}），但当前 python "
                f"({sys.executable}) 没有 cryptography，签不了载荷。\n"
                "  要么：pip install cryptography\n"
                "  要么：用后端的 venv 跑这个脚本（它本来就带）"
            )

    DIST.mkdir(exist_ok=True)
    app = DIST / f"{APP_NAME}.app"
    shutil.rmtree(app, ignore_errors=True)
    (app / "Contents" / "Resources").mkdir(parents=True)

    build_a_portable_git(app)
    place_the_tex_toolchain(app)
    # 壳先于载荷编：载荷的附加归档要带上壳本身（#953），所以壳得在打载荷之前就存在。
    # 编壳只要 app 骨架 + Shell.swift，不依赖运行时，挪到前面没有副作用。
    build_the_shell(app)
    interface = build_the_interface(args.skip_ui)
    # 服务器包在装配之前打：`stage_the_pro_only_things` 要把它放进 app/。（它只拷 git 跟踪的
    # 文件，装配进来的 app/static_ui、app/harness 不会混进去。）
    server_bundle = None
    if args.edition == "pro":
        # 服务器包复用刚导出的这份界面（同一棵树、同一个 STATIC_EXPORT），不打第二次。
        step("打组织服务器包")
        import build_server_bundle
        server_bundle = build_server_bundle.build(interface=interface, update_source=args.update_source)
    stage_into_the_package(interface)
    payload_release.stage_the_pro_only_things(BACKEND, args.edition, server_bundle)
    try:
        # 载荷在**装配之后、撤走之前**打：更新里的内容与这份全新安装逐字节一致。
        # 版本标记也是这一步写进 staged harness 的，所以随后打进 wheel 的那份
        # harness 同样带着它 —— 装好的应用靠它知道自己是哪一版。
        step("打可自更新的载荷")
        payload_release.build_the_payload(
            BACKEND / "app" / "harness", BACKEND / "app" / "static_ui", DIST / "release",
            extras=the_extras_for_the_payload(app, args.windows_shell_dir),
            edition=args.edition,
            # 组织服务器自己升级时，从签过名的 manifest 里找这一项。
            server_bundle=server_bundle)
        if args.edition == "pro":
            write_the_wire(DIST / "release")
        wheel = build_the_wheel()
    finally:
        # 装配只在"打 wheel"那一刻需要。留在工作树里的代价是**改变了源码树的
        # 答案**：`find_static_ui()` 的第二个来源就是包里那份，于是跑过一次打包
        # 的机器上，从源码起服务会拿到一份陈旧的界面，而没有任何东西会报错。
        # 2026-09-06 实测：两条测试因此转红（其中一条是"扫盘看到的应该正好是
        # git 跟踪的东西"）——它们拦对了。
        unstage_from_the_package()
    install_the_runtime(app, wheel)
    draw_the_icon(app)
    write_the_plist(app)
    # 发行写在签名之前：它是 bundle 的一部分，签完再写会破签名。
    payload_release.write_the_edition(app / "Contents" / "Resources", args.edition, args.update_source)
    sign(app)

    if not args.skip_check:
        prove_the_package_works(app, args.edition)
        prove_the_app_makes_the_platforms_pdf(app)
        # 验收是**在 bundle 里面**跑的：那一趟有没有往签好的包里写进东西（.pyc、
        # 缓存、日志），只有再验一次签名才知道。先打 dmg 再发现就晚了。
        verify_the_signature(app)
    print(f"\n✅ {app}（{megabytes_on_disk(app)} MB）")
    if not args.no_dmg:
        dmg = make_the_dmg(app, args.edition)
        foreign = dict(a_foreign_asset(entry) for entry in args.windows_installer)
        release = assemble_the_release_dir(dmg, args.release_url, foreign)
        print(HOW_TO_INSTALL.format(release=release, url=args.release_url or "http://<内网主机>/afs"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

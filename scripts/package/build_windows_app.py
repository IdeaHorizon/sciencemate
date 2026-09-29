"""打一个 Windows 便携应用：一个目录，双击 `ScienceMate.exe` 起后端 + 开界面。

蓝本是 `build_mac_app.py`（同一套装配逻辑），换掉平台相关的几件：
- 没有 `.app` bundle / plist / 签名 / dmg —— Windows 出一个**便携目录**
  `dist/ScienceMate/`，整个拷走即用（免管理员，正是这一版要的）：

    ScienceMate/
      ScienceMate.exe               C# 壳（#868，csc 现编）
      backend.json               壳按它起后端（executable/args/cwd/env）
      Resources/python/          独立 CPython + backend wheel + 全部依赖
      Resources/tectonic/bin/tectonic.exe   随包 PDF 引擎（#865/#870 的落点）
      Resources/biber/bin/biber.exe         参考文献（tectonic 从 PATH 调它）

- 独立 Python 用 uv 的 python-build-standalone（Windows 上 `python.exe` 在根，
  不是 `bin/python3`）。
- 壳用 in-box `csc.exe` 编（目标机无管理员、无 VS）。

**布局即接线**：`Resources/python/python.exe` 的 `sys.prefix.parent` 正是
`Resources`，于是 `launcher.find_the_bundled_tectonic()` 在
`Resources/tectonic/bin/tectonic.exe` 找得到（#870），`find_the_bundled_git()`
在 `Resources/git/bin/git.exe` 找得到（#871）—— backend.json 不用塞 env，放对
位置就自动生效。

## 这一版做什么 / 不做什么
做：python + 随包 harness + 全部依赖 + tectonic + **随包 git(PortableGit)** + 壳 +
backend.json，装完自检＝独立 python 跑 `doctor`（harness/pdf/git/sandbox 都在**包内**）
+ 壳起后端 `/health/ready` 200。
**做**（`--with-ui`）：连前端 static_ui 一起 build（Next.js 静态导出）→ 界面看得见。
默认不带 `--with-ui`（API-only），要界面加它。
**暂缺（各自一轮跟进）**：
- 最终压成单文件自解压/安装器。

用法（在目标 Windows 机上，构建机要有 uv）：
    uv run --python 3.12 python scripts/package/build_windows_app.py
"""
from __future__ import annotations

import argparse
import hashlib
import contextlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from pathlib import Path

# 这是**构建脚本**、不是产品入口，不走 launcher.ensure_utf8_mode（那条只管产品运行时）。
# 但它打印 ▶/✅/中文，Windows 默认 cp1252 控制台一 encode 就崩（#862 同一类）。直接把
# 两条输出流掰成 utf-8；子进程捕获也一律按 utf-8 读（doctor 的输出含中文）。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            _stream.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "platform" / "backend"
FRONTEND = REPO / "platform" / "frontend"
SHELL_DIR = REPO / "platform" / "desktop" / "windows"
DIST = REPO / "dist"


def _load_file(path: Path, name: str):
    """按文件路径装载一个模块，不碰 sys.path（理由见 build_mac_app 里 `_sibling`）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # 先登记再执行（importlib 文档的做法）：模块里的 dataclass 按字符串注解查
    # ``sys.modules[cls.__module__]``，没登记就在装载时崩（core/isolation/_native.py 实测）。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sibling(name: str):
    """按文件路径装载同目录的模块。"""
    return _load_file(Path(__file__).resolve().parent / f"{name}.py", f"afs_package_{name}")


#: `\\?\` 扩展路径：便携目录里 site-packages 嵌得深，超过 MAX_PATH 的成员
#: `os.walk`/`rglob` 会**静默跳过**（N38）—— 打进 zip 的就少了文件，谁也不报错。
#: 遍历、删目录、算体积一律走扩展路径；对外展示/启动仍用普通路径。
io_path = _load_file(REPO / "shared/lib/filesystem.py", "afs_build_filesystem").io_path
#: 模型 shell 里 ``python3`` 这个名字怎么补 —— 与运行时（``payload_environment``）同一个函数。
python3_is_python = _load_file(REPO / "core/isolation/_native.py",
                               "afs_build_isolation_native").python3_is_python


payload_release = _sibling("payload_release")
git_tracked = _sibling("git_tracked")
# tectonic 与 biber 是一对（版本、下载地址、哈希、装完自检的探针）：两个打包器读同一份。
tex_toolchain = _sibling("tex_toolchain")

APP_DIR_NAME = "ScienceMate"
PYTHON_VERSION = "3.12"
HARNESS_PACKAGES = ("core", "shared", "nodes")
HARNESS_ENTRY = "platform_runtime.py"

# 随包 git：PortableGit（Git for Windows 的便携版）。选它不选 MinGit，是因为它有
# `bin/git.exe`（+ 完整运行树 mingw64/usr/…），正对上 `find_the_bundled_git()` 找的
# `<Resources>/git/bin/git.exe`（#871）；MinGit 的 git 在 `cmd/`，对不上。tag
# `vX.Y.Z.windows.N` 里的 `.windows.` 到资源名会塌成 `.`（vA.B.C.windows.D →
# PortableGit-A.B.C.D-64-bit.7z.exe）。pin 版本＝可复现（同 mac 包 pin GIT_VERSION）。
GIT_TAG = "v2.55.0.windows.5"
GIT_VERSION = "2.55.0.5"
PORTABLE_GIT_URL = (
    "https://github.com/git-for-windows/git/releases/download/"
    f"{GIT_TAG}/PortableGit-{GIT_VERSION}-64-bit.7z.exe"
)
CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
#: 每次调 csc 都要带的两个开关。
#:
#: `/codepage:65001` 是**防御**，不是在修一个已经发生的缺陷 —— 这一点 2026-09-21
#: 一度被我自己搞错，记在这里免得下次再错一遍。
#:
#: 事实：仓库里的 .cs 是 UTF-8 无 BOM，而 csc 按机器的 ANSI 代码页读源文件。
#: 这台构建机的 ANSI 代码页恰好就是 UTF-8，所以**带不带这个开关，编出来的中文常量
#: 码点都是对的**（当天拿一个最小样本两种编法各跑一次，`卸载` 都是 5378 8F7D）。
#: 但这是这台机器的运气，不是这段代码的性质：换一台 ANSI 不是 UTF-8 的机器，
#: 每个中文字符串常量在编译那一刻就成了 `?`。判据不该依赖构建机的区域设置。
#:
#: 当天真正让中文变成 `??` 的是**另一件事**：`WScript.Shell` 建快捷方式走 ANSI
#: （`Unable to save shortcut "...\?? ScienceMate.lnk"`）。我先把它归给了 csc，
#: 靠那个最小样本才分开 —— 同型：[[feedback_report_is_not_evidence]]，一个看上去
#: 吻合的解释不是证据。
#:
#: `/utf8output` 让编译器自己的诊断也走 UTF-8，构建日志才读得出来。
CSC_FLAGS = ("/nologo", "/codepage:65001", "/utf8output")

# 自解压 Setup.exe 的 footer 魔数，必须与 platform/desktop/windows/InstallerStub.cs
# 的 MAGIC 逐位一致（改一处必改另一处，否则安装器认不出自己的 payload）。
INSTALLER_MAGIC = 0x3150495A5F534641

UV = "uv"  # main() 里定位后覆盖


def step(title: str) -> None:
    print(f"\n\u25b6 {title}", flush=True)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(str(c) for c in command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def download(url: str, dest: Path, *, attempts: int = 4, timeout: int = 120) -> None:
    """把 url 抓到 dest —— **带超时 + 重试 + 完整性校验**。

    以前直接 `urllib.request.urlretrieve(url, dest)`：没超时（网络卡住就永远挂着）、
    没重试（一个瞬时抖动整个构建就崩）、没完整性校验（半截文件当成功、下一步解压/校验
    才莫名其妙地炸）。打包器要下三个大件（tectonic ~15MB、PortableGit ~50MB），在真实
    网络上瞬时失败很常见——本 session 在 box 上就反复撞到（下 PortableGit 卡住/截断）。
    构建要**确定性**：一个会因网络运气成败的构建脚本，等于「装得上」没保障。所以：
    每次 `urlopen(timeout=)` 流式落盘 + 比对 Content-Length（截断即判失败）+ 失败退避重试。
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                expected = int(response.headers.get("Content-Length") or 0)
                with open(dest, "wb") as handle:
                    shutil.copyfileobj(response, handle)
            got = dest.stat().st_size
            if expected and got != expected:
                raise OSError(f"截断：只拿到 {got}/{expected} 字节")
            if got == 0:
                raise OSError("下到 0 字节")
            return
        except Exception as exc:  # noqa: BLE001 - 网络什么都可能抛，一律重试
            last = exc
            print(f"  下载失败（第 {attempt}/{attempts} 次）：{exc}", flush=True)
            dest.unlink(missing_ok=True)  # 别把半截文件留给下一次/下一步
            if attempt < attempts:
                time.sleep(2 * attempt)  # 线性退避：2s, 4s, 6s
    raise SystemExit(f"下载 {url} 失败（{attempts} 次都没成）：{last}")


def find_uv() -> str:
    """uv 是**构建机**的工具（不进包）。目标机上它常不在 PATH。

    答案只有一个出处：`payload_release.the_uv_executable()`。这里曾经自己找一遍，
    而共用的装配层（`_vendor_one`）直接调裸 `uv` —— 于是同一台机器上，打包器找得到、
    装配层找不到，专业版 Windows 包在 `WinError 2` 上死了好几次。
    """
    return payload_release.the_uv_executable()


def build_the_interface(skip: bool) -> Path:
    """构建界面（Next.js 静态导出）→ `platform/frontend/out`。

    `PLATFORM_STATIC_EXPORT=1` 让 next.config 切成 `output: "export"`，产出纯静态文件、
    由后端自己 serve（`find_static_ui()` 的落点）。Windows 上 `npm` 是 `npm.cmd`——
    subprocess 直接起 bare `"npm"` 过不了 CreateProcess，走 `cmd /c npm …` 让它自己
    从 PATH 认 `npm.cmd`（同 build_mac_app 的 `npm`，只是 Windows 要包一层）。
    """
    out = FRONTEND / "out"
    if skip and (out / "index.html").is_file():
        print(f"  跳过（已有 {out}）")
        return out
    step("构建界面（静态导出）")
    # 每次都 `npm ci`，不问 node_modules 在不在（理由见 build_mac_app 同一处：
    # 2026-09-16 构建机上留着上一版的 node_modules，新依赖全 `Module not found`）。
    run(["cmd", "/c", "npm", "ci", "--no-audit", "--no-fund"], cwd=FRONTEND)
    run(["cmd", "/c", "npm", "run", "build"], cwd=FRONTEND,
        env={**os.environ, "PLATFORM_STATIC_EXPORT": "1"})
    index = out / "index.html"
    if not index.is_file():
        raise SystemExit("界面构建完了但没有 out/index.html —— 静态导出没生效")
    print(f"  {out}")
    return out


def stage_into_the_package(interface: Path | None) -> None:
    """把 harness（和界面，若 build 了）放到启动器会去找的地方。

    位置由 `app/launcher.py` 的 `find_the_harness()`/`find_static_ui()` 定
    （各自的第二个来源＝随包分发）。两处各写一份就会"装是装进去了、但它去别处
    找"这种谁都不报错的分叉。
    """
    step("把 harness 装进包里")
    staged_ui = BACKEND / "app" / "static_ui"
    staged_harness = BACKEND / "app" / "harness"
    for target in (staged_ui, staged_harness):
        shutil.rmtree(target, ignore_errors=True)
    if interface is not None:
        shutil.copytree(interface, staged_ui)
    staged_harness.mkdir(parents=True)
    # 只拷 git 跟踪着的（见 `git_tracked`，与 Mac、服务器包同一条规则）。
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
    # 版本标记：装好的应用靠它知道自己是哪一版。**没有它不是"版本未知"，是"任何更新
    # 都比它新"**（is_newer(x, None) 恒真）—— Windows 版少了这一步的话，界面会永远
    # 挂着「有新版本」，点了更新、重启、还是有，因为新装的那份同样没有标记。
    # Mac 打包器在 build_the_payload 里顺带写；Windows 不打载荷（载荷跨平台通用，
    # 由 Mac 那边打一份就够），所以在这里显式写。
    version = payload_release.the_version()
    payload_release.write_the_version_marker(staged_harness, version)
    print(f"  版本标记 {payload_release.VERSION_MARKER} = {version}")
    harness_files = sum(1 for _ in staged_harness.rglob("*") if _.is_file())
    print(f"  harness {harness_files} 个文件"
          + (f"，界面 {sum(1 for _ in staged_ui.rglob('*') if _.is_file())} 个文件"
             if interface is not None else "（无界面，API-only）"))


def unstage_from_the_package() -> None:
    """撤走装配进去的东西 —— 留在工作树里会**改变源码树的答案**
    （`find_static_ui()`/`find_the_harness()` 的第二个来源就是包里那份）。"""
    for target in (BACKEND / "app" / "static_ui", BACKEND / "app" / "harness",
                   BACKEND / "app" / payload_release.VENDOR_DIR,
                   BACKEND / "app" / payload_release.SERVER_BUNDLE_DIR):
        shutil.rmtree(target, ignore_errors=True)


def build_the_wheel(with_ui: bool) -> Path:
    step("打 wheel")
    out = DIST / "wheel"
    shutil.rmtree(out, ignore_errors=True)
    run([UV, "build", "--wheel", "-o", str(out)], cwd=BACKEND)
    wheels = sorted(out.glob("*.whl"))
    if not wheels:
        raise SystemExit("没打出 wheel")
    wheel = wheels[-1]
    names = zipfile.ZipFile(wheel).namelist()
    needed = ["app/harness/core/agent_loop.py"]
    if with_ui:
        needed.append("app/static_ui/index.html")
    for want in needed:
        if want not in names:
            raise SystemExit(f"wheel 里没有 {want} —— 装出来的包会缺半边")
    print(f"  {wheel.name}（{len(names)} 个文件，{wheel.stat().st_size // 1024} KB）")
    return wheel


def harness_dependencies() -> list[str]:
    """harness 运行时要的第三方包 —— 从它自己的 pyproject 读，不抄第二份。"""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def standalone_python() -> Path:
    """一份可以整个搬走的 CPython（uv 的 python-build-standalone）。"""
    step(f"取一份独立的 CPython {PYTHON_VERSION}")
    run([UV, "python", "install", PYTHON_VERSION])
    # `uv python find` 会优先返回**项目自己的 .venv**（本仓库 .venv 也是 3.12，撞车），
    # 而那份搬不走。两条都会把它拽回 .venv：cwd 在项目里、以及 `uv run` 设的
    # `VIRTUAL_ENV`。所以问的时候两条都断：cwd 换到**非项目目录** + 剥掉 `VIRTUAL_ENV`
    # 一族激活变量（`--python-preference only-managed` 实测不管用，仍回 .venv）。
    away_env = {k: v for k, v in os.environ.items()
                if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX")}
    with tempfile.TemporaryDirectory() as away:
        found = subprocess.run([UV, "python", "find", PYTHON_VERSION], check=True,
                               capture_output=True, text=True, encoding="utf-8",
                               cwd=away, env=away_env).stdout.strip()
    executable = Path(found)
    # Windows 上 `python.exe` 就在根（不是 `bin/python3`）。
    root = executable.parent
    if not (root / "python.exe").is_file() or "uv" not in str(root).replace("\\", "/"):
        raise SystemExit(
            f"uv 给的不是可搬走的独立 Python（{executable}）。\n"
            f"先跑：uv python install {PYTHON_VERSION}")
    print(f"  {root}")
    return root


def give_python_its_python3_name(runtime: Path) -> None:
    r"""包里的解释器目录**打包时**就带上 ``python3.exe``（与 ``python.exe`` 逐字节相同）。

    模型在 shell 里敲 ``python3``；Windows 上没有一种解释器布局带这个名字，落空就是应用
    商店的桩（静默、退出码 49）。运行时 ``python3_is_python`` 会往解释器目录补，但那是
    **安装目录**：装在 ``C:\Program Files\...`` 下时普通权限写不进，名字照旧缺着；而且
    运行时往安装目录写文件，本身就违背「更新只落数据根」（self_update.py）。所以名字在
    这里放好、随安装器一起落地；运行时遇到逐字节相同的文件就不写。

    必须和 ``python.exe`` **同目录**：独立 CPython 按自己的位置找 ``python3XX.dll`` 与
    标准库。这里放不好就停下 —— 不像运行时那样「写不进就算了」：包是给所有用户的。
    """
    python3_is_python(runtime)
    alias, python = runtime / "python3.exe", runtime / "python.exe"
    if not (alias.is_file() and alias.read_bytes() == python.read_bytes()):
        raise SystemExit(f"没能在 {runtime} 里放好 python3.exe（与 python.exe 同一个文件）")
    print("  python3.exe = python.exe（模型 shell 里敲 python3 落到这份解释器）")


def install_the_runtime(resources: Path, wheel: Path) -> Path:
    step("把运行时装进包（Resources/python）")
    source = standalone_python()
    target = resources / "python"
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)
    # uv 在它管的 Python 里放了 `EXTERNALLY-MANAGED`，对这份拷贝不成立（它已经
    # 只属于这个应用），留着任何安装器都拒绝往里装，包括我们下一步。
    (target / "Lib" / "EXTERNALLY-MANAGED").unlink(missing_ok=True)
    python = target / "python.exe"
    give_python_its_python3_name(target)
    packages = [str(wheel), *harness_dependencies()]
    run([UV, "pip", "install", "--python", str(python), "--quiet", *packages])
    # 装完立刻问一句：harness 在不在（缺了现在报，比双击之后报好）。
    check = subprocess.run(
        [str(python), "-c",
         "import app.launcher as L, json;"
         "print(json.dumps({'harness': str(L.find_the_harness())}))"],
        check=True, capture_output=True, text=True, encoding="utf-8", cwd=str(target))
    facts = json.loads(check.stdout)
    if facts.get("harness") in ("None", ""):
        raise SystemExit("装进去的运行时找不到 harness")
    print(f"  harness: \u2026{facts['harness'][-60:]}")
    return python


def place_the_tectonic(resources: Path) -> None:
    """随包 PDF 引擎 → `Resources/tectonic/bin/tectonic.exe`（finder #870 的落点）。"""
    step(f"取 tectonic {tex_toolchain.TECTONIC_VERSION}（PDF 引擎）")
    tex_toolchain.place("windows", "tectonic", resources / "tectonic" / "bin" / "tectonic.exe", download)


def place_the_biber(resources: Path) -> None:
    """随包参考文献处理器 → `Resources/biber/bin/biber.exe`（launcher 把它接上 PATH）。

    为什么必须带、版本为什么是这一版：见 ``tex_toolchain`` 的模块说明。
    """
    step(f"取 biber {tex_toolchain.BIBER_VERSION}（参考文献，对 tectonic 宏包里的 biblatex）")
    tex_toolchain.place("windows", "biber", resources / "biber" / "bin" / "biber.exe", download)


def place_the_git(resources: Path) -> None:
    """随包 git → `Resources/git/`（PortableGit，有 `bin/git.exe` + 完整运行树）。

    平台的每个 Project 都是 git 仓库（init/commit/worktree/diff…全在用）。装机的人
    不是开发者——不该为了打开一个应用先去装 git。mac 那边编一份 git 进包，Windows
    这边取 PortableGit 自解压进包，同一个道理。放对 `<Resources>/git/bin/git.exe` 后，
    `find_the_bundled_git()`（#871）+ `prepare_the_environment` 自动把它接上 PATH。
    """
    step("取 PortableGit（随包 git）")
    target = resources / "git"
    shutil.rmtree(target, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tmp:
        sfx = Path(tmp) / "PortableGit.7z.exe"
        print(f"  下载 {PORTABLE_GIT_URL}")
        download(PORTABLE_GIT_URL, sfx)
        # 7-Zip 自解压：`-y -o<dir>`（`-o` 后无空格，是 SFX 的约定）。
        run([str(sfx), "-y", f"-o{target}"])
    git_exe = target / "bin" / "git.exe"
    if not git_exe.is_file():
        raise SystemExit(f"PortableGit 解压后没有 {git_exe} —— finder 会找不到")
    # 证它真能跑（不是空壳、运行树齐）。
    proof = subprocess.run([str(git_exe), "--version"],
                           capture_output=True, text=True, encoding="utf-8")
    if proof.returncode != 0 or "git version" not in proof.stdout:
        raise SystemExit(f"包内 git 跑不起来：{proof.stdout}{proof.stderr}")
    print(f"  {git_exe}：{proof.stdout.strip()}")
    # 成对再校验模型的 shell：experiment 节点跑的每条实验命令都走 `bash_shell()`，
    # 在 Windows 上就是 `shared.lib.shell._windows_bash` 的首选——随包布局
    # `<Resources>/git/usr/bin/bash.exe`（即 PortableGit 的 MSYS2 bash）。干净机器上
    # 没有系统 Git for Windows，这份就是唯一的 bash：缺了它，模型要么当场没 shell、
    # 要么裸 `bash.exe` 命中 WindowsApps 的 WSL 启动器、把命令整个丢进 WSL2（RFC #848
    # 明拒的路线）。`bin/git.exe` 在**不**等于 `usr/bin/bash.exe` 在——MinGit 就只有前者
    # 没有后者。git 那个锚 finder 会校验；bash 这个锚以前没人校验，正是这里补上。
    model_bash = target / "usr" / "bin" / "bash.exe"
    if not model_bash.is_file():
        raise SystemExit(
            f"PortableGit 解压后没有 {model_bash} —— 模型的 shell"
            f"（shared.lib.shell._windows_bash 首选那份随包 MSYS bash）会找不到，"
            f"experiment 节点在干净机器上当场没 bash"
        )
    print(f"  {model_bash}：模型的 shell 就位")
    # 把「平台的行尾/长路径不变量」显式钉进随包 git 的**系统配置**（Resources/git/etc/
    # gitconfig，随包一起走）。平台每个 Project 都是 git 仓库，attempt 沙箱靠
    # `git worktree add` / `git checkout` 从提交态签出工作树——而 Git for Windows 默认
    # `core.autocrlf=true`（真机实测系统 git 就是 true），签出时把 LF→CRLF：提交的 shell
    # 脚本签出成 `#!/bin/bash\r`，在 MSYS bash 里当场 `bad interpreter: /bin/bash^M`；数据 /
    # .tex 按字节读也被污染。平台内容是**跨平台字节精确**的（脚本在 bash 跑、数据按字节读），
    # CRLF 转换对它一律是错——显式 `autocrlf=false`（签出/提交都不转，字节精确）。顺带
    # `longpaths=true`（RFC §11 机制）：数据根 %LOCALAPPDATA%\afs 下嵌套深，干净机器 longpath
    # 关时深路径 git 操作会 `Filename too long`。设在**系统配置**＝随包这份 git 的所有仓库
    # 统一生效（launcher 把它 prepend 进 PATH，平台每条 git 都用它），不靠每仓 .gitattributes
    # （现有 .gitattributes 只保 .json/.yaml，脚本 /.tex /.py 漏在外）。
    for cfg_key, cfg_value in (("core.autocrlf", "false"), ("core.longpaths", "true")):
        set_cfg = subprocess.run(
            [str(git_exe), "config", "--system", cfg_key, cfg_value],
            capture_output=True, text=True, encoding="utf-8")
        if set_cfg.returncode != 0:
            raise SystemExit(f"设随包 git 系统配置 {cfg_key}={cfg_value} 失败："
                             f"{set_cfg.stdout}{set_cfg.stderr}")
        # 读回：确认这份随包 git 真读得到刚写的值。
        got = subprocess.run(
            [str(git_exe), "config", "--get", cfg_key],
            capture_output=True, text=True, encoding="utf-8")
        if got.stdout.strip() != cfg_value:
            raise SystemExit(f"随包 git 系统配置 {cfg_key} 没生效："
                             f"读回 {got.stdout.strip()!r} ≠ {cfg_value!r}")
        # 关键：确认它落进了**包内**的系统配置文件，而不是包外的 %PROGRAMDATA%\Git\config
        # ——后者装到别的机器上就没了，读回却照样为真（本机 git 也读 %PROGRAMDATA%），
        # 光靠上面的读回抓不出来。Git for Windows 的 --system 是安装目录相对的（真机
        # READ-ONLY 验过 `<install>/etc/gitconfig`），对便携包＝target 下；这里钉死，将来
        # git 改了 --system 的写入位置当场露出来（那时改用 `--file <target>/etc/gitconfig`）。
        origin = subprocess.run(
            [str(git_exe), "config", "--system", "--show-origin", "--get", cfg_key],
            capture_output=True, text=True, encoding="utf-8")
        origin_file = origin.stdout.split("\t", 1)[0].removeprefix("file:").strip()
        if not origin_file or not Path(origin_file).resolve().is_relative_to(target.resolve()):
            raise SystemExit(
                f"随包 git 的 {cfg_key} 落在包外（{origin_file!r}）——装到别的机器上就没了，"
                f"该在 {target} 下。git --system 写入位置变了，改用 --file <target>/etc/gitconfig"
            )
        print(f"  随包 git 系统配置 {cfg_key}={cfg_value}（在包内 {origin_file}）")


#: WebView2 SDK（官方 NuGet）。壳靠它把界面显示在**自己的窗口**里，而不是甩给浏览器。
#: 只取三个文件：两个托管程序集（csc 引用 + 运行时加载）和一个原生 loader。
#: 版本钉死 —— 不钉的话不同机器打出来的包引用不同 API 面，而分叉时两边都不报错。
WEBVIEW2_SDK_VERSION = "1.0.2903.40"
WEBVIEW2_SDK_URL = (
    f"https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/{WEBVIEW2_SDK_VERSION}")
WEBVIEW2_MEMBERS = {
    "lib/net462/Microsoft.Web.WebView2.Core.dll": "Microsoft.Web.WebView2.Core.dll",
    "lib/net462/Microsoft.Web.WebView2.WinForms.dll": "Microsoft.Web.WebView2.WinForms.dll",
    "runtimes/win-x64/native/WebView2Loader.dll": "WebView2Loader.dll",
}


def place_the_webview2_sdk(app: Path) -> Path:
    """随包 WebView2 SDK → **壳自己旁边**（`ScienceMate.exe` 同目录）。

    为什么不放 `Resources/`：这三个 DLL 是**壳的**依赖，.NET 按 exe 所在目录探测程序集、
    原生 `WebView2Loader.dll` 也按同目录找。放别处就得给壳加探测配置 —— 那是给一件
    本来不需要配置的事发明配置。

    运行时（Edge WebView2 Runtime）**不随包**：Windows 11 自带，且它是系统组件、
    该由系统更新。装完自检会真起一次窗口，运行时缺席那一刻就会现形，不靠猜。
    """
    step("取 WebView2 SDK（壳的窗口）")
    app.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "webview2.nupkg"
        print(f"  下载 {WEBVIEW2_SDK_URL}")
        download(WEBVIEW2_SDK_URL, pkg)
        with zipfile.ZipFile(pkg) as zf:
            names = set(zf.namelist())
            missing = [m for m in WEBVIEW2_MEMBERS if m not in names]
            if missing:
                raise SystemExit(
                    f"❌ WebView2 SDK 包里没有 {missing} —— 版本 {WEBVIEW2_SDK_VERSION} 的布局变了")
            for member, filename in WEBVIEW2_MEMBERS.items():
                with zf.open(member) as src, open(app / filename, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                print(f"  {app / filename}（{(app / filename).stat().st_size} 字节）")
    return app


#: 画出来的图标落在这儿；壳与安装器都拿它当 `/win32icon`。
ICON = DIST / "ScienceMate.ico"


def draw_the_icon() -> Path:
    """现画一个多尺寸 .ico，不往仓库里塞二进制（和 mac 那边同一个做法）。

    没有图标的 exe 顶着 .NET 默认图标 —— 桌面上摆一个那个，就是 mac 那边说的
    「Dock 里是一张白纸，看起来像装坏了」。而快捷方式的全部意义就是一眼认出来。
    """
    step("画图标")
    tool = DIST / "makeicon.exe"
    run([CSC, "/nologo", "/target:exe", f"/out:{tool}",
         "/r:System.Drawing.dll", str(SHELL_DIR / "MakeIcon.cs")])
    run([str(tool), str(ICON)])
    tool.unlink(missing_ok=True)
    if not ICON.is_file() or ICON.stat().st_size < 1024:
        raise SystemExit("❌ 图标没画出来")
    print(f"  {ICON}（{ICON.stat().st_size} 字节）")
    return ICON


def build_the_shell(app: Path) -> None:
    step("编译壳（csc）")
    binary = app / "ScienceMate.exe"
    core = app / "Microsoft.Web.WebView2.Core.dll"
    winforms = app / "Microsoft.Web.WebView2.WinForms.dll"
    for dll in (core, winforms, app / "WebView2Loader.dll"):
        if not dll.is_file():
            raise SystemExit(f"❌ 编壳之前 {dll.name} 得先就位（place_the_webview2_sdk）")
    # winexe：双击不闪控制台窗口。`Console.WriteLine("READY …")` 在调用方重定向了
    # stdout 时照样送得到 —— 装完自检就是这么读的。
    run([CSC, *CSC_FLAGS, "/target:winexe", f"/out:{binary}",
         f"/win32icon:{draw_the_icon()}",
         "/r:System.Web.Extensions.dll",
         "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
         f"/r:{core}", f"/r:{winforms}",
         str(SHELL_DIR / "Shell.cs")])
    if not binary.is_file():
        raise SystemExit("壳没编出来")


#: 默认时间戳服务。签名要盖时间戳，否则证书一到期，**已经发出去的每一份**的签名
#: 同时失效 —— 盖了戳的签名证明「签的时候证书是有效的」，之后证书过期也不影响。
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"

#: 用 in-box PowerShell 签，不用 signtool。
#:
#: signtool 属于 Windows SDK —— 装它要管理员，而这条线从第一天起的约束就是
#: **构建机与目标机都没有管理员**（RFC #848 §11）。`Set-AuthenticodeSignature` 是
#: PowerShell 自带的，零安装，产出的是同一个 Authenticode 结构。
#:
#: 证书和密码走**环境变量**，不进命令行：这台机器上任何进程都读得到别人的命令行
#: （`Get-CimInstance Win32_Process` 就能看见），密码不该出现在那儿。
_SIGN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
if ($env:AFS_SIGN_THUMBPRINT) {
    $cert = Get-Item ('Cert:\CurrentUser\My\' + $env:AFS_SIGN_THUMBPRINT)
} else {
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
        $env:AFS_SIGN_PFX, $env:AFS_SIGN_PASSWORD)
}
$null = Set-AuthenticodeSignature -FilePath $env:AFS_SIGN_TARGET -Certificate $cert `
    -HashAlgorithm SHA256 -TimestampServer $env:AFS_SIGN_TIMESTAMP
$check = Get-AuthenticodeSignature -FilePath $env:AFS_SIGN_TARGET
$stamped = if ($check.TimeStamperCertificate) { 'stamped' } else { 'unstamped' }
Write-Output ($check.Status.ToString() + "`t" + $check.SignerCertificate.Thumbprint + "`t" +
              $stamped + "`t" + $check.StatusMessage)
"""


class SigningCertificate:
    """签名要用的那张证书 —— 以及它从哪来。"""

    def __init__(self, *, pfx=None, thumbprint: str = "",
                 password: str = "", timestamp: str = DEFAULT_TIMESTAMP_URL):
        self.pfx = pfx
        self.thumbprint = thumbprint
        self.password = password
        self.timestamp = timestamp

    def describe(self) -> str:
        return f"CurrentUser\\My 里的 {self.thumbprint}" if self.thumbprint else f"{self.pfx}"

    def env(self, target: Path) -> dict:
        return {**os.environ,
                "AFS_SIGN_TARGET": str(target),
                "AFS_SIGN_PFX": str(self.pfx or ""),
                "AFS_SIGN_PASSWORD": self.password,
                "AFS_SIGN_THUMBPRINT": self.thumbprint,
                "AFS_SIGN_TIMESTAMP": self.timestamp}


def the_signing_certificate(pfx: str, thumbprint: str) -> "SigningCertificate | None":
    """这次构建用哪张证书签；一张都没有就 None（**不签**，而且会说出来）。

    命令行压过环境变量：CI 把证书放环境变量，手里拿着一张 pfx 时命令行说了算。
    """
    pfx = (pfx or os.environ.get("WINDOWS_SIGNING_PFX") or "").strip()
    thumbprint = (thumbprint or os.environ.get("WINDOWS_SIGNING_THUMBPRINT") or "").strip()
    stamp = os.environ.get("WINDOWS_SIGNING_TIMESTAMP") or DEFAULT_TIMESTAMP_URL
    if thumbprint:
        return SigningCertificate(thumbprint=thumbprint, timestamp=stamp)
    if not pfx:
        return None
    path = Path(pfx).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"❌ 签名证书不在：{path}")
    return SigningCertificate(pfx=path, password=os.environ.get("WINDOWS_SIGNING_PASSWORD", ""),
                              timestamp=stamp)


def sign_the_binary(target: Path, certificate: SigningCertificate) -> None:
    r"""给一个 exe 盖上 Authenticode 签名，并**当场核**它真盖上了。

    ## 判据为什么是指纹，不是 `Status -eq 'Valid'`

    `Status` 回答的是「这台机器信不信这张证书」，而这里要问的是「签上了没有」。
    两者在一种正当情况下会分开：证书的根不在信任库里（自签名证书、企业内部 CA），
    那时签名结构完全正确、`SignerCertificate.Thumbprint` 就是我们那张，而 `Status`
    是 `UnknownError`。拿 Status 当判据，会把一次成功的签名报成失败。

    反过来，Status 不是 Valid 也不能一声不吭 —— 那意味着**用户机器上仍会被拦**。
    所以：指纹对不上＝失败；指纹对上但根不受信＝成功，并把这句话说出来。

    ## 时间戳不给降级

    没盖时间戳的签名会在证书到期那天，**连同已经发出去的每一份**一起失效。网络不通
    就当场失败 —— 别静默签一个几个月后会自己变坏的包发出去。
    """
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SIGN_SCRIPT],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=certificate.env(target), timeout=300)
    if proc.returncode != 0:
        raise SystemExit(f"❌ 签 {target.name} 失败：{(proc.stderr or proc.stdout or '').strip()[:800]}")
    fields = (proc.stdout or "").strip().split("\t")
    if len(fields) < 3:
        raise SystemExit(f"❌ 签 {target.name} 之后读不懂签名状态：{proc.stdout!r}")
    status, thumbprint, stamped = fields[0], fields[1].strip(), fields[2]
    message = fields[3] if len(fields) > 3 else ""
    if not thumbprint:
        raise SystemExit(f"❌ {target.name} 签完之后没有签名者证书（status={status}）")
    if certificate.thumbprint and thumbprint.upper() != certificate.thumbprint.upper():
        raise SystemExit(f"❌ {target.name} 签上的是 {thumbprint}，不是要用的 {certificate.thumbprint}")
    if stamped != "stamped":
        raise SystemExit(
            f"❌ {target.name} 的签名没有时间戳（{certificate.timestamp} 没盖上）。\n"
            "   没时间戳的签名会在证书到期那天，连同已经发出去的每一份一起失效 —— 不发这样的包。")
    print(f"  {target.name} 已签名（{thumbprint}，已盖时间戳）")
    if status != "Valid":
        print(f"  ⚠️ 这张证书在这台机器上是 {status}：{message.strip()}")
        print("     签名结构是好的，但根不受信任（自签名 / 内部 CA）—— 用户机器上 SmartScreen 仍会拦。")


def sign_what_the_user_double_clicks(app: Path, certificate: "SigningCertificate | None") -> None:
    r"""签壳和卸载器 —— 在它们被打进 payload **之前**。

    ## 顺序是这件事的全部

    `Setup.exe = stub ‖ payload.zip ‖ 对齐 ‖ footer`，而 Authenticode 是把证书表
    **追加在文件末尾**的。所以只有一个顺序成立：

      ① 签 `ScienceMate.exe` / `Uninstall.exe`  →  ② 打 payload.zip  →
      ③ 拼 Setup.exe  →  ④ 签 Setup.exe

    反过来（先拼后签壳）就得拆包重打；而 ④ 之后再动 Setup.exe 一个字节，签名就废了。

    装在用户机器上的那两个 exe 也要签：SmartScreen 拦的是「用户双击的东西」，
    而用户双击的不只是安装器 —— 桌面图标点的是 `ScienceMate.exe`，「应用和功能」
    里点卸载启动的是 `Uninstall.exe`。只签安装器，等于只把第一道门修好。
    """
    if certificate is None:
        return
    step("签壳与卸载器")
    for name in ("ScienceMate.exe", "Uninstall.exe"):
        binary = app / name
        if not binary.is_file():
            raise SystemExit(f"❌ 要签的 {binary} 不在 —— 顺序错了（壳和卸载器得先编出来）")
        sign_the_binary(binary, certificate)


_READ_SIGNATURE_SCRIPT = r"""
$sig = Get-AuthenticodeSignature -FilePath $env:AFS_SIGN_TARGET
$thumb = if ($sig.SignerCertificate) { $sig.SignerCertificate.Thumbprint } else { '' }
$stamped = if ($sig.TimeStamperCertificate) { 'stamped' } else { 'unstamped' }
Write-Output ($sig.Status.ToString() + "`t" + $thumb + "`t" + $stamped)
"""


def read_the_signature(target: Path) -> tuple[str, str, str]:
    """(status, 指纹, stamped/unstamped)；没签过的返回 ("NotSigned", "", "unstamped")。"""
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _READ_SIGNATURE_SCRIPT],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "AFS_SIGN_TARGET": str(target)}, timeout=120)
    if proc.returncode != 0:
        raise SystemExit(f"❌ 读不出 {target} 的签名：{(proc.stderr or '').strip()[:400]}")
    fields = (proc.stdout or "").strip().split("\t")
    while len(fields) < 3:
        fields.append("")
    return fields[0], fields[1].strip(), fields[2]


def prove_the_signature_survived_the_payload(setup: Path, certificate: "SigningCertificate | None") -> None:
    r"""签过名的安装器**仍然装得上**，而且它装出来的两个 exe 也是签过的。

    ## 为什么这条闸非有不可

    `Setup.exe` 是 `stub ‖ payload.zip ‖ 对齐 ‖ footer`，而 Authenticode 把证书表
    **追加在文件末尾** —— 签完之后 EOF 就不再是 footer 的位置。桩为此专门去读 PE
    的证书表目录（`InstallerStub.cs` 的 `PayloadEnd`），拿证书表起点当「载荷末尾」。

    那段代码从写下来那天起就**没有一个签过名的文件跑过它** —— 从来没有证书，也就
    从来没有证书表。它是一段纯推演，而推演和事实之间隔着一次真跑。这条闸就是那一次：
    先签，再让 `prove_the_installer_works` 在**签过的**那一份上把安装从头走一遍。

    ## 还验「装出来的东西也签了」

    SmartScreen 拦的是「用户双击的东西」，而用户双击的不只是安装器：桌面图标点的是
    `ScienceMate.exe`，「应用和功能」里点卸载启动的是 `Uninstall.exe`。只签安装器
    等于只修好第一道门，所以这里把包解出来，逐个核。
    """
    if certificate is None:
        return
    step("签名自检")
    status, thumbprint, stamped = read_the_signature(setup)
    if not thumbprint:
        raise SystemExit(f"❌ {setup.name} 上没有签名了 —— 装完自检动过这个文件？（status={status}）")
    if stamped != "stamped":
        raise SystemExit(f"❌ {setup.name} 的签名没有时间戳")
    print(f"  ✅ {setup.name} 在跑完整条装完自检之后，签名依然在（{status}，已盖时间戳）")

    # 解一份出来核里面那两个。不起壳、不建快捷方式 —— 这一条只问签名。
    check_root = Path(tempfile.mkdtemp(prefix="sciencemate-signature-check-",
                                       dir=os.environ.get("LOCALAPPDATA") or None))
    try:
        install_dir = check_root / "ScienceMate"
        proc = subprocess.run([str(setup), "--no-launch", "--install-dir", str(install_dir)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if proc.returncode != 0:
            raise SystemExit(f"❌ 签过名的 Setup.exe 装不上了（rc={proc.returncode}）：{proc.stderr.strip()[:600]}\n"
                             "   多半是证书表把 footer 挤到了桩找不到的地方 —— 看 InstallerStub.cs `PayloadEnd`。")
        for name in ("ScienceMate.exe", "Uninstall.exe"):
            inner_status, inner_thumb, inner_stamp = read_the_signature(install_dir / name)
            if not inner_thumb:
                raise SystemExit(f"❌ 装出来的 {name} 没有签名 —— 用户双击它一样会被 SmartScreen 拦")
            if certificate.thumbprint and inner_thumb.upper() != certificate.thumbprint.upper():
                raise SystemExit(f"❌ 装出来的 {name} 签的是 {inner_thumb}，不是这次用的证书")
            if inner_stamp != "stamped":
                raise SystemExit(f"❌ 装出来的 {name} 的签名没有时间戳")
            print(f"  ✅ 装出来的 {name} 带签名（{inner_status}，已盖时间戳）")
    finally:
        shutil.rmtree(io_path(check_root), ignore_errors=True)


def say_it_is_unsigned(setup: Path) -> None:
    """没签名这件事必须**说在看得见的地方**。"""
    print(f"\n⚠️ {setup.name} 没有签名。")
    print("   用户下载后 Windows 会先拦一道「Windows 已保护你的电脑 · 未知发布者」，")
    print("   得点「更多信息 → 仍要运行」才装得上。")
    print("   要签：--sign-with <证书.pfx>（密码放环境变量 WINDOWS_SIGNING_PASSWORD），")
    print("   或 --sign-with-thumbprint <指纹>（证书已在 CurrentUser\\My）。")
    print("   买哪种证书、多少钱、多久生效：docs/WINDOWS_CODE_SIGNING.md")


def build_the_uninstaller(app: Path) -> None:
    r"""编 `Uninstall.exe` 放进便携目录 —— 装到哪儿它就跟到哪儿。

    ## 为什么卸载器在包里、不在安装器里

    卸载要在**安装之后很久**才发生：那时 Setup.exe 多半已经被用户从下载目录删掉了。
    所以能卸载的那个程序必须和应用一起躺在安装目录里，开始菜单的「卸载 ScienceMate」
    和「应用和功能」里那一条都指向它（见 `InstallFootprint.cs`）。

    它和安装器共用 `InstallFootprint.cs`：一个建快捷方式、一个删快捷方式，两边对
    「快捷方式在哪、注册表那条长什么样」只有一个答案，不会分叉。
    """
    step("编卸载器（csc）")
    binary = app / "Uninstall.exe"
    run([CSC, *CSC_FLAGS, "/target:winexe", f"/out:{binary}",
         # 卸载器也得有图标：它在开始菜单里和应用摆在一起，顶着 .NET 默认图标
         # 就像混进来一个不相干的东西。
         f"/win32icon:{ICON if ICON.is_file() else draw_the_icon()}",
         "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
         "/r:System.Web.Extensions.dll",
         str(SHELL_DIR / "Uninstaller.cs"), str(SHELL_DIR / "InstallFootprint.cs")])
    if not binary.is_file():
        raise SystemExit("卸载器没编出来")
    print(f"  {binary}（{binary.stat().st_size} 字节）")


def write_the_backend_json(app: Path, python: Path) -> None:
    """壳按它起后端。数据放哪由 app/config.py 定，这里不碰；tectonic/git 由布局
    自动被 finder 找到，env 不用塞。"""
    step("写 backend.json")
    # PYTHONUTF8=1 是**关键**、不是可选：Windows 上 launcher 的 `ensure_utf8_mode()`
    # 没在 UTF-8 模式就 `os.execv` re-exec 一次换新 pid（#862），而 os.execv 在 Windows
    # 是 spawn+退出、原进程走人。壳直接起 `python.exe`（不像开发时经 uv 中转）时，
    # 壳追踪的正是那个退出的原 pid → 壳会误判后端死了、连锁收摊。启动就给足 UTF-8、
    # 让它别 re-exec，壳追踪的就是真 server。这是 RFC/board 早记下的「壳/启动方设
    # PYTHONUTF8=1」那条。
    # **相对路径**，不是绝对：装包器在构建机上打，用户装到 `%LOCALAPPDATA%\Programs\…`
    # 或把整包拷到别处，绝对路径就指回构建机、后端起不来（装完自检的 --no-launch 只查
    # 文件在、没起后端，一度没抓到这个）。壳按「相对自己所在目录」解析（见 Shell.cs
    # ResolveAgainstShell）：executable 相对 app 根＝`Resources\python\python.exe`，
    # workingDirectory `.` ＝壳目录（app 根）。
    config = {
        "executable": str(python.relative_to(app)),
        # -B / PYTHONDONTWRITEBYTECODE：装好的目录是只读产物，后端及其子进程都不许往
        # 里写 .pyc（安装位置一旦被写就不再等于安装包，重装/校验都会撞上）。
        # 不加 -I：isolated 模式会把壳设的 PYTHON* 环境（UTF8/UNBUFFERED）一并关掉。
        "arguments": ["-B", "-X", "utf8", "-m", "app.launcher", "start"],
        # 壳自替换（#953 ④）要知道「我随哪一版装进来的」：载荷版本比它新才换，比它旧不换
        # （重装了新包、数据根里还留着旧载荷时，别把新壳换成旧的 —— 和 #958 同一条规矩）。
        "version": payload_release.the_version(),
        "workingDirectory": ".",
        "environment": {"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    }
    (app / "backend.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  {app / 'backend.json'}")


def _a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def prove_the_package_works(app: Path, python: Path, with_ui: bool = False, edition: str = "personal") -> None:
    """装完自检：①独立 python 跑 `doctor`，harness/pdf engine/sandbox 都在；
    ②起后端，`/health/ready` 答 200。两条都在**产物**上跑，不碰源码 uv 环境。"""
    step("装完自检")
    home = Path(tempfile.mkdtemp(prefix="win-package-check-"))
    # PYTHONUTF8=1：同 backend.json，起 server 别 re-exec（否则 Popen 追踪的原 pid
    # 一 re-exec 就退出，误判「后端提前退出」）——也顺带让 doctor 输出干净 UTF-8。
    env = {**os.environ, "PLATFORM_DATA_ROOT": str(home),
           "HARNESS_FRAMEWORK_HOME": str(home),
           "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}

    doctor = subprocess.run([str(python), "-B", "-X", "utf8", "-m", "app.launcher", "doctor"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env)
    print(doctor.stdout.strip())
    facts: dict[str, str] = {}
    for _line in doctor.stdout.splitlines():
        if ":" in _line:
            _k, _v = _line.split(":", 1)
            facts[_k.strip()] = _v.strip()
    # 版本标记：doctor 早就把它印出来了，只是没人判 —— 而"没有标记"不是版本未知，
    # 是**任何更新都比它新**，界面会永远挂着「有新版本」，点了更新重启还是有。
    version = facts.get("version", "")
    if not version or version.startswith("("):
        raise SystemExit(
            f"❌ 装完自检：version = {version!r} —— 这个包不知道自己是哪一版，"
            f"装上之后会永远提示有新版本")

    for key in ("harness", "pdf engine", "sandbox"):
        value = facts.get(key, "")
        if not value or value.startswith(("(none", "(not", "none")):
            raise SystemExit(f"\u274c 装完自检：{key} = {value!r} —— 这个包缺了它")

    # git 单独判：它总能解析（系统 git 兜底），但便携包必须指向**包内**那份——
    # 否则干净机器（没装 Git for Windows）上「新建项目」当场没 git。
    git = facts.get("git", "")
    if str(app).lower() not in git.lower():
        raise SystemExit(
            f"❌ 装完自检：git = {git!r} 不在包里 —— 干净机器上会没 git。"
            f"应指向 {app}\\Resources\\git\\bin\\git.exe")
    # --with-ui 打的包，界面必须在（否则双击开只有 API、看不见东西）。
    if with_ui:
        ui = facts.get("static UI", "")
        if not ui or ui.startswith("(not"):
            raise SystemExit(
                f"❌ 装完自检：static UI = {ui!r} —— --with-ui 打的包却没界面")

    port = _a_free_port()
    # stdout 接**文件**而不是 PIPE：没人读的 PIPE 在后端日志写满缓冲区那一刻把后端
    # 卡死（N06），自检就变成「90s 内没答 /health/ready」—— 指向一个假原因。
    log_path = home.parent / f"{home.name}.log"
    log_handle = log_path.open("wb")
    succeeded = False
    server = subprocess.Popen(
        [str(python), "-B", "-X", "utf8", "-m", "app.launcher", "start", "--port", str(port), "--no-browser"],
        env=env, stdout=log_handle, stderr=subprocess.STDOUT)
    try:
        import urllib.error
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            if server.poll() is not None:
                raise SystemExit("\u274c 后端提前退出，自检没起来")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=3) as r:
                    if r.status == 200:
                        ready = True
                        break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)
        if not ready:
            raise SystemExit("\u274c 后端 90s 内没答 /health/ready 200")
        # 发行是读 Resources/edition.json 得来的：放错一层就静默变成个人版。问包自己。
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/capabilities", timeout=10) as r:
            reported = json.load(r).get("edition")
        if reported != edition:
            raise SystemExit(f"\u274c 包报的发行是 {reported!r}，要打的是 {edition!r} —— edition.json 没落在解释器旁边")
        print(f"  \u2705 包自报发行 = {reported}")
        payload_release.the_package_carries_no_pro_edition(app, edition)
        # --with-ui：`/` 必须真的把界面 serve 出来（HTML），不是只有 API。光靠
        # doctor 说 static UI 在还不够——那只证明文件在，证明不了后端真挂了静态中间件。
        if with_ui:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
                    body = r.read(4096).decode("utf-8", "replace").lower()
            except (urllib.error.URLError, OSError) as exc:
                raise SystemExit(f"\u274c 后端不 serve 界面：GET / 失败（{exc}）")
            if "<!doctype html" not in body and "<html" not in body:
                raise SystemExit(
                    "\u274c GET / 回的不是 HTML —— 界面没挂上（还是 API-only）")
            print(f"  \u2705 doctor 齐、/health/ready 200、GET / 是界面 HTML（端口 {port}）")
        else:
            print(f"  \u2705 doctor 齐、后端 /health/ready 200（端口 {port}）")
        succeeded = True
    finally:
        # terminate() 只杀 launcher 这一个进程；它起的 server/worker 子进程会活下来，
        # 占着端口和数据根。按 pid 杀整棵树。
        subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with contextlib.suppress(Exception):
            server.wait(timeout=20)
        log_handle.close()
        if succeeded:
            shutil.rmtree(io_path(home), ignore_errors=True)
            log_path.unlink(missing_ok=True)
        else:
            # 失败时把日志尾巴打出来、数据根留着：没有证据的失败只能重跑一次再猜。
            print(log_path.read_text("utf-8", errors="replace")[-16000:])
            print(f"自检失败，证据保留：{home} ; {log_path}")


def bytes_on_disk(root: Path) -> int:
    return sum(f.stat().st_size for f in io_path(root).rglob("*") if f.is_file())


def megabytes_on_disk(root: Path) -> int:
    return bytes_on_disk(root) // (1024 * 1024)


#: 桩里那两个常量的类名与字段名。构建生成它们、C# 引用它们 —— 一处改两处必须一起改，
#: 所以名字写在这里，测试按这一份核对。
INSTALLER_VERSION_CLASS = "InstallerVersion"
INSTALLER_VERSION_FIELD = "Value"
INSTALLER_SIZE_FIELD = "InstalledBytes"


def write_the_installer_version(version: str, installed_bytes: int = 0) -> Path:
    """把版本号与装完占多大写成一份 C# 源码，和桩一起编 —— 单文件安装器旁边没有别的文件可读。

    向导第一页要说两句话：「版本 x.y.z」和「需要约 N GB，这个盘可用 M GB」。两个数都
    只有**打包这一刻**知道，所以一起生成、一起烧进去；写死在 .cs 里的话，下次包变大了
    没人记得改它，界面就开始说谎。

    生成物落在 `dist/`（不进仓库），编完即删：源码树里不该躺着一份会过期的版本号。
    """
    if not version or version.startswith("("):
        raise SystemExit(f"❌ 打安装器：版本号读不出来（{version!r}），向导上就会是空的")
    source = DIST / f"{INSTALLER_VERSION_CLASS}.cs"
    source.write_text(
        "// 由 scripts/package/build_windows_app.py 生成，编完就删。别手改。\n"
        f"internal static class {INSTALLER_VERSION_CLASS}\n{{\n"
        f"    internal const string {INSTALLER_VERSION_FIELD} = \"{version}\";\n"
        f"    internal const long {INSTALLER_SIZE_FIELD} = {int(installed_bytes)}L;\n"
        "}\n",
        encoding="utf-8",
    )
    return source


def make_the_installer(app: Path, edition: str = "personal",
                       certificate: "SigningCertificate | None" = None) -> Path:
    """把便携目录拼成一个自解压 `ScienceMate-Setup.exe`（自带 C# 桩，无外部 SFX）。

    结构 = `InstallerStub.exe ‖ payload.zip ‖ 对齐填充 ‖ footer(offset:Int64, MAGIC:Int64)`。
    zip 装的是便携目录**内容**（ScienceMate.exe/Resources/backend.json 在 zip 根），
    这样桩解到 installDir 后直接就位。见 InstallerStub.cs。

    桩找 footer 不按文件末尾，按 PE 证书表：Authenticode 签名会把证书表**追加**在文件
    末尾，签完之后 EOF 就不再是 footer 的位置。证书表要求 8 字节对齐，所以 footer 之前
    先补齐到 8 的倍数 —— 签名前后 footer 的位置才是同一个。
    """
    step("拼单文件安装器（自解压 Setup.exe）")
    payload = DIST / "payload.zip"
    payload.unlink(missing_ok=True)
    # 走 `\\?\` 扩展路径遍历：超过 MAX_PATH 的成员普通 rglob 会静默跳过（N38）。
    archive_root = io_path(app)
    files = sorted(f for f in archive_root.rglob("*") if f.is_file())
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for f in files:
            zf.write(f, f.relative_to(archive_root).as_posix())
    print(f"  payload.zip（{payload.stat().st_size // (1024 * 1024)} MB，{len(files)} 文件）")

    stub = DIST / "InstallerStub.exe"
    # 安装向导上要写「这是哪一版」和「需要多大地方」。用户手里只有一个 .exe，旁边没有
    # 任何文件可读，所以这两个数在**编译那一刻**烧进桩里 —— 这份 .cs 由构建生成，不进仓库。
    version_source = write_the_installer_version(payload_release.the_version(), bytes_on_disk(app))
    # `/target:winexe`：双击安装包**不该先弹一个黑窗口**（2026-09-21 wangd）。
    # 它仍旧把每一步写 stdout/stderr —— 脚本重定向照样读得到（`--no-launch` 那条自检
    # 路径就是这么读的），只是没人再被那个控制台吓一跳。
    run([CSC, *CSC_FLAGS, "/target:winexe", f"/out:{stub}",
         f"/win32icon:{ICON if ICON.is_file() else draw_the_icon()}",
         "/r:System.IO.Compression.FileSystem.dll", "/r:System.IO.Compression.dll",
         "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
         "/r:System.Web.Extensions.dll",
         str(SHELL_DIR / "InstallerStub.cs"), str(SHELL_DIR / "InstallWizard.cs"),
         str(SHELL_DIR / "InstallFootprint.cs"), str(version_source)])
    version_source.unlink(missing_ok=True)
    if not stub.is_file():
        raise SystemExit("安装器桩没编出来")

    # 专业版的安装器名带 -Pro：两种发行内容几乎一样，名字是人分得清它们的唯一办法。
    setup = DIST / ("ScienceMate-Pro-Setup.exe" if edition == "pro" else "ScienceMate-Setup.exe")
    stub_bytes = stub.read_bytes()
    offset = len(stub_bytes)
    with open(setup, "wb") as out:
        out.write(stub_bytes)
        with open(payload, "rb") as p:
            shutil.copyfileobj(p, out)
        # 证书表 8 字节对齐：填充放在 footer **之前**，签名追加证书表后 footer 位置不变。
        out.write(b"\0" * ((-out.tell()) % 8))
        out.write(struct.pack("<qq", offset, INSTALLER_MAGIC))
    payload.unlink(missing_ok=True)
    stub.unlink(missing_ok=True)
    print(f"  {setup}（{setup.stat().st_size // (1024 * 1024)} MB）")
    # 签在**最后**：Authenticode 把证书表追加在文件末尾，签完再动一个字节签名就废了。
    # 桩找 footer 走 PE 证书表而不是 EOF，为的正是这一刻（见 InstallerStub.cs `PayloadEnd`）。
    if certificate is not None:
        step("签安装器")
        sign_the_binary(setup, certificate)
    return setup


#: 壳走自更新时要带上的文件：exe 本身 + 它旁边必须有的三个 WebView2 DLL。
#: 这一份和 `WEBVIEW2_MEMBERS` 的值域是同一个答案（DLL 放在壳旁边），改一处必改另一处。
SHELL_FILES = ("ScienceMate.exe", *sorted(WEBVIEW2_MEMBERS.values()))


def export_the_shell_for_the_payload(app: Path) -> Path:
    """把壳（exe + DLL）单独落一份到 `dist/shell-windows/`，给发布装配当 extras 用。

    ## 为什么要单独落一份

    自更新换不了壳（#953），要换就得把壳放进更新载荷。载荷是在 **Mac** 上装配发布
    目录时打的，而 Windows 的壳只能在 Windows 上编 —— 所以打包器在这里把壳那几个文件
    原样拷出来，和 Setup.exe 一起传到 Mac；Mac 侧 `assemble_the_release_dir
    --windows-shell-dir` 把它打进 `extras-<ver>.tar.gz`。

    只拷「壳本身」：exe 和它旁边必须有的 DLL。不拷 backend.json（它是安装位置相关的，
    由安装器写）、不拷任何 Resources（那是运行时，不走自更新）。

    附带一份 `SHA256SUMS`：Mac 侧装配前先核，传坏了当场拒绝 —— 不让一个半截的壳
    进到签过名的载荷里去。
    """
    out = DIST / "shell-windows"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    lines = []
    for name in SHELL_FILES:
        source = app / name
        if not source.is_file():
            raise SystemExit(f"壳文件缺失：{source} —— 没法把壳放进更新载荷")
        shutil.copy2(source, out / name)
        lines.append(f"{hashlib.sha256(source.read_bytes()).hexdigest()}  {name}")
    # newline="\n"：这份核对单是给 Mac 上的 `shasum -c` 和装配脚本读的。Windows 上
    # `write_text` 默认把 \n 换成 \r\n，文件名尾巴就挂着一个 \r —— 2026-09-12 真机第一次
    # 传到 Mac 就撞上：`shasum: ScienceMate.exe: No such file or directory`，四个全找不到。
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"  壳导出到 {out}（{len(SHELL_FILES)} 个文件，供发布装配打进 extras）")
    return out


def explain_the_shell_exit(returncode: int, words: str) -> str:
    """装到的壳没等到 READY 就退了 —— 说清是哪一种退。

    两种退看起来一样（都是 exit 0），原因和该做的事完全不同：
    - 双击即退（#948 那种）：产品坏了，看壳的话。
    - 壳把自己换成了数据根里更新的载荷壳（#953 ④）：产品没坏 —— 这台机器的数据根里
      留着比这个包新的载荷（上一次自更新 / 真机测试留下的）。刚起来的那个壳换完就退，
      接着跑的是载荷里的壳，不是这个包里的。自检要看的是这个包，所以仍然红，但要
      指着载荷目录说话，别让人去查一个不存在的双击退出。
    """
    said = _indent(words)
    if "SHELL-SWAP done" in words:
        return (f"❌ 装到的壳换成了数据根里更新的载荷壳后退出（exit={returncode}）—— 这不是双击即退。\n"
                f"   这台机器的 %LOCALAPPDATA%\\afs\\payload 里有比这个包新的载荷；接着跑的是载荷里的壳，\n"
                f"   不是这个包里的，自检看不到这个包。清掉那个目录再打包。\n"
                f"   壳这次说的话：\n{said}")
    return (f"❌ 壳自己退了（exit={returncode}）—— 这就是同事双击后看到的。\n"
            f"   壳这次说的话：\n{said}")


def shortcut_target(lnk: Path, uninstaller: Path) -> str:
    r"""一个 .lnk 指向谁 —— **用产品自己的读法**。

    读 .lnk 只有 COM 这一条路（格式是二进制的，stdlib 没有解析器）。第一版这里借
    PowerShell 的 `WScript.Shell` 读，当场就撞上它为什么被产品否掉：WSH 走 ANSI，
    「卸载 ScienceMate.lnk」这种名字它读出来是空的 —— 自检会把一个好好的快捷方式
    报成坏的。

    所以不另写一份读法，直接把卸载器当程序集加载进 PowerShell，反射调它的
    `Footprint.ShortcutTarget`（`IShellLinkW`，和安装器写它用的是同一段代码）。
    判据问的于是正是产品自己的答案；产品改了读法，判据跟着改，不会分叉。
    """
    script = (
        "[Reflection.Assembly]::LoadFrom('%s').GetType('Footprint')"
        # Public,NonPublic 两个都要：方法是 public，而**类**是 internal —— 只写
        # NonPublic 的话 GetMethod 返回 null，报出来是一句 "You cannot call a method
        # on a null-valued expression"，看上去像类型没找到。
        ".GetMethod('ShortcutTarget', [Reflection.BindingFlags]'Public,NonPublic,Static')"
        ".Invoke($null, @('%s'))" % (uninstaller, lnk))
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if done.returncode != 0:
        raise SystemExit(f"❌ 读不出 {lnk} 指向哪：{(done.stderr or done.stdout or '').strip()[:400]}")
    return (done.stdout or "").strip()


def prove_the_footprint_is_there(install_dir: Path, desktop_dir: Path, start_menu_root: Path) -> list[Path]:
    r"""装完之后，桌面和开始菜单里真有指向这份安装的快捷方式，「应用和功能」里真有那一条。

    ## 为什么判据是「指向哪里」而不是「文件在不在」

    `.lnk` 文件存在只说明写了个文件。装两份、或者上一次自检留下的残骸，都能让「文件在」
    这条判据绿着通过，而用户点下去打开的是别处那一份。所以判据落在 `TargetPath` 上 ——
    那正是用户点它时会发生的事。

    卸载器删快捷方式之前问的也是同一句话（`Footprint.ShortcutTarget`），两边同一个判据。
    """
    import winreg

    exe = install_dir / "ScienceMate.exe"
    uninstaller = install_dir / "Uninstall.exe"
    expected = {
        desktop_dir / "ScienceMate.lnk": exe,
        start_menu_root / "ScienceMate" / "ScienceMate.lnk": exe,
        start_menu_root / "ScienceMate" / "卸载 ScienceMate.lnk": uninstaller,
    }
    for lnk, target in expected.items():
        if not lnk.is_file():
            raise SystemExit(f"❌ 快捷方式没建出来：{lnk}\n   （装完自检显式传了 --desktop-shortcut / --start-menu）")
        actual = shortcut_target(lnk, uninstaller)
        if actual.casefold() != str(target).casefold():
            raise SystemExit(f"❌ {lnk} 指向 {actual!r}，该指向 {str(target)!r}")
    print(f"  ✅ 桌面 + 开始菜单共 {len(expected)} 个快捷方式，都指向这份安装")

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SELFCHECK_UNINSTALL_KEY) as key:
        location = winreg.QueryValueEx(key, "InstallLocation")[0]
        uninstall_string = winreg.QueryValueEx(key, "UninstallString")[0]
    if Path(location) != install_dir:
        raise SystemExit(f"❌ 「应用和功能」那一条的 InstallLocation={location!r}，该是 {install_dir}")
    if str(uninstaller) not in uninstall_string:
        raise SystemExit(f"❌ 「应用和功能」那一条的 UninstallString={uninstall_string!r} 不指向 {uninstaller}")
    print("  ✅ 「应用和功能」里登记了一条，卸载命令指向安装目录里的 Uninstall.exe")

    record = install_dir / "install-record.json"
    if not record.is_file():
        raise SystemExit(f"❌ 没写安装清单 {record} —— 卸载器就只能按默认位置猜了")
    written = json.loads(record.read_text(encoding="utf-8"))
    if sorted(Path(x) for x in written["shortcuts"]) != sorted(expected):
        raise SystemExit(f"❌ 安装清单记的快捷方式和真建出来的对不上：{written['shortcuts']}")
    print(f"  ✅ 安装清单 {record.name} 记下了这 {len(expected)} 个落点")
    return list(expected)


def prove_the_uninstaller_works(install_dir: Path, shortcuts: list[Path], data_root: Path) -> None:
    r"""`Uninstall.exe /S`：安装目录、快捷方式、「应用和功能」那一条都没了，**数据还在**。

    ## 数据还在，是这条闸最要紧的一句

    卸载一个程序和扔掉自己的研究结果是两件事。卸载器默认不碰数据根（要删得用户在
    对话框里明确勾），而「默认」这种事只有真跑一次才知道它是不是真的 —— 所以这里
    先在数据根里放一个文件，卸载完回来看它还在不在。

    第二阶段是**另一个进程**（exe 删不掉自己所在的目录，见 Uninstaller.cs），所以
    `Uninstall.exe` 会立刻返回、目录稍后才消失 —— 判据必须是「等它消失」，不是
    「返回那一刻它已经没了」。
    """
    import winreg

    step("卸载器自检")
    canary = data_root / "keep-me.txt"
    if not canary.is_file():
        raise SystemExit(f"❌ 这条闸的前提不成立：{canary} 本来就不在")
    proc = subprocess.run([str(install_dir / "Uninstall.exe"), "/S"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)
    if proc.returncode != 0:
        raise SystemExit(f"❌ 卸载失败（rc={proc.returncode}）：{(proc.stdout or '') + (proc.stderr or '')}")

    deadline = time.time() + 120
    while install_dir.exists() and time.time() < deadline:
        time.sleep(1)
    if install_dir.exists():
        left = [str(f.relative_to(install_dir)) for f in install_dir.rglob("*")][:10]
        raise SystemExit(f"❌ 卸载后安装目录还在：{install_dir}（残留 {left}）")
    print(f"  ✅ 安装目录已删除：{install_dir}")

    still = [str(s) for s in shortcuts if s.exists()]
    if still:
        raise SystemExit(f"❌ 卸载后快捷方式还在：{still}")
    print(f"  ✅ {len(shortcuts)} 个快捷方式都删掉了")

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SELFCHECK_UNINSTALL_KEY):
            raise SystemExit(f"❌ 卸载后 HKCU\\{SELFCHECK_UNINSTALL_KEY} 还在")
    except FileNotFoundError:
        print("  ✅ 「应用和功能」里那一条也没了")

    if not canary.is_file():
        raise SystemExit(f"❌ 卸载把研究数据一起删了 —— {canary} 没了。默认卸载绝不该碰数据根。")
    print(f"  ✅ 数据根 {data_root} 原封不动（默认卸载不删数据）")


#: 自检往哪个注册表子键写「应用和功能」那一条。**不是**产品真用的
#: `Footprint.DefaultUninstallKey` —— 打包机上装着真的 ScienceMate，自检要是写到
#: 产品那个键上，卸载自检跟着就会把真安装的卸载入口删掉。
SELFCHECK_UNINSTALL_KEY = r"Software\ScienceMate-SelfCheck\Uninstall\ScienceMate"


def prove_the_installer_works(setup: Path) -> None:
    r"""装完自检：`Setup.exe --no-launch` 解到 installDir，再**从那个新位置真起一次壳**，
    证明整包搬走后还能跑（这才抓得住 backend.json 绝对路径不可搬走那类 bug——只查文件
    在、不真起壳，一度漏掉了）。

    快捷方式、「应用和功能」那一条、卸载器也在这里跑真的 —— 但落点全部改到隔离目录
    与隔离注册表键（`--desktop-dir` / `--start-menu-dir` / `--uninstall-key`）。
    不这么做只有两个选择：要么不跑这条路（那它第一次被跑就是在用户机器上），要么让
    自检往打包机真实的桌面和「应用和功能」里写东西 —— 两个都不行。
    """
    step("安装器自检")
    # 装到一个**隔离的临时目录**，不碰这台机器上真装着的 %LOCALAPPDATA%\Programs\ScienceMate
    # —— 打包机自己也在用这个应用；自检不该把它换掉。
    # 装到 `%LOCALAPPDATA%` 下，**不是** `%TEMP%`：2026-09-21 真机上，解压完紧接着
    # `Directory.Move` 会撞上杀毒软件还攥着那两万多个刚落盘的文件。这条一直没被自检
    # 抓住，正因为自检装在 `%TEMP%` —— 那个目录的实时扫描策略不一样，于是判据一路绿着，
    # 而它绿的是另一条路。装到用户真装的那一层下面去。
    check_root = Path(tempfile.mkdtemp(prefix="sciencemate-install-check-",
                                       dir=os.environ.get("LOCALAPPDATA") or None))
    install_dir = check_root / "Programs" / "ScienceMate"
    desktop_dir = check_root / "Desktop"
    start_menu_root = check_root / "StartMenu"
    desktop_dir.mkdir()
    start_menu_root.mkdir()
    # 安装器把「数据放哪」记进 install-record.json，卸载时按它问「要不要连数据一起删」。
    # 给它一个隔离的数据根，记录里写的就是这个 —— 卸载自检验「数据默认留着」时，验的
    # 才是一个自检自己造出来的目录，而不是打包机上真在用的 %LOCALAPPDATA%\afs。
    data_root = check_root / "data"
    data_root.mkdir()
    (data_root / "keep-me.txt").write_text("卸载不该删数据", encoding="utf-8")
    install_env = {**os.environ, "HARNESS_FRAMEWORK_HOME": str(data_root)}
    proc = subprocess.run([str(setup), "--no-launch", "--install-dir", str(install_dir),
                           "--desktop-shortcut", "--start-menu", "--register-uninstall",
                           "--desktop-dir", str(desktop_dir),
                           "--start-menu-dir", str(start_menu_root),
                           "--uninstall-key", SELFCHECK_UNINSTALL_KEY],
                          capture_output=True, env=install_env,
                          text=True, encoding="utf-8", errors="replace", timeout=600)
    print((proc.stdout or "").strip())
    exe = install_dir / "ScienceMate.exe"
    # 缺件按 INSTALL_INVENTORY 一处回答：这里曾另抄一份（exe/python/git/tectonic），
    # 清单加了 biber 这里就会悄悄漏掉。
    missing = [name for name in INSTALL_INVENTORY if not (install_dir / name).is_file()]
    if proc.returncode != 0 or missing:
        raise SystemExit(
            f"❌ 安装器自检失败（rc={proc.returncode}）；缺件：{missing or '(无)'}\n{proc.stderr}")
    print(f"  Setup.exe 解到 {install_dir}，清单里的 {len(INSTALL_INVENTORY)} 件都在")
    shortcuts = prove_the_footprint_is_there(install_dir, desktop_dir, start_menu_root)

    # 关键：从**装到的新位置**起壳（它读自己旁边的 backend.json、按相对壳目录解析路径），
    # 等 READY + /health/ready 200，证明搬走后后端真起得来。
    import urllib.error
    home = Path(tempfile.mkdtemp(prefix="win-installed-check-"))
    env = {**os.environ, "PLATFORM_DATA_ROOT": str(home),
           "HARNESS_FRAMEWORK_HOME": str(home), "PYTHONUNBUFFERED": "1"}
    # 壳的数据根跟 HARNESS_FRAMEWORK_HOME 走（Shell.cs `DataRoot()`），shell.log 也在那下面。
    log = home / "logs" / "shell.log"
    already = log.stat().st_size if log.is_file() else 0
    shell = start_the_shell_the_way_a_user_does(exe, home, env)
    url = None
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            for line in read_the_shell_log_since(log, already).splitlines():
                m = re.match(r"READY (http://127\.0\.0\.1:\d+/)", line.strip())
                if m:
                    url = m.group(1)
                    break
            if url:
                break
            if shell.poll() is not None:
                raise SystemExit(explain_the_shell_exit(shell.returncode, read_the_shell_log_since(log, already)))
            time.sleep(0.5)
        if not url:
            raise SystemExit(
                "❌ 装到的壳没说 READY —— 搬走后起不来（backend.json 路径不可搬？）\n"
                f"{_indent(read_the_shell_log_since(log, already))}")
        print(f"   shell> READY {url}")
        with urllib.request.urlopen(url + "health/ready", timeout=5) as r:
            if r.status != 200:
                raise SystemExit(f"❌ 装到的后端 /health/ready = {r.status}")
        print(f"  ✅ 从 {install_dir} 起壳→后端 /health/ready 200（整包可搬走）")
        prove_the_window_appears(shell.pid)
        prove_the_shell_outlives_its_launcher(shell)
        prove_a_model_command_runs_from_the_installed_app(install_dir)
        prove_the_installed_app_makes_the_platforms_pdf(install_dir)
        # 这一条必须在壳**还活着**的时候跑 —— 它验的就是「开着的时候重装」。
        prove_a_reinstall_never_breaks_a_working_install(setup, install_dir)
        # 卸载要在壳**关掉之后**跑（开着的时候卸载器会拒绝，那是另一条闸）。这里显式
        # 关一次，不等 finally —— 卸载自检得站在「应用已经关了」这个前提上。
        stop_the_shell(shell)
        prove_the_uninstaller_works(install_dir, shortcuts, data_root)
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"❌ 装到的后端 health 连不上：{exc}")
    finally:
        stop_the_shell(shell)
        shutil.rmtree(io_path(home), ignore_errors=True)
        shutil.rmtree(io_path(check_root), ignore_errors=True)


def stop_the_shell(shell: subprocess.Popen) -> None:
    """关掉自检起的那个壳，等 Job 把后端连锁收摊。关两次也没关系。"""
    with contextlib.suppress(Exception):
        shell.terminate()
        shell.wait(timeout=20)
    time.sleep(2)


#: 载荷跑通的暗号。装完自检要在**安装位置**真看到它从沙箱里出来。
PAYLOAD_MARKER = "SANDBOX-PAYLOAD-OK"

#: 探针源码：用**生产那条路**（`select_backend().prepare()`）起载荷，别手搓
#: 启动器命令行 —— 手搓等于把产品知识抄一份到自检里，抄件会分叉。两条载荷：
#: 解释器直接起（沙箱通不通），以及模型真会敲的 —— 在它的 shell 里敲 ``python3``。
_PAYLOAD_PROBE = """
import json, os, subprocess, sys
from pathlib import Path

harness, work = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(harness))
work.mkdir(parents=True, exist_ok=True)

from core import isolation
from core.isolation import CommandSpec
from shared.lib.shell import bash_shell

backend = isolation.select_backend()


def run(argv):
    spec = CommandSpec(argv=argv, cwd=str(work), writable_roots=(work,))
    launch = backend.prepare(spec, state=None)
    proc = subprocess.run(launch.argv, cwd=(launch.cwd or str(work)),
                          env={**os.environ, **(launch.env or {})},
                          capture_output=True, text=True, timeout=180)
    return {"rc": proc.returncode, "out": (proc.stdout or "").strip(),
            "err": (proc.stderr or "").strip()[:500]}


report = run((sys.executable, "-c", "print(%r)"))
report["python3"] = run((bash_shell(), "-c", 'python3 -c "import sys; print(sys.prefix)"'))
print(json.dumps({"backend": getattr(backend, "name", "?"),
                  "caps": sorted(str(c) for c in backend.capabilities()), **report}))
""" % PAYLOAD_MARKER


#: 壳窗口的标题。装完自检按它认窗口 —— 壳里改了标题这边不改，自检就会说"没窗口"。
SHELL_WINDOW_TITLE = "ScienceMate"


def shell_log_path() -> Path:
    """壳把自己说的话写在哪。和壳里 `AlsoLogToAFile()` 是同一个答案的两处读法。"""
    return Path(os.environ["LOCALAPPDATA"]) / "afs" / "logs" / "shell.log"


def read_the_shell_log_since(log: Path, offset: int) -> str:
    """只读 offset 之后新写的那截。

    日志是**追加**的，历次启动都在同一个文件里。不带起点去读，就会读到上一次的
    `READY http://127.0.0.1:xxxxx/`，然后拿一个早就关了的端口去请求 —— 判据会
    悄悄回答另一个问题。
    """
    if not log.is_file():
        return ""
    with log.open("rb") as fh:
        fh.seek(offset)
        return fh.read().decode("utf-8", "replace")


def _indent(text: str) -> str:
    return "\n".join("     " + line for line in text.strip().splitlines()) or "     （一个字都没有）"


def start_the_shell_the_way_a_user_does(exe: Path, cwd: Path, env: dict) -> subprocess.Popen:
    """按**双击**那条路起壳：没有控制台、三个标准句柄一个都不给。

    ## 为什么自检必须这么起

    2026-09-10：0.4.4 的包过了全部自检，装好双击**2 秒就自己退**（exit 0）。自检
    没抓到，因为自检是用 `stdin=PIPE, stdout=PIPE` 起壳的 —— 父进程一直攥着管道
    写端，壳的 stdin 永远不 EOF。**同事没有那个父进程。**

    夹具比产品多给了一样东西（一个攥着管道的父进程），于是它测的是另一个场景。
    这一条把差别抹掉：自检看壳的方式，和用户看它的方式**完全一样** —— 只剩日志
    文件、HTTP 端口、窗口这三个人人都有的通道。

    DETACHED_PROCESS：不继承调用方的控制台（winexe 双击时本来就没有）。
    三个句柄全给 DEVNULL：双击起来的进程拿到的就是无效句柄，行为一致。
    """
    return subprocess.Popen(
        [str(exe)], cwd=str(cwd), env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def prove_the_shell_outlives_its_launcher(shell: subprocess.Popen, seconds: float = 30.0) -> None:
    """壳被「双击」起来之后，得**一直活着**，不是活两秒。

    真机对照（2026-09-10，同一份源码，同一种起法）：
      壳里监听 stdin EOF   → 2.08s 自己退，exit 0
      把那几行删掉         → 75s 还活着
    所以这条闸的判据是「一段时间之后它还在」，不是「它起来了」。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if shell.poll() is not None:
            raise SystemExit(
                f"❌ 壳起来后 {seconds:.0f} 秒内自己退了（exit={shell.returncode}）。\n"
                "   同事双击看到的就是「窗口一闪就没了」。\n"
                "   最常见的因：壳把「我没有 stdin」读成了「上游让我收摊」。")
        time.sleep(1)
    print(f"  ✅ 壳被双击起来后活过了 {seconds:.0f} 秒（没有父进程攥着它的管道）")


#: 一份完整安装里必须在的东西。少一件，应用就是「起得来但干不了活」。
INSTALL_INVENTORY = (
    "ScienceMate.exe",
    # 卸载器也在清单里：装完没有它，用户就只能自己去删目录、还留着快捷方式和
    # 「应用和功能」里的孤儿条目。
    "Uninstall.exe",
    "Resources/python/python.exe",
    # 模型 shell 里敲的 ``python3``：少了它，名字落到应用商店的桩（见 give_python_its_python3_name）。
    "Resources/python/python3.exe",
    "Resources/git/bin/git.exe",
    "Resources/tectonic/bin/tectonic.exe",
    "Resources/biber/bin/biber.exe",
)


def prove_a_reinstall_never_breaks_a_working_install(setup: Path, install_dir: Path) -> None:
    r"""应用**开着**的时候重跑 Setup.exe：必须明确失败，且已装好的那份完好无损。

    ## 为什么这条闸在这里

    「升级」在 Windows 上就是重跑 Setup.exe —— 自更新只换 harness 与界面
    （`self_update.UNIT`），壳变了就必须重装。所以「开着的时候重装」不是边角，
    是**升级的主路**。

    2026-09-10 真机：那时安装器先 `Directory.Delete(installDir, true)` 再解压，
    删到一半撞上被占用的 `Microsoft.Web.WebView2.Core.dll` 抛
    `UnauthorizedAccessException`，留下一个 `Resources\tectonic` 已经没了的安装 ——
    应用照样起得来，PDF 产不出来，用户不知道为什么。

    判据落在**两件可观察的事**上：退出码非 0（它说了话），以及重装之后清单依然齐全
    （它没把能用的弄坏）。不去断言错误措辞 —— 措辞会改，「装好的那份还在」不会。
    """
    missing_before = [n for n in INSTALL_INVENTORY if not (install_dir / n).is_file()]
    if missing_before:
        raise SystemExit(
            f"❌ 这条闸的前提不成立：重装之前这份安装就已经缺件 {missing_before}")

    proc = subprocess.run([str(setup), "--no-launch", "--install-dir", str(install_dir)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    said = ((proc.stdout or "") + (proc.stderr or "")).strip()
    missing_after = [n for n in INSTALL_INVENTORY if not (install_dir / n).is_file()]
    if missing_after:
        raise SystemExit(
            "❌ 应用开着时重装，把已装好的那份弄坏了 —— 缺件："
            f"{missing_after}\n   安装器说：{said[:600]}\n"
            "   「先删旧的再解压」就会这样：还没有能用的新东西，先拆了唯一能用的旧东西。")
    if proc.returncode == 0:
        raise SystemExit(
            "❌ 应用开着时重装居然报成功了 —— 那么装了一半的东西去哪了？\n"
            f"   安装器说：{said[:600]}")
    print(f"  ✅ 应用开着时重装：明确失败（rc={proc.returncode}）且已装好的那份完好无损")


def prove_the_window_appears(shell_pid: int, timeout_s: float = 180.0) -> None:
    """装完自检：壳必须真的**画出一个窗口**，不是把界面甩给浏览器。

    ## 为什么这条闸必须有

    V1 的壳只打印 URL、拉起默认浏览器；当时给自己的理由是「目标机无头 SSH，窗口
    画不出也验不了」。**那个理由是错的** —— 2026-09-10 实测：headless SSH 上
    WinForms + WebView2 照样把窗口建出来，`EnumWindows` 按 pid 就找得到。
    判据一直在，只是当初没去找，代价是同事装完看到的是一个浏览器标签页。

    所以这条闸守的不只是回归，还守着「别再拿我验着方便换掉产品形态」。

    轮询而不是一次性检查：窗口有可能**刚开就关**（2026-09-10 就是这样，见
    `prove_the_shell_outlives_its_launcher`）。一闪而过的窗口只有反复查才抓得住 ——
    不过「关得太快」这件事本身由那条闸负责判红，这里只回答「有没有画出来」。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    def titles_of(pid: int) -> list[str]:
        found: list[str] = []

        def visit(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value:
                    found.append(buf.value)
            return True

        user32.EnumWindows(proc(visit), 0)
        return found

    deadline = time.time() + timeout_s
    seen: list[str] = []
    while time.time() < deadline:
        seen = titles_of(shell_pid)
        if SHELL_WINDOW_TITLE in seen:
            print(f"  ✅ 壳画出了自己的窗口（title={SHELL_WINDOW_TITLE!r}）")
            return
        time.sleep(1)
    raise SystemExit(
        f"❌ 装完自检：{timeout_s:.0f} 秒内没等到标题为 {SHELL_WINDOW_TITLE!r} 的窗口。\n"
        f"   这个进程的顶层窗口：{seen or '(一个都没有)'}\n"
        "   装好的应用应当是**一个软件**，不是一个浏览器页面。\n"
        f"   壳自己说了什么，看 {shell_log_path()}（WebView2 运行时缺席时那里会有 WEBVIEW FAILED）。"
    )


def prove_a_model_command_runs_from_the_installed_app(install_dir: Path) -> None:
    """装完自检最后一关：**在安装位置**经沙箱真跑一条载荷，断言它的 stdout 真有内容。

    ## 为什么必须有这一条

    在这之前，装完自检查的是「文件在 / doctor 说得出 / health 200 / GET / 有 HTML」——
    **一条模型命令都不跑**。可用户装完第一件事就是提课题，而那要走
    `select_backend().prepare()` → Low-IL 令牌 → `CreateProcessAsUser` → 标准句柄透传
    这一整条；它**在安装位置**从来没被自检走过（历次真机验证都是从源码目录跑的，
    那里恰好一直是好的）。

    2026-09-10 实测这条闸能同时逮住两类事：产品回归，以及**「安装目录的权限被改坏了」**
    —— 我自己一个 `icacls /grant` 探针就干过，当时没有任何一层出声，症状要等一趟真课题
    跑到 experiment 才现形（还被我误判成产品缺陷）。有了它，构建当场就红。

    判据落在**载荷的 stdout 上**，不落在返回码上：Low-IL 下句柄没透传时进程照样 rc=0
    而 print 全丢（#861 踩过），只看 rc 会放过它。
    """
    step("装完自检：在安装位置真跑一条模型命令")
    python = install_dir / "Resources" / "python" / "python.exe"
    harness = python.parent / "Lib" / "site-packages" / "app" / "harness"
    work = Path(tempfile.mkdtemp(prefix="win-payload-check-"))
    probe = work / "payload_probe.py"
    probe.write_text(_PAYLOAD_PROBE, encoding="utf-8")
    try:
        proc = subprocess.run(
            [str(python), "-X", "utf8", str(probe), str(harness), str(work / "scratch")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        line = (proc.stdout or "").strip().splitlines()
        try:
            report = json.loads(line[-1]) if line else {}
        except ValueError:
            report = {}
        if not report:
            raise SystemExit(
                "❌ 装完自检：探针自己没跑起来 —— 先怀疑自检，别急着怪产品。\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr[-1500:]!r}")
        print(f"  后端 = {report['backend']}；守得住 = {', '.join(report['caps']) or '(空集)'}")
        if PAYLOAD_MARKER not in report.get("out", ""):
            raise SystemExit(
                f"❌ 装完自检：在安装位置经 {report['backend']} 沙箱跑一条载荷**没拿到输出**"
                f"（rc={report['rc']}，stderr={report['err']!r}）。\n"
                "   装好的应用跑不了模型命令 —— 提一个真课题会在 experiment 节点全线失败。\n"
                "   常见两因：①沙箱后端在这台机器上起不来；②安装目录对低权限令牌不可读/不可执行。")
        print(f"  ✅ 载荷在安装位置真跑起来了：{report['out']}")
        python3_lands_on_the_packaged_interpreter(report.get("python3") or {}, python)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def python3_lands_on_the_packaged_interpreter(typed: dict, python: Path) -> None:
    r"""模型 shell 里敲的 ``python3`` 起来的，就是包里这份解释器；而且名字是**包里带的**。

    判据落在它报的 ``sys.prefix`` 上（依赖装在那里），不落在返回码上：落到应用商店的桩
    时 rc=49 且一个字都不打印（2026-09-23 真机）。

    「包里带的」：运行时 ``python3_is_python`` 遇到逐字节相同的文件不写，所以跑完之后
    ``python3.exe`` 仍是安装器解出来的那个独立文件；要是它成了 ``python.exe`` 的硬链接，
    就是运行时又写了一遍 —— 这里是可写的临时安装目录所以写成了，装在
    ``C:\Program Files`` 下就写不进。
    """
    lines = (typed.get("out") or "").splitlines()
    prefix = Path(lines[-1]) if lines else None
    if prefix is None or not prefix.is_dir() or not prefix.samefile(python.parent):
        raise SystemExit(
            "❌ 装完自检：模型 shell 里敲 python3 没落到包内解释器"
            f"（rc={typed.get('rc')}，stdout={typed.get('out')!r}，stderr={typed.get('err')!r}）。\n"
            "   rc=49 且没有输出 = 应用商店的桩：Resources\\python\\python3.exe 不在。")
    alias = python.parent / "python3.exe"
    if alias.samefile(python):
        raise SystemExit(
            f"❌ 装完自检：{alias} 是运行时补写的（成了 python.exe 的硬链接），不是包里带的。\n"
            "   装在 C:\\Program Files 下时运行时写不进 —— 打包时的 python3.exe 与 python.exe 不一致？")
    print(f"  ✅ 模型 shell 里的 python3 = 包内解释器（{prefix}），名字是包里带的")


def prove_the_installed_app_makes_the_platforms_pdf(install_dir: Path) -> None:
    """装完自检：**在安装位置**真编一次平台的 PDF 样本（中文 + GB/T 7714 参考文献 + 插图）。

    ## 为什么必须有这一条（2026-09-23）

    之前这里判「能出 PDF」看的是 tectonic.exe 在不在。一台干净的 Windows 上它在，可每次
    编译 5 秒 ``os error 5``（它的 formats 目录建在 %LOCALAPPDATA%，墙里写不进），随包里
    也没有模板要的 biber。打包机上一切正常：那个目录早就在，系统里也有别的 TeX。
    用户那边是 writing 写了 45 分钟、最后一步才撞上。

    所以判据落在**产物**上：样本编出了 PDF 才算过，原因是实测的原话。
    """
    step("装完自检：在安装位置真编一次平台的 PDF")
    python = install_dir / "Resources" / "python" / "python.exe"
    work = Path(tempfile.mkdtemp(prefix="win-pdf-check-"))
    probe = work / "pdf_probe.py"
    probe.write_text(tex_toolchain.PDF_PROBE, encoding="utf-8")
    try:
        proc = subprocess.run(
            [str(python), "-X", "utf8", str(probe), str(work / "home")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
        record = tex_toolchain.read_the_probe_record(proc.stdout)
        if not record:
            raise SystemExit(
                "❌ 装完自检：PDF 实测探针自己没跑起来 —— 先怀疑自检，别急着怪产品。\n"
                f"stdout={proc.stdout[-1500:]!r}\nstderr={proc.stderr[-1500:]!r}")
        if not record.get("works"):
            raise SystemExit(
                f"❌ 装完自检：装好的应用出不了平台的 PDF —— {record.get('reason')!r}\n"
                f"   工具：{record.get('identity')}\n"
                "   用户会在 writing 最后一步撞上它。")
        tools = record.get("identity") or {}
        print(f"  ✅ 在安装位置编出了平台的 PDF（{tools.get('compiler')} + biber，"
              f"{record.get('seconds')}s）")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    global UV
    parser = argparse.ArgumentParser(description="打 Windows 便携应用")
    parser.add_argument("--with-ui", action="store_true",
                        help="连前端 static_ui 一起 build（默认 API-only）")
    parser.add_argument("--skip-ui", action="store_true",
                        help="界面已 build 过（out/index.html 在）就不再 build")
    parser.add_argument("--skip-check", action="store_true",
                        help="不跑装完自检（明知会失败、拿包去调试时用）")
    parser.add_argument("--installer", action="store_true",
                        help="再拼一个自解压单文件 ScienceMate-Setup.exe（zip 857MB 较慢）")
    parser.add_argument("--edition", choices=("personal", "pro"), default="personal",
                        help="哪种发行（EXEC_PLAN_TWO_EDITIONS §1）：只差 edition.json 与自更新源")
    parser.add_argument("--update-source", default="",
                        help="烧进包里的自更新源（仓库网址）。专业版必给；个人版留空 = 出厂值")
    parser.add_argument("--sign-with", default="", metavar="证书.pfx",
                        help="用这张 pfx 给壳/卸载器/安装器签名（密码放环境变量 "
                             "WINDOWS_SIGNING_PASSWORD）。也可用环境变量 WINDOWS_SIGNING_PFX")
    parser.add_argument("--sign-with-thumbprint", default="", metavar="指纹",
                        help="用 CurrentUser\\My 里这个指纹的证书签。也可用环境变量 "
                             "WINDOWS_SIGNING_THUMBPRINT")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        raise SystemExit("这个脚本只打 Windows 包。")
    # 个人版是开源发行：只从导出的公开树打（见 payload_release 里那条的来历）。
    payload_release.the_personal_edition_is_built_from_the_public_tree(REPO, args.edition)
    UV = find_uv()
    # 证书**在动手之前**就解析好：pfx 路径打错了要在这一秒说，不是等 40 分钟打完包
    # 才发现签不了。
    certificate = the_signing_certificate(args.sign_with, args.sign_with_thumbprint)
    if certificate is not None:
        print(f"▶ 这次构建会签名：{certificate.describe()}")

    DIST.mkdir(exist_ok=True)
    app = DIST / APP_DIR_NAME
    if app.exists():
        try:
            shutil.rmtree(io_path(app))
        except OSError as exc:
            raise SystemExit(f"上一次打的包还在被占用或删不掉：{app}。关掉用着它的进程、"
                             f"查一下文件权限再来。{exc}") from exc
    resources = app / "Resources"
    resources.mkdir(parents=True)

    interface: Path | None = None
    if args.with_ui:
        interface = build_the_interface(args.skip_ui)

    # 服务器包在装配之前打：`stage_the_pro_only_things` 要把它放进 app/。（它只拷 git 跟踪的
    # 文件，装配进来的 app/static_ui、app/harness 不会混进去。）
    server_bundle = None
    if args.edition == "pro":
        import build_server_bundle
        server_bundle = build_server_bundle.build(interface=interface, update_source=args.update_source)
    stage_into_the_package(interface)
    payload_release.stage_the_pro_only_things(BACKEND, args.edition, server_bundle)
    try:
        wheel = build_the_wheel(with_ui=args.with_ui)
    finally:
        unstage_from_the_package()
    python = install_the_runtime(resources, wheel)
    place_the_tectonic(resources)
    place_the_biber(resources)
    place_the_git(resources)
    place_the_webview2_sdk(app)
    build_the_shell(app)
    build_the_uninstaller(app)
    # 签在**导出壳之前**：`dist/shell-windows/` 那一份会被打进自更新载荷，
    # 换到用户机器上就是他们双击的那个 exe —— 导出一份没签的等于自更新把签名撤了。
    sign_what_the_user_double_clicks(app, certificate)
    export_the_shell_for_the_payload(app)
    write_the_backend_json(app, python)
    # 发行写在 Resources/（python 的上一层）：launcher 那个解释器的 sys.prefix.parent。
    payload_release.write_the_edition(resources, args.edition, args.update_source)

    print(f"\n\u2705 {app}（{megabytes_on_disk(app)} MB）")
    if not args.skip_check:
        prove_the_package_works(app, python, with_ui=args.with_ui, edition=args.edition)

    if args.installer:
        setup = make_the_installer(app, args.edition, certificate)
        if not args.skip_check:
            prove_the_installer_works(setup)
            prove_the_signature_survived_the_payload(setup, certificate)
        if certificate is None:
            say_it_is_unsigned(setup)
        print(f"\n\u2705 单文件安装器：{setup}"
              "（双击出安装向导：选位置、桌面/开始菜单快捷方式，装完可直接运行）")

    print("\n整个目录拷走即用：双击 ScienceMate.exe。"
          "\n（加 --with-ui 连界面一起打；加 --installer 出单文件 Setup.exe。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

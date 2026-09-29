"""一条命令把这套东西跑起来。

## 它要消灭的东西

个人版的验收线是「安装到第一条回复 ≤ 5 分钟」。在此之前，起这套东西要：装
Postgres、跑 alembic、设四个环境变量、起后端、另起一个前端 dev server、再自己
在浏览器里敲地址。每一步都是一次放弃的机会。

WP-04（SQLite）、WP-10（档位装配自带数据根）、WP-03（UI 静态导出）之后，这些
前提一个都不剩了。剩下的只是把它们串起来：解析数据根 → 找到静态 UI → 起
uvicorn → 打开浏览器。

## 它**不**做的事

不装依赖、不改系统设置、不申请权限。一个启动器要是会动这些东西，它就成了另一
个需要先理解的东西。
"""
from __future__ import annotations

import argparse
import os
import signal
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _the_applied_payload() -> Path | None:
    """自更新切换过去的那一版载荷 —— **只有它比随包那份新才算数**。

    在 config 之前就要问「数据在哪」—— 规则只在 `app.data_root_default`，这里不抄。

    ## 为什么要比版本（2026-09-10 真机撞到）

    这里原来是「有载荷就用载荷」，理由写的是「自更新过的那份，优先于随包那份」。
    那句话在**只自更新**的世界里成立；**从有人重装的那一刻起就不成立了** ——
    而重装恰恰是我们让用户做的事：自更新换不了壳（载荷里只有 harness 和 static_ui），
    壳一变就必须重下安装包。

    真机现场：机器上先自更新到 0.4.4（数据根留下 `payload/current.json`），随后我装了
    一个**新打的包**并起来跑 —— 应用照样加载数据根里那份旧载荷，新包里的代码从头到尾
    没进过场。`/api/v1/update` 老老实实说 `installed_from: "payload"`，而我在读一份
    没跑过的修复的"验证结果"。

    正解是把问题问对：不是「有没有自更新过」，而是**「哪一份更新」**。
    版本比较用 `self_update.is_newer`（同一把尺，不在这里再实现一遍）：
    - 载荷更新 → 用载荷（自更新照常生效）
    - 一样新 / 载荷更旧 → 用随包那份（**重装能生效**，这正是原来缺的那一半）
    - 随包那份没有版本标记（这套机制之前装的）→ 任何载荷都算新，用载荷
    """
    from app.data_root_default import data_root_before_config
    from app.services.self_update import active_payload_dir, is_newer, version_of

    try:
        applied = active_payload_dir(data_root_before_config())
    except Exception:
        return None
    if applied is None:
        return None
    staged_version = version_of(applied / "harness")
    if staged_version is None:
        return None
    if not is_newer(staged_version, version_of(_the_bundled_harness())):
        return None
    return applied


def _the_bundled_harness() -> Path:
    """随包分发的那份 harness（装在应用里的）。它的版本标记是「重装带来了什么」。"""
    return Path(__file__).resolve().parent / "harness"


def switch_to_the_staged_update() -> None:
    """有暂存的更新就切成当前载荷。此刻什么都没加载，是唯一安全的切换时机。

    失败不拦启动 —— 错误写进数据根、由 GET /update 报出来。之后的一切（接管载荷里的
    app/、两个 find_*）看见的都是切换后的那一版。
    """
    from app.data_root_default import data_root_before_config
    from app.services.self_update import apply_staged_at_launch

    applied = apply_staged_at_launch(data_root_before_config())
    if applied:
        print(f"已切换到更新 {applied}")


def the_payload_app(applied: Path | None) -> Path | None:
    """载荷里带的后端 `app/`（`extras/app/`，见 payload_release.EXTRA_UNITS）—— 没带就是 None。"""
    if applied is None:
        return None
    candidate = applied / "extras" / "app"
    return candidate if (candidate / "launcher.py").is_file() else None


def hand_over_to_the_payload_app(argv: list[str] | None) -> int | None:
    """载荷里带了更新的后端 `app/` 就换成它跑 —— **同一个进程、同一个 pid**（#953 ⑤）。

    自更新原来只换 harness 和界面；后端 `app/`（API、自更新逻辑、启动器本身）一改就得
    让人重装。载荷的第二个归档现在把 `app/` 也带上（`extras/app/`），启动时由这份随包的
    launcher 把它接过来：把它所在目录放到 `sys.path` 最前面、把已经 import 进来的随包
    `app.*` 从 `sys.modules` 清掉、import 载荷里的 `app.launcher` 并把 argv 原样交给它的
    `main()`。之后 uvicorn 按字符串加载的 `app.main:app` 走的也是同一条 `sys.path`。

    ## 为什么不 `os.execv`

    壳（Windows/Mac）追踪的是它 CreateProcess 出来的那个 pid；Windows 上 `execv` 是
    spawn+退出，壳会看到「后端死了」连锁收摊 —— 正因如此壳的 backend.json 给足了
    `PYTHONUTF8=1`，让 `ensure_utf8_mode` 那次 re-exec 从来不用发生。换进程不是选项，
    换代码可以：此刻还没 import config、没开端口、没碰数据库，`app.*` 里活着的只有这个
    launcher 和 harness 桥。

    ## 两道闸（与壳自替换 #953 ④ 同形）

    - **版本**：`_the_applied_payload()` 已经只认「比随包新」的载荷 —— 重装了新包、数据根
      里留着旧载荷时，这里不会把新后端换成旧的（#958 那条规矩）。
    - **循环终止**：接过来的那份 launcher 自己也会跑到这里；它发现自己就住在载荷里，返回 None
      往下走。判据是「我在哪」而不是「换过没有」—— 环境变量那种记号会跟着 env 漏给子进程。

    返回 None ＝ 没有可接的，照常起随包这份；否则返回载荷里 `main()` 的返回值。
    """
    candidate = the_payload_app(_the_applied_payload())
    if candidate is None:
        return None
    here = Path(__file__).resolve().parent
    if here == candidate.resolve():
        return None
    version = candidate.parent.parent.name
    sys.path.insert(0, str(candidate.parent))
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]
    import importlib

    new = importlib.import_module("app.launcher")
    landed = Path(new.__file__).resolve().parent
    if landed != candidate.resolve():
        raise RuntimeError(f"要接载荷 {version} 里的 app/，import 到的却是 {landed}")
    print(f"APP-SWAP: 用载荷 {version} 里的后端 app/（{candidate}）")
    return new.main(argv)


def find_static_ui() -> Path | None:
    """静态 UI 在哪。

    三个来源，按"离用户最近"排序：显式指定 → 随包分发 → 仓库里的构建产物。
    一个都找不到时返回 None —— 后端照样起得来，只是没有界面；那时应该说清楚
    怎么补，而不是假装一切正常。
    """
    explicit = os.environ.get("STATIC_UI_ROOT")
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_dir() else None
    applied = _the_applied_payload()
    if applied is not None and (applied / "static_ui").is_dir():
        return applied / "static_ui"          # 自更新过的那份，优先于随包那份
    bundled = Path(__file__).resolve().parent / "static_ui"
    if bundled.is_dir():
        return bundled
    from_repo = Path(__file__).resolve().parents[2] / "frontend" / "out"
    return from_repo if from_repo.is_dir() else None


def the_parent_is_gone(original_ppid: int, current_ppid: int) -> bool:
    """父进程还在不在。

    父进程一死，孩子就被过继给 init/launchd（pid 1），`getppid()` 于是变了。
    判据写成"变没变"而不是"是不是 1"：两种平台上过继给谁不完全一样，而"变了"
    在哪儿都成立。
    """
    return current_ppid != original_ppid


def die_when_the_parent_does(
    *,
    original_ppid: int | None = None,
    get_ppid=None,
    on_death=None,
    interval: float = 1.0,
) -> threading.Thread:
    """父进程没了，自己也收摊。

    2026-09-06 真机实测：把桌面壳 `pkill` 掉之后，后端子进程**活了下来** ——
    一次清出 9 个孤儿，每个都还占着数据根和一个端口。壳里的
    `applicationWillTerminate` 只在 AppKit 正常退出时跑，SIGTERM / SIGKILL /
    崩溃都不跑；而用户按下的"强制退出"正是这三条路。

    孤儿的代价不是多一个进程：下次打开会撞上一个还占着同一个数据根的旧实例，
    而那种冲突的症状（库锁住、端口被占、改动互相覆盖）没有一条指得回"上次没
    退干净"。

    轮询而不是等一根管道 EOF：管道那套要父子两边都配合，而这条路必须在**父进程
    崩掉**时也成立 —— 那时候没有人还能配合。

    「父进程还在吗」的判据按平台走（`shared.lib.process_control.parent_probe`）：
    POSIX 看 `getppid()` 变没变；Windows 不过继、`getppid()` 父死也不变，改为拿父进程
    句柄问它退出没。测试注入 `get_ppid` 序列时走纯判据 `the_parent_is_gone`。
    """
    from app.services.harness_imports import harness_module

    process_control = harness_module("shared.lib.process_control")
    if get_ppid is None:
        parent_alive = process_control.parent_probe()
    else:
        start_ppid = get_ppid() if original_ppid is None else original_ppid

        def parent_alive() -> bool:
            return not the_parent_is_gone(start_ppid, get_ppid())

    terminate = on_death or (lambda: process_control.terminate(os.getpid()))
    return process_control.watch_parent(parent_alive, terminate, interval=interval)


def _bundled_binary(subdir: str, *names: str) -> Path | None:
    """随包某个二进制在哪 —— `find_the_bundled_*` 的共用底座。

    锚在**跑着我们那个解释器**旁边：`sys.prefix` 是 `Resources/python`，随包的
    工具就在 `Resources/<subdir>/bin/`（git 在 `Resources/git`、tectonic 在
    `Resources/tectonic`，打包脚本按这个布局放）。不从 `__file__` 往上数目录：
    数错一层就静默找不到、悄悄回落系统 —— 2026-09-07 git 那次真机就栽在这。

    `names` 按顺序试，**第一个存在的**即结果：跨平台二进制在 Windows 上带 `.exe`、
    POSIX 上不带，两个都传（`.exe` 在前）就都认，而不必靠 `os.name` 去选名字
    —— 那样既没法在 POSIX 宿主上确定性地测 Windows 分支（`os.name="nt"` 一
    monkeypatch，pathlib 就拒绝在本机造 WindowsPath），也把「找哪个文件」和
    「跑在哪个平台」绑死。
    """
    root = Path(sys.prefix).parent / subdir / "bin"
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def find_the_bundled_git() -> Path | None:
    """随包分发的 git 在哪。

    平台的每个 Project 仓库都是 git 仓库，而 macOS 的 `/usr/bin/git` 只是一个
    shim：没装 Command Line Tools 时它弹一个系统框然后失败。目标用户是同事
    不是开发者 —— "新建项目"这个装完后的第一个动作就会当场失败。

    所以应用自己带一份（`scripts/package/build_mac_app.py` 编的，只做本地仓库，
    不含网络那半边）。找法与 `find_the_harness` 同一套：显式指定 → 随包 →
    找不到就返回 None（那时用系统的，开发机上本来就有）。
    """
    explicit = os.environ.get("HARNESS_GIT")
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    # 布局与「两名都认」的道理都在 `_bundled_binary`。Windows 上随包的是
    # `git.exe` —— 不认它，装完的应用就永远回落系统 git，而没装 Git for Windows
    # 的机器上「新建项目」当场失败（正是自带一份要挡的那种）。
    return _bundled_binary("git", "git.exe", "git")


def find_the_bundled_biber() -> Path | None:
    """随包分发的 biber（参考文献）在哪。

    平台模板的参考文献走 biblatex/biber；tectonic 自己不带 biber，编到那一步从 PATH
    调外部的。所以它和 tectonic 一样得在 PATH 上 —— 没有它，干净机器上每篇论文都卡在
    参考文献（2026-09-23 真机：``error: program not found``）。布局同 git / tectonic。
    """
    return _bundled_binary("biber", "biber.exe", "biber")


def find_the_bundled_tectonic() -> Path | None:
    """随包分发的 tectonic（LaTeX 引擎）在哪。

    Windows 上没有任何 LaTeX（pdflatex/latexmk/MiKTeX 全无），而
    `shared.tools.library.latex._resolve_compiler` 只从 PATH 上找
    (`shutil.which("latexmk")` → `shutil.which("tectonic")`)。所以随包那份
    tectonic 若不在 PATH 上就等于不存在 —— 装完的应用发一个真课题、走到 writing
    出 .tex，编译那一步就找不到编译器、出不了 PDF。「装得上就能出 PDF」的最后
    一块。

    找法与 `find_the_bundled_git` **同一套**：显式 `HARNESS_TECTONIC` → 随包
    （锚在跑着我们的解释器旁边，`sys.prefix.parent/tectonic/bin/<name>`，和 git 的
    `git/bin/git` 并排，打包脚本按同一布局放）→ 找不到返回 None（开发机上用
    系统 latexmk，本来就不需要它）。单文件二进制，Windows 上叫 `tectonic.exe`。
    """
    explicit = os.environ.get("HARNESS_TECTONIC")
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    # 布局与「两名都认」的道理都在 `_bundled_binary`（Windows 上是 `tectonic.exe`）。
    return _bundled_binary("tectonic", "tectonic.exe", "tectonic")


def find_the_harness() -> Path | None:
    """harness 在哪。

    后端自己不做科研 —— 它 spawn 一个 harness worker，那个 worker 才是跑
    agent loop 的东西。`HARNESS_ROOT` 指不到，后端照样起得来、界面照样打得开、
    项目照样建得出，**只是发消息永远等不到回复**。2026-09-05 写验收脚本时实测：
    零配置起服务，`/health/ready` 是 200，`harness_root` 那一项是
    `not_configured`，一条消息发出去之后什么都没发生。

    所以这个答案不能留给用户去配。三个来源与 `find_static_ui` 同一套排序：显式
    指定 → 随包分发 → 仓库里的源码。判据是 `core/agent_loop.py` 在不在 ——
    与 `/health/ready` 用的判据同一条（`main.py`），不另立一份。
    """
    explicit = os.environ.get("HARNESS_ROOT")
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if (candidate / "core" / "agent_loop.py").is_file() else None
    applied = _the_applied_payload()
    if applied is not None and (applied / "harness" / "core" / "agent_loop.py").is_file():
        return applied / "harness"            # 自更新过的那份，优先于随包那份
    bundled = _the_bundled_harness()
    if (bundled / "core" / "agent_loop.py").is_file():
        return bundled
    from_repo = Path(__file__).resolve().parents[3]
    return from_repo if (from_repo / "core" / "agent_loop.py").is_file() else None


def pick_a_free_port(preferred: int | None = None, host: str = "127.0.0.1") -> int:
    """端口。给了就用给的（占了就直说），没给就让系统挑一个空的。

    默认不写死一个端口：个人电脑上 8000 这类端口十有八九已经有别的东西在用，
    而"端口被占"是最不该让用户自己去查的一类失败。

    探的是**将要绑的那个地址**，不是固定的 loopback：组织服务器绑 0.0.0.0，而
    同一台机器上 nginx 可能只占着 `<局域网IP>:18080` —— 探 127.0.0.1 说"空的"，
    真绑 0.0.0.0 才撞上。判据要落在真要做的那件事上。
    """
    if preferred is not None:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, preferred))
        return preferred
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def describe_environment() -> list[tuple[str, str]]:
    """`doctor` 打印的那几行：数据在哪、界面在哪、这台机器守得住什么。"""
    from app import assembly
    from app.config import settings

    facts = [
        # 名字只用来显示；经装配层取，别处不许自己读档位（那道扫盘闸拦得对，
        # 它已经拦下过我三次）。
        ("profile", assembly.profile_name()),
        ("data root", settings.platform_data_root or str(default_data_root())),
        ("database", settings.database_url.split("://", 1)[0]),
        ("static UI", str(find_static_ui() or "(not built — run: npm run build in platform/frontend)")),
    ]
    try:
        # harness 可能是同一个 wheel 里的包，也可能是仓库里的一个目录（开发时）。
        # 后一种情况下它不在 sys.path 上 —— doctor 的职责是**如实报告**，所以
        # 它先按配置去找，找不到就说找不到，而不是报一句 "No module named core"
        # 让人以为装漏了什么。
        harness = find_the_harness()
        if harness is None:
            facts.append(("harness", "(not found — 发消息不会有回复)"))
            facts.append(("pdf engine", "(unresolved — harness not found)"))
            return facts
        facts.append(("harness", str(harness)))
        from app.services.self_update import version_of

        facts.append(("version", version_of(harness) or "(no PAYLOAD_VERSION marker — 打这套之前装的)"))
        facts.append((
            "git",
            str(find_the_bundled_git() or shutil.which("git") or "(not found)"),
        ))
        facts.append(("biber", str(find_the_bundled_biber() or shutil.which("biber") or "(not found)")))
        harness_root = str(harness)
        if harness_root not in sys.path:
            sys.path.insert(0, harness_root)

        # 模型的 shell：experiment 节点跑的每条实验命令都走 `bash_shell()`。git、
        # tectonic 都在这儿报一行，模型的 shell 也该报——否则装完自检（跑 doctor）
        # 根本没解析过 `_windows_bash`，一份随包 bash 缺了、或干净机器上悄悄回落到
        # WSL 启动器，都要等真发一个课题、走到 experiment 才炸。这一行让「模型会拿到
        # 哪个 shell」在自检里就现形。自成一个 try：shell 解析失败不该冒充 sandbox 失败。
        try:
            from shared.lib.shell import bash_shell

            facts.append(("model shell", bash_shell()))
        except Exception as exc:  # noqa: BLE001 - 报不出就如实说，不拦 doctor
            facts.append(("model shell", f"(unresolved — {exc})"))
        # PDF 引擎：问编译时问的**同一个函数**（``latex._resolve_compiler``，latexmk 优先、
        # 缺则 tectonic；随包那份已由 prepare_the_environment 放上 PATH）。这里曾经自己排
        # 「随包 tectonic → latexmk → tectonic」：装了 MiKTeX 的机器上 doctor 说 tectonic、
        # 编译用的却是 latexmk。这一行只说**会用哪个文件**；能不能真出 PDF 看下面的「pdf」。
        try:
            from shared.tools.library import latex

            if latex.no_tex_engine():
                engine = "(none — 装不出 PDF；Windows 需随包 tectonic)"
            else:
                kind, binary = latex._resolve_compiler()
                engine = f"{kind}: {binary}"
            facts.append(("pdf engine", engine))
        except Exception as exc:  # noqa: BLE001 - 报不出就如实说，不拦 doctor
            facts.append(("pdf engine", f"(unresolved — {exc})"))
        # 能不能真出 PDF：最近一次**实测**（shared/lib/pdf_toolchain），不按文件在不在猜。
        # doctor 只读记录、不编译；从没测过就如实说没测过。
        try:
            from shared.lib import pdf_toolchain

            facts.append(("pdf", pdf_toolchain.describe(pdf_toolchain.read_record())))
        except Exception as exc:  # noqa: BLE001 - 报不出就如实说，不拦 doctor
            facts.append(("pdf", f"(unresolved — {exc})"))

        from core import isolation

        # 只读快照：doctor 要的是一个答案，不是一个后端实例。直接
        # `select_backend()` 会撞上「只有咽喉能拿后端」那道闸 —— 它拦得对，
        # 拿到后端的下一步通常就是起进程。
        record = isolation.enforcement_snapshot()
        if record.get("backend"):
            facts.append(("sandbox", f"{record['backend']}: {', '.join(record['enforced'])}"))
        else:
            facts.append(("sandbox", f"none — {record.get('unavailable_reason', 'unknown')}"))
    except Exception as exc:  # noqa: BLE001 - doctor 报告失败，不制造失败
        facts.append(("sandbox", f"none — {exc}"))
    return facts


def _open_when_ready(url: str, delay: float = 1.5) -> None:
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def prepare_the_environment() -> tuple[Path | None, Path | None]:
    """把随包分发的东西接到环境上。返回 (界面, harness) —— 找不到就是 None。

    这一步必须**跑在任何人导入 `app.config` 之前**：`Settings` 是导入那一刻对
    环境的快照，`app.main` 拿它决定挂不挂静态界面中间件。晚一步，装出来的包
    就只有 API 没有首页 —— 而且 API 全好，看起来一切正常。

    `setdefault` 而不是赋值：显式配过的一律不动。这里只负责让"什么都没配"这条
    路也能跑到底 —— 个人版的全部承诺就是这条路。
    """
    # 暂存的更新在 main() 更早的地方已经切过去了（switch_to_the_staged_update，
    # 排在把启动交给载荷里的 app/ 之前）—— 下面两个 find_* 看见的就是新的那一版。
    static_ui = find_static_ui()
    if static_ui is not None:
        os.environ.setdefault("STATIC_UI_ROOT", str(static_ui))
    bundled_git = find_the_bundled_git()
    if bundled_git is not None:
        # 把随包那份 git 放到 PATH 最前面。后端与 harness worker 都是 `"git"`
        # 从 PATH 里找 —— 一处接线，两边都拿到，不必在每个调用点各改一次。
        os.environ["PATH"] = f"{bundled_git.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ.setdefault("HARNESS_GIT", str(bundled_git))
    bundled_tectonic = find_the_bundled_tectonic()
    if bundled_tectonic is not None:
        # 同 git：放 PATH 最前面。latex 编译在 harness worker 里跑，
        # `_resolve_compiler` 用 `shutil.which("tectonic")` 解析成**绝对路径**再交给
        # 沙箱执行（#865 就是这么出的 PDF），所以只要 worker 继承到的 PATH 上有它就够。
        # 一处接线，编译那一步就找得到编译器。
        os.environ["PATH"] = f"{bundled_tectonic.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ.setdefault("HARNESS_TECTONIC", str(bundled_tectonic))
    bundled_biber = find_the_bundled_biber()
    if bundled_biber is not None:
        # tectonic 编到参考文献时从 PATH 调 biber：同一条接线。
        os.environ["PATH"] = f"{bundled_biber.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    harness = find_the_harness()
    if harness is not None:
        os.environ.setdefault("HARNESS_ROOT", str(harness))
        os.environ.setdefault("HARNESS_BRIDGE_ENABLED", "true")
    return static_ui, harness


def main(argv: list[str] | None = None) -> int:
    # 最前面（先于任何输出/文件 IO）：Windows 上没在 UTF-8 模式就带 PYTHONUTF8=1 re-exec
    # 一次，否则后端一路写读中文的 transcript/memory 会 cp1252 崩或乱码。POSIX/已是 utf-8=无操作。
    #
    # 引导住在 harness（shared.lib.platform_env），而后端是**独立包**、此刻 harness 还没上
    # sys.path —— 经 harness_module 这座桥拿它（和 _child_environment 取 utf8_mode_env 同一条
    # 路，不裸 import shared）。还没找到 harness 就跳过：那种情形后端也做不了什么，而 POSIX
    # 上 ensure_utf8_mode 本就是无操作。
    try:
        from app.services.harness_imports import harness_module

        harness_module("shared.lib.platform_env").ensure_utf8_mode()
    except Exception:  # harness 未就位 —— best-effort，不拦启动
        pass

    # 先把暂存的更新切成当前载荷，**再**看载荷里有没有带更新的后端 app/ —— 顺序反了，
    # 装完更新的第一次重启会漏接（真机 2026-09-12：指针还没写，接管看到的是「没有载荷」，
    # 新后端要等下一次启动）。接得到就把这次启动整个交给它（同一进程）；接不到照常往下。
    # 都必须在 import config / 开端口之前 —— 那之后随包这份代码就已经在场了。
    switch_to_the_staged_update()
    handed = hand_over_to_the_payload_app(argv)
    if handed is not None:
        return handed

    parser = argparse.ArgumentParser(
        prog="research-platform",
        description="Start the research platform on this machine.",
    )
    parser.add_argument("command", nargs="?", default="start", choices=["start", "doctor"])
    parser.add_argument("--port", type=int, default=None, help="默认让系统挑一个空端口")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="绑哪个地址。桌面默认只听本机；组织服务器要让同事连上，给 0.0.0.0",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--exit-with-parent", action="store_true",
        help="父进程（桌面壳）没了就自己收摊 —— 否则强制退出会留下一个孤儿后端",
    )
    args = parser.parse_args(argv)

    # ⚠️ 顺序不是风格问题：**先把环境备齐，再让任何人碰 `app.config`。**
    #
    # `Settings` 是导入那一刻对环境的一次快照，而 `app.main` 用它决定要不要挂
    # 静态界面中间件（`if settings.static_ui_root:`）。所以在这几个 setdefault
    # 之前把 config 导进来，等于让后端拿到一份"什么都没配"的快照 ——
    # 2026-09-07 真机实测：装出来的 .app 双击起来，`/` 直接 404，日志里还写着
    # `HARNESS_ROOT 未配置`。API 全好，只是**首页没了**。
    #
    # 顺序由下面这个函数一次做完；判据在
    # `tests/test_the_page_must_actually_come_up.py`（真起一次、真要一次首页）。
    static_ui, harness = prepare_the_environment()

    # 「数据在哪」问 config，不在这儿再算一遍 —— 这一行以前印的是自己抄的那份
    # 默认值，配了 PLATFORM_DATA_ROOT 时它印 `~/.harness-framework`、库却建在
    # 别处。同一句话里它还负责把这个根交给 harness（两边分叉就起不来）。
    from app.config import DataRootError, publish_the_data_root

    try:
        root = publish_the_data_root()
    except DataRootError as exc:
        # 配置错要说人话。栈回溯是给写代码的人看的；这条错误的读者是刚双击
        # 图标的用户，他要的是"哪儿配拧了、怎么改"。
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "doctor":
        for name, value in describe_environment():
            print(f"{name:>10}: {value}")
        return 0

    try:
        port = pick_a_free_port(args.port, host=args.host)
    except OSError as exc:
        print(f"端口 {args.port} 用不了（{args.host}）：{exc}", file=sys.stderr)
        return 2

    url = f"http://127.0.0.1:{port}/"
    print(f"数据在 {root}")
    if harness is None:
        print("没有找到 harness（core/agent_loop.py）—— 发消息不会有回复。"
              "开发时从仓库根跑；装包时它随包分发。", file=sys.stderr)
    if static_ui is None:
        print("没有找到界面构建产物 —— 只有 API 可用。"
              "构建：cd platform/frontend && PLATFORM_STATIC_EXPORT=1 npm run build")
    print(f"打开 {url}")
    if not args.no_browser:
        _open_when_ready(url)

    if args.exit_with_parent:
        die_when_the_parent_does()

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())

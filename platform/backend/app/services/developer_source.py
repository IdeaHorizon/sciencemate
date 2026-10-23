"""开发者模式：桌面版从一个**源码 checkout** 跑，而不是从随包 / 载荷那份。

## 要解决的事

同事改了代码想看效果，以前只有两条路：等我重新打包，或者在终端里从源码起后端再
开浏览器。桌面版装好的那份 harness、界面、后端 `app/` 都是打包时拷进去的，改自己的
checkout 对它没有任何影响。

launcher 本来就按「显式指定 → 载荷 → 随包 → 仓库」找 harness 与界面（`HARNESS_ROOT`、
`STATIC_UI_ROOT`），后端 `app/` 也有一条「交给载荷里那份」的接管路（#953 ⑤）。开发者
模式只是把这三个答案都指到同一个目录：

    <checkout>/                       HARNESS_ROOT（core/agent_loop.py 在这儿）
    <checkout>/platform/backend/app   启动时接管过去的后端（launcher.py 在这儿）
    <checkout>/platform/frontend/out  STATIC_UI_ROOT（有就用；没构建过就照旧用随包 / 载荷那份界面）

## 声明放哪

`<数据根>/developer.json`：`{"source_root": "/abs/path"}`。放数据根而不放安装目录：
.app 签过名，Windows 上正在跑的文件换不掉 —— 与载荷指针（`payload/current.json`）同一个
理由。关于页写它，launcher 在 **import config 之前**读它（所以这个模块不在导入期碰
`app.config`，和 `self_update` 一样）。

## 什么时候生效

下一次启动。harness 那一层本来就是每个 run 拉起一个 worker，改了下一条消息就是新代码；
后端 `app/` 与界面是进程启动时接的，改了要重启应用（关于页上有重启按钮）。

## 只有个人档有这回事

组织服务器是部署出来的，没有「我自己的 checkout」；它的版本归 `server_update` 管。
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.services.say import SaidToAPerson

DECLARATION = "developer.json"


class DeveloperSourceError(SaidToAPerson):
    """目录不成立时说给人听的那一句 —— 两种语言都写。"""


@dataclass(frozen=True)
class SourceCheckout:
    """一个合格的源码 checkout 的三个答案。"""
    root: Path
    harness: Path
    backend_app: Path
    static_ui: Path | None

    @property
    def has_static_ui(self) -> bool:
        return self.static_ui is not None


def declaration_path(data_root: Path) -> Path:
    return Path(data_root) / DECLARATION


def read_declared(data_root: Path) -> str | None:
    """声明里写的那个目录（原样的字符串）；没声明 / 写坏了 = None。"""
    try:
        raw = json.loads(declaration_path(data_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    source_root = raw.get("source_root") if isinstance(raw, dict) else None
    return source_root.strip() if isinstance(source_root, str) and source_root.strip() else None


def write_declared(data_root: Path, source_root: str | None) -> None:
    """写声明；`None` = 撤掉。原子写，和载荷指针同一套讲究。"""
    path = declaration_path(data_root)
    if source_root is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"source_root": source_root}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def inspect(source_root: str | os.PathLike[str]) -> SourceCheckout:
    """这个目录是不是一个能跑的 harness-framework checkout。不是就说清差哪一样。

    判据和 launcher 找 harness / 找后端用的是同一条（`core/agent_loop.py`、
    `platform/backend/app/launcher.py`）—— 这里不另立一份「长什么样才算」。
    """
    root = Path(source_root).expanduser()
    if not root.is_absolute():
        raise DeveloperSourceError(f"源码目录要写绝对路径：{root}",
                                   en=f"The source directory must be an absolute path: {root}")
    if not root.is_dir():
        raise DeveloperSourceError(f"目录不存在：{root}", en=f"Directory does not exist: {root}")
    root = root.resolve()
    harness_marker = root / "core" / "agent_loop.py"
    if not harness_marker.is_file():
        raise DeveloperSourceError(
            f"{root} 不是 harness-framework 的源码目录（没有 core/agent_loop.py）",
            en=f"{root} is not a harness-framework checkout (no core/agent_loop.py)")
    backend_app = root / "platform" / "backend" / "app"
    if not (backend_app / "launcher.py").is_file():
        raise DeveloperSourceError(
            f"{root} 里没有后端（platform/backend/app/launcher.py）",
            en=f"{root} has no backend in it (platform/backend/app/launcher.py)")
    static_ui = root / "platform" / "frontend" / "out"
    return SourceCheckout(root=root, harness=root, backend_app=backend_app,
                          static_ui=static_ui if (static_ui / "index.html").is_file() else None)


def the_declared_checkout(data_root: Path) -> SourceCheckout | None:
    """launcher 问的那一句：声明了、且目录合格，就是它；否则 None（启动照常走随包 / 载荷）。

    声明了却不合格（目录被删、改名）时**不拦启动**：那会让一台机器因为一个开发者
    设置起不来。问题由 `describe` 报到关于页上。
    """
    declared = read_declared(data_root)
    if declared is None:
        return None
    try:
        return inspect(declared)
    except DeveloperSourceError:
        return None


def git_head(root: Path) -> dict[str, str] | None:
    """分支与短 sha —— 关于页上「正在跑的是哪一份」的那一行。读不到就 None，不拦任何事。"""
    try:
        def ask(*args: str) -> str:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=5, check=True).stdout.strip()
        return {"branch": ask("rev-parse", "--abbrev-ref", "HEAD"), "commit": ask("rev-parse", "--short", "HEAD")}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def describe(data_root: Path, running_app: Path, lang: str, *, started_by_developer_mode: bool) -> dict:
    """关于页要的全部：声明了什么、合不合格、现在跑的是不是它、重启后会变成什么。

    - `declared`：声明里的目录（没声明 = None）。
    - `active`：现在这个进程的 `app/` 就住在声明的 checkout 里 —— 也就是开发者模式真的在跑。
    - `restart_needed`：声明和正在跑的对不上（刚设、刚撤、改了目录）—— 重启才生效。
    - `running_from`：这个进程的 `app/` 住在哪个 checkout 里；不在任何 checkout 里（随包 / 载荷）= None。
    - `problem`：声明了但目录不合格时的那一句（这一屏的语言）。

    `started_by_developer_mode`：这个进程是不是随包 launcher 按声明接过来的（`launcher.RUNNING_DECLARED_CHECKOUT`）。
    没声明（或声明失效）时「要不要重启」只看它：是 → 撤了还没重启；不是 → 一个在终端里从源码起的
    后端、或者测试进程，本来就不归开发者模式管，不该被要求重启。
    """
    declared = read_declared(data_root)
    running_in_a_checkout = _checkout_around(running_app)
    out: dict = {"declared": declared, "active": False, "restart_needed": False, "problem": None,
                 "harness": None, "backend_app": None, "static_ui": None, "git": None,
                 # 这个进程现在是从哪个源码目录跑的（没在任何 checkout 里 = None）：
                 # 撤了声明之后那一句「重启后才回到安装的版本」要指名道姓。
                 "running_from": str(running_in_a_checkout) if running_in_a_checkout else None}
    if declared is None:
        # 没声明；进程却是开发者模式接过来的（撤了声明还没重启）—— 重启才回到随包那份。
        out["restart_needed"] = started_by_developer_mode
        return out
    try:
        checkout = inspect(declared)
    except DeveloperSourceError as exc:
        out["problem"] = exc.said_in(lang)
        out["restart_needed"] = started_by_developer_mode
        return out
    out.update({"harness": str(checkout.harness), "backend_app": str(checkout.backend_app),
                "static_ui": str(checkout.static_ui) if checkout.static_ui else None,
                "git": git_head(checkout.root)})
    out["active"] = Path(running_app).resolve() == checkout.backend_app.resolve()
    out["restart_needed"] = not out["active"]
    return out


def _checkout_around(running_app: Path) -> Path | None:
    """这个 `app/` 住在哪个 checkout 里（`<root>/platform/backend/app`）—— 不在任何 checkout 里就 None。"""
    app = Path(running_app).resolve()
    root = app.parent.parent.parent
    if app.name == "app" and app.parent.name == "backend" and app.parent.parent.name == "platform" \
            and (root / "core" / "agent_loop.py").is_file():
        return root
    return None


__all__ = ["DECLARATION", "DeveloperSourceError", "SourceCheckout", "declaration_path", "describe",
           "git_head", "inspect", "read_declared", "the_declared_checkout", "write_declared"]

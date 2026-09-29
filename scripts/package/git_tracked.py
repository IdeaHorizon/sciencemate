"""随包走的源码 = **git 跟踪着的文件**。三个打包器（服务器包、Mac、Windows）共用这一条规则。

从前每个打包器各自 `shutil.copytree` 整棵树、再按名字排除（`ignore_patterns`），于是工作树里
有什么，包里就有什么：

* 在开发机上（2026-09-23 实测主克隆）：服务器包会带上 7 个测试库（`test*.db`）、
  `platform/backend/data/` 下 1403 个文件（本机项目的 git 仓库）、`.pytest_cache`、
  `.ruff_cache`、`.DS_Store`；桌面包的 harness 会带上 `nodes/`、`shared/` 下 9 个 `.DS_Store`。
* CI 上（PR#1139）：别的 xdist worker 的 `test-gw0.db-journal` 在 copytree 列完目录、还没拷到
  它的那一刻消失，`shutil.Error: No such file or directory`。

黑名单的失败形状永远一样：下一种杂物默认漏过。所以名单改成问 git。

口径是 **tracked**，不是扫盘闸（`tests/repository_sources.py`）用的 tracked ∪（untracked −
ignored）：扫盘闸要看见还没 `git add` 的新文件；发出去的包不该带任何一个没进过提交的文件 ——
而 `test-gw0.db-journal` 正是「没被 ignore 的 untracked」，那个口径会把它放进来。两个问题，
两个口径，别为了"复用"合成一个。

拷的是**工作树里的内容**（未提交的修改照样进包，和从前一样），只是**名单**归 git。
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

#: git 跟踪着、但不随包走的目录名。装机的人不跑测试。
#:
#: 只剩这一个名字，是因为「拷什么」已经换成问 git。从前的黑名单 —— 缓存、字节码、构建产物、
#: 打包器中途失败留下的 `app/static_ui`、`edition.json`…… —— 它们有一个共同点：都不在 git 里。
NOT_SHIPPED = frozenset({"tests"})


def _git(repo: Path, *args: str) -> str:
    """跑一条 git，按 **UTF-8** 解码它的输出。

    不用 `text=True`：那是按本机 locale 解码，而 git 吐出来的路径是 UTF-8 字节。Windows 构建机
    的 locale 是 cp1252（2026-09-24 实测），`nodes/literature/` 下三个中文名文件会被解成
    `2025�\\xad科院…` —— 不报错，只是名单里多出三个磁盘上不存在的路径，拷的那一刻才炸。
    """
    try:
        out = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"问不到 git（{exc}）—— 安装包只从 git 检出里打") from exc
    if out.returncode != 0:
        stderr = out.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"问不到 git：{stderr[:300]} —— 安装包只从 git 检出里打")
    return out.stdout.decode("utf-8")


def the_tracked_files(repo: Path, paths: Iterable[str]) -> list[str]:
    """`paths`（相对仓库根的目录或文件）底下 **git 跟踪着**、要随包走的文件，相对仓库根、排好序。

    去掉的只有两种：路径里有 `NOT_SHIPPED` 的目录名；跟踪着、但工作树里已经删掉的（删它的人的
    意思是"不要了"）。`repo` 必须就是一个 git 检出的根 —— 解压出来的源码、放在别的仓库里面的
    一份拷贝都拒绝，而不是退回整棵拷（那等于把「工作树里有什么就发什么」原样留着）。
    """
    paths = list(paths)
    if not paths:
        return []   # `git ls-files --` 不带路径 = 整个仓库，不是"什么都没有"
    top = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    if top.resolve() != repo.resolve():
        raise SystemExit(f"{repo} 不是一个 git 检出的根（git 说根在 {top}）—— "
                         "安装包只从 harness-framework 的 git 检出里打")
    tracked = set(_git(repo, "ls-files", "--cached", "-z", "--", *paths).split("\0"))
    deleted = set(_git(repo, "ls-files", "--deleted", "-z", "--", *paths).split("\0"))
    return sorted(name for name in tracked - deleted
                  if name and NOT_SHIPPED.isdisjoint(PurePosixPath(name).parts))


def copy_the_tracked_files(repo: Path, paths: Iterable[str], into: Path,
                           *, under: str = "") -> list[str]:
    """把 `the_tracked_files(repo, paths)` 拷进 `into`，返回拷了哪些（相对仓库根）。

    落点是文件相对 `under` 的路径（默认相对仓库根）：`paths=["core"]` 落在 `into/core/…`；
    `paths=["platform/backend/app"], under="platform/backend/app"` 落在 `into/…`。
    """
    names = the_tracked_files(repo, paths)
    for name in names:
        target = into / (PurePosixPath(name).relative_to(under) if under else name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / name, target)
    return names


def top_level_modules_reachable_from(staged: Path, repo: Path) -> set[str]:
    """从已经放进 `staged` 的源码里，推出还需要 `repo` 根上的哪些**顶层模块**（`import chat`）。

    仓库根上除了 worker 入口 `platform_runtime.py` 还有 `chat.py`，被它在函数体里 `import chat`
    用着。手写清单漏一个的代价是：装出来一切正常，直到用户发第一条消息，worker 在
    `No module named 'chat'` 上倒下 —— Mac 包 2026-09-06 这么倒过一次；组织服务器包从第一版起
    就这么倒着（2026-09-27 升级演练里第一次在装好的服务器上真发消息才看见），因为这一步当时
    只写进了两个桌面打包器、各一份。所以它住在这里，三个打包器调同一个。

    清单是**推**出来的：扫 import（函数体里的也算），名字对得上仓库根某个 .py 的就带上，带上之后
    再扫它自己，直到不再增加。第三方包（`import numpy`）不在仓库根上，不会被认成我们的模块；
    `conftest` 是 pytest 的，永远不带。
    """
    import ast

    candidates = {p.stem for p in repo.glob("*.py")} - {"conftest"}
    needed: set[str] = set()
    frontier = list(staged.rglob("*.py"))
    while frontier:
        source = frontier.pop()
        try:
            tree = ast.parse(source.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue  # 模板文件之类，不是这一步该管的
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        for name in found & candidates - needed:
            needed.add(name)
            frontier.append(repo / f"{name}.py")
    return needed


def carry_the_top_level_modules(repo: Path, staged: Path) -> list[str]:
    """把 `staged` 里的代码会 import 的仓库根模块拷到 `staged` 根上，返回它们的名字。

    已经在 `staged` 根上的（worker 入口本身）不再拷；推出来却不是 git 跟踪着的，拒绝打包 ——
    那是还没进过提交的文件，装出来的包会带着一个这个版本里不存在的模块。
    """
    modules = top_level_modules_reachable_from(staged, repo)
    wanted = [f"{name}.py" for name in sorted(modules) if not (staged / f"{name}.py").is_file()]
    untracked = set(wanted) - set(copy_the_tracked_files(repo, wanted, staged))
    if untracked:
        raise SystemExit(f"worker 会 import {sorted(untracked)}，可 git 没跟踪它们 —— 先提交再打包")
    return sorted(modules)

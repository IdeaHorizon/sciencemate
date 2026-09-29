"""原生后端（darwin / linux）共用的那几件事。

* **可写面**：``write_layers`` 交出一份声明，规则只有一条 —— **最具体的那条声明说了
  算**。有 deny 原语的机制按 ``WriteLayers.ordered``（深度升序）发射，后写的赢；
  ``.git`` 永远最后拒。旧的 broad → readonly → priority 三层次序只在"洞不嵌套"时才
  等价于这条规则，#899-A 实测它会把可写根内部声明的只读洞重新盖回可写。
* **纯放行清单的机制没有 deny 层**（Landlock）：它不能"放行 /tmp 再拒掉 /tmp 下的
  worktree"。这种机制别放行共享 scratch，改给命令一个**私有 scratch**
  （``private_scratch``）并把 TMPDIR 指过去；自己的根按 ``WriteLayers.own`` 放行，
  worktree 其余部分因为不在清单里而天然拒写。
* **scratch**：**私有**（每条命令一个）+ harness 自己的缓存子目录（跨命令持久）。共享
  ``/tmp`` 与整棵用户缓存**不再**进可写面 —— #872 实测那两条把别的 run 的账本和
  ``/tmp/hf-jobs/*/record.json`` 一并交了出去。
* **家**（``CommandSpec.home``）：墙拒掉了程序的真家，平台自己的程序就得**同时给它一个**。
  用户的程序（``host``，默认）看见用户真实的家、只读 —— elan / conda / juliaup 按家找
  自己的安装，换了家就找不到。平台自带、自成一体的程序（``own``：随包 tectonic 与它调的
  biber）住墙给的家（:func:`payload_home`）：干净、完整、按本平台原生词汇布置，缓存区
  持久。只给 TMPDIR 与 ``XDG_CACHE_HOME`` 是 POSIX 的词汇 —— Windows 程序经 Known Folder
  找 ``%LOCALAPPDATA%``，不看这两个变量：2026-09-23 一台干净 Windows 上 tectonic 每次
  5 秒 ``os error 5``（它的 formats 目录建在 ``%LOCALAPPDATA%``），开发机上因为那个目录
  早就存在而从来没露面。
* **env 脱敏**：宿主进程的环境原样给模型（这是「本机就是环境」的含义），但名字像凭据的
  变量一律不给 —— 判据与 ``core.secrets`` 同一条正则，不另抄一份。
* **NativeLaunch**：原生后端交回咽喉的东西。``cwd`` / ``env`` 由咽喉应用；``terminate``
  返回 False 表示后端这边没有可停的东西（让咽喉直接杀进程组，别等 5 秒 drain）；
  ``cleanup`` 收私有 scratch 与 cgroup scope。
"""

from __future__ import annotations

import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# 模块级而不现读 ``sys.platform``：测试要在任何宿主上模拟 Windows 这一支，又不能把
# ``sys.platform`` 整个改掉连累别的模块（与 ``shared.lib.shell._WINDOWS`` 同一做法）。
_WINDOWS = sys.platform == "win32"


def canonical(path: Path | str) -> Path | None:
    """真实路径（跟符号链接走）。seatbelt 按真实路径匹配：``/tmp`` 在 macOS 上是
    ``/private/tmp`` 的链接，规则写成 ``/tmp/...`` 一条都不会命中（真跑探到的）。"""
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None
    return candidate if candidate.exists() else None


def cache_root() -> Path:
    """宿主这个用户的缓存根，按本平台的原生约定。**不要求已经存在**。

    曾经要求存在（``canonical``），于是 Windows 上 —— ``~/.cache`` 默认没有 —— 持久缓存
    整块不存在，模型的每条命令都从零下载，而账上看不出差别。
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))


def cache_scratch() -> Path | None:
    """harness 自己的缓存子目录 —— 声明根之外唯一一块**持久**可写空间。

    这里曾经交出去的是整棵用户缓存目录（``~/.cache`` / ``~/Library/Caches``）。理由
    成立 —— 科学库拿不到缓存会反复报 "cache unwritable" 并退化到 tmp，噪音会训练模型
    忽略真正的拒写 —— 但代价是把**别的程序**的缓存一并交了出去。收窄到我们自己的子
    目录：程序在墙里看到的缓存区（:func:`payload_home`）就链到这里，跨命令、跨 run
    稳定；别人的缓存回到只读。
    """
    try:
        target = cache_root() / "harness-framework"
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return canonical(target)


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _depth(path: Path) -> int:
    return len(path.parts)


@dataclass(frozen=True)
class WriteLayers:
    """一条命令的可写面 —— **一份声明，两种发射方式**。

    模型只有一条："最具体的那条声明说了算"。``readonly_roots`` 里挖在可写根内部的洞
    是只读；可写根挖在只读根内部仍是可写；任意层数都成立。它有两种兑现方式：

    * **有 deny 原语的机制**（bwrap ``--ro-bind`` / seatbelt ``deny`` / win32 重打
      Medium 标签）：按 :attr:`ordered` 从浅到深发射，后写的赢 —— 深度序就是"最具体
      的赢"。2026-09-08 之前这里发的是 broad → readonly → priority：一个住在只读根
      里的可写根（绑定 Project 的 run 里这是**常态**）会把它内部声明的只读洞重新盖回
      可写（#899-A 实测 ``dependency_root`` 被写穿）。
    * **纯放行清单**（Landlock）：没有 deny 层，只能靠"不放行"表达只读。
      :meth:`allow_list` 用**补集覆盖**表达洞；超出规则预算时如实报告这条边界没兑现，
      不假装（#900-B1）。

    ``.git`` 是绝对否决，永远最后发。
    """

    own: tuple[Path, ...]
    """调用方声明的可写根（自己的节点目录、run-local），已规范化。"""
    scratch: tuple[Path, ...]
    """本条命令的私有 scratch + harness 自己的缓存子目录。

    **不含共享 /tmp**：共享 tmp 一挂就等于把别的 run 的状态目录和
    ``/tmp/hf-jobs/*/record.json`` 交出去 —— #872 实测一条只声明了自己临时目录的沙箱
    命令把作业事实账的 ``exit_code`` 从 138（被资源守卫杀死）改成了 0，而
    ``enforcement_record()`` 仍自报 ``write_boundary`` 已兑现。容器年代每个 attempt 有
    自己的 ``/tmp`` namespace，挂它没有跨 run 后果；容器一撤，共享 ``/tmp`` 就从
    "无所谓"变成了真洞。"""
    readonly: tuple[Path, ...]
    """调用方声明的拒写根 —— **全部**，包括嵌套在可写根内部的那些。"""
    git: tuple[Path, ...]
    """最后再拒：任何可写根下的 ``.git``（目录或 worktree 指针文件）。"""
    ordered: tuple[tuple[Path, str], ...]
    """(路径, ``"rw"`` | ``"ro"``)，按路径深度升序 —— 有 deny 原语的后端按这个次序发射。"""

    @property
    def writable(self) -> tuple[Path, ...]:
        return (*self.own, *self.scratch)

    def grants_write(self, path: Path | str) -> bool:
        """把这组根交给后端之后，``path`` 最终到底可不可写。

        这是 ``Invariant.READONLY_CARVEOUT`` 的**判定**，对外公开：节点不必再自己
        镜像一份后端的绑定次序去猜答案（#903 要决定的那份镜像因此可以不建）。它与
        :attr:`ordered` 同源 —— 判定和发射读同一份数据，不会各自演化。
        """
        candidate = Path(path)
        best_depth, best_mode = -1, None
        for root, mode in self.ordered:
            if _inside(candidate, root) and _depth(root) > best_depth:
                best_depth, best_mode = _depth(root), mode
        if best_mode != "rw":
            return False
        return not any(_inside(candidate, marker) for marker in self.git)

    def carve_outs(self) -> tuple[Path, ...]:
        """嵌套在某个可写根**内部**的只读根 —— 需要 deny 原语才能兑现的那些。"""
        writable = self.writable
        return tuple(
            root for root in self.readonly
            if any(_inside(root, w) and root != w for w in writable)
        )

    def allow_list(self, *, budget: int = 512) -> tuple[tuple[Path, ...], bool, str]:
        """纯放行清单机制能放行的根 → (清单, 洞是否兑现, 没兑现的原因)。

        **没有洞时返回的就是可写根本身**，与旧行为逐字节相同 —— 补集只在"本来就有洞、
        而且今天一定守不住"的场景里生效。有洞时走补集覆盖：放行从可写根到洞这条链上
        的兄弟条目，洞本身不进清单。两个代价（lujy 在 #900 里点名要人拍板的那两条）：

        * **枚举是快照**：补集在 prepare 时刻算，运行期在可写根下新建的目录不在清单
          里，会被拒。
        * **宽目录**：几百个条目的可写根会展开成几百条规则。所以有 ``budget``；超了
          **不**悄悄降级成"放行整个根还说守住了"，而是放行整个根并如实报告这条边界
          没兑现，由记账把它送到读账的人面前。

        ⚠️ **补集覆盖有一个逃不掉的代价**（2026-09-15 在 Linux CI 上被判据抓到）：
        放行的是"到洞这条链上的兄弟条目"，**链上的目录本身不在清单里**。于是
        在可写根里**直接新建**一个文件会被拒 —— 洞守住了，但根的"能新建"丢了。

        为什么不反过来（放行整个根、把洞报成没兑现）：那等于让声明为只读的数据
        可被改写，**削弱的正是这套机制要建的写边界**。宁可少一项能力，不可少一道墙。

        所以这一条不是缺陷、是这个后端上的真实代价，由 :meth:`chain_dirs` 交给
        调用方如实记账（linux.py 把它挂进 ``unmet``）。节点要在根里新建文件时，
        应当把可写根选在洞的**外面**，而不是指望这里。
        """
        holes = self.carve_outs()
        if not holes:
            return self.writable, True, ""
        allowed: list[Path] = []
        for root in self.writable:
            inner = [hole for hole in holes if _inside(hole, root) and hole != root]
            if not inner:
                allowed.append(root)
                continue
            if not _carve(root, inner, budget, allowed):
                return self.writable, False, (
                    f"a read-only carve-out under {root} needs more than {budget} "
                    "allow-rules to express on an allow-list backend"
                )
        return tuple(allowed), True, ""


    def chain_dirs(self) -> tuple[Path, ...]:
        """因为挖了洞而**自己进不了放行清单**的可写根（见 :meth:`allow_list`）。

        它们下面原有的条目仍然可写，但**不能在里面新建**。这是补集覆盖在纯放行
        清单后端上的真实代价，必须被记账送到读账的人面前 —— 否则节点只会看到
        一个没有理由的 `FileNotFoundError`。
        """
        holes = self.carve_outs()
        if not holes:
            return ()
        return tuple(
            root for root in self.writable
            if any(_inside(hole, root) and hole != root for hole in holes)
        )


def _carve(root: Path, holes: Sequence[Path], budget: int, out: list[Path]) -> bool:
    """放行 ``root`` 但挖掉 ``holes``：逐层枚举兄弟条目。超预算返回 False。"""
    if any(root == hole for hole in holes):
        return True  # root 自己就是洞：整个不放行
    inner = [hole for hole in holes if _inside(hole, root) and hole != root]
    if not inner:
        out.append(root)
        return len(out) <= budget
    try:
        children = sorted(root.iterdir())
    except OSError:
        return False
    for child in children:
        if not _carve(child, inner, budget, out):
            return False
    return len(out) <= budget


def write_layers(
    writable_roots: Sequence[Path | str],
    readonly_roots: Sequence[Path | str],
    *,
    scratch_dir: Path | None = None,
) -> WriteLayers:
    """把调用方的两组根整理成一份可发射、也可判定的可写面。

    ``scratch_dir`` 是**本条命令的私有 tmp**（:func:`private_scratch`）。每个后端都要
    给一个 —— 共享 ``/tmp`` 不再进可写面（#872）。
    """
    readonly: list[Path] = []
    for raw in readonly_roots:
        resolved = canonical(raw)
        if resolved is not None and resolved not in readonly:
            readonly.append(resolved)
    own: list[Path] = []
    for raw in writable_roots:
        resolved = canonical(raw)
        if resolved is not None and resolved not in own:
            own.append(resolved)
    scratch: list[Path] = []
    for candidate in (scratch_dir, cache_scratch()):
        resolved = canonical(candidate) if candidate is not None else None
        if resolved is None or resolved in own or resolved in scratch:
            continue
        # 别因为放行缓存而顺带放行住在缓存目录下的 worktree。这条守卫此前只在
        # Landlock / win32 两个分支里有，bwrap / seatbelt 没有 —— 现在所有后端一致。
        if any(_inside(r, resolved) for r in readonly):
            continue
        scratch.append(resolved)
    modes: dict[Path, str] = {}
    for path in readonly:
        modes[path] = "ro"
    for path in (*own, *scratch):
        # 同一条路径两种声明：显式可写赢（旧 priority 层就是这个语义）。
        modes[path] = "rw"
    ordered = tuple(sorted(modes.items(), key=lambda item: (_depth(item[0]), str(item[0]))))
    git: list[Path] = []
    for root in (*own, *scratch):
        marker = root / ".git"
        if marker.exists() and marker not in git:
            git.append(marker)
    return WriteLayers(tuple(own), tuple(scratch), tuple(readonly), tuple(git), ordered)


def enclosing_git_paths(root: Path) -> tuple[Path, ...]:
    """``root`` 所在仓库里"改了就改写历史"的那几条路径：gitdir、common dir、以及工作树
    顶层的 ``.git``（目录或 worktree 指针文件）。``root`` 不在任何仓库里 → 空。

    只认 ``root`` **所在**的那个仓库：GIT_UNWRITABLE 承诺的是"这条 run 改不了自己项目的
    历史"，可写根是这个项目工作树里的目录。可写根之下另有别的仓库不是这条不变量的事
    （那是 WRITE_BOUNDARY 的事，而它已经把可写面收到了这几个根）。
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--absolute-git-dir", "--git-common-dir",
             "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if out.returncode != 0:
        return ()
    lines = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    if len(lines) != 3:
        return ()
    gitdir, common, top = (Path(line) for line in lines)
    found: list[Path] = []
    for candidate in (gitdir, common if common.is_absolute() else top / common, top / ".git"):
        resolved = canonical(candidate)
        if resolved is not None and resolved not in found:
            found.append(resolved)
    return tuple(found)


def git_paths_inside_writable(own: Sequence[Path]) -> tuple[tuple[Path, Path], ...]:
    """(可写根, 它里面的 git 路径) —— 非空即"守不住 .git"。

    这是 GIT_UNWRITABLE 的**结构判据**：判据挂在"可写根含不含 gitdir / 指针文件"这个
    结构事实上，不挂在"我们相信调用方会传节点目录"上。哪天有人把工作树根当可写根传
    进来（manifest 的 rw 挂载就是个口子），这里就会亮，而不是让一份"守到了"的账悄悄
    变成假的。纯放行清单（Landlock）没有 deny 层，只能靠它；bwrap 有 ``--ro-bind``
    压住 ``.git``，不需要它。
    """
    roots = [p for p in (canonical(r) for r in own) if p is not None]
    hits: list[tuple[Path, Path]] = []
    for root in roots:
        for git_path in enclosing_git_paths(root):
            if any(_inside(git_path, writable) for writable in roots):
                hits.append((root, git_path))
    return tuple(hits)


def private_scratch() -> Path:
    """给一条命令一块只属于它的地方：``tmp``（替代共享 /tmp），``home="own"`` 时还有它的家
    （:func:`payload_home`）。命令结束整块删掉。"""
    return Path(tempfile.mkdtemp(prefix="hf-scratch-")).resolve()


_USER_SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"


def _windows_profile_folders() -> tuple[str, ...]:
    """这台机器上跟着 ``%USERPROFILE%`` 走的那些 Known Folder（相对家的路径）。

    Known Folder 的路径由这张注册表里的 ``%USERPROFILE%\\...`` 按**进程自己的环境**展开
    （真机实测：Low-IL 载荷里把 ``USERPROFILE`` 换掉，``LocalAppData`` / ``RoamingAppData``
    / ``Documents`` 都跟着换；目录不存在时系统接口返回 ``0x80070002``，tectonic 就报
    "App data directories not supported"）。所以家里要把它们建出来 —— 名单不手抄，读
    Explorer 自己用的这张表：系统加了新文件夹、用户把「文档」重定向到 OneDrive（那一条
    就不再以 ``%USERPROFILE%`` 开头，不跟着走），这里都如实跟着变。

    ``AppData\\Local`` 底下的不建：那一整块是链到持久缓存的（见 :func:`payload_home`）。
    """
    found = {"AppData\\Roaming"}
    prefix = "%USERPROFILE%\\"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _USER_SHELL_FOLDERS) as key:
            index = 0
            while True:
                try:
                    _name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if not isinstance(value, str) or not value.upper().startswith(prefix):
                    continue
                relative = value[len(prefix):].strip("\\")
                lowered = relative.lower()
                if lowered == "appdata\\local" or lowered.startswith("appdata\\local\\"):
                    continue
                if relative:
                    found.add(relative)
    except OSError:
        pass
    return tuple(sorted(found))


def _link_dir(link: Path, target: Path | None) -> None:
    """让 ``link`` 这个目录就是 ``target``。没有 target（持久缓存建不出来）就是个普通目录。

    Windows 用目录联接：免管理员（符号链接要开发者模式）。命令结束时整块 scratch 被
    ``shutil.rmtree`` 收掉 —— 它删的是联接 / 符号链接**本身**，不进目标（Windows 上真机
    验过：目标里的文件原样在）。
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    if target is not None:
        try:
            if sys.platform == "win32":
                import _winapi

                _winapi.CreateJunction(str(target), str(link))
            else:
                link.symlink_to(target, target_is_directory=True)
            return
        except OSError:
            # 链不上 = 这条命令的缓存不跨命令保留：慢，不是错。照样给一个可写目录。
            pass
    link.mkdir(exist_ok=True)


def payload_home(scratch_dir: Path) -> dict[str, str]:
    """墙给这条命令的家 —— 按本平台的原生词汇整套给出，返回要设的环境变量。

    布局（都在私有 scratch 里，命令结束一起删）::

        <scratch>/tmp     TMPDIR / TMP / TEMP
        <scratch>/home    HOME（Windows 另有 USERPROFILE / HOMEDRIVE / HOMEPATH）
            缓存区 ──→ :func:`cache_scratch`（持久、跨命令）
              POSIX   ~/.cache            = XDG_CACHE_HOME
              macOS   ~/Library/Caches    = XDG_CACHE_HOME
              Windows ~\\AppData\\Local     = LOCALAPPDATA = XDG_CACHE_HOME
            其余（配置、数据、Roaming、「文档」……）随命令结束删掉

    为什么缓存区持久、其余不持久：缓存丢了只是慢（tectonic 首编 82 秒，之后 0.4 秒），
    值得跨命令留；配置与数据跨命令、跨项目留着，就是一条让这次的代码给下一次的代码
    下钩子的通道（Python 用户 site 里放个 ``.pth``，之后每个项目的每条 python 都执行
    它）。持久缓存早已是跨 run 共享的那一块（#872），这里没有新增共享面。

    只给 ``CommandSpec.home == "own"`` 的程序（平台自带、自成一体的工具）。用户的程序
    （``host``）要看见用户真实的家：elan 按 ``~/.elan`` 找 Lean，换了家它就找不到工具链。
    """
    home = scratch_dir / "home"
    tmp = scratch_dir / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    cache = cache_scratch()
    env: dict[str, Path | str] = {"HOME": home, "TMPDIR": tmp, "TMP": tmp, "TEMP": tmp}
    if sys.platform == "win32":
        for relative in _windows_profile_folders():
            (home / relative).mkdir(parents=True, exist_ok=True)
        local = home / "AppData" / "Local"
        _link_dir(local, cache)
        env.update({
            "USERPROFILE": home,
            "HOMEDRIVE": home.drive,
            "HOMEPATH": str(home)[len(home.drive):],
            "APPDATA": home / "AppData" / "Roaming",
            "LOCALAPPDATA": local,
            "XDG_CACHE_HOME": local,
        })
    else:
        caches = home / "Library" / "Caches" if sys.platform == "darwin" else home / ".cache"
        _link_dir(caches, cache)
        env.update({
            "XDG_CACHE_HOME": caches,
            "XDG_CONFIG_HOME": home / ".config",
            "XDG_DATA_HOME": home / ".local" / "share",
            "XDG_STATE_HOME": home / ".local" / "state",
        })
    return {name: str(value) for name, value in env.items()}


def python3_is_python(bin_dir: Path) -> None:
    """Windows：让解释器目录里的 ``python3.exe`` 就是它旁边那个 ``python.exe``。

    :func:`payload_environment` 把解释器目录放在 PATH 最前面，默认了一件事：那个目录里
    有叫 ``python3`` 的东西。POSIX 上每一种布局都成立（venv 的 ``bin/python3``、独立
    CPython 的 ``bin/python3``）；Windows 上**没有一种**成立 —— venv、python.org 安装包、
    uv 的独立 CPython 都只有 ``python.exe``。于是 ``python3`` 一路找到
    ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python3.exe``：应用商店的桩。2026-09-23 真机
    实测（非管理员 Windows 11）：模型 shell（MSYS bash）里敲 ``python3 -c ...`` **一个字
    都不打印、退出码 49** —— 连"找不到"都不说。

    补上的就是 POSIX venv 里 ``python3 -> python`` 那条链：同一个目录、同一个文件（硬链接）。
    必须在**同一个目录**：venv 的 ``python.exe`` 是启动器，按自己位置找 ``..\\pyvenv.cfg``；
    独立 CPython 的 ``python.exe`` 按自己位置找 ``python3XX.dll`` 与标准库 —— 挪到别处都
    起不来。真机验过：硬链接后 ``python3`` 的 ``sys.prefix`` 就是 venv 本身（依赖都在），
    bash 里 ``python3`` 与 ``/usr/bin/env python3``（shebang 的写法）都解析到它。

    **随包应用不靠这里**：解释器目录就是安装目录，装在 ``C:\\Program Files`` 下时普通
    权限写不进。所以打包器在打包时就用这个函数把名字放好（``build_windows_app.
    give_python_its_python3_name``），装完即在；这里遇到它逐字节相同就不写。运行时这段
    补的是开发环境（venv、uv 的独立 CPython）。

    幂等：已经是同一个文件、或逐字节相同的拷贝，就不动（每条命令都会走到这里）。旧的
    （解释器换过版本）原子替换。文件系统不支持硬链接（exFAT 的 U 盘 —— 便携目录允许
    整个拷走）退回拷贝。写不进就不改，命令照样起（不因为补不上一个名字把整条命令拒掉）。
    """
    source = bin_dir / "python.exe"
    alias = bin_dir / "python3.exe"
    if not source.is_file():
        return
    try:
        if filecmp.cmp(alias, source, shallow=True):
            return
    except OSError:
        pass  # 还没有这个名字
    staged = bin_dir / f"python3.exe.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        try:
            os.link(source, staged)
        except OSError:
            shutil.copy2(source, staged)
        os.replace(staged, alias)
    except OSError:
        pass
    finally:
        staged.unlink(missing_ok=True)


def payload_environment(
    extra: Mapping[str, str] | None,
    *,
    scratch_dir: Path,
    home: str = "host",
) -> dict[str, str]:
    """宿主环境减去凭据，再按 ``home``（``CommandSpec.home``）安排程序住哪，最后叠调用方显式给的。

    * ``host``：用户真实的家照旧可见；私有 tmp + 持久缓存（``XDG_CACHE_HOME``）。
    * ``own``：墙给的家（:func:`payload_home`），整套原生词汇都换掉。

    凭据判据与 core.secrets 同源。调用方（``CommandSpec.environment``）显式设的变量
    最后叠上去、它说了算 —— 那是它自己的决定和后果。
    """
    from core.secrets import is_secret_name

    env = {name: value for name, value in os.environ.items() if not is_secret_name(name)}
    # 模型在这个 shell 里敲 `python3`，指的必须是**带着这个项目依赖的那一个**
    # —— 也就是跑着 harness 的这一个。virtualenv 就是这么工作的：把自己的
    # bin 放在 PATH 最前面，于是 `python3` 有你装的那些包。
    #
    # 2026-09-07 真机实测：不这么做时，`.app` 里模型敲 `python3 -c "import numpy"`
    # 拿到的是系统 python（Xcode 3.9，没有 numpy），然后它开始满硬盘找：
    #
    #     find ~/.harness-framework -maxdepth 6 -name "python*" | head -40
    #     === find python with numpy ===
    #
    # 这不是模型笨，是我们把它扔进了一个 `python3` 不是项目解释器的 shell。
    # 同一条链上更下面那两处（框架自己 spawn、节点写死绝对路径）已经在 #827
    # 收成一个答案；这一处是**模型自己敲的命令**，改不了它敲什么，只能让那个
    # 名字在这个 shell 里指对东西。
    #
    # 是 ``sys.executable`` **原样**所在的目录，不跟符号链接走：POSIX venv 的
    # ``bin/python`` 就是指向基础解释器的链接，venv 靠"从哪个目录被起"找到
    # ``pyvenv.cfg``。这里曾经 ``.resolve()``，一跟就走出了 venv —— 2026-09-23 本机
    # 实测（uv 建的 .venv）：模型敲的 ``python3`` 的 ``sys.prefix`` 是 uv 那份 CPython，
    # ``import numpy`` 直接 ModuleNotFoundError。与 ``the_interpreter_for_model_code()``
    # （原样的 ``sys.executable``）是同一个答案。取不到（空串 / None）就不加：
    # ``Path("").parent`` 是 ``.``，放在最前面等于让工作目录里的文件顶替命令。
    if sys.executable:
        interpreter_bin = Path(sys.executable).parent
        if _WINDOWS:
            python3_is_python(interpreter_bin)
        env["PATH"] = os.pathsep.join(
            [str(interpreter_bin), *(p for p in env.get("PATH", "").split(os.pathsep) if p)]
        )
    if home == "own":
        env.update(payload_home(scratch_dir))
    else:
        tmp = scratch_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TMP", "TEMP"):
            env[name] = str(tmp)
        # 缓存收窄到 harness 自己的子目录之后，得把工具指过去 —— 否则 uv/pip 仍去写
        # ``~/.cache/uv``（只读）并把 "cache unwritable" 刷成噪音（#872）。
        cache = cache_scratch()
        if cache is not None:
            env["XDG_CACHE_HOME"] = str(cache)
    for name, value in (extra or {}).items():
        env[str(name)] = str(value)
    # Windows：模型在这个 shell 里敲的 python 也起在 UTF-8 模式 —— 它写中文文件（数据、
    # 说明）不该 cp1252 崩或乱码。一处回答（platform_env），POSIX 空 dict、无副作用。
    from shared.lib.platform_env import utf8_mode_env

    env.update(utf8_mode_env())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


_EXEC_SHIM = Path(__file__).with_name("_exec.py")


def exec_shim_argv() -> list[str]:
    """链的最内层：``python -I -B _exec.py --`` —— 载荷起不起得来，由它说（见 ``_exec.py``）。

    bwrap / seatbelt 自己 ``execvp`` 载荷失败只会以普通非零码退出（1 / 71）+ 一行自己方言
    的 stderr，咽喉分不出"没起来"和"跑完了返回非零"；Landlock 启动器自己 exec、自己说，
    不需要这一层。代价是每条命令多一次解释器启动（本机实测 ~18ms）。
    """
    return [sys.executable, "-I", "-B", str(_EXEC_SHIM), "--"]


def run_ok(argv: Sequence[str], *, timeout: float = 10.0) -> bool:
    try:
        return subprocess.run(
            list(argv), capture_output=True, timeout=timeout, check=False
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@dataclass
class NativeLaunch:
    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    unit: str | None = None
    """linux：systemd scope 名。有它，terminate 就 ``systemctl --user stop`` 整个 cgroup。"""
    stop_argv: list[str] = field(default_factory=list)
    scratch_dir: Path | None = None
    """本条命令的私有 scratch，cleanup 时整目录删掉。每个后端都给，不再挂共享 /tmp。"""
    unmet: tuple[tuple[str, str], ...] = ()
    """**这一条命令**没兑现的不变量：((invariant, 原因), ...)。

    ``Backend.capabilities()`` 答的是"这台机器能守到哪几条"，是每后端一份；有些边界
    却是**逐条命令**才知道守没守住 —— 典型是"可写根内部的只读洞"在纯放行清单后端上
    超了规则预算（#900-B1）。那种时候不能沉默，也不能改 capabilities 去骗全局记账：
    挂在这条 launch 上，由咽喉写进 transcript（``isolation_gap``）。"""

    def terminate(self, *, remove: bool = False) -> bool:
        del remove
        if not self.stop_argv:
            return False  # 没有后端侧可停的东西 → 咽喉直接杀进程组
        return run_ok(self.stop_argv, timeout=10.0)

    def cleanup(self) -> None:
        if self.stop_argv:
            run_ok(self.stop_argv, timeout=10.0)
        if self.scratch_dir is not None:
            shutil.rmtree(self.scratch_dir, ignore_errors=True)

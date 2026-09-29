"""Project-worktree observation shared by every Harness node.

Nodes never run Git commands and do not decide commit boundaries.  The
Platform lends the runtime one Session worktree.  Every scientific node has a
stable top-level directory in that worktree: downstream nodes read upstream
directories directly, while writes remain confined to the current owner's
directory.  The Platform remains the only component allowed to create commits.

## 写边界的分工（2026-08-13 重构）

- **墙** = 进程沙箱（core/sandbox.py）：模型的 shell / python 子进程在
  spawn 那一刻被钉住可写面，越界写在写的那一行拿到 Permission denied。
- **权威** = 提交闸：dangerous_commands 硬拒改写历史的 git；平台 checkpoint
  用自己 DB 记的 head fail-closed 校验（project_repository）。
- **见证** = 本模块的 capture/enforce：每次工具调用后对照 `git status`，
  发现越界写只**报告**（transcript 事件 + 工具结果附注），不回退、不隔离、
  不删除 —— 本模块没有任何销毁能力。

此前这里是一套"拍快照 → 比对 → 推断作者 → 撤销"的事后取证机器（chmod 墙 /
字节基线 / 调用级+run 级双作用域 / 隔离区 / 归因分类），推断错过两回，
代价都是真数据（2026-08-11 六份 LAMMPS 日志 + 一份评审意见；2026-08-13
orchestrator 自己的三份产物）。根因是它判"文件系统上出现了什么"而不是
"谁写的" —— 后者在 spawn 那一刻是构造事实，事后只能猜。整套已删除。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

from core.tool_errors import ToolRejection as _ToolRejection

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
#: 工作区当前内容的指纹 = 影子 tree 的 OID。progress_breaker.durable_signature
#: 拿它判"这一轮世界变没变"，所以它必须是内容寻址的、且每次观测都刷新。
FINGERPRINT_KEY = "_project_workspace_fingerprint"
#: 上一次观测时的影子 tree —— "这一次工具调用改了什么"的比较基准。
_BASELINE_TREE_KEY = "_project_workspace_baseline_tree"
_GUARD_BASELINE_KEY = "_project_workspace_guard_baseline"
#: 超过这个大小的文件不进影子 tree（进去就等于每次工具调用往对象库塞一份
#: 副本；一份每步都在长的模拟日志能把仓库撑爆）。它们只记身份不记内容。
_MAX_OBSERVED_BLOB_BYTES = 5_000_000
#: 一次观测送进 transcript 的 diff 上限。单文件先截，总量再截，都留标记。
_MAX_FILE_PATCH_BYTES = 16_000
_MAX_PATCH_BYTES = 64_000
#: 节点 → 它在 Project 工作区里拥有的写作用域。**唯一真相源**：CLI 建仓
#: （core/project_bootstrap）、平台建仓与 checkpoint 闸（platform/backend
#: project_repository 的镜像 + 契约测试）、产物落点（core/paths）、跨节点读
#: （state._artifact_search_dirs）全部从这里推，不各写一份。
#:
#: 调度器的作用域是 `notes/` —— 用户看得见的研究记录目录，装它写给用户的
#: 笔记、结论、快速出的图、编译出的 PDF。它**没有私人抽屉**：草稿和试算落
#: run 自己的目录（见 `working_directory`），给用户看过的东西必须已经在记录里。
#: 2026-09-10 之前它的作用域是 `.research/orchestration/`：真项目里躺着
#: `run_scan_E2.py`、`ising_results.json`、`test_ws.txt`，还有一次它把论文 PDF
#: 编译在那里，用户在文件树里找不到（那个目录整片被折成"平台记账"）。
_NODE_WORKSPACES = {
    "literature": "literature",
    # 目录按「它是研究里的什么」命名，不按节点名（2026-09-12 改名：
    # hypothesis→plan、experiment→experiments、postprocess→figures、writing→paper）。
    # 键仍是节点类型 —— 节点是谁不变，变的只是它的东西在文件夹里叫什么。
    "hypothesis": "plan",
    "data": "data",
    "experiment": "experiments",
    "observation": "observation",
    "derivation": "derivation",
    "postprocess": "figures",
    "writing": "paper",
    "_reviewer": "reviews",
    "reviewer": "reviews",
    "_orchestrator": "notes",
    "orchestrator": "notes",
    "_curator": "MEMORY.md",
    "memory_curator": "MEMORY.md",
}

#: 相对路径锚在 run 目录 `scratch/` 而不是自己作用域的节点。
#:
#: 调度器一边跟用户对话一边试算：写个脚本、算个表、画张草图。这些是草稿，
#: 不是研究记录 —— 落进 `notes/` 就是把 `test_ws.txt` 摆到用户面前。所以它
#: 键入的相对路径落 `<run>/scratch/`（随 run 生灭，gitignore）；要进记录的
#: 东西它得**指名**：`project/notes/<文件>`、`save_artifact`、或 compile_latex
#: 的产物（产物目录锚在 `notes/`，见 core/paths._node_anchor）。
#: 呈现闸（core/loop_hooks_builtin `presentation_gate`）保证摆进对话的路径
#: 都在记录里。
_SCRATCH_ANCHORED = frozenset({"_orchestrator", "orchestrator"})


def producing_node_dirs() -> tuple[str, ...]:
    """producing 节点的目录（去重、保持表序）—— 建仓与"上游目录可读"提示都用它。"""
    out: list[str] = []
    for node_type, owned in _NODE_WORKSPACES.items():
        if node_type.startswith("_") or node_type in _SYSTEM_NODE_ALIASES:
            continue
        if owned not in out:
            out.append(owned)
    return tuple(out)


def system_node_dirs() -> tuple[str, ...]:
    """架构节点（reviewer / orchestrator）的目录；文件作用域的（curator）不算。"""
    out: list[str] = []
    for node_type in _SYSTEM_NODE_ALIASES:
        owned = _NODE_WORKSPACES[node_type]
        if not Path(owned).suffix and owned not in out:
            out.append(owned)
    return tuple(out)


#: 架构节点在表里不带下划线的别名 —— 它们不是 producing 节点。
_SYSTEM_NODE_ALIASES = ("reviewer", "orchestrator", "memory_curator")


def owning_node_for_path(worktree, path) -> str | None:
    """这条路径归哪个节点所有 —— 反查 `_NODE_WORKSPACES`。

    有了它，"你越界了"就能变成"这是 X 的东西，去让 X 做"。只说不许而不说该
    找谁，调用方唯一能做的就是重试（E2E v25 实测：orchestrator 撞了 3 次）。
    """
    from pathlib import Path as _Path

    try:
        relative = _Path(path).resolve().relative_to(_Path(worktree).resolve())
    except (ValueError, OSError):
        return None
    parts = relative.parts
    if not parts:
        return None
    for node_type, owned in _NODE_WORKSPACES.items():
        if node_type.startswith("_"):
            continue  # 同一目录有带下划线与不带的两个别名，返回可读的那个
        owned_parts = _Path(owned).parts
        if parts[: len(owned_parts)] == owned_parts:
            return node_type
    return None


def validate_deliverable_projection(project_root, node_type: str, rel_path: str) -> str:
    """用户点名的交付文件可以落在哪 —— run_node（派发时）与 save_artifact
    （落盘时）共用的**同一个**判据；两端各拿一份就会各自演化。

    合法落点 = 产出节点自己的作用域 ∪ 无主之地。其余各给各的指路。
    返回规范化的 posix 相对路径；不合法抛 ProjectWorkspaceError。
    """
    from pathlib import Path as _Path

    from shared.lib import dangerous_commands as _danger

    raw = str(rel_path or "").strip()
    rel = _Path(raw)
    # Path("") 归一成 Path(".")——空值要在进 Path 之前判，否则从这条缝溜过去。
    if not raw or str(rel) == "." or rel.is_absolute() or ".." in rel.parts:
        raise ProjectWorkspaceError(
            f"deliverable.path 必须是 Project 内的相对路径：{rel_path!r}"
        )
    posix = rel.as_posix()
    protected = _danger.match_protected_file_path(posix)
    if protected is not None:
        raise ProjectWorkspaceError(
            f"deliverable.path 落在框架状态（{protected}）上：{posix!r}。"
            f"交付投影写的是**用户可读的文件**，账本身份由 save_artifact 自己管。"
        )
    if posix.startswith(".research/"):
        raise ProjectWorkspaceError(
            f"deliverable.path 不能进 .research/（框架状态）：{posix!r}"
        )
    own = _NODE_WORKSPACES.get(str(node_type))
    owner = owning_node_for_path(project_root, _Path(project_root) / rel)
    if owner is not None and own is not None and not posix.startswith(str(own)):
        raise ProjectWorkspaceError(
            f"deliverable.path {posix!r} 在 {owner} 的作用域里，而产出方是 "
            f"{node_type}。要么放产出方自己的目录，要么放项目根（无主之地）。"
        )
    return posix


class ProjectWorkspaceError(RuntimeError, _ToolRejection):
    """A Platform-provided workspace is not a safe Git worktree.

    这是**故意的拒绝**（路径越界、工作树不安全），不是代码崩了 —— 继承
    ToolRejection，dispatch 才会把它记成 rejected 而不是 tool_exception。
    实测本机 1038 条工具失败里，15 条 `ProjectWorkspaceError: Path escaped
    the Project boundary` 一直被当成异常展示，读起来像平台出故障。
    """


def _segment(value: str) -> str:
    clean = _SAFE_SEGMENT.sub("-", str(value)).strip("-.")
    return (clean or "run")[:128]


def _git(
    root: Path,
    *args: str,
    check: bool = True,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise ProjectWorkspaceError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _git_status(root: Path, *args: str) -> tuple[int, str]:
    """需要看**退出码**的 git 调用（`merge-base --is-ancestor` 靠退出码表态）。

    `_git(check=False)` 只给 stdout —— 用它判断"是不是祖先"会永远得到空串，
    判据静默失效。这类"用错通道所以门是哑的"是今晚的高频缺陷，单独开一个入口。
    """
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    return result.returncode, result.stdout


def bind_project_workspace(state: Any, project_worktree: Path | str | None) -> None:
    """Bind one run to its stable, policy-owned Project directory."""
    if project_worktree is None:
        return
    root = Path(project_worktree).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise ProjectWorkspaceError(f"Project worktree does not exist: {root}")
    top = Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if top != root:
        raise ProjectWorkspaceError("Project workspace must be the Git worktree root")
    # 旧布局闸：工作区若还是信封时代的形状（`<节点>/artifacts/*.json`），
    # **当场拒绝开跑**。放这里是因为它是每个绑 Project 的 run 的必经点；
    # 让它 fail-closed 而不是让读取层返回"没有记录"——后者是个静默的错答案
    # （义务账本会认为 Analysis 从没跑过、目录页空空如也）。
    from core.research_state_reader import (
        UnmigratedWorkspaceError,
        _unmigrated_message,
        legacy_fragments,
    )

    fragments = legacy_fragments(root)
    if fragments:
        raise UnmigratedWorkspaceError(_unmigrated_message(fragments))
    owned = _NODE_WORKSPACES.get(str(state.node_type))
    if owned is None:
        # Unknown extension nodes get an isolated run directory.  They cannot
        # silently claim one of the six scientific owners' stable roots.
        owned = f"runs/extensions/{_segment(state.node_type)}/{_segment(state.run_id)}"
    relative = Path(owned)
    working = root / relative
    # 作用域可以是**一个文件**（curator 只拥有 MEMORY.md），也可以是一个目录。
    # 这个区分只在这里做一次 —— 谁分配作用域，谁负责说清楚它是什么形状；
    # 别处再靠路径字符串猜一遍，就会出 `MEMORY.md/artifacts` 那种目录/文件
    # 类型混淆（E2E v7 curator 三次 NotADirectoryError 就是这么来的）。
    # 判据用"最后一段带扩展名"而不是硬编码 MEMORY.md：以后再有文件作用域的
    # 节点，名单式判断会默认漏过。
    file_scoped = bool(relative.suffix)
    if file_scoped:
        working.parent.mkdir(parents=True, exist_ok=True)
        working.touch(exist_ok=True)
        working = working.parent
    else:
        working.mkdir(parents=True, exist_ok=True)
    state.project_worktree = root
    state.workspace_root = working
    state.workspace_relative_path = owned
    # 记录落在节点**自己的目录**里（正文就是文件，没有 artifacts/ 这一层）；
    # 文件作用域的节点没有可写目录 —— 它的交付物就是那个文件本身，记录留在
    # run 本地（None 表示"没有 Git 内的记录目录"）。
    state.workspace_records_dir = None if file_scoped else working
    state.hook_state["_project_workspace_expected_head"] = _git(root, "rev-parse", "HEAD").strip()
    _heal_legacy_readonly(root)


def _heal_legacy_readonly(root: Path) -> None:
    """迁移 shim：旧版 chmod 守卫（2026-08-11 ~ 08-13）崩溃时会把兄弟节点
    目录留在只读态，而它的自愈随那套机制一起删了。存量 worktree 撞上就是
    "权限不足"指向假原因。只修**明显是旧守卫手笔**的形状（owner 写位被清），
    等存量 worktree 都翻新过一轮后整个函数可删。
    """
    try:
        for entry in root.iterdir():
            if not entry.is_dir() or entry.is_symlink() or entry.name == ".git":
                continue
            try:
                mode = entry.stat().st_mode
                if not mode & 0o200:
                    for path in [entry, *entry.rglob("*")]:
                        try:
                            if not path.is_symlink():
                                path.chmod(path.stat().st_mode | 0o200)
                        except OSError:
                            continue
            except OSError:
                continue
    except OSError:
        pass


def working_directory(state: Any) -> Path:
    """Return the default cwd for model-controlled file and process tools.

    这个目录同时是**模型键入的相对路径**的锚点，和 `core.paths.node_output_dir`
    给出的**本节点产物目录**的父目录 —— 两者必须是同一个地方，否则"谁建的"和
    "谁来找"就会分叉：prepare 把 manuscript 建在一处、stage/compile 在另一处找，
    两边都不报错，图就是进不了稿子（E2E v20 实测）。
    """
    bound = getattr(state, "workspace_root", None)
    if bound is not None:
        path = Path(bound)
        if str(getattr(state, "node_type", "") or "") in _SCRATCH_ANCHORED:
            # 调度器没有私人抽屉：相对路径落 run 目录的 scratch/，不落记录。
            # 产物锚点（core/paths._node_anchor）仍在它的作用域 notes/ ——
            # 这是刻意的分叉：草稿归 run，产物归记录。
            path = Path(state.root) / "scratch"
    else:
        from core.paths import node_outputs_root

        path = node_outputs_root(state)
    path.mkdir(parents=True, exist_ok=True)
    return path


def tool_relpath(state: Any, path: Path | str) -> str:
    """`resolve_tool_path` 的**逆**：要交给模型的那个路径字符串该长什么样。

    路径有两种方言，各有各的锚点，**不能混用**：

      - 交给模型的（工具返回值、提示语里写的下一步）→ 锚在 `working_directory`，
        由本函数产出、`resolve_tool_path` 解析。
      - 写进记录的（artifact metadata、跨节点引用）→ 锚在 Project worktree，
        由 `core.paths.display_relpath` 产出、`resolve_display_relpath` 解析。

    以前只有两个**解析器**，没有对应的**产出函数**，于是想告诉模型"文件在这儿"
    的地方只能自己拼一个 —— hypothesis 的 `stage_hypothesis_draft` 就拼出了
    `outputs/hypothesis/drafts/x.md`，而它自己那句"下一步请
    save_artifact(content_from_file=…)"用同一套工具**永远读不到那个路径**
    （2026-08-27 实测：绑 worktree 的 run 上 100% 失败）。工具文案许诺的能力，
    接口必须给得出。
    """
    p = Path(str(path)).resolve()
    try:
        return p.relative_to(working_directory(state).resolve()).as_posix()
    except ValueError:
        # 工作目录之外但仍在边界内的，用绝对路径 —— `resolve_tool_path` 收。
        return str(p)


def _own_node_package(state: Any) -> Path | None:
    """本节点自己的 nodes/<type>/ 目录 —— 只读例外的唯一范围。"""
    node_type = str(getattr(state, "node_type", "") or "").strip()
    if not node_type:
        return None
    try:
        from core.loader import node_dir

        own = node_dir(node_type).resolve()
    except Exception:
        return None
    return own if own.is_dir() else None


class _Candidates(NamedTuple):
    """一个相对路径可能指向哪里。

    `tried` 按**优先级**排（第一个存在的胜出）；`default` 是都不存在时的落点 ——
    这两件事必须分开写。它们通常相同（自己的目录既是首选、也是报错该指向的地方），
    但显式 `project/` 前缀正好相反：调用方点名要 Project 根，工作目录下的同名目录
    只是兼容性优先，**都不存在时该落在根上**。把 default 写死成"第一个"会把这条
    路径悄悄改掉（写交付文件时落进 `<node>/project/…`，而且不报错）。
    """

    tried: list[Path]
    default: Path


def _only(path: Path) -> _Candidates:
    """显式前缀给的是显式答案：只有一个候选，它同时也是 default。"""
    return _Candidates([path], path)


def _first_existing(candidates: _Candidates) -> Path:
    for candidate in candidates.tried:
        if candidate.exists():
            return candidate
    return candidates.default


def _relative_candidates(
    state: Any, project_root: Path, path: Path, *, write: bool
) -> _Candidates:
    """一个相对路径**可能**指向哪里，按优先级排。

    ## 病根：模型手里同时有两套坐标系，而工具只认其中一套

    文件工具的相对路径锚在**节点自己的目录**；而模型读到的每一份跨节点坐标
    （开局地图、上游产物清单、别处报错里抄来的路径）都是**工作区根**坐标。
    于是它忠实地把地图上的 `figures/figures` 递进来，被解析成
    `paper/postprocess/figures` —— 不存在。E2E v22 实测 `list_files` 89 次
    "找不到目录"，**89 次全是这个形状**（节点名被贴重一遍）。

    此前修过两轮，都是在**说服模型**这一侧：地图改发绝对路径、报错附上
    "工作区里叫 X 的在这里"。89 次里 89 次都收到了提示，一次都没纠正过来；
    2026-09-09 那次现场里，模型甚至拿到了绝对路径提示，还是把它拧回相对路径
    又贴了一遍节点名。**框架机械答得出的问题，不该反复交给模型去猜**。

    所以改在解析这一侧：读的时候两套坐标都试一遍，取真实存在的那个。歧义只在
    "两处都存在"时才发生，那时以自己的目录为准（严格保持既有行为）。

    ## 写侧不参与

    写只能落在自己的作用域里，多一个锚点就是多一个越界口子。唯一的例外是
    显式 `project/` 前缀 —— 那是调用方明确点名"我要写 Project 根下的交付文件"
    （2026-08-31 加的），显式前缀给的是显式答案，读写同一条。
    """
    parts = path.parts
    work_dir = working_directory(state)

    if parts and parts[0] == "workspace":
        # 显式前缀 = 显式答案：调用方说了"我的工作区"，不再另找。
        inner = Path(*parts[1:]) if len(parts) > 1 else Path(".")
        return _only((work_dir / inner).resolve(strict=False))
    if str(path) in {"transcript.jsonl", "summary.json", "pause_pending.json"}:
        return _only((Path(state.root) / path).resolve(strict=False))

    out: list[Path] = []
    if parts and parts[0] == "artifacts":
        # run 本地的产物优先于工作目录下的同名目录（既有优先级，逐字保留）。
        # `.history/` 早已不存在，不再为它留位。
        out.append((Path(state.root) / path).resolve(strict=False))
    out.append((work_dir / path).resolve(strict=False))

    if parts and parts[0] == "project":
        rooted = (
            (project_root / Path(*parts[1:])).resolve(strict=False)
            if len(parts) > 1 else project_root
        )
        out.append(rooted)
        # ⚠️ default 是 rooted，不是 out[0]：调用方点名了 Project 根，工作目录下
        # 的同名目录只是兼容性优先。都不存在时落回工作目录 = 悄悄改掉交付写路径。
        return _Candidates(out, rooted)
    if write:
        return _Candidates(out, out[0])

    # ── 以下只在读侧 ──────────────────────────────────────────────────
    own = _own_node_package(state)
    if (own is not None and len(parts) > 1 and parts[0] == "nodes"
            and parts[1] == str(getattr(state, "node_type", "") or "")):
        # harness.yaml 用的就是这种仓库相对写法（`nodes/_reviewer/specs/...`）。
        # 它锚在节点工作目录上会永远"找不到文件"，改用绝对路径又撞边界 —— 两条
        # 路都走不通，那份 spec 就 100% 读不到（而测试全绿：测试不绑 Project）。
        out.append(own.joinpath(*parts[2:]).resolve(strict=False))
    # 工作区根坐标：模型看到的跨节点路径全长这样。越界与否照旧由下面那道
    # 边界检查判 —— 这里只是多给一个候选，不放宽任何一条边界。
    out.append((project_root / path).resolve(strict=False))
    # 都不存在时指回自己的目录：那是模型该先去看的地方，报错也该说那一个。
    return _Candidates(out, out[0])


def resolve_tool_path(state: Any, value: str, *, write: bool = False) -> Path:
    """Resolve a tool path and enforce the Project read/write boundary.

    Historical callers commonly prefix paths with ``workspace/``.  Strip that
    logical prefix for Platform-bound runs while preserving explicit run-state
    paths such as ``artifacts/...`` for read-only inspection.
    """
    path = Path(value).expanduser()
    if getattr(state, "project_worktree", None) is None:
        resolved = (
            path.resolve(strict=False)
            if path.is_absolute()
            else (working_directory(state) / path).resolve(strict=False)
        )
        # 注：相对路径锚在 working_directory 上（绑与不绑都一样），产物漏斗
        # `core.paths.node_output_dir` 对本节点也锚在同一处 —— 两者必须重合。
        # 没绑 Project 的 run（CLI / fixture）也要有写边界。读可以到处读（跑
        # 计算要读系统里的依赖、数据集），但**写**必须留在这一轮 run 目录里 ——
        # 否则一个绝对路径就能写到任意位置。这条以前只被 writing 节点私下实现
        # 了一份（`_is_in_run_root`），别的工具走同一个解析器却没有边界。
        if write:
            run_root = Path(state.root).resolve()
            if not (resolved == run_root or resolved.is_relative_to(run_root)):
                # bypass = 平台声明的完全无人值守：它豁免的是"会去问人"的安全
                # 护栏，不豁免节点归属那条结构不变量（谁能写谁的目录）。这里属于
                # 前者 —— 主机文件系统的写入范围。
                from shared.lib import dangerous_commands as _danger

                if not _danger.bypass_enabled():
                    raise ProjectWorkspaceError(
                        f"Path escaped the run directory: {value!r}"
                    )
        return resolved
    project_root = Path(state.project_worktree).resolve()
    if path.is_absolute():
        resolved = path.resolve(strict=False)
    else:
        resolved = _first_existing(
            _relative_candidates(state, project_root, path, write=write))
    run_root = Path(state.root).resolve()
    if resolved == run_root or resolved.is_relative_to(run_root):
        return resolved
    if not (resolved == project_root or resolved.is_relative_to(project_root)):
        # 节点自己的 package（nodes/<own type>/）是**只读**例外。harness.yaml 里
        # 白纸黑字写着 `read_file('nodes/_reviewer/specs/project_synthesis.md')`
        # 之类的指路，而那个位置在 Project 边界外 —— 文案许诺的能力，接口得给得
        # 出，否则模型只能改用绝对路径去撞边界（实测 8 次）。范围严格限自己那个
        # 目录：跨节点 spec 照旧拦，写照旧拦（写权限在下面单独判）。
        if not write:
            own = _own_node_package(state)
            if own is not None and (resolved == own or resolved.is_relative_to(own)):
                return resolved
        raise ProjectWorkspaceError(
            f"Path escaped the Project boundary: {value!r}\n"
            f"可读范围：本 Project 目录 {str(project_root)!r}"
            + (f"、本节点自己的 spec 目录 {str(_own_node_package(state))!r}"
               if _own_node_package(state) is not None else "")
            + "。\n平台代码、别的节点的 spec、框架状态目录都不在可读范围内；"
            "要找论文缓存用 literature 的检索工具，不要直接读盘。"
        )
    if write:
        owned_target = (project_root / str(state.workspace_relative_path)).resolve()
        owned_is_file = owned_target.suffix != "" and owned_target.name == "MEMORY.md"
        permitted = (
            resolved == owned_target
            if owned_is_file
            else resolved == owned_target or resolved.is_relative_to(owned_target)
        )
        if not permitted and getattr(state, "hook_state", {}).get("_deliverable_writes"):
            # 交付写权（harness.deliverable_writes，2026-08-31）：放开的只是
            # **无主之地**——不属于任何节点作用域、不是框架状态的路径，典型是
            # 用户点名的交付文件（project/LITERATURE_REVIEW.md）。三种拒绝各给
            # 各的指路：只说不许而不说该找谁，调用方唯一能做的就是重试。
            from shared.lib import dangerous_commands as _danger

            rel = resolved.relative_to(project_root).as_posix()
            protected = _danger.match_protected_file_path(rel)
            owner = owning_node_for_path(project_root, resolved)
            if rel.startswith(".research/"):
                # `.research/*` 是框架状态，交付写权不涉足（调度器的作用域
                # 已经不在这下面了：它写 notes/，草稿落 run 目录）。
                raise ProjectWorkspaceError(
                    f"'.research/' 下除自己的作用域外是框架状态，交付写权不涉足：{rel!r}"
                )
            if protected is not None:
                raise ProjectWorkspaceError(
                    _danger.FILE_PATH_DENY_MESSAGE.format(category=protected)
                )
            if owner is not None:
                raise ProjectWorkspaceError(
                    f"'{rel}' 是 {owner} 的作用域——它的产出该由它自己做："
                    f"run_node(node_type={owner!r})。交付写权只覆盖无主之地"
                    f"（如 project/<用户点名的文件>）。"
                )
            permitted = True
        if not permitted:
            raise ProjectWorkspaceError(
                f"Node '{state.node_type}' may only write {state.workspace_relative_path}"
            )
    return resolved


def validate_tool_cwd(state: Any, value: str | None) -> Path:
    """A process tool may execute only with cwd inside the owned workspace."""
    if not value:
        return working_directory(state)
    resolved = resolve_tool_path(state, value, write=True)
    if resolved.exists() and not resolved.is_dir():
        raise ProjectWorkspaceError(f"Tool cwd is not a directory: {value!r}")
    return resolved


_DELEGATED_KEY = "_project_workspace_delegated"


def _dispatch_subtree_workspaces(node_type: str) -> list[str]:
    """这次派发能合法产出的目录 —— 子节点**及其可再派的后代**。

    委派面必须是**子树**，不是一层。父派 writing，writing 自己调 postprocess
    出图（`callable_nodes: [postprocess]`，正规路径）——孙辈写自己的目录时，
    父的 permitted 里没有它，于是整轮被判越界回滚。实测代价：

      2026-08-07 ×3  派 hypothesis → 回滚 ['.git', 'literature']
      2026-08-08     派 writing    → 回滚 ['figures/artifacts/visual_brief__…']

    第二条那次，orchestrator 从报错推出"writing 画不了图"，改成自己先派
    postprocess 再派 writing，并把这个绕行写进 scratchpad —— 此后 61:4 的
    派图比例是这一条错误结论的复利，而不是调度心智出了问题。

    闭包取自各 harness 的 `callable_nodes`（静态声明，已有的真相源），不是
    运行时"我派了谁"——后者只能在**发生之后**知道，而守卫的比对发生在工具
    返回之后，孙辈那次登记写在子的 state 上，父永远收不到。

    不变量没有放松：每一层仍只能写自己的目录 + 自己派发期间的子树。writing
    用 shell 往 postprocess/ 里写，在 writing 自己那一层照样被拦。
    """
    seen: list[str] = []
    pending = [str(node_type)]
    visited: set[str] = set()
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        owned = _NODE_WORKSPACES.get(current)
        if owned and owned not in seen:
            seen.append(owned)
        for callee in _callable_nodes(current):
            if callee == "*":
                # 全权（只有 _orchestrator 声明它，且它从不作为 callee 出现）。
                # 真出现时不静默放行整棵树 —— 停在这一层，宁可误报也别失守。
                continue
            if callee not in visited:
                pending.append(callee)
    return seen


_CALLABLE_CACHE: dict[str, tuple[str, ...]] = {}


def _callable_nodes(node_type: str) -> tuple[str, ...]:
    """读 harness 声明的 callable_nodes；读不到就当叶子（宁可窄，不可宽）。"""
    if node_type in _CALLABLE_CACHE:
        return _CALLABLE_CACHE[node_type]
    try:
        from core.loader import load_harness

        declared = tuple(str(n) for n in (load_harness(node_type).callable_nodes or []))
    except Exception:
        declared = ()
    _CALLABLE_CACHE[node_type] = declared
    return declared


def note_delegated_workspace(state: Any, node_type: str) -> None:
    """登记"本次工具调用期间派发了这个子节点"，其**子树**目录随之视为在场。

    由 run_node 在**工具入口**调用（多条派发路径的必经点）。记录由
    enforce_after_tool 消费后清空 —— 生命周期 = 这一次工具调用。
    唯一用途是给事后**见证**降噪：派发期间子树目录里出现的新文件是子节点的
    正当产出，不该被报成本节点的越界写。没有任何执法动作挂在它上面。
    """
    owned_dirs = _dispatch_subtree_workspaces(node_type)
    if not owned_dirs or getattr(state, "project_worktree", None) is None:
        return
    active = list(state.hook_state.get(_DELEGATED_KEY) or [])
    state.hook_state[_DELEGATED_KEY] = active + [d for d in owned_dirs if d not in active]


def _delegated_workspaces(state: Any) -> list[Path]:
    return [Path(p) for p in (state.hook_state.get(_DELEGATED_KEY) or []) if p]


#: Docker 的只读 bind/rootfs 通常报 EROFS，也可能由内核权限报 EPERM/EACCES。
#: 三种都识别，错误翻译不能依赖某个容器运行时的单一措辞。
_DENIED = re.compile(
    r"(?:Permission denied|Operation not permitted|Read-only file system|EACCES|errno 13)"
)


def explain_permission_denied(state: Any, text: str) -> str | None:
    """把 "Permission denied" 翻译成"这是谁的东西、你该怎么做"。

    进程沙箱（core/sandbox.py）给的原生报错是
    `/bin/sh: ../hypothesis/x: Operation not permitted` —— 位置精确，但
    **读起来像环境问题**：模型接下来多半去试 `chmod +w` 或 `sudo`，
    那是被假原因带偏（反复出现的最贵的一类错误）。

    路径里的 `..` 是相对**工具的 cwd**（节点自己的目录），不是相对工作区根 ——
    锚错了就永远归不出 owner，而这道翻译会静默失效（第一版就是这么写错的）。
    两个锚点都试。

    只在真的命中"工作区里别人的目录"时才加注。系统别处的权限问题原样透传 ——
    用一句错的解释盖住一个真的环境故障，比不解释更糟。
    """
    if not text or not _DENIED.search(text):
        return None
    root_value = getattr(state, "project_worktree", None)
    if root_value is None:
        return None
    root = Path(root_value)
    owned = str(getattr(state, "workspace_relative_path", "") or "")
    anchors = [Path(getattr(state, "workspace_root", None) or root), root]
    owned_top = Path(owned).parts[0] if owned else ""
    for candidate in re.findall(r"[\w./~-]*[\w-](?:/[\w.-]+)+", text):
        for anchor in anchors:
            path = (Path(candidate) if Path(candidate).is_absolute()
                    else anchor / candidate)
            owner = owning_node_for_path(root, path)
            if owner is None:
                continue
            if owned_top and _NODE_WORKSPACES.get(owner, "").split("/")[0] == owned_top:
                continue                      # 自己的东西，别乱解释
            return (
                f"⛔ `{candidate}` 属于 **{owner}** 节点，这一轮它是只读的 —— "
                f"这不是环境故障，别去 chmod / sudo。\n"
                f"你只能写自己的作用域 `{owned}`。要改 {owner} 的东西：用 "
                f"run_node 起 {owner}，把要求写进 node_inputs 让它自己改。"
            )
    return None


def capture_before_tool(state: Any) -> None:
    """给事后见证拍一张"调用前"的路径快照。

    只记**路径集合**（不记字节、不记 mode、不做任何可执行动作的准备）：
    调用后新出现的越界路径 = 这一次调用的越界证据。已经在场的不重复报 ——
    它们要么上次报过，要么是别人（框架 / 子节点）的正当在制品。
    """
    root_value = getattr(state, "project_worktree", None)
    owned_value = str(getattr(state, "workspace_relative_path", "") or "")
    if root_value is None or not owned_value:
        return
    root = Path(root_value)
    state.hook_state[_GUARD_BASELINE_KEY] = {
        "head": _git(root, "rev-parse", "HEAD").strip(),
        "outside": _outside_paths(state, root, [Path(owned_value), *_delegated_workspaces(state)]),
    }


#: 历史隔离区（守卫还会销毁东西的年代留下的）。已不再写入，但存量目录在
#: worktree 里未提交 —— 从见证范围里排除，否则每次调用都报一遍旧账。
_LEGACY_QUARANTINE = Path(".research/quarantine")


def _outside_paths(state: Any, root: Path, permitted: list[Path]) -> dict[str, str]:
    """作用域之外的脏路径 → 内容指纹。

    指纹是必要的：只比路径集合，"已在场文件被改了内容"就不可见 —— 而篡改
    上游文件恰恰是最贵的那类越界。只哈希内容，不看 mode（mode 会被 chmod
    类操作扰动，正是旧守卫的误判源之一），不存字节（见证不需要恢复能力）。
    """
    dirty = _status_paths(_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    out: dict[str, str] = {}
    for item in dirty:
        if Path(item).is_relative_to(_LEGACY_QUARANTINE):
            continue
        if any(Path(item).is_relative_to(area) for area in permitted):
            continue
        out[item] = _content_signature(root / item)
    return out


def _content_signature(target: Path) -> str:
    try:
        if target.is_symlink():
            return "symlink:" + target.readlink().as_posix()
        if target.is_dir():
            return "dir"
        if not target.exists():
            return "missing"
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"          # 读不了就当"说不清"，enforce 侧不据此报警


def _committed_paths_between(root: Path, base: str, head: str) -> list[str] | None:
    """base..head 之间所有提交碰过的路径；无法确认为快进则返回 None。

    None 与空列表语义不同 —— None = "说不清"，调用方必须按越界处理（回退）；
    空列表 = "确实是快进且没碰任何文件"。fail-closed 写在类型里，不写在注释里。
    """
    if not base or not head:
        return None
    # 必须是快进：base 是 head 的祖先。回退 / 换分支不是"合法推进"。
    code, _ = _git_status(root, "merge-base", "--is-ancestor", base, head)
    if code != 0:
        return None
    code, out = _git_status(root, "diff", "--name-only", "-z", f"{base}..{head}")
    if code != 0:
        return None
    return [item for item in out.split("\0") if item]


def unchanged_since(
    state: Any, relative_path: str, *, since_commit: str | None = None
) -> bool | None:
    """这个文件从 `since_commit` 到此刻有没有被动过（提交的和没提交的都算）。

    → True  = 一个字都没改过
      False = 改过
      None  = **说不清**（不是快进 / git 出错 / 没绑工作区）——
              调用方必须按"不能确认"处理，不许当 True。

    ## 这是给"冻结"用的判据

    现在的冻结是产出方**自己**往 metadata 写 `frozen: true` —— 判据是**自证**：
    想造假改个字段就行。而同一件事 Git 记得更硬：

        冻结 = 这个文件在下游开跑之后一次都没被改过

    要造假得改写历史，而 commit 权限只在平台手里（harness 只能**请求**
    checkpoint，见 request_completion_checkpoint）。**严格更强，而且不需要
    任何"字段名"这个概念** —— 后者正是 2026-08-10 那一夜反复卡住的东西
    （产物在、字段缺/改名 → 判死）。

    ## 为什么是「没改过」而不是「有个冻结时刻」

    "在某时刻被冻结"要么落一个时间戳（又一份可篡改的自证），要么落一份
    登记表（又一个真相源）。而下游真正关心的是**一句话**：
    我开跑之后，这份预注册还是不是我当时看到的那份。这句话 Git 直接能答。

    `since_commit` 缺省取 `_project_workspace_expected_head` —— 本 run 绑定
    工作区时记下的 HEAD，也就是"我开跑时看到的世界"。
    """
    root_value = getattr(state, "project_worktree", None)
    if root_value is None:
        return None
    root = Path(root_value)
    base = str(
        since_commit
        or getattr(state, "hook_state", {}).get("_project_workspace_expected_head")
        or ""
    ).strip()
    if not base:
        return None
    rel = str(relative_path).strip()
    if not rel:
        return None

    code, head = _git_status(root, "rev-parse", "HEAD")
    if code != 0:
        return None
    head = head.strip()

    # 必须是快进：回退 / 换分支不是"合法推进"，说不清就说不清。
    code, _ = _git_status(root, "merge-base", "--is-ancestor", base, head)
    if code != 0:
        return None

    code, committed = _git_status(
        root, "diff", "--name-only", "-z", f"{base}..{head}", "--", rel
    )
    if code != 0:
        return None
    if [item for item in committed.split("\0") if item]:
        return False

    # 未提交的改动同样算"改过" —— 只看提交历史，会把正在被编辑的文件判成冻结。
    code, dirty = _git_status(
        root, "status", "--porcelain=v1", "--untracked-files=all", "--", rel
    )
    if code != 0:
        return None
    return not dirty.strip()


def enforce_after_tool(state: Any, tool_name: str) -> dict[str, Any] | None:
    """事后**见证**：报告这一次工具调用的越界写与 HEAD 变化。只报告。

    墙在别处（core/sandbox.py 的进程沙箱 + dangerous_commands 的 git 闸 +
    平台 checkpoint 的 head 校验）。这里没有任何回退 / 隔离 / 删除能力 ——
    "解释不了"不构成销毁的授权，这个教训用真数据付过两次学费
    （2026-08-11、2026-08-13，见模块 docstring）。

    返回 None（无事）或 {"paths": [...], "note": str}（有越界写的证据，
    调用方把 note 附进工具结果让模型看到）；或 {"witness_unavailable": True,
    "paths": [], "note": str}（调用前快照缺席 → 「本次无越界」**算不出来**）。

    为什么缺 baseline 不能返 None：None 在调用方读作「本次无越界」。快照没拍成
    时这是个假值 —— 账上会写着「见证过、干净」，而实际是「没见证」。派发口
    （core/tool_registry）在快照崩了时照跑工具、结果附 `workspace_witness_failed`；
    这里是它的对称面：after 端如实报「无见证」而不是报空集。两件事一条规则。
    """
    root_value = getattr(state, "project_worktree", None)
    owned_value = str(getattr(state, "workspace_relative_path", "") or "")
    if root_value is None or not owned_value:
        return None
    root = Path(root_value)
    baseline = state.hook_state.pop(_GUARD_BASELINE_KEY, {}) or {}
    permitted = [Path(owned_value), *_delegated_workspaces(state)]
    state.hook_state.pop(_DELEGATED_KEY, None)

    # ── HEAD：快进 = 平台的 checkpoint 权威在干活，抬基线；其余留证据 ──────
    #
    # 模型自己造不出提交：git 写命令在闸口硬拒（#389/#392），沙箱的可写面又
    # 不含 `.git`。所以"是不是快进"就够分类了 —— 不再需要"这个提交碰的路径
    # 在不在我派发过的子树里"那套 run 级委派记账（它曾经是误删实验数据的
    # 推断链条的一环）。
    expected_head = str(
        baseline.get("head") or state.hook_state.get("_project_workspace_expected_head") or ""
    )
    actual_head = _git(root, "rev-parse", "HEAD").strip()
    if expected_head and actual_head != expected_head:
        committed = _committed_paths_between(root, expected_head, actual_head)
        state.hook_state["_project_workspace_expected_head"] = actual_head
        if committed is None:
            state.append_transcript(
                "workspace_head_changed_unattributed",
                previous=expected_head,
                current=actual_head,
                note="HEAD 变了且不是快进（或读不出来）；已留证据、未动任何文件。"
                     "提交权限由命令闸口 + 平台 checkpoint 权威判定。",
            )
        else:
            state.append_transcript(
                "workspace_head_advanced",
                previous=expected_head,
                current=actual_head,
                paths=committed[:50],
                note="平台 checkpoint 权威推进 HEAD（快进）。",
            )

    # ── 越界写：新出现的才是这一次调用的证据；报告，不动 ──────────────────
    outside_before = baseline.get("outside")
    if outside_before is None:
        # capture 没跑成 → 归因不了。不误报（没有 paths），也不假报「干净」：
        # 如实说这一次没见证。
        return {
            "witness_unavailable": True,
            "paths": [],
            "note": (
                "本次调用没有越界见证：调用前的工作区快照缺席，"
                "「这次有没有在作用域外写东西」算不出来 —— 不是「没有越界」。"
            ),
        }
    outside_now = _outside_paths(state, root, permitted)
    fresh = sorted(
        item for item, signature in outside_now.items()
        if signature != "unreadable"
        and outside_before.get(item, "") != signature
        and outside_before.get(item) != "unreadable"
    )
    if not fresh:
        return None
    owners = {item: owning_node_for_path(root, root / item) for item in fresh}
    named = sorted({owner for owner in owners.values() if owner})
    note = (
        f"⚠️ 本次调用在你的作用域 `{owned_value}` 之外写了 {len(fresh)} 个路径"
        f"（{', '.join(fresh[:5])}{'…' if len(fresh) > 5 else ''}）。"
        "文件留在原地未动，但它们不属于你的交付面，不会进你的 checkpoint。"
        + (
            f"要改 {' / '.join(named)} 的东西：用 run_node 派发那个节点，"
            "把要求写进 node_inputs 让它自己改。"
            if named else ""
        )
    )
    state.append_transcript(
        "workspace_out_of_scope_writes",
        tool_name=tool_name,
        node_type=state.node_type,
        owned_path=owned_value,
        paths=fresh[:50],
        owners={k: v for k, v in owners.items() if v},
        note=note,
    )
    return {"paths": fresh, "note": note}


def _status_paths(raw: str) -> list[str]:
    paths: list[str] = []
    records = raw.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        path = record[3:] if len(record) >= 4 else ""
        if record[:2] in {"R ", "C ", "RM", "CM"} and index < len(records):
            path = records[index]
            index += 1
        if path:
            paths.append(path)
    return sorted(set(paths))


def _oversized_placeholder(path: str, size: int, mtime_ns: int) -> str:
    """超限文件在影子 tree 里的替身 —— 只记身份，不记内容。

    身份里必须带 size+mtime：替身内容不变就等于"这个文件没动过"，一份每步
    追加的模拟日志会因此彻底隐形。
    """
    return (
        f"[未观测内容：{size} 字节，超过 {_MAX_OBSERVED_BLOB_BYTES} 字节的观测上限]\n"
        f"path={path}\nsize={size}\nmtime_ns={mtime_ns}\n"
    )


def _workspace_tree(state: Any, root: Path, relative: str) -> str:
    """把这个节点目录的**当前内容**写成一个 Git tree，返回 OID。

    ## 为什么要绕这一手

    Git 只会拿 HEAD（或另一个 tree）跟工作区比。而"这一次工具调用改了什么"
    要比的是**上一次观测**与现在 —— 上一次观测的内容在 Git 里没有名字，比不
    了。把每次观测都落成一个 tree，它就有名字了，`git diff <上次> <这次>` 就
    是精确答案：新建、删除、改名、二进制、模式位全由 Git 判，不在 Python 里
    再实现一遍 diff。

    tree 的 OID 顺带就是内容指纹（内容寻址），旧那套"status + patch + 逐文件
    sha256"的手工摘要连同它的未跟踪文件补算一起删掉了。

    索引写在 run 自己的目录里，不碰仓库真索引：平台的 checkpoint 随时可能在
    同一个 worktree 上 `git add`，两边抢 index.lock 会把科研工作弄挂。索引跨
    调用保留还让 Git 能靠 stat 跳过没动过的文件，不必每次重读整个目录。
    """
    index = Path(getattr(state, "root", root)) / ".workspace-index"
    index.parent.mkdir(parents=True, exist_ok=True)
    env = {"GIT_INDEX_FILE": str(index)}
    listing = _git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", relative
    )
    tracked: list[str] = []
    oversized: list[tuple[str, int, int]] = []
    for path in (item for item in listing.split("\0") if item):
        target = root / path
        try:
            if target.is_symlink():
                tracked.append(path)
                continue
            stat = target.stat()
        except OSError:
            continue      # 读不到就当不在场；见证不为一个消失的文件报错
        if stat.st_size <= _MAX_OBSERVED_BLOB_BYTES:
            tracked.append(path)
        else:
            oversized.append((path, stat.st_size, stat.st_mtime_ns))
    # 上一次留下的条目里，这次已经不在场的必须清掉，否则删除永远出不来
    # （tree 里还挂着那个文件）。用"删差集"而不是"清空重建"：索引保留着
    # stat 缓存，没动过的文件 Git 就不必再读一遍内容 —— 一个上万文件的
    # experiment 目录，两者是每次工具调用都全量重哈希与几乎零成本的差别。
    present = {*tracked, *(path for path, _, _ in oversized)}
    gone = sorted(
        item
        for item in _git(root, "ls-files", "-z", env=env).split("\0")
        if item and item not in present
    )
    if gone:
        _git(
            root, "update-index", "--force-remove", "-z", "--stdin",
            stdin="\0".join(gone) + "\0", env=env,
        )
    if tracked:
        _git(
            root, "update-index", "--add", "-z", "--stdin",
            stdin="\0".join(tracked) + "\0", env=env,
        )
    for path, size, mtime_ns in oversized:
        oid = _git(
            root, "hash-object", "-w", "--stdin",
            stdin=_oversized_placeholder(path, size, mtime_ns),
        ).strip()
        _git(root, "update-index", "--add", "--cacheinfo", f"100644,{oid},{path}", env=env)
    return _git(root, "write-tree", env=env).strip()


def _numstat(raw: str) -> list[tuple[str, int, int]]:
    """`git diff --numstat -z` → [(path, +行, -行)]。二进制给 "-"，记 0。"""
    out: list[tuple[str, int, int]] = []
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        parts = field.split("\t")
        if len(parts) < 3:
            continue
        adds, dels, path = parts[0], parts[1], parts[2]
        if not path:                       # 改名/复制：源和目标各占一条后续记录
            if index + 1 >= len(fields):
                break
            path = fields[index + 1]
            index += 2
        out.append((
            path,
            int(adds) if adds.isdigit() else 0,
            int(dels) if dels.isdigit() else 0,
        ))
    return out


_STATUS_WORDS = {"A": "added", "D": "deleted", "M": "modified", "T": "modified"}


def _name_status(raw: str) -> dict[str, str]:
    """`git diff --name-status -z` → {path: added|modified|deleted|renamed}。

    卡片上"Edited"与"Created"是两句不同的话。让 Git 说，别让 UI 从 ±行数猜
    （`-0` 既可能是新建也可能是纯追加）。
    """
    out: dict[str, str] = {}
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        code = fields[index]
        index += 1
        if not code:
            continue
        if code[0] in {"R", "C"}:
            if index + 1 >= len(fields):
                break
            out[fields[index + 1]] = "renamed"
            index += 2
            continue
        if index >= len(fields):
            break
        out[fields[index]] = _STATUS_WORDS.get(code[0], "modified")
        index += 1
    return out


def _cut_section(section: list[str], allowance: int) -> tuple[str, bool]:
    """按额度截一个文件的 diff：保头去尾。

    文件头（`diff --git` / `new file mode` / `---` / `+++`）永远留着 —— UI 靠它
    才知道"这个文件也变了"；砍掉头等于让一个文件从证据里消失。截断标记写成
    context 行（前导空格），解析器和渲染器都不必为它加分支。
    """
    text = "\n".join(section)
    if len(text.encode("utf-8", "replace")) <= max(allowance, 0):
        return text, False
    header: list[str] = []
    for line in section:
        header.append(line)
        if line.startswith("+++ ") or line.startswith("Binary files "):
            break
    kept = list(header)
    used = len("\n".join(kept).encode("utf-8", "replace"))
    for line in section[len(header):]:
        size = len(line.encode("utf-8", "replace")) + 1
        if used + size > allowance:
            break
        kept.append(line)
        used += size
    kept.append(" … 这个文件的 diff 过长，已截断（完整内容在 Project Git 里）")
    return "\n".join(kept), True


def _bounded_patch(patch: str) -> tuple[str, bool]:
    """单文件先限额、总量再限额，截了就如实说截了。

    不限额的话，一次观测能把几 MB 正文塞进 transcript 和事件库；静默截断则
    会让读者把"截断处"当成"改动到此为止"。两个都不行。
    """
    if not patch.strip():
        return "", False
    sections: list[list[str]] = []
    current: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git ") and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)
    kept: list[str] = []
    truncated = False
    budget = _MAX_PATCH_BYTES
    for section in sections:
        text, cut = _cut_section(section, min(_MAX_FILE_PATCH_BYTES, budget))
        truncated = truncated or cut
        kept.append(text)
        budget -= len(text.encode("utf-8", "replace"))
    return "\n".join(kept), truncated


def _diff_facts(root: Path, base: str, tree: str, relative: str) -> dict[str, Any]:
    """base → tree 之间，这个节点目录里发生的全部事实。"""
    stats = _numstat(_git(root, "diff", "--numstat", "-z", base, tree, "--", relative))
    statuses = _name_status(
        _git(root, "diff", "--name-status", "-z", base, tree, "--", relative)
    )
    patch, truncated = _bounded_patch(
        _git(root, "diff", "--no-ext-diff", "--unified=3", base, tree, "--", relative)
    )
    return {
        "paths": sorted(path for path, _, _ in stats),
        "files_changed": len(stats),
        "additions": sum(adds for _, adds, _ in stats),
        "deletions": sum(dels for _, _, dels in stats),
        "file_stats": [
            {
                "path": path,
                "additions": adds,
                "deletions": dels,
                "status": statuses.get(path, "modified"),
            }
            for path, adds, dels in sorted(stats)[:50]
        ],
        "patch": patch,
        "patch_truncated": truncated,
    }


def workspace_snapshot(state: Any) -> dict[str, Any] | None:
    """这个 run 到目前为止**一共**改了什么（对 HEAD）—— checkpoint 的口径。

    平台按 `paths` 提交，所以这里的路径必须来自 `git status`（工作区对 HEAD
    的脏路径），不能来自 diff：两者在"平台刚 checkpoint 完"这种时刻会分叉。
    """
    root_value = getattr(state, "project_worktree", None)
    relative = str(getattr(state, "workspace_relative_path", "") or "")
    if root_value is None or not relative:
        return None
    root = Path(root_value)
    paths = _status_paths(
        _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", relative)
    )
    if not paths:
        return None
    tree = _workspace_tree(state, root, relative)
    facts = _diff_facts(root, "HEAD", tree, relative)
    return {
        "workspace_root": str(root),
        "workspace_prefix": relative,
        "paths": paths,
        "files_changed": len(paths),
        "additions": facts["additions"],
        "deletions": facts["deletions"],
        "fingerprint": tree,
    }


def workspace_delta(state: Any) -> dict[str, Any] | None:
    """自**上一次观测**以来这个节点目录变了什么。没变则 None。

    ## 为什么不是"对 HEAD"

    对 HEAD 比回答的是"这个节点至今一共改了多少"。把那个数挂在一次工具调用
    旁边就是说谎：2026-08-17 用户的截图里，写了 6 个文件的那一步显示
    "Edited 14 files +1869"，因为上一步的 8 个文件对 HEAD 仍然是脏的，被整个
    又数了一遍。卡片问的是"这一次干了什么"，答案只能来自"上一次观测 ↔ 现在"。

    顺带解决了另一半：新建的未跟踪文件对 `git diff HEAD` 是不可见的（旧代码
    因此手工补算行数，而 patch 那边是空的）—— 走影子 tree 之后它们就是普通的
    新增文件，diff 正文自然有内容，这正是截图里那一整批 `+N -0` 的文件。
    """
    root_value = getattr(state, "project_worktree", None)
    relative = str(getattr(state, "workspace_relative_path", "") or "")
    if root_value is None or not relative:
        return None
    root = Path(root_value)
    tree = _workspace_tree(state, root, relative)
    baseline = str(state.hook_state.get(_BASELINE_TREE_KEY) or "")
    if baseline == tree:
        return None
    state.hook_state[_BASELINE_TREE_KEY] = tree
    state.hook_state[FINGERPRINT_KEY] = tree
    facts = _diff_facts(root, baseline or "HEAD", tree, relative)
    if not facts["files_changed"]:
        return None
    facts.update({
        "workspace_root": str(root),
        "workspace_prefix": relative,
        "fingerprint": tree,
    })
    return facts


#: 平台自己的记账目录 —— 不是研究产物。
#:
#: `.research/` 装的是运行时内务：上下文压缩审计（compression_log）、
#: run manifest、资源画像…… 它们**确实**是工作区里的真实改动，但对"这个
#: 研究做了什么"零信息。判据放在这一层（产生事实的地方）算一次，UI 只读
#: 结论 —— 别在前端再写一份同义的名单（那就是两个会各自演化的真相）。
_INTERNAL_WORKSPACE_PREFIXES = (".research/",)


def _is_internal_bookkeeping(path: str) -> bool:
    return path.startswith(_INTERNAL_WORKSPACE_PREFIXES)


def observe_after_tool(state: Any, tool_name: str) -> dict[str, Any] | None:
    """Emit one transcript event when a tool changed managed Project files."""
    try:
        delta = workspace_delta(state)
        if delta is None:
            return None
        state.append_transcript(
            "workspace_changed",
            tool_name=tool_name,
            node_type=state.node_type,
            run_id=state.run_id,
            workspace_prefix=delta["workspace_prefix"],
            paths=delta["paths"],
            files_changed=delta["files_changed"],
            additions=delta["additions"],
            deletions=delta["deletions"],
            file_stats=delta["file_stats"],
            patch=delta["patch"],
            patch_truncated=delta["patch_truncated"],
            fingerprint=delta["fingerprint"],
            # 这次改动是不是**纯**内务（全部落在 .research/ 里）。整批都是
            # 记账时，UI 不必为它画一张"Edited 2 files"的卡 —— 实测一个会话
            # 里 compression_log 被改了 16 次，用户问"这个是干嘛的"。
            # 混合改动照原样呈现：真产物在里面，不能因为夹了内务就少报。
            internal_only=all(
                _is_internal_bookkeeping(p) for p in (delta["paths"] or [])
            ) and bool(delta["paths"]),
        )
        return delta
    except Exception as exc:  # observation must never break scientific work
        try:
            state.append_transcript(
                "workspace_observation_failed",
                tool_name=tool_name,
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
        except Exception:
            pass
        return None


def request_completion_checkpoint(state: Any, status: str) -> dict[str, Any] | None:
    """Ask the owning Platform to checkpoint a completed node's dirty paths."""
    snapshot = workspace_snapshot(state)
    if snapshot is None:
        return None
    state.append_transcript(
        "workspace_checkpoint_requested",
        node_type=state.node_type,
        run_id=state.run_id,
        run_status=status,
        workspace_prefix=snapshot["workspace_prefix"],
        paths=snapshot["paths"],
        files_changed=snapshot["files_changed"],
        additions=snapshot["additions"],
        deletions=snapshot["deletions"],
        fingerprint=snapshot["fingerprint"],
    )
    return snapshot

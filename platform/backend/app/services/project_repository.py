"""Git-native, portable research Project repositories.

The repository is the durable content authority.  Database rows remain useful
query projections, but a Project revision is not publishable unless it is
backed by a Git commit.  Session worktrees isolate proposed changes; publishing
replays approved paths onto ``main`` as one linear commit instead of asking an
agent to resolve an arbitrary Git merge.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from app.services.harness_imports import HarnessNotImportable, ensure_harness_importable
from pathlib import Path, PurePosixPath
from typing import IO

import yaml

from app.config import data_root, settings
from app.services import frozen_register
from app.services.harness_contract import HarnessContractUnavailable, materials_module


log = logging.getLogger(__name__)


def _materials_if_available():
    """材料模块，取不到就返回 None。

    分档的理由：`materials` 有两类调用点，failure 的代价差一个数量级。

      · **本职工作**（上传落盘 `place`）—— 取不到就该硬失败，说清是 harness
        checkout 没配好。默默不做等于回执说"存好了"而盘上什么都没有。
      · **顺手修复**（`materialize` 补字节、`file_tree` 端出材料行）—— 这些挂在
        建会话、发布、列文件这些主路径上。让一个没配 `HARNESS_ROOT` 的部署
        连会话都开不出来，是把一道次要能力的缺席升级成主路径全损。

    所以这里给后一类一个出口，而且**不静默**：吵一声，让缺配置这件事看得见。
    """
    try:
        return materials_module()
    except HarnessContractUnavailable as exc:
        log.warning(
            "用户交来的文件这一层不可用（%s）—— 材料不会被补齐或列出。"
            "这台部署要配 HARNESS_ROOT 指向 harness checkout。", exc,
        )
        return None


def _chunk_paths_for_argv(paths: list[str], *, max_bytes: int = 200_000) -> Iterator[list[str]]:
    """把路径列表切成能安全放进一条命令行的批。

    checkpoint 的路径数量没有上限（见 `_validate_checkpoint_paths` 内注释），
    但 `git status/add -- <paths...>` 把路径拼进 argv，几千条就会顶到
    ARG_MAX。这是实现细节，不许变成用户可见的失败 —— 分批调用，git 的
    staging 与 status 都是可累积的，语义不变。
    """
    batch: list[str] = []
    batch_bytes = 0
    for path in paths:
        size = len(path.encode("utf-8")) + 1
        if batch and batch_bytes + size > max_bytes:
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(path)
        batch_bytes += size
    if batch:
        yield batch


PROJECT_SCHEMA_VERSION = 2
DEFAULT_BRANCH = "main"


def _refuse_the_event_loop_thread(operation: str) -> None:
    """仓库 I/O 不许跑在事件循环线程上 —— 慢要响，不许静默拖垮所有人。

    这个类的每个方法都是同步的（子进程 + 读盘），而它的调用方几乎全是 async
    处理器。此前的做法是"在调用点记得 `to_thread`"，那是一份名单：漏一处，
    整台后端就以那一处的耗时为量子停摆（2026-09-09 node20：会话列表 5 秒一
    轮询，每次 35 秒，摄取停了 13 分钟）。判据放在唯一的出口这里，漏掉的调用
    点当场报错，而不是变慢。合法的路只有一条：`run_in_repository_thread`。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ProjectRepositoryError(
        f"git {operation} was invoked on the event loop thread; "
        "repository I/O must go through run_in_repository_thread()"
    )


async def run_in_repository_thread(fn, /, *args, **kwargs):
    """在工作线程里跑一个仓库方法 —— async 调用方唯一的入口。"""
    return await asyncio.to_thread(fn, *args, **kwargs)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: 这台机器上一条路径能有多长。**Windows 是硬预算**：文件 260、目录 260−12=248
#: （`CreateDirectory` 要给目录里的 8.3 文件名留位置）。用模块级标志而不是现读
#: `os.name`，是为了让「Windows 上会怎样」在任何宿主上都测得了 —— 同
#: `core.data_provenance._WINDOWS` 的做法。
_WINDOWS = os.name == "nt"

#: 短名取多少位十六进制。48 bit：一个数据根里几万个会话的碰撞概率也在 1e-7
#: 量级，而**碰撞不会静默** —— 建工作树时会当场发现那里已经是别的会话
#: （见 `create_session_worktree` 的分支核对）。
_WORKTREE_DIR_HEX = 12


def _worktree_dir_name(identifier: str) -> str:
    r"""身份 → **工作树那棵树里**的目录名。

    ## 为什么可以改名

    目录是**地址**，不是身份。这条在本仓库早有明文（`core/worker_addressing.py`：
    「身份与地址是两回事：地址可以因为长度限制被挪走，身份不会变」），而工作树
    这边本来就是这么长的：lane 的身份从**分支名**现算（`open_child_lanes` 读
    `refs/heads/lane/<session>/<lane>`），没有一处从目录名反推 id。
    启动闸 `_refuse_to_serve_where_the_data_is_not` 比的也是**根**
    （`Path(p).parent.parent`），不是叶子名。

    ## 为什么必须改名（Windows）

    2026-09-10 真机（9800x3d，非管理员 Win11，`LongPathsEnabled=0`）：平台的会话
    在 init 那一步就崩，

        [WinError 206] The filename or extension is too long:
        …\afs\project-worktrees\<uuid36>\<uuid36>\.research\runtime\runs\
        orchestrator__<uuid36>__session__<uuid36>\artifacts        ← 255 字符

    255 > 248。同一个数据根里最深的文件已经 258。病根是**同一对 id 在这条路径里
    各出现两次**，光目录名就吃掉 226 字符里的 145。

    砍哪一段是量出来的、不是估的：run 目录下最深的尾巴只有 24
    （`messages_checkpoint.json`），工作树根下最深 ~92
    （`experiments/execution_envelope__sha256_<hex>.json`）。把工作树那
    两级从 36 砍到 12 就够（−48）：255→207、258→210，两头都留出余量。
    因此**不动** `orchestrator__<p>__session__<s>`（那是 run 的身份，改它要迁移
    存量），也不动仓库树。

    ## 为什么 POSIX 原样

    那里没有预算问题（4096），而给活着的工作树改名要 `git worktree repair`
    ——拿一次真实的迁移风险去换零收益。路径预算是 OS 的性质，这就是一条 OS
    接缝，和 `core.paths.default_home()` 同一形状。
    """
    if not _WINDOWS:
        return identifier
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:_WORKTREE_DIR_HEX]
_MIME_EXTENSIONS = {
    "application/json": ".json",
    "application/pdf": ".pdf",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/x-bibtex": ".bib",
    "text/x-tex": ".tex",
}
_ARTIFACT_DIRECTORIES = {
    "survey_report": "literature",
    "literature_index": "literature",
    "research_plan": "plans",
    "hypothesis_innovation_report": "plans",
    "hypothesis_research_overview": "plans",
    "pre_registration": "preregistrations",
    "experiment_log": "experiments",
    "clean_results": "experiments",
    "analysis_report": "analyses",
    "figure": "figures",
    "figure_package": "figures",
    "paper_outline": "manuscripts",
    "paper_draft": "manuscripts",
    "paper_tex": "manuscripts",
    "paper_pdf": "manuscripts",
    "manuscript": "manuscripts",
    "writing_preflight_plan": "manuscripts",
    "review_report": "reviews",
    "review_critique": "reviews",
    "dataset": "datasets",
    "data_profile": "datasets",
    "data_pipeline": "datasets",
    "code": "code",
}

# Stable Project v2 collaboration roots: node type → the directory it owns.
# Node packages remain free to choose the structure *inside* their own
# directory; the Platform owns only this top-level boundary and Git lifecycle.
#
# 这是 harness `core.project_workspace._NODE_WORKSPACES` 的**镜像**：后端进程不
# import harness core，所以建仓与 checkpoint 闸需要自己的一份。两份分叉的后果
# 是静默的（节点写盘被判越界，而报错指向"越界"不指向"两张表对不上"），
# `tests/test_node_workspaces_match_the_harness.py` 把两边钉在同一张表上。
NODE_WORKSPACES: dict[str, str] = {
    "literature": "literature",
    "hypothesis": "plan",
    "data": "data",
    "experiment": "experiments",
    "observation": "observation",
    # 推导：第三种证据模态（与 experiment 干预式、observation 检视式并列）。
    # 它是 producing 节点，产 derivation_log —— 有产物就要有自己的目录。
    "derivation": "derivation",
    "postprocess": "figures",
    "writing": "paper",
}
#: 架构节点：审稿人写 reviews/；调度器写 notes/（用户看得见的研究记录，
#: 装它写给用户的笔记、结论、快速出的图、编译出的 PDF）。调度器**没有私人
#: 抽屉**：草稿落 run 目录，`.research/orchestration/` 从此只装平台自己的
#: 会话/lane 记账。
SYSTEM_WORKSPACES: dict[str, str] = {
    "reviewer": "reviews",
    "orchestrator": "notes",
}
#: 目录 → 归谁。`_owner_of` 从这里反查，不各写一条 if。
_OWNER_BY_ROOT: dict[str, str] = {
    **{directory: node for node, directory in NODE_WORKSPACES.items()},
    **{directory: node for node, directory in SYSTEM_WORKSPACES.items()},
}


def _node_write_rules() -> str:
    """`access/nodes.yaml` 的节点写权限段 —— 从 NODE_WORKSPACES 派生。

    这一段原先在本文件里手抄了两份（建仓一份、修复一份），加上 NODE_WORKSPACES
    本身，三份名单回答同一个问题："这个项目有哪些节点目录"。

    2026-08-18 接入 observation 时现形：harness 侧的名单改了、这三份没改，于是
    仓库里根本没有 observation/ 目录 —— 而三份名单谁也不会因为对不上而报错。
    """
    return "".join(
        f"  {node}: {{write: [{directory}]}}\n"
        for node, directory in (*NODE_WORKSPACES.items(), *SYSTEM_WORKSPACES.items())
    )


def _nodes_yaml() -> str:
    """整份 `access/nodes.yaml` —— 建仓与 v1→v2 修复共用，不各抄一份。"""
    return (
        "schema_version: 1\ndefault_read: true\ndefault_write: false\n"
        "nodes:\n"
        + _node_write_rules()
        + "  memory_curator: {write: [MEMORY.md]}\n"
        "extensions:\n"
        "  root: runs/extensions\n"
        "  isolation: node_and_run\n"
        "platform:\n"
        "  git_authority: true\n"
        "  write: [" + ", ".join(sorted(PLATFORM_MANAGED_ROOTS)) + "]\n"
    )


PLATFORM_MANAGED_ROOTS = frozenset(
    {
        "project.yaml",
        "PROJECT.md",
        "MEMORY.md",
        "access",
        "resources",
        # 用户交来的原件（core.materials）：平台上传落这里，字节在池里、指针进 git。
        "sources",
        ".research",
        "runs",
    }
)


#: 一次目录列举最多返回多少行。到顶时**必须**在响应里说出来 —— 见 `file_tree`。
_MAX_LISTING_ENTRIES = 1_000


#: 节点工作区的根 —— 值域与 `core.project_workspace._NODE_WORKSPACES` 一致。
#: `_owner_of` 与 `_is_bookkeeping` 都从这里推，不各写一份。
_WORKSPACE_ROOTS = (
    *NODE_WORKSPACES.values(),
    *SYSTEM_WORKSPACES.values(),
    "MEMORY.md",
)


def _workspace_root_of(relative: str) -> str | None:
    """这条路径落在哪个节点的工作区根下（没有就是 None）。"""
    path = PurePosixPath(relative)
    for root in _WORKSPACE_ROOTS:
        candidate = PurePosixPath(root)
        if path == candidate or candidate in path.parents:
            return root
    return None


def _is_bookkeeping(relative: str) -> bool:
    """这条路径是**平台记账**，还是研究产出。

    前端原来自己按"路径里有没有以 `.` 开头的段"判。那条规则对 `.history/`
    `.research/ledger/` 都判对了，只有一个反例 —— 而那个反例恰好是
    用户 2026-09-09 找不到的那篇论文：调度器的工作区是 `.research/orchestration`，
    它**产出的东西**（包括 `latex_build/main.pdf`）于是整片被归进"平台记账"
    折叠起来。目录名带点是它所在位置的事，不是它是什么的事。

    判据改成：先把它所属的**节点工作区根**摘掉，剩下的部分再看有没有点开头
    的段。这样 `.research/orchestration/latex_build/main.pdf` 摘掉根之后是
    `latex_build/main.pdf` —— 研究产出；而 `paper/.drafts/x.md`（若有）
    摘掉 `paper` 之后仍带点开头的段 —— 记账。`.research/contracts/…` 不属于
    任何节点，整条来判 —— 记账。

    放在这里而不是前端，是因为"哪些目录是节点工作区"在这一侧已经有真相源
    （`NODE_WORKSPACES`）。界面自己拿路径形状去猜，就是第二份会分叉的判据。
    """
    root = _workspace_root_of(relative)
    remainder = (
        PurePosixPath(relative).relative_to(root).as_posix() if root else relative
    )
    if remainder in (".", ""):
        return False
    return any(part.startswith(".") for part in PurePosixPath(remainder).parts)


#: 建仓样板 README 的开头 —— 与 `core.loop_hooks_builtin._BOILERPLATE_README`
#: 认的是同一段文字。那边把它从"节点自述"里滤掉，这边不再生成它。
_OWNER_BOILERPLATE = re.compile(
    r"\A# [^\n]+\n\nOwner: `[\w_]+`\.\n(\nThe owner controls the internal layout\.)?"
)


def _is_untouched_owner_boilerplate(path: Path) -> bool:
    """这份 README 还是建仓那天写下的样板，一个字没改过。"""
    try:
        return bool(_OWNER_BOILERPLATE.match(path.read_text(encoding="utf-8")))
    except OSError:
        return False


#: 根这一层的顺序 = 研究流程的顺序，从 `NODE_WORKSPACES` 派生。
#:
#: 前端原来自己拿一份 `ROOT_ORDER` 名单排。2026-09-10 真机看出来：那份名单
#: 漏了 `derivation` 和 `observation`（后加的两个节点），于是这两个节点目录
#: 跟 `CODEOWNERS` `PROJECT.md` 混在一起排到了最前面 —— 又是"写名单，新东西
#: 默认漏过"。顺序跟着**节点表**走，加节点自动排对。
_PIPELINE_ORDER = {
    name: index
    for index, name in enumerate(
        (*NODE_WORKSPACES.values(), *SYSTEM_WORKSPACES.values(),
         "sources", "runs", "resources", "access", ".research")
    )
}


def _listing_order(row: dict) -> tuple:
    """同一层里的顺序：先按流程位次，名单外的（散文件、新目录）排在后面。

    **排序不是过滤**：位次表里没有的照样列出来，只是排在节点目录之后。
    """
    name = str(row.get("name") or "")
    return (_PIPELINE_ORDER.get(name, len(_PIPELINE_ORDER)), name)


def _owner_of(relative: str) -> str:
    """这条路径（文件或目录）归谁 —— 目录和文件由**同一个**函数回答。

    以前有两份：建仓写 README 时一份、`file_tree` 里另有一条 if/elif 链。两份
    都在回答"这个东西归谁"，而只有后者认得 `MEMORY.md` 和
    `runs/extensions/<node>/` —— 两份名单迟早分叉，且分叉不报错
    （[[feedback_one_truth_source_per_question]]）。
    """
    parts = PurePosixPath(relative).parts
    if not parts:
        return "platform"
    owner = _OWNER_BY_ROOT.get(parts[0])
    if owner is not None:
        return owner
    if relative == "MEMORY.md":
        return "memory_curator"
    if len(parts) >= 3 and parts[:2] == ("runs", "extensions"):
        return parts[2]
    # `.research/orchestration/` 里只剩平台写的会话/lane 记账 —— 归平台。
    return "platform"


def _the_file_lock():
    """跨进程锁的机制在 harness 侧一处回答（`shared.lib.filelock`）；后端 import
    真的那份，不复制。找不到 harness 就按仓库自己的错误类型说清楚。"""
    try:
        ensure_harness_importable()
        from shared.lib import filelock
    except (HarnessNotImportable, ImportError) as exc:
        raise ProjectRepositoryError(
            "Cross-process Project locking needs the harness checkout "
            "(HARNESS_ROOT) to be importable"
        ) from exc
    return filelock


class ProjectRepositoryError(RuntimeError):
    """A repository invariant or Git operation failed."""


class ProjectFileTooLargeError(ProjectRepositoryError):
    """The file exists but is too big to hand to a browser in one response.

    分出一个子类是因为调用方要能把它和"文件不存在"分开：两者都是
    `ProjectRepositoryError`，但一个该回 404、一个该回 413 并把上限告诉用户。
    合成一种错误的话，界面只能说"打不开"，而用户下一步该做什么完全不同。
    """

    def __init__(self, message: str, *, size_bytes: int, max_bytes: int) -> None:
        super().__init__(message)
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes


class ChildLaneConflictError(ProjectRepositoryError):
    """两条 lane 改了同一个文件 —— **不自动解**（RFC D5）。

    六节点目录所有制保证正常情况下零冲突，所以真冲突是"所有制被破坏了"的
    信号，不是一次需要调和的合并。自动解冲突就是「无害的两段能组合出销毁」
    的那个形状：每一步看起来都合理，合起来把一份研究产物改成了谁也没写过的
    样子，而且没人会知道。

    所以这里只做一件事：**吵**，并且指名是哪几个文件。重跑、放弃、还是上报，
    由调度器决定。
    """

    def __init__(self, message: str, *, lane_id: str, paths: Sequence[str]) -> None:
        super().__init__(message)
        self.lane_id = lane_id
        self.paths = tuple(paths)


@dataclass(frozen=True, slots=True)
class RepositoryStatus:
    project_id: str
    branch: str
    head_commit: str
    clean: bool
    path: str


@dataclass(frozen=True, slots=True)
class SessionWorkspace:
    project_id: str
    session_id: str
    branch: str
    base_commit: str
    head_commit: str
    path: str
    ahead_by: int
    behind_by: int
    clean: bool


@dataclass(frozen=True, slots=True)
class RevisionWrite:
    commit_sha: str
    repository_path: str
    branch: str
    additions: int
    deletions: int


@dataclass(frozen=True, slots=True)
class RepositoryDiff:
    patch: str
    additions: int
    deletions: int
    files_changed: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ChildLane:
    """一条**子 lane**：派给一个子节点的独立 Git 工作树 + 分支（RFC D4/D5）。

    lane 存在的理由是所有制：一个 Session worktree 只有一条 mutation lane，
    于是"派活之后立刻返回"这件事在 Git 层面做不到 —— 后台 child 会与父节点、
    与平台 checkpoint 并发改同一棵树。给它自己的树，那个冲突就不存在了。
    """

    project_id: str
    session_id: str
    lane_id: str
    node_type: str
    branch: str
    path: str
    #: 开 lane 那一刻 session 分支的 HEAD。merge 与权限判定都从它现算。
    base_commit: str


@dataclass(frozen=True, slots=True)
class LaneMerge:
    """一次 lane 落地的结果。"""

    lane_id: str
    branch: str
    #: None = 这条 lane 一个字都没改（合法：子节点跑了但没产出文件）。
    merge_commit: str | None
    session_head: str
    #: 这次带进 session 分支的路径。
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckpointSelection:
    """一次 checkpoint 校验后的取舍：进库的路径，以及两类**不适合进 Git**的排除。"""

    paths: list[str]
    oversized: tuple[tuple[str, int], ...]
    bulk: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class WorkspaceCheckpoint:
    commit_sha: str | None
    branch: str
    paths: tuple[str, ...]
    status: str
    audit_ref: str | None = None
    #: P6：冻结路径守卫的处置记录。(path, action) —— action ∈
    #: "restored"（从 HEAD 恢复了被改动的冻结内容，可验证）|
    #: "refused"（HEAD 内容对不上登记 hash，只把改动挡在 commit 外）
    frozen_violations: tuple[tuple[str, str], ...] = ()
    #: 因为超过 Git blob 上限而**排除出本次 checkpoint** 的文件 (path, bytes)。
    #: 不是违规，是"这个文件不适合进 Git"——见 _validate_checkpoint_paths。
    oversized_excluded: tuple[tuple[str, int], ...] = ()
    #: 因为**整个目录**超过 checkpoint 预算而排除的目录 (dir, bytes, files)。
    #: 数据集的单位是目录 —— 见 _validate_checkpoint_paths。
    bulk_excluded: tuple[tuple[str, int, int], ...] = ()


def _yaml_scalar(value: object) -> str:
    """JSON scalars are valid YAML scalars and avoid an unsafe YAML emitter."""
    return json.dumps(value, ensure_ascii=False)


def _yaml_mapping(values: dict[str, object]) -> str:
    lines = []
    for key, value in values.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {_yaml_scalar(item)}" for item in value)
        elif value is None:
            lines.append(f"{key}: null")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


def _artifact_type_policy_yaml() -> str:
    """Optional semantic publication policy, separate from node handoff.

    Project v2 does not use typed artifacts to move files between nodes.  This
    policy remains for explicit user/API Artifact candidates whose publication
    may require receipts, such as a manuscript release.
    """
    project_types = (
        "survey_report",
        "analysis_report",
        "experiment_log",
        "research_plan",
        "paper_draft",
        "paper_tex",
        "paper_pdf",
        "paper_outline",
        "review_report",
        "code",
        "figure",
        "table",
        "report",
        "data_profile",
        "data_pipeline",
        "dataset",
        "other",
        "manuscript",
        "pre_registration",
        "writing_validation_report",
        "review_critique",
        "clean_results",
        "figure",
        "hypothesis_innovation_report",
        "hypothesis_research_overview",
        "literature_index",
        "writing_preflight_plan",
    )
    no_receipt = {
        "code",
        "other",
        "report",
        "review_critique",
        "review_report",
        "writing_validation_report",
        "writing_preflight_plan",
    }
    types = {
        artifact_type: {
            "ownership_source": "user_or_legacy_gateway",
            "required_receipts": ([] if artifact_type in no_receipt else ["producer_validation"]),
        }
        for artifact_type in project_types
    }
    types["manuscript"]["required_receipts"] = [
        "writing_validation",
        "review_approval",
    ]
    types["paper_pdf"]["required_receipts"] = [
        "writing_validation",
        "review_approval",
    ]
    for artifact_type in (
        "paper",
        "documentation",
        "benchmark",
        "reference_code",
        "news_article",
        "lab_update",
    ):
        types[artifact_type] = {
            "ownership_source": "user",
            "required_receipts": [],
        }
    return yaml.safe_dump(
        {
            "schema_version": 2,
            "default": "deny",
            "node_ownership_source": "project_directories",
            "types": types,
        },
        allow_unicode=True,
        sort_keys=False,
    )


class GitProjectRepository:
    """All filesystem and Git mutations for one platform Project boundary."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, repository_root: Path | None = None, worktree_root: Path | None = None):
        self.repository_root = (repository_root or data_root("repositories")).resolve()
        self.worktree_root = (worktree_root or data_root("worktrees")).resolve()
        self.git = settings.project_git_executable

    @classmethod
    def _lock(cls, project_id: str) -> threading.RLock:
        with cls._locks_guard:
            return cls._locks.setdefault(project_id, threading.RLock())

    @contextmanager
    def _project_lock(self, project_id: str) -> Iterator[None]:
        """Serialize canonical mutations across threads and worker processes.

        跨进程锁的**机制**在 harness 侧一处回答（`shared.lib.filelock`：POSIX
        `flock` / Windows `msvcrt.locking`，两边都真锁）；后端不复制一份。
        """
        project_id = self._safe_id(project_id, "project id")
        lock_directory = self.repository_root / ".locks"
        lock_directory.mkdir(parents=True, exist_ok=True)
        lock_path = lock_directory / f"{project_id}.lock"
        filelock = _the_file_lock()
        with self._lock(project_id), lock_path.open("a+") as handle:
            filelock.acquire(handle)
            try:
                yield
            finally:
                filelock.release(handle)

    @staticmethod
    def _safe_id(value: str, label: str) -> str:
        candidate = str(value).strip()
        if not _SAFE_ID.fullmatch(candidate):
            raise ProjectRepositoryError(f"Unsafe {label}: {value!r}")
        return candidate

    def project_path(self, project_id: str) -> Path:
        # 仓库树**不**走短名：它浅（`<根>/<id>`），够不着预算；而它装的是
        # 项目的持久数据，改名等于把既有项目挪走。短名只用在够得着预算的
        # 那棵树上，理由写在 `_worktree_dir_name`。
        return self.repository_root / self._safe_id(project_id, "project id")

    def session_path(self, project_id: str, session_id: str) -> Path:
        return (
            self.worktree_root
            / _worktree_dir_name(self._safe_id(project_id, "project id"))
            / _worktree_dir_name(self._safe_id(session_id, "session id"))
        )

    def session_branch(self, session_id: str) -> str:
        return f"session/{self._safe_id(session_id, 'session id')}"

    # ── 子 lane（RFC 异步运行时 D4 / D5）────────────────────────────────────
    #
    # ## 为什么 lane 原语长在这里，而不是"调度器侧机械 merge"
    #
    # RFC 的 D5 写的是「子分支跑完 → **调度器侧**机械 merge 回 session 分支」。
    # 核对代码之后那句不成立：**worker 全程不碰 git**。节点写完文件只发一条
    # `workspace_checkpoint_requested`（core/project_workspace.py），真正 commit
    # 的是 App Server —— Git 写权限从设计上就只在平台这一侧
    # （`checkpoint_session_workspace` 的 docstring 原话："The node reports paths
    # but never receives Git authority."）。
    #
    # 所以 lane 的三个动作（开 / 落 / 合）都必须是**平台**的动作，由调度器
    # 经协议请求。这不是实现选择，是既有的权限边界，改它等于让节点能改历史。
    #
    # ## lane 的注册表就是 Git 自己
    #
    # 不另存一份"当前有哪些 lane"。`git worktree list` + 分支名就是答案，
    # 而它与磁盘上的事实**不可能**分叉（那正是另存一份必然带来的东西）。

    #: lane 分支/工作树的命名。两处都从这里取。
    #:
    #: ⚠️ **不能**叫 `session/<sid>/lane/<lid>` —— Git 的 ref 名是目录：
    #: `refs/heads/session/s1` 存在时就不可能再有 `refs/heads/session/s1/lane/l1`
    #: （`cannot lock ref … 'refs/heads/session/s1' exists`）。第一版就是这么
    #: 写的，十六条测试一起红。这类事只有真 git 说了算，替身测不出来。
    def lane_branch(self, session_id: str, lane_id: str) -> str:
        return (
            f"lane/{self._safe_id(session_id, 'session id')}"
            f"/{self._safe_id(lane_id, 'lane id')}"
        )

    def lane_path(self, project_id: str, session_id: str, lane_id: str) -> Path:
        """lane 工作树的位置 —— **session 工作树之外**。

        放进 session 树里面，Git 会把整棵 lane 当成 session 的未跟踪垃圾，
        而 checkpoint 的脏路径枚举是 `--untracked-files=all`。
        """
        return (
            self.worktree_root
            / _worktree_dir_name(self._safe_id(project_id, "project id"))
            / f"{_worktree_dir_name(self._safe_id(session_id, 'session id'))}__lane__"
              f"{_worktree_dir_name(self._safe_id(lane_id, 'lane id'))}"
        )

    def open_child_lanes(self, project_id: str, session_id: str) -> tuple[ChildLane, ...]:
        """这个 session 此刻开着哪些 lane —— **从 Git 现算**。"""
        repo = self.project_path(project_id)
        prefix = f"lane/{self._safe_id(session_id, 'session id')}/"
        raw = self._git(repo, "worktree", "list", "--porcelain", check=False)
        lanes: list[ChildLane] = []
        path = ""
        for line in raw.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.startswith("branch "):
                ref = line[len("branch "):].strip()
                branch = ref.removeprefix("refs/heads/")
                if not branch.startswith(prefix):
                    continue
                lane_id = branch[len(prefix):]
                lanes.append(
                    ChildLane(
                        project_id=project_id,
                        session_id=session_id,
                        lane_id=lane_id,
                        node_type=self._lane_node_type(Path(path), branch),
                        branch=branch,
                        path=path,
                        base_commit=self._git(
                            repo, "merge-base", self.session_branch(session_id), branch,
                            check=False,
                        ),
                    )
                )
        return tuple(lanes)

    def _lane_node_type(self, path: Path, branch: str) -> str:
        """lane 是给哪个节点开的 —— 记在**开 lane 那条提交**的 trailer 里。

        不另存一个 registry 文件：那会是第二个真相源，而它一定会和 Git 分叉
        （worktree 被手工删掉、分支被改名…）。trailer 与分支同生共死。
        """
        raw = self._git(path, "log", "-1", "--format=%B", branch, check=False)
        for line in raw.splitlines():
            if line.startswith("Lane-Node-Type:"):
                return line.split(":", 1)[1].strip()
        return ""

    def open_child_lane(
        self,
        *,
        project_id: str,
        session_id: str,
        lane_id: str,
        node_type: str,
    ) -> ChildLane:
        """给一个子节点开一条 lane。已经开着同一条（同 id 同节点）→ 原样返回。

        ## 同节点并行机械禁令（RFC D5）

        同一个 node_type 已经有 lane 开着就拒绝：两条 lane 会改同一个节点目录，
        合第二条**必然**冲突。这件事判据能机械回答，就不交给模型 —— 让它在
        派发那一刻就撞墙，比让它跑完一小时再在 merge 处炸掉便宜得多。
        """
        project_id = self._safe_id(project_id, "project id")
        session_id = self._safe_id(session_id, "session id")
        lane_id = self._safe_id(lane_id, "lane id")
        node = re.sub(r"[^A-Za-z0-9._-]+", "-", str(node_type)).strip("-.")[:64]
        if not node:
            raise ProjectRepositoryError("Child lane needs a node type")
        repo = self.project_path(project_id)
        branch = self.lane_branch(session_id, lane_id)
        path = self.lane_path(project_id, session_id, lane_id)
        with self._project_lock(project_id):
            session = self.session_status(project_id, session_id)
            for existing in self.open_child_lanes(project_id, session_id):
                if existing.lane_id == lane_id:
                    if existing.node_type != node:
                        raise ProjectRepositoryError(
                            f"Lane {lane_id} already belongs to node "
                            f"{existing.node_type!r}, not {node!r}"
                        )
                    return existing
                if existing.node_type == node:
                    raise ProjectRepositoryError(
                        f"{node} 已经有一条在跑的 lane（{existing.lane_id}）——"
                        "同一个节点不能并行两条：两条都会改同一个节点目录，"
                        "合第二条必然冲突。等它落地，或者先放弃它。"
                    )
            base = session.head_commit
            path.parent.mkdir(parents=True, exist_ok=True)
            self._git(repo, "worktree", "add", "-b", branch, str(path), base)
            self._configure_identity(path)
            # 开 lane 本身留一条提交：它带着 `Lane-Node-Type` trailer，于是
            # "这条 lane 是给谁开的"与分支同生共死，不需要第二份记录。
            self._write(
                path,
                f".research/orchestration/lanes/{lane_id}.yaml",
                _yaml_mapping(
                    {
                        "schema_version": 1,
                        "lane_id": lane_id,
                        "session_id": session_id,
                        "node_type": node,
                        "base_commit": base,
                    }
                ),
            )
            self._git(path, "add", f".research/orchestration/lanes/{lane_id}.yaml")
            self._git(
                path, "commit", "-m", f"lane: open {lane_id} for {node}",
                "-m", f"Lane-Node-Type: {node}\nSession-ID: {session_id}\n"
                      f"Base-Commit: {base}",
            )
            return ChildLane(
                project_id=project_id,
                session_id=session_id,
                lane_id=lane_id,
                node_type=node,
                branch=branch,
                path=str(path),
                base_commit=base,
            )

    #: 只有一个写者的共享文件：它们不属于任何节点目录，所以所有制保证不了
    #: 零冲突。lane 不许带着它们落地 —— 改动要走 proposal，由调度器单写
    #: （与 KB「无直接写路径」同构，RFC D5）。
    SHARED_SINGLE_WRITER_PATHS: tuple[str, ...] = ("MEMORY.md",)

    def merge_child_lane(
        self, *, project_id: str, session_id: str, lane_id: str
    ) -> LaneMerge:
        """把一条 lane 落回 session 分支。冲突**不解**，抛 `ChildLaneConflictError`。"""
        project_id = self._safe_id(project_id, "project id")
        session_id = self._safe_id(session_id, "session id")
        lane_id = self._safe_id(lane_id, "lane id")
        repo = self.project_path(project_id)
        branch = self.lane_branch(session_id, lane_id)
        lane_root = self.lane_path(project_id, session_id, lane_id)
        with self._project_lock(project_id):
            if not lane_root.is_dir():
                raise ProjectRepositoryError(f"Child lane is not open: {lane_id}")
            session = self.session_status(project_id, session_id)
            root = Path(session.path)
            # 没 checkpoint 就来 merge = 子节点的活还在工作区里没进 Git。
            # 这里**不替它 commit**：谁写的谁负责入库，代提交等于把"这段产物
            # 属于哪个 run"这件事变成猜的。
            if self._git(lane_root, "status", "--porcelain=v1"):
                raise ProjectRepositoryError(
                    f"Lane {lane_id} has uncommitted work — checkpoint it before merging"
                )
            merge_base = self._git(repo, "merge-base", session.branch, branch)
            lane_head = self._git(lane_root, "rev-parse", "HEAD")
            changed = tuple(
                line for line in self._git(
                    repo, "diff", "--name-only", f"{merge_base}..{lane_head}"
                ).splitlines() if line.strip()
            )
            # 开 lane 那条自己的登记文件不算产出。
            changed = tuple(
                path for path in changed
                if path != f".research/orchestration/lanes/{lane_id}.yaml"
            )
            if not changed:
                return LaneMerge(
                    lane_id=lane_id, branch=branch, merge_commit=None,
                    session_head=session.head_commit, paths=(),
                )
            shared = tuple(p for p in changed if p in self.SHARED_SINGLE_WRITER_PATHS)
            if shared:
                raise ChildLaneConflictError(
                    f"Lane {lane_id} changed shared single-writer file(s): "
                    + ", ".join(shared)
                    + " —— 这些文件不属于任何节点目录，所有制保证不了零冲突。"
                    "改动要随完成回报交 proposal，由调度器单写落盘。",
                    lane_id=lane_id, paths=shared,
                )
            if self._git(root, "status", "--porcelain=v1"):
                raise ProjectRepositoryError(
                    "Session worktree is dirty — merge needs a clean lane to land on"
                )
            # ⚠️ merge commit **必须带 `Session-ID` trailer**（RFC D6）。
            #
            # `_commits_not_made_by_platform` 认的是"平台身份 + 这个 session 的
            # trailer"两条一起。少了 trailer，平台自己做的 merge 会被自己的权威
            # 判据当成外人 → 下一次 checkpoint fail-closed → **会话再也自己好
            # 不了**。这个症状是老朋友（2026-08-13 E2E v26，一条 run 在 8.9 小时
            # 模拟跑完之前就死了），D6 就是为它写的。
            #
            # 同理：lane 里的**每一条**提交都要带它，否则 merge 会把一段没有
            # trailer 的历史接进 session 分支，从此这条会话的权威链永久带毒。
            # 节点侧那条由 `checkpoint_session_workspace` 保证（它一直在写）。
            rc, _ = self._git_exit_code(
                root, "merge", "--no-ff", "--no-edit",
                "-m", f"lane: merge {lane_id} ({branch})\n\n"
                      f"Lane-ID: {lane_id}\nSession-ID: {session_id}",
                branch,
            )
            if rc != 0:
                conflicted = tuple(
                    line for line in self._git(
                        root, "diff", "--name-only", "--diff-filter=U", check=False
                    ).splitlines() if line.strip()
                )
                self._git(root, "merge", "--abort", check=False)
                raise ChildLaneConflictError(
                    f"Lane {lane_id} conflicts with the Session branch on: "
                    + ", ".join(conflicted or ("<unknown>",))
                    + " —— 六节点目录所有制保证正常情况下零冲突，所以这是"
                    "所有制被破坏了的信号。不自动解：重跑、放弃还是上报，由调度器定。",
                    lane_id=lane_id, paths=conflicted,
                )
            return LaneMerge(
                lane_id=lane_id, branch=branch,
                merge_commit=self._git(root, "rev-parse", "HEAD"),
                session_head=self._git(root, "rev-parse", "HEAD"),
                paths=changed,
            )

    def close_child_lane(
        self, *, project_id: str, session_id: str, lane_id: str, delete_branch: bool = True
    ) -> None:
        """撤掉一条 lane 的工作树（默认连分支一起删）。

        `delete_branch=False` 用于**放弃**：树撤掉、分支留着，那段工作还能被
        人翻出来。「只留证据不判决」—— 放弃一条 lane 不该顺手销毁它的历史。
        """
        project_id = self._safe_id(project_id, "project id")
        session_id = self._safe_id(session_id, "session id")
        lane_id = self._safe_id(lane_id, "lane id")
        repo = self.project_path(project_id)
        path = self.lane_path(project_id, session_id, lane_id)
        branch = self.lane_branch(session_id, lane_id)
        with self._project_lock(project_id):
            if path.exists():
                self._git(repo, "worktree", "remove", "--force", str(path), check=False)
            self._git(repo, "worktree", "prune", check=False)
            if delete_branch:
                self._git(repo, "branch", "-D", branch, check=False)

    def _git(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> str:
        _refuse_the_event_loop_thread(args[0] if args else "git")
        try:
            result = subprocess.run(
                [self.git, *args],
                cwd=cwd,
                env={**os.environ, **(env or {})},
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            operation = args[0] if args else "git"
            raise ProjectRepositoryError(f"Git operation failed: {operation}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-1500:]
            raise ProjectRepositoryError(
                f"git {' '.join(args)} failed ({result.returncode}): {detail}"
            )
        return result.stdout.strip()

    #: 未跟踪的瞬时文件后缀 —— 内容无关，不该 wedge 整条 stage→publish 交付链。
    #: 2026-08-23 实测（E2E v30）：一个残留 `MEMORY.md.lock` 让 stage_artifact_candidate
    #: 与 receipt 写入全部 503 "Session worktree has uncommitted changes"，referee 已批
    #: 的 manuscript/prereg/results 一个都发不出去，Artifacts 面永远空。锁文件是写者
    #: 崩溃/被打断留下的墓碑，绝不是待发布内容。
    _TRANSIENT_UNTRACKED_SUFFIXES = (".lock",)

    def _worktree_has_blocking_changes(self, root: Path) -> bool:
        """worktree 是否有**该拦住写入**的未提交改动。

        跟踪文件的任何改动（modified/added/deleted/renamed）一律拦 —— 那是真的
        完整性风险。**只**放行未跟踪且命中 `_TRANSIENT_UNTRACKED_SUFFIXES` 的文件
        （扫盘式判断：命中后缀才放，其余一律拦，不写“允许名单”反向漏）。
        """
        status = self._git(root, "status", "--porcelain")
        if not status:
            return False
        for line in status.splitlines():
            if not line.strip():
                continue
            code = line[:2]
            path = line[3:].strip().strip('"')
            if code == "??" and path.endswith(self._TRANSIENT_UNTRACKED_SUFFIXES):
                continue
            return True
        return False

    @staticmethod
    def _write(root: Path, relative: str, content: str) -> None:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _configure_identity(self, path: Path) -> None:
        self._git(path, "config", "user.name", "Research Platform")
        self._git(path, "config", "user.email", "research-platform@localhost")
        self._git(path, "config", "commit.gpgsign", "false")

    def initialize_project(
        self,
        *,
        project_id: str,
        name: str,
        description: str | None,
        research_domain: str | None,
        owner_id: str,
    ) -> RepositoryStatus:
        """Create the fixed Project schema and its genesis commit, idempotently."""
        project_id = self._safe_id(project_id, "project id")
        destination = self.project_path(project_id)
        with self._project_lock(project_id):
            if (destination / ".git").is_dir():
                self._migrate_existing_project_v2(
                    destination,
                    project_id=project_id,
                    name=name,
                    description=description,
                    research_domain=research_domain,
                    owner_id=owner_id,
                )
                return self.status(project_id)
            if destination.exists() and any(destination.iterdir()):
                raise ProjectRepositoryError(
                    f"Project repository path is non-empty but is not Git: {destination}"
                )
            self.repository_root.mkdir(parents=True, exist_ok=True)
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{project_id}.init-", dir=self.repository_root)
            )
            try:
                self._git(temporary, "init", "-b", DEFAULT_BRANCH)
                self._configure_identity(temporary)
                self._write(
                    temporary,
                    "project.yaml",
                    _yaml_mapping(
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "project_id": project_id,
                            "title": name,
                            "description": description,
                            "research_domain": research_domain,
                            "status": "active",
                            "default_branch": DEFAULT_BRANCH,
                            "publication_mode": "interactive",
                        }
                    ),
                )
                self._write(
                    temporary,
                    "PROJECT.md",
                    f"# {name}\n\n"
                    f"{description or 'Git-native scientific research project.'}\n\n"
                    "## Research objective\n\n"
                    "Define the falsifiable objective for this Project.\n\n"
                    "## Scope and constraints\n\n"
                    "Record scientific, ethical, budget, and time constraints.\n\n"
                    "## Acceptance criteria\n\n"
                    "Describe the evidence required to consider the Project complete.\n",
                )
                self._write(
                    temporary,
                    "MEMORY.md",
                    "# Project memory\n\n"
                    "This is the canonical, curated memory for the research Project. "
                    "Keep entries concise and evidence-linked; compact superseded entries "
                    "instead of creating a second memory authority.\n\n"
                    "## Goals\n\n- Establish the Project's measurable research goals.\n\n"
                    "## Decisions\n\n"
                    "<!-- YYYY-MM-DD | decision | rationale | evidence/commit -->\n\n"
                    "## Findings\n\n"
                    "<!-- YYYY-MM-DD | finding | confidence | evidence path -->\n\n"
                    "## Constraints\n\n"
                    "<!-- stable constraints that affect research execution -->\n\n"
                    "## Open questions\n\n"
                    "<!-- unresolved questions and the next evidence needed -->\n",
                )
                self._write(
                    temporary,
                    "CODEOWNERS",
                    "* @project-leads\n/access/ @project-admins\n",
                )
                self._write(
                    temporary,
                    ".gitattributes",
                    "*.pdf diff=pdf\n*.png diff=image\n*.jpg diff=image\n"
                    "*.json text eol=lf\n*.yaml text eol=lf\n",
                )
                self._write(
                    temporary,
                    ".gitignore",
                    # `.research/runtime/` 不进版本，但**不是缓存**：一次研究
                    # 到底发生了什么（transcript / events / checkpoint / 工具
                    # 原文）全部在那下面，没有任何一步能把它重算回来。
                    # `.research/cache/` 是它 2026-08-27 之前的名字，存量会话
                    # 还在用，所以两条都要忽略。
                    ".research/runtime/\n.research/cache/\n.research/locks/\n"
                    "**/__pycache__/\n**/.pytest_cache/\n**/.venv/\n"
                    "**/build/\n**/cache/\n"
                    "*.tmp\n*.swp\n.DS_Store\n",
                )
                self._write(
                    temporary,
                    "access/members.yaml",
                    "schema_version: 1\nmembers:\n"
                    f'  - user_id: {_yaml_scalar(owner_id)}\n    role: "lead"\n',
                )
                self._write(
                    temporary,
                    "access/roles.yaml",
                    "schema_version: 1\n"
                    "roles:\n"
                    "  lead: [read, propose, review, merge, administer]\n"
                    "  researcher: [read, propose]\n"
                    "  reviewer: [read, review]\n"
                    "  viewer: [read]\n",
                )
                self._write(
                    temporary,
                    "access/artifact-types.yaml",
                    _artifact_type_policy_yaml(),
                )
                self._write(temporary, "access/nodes.yaml", _nodes_yaml())
                for directory in (
                    *NODE_WORKSPACES.values(),
                    *SYSTEM_WORKSPACES.values(),
                    "runs",
                    ".research/orchestration/sessions",
                    ".research/releases",
                    ".research/migrations",
                ):
                    # 目录靠 `.gitkeep` 占位，**不**再写一份 README 所有权样板。
                    #
                    # 那段样板（"Owner: `x`. The owner controls…"）是这个仓库里
                    # 第三份"哪些目录归谁"的名单 —— 前两份是 `NODE_WORKSPACES`
                    # 和 `access/nodes.yaml`，而只有后者是真的被执行的那一份
                    # （checkpoint 读它判越界）。三份回答同一个问题，改一处不会
                    # 让另外两处报错。
                    #
                    # 而且它本来就没人看：orientation hook 里的
                    # `_BOILERPLATE_README` 专门把这一句滤掉 —— E2E v13 的结论是
                    # "每个节点都挂着一句 Owner: `x`.，零信息量，还掩盖了'这个
                    # 节点还没写自述'"。框架一边生成它、一边过滤它。
                    #
                    # 节点想写自述照样可以写 `README.md`，那时它才**是**自述。
                    self._write(temporary, f"{directory}/.gitkeep", "")
                self._write(temporary, ".research/schema-version", f"{PROJECT_SCHEMA_VERSION}\n")
                self._write(
                    temporary,
                    ".research/repository.yaml",
                    "schema_version: 2\nauthority: platform_git\n"
                    "session_isolation: worktree\ncheckpoint: every_run_outcome\n"
                    "interactive_publish: manual\ncontinuous_publish: automatic\n"
                    "artifact_transport: disabled\n",
                )
                self._git(temporary, "add", "--all")
                self._git(
                    temporary,
                    "commit",
                    "-m",
                    "project: initialize research repository",
                    "-m",
                    f"Project-ID: {project_id}\nSchema-Version: {PROJECT_SCHEMA_VERSION}",
                )
                if destination.exists():
                    destination.rmdir()
                temporary.replace(destination)
                temporary = destination
                return self.status(project_id)
            except Exception:
                if temporary.exists() and temporary != destination:
                    shutil.rmtree(temporary, ignore_errors=True)
                raise

    def _migrate_existing_project_v2(
        self,
        root: Path,
        *,
        project_id: str,
        name: str,
        description: str | None,
        research_domain: str | None,
        owner_id: str,
    ) -> None:
        """Upgrade a v1 Project without discarding historical Git content."""
        marker = root / ".research/schema-version"
        try:
            current = int(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            current = 1
        if current >= PROJECT_SCHEMA_VERSION and (root / "project.yaml").is_file():
            return
        if self._git(root, "status", "--porcelain"):
            raise ProjectRepositoryError(
                "Cannot migrate a canonical Project repository with uncommitted changes"
            )

        legacy_root = root / ".research/legacy/v1"
        legacy_root.mkdir(parents=True, exist_ok=True)
        memory_entries: list[str] = []
        old_memory = root / "memory"
        if old_memory.is_dir():
            for item in sorted(path for path in old_memory.rglob("*") if path.is_file()):
                if item.name == ".gitkeep":
                    continue
                try:
                    content = item.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                if content:
                    memory_entries.append(
                        f"- Migrated from `{item.relative_to(root).as_posix()}`: "
                        f"{content[:2000].replace(chr(10), ' ')}"
                    )

        for legacy in ("PROJECT.yaml", "README.md"):
            source = root / legacy
            if source.exists():
                target = legacy_root / legacy
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
        for legacy in ("memory", "artifacts", "documents", "receipts", "sessions", "workflows"):
            source = root / legacy
            if source.exists():
                target = legacy_root / legacy
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
        for legacy in ("node-capabilities.yaml", "artifact-types.yaml", "branch-protection.yaml"):
            source = root / "access" / legacy
            if source.exists():
                target = legacy_root / "access" / legacy
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))

        self._write(
            root,
            "project.yaml",
            _yaml_mapping(
                {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "project_id": project_id,
                    "title": name,
                    "description": description,
                    "research_domain": research_domain,
                    "status": "active",
                    "default_branch": DEFAULT_BRANCH,
                    "publication_mode": "interactive",
                }
            ),
        )
        self._write(
            root,
            "PROJECT.md",
            f"# {name}\n\n{description or 'Git-native scientific research project.'}\n\n"
            "## Research objective\n\nDefine the falsifiable objective.\n\n"
            "## Scope and constraints\n\nRecord the Project constraints.\n\n"
            "## Acceptance criteria\n\nDefine the required evidence.\n",
        )
        self._write(
            root,
            "MEMORY.md",
            "# Project memory\n\n## Goals\n\n## Decisions\n\n## Findings\n\n"
            + ("\n".join(memory_entries) if memory_entries else "<!-- no migrated entries -->")
            + "\n\n## Constraints\n\n## Open questions\n",
        )
        self._write(root, "access/nodes.yaml", _nodes_yaml())
        if not (root / "access/members.yaml").is_file():
            self._write(
                root,
                "access/members.yaml",
                "schema_version: 1\nmembers:\n"
                f'  - user_id: {_yaml_scalar(owner_id)}\n    role: "lead"\n',
            )
        if not (root / "access/roles.yaml").is_file():
            self._write(
                root,
                "access/roles.yaml",
                "schema_version: 1\nroles:\n"
                "  lead: [read, propose, review, merge, administer]\n"
                "  researcher: [read, propose]\n  reviewer: [read, review]\n  viewer: [read]\n",
            )
        self._write(root, "access/artifact-types.yaml", _artifact_type_policy_yaml())
        for directory in (*NODE_WORKSPACES.values(), *SYSTEM_WORKSPACES.values(), "runs"):
            readme = root / directory / "README.md"
            if readme.is_file() and _is_untouched_owner_boilerplate(readme):
                # 只删**一个字都没改过**的那种。节点自己写过的 README 是它的
                # 自述，会被带给所有后续节点看 —— 那是真内容，不许动。
                readme.unlink()
            elif not readme.is_file():
                self._write(root, f"{directory}/.gitkeep", "")
        self._write(root, ".research/schema-version", f"{PROJECT_SCHEMA_VERSION}\n")
        self._write(
            root,
            ".research/repository.yaml",
            "schema_version: 2\nauthority: platform_git\n"
            "session_isolation: worktree\ncheckpoint: every_run_outcome\n"
            "interactive_publish: manual\ncontinuous_publish: automatic\n"
            "artifact_transport: disabled\n",
        )
        self._write(
            root,
            ".research/migrations/project-v2.json",
            json.dumps(
                {
                    "schema_version": 2,
                    "migration": "project-v1-to-v2",
                    "legacy_root": ".research/legacy/v1",
                    "migrated_memory_entries": len(memory_entries),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        self._git(root, "add", "--all")
        self._git(
            root,
            "commit",
            "-m",
            "migration: upgrade Project repository to schema v2",
            "-m",
            f"Project-ID: {project_id}\nSchema-Version: {PROJECT_SCHEMA_VERSION}",
        )

    def status(self, project_id: str) -> RepositoryStatus:
        path = self.project_path(project_id)
        if not (path / ".git").is_dir():
            raise ProjectRepositoryError(f"Project repository is not initialized: {project_id}")
        branch = self._git(path, "branch", "--show-current") or DEFAULT_BRANCH
        return RepositoryStatus(
            project_id=project_id,
            branch=branch,
            head_commit=self._git(path, "rev-parse", "HEAD"),
            clean=not bool(self._git(path, "status", "--porcelain")),
            path=str(path),
        )

    def policy_hash(self, project_id: str, revision: str = "HEAD") -> str:
        """Hash the exact governance inputs used to derive workflow status."""
        repo = self.project_path(project_id)
        digest = hashlib.sha256()
        for relative in (
            "project.yaml",
            "access/nodes.yaml",
            "access/roles.yaml",
            "access/members.yaml",
            "access/artifact-types.yaml",
        ):
            content = self._git(repo, "show", f"{revision}:{relative}")
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(content.encode())
            digest.update(b"\0")
        return digest.hexdigest()

    def read_file_at_revision(
        self, project_id: str, relative_path: str, revision: str = "HEAD"
    ) -> str:
        """Read one tracked file without exposing the Git command boundary."""
        safe_path = self._safe_relative_path(relative_path)
        return self._git(self.project_path(project_id), "show", f"{revision}:{safe_path}")

    def file_tree(
        self,
        project_id: str,
        *,
        session_id: str | None = None,
        path: str = "",
        max_entries: int = _MAX_LISTING_ENTRIES,
    ) -> dict[str, object]:
        """列举 `path` 这一层的直接子项 —— 目录在前，文件在后。

        ## 为什么是一层，不是"整棵树一次给完"

        原来这里把整个工作区排成一个平坦列表，按路径字母序切前 5000 条。
        2026-09-09 在一个真项目上现形：9171 个文件，`.research/`(4542) 和
        `observation/`(4520) 按字母序排在前面就把配额吃光，于是 `figures/`
        `project.yaml` `resources/` `reviews/` `runs/` `paper/` —— **字母序
        排在 o 后面的每一个顶层目录整个消失**，包括用户当时正在找的那篇论文。

        真正致命的不是切，是**切得没有声音**：响应里没有任何字段说"还有 4171
        个文件没给你"，于是界面理直气壮地显示"5000 Project files"，用户和
        agent 都只能猜是不是界面折叠了。[[feedback_absent_check_looks_like_passed_check]]

        一层一层给之后，"字母序靠后的整个目录凭空消失"这件事在构造上不可能
        发生：每一层的目录行都在，展开才要钱。单层仍然可能到顶（一个目录直接
        塞了几千个文件），那时 `truncated` / `totalEntries` 会**说出来**。

        返回值里的 `totalFiles` 是整个工作区的文件总数 —— 界面要能回答"这个
        项目一共有多少文件"，那个问题不该靠把它们全列一遍来回答。
        """
        root = (
            Path(self.session_status(project_id, session_id).path)
            if session_id
            else self.project_path(project_id)
        )
        prefix = self._safe_relative_path(path) if str(path).strip() else ""
        materials = _materials_if_available()
        tracked = {
            self._safe_relative_path(item)
            for item in self._git(root, "ls-files", "-z").split("\0")
            if item
        }
        untracked = {
            self._safe_relative_path(item)
            for item in self._git(root, "ls-files", "--others", "--exclude-standard", "-z").split(
                "\0"
            )
            if item
        }
        changed = {
            self._safe_relative_path(item)
            for item in self._git(root, "diff", "--name-only", "HEAD").splitlines()
            if item.strip()
        }

        def status_of(relative: str) -> str:
            if relative in untracked:
                return "untracked"
            return "modified" if relative in changed else "committed"

        # 材料（用户交来的文件）的字节是 gitignored 的，Git 只看得见 `.ref`
        # 指针。把指针端出去等于让用户在自己的文件树里找不到自己传的文件，
        # 只看见一个 `.ref` 双胞胎 —— 所以指针那一行换成材料本身那一行，
        # 真名、真大小、真状态（状态问的是**指针**进没进 main：字节从不进
        # 版本，拿它回答"这份材料跨会话可见了吗"只会答错）。
        material_rows: dict[str, dict[str, object]] = {}
        if materials is not None:
            for reference in materials.inventory(root):
                material_rows[reference.path] = {
                    "path": reference.path,
                    "name": PurePosixPath(reference.path).name,
                    "kind": "file",
                    "sizeBytes": reference.size_bytes,
                    "owner": "platform",
                    "bookkeeping": False,
                    "tracked": reference.ref_path in tracked,
                    "status": status_of(reference.ref_path),
                    "source": reference.source,
                    "uploadedBy": reference.uploaded_by,
                    "uploadedAt": reference.uploaded_at,
                    "note": reference.note,
                    "sha256": reference.sha256,
                    "missing": not reference.present,
                }

        materials_prefix = (
            f"{materials.MATERIALS_RELATIVE}/" if materials is not None else None
        )
        every_file: dict[str, int] = {}
        for relative in tracked | untracked:
            if materials_prefix is not None and relative.startswith(materials_prefix):
                continue
            candidate = root / relative
            try:
                every_file[relative] = candidate.stat().st_size
            except OSError:
                # ls-files 报了它、盘上没有（刚被删、或是嵌套仓库的目录项）。
                # 少一行比整棵树抛错好。
                continue
        for relative, row in material_rows.items():
            every_file[relative] = int(row["sizeBytes"])

        scope = f"{prefix}/" if prefix else ""
        depth = len(PurePosixPath(prefix).parts) if prefix else 0
        files: list[dict[str, object]] = []
        directories: dict[str, dict[str, object]] = {}
        for relative, size in every_file.items():
            if scope and not relative.startswith(scope):
                continue
            parts = PurePosixPath(relative).parts
            if len(parts) == depth + 1:
                row = material_rows.get(relative)
                if row is None:
                    row = {
                        "path": relative,
                        "name": parts[-1],
                        "kind": "file",
                        "sizeBytes": size,
                        "owner": _owner_of(relative),
                        "bookkeeping": _is_bookkeeping(relative),
                        "tracked": relative in tracked,
                        "status": status_of(relative),
                    }
                files.append(row)
                continue
            child = "/".join(parts[: depth + 1])
            bucket = directories.get(child)
            if bucket is None:
                bucket = directories[child] = {
                    "path": child,
                    "name": parts[depth],
                    "kind": "directory",
                    "sizeBytes": 0,
                    "fileCount": 0,
                    "changedCount": 0,
                    "owner": _owner_of(child),
                    # 目录行的记账status 由**里面的东西**决定，下面按孩子逐个收敛。
                    # 只要里面有一件研究产出，这个目录就不是记账 —— 否则
                    # `.research/`（里面既有框架账本、也有调度器的整个工作区）
                    # 会被整个折起来，调度器编出来的论文又一次找不到，只是这次
                    # 少埋一层。容器是不是内务，得看它装着什么。
                    "bookkeeping": True,
                    "tracked": True,
                    "status": "committed",
                }
            bucket["sizeBytes"] = int(bucket["sizeBytes"]) + size
            bucket["fileCount"] = int(bucket["fileCount"]) + 1
            if not _is_bookkeeping(relative):
                bucket["bookkeeping"] = False
            if status_of(relative) != "committed":
                bucket["changedCount"] = int(bucket["changedCount"]) + 1
                bucket["status"] = "modified"

        ordered = sorted(directories.values(), key=_listing_order) + sorted(
            files, key=_listing_order
        )
        limit = max(1, int(max_entries))
        return {
            "path": prefix,
            "entries": ordered[:limit],
            "totalEntries": len(ordered),
            "truncated": len(ordered) > limit,
            "totalFiles": len(every_file),
        }

    def _resolve_worktree_file(
        self,
        project_id: str,
        relative_path: str,
        *,
        session_id: str | None = None,
    ) -> tuple[str, Path]:
        """把一个仓库相对路径解析成磁盘上的真实文件。

        文本读取和字节读取共用这一处：路径穿越的判据只有一份，两条读取路径
        不会各自演化出一份略有不同的检查（两份副本里迟早只有一份被加固）。
        """
        root = (
            Path(self.session_status(project_id, session_id).path)
            if session_id
            else self.project_path(project_id)
        )
        safe = self._safe_relative_path(relative_path)
        candidate = (root / safe).resolve()
        if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
            raise ProjectRepositoryError(f"Project file does not exist: {safe}")
        return safe, candidate

    def read_worktree_file(
        self,
        project_id: str,
        relative_path: str,
        *,
        session_id: str | None = None,
        max_bytes: int = 200_000,
    ) -> dict[str, object]:
        safe, candidate = self._resolve_worktree_file(
            project_id, relative_path, session_id=session_id
        )
        body = candidate.read_bytes()
        binary = b"\0" in body[:8_000]
        return {
            "path": safe,
            "sizeBytes": len(body),
            "binary": binary,
            "truncated": len(body) > max_bytes,
            "content": None if binary else body[:max_bytes].decode("utf-8", errors="replace"),
        }

    def read_worktree_bytes(
        self,
        project_id: str,
        relative_path: str,
        *,
        session_id: str | None = None,
        max_bytes: int,
    ) -> tuple[str, bytes]:
        """原样读出文件字节 —— 图和 PDF 只能这么读。

        `read_worktree_file` 解码成 UTF-8 字符串，遇到二进制直接返回
        `content=None`：PNG 和 PDF 在那条路上**根本取不到内容**。节点产出的图
        和论文因此在界面上只有一个文件名。

        大小上限在 `read_bytes()` **之前**用 stat 判：先读进内存再检查，等于
        任何一个大文件都能让后端把它完整加载一遍才拒绝。
        """
        safe, candidate = self._resolve_worktree_file(
            project_id, relative_path, session_id=session_id
        )
        size = candidate.stat().st_size
        if size > max_bytes:
            raise ProjectFileTooLargeError(
                f"Project file is too large to preview: {safe} ({size} bytes)",
                size_bytes=size,
                max_bytes=max_bytes,
            )
        return safe, candidate.read_bytes()

    def ensure_session_workspace(
        self,
        *,
        project_id: str,
        session_id: str,
        base_commit: str | None,
        title: str,
        created_by: str,
    ) -> SessionWorkspace:
        project_id = self._safe_id(project_id, "project id")
        session_id = self._safe_id(session_id, "session id")
        repo = self.project_path(project_id)
        path = self.session_path(project_id, session_id)
        branch = self.session_branch(session_id)
        with self._project_lock(project_id):
            if not (repo / ".git").is_dir():
                raise ProjectRepositoryError(f"Project repository is not initialized: {project_id}")
            base = base_commit or self._git(repo, "rev-parse", DEFAULT_BRANCH)
            self._git(repo, "cat-file", "-e", f"{base}^{{commit}}")
            if path.exists():
                # 目录名是身份的**短名**（见 `_worktree_dir_name`）。短名会撞是
                # 天文数字级的小概率，但撞了必须**当场说出来**：这里已经是别人的
                # 工作树，接着用就是两个会话共写一棵树，而两边都不会报错。
                # 判据取分支（身份的真正载体），不新造记号文件。
                here = self._git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
                if here and here != branch:
                    raise ProjectRepositoryError(
                        f"Session worktree directory {path} already belongs to "
                        f"branch {here!r}, not {branch!r} — refusing to share it."
                    )
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                branch_exists = bool(
                    self._git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
                )
                args = (
                    ("worktree", "add", str(path), branch)
                    if branch_exists
                    else ("worktree", "add", "-b", branch, str(path), base)
                )
                self._git(repo, *args)
                self._configure_identity(path)
                self._write(
                    path,
                    f".research/orchestration/sessions/{session_id}.yaml",
                    _yaml_mapping(
                        {
                            "schema_version": 2,
                            "session_id": session_id,
                            "title": title,
                            "created_by": created_by,
                            "base_commit": base,
                            "branch": branch,
                        }
                    ),
                )
                self._git(
                    path,
                    "add",
                    f".research/orchestration/sessions/{session_id}.yaml",
                )
                self._git(
                    path,
                    "commit",
                    "-m",
                    f"session: initialize {session_id}",
                    "-m",
                    f"Session-ID: {session_id}\nBase-Commit: {base}",
                )
            # 用户交来的文件：`.ref` 指针随 Git 分支过来，字节不会（它们在池里
            # 按内容寻址，不进版本）。这一步把字节接回来 —— 少了它，新会话的
            # agent 看到的是一个指针文件旁边缺了实体，也就是"传过的文件读不到"。
            # 幂等：实体已在就什么都不做。
            _materials = _materials_if_available()
            if _materials is not None:
                _materials.materialize(path)
            return self.session_status(project_id, session_id, base_commit=base)

    def session_status(
        self, project_id: str, session_id: str, *, base_commit: str | None = None
    ) -> SessionWorkspace:
        repo = self.project_path(project_id)
        path = self.session_path(project_id, session_id)
        branch = self.session_branch(session_id)
        if not path.is_dir():
            raise ProjectRepositoryError(f"Session worktree is not initialized: {session_id}")
        base = base_commit or self._git(repo, "merge-base", DEFAULT_BRANCH, branch)
        counts = self._git(
            repo, "rev-list", "--left-right", "--count", f"{DEFAULT_BRANCH}...{branch}"
        )
        behind, ahead = (int(value) for value in counts.split())
        return SessionWorkspace(
            project_id=project_id,
            session_id=session_id,
            branch=branch,
            base_commit=base,
            head_commit=self._git(path, "rev-parse", "HEAD"),
            path=str(path),
            ahead_by=ahead,
            behind_by=behind,
            clean=not bool(self._git(path, "status", "--porcelain")),
        )

    @staticmethod
    def artifact_paths(artifact_id: str, artifact_type: str, mime_type: str) -> tuple[str, str]:
        safe_artifact_id = GitProjectRepository._safe_id(artifact_id, "artifact id")
        directory = _ARTIFACT_DIRECTORIES.get(artifact_type, "other")
        extension = _MIME_EXTENSIONS.get(mime_type, ".txt")
        root = f"artifacts/{directory}/{safe_artifact_id}"
        return f"{root}/content{extension}", f"{root}/manifest.yaml"

    @classmethod
    def candidate_paths(
        cls,
        *,
        artifact_id: str,
        artifact_type: str,
        mime_type: str,
        resource_type: str,
        resource_key: str,
    ) -> tuple[str, str]:
        if resource_type == "artifact":
            return cls.artifact_paths(artifact_id, artifact_type, mime_type)
        prefix = f"{resource_type}/"
        suffix = resource_key[len(prefix) :] if resource_key.startswith(prefix) else resource_key
        suffix = cls._safe_relative_path(suffix.strip("/"))
        if not suffix:
            raise ProjectRepositoryError("Project document/config resource key has no path")
        if not PurePosixPath(suffix).suffix:
            suffix += _MIME_EXTENSIONS.get(mime_type, ".txt")
        root = "documents" if resource_type == "project_doc" else "resources/project-settings"
        safe_artifact_id = cls._safe_id(artifact_id, "artifact id")
        return f"{root}/{suffix}", f".research/resources/{safe_artifact_id}.yaml"

    def write_artifact_revision(
        self,
        *,
        project_id: str,
        session_id: str,
        artifact_id: str,
        artifact_type: str,
        name: str,
        content: str,
        mime_type: str,
        checksum: str,
        version: int,
        actor_id: str,
        change_set_id: str,
        expected_head_commit: str,
        resource_type: str = "artifact",
        resource_key: str = "",
        source_run_id: str | None = None,
        owner_node: str | None = None,
        source_attestation: dict | None = None,
        artifact_contract: dict | None = None,
    ) -> RevisionWrite:
        content_path, manifest_path = self.candidate_paths(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            mime_type=mime_type,
            resource_type=resource_type,
            resource_key=resource_key,
        )
        workspace = self.session_status(project_id, session_id)
        root = Path(workspace.path)
        evidence_paths: list[str] = []
        attestation_hash: str | None = None
        contract_hash: str | None = None
        if owner_node and source_attestation is None:
            raise ProjectRepositoryError(
                "Node-owned Project revisions require framework attestation evidence"
            )
        if (source_attestation is None) != (artifact_contract is None):
            raise ProjectRepositoryError(
                "Artifact attestation and contract must be persisted together"
            )
        if source_attestation is not None and artifact_contract is not None:
            attestation_hash = str(source_attestation.get("attestation_sha256") or "")
            contract_hash = str(artifact_contract.get("contract_sha256") or "")
            source_record_hash = str(source_attestation.get("record_sha256") or "")
            for label, value in (
                ("attestation", attestation_hash),
                ("contract", contract_hash),
                ("source record", source_record_hash),
            ):
                if not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ProjectRepositoryError(f"Artifact {label} hash must be SHA-256")
            for label, payload, hash_field, claimed_hash in (
                ("attestation", source_attestation, "attestation_sha256", attestation_hash),
                ("contract", artifact_contract, "contract_sha256", contract_hash),
            ):
                canonical_body = {key: value for key, value in payload.items() if key != hash_field}
                calculated_hash = hashlib.sha256(
                    json.dumps(
                        canonical_body,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if calculated_hash != claimed_hash:
                    raise ProjectRepositoryError(
                        f"Artifact {label} payload does not match its declared hash"
                    )
            producer = source_attestation.get("producer")
            if (
                source_attestation.get("artifact_type") != artifact_type
                or not isinstance(producer, dict)
                or producer.get("node_type") != owner_node
            ):
                raise ProjectRepositoryError(
                    "Artifact attestation does not match the Project revision identity"
                )
            safe_artifact_id = self._safe_id(artifact_id, "artifact id")
            contract_path = f".research/contracts/artifacts/{contract_hash}.json"
            attestation_path = (
                f".research/attestations/{safe_artifact_id}/{source_record_hash}.json"
            )
            evidence_paths = [contract_path, attestation_path]
        with self._project_lock(project_id):
            if self._git(root, "rev-parse", "HEAD") != expected_head_commit:
                raise ProjectRepositoryError(
                    "Session Git head changed outside Platform revision authority"
                )
            if self._worktree_has_blocking_changes(root):
                raise ProjectRepositoryError("Session worktree has uncommitted changes")
            self._write(root, content_path, content)
            if source_attestation is not None and artifact_contract is not None:
                self._write(
                    root,
                    evidence_paths[0],
                    json.dumps(
                        artifact_contract,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
                self._write(
                    root,
                    evidence_paths[1],
                    json.dumps(
                        source_attestation,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
            self._write(
                root,
                manifest_path,
                _yaml_mapping(
                    {
                        "schema_version": 1,
                        "artifact_id": artifact_id,
                        "type": artifact_type,
                        "name": name,
                        "owner_node": owner_node or "user",
                        "source_attestation_sha256": attestation_hash,
                        "artifact_contract_sha256": contract_hash,
                        "revision": version,
                        "content_sha256": checksum,
                        "mime_type": mime_type,
                        "source_session": session_id,
                        "source_run": source_run_id,
                        "resource_type": resource_type,
                        "resource_key": resource_key,
                    }
                ),
            )
            tracked_paths = [content_path, manifest_path, *evidence_paths]
            self._git(root, "add", "--", *tracked_paths)
            if not self._git(root, "diff", "--cached", "--name-only"):
                return RevisionWrite(
                    commit_sha=self._git(root, "rev-parse", "HEAD"),
                    repository_path=content_path,
                    branch=workspace.branch,
                    additions=0,
                    deletions=0,
                )
            numstat = self._git(root, "diff", "--cached", "--numstat", "--", *tracked_paths)
            additions = deletions = 0
            for line in numstat.splitlines():
                fields = line.split("\t", 2)
                if len(fields) >= 2:
                    additions += int(fields[0]) if fields[0].isdigit() else 0
                    deletions += int(fields[1]) if fields[1].isdigit() else 0
            self._git(
                root,
                "commit",
                "-m",
                f"artifact({artifact_type}): revise {name}"[:200],
                "-m",
                (
                    f"Artifact-ID: {artifact_id}\nArtifact-SHA256: {checksum}\n"
                    f"Session-ID: {session_id}\nChange-Set-ID: {change_set_id}\n"
                    f"Actor-ID: {actor_id}"
                    + (f"\nRun-ID: {source_run_id}" if source_run_id else "")
                    + (
                        f"\nArtifact-Attestation-SHA256: {attestation_hash}"
                        if attestation_hash
                        else ""
                    )
                    + (f"\nArtifact-Contract-SHA256: {contract_hash}" if contract_hash else "")
                ),
            )
            return RevisionWrite(
                commit_sha=self._git(root, "rev-parse", "HEAD"),
                repository_path=content_path,
                branch=workspace.branch,
                additions=additions,
                deletions=deletions,
            )

    @classmethod
    def tracked_paths_for_version(
        cls,
        *,
        artifact_id: str,
        artifact_type: str,
        mime_type: str,
        resource_type: str,
        resource_key: str,
    ) -> tuple[str, str]:
        return cls.candidate_paths(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            mime_type=mime_type,
            resource_type=resource_type,
            resource_key=resource_key,
        )

    def write_receipt(
        self,
        *,
        project_id: str,
        session_id: str,
        artifact_id: str,
        revision_hash: str,
        receipt_type: str,
        receipt_hash: str,
        payload: dict,
        actor_id: str,
        expected_head_commit: str,
    ) -> RevisionWrite:
        artifact_id = self._safe_id(artifact_id, "artifact id")
        receipt_type = self._safe_id(receipt_type, "receipt type")
        if not re.fullmatch(r"[0-9a-f]{64}", revision_hash):
            raise ProjectRepositoryError("Artifact revision hash must be SHA-256")
        if not re.fullmatch(r"[0-9a-f]{64}", receipt_hash):
            raise ProjectRepositoryError("Receipt hash must be SHA-256")
        receipt_material = dict(payload)
        receipt_material.pop("issued_at", None)
        calculated_receipt_hash = hashlib.sha256(
            json.dumps(
                receipt_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if calculated_receipt_hash != receipt_hash:
            raise ProjectRepositoryError("Receipt payload does not match its declared hash")
        relative = (
            f"receipts/{artifact_id}/{revision_hash}/{receipt_type}__{receipt_hash[:16]}.json"
        )
        workspace = self.session_status(project_id, session_id)
        root = Path(workspace.path)
        with self._project_lock(project_id):
            if self._git(root, "rev-parse", "HEAD") != expected_head_commit:
                raise ProjectRepositoryError(
                    "Session Git head changed outside Platform receipt authority"
                )
            if self._worktree_has_blocking_changes(root):
                raise ProjectRepositoryError("Session worktree has uncommitted changes")
            content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            self._write(root, relative, content)
            self._git(root, "add", "--", relative)
            if self._git(root, "diff", "--cached", "--name-only"):
                self._git(
                    root,
                    "commit",
                    "-m",
                    f"receipt({receipt_type}): {artifact_id}",
                    "-m",
                    (
                        f"Artifact-ID: {artifact_id}\nArtifact-SHA256: {revision_hash}\n"
                        f"Receipt-SHA256: {receipt_hash}\nSession-ID: {session_id}\n"
                        f"Actor-ID: {actor_id}"
                    ),
                )
            return RevisionWrite(
                commit_sha=self._git(root, "rev-parse", "HEAD"),
                repository_path=relative,
                branch=workspace.branch,
                additions=len(content.splitlines()),
                deletions=0,
            )

    @staticmethod
    def _safe_relative_path(value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value.strip()
            or str(path) == "."
            or path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
        ):
            raise ProjectRepositoryError(f"Unsafe repository path: {value!r}")
        return str(path)

    def diff(
        self,
        *,
        project_id: str,
        session_id: str,
        paths: list[str] | None = None,
        max_bytes: int = 200_000,
    ) -> RepositoryDiff:
        """会话相对 main 的补丁正文 —— **按预算出**，读多少算多少。

        从前这里先让 git 生成整份补丁、再对每个改动文件读 1MB 样本扫密钥、最后
        在 Python 里截到 `max_bytes`。三步全部与工作区体量成正比：一个把日志
        tar 解成 9048 个文件的会话，补丁正文 1.83 GB，一次调用 35 秒，而它挂在
        会话列表 5 秒一次的轮询上，整台后端以 35 秒为量子停摆（2026-09-09 node20）。

        现在：先要名单（`--name-only`，与内容体量无关），再**逐文件**要补丁，
        攒够 `max_bytes` 就停并标 `truncated`。计数只描述这份补丁本身 ——
        「改了多少文件」另有 `changed_paths`，它不读内容。

        密钥扫描不在这里：那是写边界（checkpoint）的闸；读侧再扫一遍是同一道
        闸的抄件，代价随每次读放大。
        """
        workspace = self.session_status(project_id, session_id)
        source = Path(workspace.path)
        safe_paths = [self._safe_relative_path(path) for path in (paths or [])]
        session_record = (
            f".research/orchestration/sessions/{self._safe_id(session_id, 'session_id')}.yaml"
        )
        selected_pathspecs = safe_paths or [".", f":(exclude){session_record}"]
        tracked = sorted(
            {
                self._safe_relative_path(path)
                for path in self._git(
                    source, "diff", "--name-only", "-z", DEFAULT_BRANCH, "--", *selected_pathspecs,
                ).split("\0")
                if path
            }
        )
        untracked = sorted(
            {
                self._safe_relative_path(path)
                for path in self._git(
                    source, "ls-files", "--others", "--exclude-standard", "-z",
                    "--", *selected_pathspecs, check=False,
                ).split("\0")
                if path
            }
            - set(tracked)
        )
        git_diff = (
            "-c", f"diff.bigFileThreshold={settings.project_git_max_blob_bytes}",
            "diff", "--no-ext-diff", "--unified=3",
        )
        chunks: list[str] = []
        budget = max_bytes
        truncated = False
        for relative, is_tracked in [(p, True) for p in tracked] + [(p, False) for p in untracked]:
            candidate = source / relative
            if not is_tracked and not candidate.is_file():
                continue
            # 超过 blob 上限的文件只报身份不出正文：它本来就进不了 Git（checkpoint 会
            # 排除它），补丁里也不该把几十 MB 的内容读出来。stat 一次，不读内容。
            if candidate.is_file() and candidate.stat().st_size > settings.project_git_max_blob_bytes:
                text = (
                    f"diff --git a/{relative} b/{relative}\n"
                    f"--- a/{relative}\n+++ b/{relative}\n@@ -0,0 +0,0 @@\n"
                    " [content withheld: exceeds the Git blob limit; register external storage]"
                )
            else:
                text = (
                    self._git(source, *git_diff, DEFAULT_BRANCH, "--", relative)
                    if is_tracked
                    else self._git(
                        source, *git_diff, "--no-index", "--", "/dev/null", relative, check=False
                    )
                )
            if not text:
                continue
            encoded = text.encode("utf-8")
            if len(encoded) > budget:
                truncated = True
                if not chunks:
                    chunks.append(encoded[:budget].decode("utf-8", errors="ignore"))
                break
            chunks.append(text)
            budget -= len(encoded) + 1
        raw = "\n".join(chunks)
        if truncated:
            raw += "\n... diff truncated ...\n"
        lines = raw.splitlines()
        additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        files_changed = sum(1 for line in lines if line.startswith("diff --git "))
        return RepositoryDiff(raw, additions, deletions, files_changed, truncated)

    def commit_with_trailer(self, project_id: str, trailer: str, value: str) -> str | None:
        """main 上有没有带这条 trailer 的提交 —— 有就返回它的 sha。

        ## 为什么幂等判据该在 git 上

        「这一轮交付过没有」从前问的是 `project_revisions` 表里有没有一行带特定
        message 的记录。可 manifest 里白纸黑字写着 `authority: git` —— 表是投影。
        判据建在投影上，投影和事实分叉时不报错，只是悄悄地重复交付或永不交付。

        `publish_linear` 早就是这么判的（`Change-Set-ID:` trailer + `--grep`）。
        这里只是把交付这条路也搬到同一把尺子上。
        """
        repo = self.project_path(project_id)
        found = self._git(
            repo, "log", DEFAULT_BRANCH, "--format=%H", "--grep",
            f"^{trailer}: {value}$", "-n", "1", check=False,
        )
        return found.splitlines()[0] if found else None

    def paths_differing_from_main(self, project_id: str, session_id: str) -> list[str]:
        """此刻会话与 main **内容确实不一样**的文件（两点 diff）。

        与"这个会话改过哪些"（三点 diff，相对分叉点）不是一回事：把 main 那一版
        取回来之后，文件仍然出现在三点 diff 里（相对分叉点确实变了），但它和 main
        已经一模一样 —— 那就不再是冲突。

        2026-09-05 真机点验抓到的：只用三点 diff 判冲突，"用项目那一版"解决完
        之后冲突还挂在那里，永远解不掉。
        """
        workspace = self.session_status(project_id, session_id)
        root = Path(workspace.path)
        return sorted(
            {
                self._safe_relative_path(path)
                for path in self._git(
                    root, "diff", "--name-only", DEFAULT_BRANCH, "HEAD"
                ).splitlines()
                if path.strip()
            }
        )

    def paths_changed_on_main_since(self, project_id: str, session_id: str) -> list[str]:
        """从这个会话分出去之后，main 上又改了哪些文件。

        与 `changed_paths`（会话这边改了哪些）取交集，就是**冲突**：两边动了
        同一个文件。这是 git 世界里冲突的定义 —— 从前它是「manifest 里同一个
        resource_key 的版本对不上」，那是在 git 之上又建了一套资源级的版本模型。
        """
        repo = self.project_path(project_id)
        branch = self.session_branch(session_id)
        base = self._git(repo, "merge-base", DEFAULT_BRANCH, branch)
        return sorted(
            {
                self._safe_relative_path(path)
                for path in self._git(
                    repo, "diff", "--name-only", f"{base}..{DEFAULT_BRANCH}"
                ).splitlines()
                if path.strip()
            }
        )

    def take_main_version(self, project_id: str, session_id: str, path: str) -> str:
        """把 main 上那一版取回会话工作区并提交 —— 「用项目那一版」的实现。

        另一个选项（「用我这一版」）不需要任何动作：发布本来就是把会话的路径
        回放到 main 上，会话那一版自然胜出。所以那条路**什么都不做**才是对的，
        造一条空记录只是为了让两边看起来对称。
        """
        safe = self._safe_relative_path(path)
        workspace = self.session_status(project_id, session_id)
        root = Path(workspace.path)
        with self._project_lock(project_id):
            self._git(root, "checkout", DEFAULT_BRANCH, "--", safe)
            if not self._git(root, "status", "--porcelain", "--", safe):
                return workspace.head_commit
            self._git(root, "add", "--", safe)
            self._configure_identity(root)
            self._git(root, "commit", "-m", f"Take project version of {safe}")
            return self._git(root, "rev-parse", "HEAD")

    def changed_paths(self, project_id: str, session_id: str) -> list[str]:
        """All committed Session paths that differ from canonical main."""
        workspace = self.session_status(project_id, session_id)
        root = Path(workspace.path)
        session_record = (
            f".research/orchestration/sessions/{self._safe_id(session_id, 'session_id')}.yaml"
        )
        return sorted(
            {
                self._safe_relative_path(path)
                for path in self._git(
                    root,
                    "diff",
                    "--name-only",
                    f"{DEFAULT_BRANCH}...HEAD",
                    "--",
                    ".",
                ).splitlines()
                if path.strip() and path != session_record
            }
        )

    @staticmethod
    def _secret_like(content: bytes) -> bool:
        sample = content[:1_000_000]
        patterns = (
            b"-----BEGIN PRIVATE KEY-----",
            b"-----BEGIN RSA PRIVATE KEY-----",
            b"-----BEGIN OPENSSH PRIVATE KEY-----",
            b"AKIA",
        )
        return any(pattern in sample for pattern in patterns)

    def _validate_checkpoint_paths(
        self,
        root: Path,
        *,
        project_id: str,
        node_type: str,
        run_id: str,
        workspace_prefix: str,
        paths: list[str],
    ) -> CheckpointSelection:
        canonical = self.project_path(project_id)
        policy_text = self._git(canonical, "show", f"{DEFAULT_BRANCH}:access/nodes.yaml")
        policy = yaml.safe_load(policy_text) or {}
        policy_node = {
            "_curator": "memory_curator",
            "_reviewer": "reviewer",
            "_orchestrator": "orchestrator",
        }.get(node_type, node_type)
        node_policy = (policy.get("nodes") or {}).get(policy_node) or {}
        allowed = tuple(
            self._safe_relative_path(str(value)) for value in (node_policy.get("write") or [])
        )
        if not allowed:
            extension = policy.get("extensions") or {}
            extension_root = self._safe_relative_path(
                str(extension.get("root") or "runs/extensions")
            )
            safe_node = self._safe_id(node_type, "extension node type")
            safe_run = self._safe_id(run_id, "extension run id")
            allowed = (f"{extension_root}/{safe_node}/{safe_run}",)
        declared_prefix = self._safe_relative_path(workspace_prefix)
        if declared_prefix not in allowed:
            raise ProjectRepositoryError(
                f"Node workspace does not match canonical ownership: {declared_prefix}"
            )
        prefixes = tuple(PurePosixPath(value) for value in allowed)
        # 路径**数量**没有上限：数量唯一的真实约束是 argv 长度，那是实现细节，
        # 由 `_chunk_paths_for_argv` 消化。体量的约束在下面按目录算。
        safe_paths = sorted({self._safe_relative_path(path) for path in paths})
        if not safe_paths:
            return CheckpointSelection([], (), ())
        oversized: list[tuple[str, int]] = []
        sizes: dict[str, int] = {}
        for relative in safe_paths:
            if len(relative.encode("utf-8")) > 1_000:
                raise ProjectRepositoryError("Node workspace checkpoint path is too long")
            parsed = PurePosixPath(relative)
            if not any(parsed.is_relative_to(prefix) for prefix in prefixes):
                raise ProjectRepositoryError(
                    f"Node workspace change escaped its owned Project directory: {relative}"
                )
            candidate = root / relative
            if candidate.is_symlink():
                raise ProjectRepositoryError(
                    f"Node workspace checkpoint cannot contain a symlink: {relative}"
                )
            if candidate.is_file():
                size = candidate.stat().st_size
                if size > settings.project_git_max_blob_bytes:
                    # 排除，不是抛。违规分两类：越界写 / 符号链接 / 像凭据是
                    # **契约违规**，必须硬拒；文件太大不是违规，是"这个文件不
                    # 适合进 Git"。抛出去的代价是整次 checkpoint 全挂（2026-08-11：
                    # 一个 150MB 的轨迹让 experiment 的每一次 checkpoint 都失败，
                    # 9 份日志一次都没进 Git）。**全损严格劣于丢一个文件。**
                    oversized.append((relative, size))
                    continue
                with candidate.open("rb") as source_file:
                    sample = source_file.read(1_000_000)
                if self._secret_like(sample):
                    raise ProjectRepositoryError(
                        f"Project file resembles a private credential and cannot be committed: "
                        f"{relative}"
                    )
                sizes[relative] = size
        # 同一规矩再往上一层：**一个目录**也可能"不适合进 Git"。一份解开的数据集
        # 是几千个各自不大的文件（2026-09-09：4492 个日志、897MB，两份副本），
        # 逐文件的上限看不见它，而它一旦进了会话分支，每一次"这个会话改了什么"
        # 都要跟它成正比。目录是数据集的单位，所以按节点目录下的**第一层子目录**
        # 汇总；超预算的整个目录排除并如实上报，让节点知道该走 `extract_material`
        # 把数据放进材料池（gitignored，按内容寻址）。
        totals: dict[str, tuple[int, int]] = {}
        owners: dict[str, str] = {}
        for relative, size in sizes.items():
            parsed = PurePosixPath(relative)
            for prefix in prefixes:
                if parsed.is_relative_to(prefix):
                    below = parsed.relative_to(prefix).parts
                    if len(below) >= 2:
                        directory = str(prefix / below[0])
                        owners[relative] = directory
                        count, total = totals.get(directory, (0, 0))
                        totals[directory] = (count + 1, total + size)
                    break
        bulk = sorted(
            (directory, total, count)
            for directory, (count, total) in totals.items()
            if total > settings.project_git_max_tree_bytes
        )
        excluded_dirs = {directory for directory, _, _ in bulk}
        excluded = {path for path, _ in oversized} | {
            path for path, directory in owners.items() if directory in excluded_dirs
        }
        return CheckpointSelection(
            [p for p in safe_paths if p not in excluded], tuple(oversized), tuple(bulk)
        )

    def revert_last_session_commit(
        self, *, project_id: str, session_id: str
    ) -> dict[str, object]:
        """撤销本会话最近一次写 —— 用 **revert**，不是恢复文件备份。

        为什么不是"从 .history 恢复那个文件"（CLI 的 /undo 一直是那样做的）：

        1. **粒度错了。** 一次平台操作往往同时写产物、索引、账本；单独把产物
           回滚回去，会造出一个**从未存在过的状态**（产物是旧的、索引是新的）。
           Git 的单位是 commit = 一次操作，回不出不一致。
        2. **语义错了。** 本仓自己的规矩是"撤销 = 留痕的反向操作，不是删除"
           （CLI 在 KB claim 的撤销提示里就是这么写的）。revert 生成一个新
           commit，历史完整；覆盖文件则是抹掉证据。
        3. v2.1 之后工作区**就是** Git，再维护一套 `.history` 是第二个真相源。

        `.history` 那一套仍然保留，但**只服务 CLI 的独立运行**（那里没有 Git
        工作区，文件备份是唯一可行的撤销）。两者底座不相交，所以不是分叉。
        见 `chat.py:_cmd_undo` 的对照说明与边界条件。

        冻结产物不许回滚：那是预注册防篡改语义，故意不给绕。
        """
        workspace = self.session_status(project_id, session_id)
        root = Path(workspace.path)
        with self._project_lock(project_id):
            head = self._git(root, "rev-parse", "HEAD").strip()
            base = (workspace.base_commit or "").strip()
            if not head or head == base:
                raise ValueError("这个 Session 还没有可撤销的提交")
            parents = self._git(root, "rev-list", "--parents", "-n", "1", head).split()
            if len(parents) < 2:
                raise ValueError("最近一次提交没有父提交，无法撤销")
            changed = [
                path
                for path in self._git(
                    root, "diff", "--name-only", "-z", f"{head}~1", head
                ).split("\0")
                if path
            ]
            frozen = self._frozen_register(root)
            blocked = sorted(path for path in changed if path in frozen)
            if blocked:
                raise ValueError(
                    "这次提交动了已冻结的产物，不能撤销（预注册防篡改语义）："
                    + "、".join(blocked[:5])
                )
            # 工作区脏就不能 revert（git 自己会拒，但它抛的是 128 + 一段英文）。
            # 提前检查，把它变成一句能照做的话 —— 报错不给下一步，等于只告诉
            # 用户"坏了"。
            # porcelain 每行是 `XY<空格>路径`，X/Y 各一字符。按固定下标切会在
            # 某些状态码下吃掉路径首字母（实测切出 "ypothesis/..."）—— 报错里
            # 给一个**错的路径**比不给更坏，所以按状态位长度切再 strip。
            dirty = [
                line[2:].strip()
                for line in self._git(root, "status", "--porcelain").splitlines()
                if line.strip()
            ]
            if dirty:
                raise ValueError(
                    "工作区还有未提交的改动，撤销会覆盖它们。先等这一轮跑完"
                    "（它结束时会自动 checkpoint），或手动处理这些文件："
                    + "、".join(dirty[:5])
                    + ("…" if len(dirty) > 5 else "")
                )
            subject = self._git(root, "log", "-1", "--format=%s", head).strip()
            self._configure_identity(root)
            self._git(root, "revert", "--no-edit", "--no-commit", head)
            # 与正常提交同一条冻结守卫：revert 也可能把冻结路径改回去。
            self._enforce_frozen_register(root)
            self._git(
                root, "commit", "-m",
                f"Revert: {subject}"[:200],
                "--allow-empty",
            )
            new_head = self._git(root, "rev-parse", "HEAD").strip()
        return {
            "revertedCommit": head,
            "revertedSubject": subject,
            "headCommit": new_head,
            "changedPaths": changed[:50],
        }

    #: 平台提交时配在 worktree 上的身份（见 `_ensure_worktree_identity`）。
    #: 节点**没有**提交能力（改写历史的 git 在命令闸口硬拒），所以这个身份
    #: 加上 Session-ID trailer 就是"这条提交是平台做的"的机械证据。
    PLATFORM_COMMITTER_EMAIL = "research-platform@localhost"

    def _commits_not_made_by_platform(
        self, root: Path, *, expected: str, current: str, session_id: str
    ) -> list[str]:
        """`expected..current` 之间，哪些提交不是平台自己做的？空列表 = 全是平台做的。

        ## 为什么判据要从"DB 记得对不对"换成"这些提交是谁做的"

        原来的判据是 `current_head != expected_head_commit` 就一律拒绝。但那个
        expected 来自 `SessionProjection.git_head_commit_sha` —— 一条**写进 DB
        之后才生效**的记录，而 Git 提交在 `checkpoint_session_workspace` 返回的
        那一刻就已经落地了。两者之间隔着一段可以失败的代码（见 local_execution
        里 checkpoint 与 `db.commit()` 之间的 on_progress 推送：客户端一断开
        它就抛）。于是：

            Git 里有了提交（不可撤销）  +  DB 没记住   =  永久分叉
            下一次 checkpoint 拿陈旧的 expected 一比 → fail-closed → 整个 turn 炸
            → session failed，而且**再也自己好不了**

        2026-08-13 E2E v26 实测代价：平台 14:14 自己提交了一个 checkpoint，
        19:50 下一次 checkpoint 把**自己那条提交**判成外人，会话当场失败；
        而那时 8.9 小时的模拟还在跑，跑完时已经没有任何东西在看着它。

        新判据不依赖任何"希望被保存下来的记录"，只看 Git 里可重新读出来的事实：
        节点没有提交能力，所以 expected..current 之间只要**每一条都是平台做的**，
        就说明 DB 只是落后了，采纳磁盘 HEAD 继续即可。反之只要有一条不是，
        那才是真越权，照旧拒绝 —— 并且**指名是哪几条**，否则调用方只能重试。
        """
        if not expected:
            # 没有期望值（首次 checkpoint / 记录从未写成）——没有可比对的基线，
            # 不构成"有人越权"的证据。
            return []
        # 历史被改写（expected 不再是祖先）是最严重的形态：它意味着已经提交的
        # 东西被人动过。这种情况下**不看提交者是谁**，一律拒绝。
        rc, _ = self._git_exit_code(root, "merge-base", "--is-ancestor", expected, current)
        if rc != 0:
            return [f"{expected[:12]}..{current[:12]} (history rewritten or unrelated)"]
        raw = self._git(
            root, "log", "--format=%H%x1f%ce%x1f%B%x1e", f"{expected}..{current}"
        )
        foreign: list[str] = []
        for record in raw.split("\x1e"):
            record = record.strip("\n")
            if not record.strip():
                continue
            parts = record.split("\x1f")
            if len(parts) < 3:
                foreign.append(record[:12] + " (unreadable)")
                continue
            sha, committer_email, body = parts[0], parts[1], parts[2]
            platform_made = (
                committer_email == self.PLATFORM_COMMITTER_EMAIL
                and f"Session-ID: {session_id}" in body
            )
            if not platform_made:
                foreign.append(f"{sha[:12]} by {committer_email or '<unknown>'}")
        return foreign

    def _git_exit_code(self, cwd: Path, *args: str) -> tuple[int, str]:
        """需要看**退出码**的 git 调用（`merge-base --is-ancestor` 靠退出码表态）。

        `_git(check=False)` 只把 stdout 还给调用方 —— 拿它判"是不是祖先"会永远
        得到空串，判据静默失效。这类"用错通道所以门是哑的"踩过不止一次。
        """
        _refuse_the_event_loop_thread(args[0] if args else "git")
        try:
            result = subprocess.run(
                [self.git, *args],
                cwd=cwd,
                env={**os.environ},
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectRepositoryError("Git operation failed: merge-base") from exc
        return result.returncode, result.stdout

    def checkpoint_session_workspace(
        self,
        *,
        project_id: str,
        session_id: str,
        node_type: str,
        run_id: str,
        run_status: str,
        workspace_prefix: str,
        paths: list[str],
        expected_head_commit: str,
    ) -> WorkspaceCheckpoint:
        """Commit one node outcome through the Platform-owned Git boundary.

        Completion and publication are deliberately separate.  Useful partial
        work, blocker reports, and failed attempts remain visible on the
        Session branch instead of being hidden under audit refs or deleted.
        The node reports paths but never receives Git authority.
        """
        workspace = self.session_status(project_id, session_id)
        return self._checkpoint_worktree(
            project_id=project_id,
            session_id=session_id,
            root=Path(workspace.path),
            branch=workspace.branch,
            node_type=node_type,
            run_id=run_id,
            run_status=run_status,
            workspace_prefix=workspace_prefix,
            paths=paths,
            expected_head_commit=expected_head_commit,
        )

    def checkpoint_child_lane(
        self,
        *,
        project_id: str,
        session_id: str,
        lane_id: str,
        node_type: str,
        run_id: str,
        run_status: str,
        workspace_prefix: str,
        paths: list[str],
    ) -> WorkspaceCheckpoint:
        """lane 里的 checkpoint —— 与 session 那条**同一份实现**（RFC P2-a）。

        两条到达路径逐字做同一件事，所以只有一份实现。各写一份的下场是可以
        预演的：lane 那份少走一次冻结账本、或者少一条 trailer，于是"子节点跑
        出来的历史"和"父节点跑出来的历史"在库里长得不一样，而两边都不报错。

        ## 它**没有** `expected_head_commit` 参数

        session 那条的 expected 来自 DB 里的一行（会落后、会分叉，D6 那次事故
        就出在它身上）。lane 不需要那份记录：基线是 `merge-base(session, lane)`
        —— 从 Git 现算，不可能陈旧。「不可撤销的事发生后，别把判据建在希望被
        保存下来的记录上」。
        """
        repo = self.project_path(project_id)
        root = self.lane_path(project_id, session_id, lane_id)
        branch = self.lane_branch(session_id, lane_id)
        if not root.is_dir():
            raise ProjectRepositoryError(f"Child lane is not open: {lane_id}")
        return self._checkpoint_worktree(
            project_id=project_id,
            session_id=session_id,
            root=root,
            branch=branch,
            node_type=node_type,
            run_id=run_id,
            run_status=run_status,
            workspace_prefix=workspace_prefix,
            paths=paths,
            expected_head_commit=self._git(
                repo, "merge-base", self.session_branch(session_id), branch
            ),
        )

    def _checkpoint_worktree(
        self,
        *,
        project_id: str,
        session_id: str,
        root: Path,
        branch: str,
        node_type: str,
        run_id: str,
        run_status: str,
        workspace_prefix: str,
        paths: list[str],
        expected_head_commit: str,
    ) -> WorkspaceCheckpoint:
        safe_run_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(run_id)).strip("-.")[:128]
        safe_node = re.sub(r"[^A-Za-z0-9._-]+", "-", str(node_type)).strip("-.")[:128]
        if not safe_run_id or not safe_node:
            raise ProjectRepositoryError("Node checkpoint identity is invalid")
        with self._project_lock(project_id):
            current_head = self._git(root, "rev-parse", "HEAD")
            if current_head != expected_head_commit:
                foreign = self._commits_not_made_by_platform(
                    root,
                    expected=expected_head_commit,
                    current=current_head,
                    session_id=session_id,
                )
                if foreign:
                    raise ProjectRepositoryError(
                        "Session Git head changed outside Platform checkpoint authority: "
                        + ", ".join(foreign[:5])
                    )
                # 全是平台自己做的提交 —— 说明 DB 那条记录**落后了**，不是有人越权。
                # 采纳磁盘上的 HEAD 继续。见 `_commits_not_made_by_platform` 的说明。
                log.warning(
                    "session %s: adopting on-disk head %s over stale expectation %s "
                    "(all intervening commits are Platform-made)",
                    session_id, current_head[:12], (expected_head_commit or "<none>")[:12],
                )
            selection = self._validate_checkpoint_paths(
                root,
                project_id=project_id,
                node_type=node_type,
                run_id=run_id,
                workspace_prefix=workspace_prefix,
                paths=paths,
            )
            safe_paths = selection.paths
            dirty: set[str] = set()
            for chunk in _chunk_paths_for_argv(safe_paths):
                dirty.update(
                    self._git(
                        root,
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                        "--",
                        *chunk,
                    )
                    .strip()
                    .splitlines()
                )
            dirty.discard("")
            if not dirty:
                return WorkspaceCheckpoint(
                    commit_sha=None,
                    branch=branch,
                    paths=tuple(),
                    status=run_status,
                    oversized_excluded=selection.oversized,
                    bulk_excluded=selection.bulk,
                )
            message = f"node({safe_node}): checkpoint {safe_run_id}"
            trailers = (
                f"Node-Type: {safe_node}\nRun-ID: {safe_run_id}\n"
                f"Session-ID: {session_id}\nRun-Status: {run_status}"
            )
            if self._git(root, "diff", "--cached", "--name-only"):
                raise ProjectRepositoryError("Session worktree has unrelated staged changes")
            for chunk in _chunk_paths_for_argv(safe_paths):
                self._git(root, "add", "--all", "--", *chunk)
            frozen_violations = self._enforce_frozen_register(root)
            if not self._git(root, "diff", "--cached", "--name-only"):
                return WorkspaceCheckpoint(
                    commit_sha=None,
                    branch=branch,
                    paths=tuple(),
                    status=run_status,
                    frozen_violations=frozen_violations,
                    oversized_excluded=selection.oversized,
                    bulk_excluded=selection.bulk,
                )
            self._git(root, "commit", "-m", message, "-m", trailers)
            commit = self._git(root, "rev-parse", "HEAD")
            return WorkspaceCheckpoint(
                commit_sha=commit,
                branch=branch,
                paths=tuple(safe_paths),
                status=run_status,
                frozen_violations=frozen_violations,
                oversized_excluded=selection.oversized,
                bulk_excluded=selection.bulk,
            )

    #: 记录账本的位置（唯一定义在 app.services.frozen_register）。
    _LEDGER_RELATIVE = frozen_register.LEDGER_RELATIVE

    def _frozen_register(self, root: Path) -> dict[str, str]:
        """全 worktree 扫账本 → {相对路径: 冻结时内容 sha256}。

        判读实现在 `app.services.frozen_register`（纯 stdlib，零 app 依赖）——
        它是 harness `core.ledger.RecordStore.pinned` 的镜像，两边由
        `tests/test_frozen_register_contract.py` 钉住。拆成独立模块是为了那条
        契约测试在**两边的 CI**里都 import 得进来：长在本模块里时它会经
        `app.config` 拉起 pydantic_settings，harness 的 job 不装后端依赖，
        守卫于是只能 skip —— 而 skip 不是红色，没人会看见。
        """
        return frozen_register.scan_worktree(root)

    def _enforce_frozen_register(self, root: Path) -> tuple[tuple[str, str], ...]:
        """冻结路径的改动不许进 commit。在 `git add` 之后、commit 之前调用。

        对每个已暂存、且在登记表里的路径：内容 hash 与登记不符时 ——
        - HEAD 里的版本能对上登记 hash → `checkout HEAD -- path`，把工作区和
          暂存区都恢复成冻结内容（可验证的还原，改动彻底消失）；
        - HEAD 也对不上（守卫上线前漏进去的历史）→ 只 `reset` 出暂存区，改动
          留在工作区但进不了库，等人处理。
        守卫拿不到登记表就当没有（空表 = 无冻结承诺，不拦任何东西）；但登记
        表**本身**的行只增不改 —— 它也是冻结路径的一部分吗？不是：追加新冻结
        条目是合法写入，这里不拦登记表自身。
        """
        registry = self._frozen_register(root)
        if not registry:
            return ()
        staged = [
            p for p in self._git(root, "diff", "--cached", "--name-only", "-z").split("\0")
            if p
        ]
        violations: list[tuple[str, str]] = []
        for rel in staged:
            expected = registry.get(rel)
            if expected is None:
                continue
            candidate = root / rel
            try:
                actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
            except OSError:
                actual = None          # 文件被删了也算改动
            if actual == expected:
                continue
            head_bytes = self._git_blob_bytes(root, f"HEAD:{rel}")
            head_sha = (
                hashlib.sha256(head_bytes).hexdigest() if head_bytes is not None else None
            )
            if head_sha == expected:
                self._git(root, "checkout", "HEAD", "--", rel)
                violations.append((rel, "restored"))
            else:
                self._git(root, "reset", "--", rel)
                violations.append((rel, "refused"))
        return tuple(violations)

    def _git_blob_bytes(self, cwd: Path, spec: str) -> bytes | None:
        """按字节取一个 blob（`_git` 是文本模式且 strip，算内容 hash 会失真）。"""
        try:
            result = subprocess.run(
                [self.git, "cat-file", "-p", spec],
                cwd=cwd, capture_output=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    def _git_env(self, cwd: Path, env: dict[str, str], *args: str, check: bool = True) -> str:
        try:
            result = subprocess.run(
                [self.git, *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectRepositoryError(f"Git operation failed: {args[0]}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-1500:]
            raise ProjectRepositoryError(
                f"git {' '.join(args)} failed ({result.returncode}): {detail}"
            )
        return result.stdout.strip()

    def commit_main_files(
        self,
        *,
        project_id: str,
        expected_main_commit: str,
        files: dict[str, str],
        message: str,
        operation_id: str,
        actor_id: str,
        allowed_roots: frozenset[str] = frozenset(
            {"project.yaml", "PROJECT.md", "MEMORY.md", "access", "resources", "sources",
             ".research"}
        ),
    ) -> str:
        """Commit an authorized Project metadata projection directly to main."""
        if not files:
            raise ProjectRepositoryError("Cannot commit an empty Project metadata update")
        repo = self.project_path(project_id)
        safe_files = {
            self._safe_relative_path(relative): content for relative, content in files.items()
        }
        if any(PurePosixPath(path).parts[0] not in allowed_roots for path in safe_files):
            raise ProjectRepositoryError("Project metadata update escaped governed roots")
        with self._project_lock(project_id):
            existing = self._git(
                repo,
                "log",
                DEFAULT_BRANCH,
                "--format=%H",
                "--grep",
                f"^Operation-ID: {operation_id}$",
                "-n",
                "1",
                check=False,
            )
            if existing:
                return existing.splitlines()[0]
            head = self._git(repo, "rev-parse", DEFAULT_BRANCH)
            if head != expected_main_commit:
                raise ProjectRepositoryError("Project Git head changed during metadata update")
            if self._git(repo, "status", "--porcelain"):
                raise ProjectRepositoryError("Canonical Project worktree is not clean")
            for relative, content in safe_files.items():
                self._write(repo, relative, content)
            self._git(repo, "add", "--", *safe_files)
            if not self._git(repo, "diff", "--cached", "--name-only"):
                return head
            self._git(
                repo,
                "commit",
                "-m",
                message[:500],
                "-m",
                f"Operation-ID: {operation_id}\nActor-ID: {actor_id}",
            )
            return self._git(repo, "rev-parse", "HEAD")

    def publish_linear(
        self,
        *,
        project_id: str,
        session_id: str,
        expected_main_commit: str,
        paths: list[str],
        message: str,
        change_set_id: str,
        actor_id: str,
    ) -> str:
        """Replay approved Session paths onto main and create one linear commit."""
        repo = self.project_path(project_id)
        workspace = self.session_status(project_id, session_id)
        source = Path(workspace.path)
        safe_paths = [self._safe_relative_path(path) for path in paths]
        if not safe_paths:
            raise ProjectRepositoryError("Cannot publish an empty path set")
        with self._project_lock(project_id):
            existing = self._git(
                repo,
                "log",
                DEFAULT_BRANCH,
                "--format=%H",
                "--grep",
                f"^Change-Set-ID: {change_set_id}$",
                "-n",
                "1",
                check=False,
            )
            if existing:
                return existing.splitlines()[0]
            head = self._git(repo, "rev-parse", DEFAULT_BRANCH)
            if head != expected_main_commit:
                raise ProjectRepositoryError("Project Git head changed; refresh before publishing")
            if self._git(repo, "status", "--porcelain"):
                raise ProjectRepositoryError("Canonical Project worktree is not clean")
            try:
                return self._stage_and_commit_change_set(
                    repo=repo,
                    source=source,
                    safe_paths=safe_paths,
                    session_id=session_id,
                    change_set_id=change_set_id,
                    actor_id=actor_id,
                    message=message,
                    workspace=workspace,
                )
            except Exception:
                # 发布必须原子。此前任何一步失败都把已拷进来的文件留在 canonical
                # 里，于是**后续每一次 publish 都撞 "not clean"，永久堵死**
                # （E2E v19 实测：一条失效 pathspec 让整个项目再也发不出去）。
                # 复位到 HEAD —— 这个仓由平台独占，没有别人的在制品会被误伤。
                self._git(repo, "reset", "--hard", "HEAD", check=False)
                self._git(repo, "clean", "-fd", check=False)
                raise

    def _stage_and_commit_change_set(
        self,
        *,
        repo: Path,
        source: Path,
        safe_paths: list[str],
        session_id: str,
        change_set_id: str,
        actor_id: str,
        message: str,
        workspace: Any,
    ) -> str:
            # 只对**真的动过**的路径 add。change set 里可能有既不在 Session
            # 也不在 canonical 的路径（产物建了又删、从未发布过 —— E2E v19 实测：
            # 模型误造的重复 experiment_log 被清掉后，那条路径两边都没有）。
            # 拿这种 pathspec 调 git add 会 `fatal: did not match any files`，
            # 整个 publish 死掉；而它在语义上是 no-op，本就不该进 add 列表。
            touched: list[str] = []
            for relative in safe_paths:
                source_path = source / relative
                target_path = repo / relative
                if source_path.is_file():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, target_path)
                    touched.append(relative)
                elif target_path.exists():
                    target_path.unlink()
                    touched.append(relative)
            session_manifest = f".research/orchestration/sessions/{session_id}.yaml"
            source_manifest = source / session_manifest
            if source_manifest.is_file():
                target_manifest = repo / session_manifest
                target_manifest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_manifest, target_manifest)
                touched.append(session_manifest)
            if not touched:
                raise ProjectRepositoryError("Approved Session paths produced no canonical change")
            self._git(repo, "add", "--all", "--", *touched)
            if not self._git(repo, "diff", "--cached", "--name-only"):
                raise ProjectRepositoryError("Approved Session paths produced no canonical change")
            self._git(
                repo,
                "commit",
                "-m",
                message[:500],
                "-m",
                f"Change-Set-ID: {change_set_id}\nSession-ID: {session_id}\nActor-ID: {actor_id}",
            )
            published_commit = self._git(repo, "rev-parse", "HEAD")
            # Preserve every Session edit under an audit ref, then align the
            # reusable Session worktree with canonical main.  No commit becomes
            # unreachable, while subsequent ChangeSets start from the new CAS base.
            audit_ref = f"refs/audit/sessions/{session_id}/{workspace.head_commit}"
            self._git(repo, "update-ref", audit_ref, workspace.head_commit)
            self._git(source, "reset", "--hard", published_commit)
            # 发布把指针带上了 main；canonical 主干自己也要拿到字节，否则
            # "项目还没有会话"时界面读这份材料会 404。幂等、只补缺的。
            _materials = _materials_if_available()
            if _materials is not None:
                _materials.materialize(repo)
            return published_commit

    # ── 用户交来的文件（材料）────────────────────────────────────────────
    #
    # 落点、字节池、指针三件事全在 `core.materials` —— 平台与 CLI 共用一份，
    # 不在这里再实现一遍。本层只做一件平台独有的事：**提交**。
    #
    # 落在**会话分支**上，不再直接 commit canonical main。理由是那条老路留下
    # 的缺口：材料在 main 上，已存在的会话分支早就分出去了，于是"用户在会话里
    # 传了文件、这个会话的 agent 永远读不到"。跨会话复用走 publish —— 和其它
    # 任何改动同一条路，没有第二套账。

    def add_session_material(
        self,
        project_id: str,
        session_id: str,
        *,
        filename: str,
        stream: IO[bytes],
        uploaded_by: str,
        note: str = "",
        source: str = "upload",
        max_bytes: int | None = None,
    ) -> tuple[Any, str | None]:
        """把一份用户文件放进会话工作区并提交指针。返回 (材料, commit sha)。

        commit sha 为 None = 幂等命中（同名同内容重传），没有产生新提交。
        """
        # 这一步是本职工作，取不到模块就硬失败 —— 默默不做等于回执说"存好了"
        # 而盘上什么都没有。
        materials = materials_module()
        workspace = self.session_status(project_id, session_id)
        root = Path(workspace.path)
        with self._project_lock(project_id):
            reference, paths = materials.place(
                root,
                filename,
                stream,
                uploaded_by=uploaded_by,
                note=note,
                material_source=source,
                max_bytes=max_bytes,
            )
            commit = self._commit_platform_write(
                root,
                session_id=session_id,
                actor_id=uploaded_by,
                paths=paths,
                subject=f"materials: add {reference.name}",
                trailers=(
                    f"Material-SHA256: {reference.sha256}",
                    f"Material-Bytes: {reference.size_bytes}",
                ),
            )
        return reference, commit

    def _commit_platform_write(
        self,
        root: Path,
        *,
        session_id: str,
        actor_id: str,
        paths: Sequence[str],
        subject: str,
        trailers: Sequence[str] = (),
    ) -> str | None:
        """平台自己在会话分支上提交若干路径。返回 commit sha，无改动时 None。

        `Session-ID` 与平台提交者身份是 `_commits_not_made_by_platform` 的判据
        —— 没有它们，下一次 checkpoint 会把这条提交判成越权并让整个 turn 失败。
        所以提交形状只能有一处，别在第四个地方手抄一份。

        提交用 pathspec 限定：工作树里别人未提交的改动一个都不会被顺手带走。
        """
        safe = [self._safe_relative_path(value) for value in paths]
        if not safe:
            return None
        self._git(root, "add", "--", *safe)
        if not self._git(root, "diff", "--cached", "--name-only", "--", *safe):
            return None
        body = "\n".join([f"Session-ID: {session_id}", f"Actor-ID: {actor_id}", *trailers])
        self._git(root, "commit", "-m", subject, "-m", body, "--", *safe)
        return self._git(root, "rev-parse", "HEAD")

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()


def get_project_repository() -> GitProjectRepository:
    """Resolve configured roots at call time (important for tests and tenants)."""
    return GitProjectRepository()

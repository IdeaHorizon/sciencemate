"""平台所有持久化路径的**唯一来源**。

设计：所有可被用户配置 / 跨机器迁移的状态都根在 `$HARNESS_FRAMEWORK_HOME`
（默认 `~/.harness-framework`）下。这样：
  - 同事 clone 代码 ≠ clone 知识；要带 KB 来就 init 这个目录或挂载 / 同步它
  - 备份只备一处
  - run-local 状态（artifacts / transcript）跟 KB 同根，chunk 反查 artifact 不跨 fs

布局（v0.8 起项目嵌套 runs）：
  $HARNESS_FRAMEWORK_HOME/
    org/                          # 跨项目共享（concepts + org claims + chunks + skills）
    projects/<slug>/              # 项目持久（KB + memory + runs；交付物在项目工作区里，冻结即交付）
      kb_*.jsonl
      memory.jsonl
      PROJECT.md / PROJECT_MANIFEST.md
      runs/<run_id>/              # ★ v0.8 新：项目嵌套的 run-local 状态
        artifacts/                # 结构化 artifact JSON（save_artifact，有 provenance 账本）
        outputs/<node>/<kind>/    # ★ v0.9 新：节点副产物（见下）
        transcript.jsonl
        summary.json
        messages_checkpoint.json
    runs_anon/<run_id>/           # ★ v0.8 新：无 project_id 的 ad-hoc run
    runs/<run_id>/                # [deprecated] 旧 flat 布局；只读 fallback
    literature/                   # ★ v0.9 新：跨项目文献缓存（不属于任何 run/project）
      survey_index/  search_cache/  papers/  credentials/  reference/
    toolchain/                    # 这台机器**实测**出来的工具链事实（pdf.json）——
                                  # 最近一次实测的记录，不是配置；见 shared/lib/pdf_toolchain
    user/
      PROFILE.md
      identity.json

── 节点副产物布局（v0.9 起统一命名，v2.1 起改锚点）────────────────────────
在 v0.9 之前每个节点把副产物目录名硬编码在自己的 tool 里（`drafts/`、`figures/`、
`.postprocess/`、`deliverables/` …），core/paths.py 对它们一无所知，导致同一个 run
目录下七种命名风格并存，且 run 级 `deliverables/` 与项目级 `deliverables/` **同名
不同义**。v0.9 起统一由本模块的 `node_output_dir()` 及其具名 helper 给出，节点侧
不得再硬编码目录名。

**v2.1 workspace-first 改锚点**：绑了 Project worktree 时落

  <worktree>/<节点自己的目录>/<kind>/

没绑（CLI / fixture）时才落 `<run_root>/outputs/<node_type>/<kind>/`。
原因见 `node_output_dir` 的 docstring —— 平台把 run_root 放在 gitignored 的
`.research/cache/` 里，锚在那儿的产物出不了这一轮 run。绑定时不再多加一层
`outputs/`：节点目录本身就是它的产出，多一层会让"模型键入的相对路径"和
"工具算出来的产物路径"分成两个对不上的锚点。

  outputs/hypothesis/drafts/          （旧 <run>/drafts/）
  outputs/postprocess/figures/        （旧 <run>/figures/）
  outputs/postprocess/scratch/        （旧 <run>/.postprocess/）
  outputs/postprocess/data_cache/     （旧 <run>/.postprocess/data_cache/）
  outputs/writing/manuscript_project/ （旧 <run>/manuscript_project/）
  outputs/writing/bundles/            （旧 <run>/deliverables/ —— ★ 改名消歧）

**deliverables 消歧**：项目级不再有单独的交付物目录 —— 冻结的产物就是各节点目录里
那个文件，由账本（`.research/ledger/records.jsonl`）钉死，没有第二份拷贝。run 级的
投稿包 zip 是 run-local 中间产物，叫 `outputs/writing/bundles/`，不叫 deliverables。

**编译现场 ≠ 编译产物（latex）**：这两件事有两个目录，不是一个。
  `latex_scratch_dir()`  <run_root>/latex_build/<节点>/<name>/  编译现场，gitignored
  `latex_build_dir()`    <节点目录>/latex_build/<name>/         交付产物，进 Git
为什么必须分开见 `latex_build_dir` 的 docstring。

**尚未迁移（写入方在本次改动的文件白名单之外，暂留原位但真相源仍在本模块）**：
  <run>/data_preprocessing/   —— 写入方散落在 nodes/data/tools/ 多个文件
这一处走 `data_workspace_dir()`，标了 LEGACY_IN_PLACE。

**向后兼容**：旧 run 目录里的产物用 `prefer_existing()` 读回退 —— 新路径不存在而旧
路径存在时返回旧路径，所以历史 run 仍可读；新 run 一律写新路径。

环境变量覆盖（按优先级）：
  HARNESS_FRAMEWORK_HOME       —— 顶层 root
  HARNESS_RUNS_ROOT            —— run 账本父目录（写方显式解析 base_dir 时发布，
                                  读方据此找账本；见 `runs_parent`）
  HARNESS_FRAMEWORK_ORG_HOME   —— 单独覆盖 org/ 路径（用于跨机器共享 org KB）
  HARNESS_FRAMEWORK_PROJECTS_HOME —— 单独覆盖 projects/ 路径（一个项目的成员共用一份项目层）
  HARNESS_LITERATURE_HOME      —— 单独覆盖 literature/ 缓存根
  HARNESS_CAS_JOURNAL_RANKING  —— 中科院期刊分区表 xlsx（用户提供的输入数据）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def default_home() -> Path:
    """没有 `HARNESS_FRAMEWORK_HOME` 时的默认数据根 —— 「默认根在哪」只在这里回答。

    POSIX：`~/.harness-framework`。**Windows：`%LOCALAPPDATA%\\afs`** —— Windows 应用把
    数据放在 `LOCALAPPDATA`（`C:\\Users\\<u>\\AppData\\Local`）下，不是 `~/.` 点目录（那是
    POSIX 惯例）。RFC #848 决策 C；wangd 09-05 定的 `~/.harness-framework` 只对 POSIX。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "afs"
    return Path.home() / ".harness-framework"


def home() -> Path:
    """顶层 root。设了 `HARNESS_FRAMEWORK_HOME` 用它，否则 :func:`default_home`。"""
    override = os.getenv("HARNESS_FRAMEWORK_HOME")
    return Path(override) if override else default_home()


def org_root() -> Path:
    """org 层（跨项目共享）。可单独覆盖。"""
    return Path(os.getenv(
        "HARNESS_FRAMEWORK_ORG_HOME",
        str(home() / "org"),
    ))


def runs_root() -> Path:
    """[v0.8 起 deprecated] 旧 flat runs/ 目录。v0.8 起所有 run 走项目嵌套
    (`projects/<id>/runs/`)；anon run 走 `runs_anon/`。本函数保留只为：
      (a) backward-compat 读旧数据
      (b) STATE_DIR env var 旧语义
      (c) `ensure_dirs()` 仍创建（不会用但留着不破坏旧 install）
    新代码请用 `runs_parent(project_id)`。"""
    return home() / "runs"


def runs_anon_root() -> Path:
    """[v0.8 新] 无 project_id 的 ad-hoc run 落这（fixture / smoke / single-node test）。"""
    return home() / "runs_anon"


def runs_parent(project_id: str | None) -> Path:
    """[v0.8 新] **新代码统一入口**：给定 project_id 返本项目 runs 父目录。

    `HARNESS_RUNS_ROOT` 设 → 直接返它（**账本在别处**，见下）
    project_id 设 → `projects/<id>/runs/`  （项目嵌套）
    project_id None → `runs_anon/`          （ad-hoc）

    State.new / chat.py / dogfood driver / executor 都该用本函数算 base_dir。
    子 run 通过 `state.root.parent` 自动继承父 dir，所以一处对了全套对。

    ── 为什么需要 HARNESS_RUNS_ROOT ──────────────────────────────────────
    写方（platform_runtime / chat.py / dogfood driver）都是**显式**解析
    base_dir 的：平台底座传 `state_dir`，run 落在会话 worktree 的
    `.research/cache/runtime/runs/`，而不是 `$HARNESS_FRAMEWORK_HOME` 底下。
    但读方只有 project_id 可用，于是照本函数的老实现去 `projects/<id>/runs/`
    翻账本 —— 那里永远是空的。

    后果不是理论上的（2026-08-21 实测）：`last_dreaming_at()` 因此永远判
    "从未 dream 过"，把一个做完的研究钉死 6.5 小时；
    `decision_package._producer_was_truncated()` 因此恒返 None，PR#398 的
    "截断≠做完"机械送达在平台上整个失效（这条更阴，fail-open，不死锁，
    只是安静地烧钱）。

    修法不是让每个读方各自去猜账本在哪 —— 那是把分裂往下挪一层。写方**已经
    知道**真相，让它把真相发布出来，读方问同一个地方。CLI 不设这个变量，
    行为完全不变。
    """
    explicit_root = os.getenv("HARNESS_RUNS_ROOT")
    if explicit_root:
        return Path(explicit_root)
    if project_id:
        return projects_root() / project_id / "runs"
    return runs_anon_root()


def projects_root() -> Path:
    """项目层（一个项目一份：KB、记忆、作业账本、runs）。可单独覆盖。

    **项目知道的属于项目，不属于说话人**（`docs/RFC_PROJECT_HOME_20260924.md`）。平台给每个人
    一个 harness home（身份、画像在那里），但一个项目的成员共用一份项目层 —— 所以平台**告诉**
    worker 与桥项目层在哪（`HARNESS_FRAMEWORK_PROJECTS_HOME`，和组织层同一个做法），没说就是
    这个 home 自己的 `projects/`（个人 CLI 用法不变）。

    这是项目层路径的**唯一**构造点：别处拼 `home() / "projects"` 就绕开了平台说的那一份
    （`tests/test_a_project_has_one_home.py` 扫盘）。
    """
    return Path(os.getenv("HARNESS_FRAMEWORK_PROJECTS_HOME", str(home() / "projects")))


def project_dir(project_id: str | None) -> Path | None:
    if not project_id:
        return None
    return projects_root() / project_id


def run_dir(run_id: str, project_id: str | None = None) -> Path:
    """[v0.8 起] 计算 run 目录路径。

    project_id 给 → `projects/<id>/runs/<run_id>/`
    project_id None → `runs_anon/<run_id>/`

    旧调用 `run_dir(run_id)` 不传 project_id → 落 `runs_anon/`，跟之前
    `runs/<run_id>/` 不一样，所以**任何旧代码调本函数都需复审**。本仓库内
    所有调用点已经在 v0.8 切到 `runs_parent(project_id) / run_id` 或更高层
    helper。如果有外部依赖请走 `find_run_dir(run_id)` 跨布局查。
    """
    return runs_parent(project_id) / run_id


def find_run_dir(run_id: str) -> Path | None:
    """[v0.8 新] 跨布局查 run 目录。返第一个命中的；否则 None。

    搜索顺序（按 cost 升序）：
      1. runs_anon/<run_id>/                       (新 anon)
      2. projects/<any>/runs/<run_id>/             (新项目嵌套，需扫所有项目)
      3. runs/<run_id>/                            (旧 flat HARNESS_FRAMEWORK_HOME 内)
      4. STATE_DIR/<run_id>/                       (旧 env var fallback)

    用于 read_external_artifact 这类跨 run 引用 —— caller 不必关心 run 在哪。
    """
    # 1. anon
    p = runs_anon_root() / run_id
    if p.exists():
        return p
    # 2. 项目嵌套：扫 projects/
    pr = projects_root()
    if pr.exists():
        for proj in pr.iterdir():
            if not proj.is_dir():
                continue
            cand = proj / "runs" / run_id
            if cand.exists():
                return cand
    # 3. 旧 flat
    p = runs_root() / run_id
    if p.exists():
        return p
    # 4. STATE_DIR (deprecated)
    state_dir = os.getenv("STATE_DIR")
    if state_dir:
        p = Path(state_dir) / run_id
        if p.exists():
            return p
    return None


# ══════════════════════════════════════════════════════════════════════════
# run 级节点副产物（v0.9 / issue #166.4）—— 本段是这些目录名的**唯一真相源**
# ══════════════════════════════════════════════════════════════════════════

NODE_OUTPUTS_DIRNAME = "outputs"

#: 规范 kind 名。节点侧引用常量而不是字面量，改名只改这里。
HYPOTHESIS_DRAFTS = ("hypothesis", "drafts")
POSTPROCESS_FIGURES = ("postprocess", "figures")
POSTPROCESS_SCRATCH = ("postprocess", "scratch")
POSTPROCESS_DATA_CACHE = ("postprocess", "data_cache")
WRITING_MANUSCRIPT = ("writing", "manuscript_project")
WRITING_BUNDLES = ("writing", "bundles")

#: 新 canonical 相对路径 → v0.9 之前的旧相对路径。只用于**读回退**，不再写。
LEGACY_RUN_SUBDIRS: dict[str, str] = {
    "outputs/hypothesis/drafts": "drafts",
    "outputs/postprocess/figures": "figures",
    "outputs/postprocess/scratch": ".postprocess",
    "outputs/postprocess/data_cache": ".postprocess/data_cache",
    "outputs/writing/manuscript_project": "manuscript_project",
    "outputs/writing/bundles": "deliverables",
}


def node_outputs_root(state: Any) -> Path:
    """本 run 的节点副产物公共父目录。

    绑了 Project worktree 就是 `<worktree>/<本节点目录>/`；否则退回
    `<run_root>/outputs/`（CLI / fixture 等无项目的 run）。
    """
    return _node_anchor(state, getattr(state, "node_type", ""))


def node_output_dir(
    state: Any,
    node_type: str,
    kind: str = "",
    *,
    create: bool = False,
) -> Path:
    """**统一入口**：某个节点这一轮的非 artifact 副产物目录。

    node_type 用节点名（hypothesis / postprocess / writing / data …），kind 是该
    节点内部的产物类别（drafts / figures / scratch / bundles …）。kind 可为多段
    （`"a/b"`），空则返回节点根。

    任何节点想落一个非 artifact 的副产物文件，都必须经过本函数或它的具名
    wrapper —— 不允许再出现 `state.root / "某个字面量"`。

    ── 锚点（v2.1 workspace-first）─────────────────────────────────────────
    绑了 Project worktree：`<worktree>/<node_type 的节点目录>/<kind>/`。
    没绑（CLI / fixture）：`<run_root>/outputs/<node_type>/<kind>/`。

    绑定时**不再多加一层 `outputs/`**：节点目录本身就是这个节点的产出，多一层
    只会让"模型键入的相对路径"和"工具算出来的产物路径"分成两个锚点 ——
    `working_directory()` 给的是 `<worktree>/writing/`，多一层就变成
    `<worktree>/writing/outputs/sci_manuscript`，于是 prepare 建在一处、
    compile/stage 找在另一处，谁都没错但对不上。两个锚点必须重合。

    以前无条件锚在 `<run_root>` 上。平台把 run_root 放在
    `<worktree>/.research/cache/` 里，而那是 **gitignored 的缓存**，于是所有节点
    副产物 —— 图、manuscript 工程、投稿包 —— 都：进不了 checkpoint、发布不到
    project main、下一个 session 的 worktree 里一张图都没有（E2E v19/v20 实测：
    两张 fig PNG 只存在于上一个 session 的 cache 目录里，论文里的
    `\\includegraphics` 因此无源可引）。产物属于产它的节点，就该落在那个节点
    自己的 Git 目录里；缓存目录只留 transcript 这类真正一次性的东西。

    传 node_type 而不是只看 `state.node_type`，是因为**跨节点读**是合法的：
    writing 要读 postprocess 的图。写边界另有 `resolve_tool_path` 把关，本函数
    只负责回答"那个节点的产物在哪"。
    """
    if not node_type or "/" in node_type or ".." in node_type:
        raise ValueError(f"invalid node_type: {node_type!r}")
    d = _node_anchor(state, node_type)
    if kind:
        parts = [p for p in str(kind).split("/") if p]
        if any(p == ".." for p in parts):
            raise ValueError(f"invalid kind: {kind!r}")
        # 目录名已经说明了它是什么时不再重复一层：postprocess 的目录叫 `figures/`，
        # 它的 figures 产物就直接在 `figures/` 下，而不是 `figures/figures/`。
        # 没绑 worktree 时锚点是 `outputs/<node_type>/`，不会撞上，照旧加层。
        if parts and d.name == parts[0]:
            parts = parts[1:]
        for p in parts:
            d = d / p
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def display_relpath(state: Any, path: Path | str) -> str:
    """把一个绝对路径写进返回值 / artifact 元数据时该用的相对形式。

    锚点优先级：Project worktree → run 根 → 原样绝对路径。

    以前各处直接写 `str(p.relative_to(state.root))`。产物锚点搬进 worktree 之后
    那句话会**抛 ValueError** —— 而且就算不抛，run-root 相对路径对别的节点也毫无
    意义（它们看不见这一轮的缓存目录）。worktree 相对路径才是跨节点能解析的那个。
    """
    p = Path(str(path))
    if not p.is_absolute():
        return p.as_posix()
    for anchor in (getattr(state, "project_worktree", None), getattr(state, "root", None)):
        if anchor is None:
            continue
        try:
            return p.resolve().relative_to(Path(str(anchor)).resolve()).as_posix()
        except ValueError:
            continue
    return p.as_posix()


def display_anchors(state: Any) -> list[Path]:
    """`display_relpath` 用的锚点，**按它用的顺序**。写方读方共用这一条列表。

    抄一份顺序出去就等于开一个新真相源：写方改了优先级、读方还按老顺序解，
    同一个字符串在两端指向两个地方，而且两边都不报错。
    """
    anchors: list[Path] = []
    for anchor in (getattr(state, "project_worktree", None), getattr(state, "root", None)):
        if anchor is None:
            continue
        resolved = Path(str(anchor)).resolve()
        if resolved not in anchors:
            anchors.append(resolved)
    return anchors


def display_relpath_candidates(state: Any, value: Path | str) -> list[Path]:
    """一个存下来的相对路径**可能**指向的全部绝对路径，按锚点优先级排。

    给错误信息用：锚点分叉时"文件不存在"和"我找错地方了"长得一模一样，
    把试过的位置全列出来，下一次这类分叉一行就能认出来，不用再查 18 天。
    """
    p = Path(str(value)).expanduser()
    if p.is_absolute():
        return [p.resolve()]
    return [(anchor / p).resolve() for anchor in display_anchors(state)]


def resolve_display_relpath(state: Any, value: Path | str) -> Path:
    """`display_relpath` 的**逆**。读一个写进记录里的相对路径只能走这里。

    写方按 `display_anchors` 的顺序挑锚点，读方就必须按同一顺序回解 ——
    这两个函数是一对，改一个必须改另一个。

    自己拼 `state.root / rel` 在绑了 Project worktree 的 run 上**必错**：产物落在
    `<worktree>/<node>/…`，run 根却在 `<worktree>/.research/cache/runtime/runs/<id>/`
    底下，拼出来的路径从来没存在过。而且它不报错，只是"文件不存在" —— 与"图真的
    没渲染出来"长得完全一样。2026-08-09 锚点搬进 worktree 时只改了写方，六个读方
    原地留了 18 天，平台上配图闸从那天起一次都没通过过，测试却全绿（测试不绑
    worktree，写读恰好同锚）。

    落点选择：命中的第一个存在的候选；一个都不存在时返回**首选锚点**下的那个，
    好让错误信息指向它本该在的地方，而不是指向一个碰巧算得出来的位置。
    """
    candidates = display_relpath_candidates(state, value)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else Path(str(value)).expanduser().resolve()


def within_run_scope(state: Any, path: Path | str) -> bool:
    """这个路径是否落在**本 run 自己的树**里（Project worktree 或 run 根）。

    这是一个**出处**问题，不是权限问题：交付物声明指向的文件必须是这一轮真正
    产出的东西，而不是随手引的外部路径。权限问题走
    `core.project_workspace.resolve_tool_path` —— 两者判据不同，别互相顶替
    （读是允许越出本树的，但那不代表越出去的东西可以当交付物）。
    """
    p = Path(str(path)).resolve()
    return any(p == root or p.is_relative_to(root) for root in display_anchors(state))



def _node_anchor(state: Any, node_type: str) -> Path:
    """某个节点这一轮产物的根目录。

    绑了 worktree 就是那个节点在 Project Git 里的目录；否则
    `<run_root>/outputs/<node_type>/`。

    **本节点的这个目录同时就是 `working_directory()`** —— 即模型键入的相对路径
    的锚点。方向是单向的：`working_directory` 来问这里，这里不回头问它（互相
    调用会成环）。这条重合是硬约束：分成两个锚点，"谁建的"和"谁来找"就对不上，
    而且两边都不报错（E2E v20：prepare 把 manuscript 建在一处，stage/compile 在
    另一处找，图始终进不了稿子）。

    唯一的例外是调度器（`project_workspace._SCRATCH_ANCHORED`）：它的相对路径
    锚在 run 目录 `scratch/`，产物仍锚在它的作用域 `notes/`。这个分叉是刻意的
    ——草稿归 run、产物归记录——而且只对它成立：它没有 prepare/stage 那种
    "先建再找"的多步流程，compile_latex 从它指名的源目录读、往 notes/ 下写。
    """
    worktree = getattr(state, "project_worktree", None)
    if worktree is not None:
        owned = _worktree_node_dir(worktree, node_type or getattr(state, "node_type", ""))
        if owned is not None:
            return owned
    return _run_root(state) / NODE_OUTPUTS_DIRNAME / (
        node_type or str(getattr(state, "node_type", "") or "node")
    )


def _run_root(state: Any) -> Path:
    """本 run 的根目录。

    只接受 State（或具备 `.root` 的替身）。**不再兼容直接传一个 Path** ——
    `Path.root` 是 `"/"`，传错了不会报错，只会把产物静默指到文件系统根目录。
    """
    if isinstance(state, (str, Path)):
        raise TypeError(
            "node output paths take the State, not a run-root path "
            "(锚点要看 state.project_worktree / node_type，只给根目录算不出来)"
        )
    root = getattr(state, "root", None)
    if root is None:
        raise ValueError("state must expose .root")
    return Path(str(root))


def _worktree_node_dir(worktree: Path | str, node_type: str) -> Path | None:
    """节点在 Project worktree 里自己的目录；文件作用域/未知节点返回 None。

    作用域表在 `core.project_workspace` —— 那里是分配作用域的地方，这里只读它。
    再抄一份名单，改一处忘一处就会让产物落到别人的目录里。
    """
    from core.project_workspace import _NODE_WORKSPACES

    owned = _NODE_WORKSPACES.get(str(node_type or ""))
    if not owned:
        return None
    relative = Path(owned)
    if relative.suffix:  # 文件作用域（curator 只拥有 MEMORY.md），没有产物目录
        return None
    return Path(str(worktree)) / relative


# `node_output_relpath()` 已删除（2026-08-27）。
#
# 它宣称自己产出"写进 artifact 元数据用"的相对路径，但那个字符串是**凭布局硬拼**
# 的（`outputs/<node>/<kind>`），与产物真正的落点无关：绑了 Project worktree 时
# 文件在 `<worktree>/<node>/<kind>/`，两个真锚点都对不上。这就是第三个锚点。
#
# 一条相对路径只有两种合法出身，各自成对：
#   写进记录 → `display_relpath()`                ← `resolve_display_relpath()`
#   交给模型 → `project_workspace.tool_relpath()` ← `resolve_tool_path()`
# 没有第三种。要目录本身用 `node_output_dir()`。


def prefer_existing(state_root: Path | str, canonical: Path) -> Path:
    """向后兼容读回退。

    新 canonical 路径不存在、而它对应的 v0.9 之前旧路径存在时返回旧路径；否则一律
    返回 canonical。**只影响读**：新产物永远写 canonical。
    """
    canonical = Path(canonical)
    if canonical.exists():
        return canonical
    root = Path(str(state_root))
    try:
        rel = canonical.relative_to(root).as_posix()
    except ValueError:
        return canonical
    for new_rel, old_rel in LEGACY_RUN_SUBDIRS.items():
        if rel == new_rel:
            legacy = root / old_rel
        elif rel.startswith(new_rel + "/"):
            legacy = root / old_rel / rel[len(new_rel) + 1:]
        else:
            continue
        if legacy.exists():
            return legacy
    return canonical


# ── 具名 helper（节点侧只调这些）──────────────────────────────────────────
def hypothesis_drafts_dir(state: Any, *, create: bool = False) -> Path:
    return node_output_dir(state, *HYPOTHESIS_DRAFTS, create=create)


def postprocess_figures_dir(state: Any, *, create: bool = False) -> Path:
    return node_output_dir(state, *POSTPROCESS_FIGURES, create=create)


def postprocess_scratch_dir(state: Any, *, create: bool = False) -> Path:
    """postprocess 生成的绘图代码 / 调试残留（旧 `.postprocess/`）。"""
    return node_output_dir(state, *POSTPROCESS_SCRATCH, create=create)


def postprocess_data_cache_dir(state: Any, *, create: bool = False) -> Path:
    return node_output_dir(state, *POSTPROCESS_DATA_CACHE, create=create)


def writing_manuscript_dir(
    state: Any,
    name: str = "manuscript_project",
    *,
    create: bool = False,
) -> Path:
    """LaTeX 手稿工程目录。name 允许多个并存（如 `manuscript_project_clean`）。"""
    return node_output_dir(state, "writing", name or "manuscript_project", create=create)


def writing_bundles_dir(state: Any, *, create: bool = False) -> Path:
    """★ run 级投稿包目录（旧名 `deliverables/`；项目级没有交付物目录，冻结即交付）。"""
    return node_output_dir(state, *WRITING_BUNDLES, create=create)


LATEX_BUILD = "latex_build"


def latex_build_dir(
    state: Any,
    node_type: str = "",
    output_name: str = "",
    *,
    create: bool = False,
) -> Path:
    """LaTeX **产物**目录：`<调用节点的产物目录>/latex_build/[<output_name>]`。

    `compile_latex` 是框架级工具，任何节点都能调，所以默认锚在**调用它的节点**
    上，而不是写死 writing。

    ## 这里只放交付物，编译在别处（`latex_scratch_dir()`）

    最早编译现场和产物都在 `<run_root>/latex_build/` —— 那是 gitignored 的 run
    缓存，于是编出来的 PDF 跟着缓存一起蒸发，交付物出不了这一轮 run。修法是把
    整个目录搬进节点的 Git 目录，**现场也跟着搬了进来**。

    于是每次 `compile_latex` 都在被观测、会入库的工作区里重建一份完整的源码
    副本 + TeX 中间件（.aux/.log/.out/.bbl/.pdf）。代价（2026-08-19 实测）：
      · 一次编译 = 18 个新文件、64 KB 的 diff 正文，直接顶爆协议单帧上限，
        一条跑了 63 分钟的 run 被自己的观测事件杀死；
      · 稿子的每个 .tex 在 worktree 里存在 3 份（源工程 + review 构建 +
        clean 构建），"一个问题一个真相源"当场破掉；
      · 源工程天然包含构建目录，`_copy_project_tree` 得反复处理"别把自己拷进
        自己"（E2E v22 连撞 4 次"不能与 latex_build 重叠"）。

    "PDF 要活过这一轮"和"编译需要一块可以随便清空的场地"是**两个**需求，
    合成一个目录就必然二选一。分开之后两个都成立：现场回到 run 缓存，产物
    留在这里 —— 而且这里从此只有 PDF + build.json（编译出处），diff 恒定
    在几百字节，不随稿件长度增长。
    """
    nt = node_type or str(getattr(state, "node_type", "") or "writing")
    d = node_output_dir(state, nt, LATEX_BUILD, create=create)
    return d / output_name if output_name else d


def latex_scratch_dir(
    state: Any,
    node_type: str = "",
    output_name: str = "",
    *,
    create: bool = False,
) -> Path:
    """LaTeX **编译现场**：`<run_root>/latex_build/<node_type>/[<output_name>]`。

    源码副本、TeX 中间件、失败时的日志都落这里。run 缓存是 gitignored 的，
    所以现场既不进 checkpoint、也不进工作区观测事件 —— 它本来就该是一块
    可以随便 rmtree 的场地。成功编译后由 `compile_latex` 把 PDF 提升到
    `latex_build_dir()`。

    不走 `node_output_dir()`：那个函数的语义是"这个节点的**产出**在哪"，绑了
    worktree 就必然落进 Git 目录。现场恰恰要求相反，锚点只能是 run 根。
    """
    nt = node_type or str(getattr(state, "node_type", "") or "writing")
    d = _run_root(state) / LATEX_BUILD / nt
    if output_name:
        d = d / output_name
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def writing_latex_build_dir(state: Any, output_name: str = "") -> Path:
    return latex_build_dir(state, "writing", output_name)


# ── LEGACY_IN_PLACE：真相源在这里，但物理位置暂未迁移（写入方在白名单外）──


def data_workspace_dir(state_root: Path | str, sub: str = "") -> Path:
    """LEGACY_IN_PLACE `<run>/data_preprocessing/[<sub>]` —— data 节点共享工作区。

    写入方散落在 nodes/data/tools/ 的 mesh_generator / cfd_case_router /
    web_search / atomic_structure_recovery / coordinate_profile 等多个文件里，且
    `_detect_existing_cfd_mesh_assets()` 会 rglob 整个工作区找已有网格资产 ——
    只搬其中一个子目录会让扫描漏掉资产，所以整体保持原位，等一次性迁移。
    """
    d = Path(str(state_root)) / "data_preprocessing"
    return d / sub if sub else d


def data_package_dir(state_root: Path | str, namespace: str = "") -> Path:
    """data 节点的 preprocessing package 目录。

    `namespace` 空 = 历史上那个唯一的包（`preprocessing_package`）。一次 run 交付
    多个包时，由 `package_publisher.package_paths()` 传一个已规范化的命名空间
    （`<delivery_name>__<run_id>`）—— **终点仍然只由本函数算出来**。多包是节点的
    需求，"包落在哪一层目录"是布局，布局的真相源只有 core.paths 一个；节点侧自己
    `data_workspace_dir(...) / 名字` 就是把这件事拆成了两个会各自演化的答案。

    `state_root` 也允许传 project workspace 根（data 绑了 Project 时的耐久锚点），
    本函数只负责它下面那一段。
    """
    return data_workspace_dir(state_root, namespace or "preprocessing_package")


# ══════════════════════════════════════════════════════════════════════════
# 跨项目文献缓存（v0.9：把散在 $HOME 各处的硬编码路径收进框架 root）
# ══════════════════════════════════════════════════════════════════════════

def literature_root() -> Path:
    """`$HARNESS_FRAMEWORK_HOME/literature/`。

    这一层**故意**平级于 projects/：论文 PDF、SQLite 索引、搜索缓存都是跨项目复用
    的，绑到某个 run/project 会重复下载同一篇 PDF。可用 HARNESS_LITERATURE_HOME
    单独覆盖（挂到大盘 / 共享网盘）。
    """
    return Path(os.getenv("HARNESS_LITERATURE_HOME", str(home() / "literature")))


def literature_index_dir() -> Path:
    return literature_root() / "survey_index"


def literature_cache_dir() -> Path:
    return literature_root() / "search_cache"


def literature_papers_dir() -> Path:
    """下载的论文 PDF / 仅索引 JSON（旧 `~/survey-harness/papers`）。"""
    return literature_root() / "papers"


def literature_credentials_dir() -> Path:
    """站点 cookie 等凭据（旧 `~/.hermes/`）。"""
    return literature_root() / "credentials"


def literature_reference_dir() -> Path:
    """用户提供的参考数据（期刊分区表等），非平台产物。"""
    return literature_root() / "reference"


def toolchain_facts_dir() -> Path:
    """这台机器实测出来的工具链事实放哪（跨项目，和 literature/ 同一层）。

    里面是**最近一次实测的记录**（什么时候、用哪套工具、编没编过），不是配置 ——
    改它不会让任何东西变好，删了下一次启动会重新实测。
    """
    return home() / "toolchain"


#: v0.9 之前跑到框架外的 $HOME 硬编码路径。只用于读回退。
LEGACY_LITERATURE_DIRS: dict[str, Path] = {
    "papers": Path.home() / "survey-harness" / "papers",
    "credentials": Path.home() / ".hermes",
}


def legacy_literature_dir(kind: str) -> Path | None:
    """旧 $HOME 位置，存在才返回（迁移期读回退用）。"""
    p = LEGACY_LITERATURE_DIRS.get(kind)
    return p if p is not None and p.exists() else None


#: 期刊分区表候选文件名（用户导出的原始中文文件名也认，但不写死成唯一来源）。
CAS_RANKING_FILENAMES = (
    "cas_journal_ranking.xlsx",
    "2025中科院期刊分区表excel完整版.xlsx",
)


def cas_journal_ranking_path() -> Path | None:
    """中科院期刊分区表 —— **用户提供的输入数据**，不是平台产物。

    查找顺序：
      1. `$HARNESS_CAS_JOURNAL_RANKING`（显式指定文件路径，最高优先级）
      2. `<literature_root>/reference/<CAS_RANKING_FILENAMES>`
      3. `~/<CAS_RANKING_FILENAMES>`（v0.9 之前的硬编码位置，读回退）
    找不到返回 None —— 调用方负责给出可操作的报错，不要静默降级到某个中文文件名。
    """
    env = os.getenv("HARNESS_CAS_JOURNAL_RANKING")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    for base in (literature_reference_dir(), Path.home()):
        for name in CAS_RANKING_FILENAMES:
            p = base / name
            if p.exists():
                return p
    return None


def cas_journal_ranking_hint() -> str:
    """分区表缺失时给用户看的说明（不硬编码成唯一中文文件名）。"""
    return (
        "未找到中科院期刊分区表，CAS 分区加权已跳过。"
        f"请把分区表 xlsx 放到 {literature_reference_dir() / CAS_RANKING_FILENAMES[0]}，"
        "或设置环境变量 HARNESS_CAS_JOURNAL_RANKING=<xlsx 绝对路径>。"
    )


def user_root() -> Path:
    return home() / "user"


def identity_path() -> Path:
    return user_root() / "identity.json"


def ensure_dirs() -> None:
    """启动时调一次，确保 root 目录存在。"""
    home().mkdir(parents=True, exist_ok=True)
    org_root().mkdir(parents=True, exist_ok=True)
    runs_root().mkdir(parents=True, exist_ok=True)         # legacy, 留
    runs_anon_root().mkdir(parents=True, exist_ok=True)    # v0.8 新
    projects_root().mkdir(parents=True, exist_ok=True)
    user_root().mkdir(parents=True, exist_ok=True)


# ── Backward-compat：保留 <repo>/output/ 作 fallback fixture root ────────────
def legacy_output_root() -> Path | None:
    """旧 `<repo>/output/`。仅 migration 脚本用，正常路径走 runs_root。"""
    repo_root = Path(__file__).resolve().parent.parent
    legacy = repo_root / "output"
    return legacy if legacy.exists() else None

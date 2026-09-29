"""框架级危险命令拦截：run_bash / execute_python 通用。

设计：两个模式，二选一，全局切换：
  - **普通模式（默认）**：命中高危模式 → 走真实 HITL pause（复用
    request_human_input 同款 pause_event 契约），等人明确批准才放行；
    批准判定由 `highrisk_confirm_hook`（always-on loop hook）在下一轮 turn_start
    读取人的回答文本完成，写一次性确认标记，工具重试时消费掉。
  - **bypass 模式**：跳过交互确认并写审计 transcript，但绝不跳过强制 Docker
    沙盒、路径挂载或资源限制。

不改 nodes/*/tools/（各节点自制的安全层如 experiment 的 safe_bash.py 继续独立
存在、独立维护，不受这里影响）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Iterable

log = logging.getLogger("dangerous_commands")

# ─────────────────────────────────────────────────────────────────────────────
# 模式切换（全局，跟 core/pause_driver.py 的 AUTO_APPROVE_ENABLED 同款模式）
# ─────────────────────────────────────────────────────────────────────────────

BYPASS_ENABLED: bool = os.getenv("HARNESS_BYPASS_DANGEROUS_COMMANDS") == "1"


def set_bypass_mode(enabled: bool) -> None:
    """chat.py --bypass-permissions / run_node.py --bypass-permissions 启动时调；
    REPL 内 `/bypass on|off` 也调本函数。"""
    global BYPASS_ENABLED
    BYPASS_ENABLED = bool(enabled)


def bypass_enabled() -> bool:
    return BYPASS_ENABLED


#: 本次会话**预先授权**的高危类别（`match_high_risk` 返回的那些标签）。
#:
#: 为什么需要中间档 —— 2026-08-10 实测：一轮无人值守 E2E 停在
#: 「真实外部作业提交」的审批上，静默挂了两小时。当时只有两个极端可选：
#:
#:   bypass_dangerous=True    连 `dd of=/dev/` 一起放行 —— 扩大授权，不能顺手搭车
#:   bypass_dangerous=False   每个高危点都停 —— 而无人值守时没有人会来答
#:
#: 于是"无人值守"这个承诺，在最需要它的那个节点（experiment 要提交真实作业）
#: 上必然失效。缺的不是开关，是**粒度**：出发前说清楚这一趟授权什么。
#:
#: 空集合 = 保持原样（每个高危点都停）。这是默认值，且必须是默认值：
#: 授权范围只能由发起的人显式给出，不能由框架替他推断。
#:
#: `"*"` = 全部授权（UI 上的「连续」档）。为什么是通配符而不是"把已知类别
#: 列进去"：类别集合**不封闭** —— 节点可以定义自己的（experiment 的
#: 「真实外部作业提交」就不在本模块的表里）。写成名单，则任何人新加一个
#: 类别，所有声称"全部授权"的会话都会在那里停下问一个不在的人，而且不报错。
#: 这正是"护栏要扫盘不要写名单"。
PREAUTHORIZED_CATEGORIES: set[str] = set()

#: 通配符字面量。集中一处，免得前后端/运行时各写各的字符串。
ALL_CATEGORIES = "*"


def set_preauthorized_categories(categories: "Iterable[str] | None") -> None:
    """设定本次会话预授权的高危类别。传 None / 空 → 清空（恢复"每次都问"）。"""
    global PREAUTHORIZED_CATEGORIES
    PREAUTHORIZED_CATEGORIES = {str(c).strip() for c in (categories or ()) if str(c).strip()}


def preauthorized(category: str) -> bool:
    if not category:
        return False
    return ALL_CATEGORIES in PREAUTHORIZED_CATEGORIES or category in PREAUTHORIZED_CATEGORIES


def known_risk_categories() -> list[str]:
    """本模块**自己**认识的高危类别标签，供 UI 做选项、供调用方校验拼写。

    ⚠️ 这**不是**全集：节点可以有自己的类别（experiment 的
    `resource_manager` 就定义了「真实外部作业提交」），它们不在这份表里。
    所以这份清单只能用来**提示拼错**，不能用来当白名单硬拒 —— 硬拒就会把
    节点自定义的合法类别一并挡掉，正是"护栏写成名单，新东西默认漏过/误杀"
    的老毛病。
    """
    return sorted({label for _, label in (*_SHELL_PATTERNS, *_PYTHON_PATTERNS)})


def unrecognized_categories(categories: "Iterable[str]") -> list[str]:
    """挑出本模块不认识的标签。用来**吵**，不用来拒。

    为什么必须吵：预授权拼错一个字，症状是"我明明授权了，它还是停下来问人"
    —— 一个静默的无效声明。而无人值守正是没人看着的时候，静默失效等于
    2026-08-10 那次挂两小时原样复现。
    """
    known = set(known_risk_categories())
    return [c for c in categories if c and c != ALL_CATEGORIES and c not in known]


# ─────────────────────────────────────────────────────────────────────────────
# 危险模式检测（shell 命令 + Python 代码两种口径）
# ─────────────────────────────────────────────────────────────────────────────

_SHELL_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\brm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)\b"), "递归强制删除 (rm -rf)"),
    (re.compile(r"\brm\s+-\w*r\w*\s+/(?:\s|$)"), "删除根路径 (rm -r /)"),
    (re.compile(r"\bdd\s+[^\n]*\bof="), "块设备写入 (dd of=)"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "格式化文件系统 (mkfs)"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bsudo\b"), "提权 (sudo)"),
    (re.compile(r"\bchmod\s+-R\s+000\b|\bchmod\s+000\b"), "全权限清零 (chmod 000)"),
    (re.compile(r"\bgit\s+push\s+[^\n]*--force\b"), "强推覆盖远端历史 (git push --force)"),
    (re.compile(r">\s*/dev/sd[a-z]\b"), "直写块设备"),
    (re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b"), "关机/重启系统"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.IGNORECASE), "删数据库表/库 (DROP TABLE/DATABASE)"),
)

_PYTHON_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bshutil\.rmtree\s*\("), "递归删除 (shutil.rmtree)"),
    (re.compile(r"\bos\.removedirs?\s*\("), "递归删除 (os.remove/removedirs)"),
    (re.compile(r"\bos\.system\s*\(|\bsubprocess\.\w+\s*\("), "shell-out（可能间接跑高危命令）"),
    (re.compile(r"\bos\.popen\s*\("), "shell-out (os.popen)"),
)


def match_high_risk(text: str, *, mode: str = "shell") -> str | None:
    """返回命中的高危类别标签；未命中返 None。mode ∈ {'shell', 'python'}。"""
    if not text:
        return None
    patterns = _SHELL_PATTERNS if mode == "shell" else _PYTHON_PATTERNS
    for pat, label in patterns:
        if pat.search(text):
            return label
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 越界写入检测（v3.2 —— 与"高危"是两类不同性质的规则）
#
# 高危（上面）= 环境安全问题："危险但可能合理"（rm -rf 清缓存），所以走
#   ask-确认，且可被 /bypass 豁免（你信任环境时）。
# 越界（这里）= 契约完整性问题：shell/python 直写框架管理的状态（artifacts /
#   KB jsonl / memory / transcript）**永远是错的** —— 产 artifact 该走
#   save_artifact（有所有权/冻结/scope 检查），写 KB 该走 create_claim。
#   没有任何"人确认一下就合理"的场景，所以是**硬拒 + 指路**，不问人、
#   **不受 /bypass 影响**（Claude Code 同款分层：bypassPermissions 不越过 deny）。
#
# 背景（v9 dogfood 实测）：`echo '{...}' > artifacts/manuscript__x.json` 对
# OS 完全无害（不命中任何高危 pattern），却绕过了 save_artifact 的全部
# deliverable 所有权检查 —— list_artifacts 是纯文件扫描，写进去就算数。
#
# 已知局限（诚实声明）：字符串 pattern 挡不住动态拼路径（python 里
# `p = "arti" + "facts"` 之类）。兜底是账本：记录只经 save_artifact 进
# `.research/ledger/records.jsonl`，手写进目录的文件不在账本上，list_artifacts /
# 目录页 / 收尾清单都看不见它 —— 两层合起来对"LLM 抄近道"这个威胁模型足够；
# 对抗恶意攻击者不在设计目标内（单机自用环境）。
# ─────────────────────────────────────────────────────────────────────────────

# 框架管理状态的路径特征（出现在写入类命令里即越界）
_PROTECTED_SIG = (
    r"(?:artifacts/|deliverables/|kb_\w+\.jsonl|memory\.jsonl|memory/"
    r"|transcript\.jsonl|summary\.json|records\.jsonl|\.research/ledger/"
    # project.yaml 是平台管理的项目配置（PATCH /config 是唯一合法改法）——
    # 2026-08-31 给协调者放开无主之地写权时发现它既无属主又不在签名里，
    # 一并收进来（shell/python/file 三条到达路径同时生效）。
    r"|project\.yaml"
    r"|\.scope_map\.yaml|framework_exemptions\.yaml|PROFILE\.md|PROJECT\.md)"
)

# shell：写入类操作 × 保护路径。[^;|&\n]* 限制在同一条子命令内匹配。
_BOUNDARY_SHELL_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r">{1,2}\s*\S*" + _PROTECTED_SIG),
     "shell 重定向写入框架状态"),
    (re.compile(r"\btee\b[^;|&\n]*" + _PROTECTED_SIG),
     "tee 写入框架状态"),
    (re.compile(r"\b(cp|mv|rsync|install)\b[^;|&\n]*" + _PROTECTED_SIG),
     "复制/移动进框架状态目录"),
    (re.compile(r"\b(rm|unlink|truncate|shred)\b[^;|&\n]*" + _PROTECTED_SIG),
     "删除/截断框架状态文件"),
    (re.compile(r"\bsed\s+-i\b[^;|&\n]*" + _PROTECTED_SIG),
     "sed -i 原地改写框架状态"),
    (re.compile(r"\btouch\b[^;|&\n]*" + _PROTECTED_SIG),
     "touch 创建框架状态文件"),
)

# python：写模式 open / Path.write_* / shutil / os 删改 × 保护路径
_BOUNDARY_PYTHON_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"open\([^)]*" + _PROTECTED_SIG + r"[^)]*,\s*['\"][rb+]*[wax][rb+]*['\"]"),
     "python open(...,'w'/'a') 写框架状态"),
    (re.compile(_PROTECTED_SIG + r"[^\n)]*\)?[^\n]*\.write_(text|bytes)\("),
     "python Path.write_text/bytes 写框架状态"),
    (re.compile(r"\bshutil\.(copy\w*|move)\([^)]*" + _PROTECTED_SIG),
     "python shutil 复制/移动进框架状态"),
    (re.compile(r"\bos\.(remove|unlink|rename|replace)\([^)]*" + _PROTECTED_SIG),
     "python os 删除/改名框架状态文件"),
)


# ── Git 提交权限：专属平台，节点工具连做的能力都不给（2026-08-11）──────────
#
# v2.1 写着「Project = Git repo，提交权限专属平台，harness 只能*请求*
# checkpoint」。但在**行使权限的地方**一道门都没有：`git commit`、
# `reset --hard`、`rebase`、`checkout` 从 run_bash 一路畅通。唯一写着这条红线
# 的地方是 `nodes/experiment/harness.yaml` 的 prompt（"必须先
# request_human_input"）—— prompt 里的「必须」不是机制。
#
# 于是这条不变量只靠一个**事后取证**的守卫兜底：比对内存里的私有基线，
# 解释不了的提交就 reset。2026-08-11 它把一个完全合法的 checkpoint 判成越界，
# 157 个文件、6 份 LAMMPS 生产日志从磁盘消失。
#
# 名单的方向很重要：**白名单只读子命令，其余一律拒**。反过来（黑名单列出
# 改写操作）意味着 git 以后新增的任何子命令默认放行 —— 那种名单落地即过期。
#
# 这不是安全类确认，是契约完整性：人点头也不能让节点拿到提交权限（点了头就
# 会出现平台 DB 与磁盘分叉，下一次 checkpoint 直接 fail-closed）。所以归
# boundary 类硬拒，不 pause、不吃 bypass。
#: 纯读。节点查冻结、看 diff、读历史都是正当需求，不能连坐。
#: `config` **不在**这里 —— `git config core.hooksPath /tmp/x` 能让平台之后的
#: 提交去跑任意钩子，那不叫只读。
_GIT_READ_ONLY_SUBCOMMANDS = frozenset({
    "annotate", "blame", "bisect", "cat-file", "check-ignore",
    "count-objects", "describe", "diff", "diff-tree", "difftool", "grep",
    "help", "log", "ls-files", "ls-remote", "ls-tree", "merge-base",
    "name-rev", "rev-list", "rev-parse", "shortlog", "show", "show-ref",
    "status", "var", "verify-commit", "version", "whatchanged",
})

#: 不是只读，但碰不到 **Project 仓库的历史** —— 拒了就是误伤。
#:
#: 复刻别人的工作要 clone 外部代码，这是正当科研操作：`repro_snapshot.py`
#: 自己就有正则在检测 `git clone` / `git submodule`，说明节点预期会跑它们。
#:
#:   clone      建的是**另一个**仓库，动不了本仓的 HEAD/refs
#:   submodule  不移动 superproject 的 HEAD；写进工作区的文件由工作区守卫管
#:   apply      改工作区，不产生提交；同上
#:
#: 明确**不**放进来的两个看着无害的：
#:   fetch      `git fetch . HEAD:some-branch` 能直接改本地分支
#:   config     见上，能改写钩子路径
_GIT_OUTSIDE_PROJECT_HISTORY = frozenset({"clone", "submodule", "apply"})

#: `git` 与子命令之间的全局选项。带值的（-C/-c/--git-dir…）要连值一起跳过。
_GIT_GLOBAL_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree",
                                         "--namespace", "--exec-path"})

_GIT_INVOCATION = re.compile(r"(?:^|[;&|`]|\$\()\s*(?:\w+=\S*\s+)*git\b([^;&|`\n]*)")


def _git_subcommand(tail: str) -> str | None:
    """从 `git` 之后的部分取出子命令，跳过全局选项。取不到返回 None。"""
    tokens = tail.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            return token
        if token in _GIT_GLOBAL_OPTS_WITH_VALUE:
            index += 2
            continue
        if "=" in token:            # --git-dir=/x 形式，自带值
            index += 1
            continue
        index += 1
    return None


def match_git_authority_violation(text: str) -> str | None:
    """命中「节点想自己行使 Git 权限」→ 返回类别标签，否则 None。

    守的是**本仓的提交权限**，不是"凡 git 皆拦"。三类放行：

      · 纯读（log/diff/status/rev-parse…）—— 查冻结、看 diff 是正当需求
      · 碰不到本仓历史的（clone/submodule/apply）—— 复刻外部代码要用
      · 认不出子命令的（`git --version`、光一个 `git`）—— 做不了事
    """
    if not text or "git" not in text:
        return None
    for match in _GIT_INVOCATION.finditer(text):
        sub = _git_subcommand(match.group(1))
        if (sub is None
                or sub in _GIT_READ_ONLY_SUBCOMMANDS
                or sub in _GIT_OUTSIDE_PROJECT_HISTORY):
            continue
        return f"Git 权限越界（git {sub}）"
    return None


_PROTECTED_PATH_RE = re.compile(r"(?:^|/)" + _PROTECTED_SIG)

FILE_PATH_DENY_MESSAGE = (
    "⛔ 这条路径是框架管理的状态（{category}），文件工具不能直写。\n"
    "产 artifact → save_artifact（有所有权/冻结/scope 检查）；"
    "外部现成材料 → import_artifact；记忆 → memory_write。\n"
    "可以直写的：用户点名的交付文件与项目根的普通文件"
    "（如 project/LITERATURE_REVIEW.md）、自己作用域内的工作文件。"
)


def match_protected_file_path(rel_path: str) -> str | None:
    """文件工具版的越界判据：与 shell/python 共用同一份 _PROTECTED_SIG。

    write_file/edit_file 是同一条不变量（"框架管理的状态只由生命周期工具写"）
    的**第三条到达路径**——shell 与 python 两条早在 v3.2 就接了闸，这条是
    2026-08-31 给协调者放开交付写权时一并接上的。传相对 Project 根的 posix
    路径；命中返回类别文本，调用方硬拒（不受 /bypass 影响）。
    """
    if not rel_path:
        return None
    m = _PROTECTED_PATH_RE.search(rel_path)
    return m.group(0).lstrip("/") if m else None


def match_boundary_violation(text: str, *, mode: str = "shell") -> str | None:
    """返回命中的越界类别标签；未命中返 None。mode ∈ {'shell', 'python'}。

    命中 = 调用方应**硬拒**（status='error' + 指路正确工具），不 pause、
    不看 bypass_enabled()。
    """
    if not text:
        return None
    patterns = (_BOUNDARY_SHELL_PATTERNS if mode == "shell"
                else _BOUNDARY_PYTHON_PATTERNS)
    for pat, label in patterns:
        if pat.search(text):
            return label
    # Git 权限对两种模式是同一条规则：python 里 subprocess 起 git 与 shell
    # 直接跑 git 是同一件事，不给它留个后门。python 的参数列表
    # （`['git', 'commit', ...]`）先摊平成命令行形状再套同一条判据。
    return match_git_authority_violation(
        _flatten_python_argv(text) if mode == "python" else text
    )


def _flatten_python_argv(text: str) -> str:
    """把 `subprocess.run(['git', 'commit', '-m', 'x'])` 摊成命令行形状。

    引号和逗号变空格，`[`/`(` 变成子命令边界 —— 这样同一条 shell 判据能直接用，
    不用为 python 再写一份会各自演化的规则。
    """
    return text.translate(str.maketrans({"'": " ", '"': " ", ",": " ", "[": ";", "(": ";"}))


_ARTIFACT_DENY_BODY = (
    "不能用 shell/python 直写框架管理的状态"
    "（artifacts / deliverables / KB jsonl / memory / transcript）。\n"
    "正确姿势：产 artifact → save_artifact；写 KB → create_claim/create_concept；"
    "记笔记 → write_scratchpad / memory_note。\n"
    "只是要读/导出？用 read_artifact / search_kb，或把命令改成纯读"
    "（cat/grep/ls 不带写入重定向就不拦）。"
)

_GIT_DENY_BODY = (
    "Project 是一个 Git 仓库，**提交权限专属平台**；节点只能*请求* checkpoint，"
    "不能自己动 Git 历史/HEAD/索引。\n"
    "正确姿势：把产物写进**你自己节点的目录**就行 —— 节点跑完平台会自动 "
    "checkpoint，你不需要（也不允许）commit/add/reset/checkout。\n"
    "只是要读历史？`git log` / `show` / `diff` / `status` / `rev-parse` 这些纯读"
    "子命令不拦。"
)


def boundary_deny_message(category: str) -> str:
    """按类别给出**指向真实原因**的拒绝文案。

    2026-08-11：Git 权限刚接进 boundary 类时，套用的是 artifact 那套文案 ——
    模型被告知"请改用 save_artifact"，而它其实是跑了 `git commit`。
    报错指向假原因最贵：它让人（和模型）去修一个不存在的问题。
    """
    body = _GIT_DENY_BODY if "Git 权限越界" in category else _ARTIFACT_DENY_BODY
    return (
        f"⛔ 越界写入拦截（{category}）：{body}\n"
        "此拦截是契约完整性规则，**不受 /bypass 影响**"
        "（bypass 只豁免环境安全类确认）。"
    )


class _BoundaryDenyMessage(str):
    """`BOUNDARY_DENY_MESSAGE.format(category=…)` 的兼容外壳。

    三个调用点写的都是 `.format(category=boundary)`，其中一个在同事拥有的
    `nodes/experiment/tools/safe_bash.py` 里。让 `.format` 直接转发到
    `boundary_deny_message`，三处同时拿到对的文案，而不用去动别人的节点 ——
    也不会留下三份会各自演化的抄件。
    """

    def format(self, *args, **kwargs):        # noqa: A003 - 就是要盖掉 str.format
        category = kwargs.get("category") or (args[0] if args else "")
        return boundary_deny_message(str(category))


BOUNDARY_DENY_MESSAGE = _BoundaryDenyMessage(boundary_deny_message("{category}"))


# ─────────────────────────────────────────────────────────────────────────────
# shell 探查专用模式（v3.3，2026-07-09）—— harness.shell_probe_only 节点的
# run_bash 只许真·只读探查。角色边界机械化：实测 orchestrator（"协调者不做研究"）
# 被 write_file 白名单弹回后，直接绕道 run_bash heredoc 替 producing 节点写代码
# 跑实验 —— "run_bash 只用于只读探查"当时是纯 prompt 文字，零机械后果。
#
# 拦两类（各自独立成因）：
#   ① 文件系统写（任意路径，比 boundary 的"框架状态"更宽）：探查不需要写盘。
#      放行只读惯用法：`2>/dev/null`、`>/dev/null`、`2>&1`。
#   ② 内联解释器 / 环境变更：heredoc、python -c、bash -c、pip/conda install ——
#      "写个脚本跑实验"正是实测逃逸路径；合法探查（which/pip show/nproc/
#      nvidia-smi/git status/cat/grep）不需要这些。
# ─────────────────────────────────────────────────────────────────────────────

_PROBE_ONLY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # 包管理安装放最前：`pip install` 里的 "install" 会被下面文件写 pattern
    # 误标签（都拦，但报错要指对原因）
    (re.compile(r"\b(pip[0-9.]*|pip3)\s+install\b|\bconda\s+install\b"
                r"|\bapt(-get)?\s+install\b|\bbrew\s+install\b"),
     "安装软件（环境变更）"),
    # ① 文件系统写
    (re.compile(r"(?<!\d)>{1,2}(?!&)\s*(?!/dev/null\b)\S"),
     "重定向写文件"),
    (re.compile(r"\btee\b"), "tee 写文件"),
    (re.compile(r"\b(cp|mv|rsync|install|dd)\b"), "复制/移动/写盘"),
    (re.compile(r"\b(rm|unlink|truncate|shred)\b"), "删除/截断文件"),
    (re.compile(r"\b(touch|mkdir|ln)\b"), "创建文件/目录/链接"),
    (re.compile(r"\bsed\s+(-[a-zA-Z]+\s+)*-i\b"), "sed -i 原地改写"),
    (re.compile(r"\b(chmod|chown)\b"), "改权限"),
    # ② 内联解释器 / 环境变更
    (re.compile(r"<<-?\s*['\"]?\w"), "heredoc 内联脚本"),
    (re.compile(r"\bpython[0-9.]*\s+(-\w+\s+)*-c\b"), "python -c 内联执行"),
    (re.compile(r"\bpython[0-9.]*\s+-(\s|$)"), "python 读 stdin 执行"),
    (re.compile(r"\b(bash|sh|zsh)\s+-c\b"), "shell -c 内联执行"),
    (re.compile(r"\b(perl|node|ruby)\s+-e\b"), "解释器内联执行"),
    (re.compile(r"\bwget\b|\bcurl\b[^|;&\n]*(\s-[a-zA-Z]*[oO]\b|--output\b|--remote-name\b)"),
     "下载写盘"),
)


def match_probe_only_violation(text: str) -> str | None:
    """shell_probe_only 模式下检查命令。返回命中类别；纯探查返 None。"""
    if not text:
        return None
    for pat, label in _PROBE_ONLY_PATTERNS:
        if pat.search(text):
            return label
    return None


PROBE_ONLY_DENY_MESSAGE = (
    "⛔ 探查专用 shell（{category}）：本节点的 run_bash 只用于**只读探查**"
    "（ls/grep/which/nproc/nvidia-smi/git status/pip show…），不能写文件、"
    "跑内联脚本或装软件。\n"
    "你是协调者：真研究/计算/产出 → `run_node` 起对应节点（它们有完整的"
    "执行环境、质量检查和 review 把关）；替 producing 节点干活 = 绕过全套"
    "质量体系，产出的 provenance 也会在 review 被红线否决。\n"
    "此拦截**不受 /bypass 影响**（角色边界，不是环境安全确认）。"
)


# ─────────────────────────────────────────────────────────────────────────────
# 一次性确认标记（human 批准后，工具重试时消费）
# ─────────────────────────────────────────────────────────────────────────────

_STATE_KEY = "_highrisk_confirmed_cmds"
_PENDING_KEY = "_highrisk_pending_ask"


def _cmd_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


#: 通行证在盘上活多久。一次性凭证不该永生 —— 人批的是"现在这一次"。
PASS_TTL_SECONDS = 3600


def _pass_store_path(state):
    """通行证落盘处：**会话级**，不是 run 级。

    ## 为什么必须落盘（2026-08-31 实测的确定性死循环）

    批准原来只写 `state.hook_state[_STATE_KEY]` —— 那是**这一个 run 的内存**。
    而 `record_highrisk_answer` 只在 pause 还活着时被调；pause 一旦作废，
    人的答复就被当成**新的一轮**，节点用全新 State 重跑，通行证不存在，
    同一道**确定性**的闸再次触发 —— 回到原点。

    现场：连着三次「批准执行」换回三次一模一样的高危提问，
    `pause.abandoned` 每次都报，而 worker 从头到尾没死过。
    **这个部署上高危操作永远批不下来。**

    讽刺的是 approval_contract 自己写着「通行证绑定在你这次提交的**逐字相同的
    参数**上」—— 身份（工具 + 逐字文本的 sha256）本来就可持久化，
    却被存在了最活不过一轮的地方。

    落在 project_root（memory.jsonl 同级）并按 session 隔离：
    同一会话里换 run 仍然认，换会话不认。没有 project_root（教学版 run-local）
    时退回只用内存 —— 那种形态本来就没有跨 run 语义。
    """
    root = getattr(state, "project_root", None)
    return (root / "highrisk_passes.json") if root else None


def _pass_id(state, text: str) -> str:
    return f"{getattr(state, 'session_id', None) or '-'}:{_cmd_key(text)}"


def _load_passes(state) -> dict:
    path = _pass_store_path(state)
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:          # 坏文件不该把高危闸变成放行，也不该炸掉调用方
        return {}
    return data if isinstance(data, dict) else {}


def _save_passes(state, passes: dict) -> None:
    path = _pass_store_path(state)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(passes, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass                    # 落盘失败不能反过来把批准搞没（内存那份还在）


def _now() -> float:
    return time.time()


def is_confirmed(state, text: str) -> bool:
    confirmed = state.hook_state.get(_STATE_KEY) or {}
    if _cmd_key(text) in confirmed:
        return True
    # 内存里没有 → 可能是"人答复被当成新一轮、节点换了 State"那条路。
    entry = _load_passes(state).get(_pass_id(state, text))
    if not isinstance(entry, dict):
        return False
    expires = entry.get("expires_at")
    return isinstance(expires, (int, float)) and _now() < expires


def consume_confirmation(state, text: str) -> None:
    """一次性：这次放行后清掉标记，同一命令下次再触发高危模式要重新问。"""
    confirmed = state.hook_state.get(_STATE_KEY) or {}
    confirmed.pop(_cmd_key(text), None)
    state.hook_state[_STATE_KEY] = confirmed
    passes = _load_passes(state)
    if passes.pop(_pass_id(state, text), None) is not None:
        _save_passes(state, passes)


def mark_confirmed(state, text: str) -> None:
    confirmed = state.hook_state.setdefault(_STATE_KEY, {})
    confirmed[_cmd_key(text)] = True
    passes = _load_passes(state)
    # 顺手清掉过期的，别让这个文件无限长
    now = _now()
    passes = {
        k: v for k, v in passes.items()
        if isinstance(v, dict) and isinstance(v.get("expires_at"), (int, float))
        and v["expires_at"] > now
    }
    passes[_pass_id(state, text)] = {"granted_at": now, "expires_at": now + PASS_TTL_SECONDS}
    _save_passes(state, passes)


#: 审批面能看到多少被批的内容。
#:
#: 原来写死 300，且文案叫「**完整**内容（前 300 字符）」—— 自相矛盾，
#: 而且对一段 Python 根本不够：2026-08-31 实测，一次
#: `subprocess.run([sys.executable, os.path.join(base, 'code'` 恰好被切在
#: 决定性的那个 token 上，审批人**看不到它要跑哪个脚本**就得按批准。
#: 这套文件自己的原则是「审批一个看不见内容的高危操作等于没有审批」。
#: 放到 2000：足够装下真实的命令/片段，又不至于让面板失控。
APPROVAL_PREVIEW_CHARS = 2000


def _preview_block(preview: str) -> str:
    """把被批的内容摆给人看 —— 截断了就**说自己截了多少**，别叫「完整内容」。"""
    text = preview or ""
    if len(text) <= APPROVAL_PREVIEW_CHARS:
        return f"完整内容（{len(text)} 字符）：\n{text}"
    shown = text[:APPROVAL_PREVIEW_CHARS]
    return (
        f"内容前 {APPROVAL_PREVIEW_CHARS} 字符（共 {len(text)} 字符，"
        f"已截断 {len(text) - APPROVAL_PREVIEW_CHARS} 字符）：\n{shown}"
    )


def register_pending_ask(state, *, tool: str, text: str, category: str) -> None:
    """工具即将 pause 前登记，供 highrisk_confirm_hook 在下一轮读人回答时定位。"""
    state.hook_state[_PENDING_KEY] = {"tool": tool, "text": text, "category": category}


def pop_pending_ask(state) -> dict | None:
    return state.hook_state.pop(_PENDING_KEY, None)


# ── 选项即协议 ──────────────────────────────────────────────────────────────
#
# 界面上印的那两行选项和后端认的答复，必须是**同一个东西的两种渲染**。
# issue #422 实测：CLI 印 `[1] 批准执行 / [2] 拒绝`，人输入 `1`，后端只认
# "批准/同意/approve"，于是判成未批准 → 不发通行证 → 模型不再重调 submit_job。
# 界面广告的能力，后端得接得住。所以选项文案在这里定义一次，pause_event 用它
# 渲染，判定也用它反解。
APPROVE_OPTION = "批准执行"
DENY_OPTION = "拒绝"
HIGHRISK_OPTIONS = [APPROVE_OPTION, DENY_OPTION]

# 批准 / 拒绝关键词（跟 shared/tools/run_node.py 里 fake-orchestrator 的
# 关键词判定风格一致：宽松匹配批准词，默认不批准更安全）
_APPROVE_WORDS = re.compile(
    r"批准|同意|可以|确认执行|执行吧|去吧|approve|approved|^yes\b|^ok\b|^y\b",
    re.IGNORECASE,
)

#: 裸选项序号的各种写法：`1` / `1.` / `[1]` / `(1)` / `（1）` / `选 1`。
_BARE_INDEX = re.compile(r"^(?:选\s*)?[\[(（]?\s*(\d+)\s*[\])）]?\s*[.。、)]?$")


def response_text(answer: object) -> str:
    """从人工答复里取出**人真正说的那句话**。

    resume 回填给模型的是一个信封（`core/agent_loop.py::resume_loop`）：

        {"status": "success", "response": "1", "asked_by": "experiment"}

    而 hook 从 messages 里读到的就是这个信封的 JSON 字符串。此前判定直接拿
    整串去匹配 —— 于是 `^yes\b` / `^ok\b` / `^y\b` 这三条锚定规则在真实路径上
    **永远不可能成立**（串首是 `{`），`1` 更是无从谈起。判定必须先拆信封。
    """
    if isinstance(answer, dict):
        inner = answer.get("response")
        return "" if inner is None else str(inner).strip()
    text = ("" if answer is None else str(answer)).strip()
    if not (text.startswith("{") and text.endswith("}")):
        return text
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(payload, dict) and "response" in payload:
        inner = payload.get("response")
        return "" if inner is None else str(inner).strip()
    return text


def _option_choice(text: str, options: list[str] | None = None) -> str | None:
    """把答复解析成它选中的那个选项文案；不是选项就返回 None。

    认三种写法：裸序号（界面印的 `[1]`）、选项原文、选项原文的前缀词。
    """
    opts = options or HIGHRISK_OPTIONS
    m = _BARE_INDEX.match(text)
    if m:
        idx = int(m.group(1))
        return opts[idx - 1] if 1 <= idx <= len(opts) else None
    stripped = text.strip().strip("[]()（）").strip()
    for opt in opts:
        if stripped == opt:
            return opt
    return None


def looks_like_approval(answer: object, options: list[str] | None = None) -> bool:
    """人这次到底批没批。

    顺序是**先选项、后关键词**：选项是界面给出的确定协议，关键词只是兜底的
    自由文本理解。选中"拒绝"必须当场判否 —— 否则 `拒绝` 里那个"绝"字将来
    多一条宽松词就可能被误吞。
    """
    text = response_text(answer)
    if not text:
        return False
    choice = _option_choice(text, options)
    if choice is not None:
        return choice == APPROVE_OPTION
    return bool(_APPROVE_WORDS.search(text))


def record_highrisk_answer(state, answer: object,
                           options: list[str] | None = None) -> bool:
    """在框架**确知人选了什么**的那一刻，把批准与否机械记账。

    与 decision package 的 `record_decision_answer`（#155/#183）同形，且原因
    完全一样：pause_driver 手里同时有原始答复和那份选项表，hook 那边只剩一条
    JSON 信封。判定放在信息最全的地方做一次，写成事实；hook 读事实，不再猜。

    记账失败绝不能打断 resume —— 这里只写 hook_state，没有可失败的 IO。
    """
    approved = looks_like_approval(answer, options)
    pending = state.hook_state.get(_PENDING_KEY)
    if isinstance(pending, dict):
        pending["approved"] = approved
        pending["answer_text"] = response_text(answer)
    return approved


def build_pause_payload(state, *, tool: str, text: str, category: str,
                         preview: str) -> dict:
    """统一构造高危命令确认的 pause_event（跟 request_human_input 同款契约）。

    `pause_event` 是给**人**看的；`approval_contract` 是给**模型**看的。两者都
    必须有：pause_event 里那句"批准请回复批准"只回答了人该做什么，模型这边
    完全没有交代，它会默认"人批准了 = 框架替我执行了"，然后去查一个根本不存在
    的结果。见 core/loop_hooks_builtin.py::_highrisk_confirm_on_turn_start 的
    实测记录。
    """
    # 预授权：出发前就说清楚这一趟批准哪些类别，到点不再问人。
    #
    # 检查放在**这个漏斗里**，不放在各调用方 —— 调用方有三处、分属两个 owner
    # 的节点目录，写成名单式检查就是"新增一个高危工具默认漏过预授权"，而且
    # 越界改别人的节点。这里是每一次高危确认的必经之路，且已经拿到 category。
    #
    # 走的是**和人批准完全同一条路**：发一张绑定逐字命令的一次性通行证，然后
    # 让模型用相同参数重调一次。不另开一条"框架替你执行"的路径 —— 那正是
    # E2E v19 里模型误以为"批准 = 已执行"的那个坑，多一条路就多一种误解。
    if preauthorized(category):
        mark_confirmed(state, text)
        try:
            state.append_transcript(
                "highrisk_preauthorized", tool=tool, category=category,
                preview=preview[:200],
            )
        except Exception:
            pass
        return {
            "status": "preauthorized",
            "approval_contract": (
                f"这次 {category}（工具：{tool}）已按**本次会话出发前声明的授权范围**"
                f"自动批准，没有惊动任何人。\n"
                f"批准同样**不会**替你执行 —— 它只发了一张绑定逐字参数的一次性"
                f"通行证。请立刻用与刚才完全相同的参数重新调用一次 {tool}。\n"
                f"改任何一个参数都会作废通行证并重新触发审批。"
            ),
            "category": category,
        }

    register_pending_ask(state, tool=tool, text=text, category=category)
    return {
        "status": "pause",
        "approval_contract": (
            f"这次调用**没有执行**，它在等人批准。批准本身也不会执行它 —— "
            f"批准只会发一张一次性通行证，绑定在你这次提交的逐字相同的参数上。"
            f"拿到批准后，你必须用**完全相同的参数**重新调用一次 {tool}，"
            f"那一次才会真正执行。改任何一个参数都会作废通行证并重新触发审批。"
        ),
        "pause_event": {
            "question": f"⚠️ 检测到高危操作（{category}），是否批准执行？",
            "context": (
                f"工具：{tool}\n命中类别：{category}\n"
                f"{_preview_block(preview)}\n\n"
                f"批准请回复 1（或 \"{APPROVE_OPTION}\"/\"批准\"/approve）；"
                f"拒绝回复 2 或任意其它文本。"
            ),
            "options": list(HIGHRISK_OPTIONS),
            "asking_node_type": state.node_type,
            "asking_run_id": state.run_id,
            "metadata": {"type": "highrisk_confirm", "tool": tool, "category": category},
        },
    }

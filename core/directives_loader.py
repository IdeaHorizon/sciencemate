"""指令层加载器：**文件是权威，读的是当下那一份。**

## 从「冻结的抄件」回到「文件本身」（RFC X3）

这里曾经有第二条路：平台把三层指令抄成一份 JSON 快照冻在会话行上，随请求
下发，`platform_runtime` 校验后落成 `<state_dir>/.platform-instructions/` 三个
只读文件 + 一个 manifest，再由本文件读回来校验哈希。同一个问题两套实现，而
只有一套被维护 —— 实测后果：

1. 平台的「项目指令」编辑器写 `<data_root>/projects/<id>/PROJECT.md`，
   而本文件读的是 **worktree 里的 `PROJECT.md`**（Project v2 的权威）。
   用户在界面上改完，agent 一个字都读不到，两边都不报错。
2. 组织层永远是空的：三处路径、一个校验器、一段注入，**零写入口**。
3. 那一列可以是 NULL（041 建列时没有 backfill），而消费方是硬 raise ——
   041 之前建的会话从此一轮都跑不了，用户看到的只有一句「这一轮没能完成」。

现在只有一条路，就是 CLI 一直走的那条：

  <harness home>/user/PROFILE.md             ← 个人层（平台上 home 已按用户分）
  <harness home>/user/RESEARCH_SETTINGS.md   ← 个人层里机器维护的那半（平台写）
  <project worktree>/PROJECT.md              ← 项目层（git 就是它的版本与冻结）

**冻结不是把文本抄进数据库一列，是 git。**会话有自己的 worktree 分支，它看到
的 `PROJECT.md` 就是那条分支上的那一份；别人改主干不会从它脚下抽走。个人层
没有这种需要 —— 用户改了偏好就该下一轮生效，冻上三个星期才是 bug。

**「当时读到的是哪一份」是证据，不是判决**：每层的 sha256 随这一轮上报
（`load_directives_for_node` 的 `digests`），进事件流。要审计就查那一轮的
记录，而不是查一个三周前冻下来、与本轮无关的列。

PROJECT.md 支持 "## 节点级指令" 段，下面以 `### <node_type>` 子段区分每节点
的额外指令；加载时按 node_type 自动提取相关段。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

log = logging.getLogger("directives_loader")

#: 个人层里**机器维护**的那半：平台把结构化研究设置（语言、引用风格……）
#: 渲染成文本写在这里。与 PROFILE.md 分开，是因为它们的写者不同 ——
#: 合在一个文件里，平台每次重写都会把用户自己写的话冲掉。
RESEARCH_SETTINGS_FILENAME = "RESEARCH_SETTINGS.md"
PROFILE_FILENAME = "PROFILE.md"
PROJECT_FILENAME = "PROJECT.md"


def _user_dir() -> Path:
    from core.paths import home as _root  # 「根在哪」一处回答（含 Windows 分支）

    user_dir = _root() / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def _profile_path() -> Path:
    return _user_dir() / PROFILE_FILENAME


def _research_settings_path() -> Path:
    return _user_dir() / RESEARCH_SETTINGS_FILENAME


def _project_md_path(project_root: Path | None) -> Path | None:
    if not project_root:
        return None
    return Path(project_root) / PROJECT_FILENAME


def _read_text(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("读 %s 失败：%s", path, exc)
        return None


def _digest(content: str | None) -> str | None:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compose(*parts: str | None) -> str | None:
    kept = [part.strip() for part in parts if part and part.strip()]
    return "\n\n".join(kept) if kept else None


def read_profile_md() -> str | None:
    """读个人层：用户自己写的 PROFILE.md + 平台维护的研究设置。不存在返回 None。"""
    return _compose(_read_text(_profile_path()), _read_text(_research_settings_path()))


def read_project_md(project_root: Path | None) -> str | None:
    """读项目 PROJECT.md。不存在返回 None。"""
    return _read_text(_project_md_path(project_root))

# ── per-node 段提取 ─────────────────────────────────────────────────────────

_MANAGED_NODE_START = "<!-- platform:node-instructions:v1 -->"
_MANAGED_NODE_END = "<!-- /platform:node-instructions:v1 -->"
_MANAGED_NODE_BLOCK_RE = re.compile(
    rf"{re.escape(_MANAGED_NODE_START)}(?P<body>.*?){re.escape(_MANAGED_NODE_END)}",
    re.DOTALL,
)
_MANAGED_NODE_SECTION_RE = re.compile(
    r"^### Node: ([a-z][a-z0-9_-]{0,63})[ \t]*\n(.*?)(?=^### Node: |\Z)",
    re.MULTILINE | re.DOTALL,
)
_PLATFORM_NODE_SCOPE = {
    "literature": "literature",
    "hypothesis": "experiments",
    "experiment": "experiments",
    "observation": "observations",
    "derivation": "derivations",
    "data": "analysis",
    "postprocess": "analysis",
    "writing": "writing",
    "_reviewer": "review",
}

# 匹配 "## 节点级指令" 段后面的内容（直到下一个 ## 或文件结尾）
_NODE_SECTION_HEADER_RE = re.compile(
    r"^##\s*(节点级指令|Per-Node\s+Directives|Per-Node|节点指令)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_managed_project_directives(project_md: str, node_type: str) -> str | None:
    starts = project_md.count(_MANAGED_NODE_START)
    ends = project_md.count(_MANAGED_NODE_END)
    if starts == ends == 0:
        return None
    if starts != 1 or ends != 1:
        raise RuntimeError("PROJECT.md must contain one complete managed node section")
    match = _MANAGED_NODE_BLOCK_RE.search(project_md)
    if not match:
        raise RuntimeError("PROJECT.md managed node section is malformed")
    body = match.group("body")
    header = re.match(r"\A\s*## Node-specific instructions[ \t]*\n", body)
    if not header:
        raise RuntimeError("PROJECT.md managed node heading is malformed")
    node_body = body[header.end() :]
    selected: str | None = None
    requested_scope = _PLATFORM_NODE_SCOPE.get(node_type, node_type)
    seen: set[str] = set()
    cursor = 0
    for section in _MANAGED_NODE_SECTION_RE.finditer(node_body):
        if node_body[cursor : section.start()].strip():
            raise RuntimeError("PROJECT.md managed node section contains unmanaged text")
        section_type, section_content = section.groups()
        if section_type in seen:
            raise RuntimeError(f"PROJECT.md contains duplicate node type: {section_type}")
        seen.add(section_type)
        if section_type == requested_scope:
            selected = f"### {section_type}\n{section_content.rstrip()}"
        cursor = section.end()
    if node_body[cursor:].strip():
        raise RuntimeError("PROJECT.md managed node section contains unmanaged text")
    general = f"{project_md[: match.start()]}{project_md[match.end() :]}".rstrip()
    parts = [general] if general else []
    if selected:
        parts.append(f"## 节点级指令（{requested_scope}）\n\n{selected}")
    return "\n\n".join(parts) if parts else None


def _split_node_section(project_md: str) -> tuple[str, str | None]:
    """把 PROJECT.md 切成 (general 部分, 节点级指令 part)。

    "节点级指令" 部分如果不存在返回 (whole_text, None)。
    """
    m = _NODE_SECTION_HEADER_RE.search(project_md)
    if not m:
        return project_md, None
    general = project_md[: m.start()].rstrip()
    node_part = project_md[m.end() :]
    return general, node_part


def _extract_node_subsection(node_part: str, node_type: str) -> str | None:
    """从"节点级指令"part 提取 `### <node_type>` 子段。

    匹配规则：
      ### literature          → 命中
      ### Literature          → 命中（大小写不敏感）
      ### literature 节点      → 命中（前缀匹配）
    """
    if not node_part:
        return None
    # 找所有 ### 开头的子段
    pattern = re.compile(r"^###\s+(\S+)([^\n]*)\n(.*?)(?=^###\s+|\Z)", re.MULTILINE | re.DOTALL)
    for m in pattern.finditer(node_part):
        section_name = m.group(1).strip().lower()
        if section_name == node_type.lower():
            return m.group(0).rstrip()
    return None


def extract_relevant_project_directives(project_md: str | None, node_type: str) -> str | None:
    """从 PROJECT.md 提取跟该 node_type 相关的内容。

    - 通用项目段（"节点级指令"之前的内容）→ 全部保留
    - 节点级指令段 → 只保留 `### <node_type>` 子段
    """
    if not project_md:
        return None
    managed = _extract_managed_project_directives(project_md, node_type)
    if _MANAGED_NODE_START in project_md or _MANAGED_NODE_END in project_md:
        return managed
    general, node_part = _split_node_section(project_md)
    relevant_sub = _extract_node_subsection(node_part, node_type) if node_part else None

    out_parts: list[str] = []
    if general:
        out_parts.append(general)
    if relevant_sub:
        out_parts.append(f"## 节点级指令（{node_type}）\n\n{relevant_sub}")
    return "\n\n".join(out_parts) if out_parts else None


# ── 入口：给 context_engine 调 ──────────────────────────────────────────────


def load_directives_for_node(state, node_type: str) -> dict:
    """读当下的 PROFILE.md（+ 研究设置）与 PROJECT.md，按 node_type 切。

    项目层的权威是**这个会话的 worktree 里那份 `PROJECT.md`**：git 分支就是
    它的冻结，别人改主干不会从本会话脚下抽走。CLI 没有 worktree，落回
    `state.project_root`。

    返回：
      {
        "profile": str | None,   # 个人层（用户的 PROFILE.md + 平台维护的研究设置）
        "project": str | None,   # 提取后跟该 node 相关的项目部分
        "digests": {"personal": sha256|None, "project": sha256|None},
      }

    `digests` 是**证据**：这一轮实际读到的那几段文本的指纹，随本轮上报。
    审计问的是「这一轮用的是哪一份」，答案只能由这一轮自己给出。
    """
    personal = read_profile_md()
    project_worktree = getattr(state, "project_worktree", None)
    project_anchor = project_worktree or getattr(state, "project_root", None)
    project_md_full = read_project_md(Path(project_anchor)) if project_anchor else None
    project_relevant = extract_relevant_project_directives(project_md_full, node_type)

    return {
        "profile": personal,
        "project": project_relevant,
        "digests": {
            "personal": _digest(personal),
            # 指纹挂在**整份** PROJECT.md 上，不是按 node 切完那一段 ——
            # 审计要回答的是「这个会话读的是哪一版项目指令」，切片是本轮的
            # 投影，换个 node 就变，拿它当身份会得到几十个互不相等的答案。
            "project": _digest(project_md_full),
        },
    }


# ── 写入辅助（工具用）─────────────────────────────────────────────────────
#
# 这里不再有「快照是只读的」那道闸。平台上这两个文件都在**沙箱写边界之外**
# （写边界 = 会话 worktree 里节点自己的目录 + 本次 run 的 state 目录，见
# `core/sandbox.py::write_roots_for`），模型调工具写它们会被进程沙箱按系统
# 错误拒掉 —— 墙在 spawn 那一层，不靠这里再写一句话。


def write_profile_md(content: str) -> Path:
    """覆盖写 PROFILE.md。用 user-confirm 路径调（工具）。"""
    p = _profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def write_project_md(project_root: Path, content: str) -> Path:
    """覆盖写 PROJECT.md。"""
    p = Path(project_root) / PROJECT_FILENAME
    Path(project_root).mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def append_to_section(md_text: str, section_header: str, new_line: str) -> str:
    """在 markdown 指定 `## ` 段末尾追加一行（找不到 section 则新建）。"""
    pattern = re.compile(rf"^({re.escape(section_header)})\s*$", re.MULTILINE | re.IGNORECASE)
    m = pattern.search(md_text)
    if not m:
        # 新建段：附加在末尾
        return md_text.rstrip() + f"\n\n{section_header}\n\n{new_line}\n"
    # 找到段，找下一个 ## 段头或文件尾
    after = md_text[m.end() :]
    next_pattern = re.compile(r"^##\s+", re.MULTILINE)
    nm = next_pattern.search(after)
    if nm:
        insert_at = m.end() + nm.start()
        return md_text[:insert_at].rstrip() + f"\n{new_line}\n\n" + md_text[insert_at:]
    return md_text.rstrip() + f"\n{new_line}\n"

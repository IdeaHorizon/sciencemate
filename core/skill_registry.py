"""Skill 注册表 v2 —— folder/SKILL.md 模型。

设计原则：
  1. **Skill 是独立系统，不是 KB entity**。markdown 文件是真相源。
  2. 每个 skill = 一个 folder，必含 SKILL.md（frontmatter + body）。
  3. **两级加载（v3.9 起的默认）**：system_prompt 里只注入 L1 索引
     （name + description + applies_when），正文由模型按需 `load_skill` 取；
     `always_load: true` 的流程主干 skill 例外，仍全文常驻。
     `HARNESS_SKILLS_RENDER=full` 一键退回全文常驻旧行为（见 render_mode）。
  4. status=deprecated 自动跳过渲染。
  5. assets（examples/ references/ validation/）路径列在 prompt 里，LLM 按需
     走 `load_skill(name, asset=...)` 取 —— skill 目录在项目读边界外，
     read_file 够不到。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _flatten_applies_when(items: Any) -> list[str]:
    """`applies_when` 在真实 SKILL.md 里既有纯字符串也有 dict（如
    `{when: ..., then: ...}`）。索引条目要的是一行人话，所以统一压平成字符串，
    dict 取其 values 拼接 —— 解析不了也不能把这条路由信号整个丢掉。
    """
    out: list[str] = []
    for it in items or []:
        if isinstance(it, str):
            s = it.strip()
        elif isinstance(it, dict):
            s = " → ".join(str(v).strip() for v in it.values() if v)
        else:
            s = str(it).strip()
        if s:
            out.append(s)
    return out


@dataclass
class Skill:
    """单个 skill 的运行时表示。

    canonical 字段来自 SKILL.md frontmatter；body 是 frontmatter 后的 markdown。
    runtime 字段（origin / source_dir / assets）由 loader 填。

    **可见性由 `origin` 决定（folder 位置 = scope，不另设字段）**：
      - origin="framework"   → shared/skills/，所有节点可见
      - origin="imported"    → org/skills/，所有节点可见
      - origin="node:<X>"    → nodes/X/skills/，只有节点 X 可见
      - origin="runtime"     → 还没落 SKILL.md（在 proposals 里），不渲染

    想"升级"一个 node-local skill 让所有节点共享 —— **直接 mv 文件夹**到
    shared/skills/ 或 org/skills/。这是有意的 friction：移动是有意识的动作，
    标记了"我承认这是通用 skill"。
    """
    name: str
    description: str
    body_markdown: str                                # frontmatter 后的全部内容
    applies_when: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    relevant_concepts: list[str] = field(default_factory=list)
    status: str = "validated"                          # proposed | validated | deprecated
    always_load: bool = False                          # True = 正文常驻（流程主干 skill）
    # runtime 字段（不在 frontmatter 也不写回文件）
    origin: str = "framework"                          # framework | imported | node:<name> | runtime
    source_dir: str | None = None                      # 文件夹绝对路径（None = runtime proposal）
    assets: list[str] = field(default_factory=list)    # examples/* + references/* + validation/* 相对路径

    def skill_md_path(self) -> str | None:
        """SKILL.md 的绝对路径（诊断 / 落盘用）。

        ⚠️ **不要把它当成给模型的取正文指令**。skill 装在框架安装目录里，而
        绑了 Project 的 run 只能读 run root / project root 之内的路径
        （core/project_workspace.resolve_tool_path）—— 这个路径对平台上的每一个
        run 都在边界外。取正文走 `load_skill` 工具（正文本来就在注册表内存里）。
        """
        if not self.source_dir:
            return None
        return str(Path(self.source_dir) / "SKILL.md")

    def render_index(self) -> str:
        """L1 索引条目：只有路由信号 + 取正文的方式。

        **描述是路由信号，不是说明文字** —— 模型靠 description/applies_when 判断
        "这一轮要不要读它"，判断错了整个 skill 就等于不存在。所以这两项必须留，
        正文（可能几十 KB）不留。

        取正文用 `load_skill(name)`，**不是** read_file 绝对路径：后者在平台上
        100% 被项目读边界拒（2026-08-17 实测），而 CLI/fixture run 不绑 project、
        读侧不设边界，所以测试全绿、只有真跑才暴露。
        """
        lines: list[str] = [f"### {self.name}"]
        if self.description:
            lines.append(f"_{self.description}_")
        applies = _flatten_applies_when(self.applies_when)
        if applies:
            lines.append(f"**适用：** {'；'.join(applies)}")
        lines.append(f"**正文：** `load_skill('{self.name}')`")
        return "\n".join(lines)

    def render_prompt(self) -> str:
        """渲染进 system_prompt 的内容。

        策略：直接用 body_markdown（人写的 prose 就是给 LLM 看的），加一个轻量
        metadata header。Assets 路径列出来让 LLM 知道按需 read_file。
        """
        lines: list[str] = []
        lines.append(f"### Skill: {self.name}")
        if self.description:
            lines.append(f"_{self.description}_")
        if self.applies_when:
            lines.append("")
            lines.append("**适用场景：**")
            for c in self.applies_when:
                lines.append(f"- {c}")
        if self.tools_used:
            lines.append("")
            lines.append(f"**涉及工具：** {', '.join(self.tools_used)}")
        if self.expected_outcome:
            lines.append("")
            lines.append(f"**预期结果：** {self.expected_outcome}")
        lines.append("")
        lines.append(self.body_markdown.strip())
        if self.assets:
            lines.append("")
            # 同样不能给 read_file 路径：assets 和 SKILL.md 住在一起，一样在
            # 项目读边界之外。走 load_skill 的 asset 参数。
            lines.append(
                f"**Skill assets**（需要时用 "
                f"`load_skill(name='{self.name}', asset='<下面的相对路径>')` 取）："
            )
            for a in self.assets:
                lines.append(f"- {a}")
        return "\n".join(lines)


# ── 注册表（in-memory） ─────────────────────────────────────────────────────

_SKILLS: dict[str, Skill] = {}


def register_skill(skill: Skill) -> None:
    """注册一个 skill（loader 用；也保留给手动 code-time 注册）。

    同名再注册会覆盖（loader 重跑时刷新）。
    """
    _SKILLS[skill.name] = skill


def get_skill(name: str) -> Skill | None:
    return _SKILLS.get(name)


def all_skill_names() -> list[str]:
    return sorted(_SKILLS.keys())


def all_skills() -> list[Skill]:
    return [_SKILLS[n] for n in sorted(_SKILLS.keys())]


def clear_registry() -> None:
    """清空注册表（测试用）。"""
    _SKILLS.clear()


def _is_visible_to(skill: Skill, node_type: str | None) -> bool:
    """skill 对某个节点是否可见 —— **核心规则就这一条**：
       node-local skill（origin='node:X'）只对节点 X 可见。
       framework / imported / runtime origin 对所有节点可见。
    """
    if not skill.origin.startswith("node:"):
        return True
    if node_type is None:
        return True   # 调试模式或 caller 不在节点上下文：不过滤
    owner = skill.origin.split(":", 1)[1]
    return owner == node_type


def render_mode() -> str:
    """`index`（默认，两级加载）| `full`（全文常驻，旧行为）。

    kill switch：`HARNESS_SKILLS_RENDER=full` 一键退回旧行为。改动波及每个节点的
    每一轮请求，必须留一条不改代码就能撤退的路。
    """
    import os

    mode = (os.environ.get("HARNESS_SKILLS_RENDER") or "index").strip().lower()
    return mode if mode in ("index", "full") else "index"


def render_skills(names: list[str], *,
                   node_type: str | None = None,
                   include_deprecated: bool = False,
                   mode: str | None = None) -> str:
    """把指定的若干 skill 渲染为单段 markdown，用于 system_prompt。

    两级加载（v3.9）：默认只渲染 L1 索引（名字 + description + applies_when +
    正文路径），正文由模型按需 `read_file` 取。`always_load: true` 的 skill 仍
    渲染全文 —— 流程主干型 skill 一旦漏读就整条链走歪，不值得省那点 token。

    改造前：23 个 skill 每轮常驻 99.7 KB（writing 单节点 33.6 KB），
    与"这一轮在做什么"完全无关。

    过滤规则不变：
      - status=deprecated 默认跳过
      - origin='node:Y' 且当前 node_type ≠ Y 跳过（防节点间 skill 干扰）
      - 缺失的 skill 名静默跳过（warn 在 loader 那一层）
    """
    effective = mode or render_mode()
    blocks: list[str] = []
    for name in names:
        s = _SKILLS.get(name)
        if s is None:
            continue
        if s.status == "deprecated" and not include_deprecated:
            continue
        if not _is_visible_to(s, node_type):
            continue
        if effective == "full" or s.always_load:
            blocks.append(s.render_prompt())
        else:
            blocks.append(s.render_index())
    return "\n\n".join(blocks)


def visible_skills_for(node_type: str | None, *,
                        include_deprecated: bool = False) -> list[Skill]:
    """给某个 node_type 列出它**能看到**的 skill。用于 list_skills 工具。"""
    out = []
    for s in all_skills():
        if s.status == "deprecated" and not include_deprecated:
            continue
        if not _is_visible_to(s, node_type):
            continue
        out.append(s)
    return out

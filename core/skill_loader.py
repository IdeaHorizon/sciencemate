"""SKILL.md folder 加载器。

Folder 形态：
  <skill_dir>/
    SKILL.md            必须，frontmatter + body
    examples/           可选，asset
    references/         可选，asset
    validation/         可选，asset
    README.md           可选（给人看的入门，loader 不读）

SKILL.md 形态：
  ---
  name: <skill_name>
  description: <一行>
  applies_when: [<list>]
  tools_used: [<list>]
  expected_outcome: <一行>
  relevant_concepts: [<concept_id>...]
  status: validated | proposed | deprecated   # 默认 validated
  always_load: true | false                   # 默认 false；true = 正文常驻不走两级加载
  ---

  ## 工作流
  ... markdown body ...

加载入口（bootstrap 调）：
  - load_all_skills(framework_root): 自动扫 shared/skills + nodes/*/skills + org/skills
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from core.skill_registry import Skill, register_skill

log = logging.getLogger("skill_loader")

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<fm>.*?)\n---\s*\n(?P<body>.*)$",
    re.DOTALL,
)

_ASSET_SUBDIRS = ("examples", "references", "validation")


def _as_list(v: Any) -> list:
    """frontmatter 的列表字段允许写成单个字符串。

    `list("every writing run")` 会把字符串拆成单字符列表 —— applies_when 于是在
    prompt 里渲染成一个字符一行。这条 bug 一直在放大 prompt，两级加载把它显形了。
    标量一律包成单元素列表。
    """
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def parse_skill_md(text: str) -> tuple[dict, str]:
    """解析 SKILL.md → (frontmatter_dict, body_str)。

    缺 frontmatter（极少）→ 返回 ({}, text)。
    """
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return {}, text
    fm_text = m.group("fm")
    body = m.group("body")
    try:
        fm = yaml.safe_load(fm_text) or {}
        if not isinstance(fm, dict):
            log.warning("SKILL.md frontmatter 不是 dict，忽略：%r", fm)
            fm = {}
    except yaml.YAMLError as e:
        log.warning("SKILL.md frontmatter YAML 解析失败：%s", e)
        fm = {}
    return fm, body


def load_skill_from_folder(skill_dir: Path, *, origin: str = "framework") -> Skill | None:
    """从一个 skill folder 读 SKILL.md，构造 Skill 对象。

    可见性完全由 origin 决定（看 Skill.origin docstring）—— **不需要额外字段**。
    找不到 SKILL.md 或 frontmatter 缺 name → 返回 None + log warning。
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        log.warning("跳过 %s：缺 SKILL.md", skill_dir)
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("读 %s 失败：%s", skill_md, e)
        return None

    fm, body = parse_skill_md(text)
    name = fm.get("name") or skill_dir.name   # fallback：用文件夹名
    if not name:
        log.warning("跳过 %s：frontmatter 缺 name 且文件夹名空", skill_dir)
        return None

    description = (fm.get("description") or "").strip()
    if not description:
        log.warning("Skill %r 缺 description（推荐补一行）", name)

    # 扫 assets
    assets: list[str] = []
    for sub in _ASSET_SUBDIRS:
        sub_dir = skill_dir / sub
        if sub_dir.is_dir():
            for f in sorted(sub_dir.rglob("*")):
                if f.is_file() and not f.name.startswith("."):
                    assets.append(str(f.relative_to(skill_dir)))

    return Skill(
        name=str(name),
        description=description,
        body_markdown=body.strip(),
        applies_when=_as_list(fm.get("applies_when")),
        tools_used=_as_list(fm.get("tools_used")),
        expected_outcome=str(fm.get("expected_outcome") or "").strip(),
        relevant_concepts=_as_list(fm.get("relevant_concepts")),
        status=str(fm.get("status") or "validated"),
        always_load=bool(fm.get("always_load") or False),
        origin=origin,
        source_dir=str(skill_dir),
        assets=assets,
    )


def discover_skill_folders(parent_dir: Path) -> list[Path]:
    """parent_dir 下所有含 SKILL.md 的直接子目录（不递归二层）。

    跳过 . / __pycache__ / 等。
    """
    if not parent_dir.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(parent_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("__"):
            continue
        if (child / "SKILL.md").exists():
            out.append(child)
    return out


def load_skills_in_dir(parent_dir: Path, *, origin: str) -> int:
    """加载 parent_dir 下所有 skill folder。返回加载数。"""
    count = 0
    for folder in discover_skill_folders(parent_dir):
        skill = load_skill_from_folder(folder, origin=origin)
        if skill is not None:
            register_skill(skill)
            count += 1
    return count


# ── Bootstrap 入口 ──────────────────────────────────────────────────────────

def load_all_skills(framework_root: Path | None = None) -> dict[str, int]:
    """扫所有 skill 来源并加载进 registry。

    顺序（按 register 覆盖优先级倒序写：后注册的覆盖前面的同名）：
      1. shared/skills/*/SKILL.md          → origin=framework
      2. nodes/*/skills/*/SKILL.md         → origin=node:<name>
      3. $HARNESS_FRAMEWORK_ORG_HOME 或 $HARNESS_FRAMEWORK_HOME/org/skills/*/SKILL.md
                                            → origin=imported

    返回 {source: count} 统计。
    """
    if framework_root is None:
        framework_root = Path(__file__).resolve().parent.parent
    counts: dict[str, int] = {}

    # 1. shared/skills/
    shared_skills = framework_root / "shared" / "skills"
    counts["framework"] = load_skills_in_dir(shared_skills, origin="framework")

    # 2. nodes/*/skills/
    nodes_dir = framework_root / "nodes"
    nodes_total = 0
    if nodes_dir.is_dir():
        for node_dir in sorted(nodes_dir.iterdir()):
            if not node_dir.is_dir():
                continue
            ndir = node_dir / "skills"
            n = load_skills_in_dir(ndir, origin=f"node:{node_dir.name}")
            nodes_total += n
    counts["node_local"] = nodes_total

    # 3. org/skills/
    from core.paths import home as _root  # 「根在哪」一处回答（含 Windows 分支）

    home = _root()
    org_home = Path(os.getenv(
        "HARNESS_FRAMEWORK_ORG_HOME",
        str(home / "org"),
    ))
    org_skills = org_home / "skills"
    counts["imported"] = load_skills_in_dir(org_skills, origin="imported")

    log.info("Skills loaded: %s", counts)
    return counts

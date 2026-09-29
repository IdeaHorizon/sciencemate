"""验证 docs/skill-spec.md §8 dummy example skill 加载 + 渲染正确。

skill folder 是 `.example_skill`（以 . 开头），skill_loader 自动 skip 不污染真 registry。
这里手动 load 验证规范跑通。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.skill_loader import load_skill_from_folder, parse_skill_md
from core.skill_registry import Skill


EXAMPLE_DIR = Path(__file__).parent.parent / "nodes" / "experiment" / "skills" / ".example_skill"


def test_example_skill_loadable():
    """从 folder 能 load 出 Skill 对象。"""
    s = load_skill_from_folder(EXAMPLE_DIR, origin="node:experiment")
    assert s is not None
    assert s.name == ".example_skill"
    assert s.description.startswith("dummy skill")
    assert s.status == "validated"
    assert s.origin == "node:experiment"


def test_example_skill_frontmatter_fields_complete():
    """6 个必填 frontmatter 字段都被解析。"""
    s = load_skill_from_folder(EXAMPLE_DIR, origin="framework")
    assert len(s.applies_when) >= 2   # 至少 2 条触发场景
    assert "run_bash" in " ".join(s.applies_when) or "save_artifact" in " ".join(s.applies_when)
    assert set(s.tools_used) >= {"read_file", "save_artifact", "freeze_artifact"}
    assert s.expected_outcome


def test_example_skill_renders_into_prompt():
    """render_prompt() 出来应包含工作流 + pitfalls + 完整例子。"""
    s = load_skill_from_folder(EXAMPLE_DIR, origin="framework")
    rendered = s.render_prompt()
    assert "Skill: .example_skill" in rendered
    assert "## 工作流" in rendered
    assert "Pitfalls" in rendered
    assert "完整例子" in rendered
    assert "适用场景" in rendered
    assert "涉及工具" in rendered
    # 不应该包含 frontmatter 原文
    assert "---\nname:" not in rendered


def test_example_skill_parseable_via_parse_helper():
    """直接 parse_skill_md（不靠 folder）也能解析。"""
    text = (EXAMPLE_DIR / "SKILL.md").read_text(encoding="utf-8")
    fm, body = parse_skill_md(text)
    assert fm.get("name") == ".example_skill"
    assert isinstance(fm.get("applies_when"), list)
    assert "工作流" in body
    assert "Pitfalls" in body


def test_dot_prefixed_skill_skipped_by_loader():
    """实际 framework 启动时，'.example_skill' 因为以 . 开头应当被自动跳过。"""
    from core.skill_registry import get_skill
    from core.bootstrap import bootstrap
    bootstrap()
    # bootstrap 完后 registry 里**不应该**有 .example_skill
    s = get_skill(".example_skill")
    assert s is None, "dummy example 不应该污染真 registry（folder 以 . 开头 → skill_loader 应跳过）"

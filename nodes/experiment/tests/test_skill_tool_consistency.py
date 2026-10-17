"""Experiment skills may only instruct calls available on the live tool surface."""
from __future__ import annotations

from pathlib import Path
import re

import yaml

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.tool_registry import list_tools_for_node


_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
_CALL = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<name>[a-z][a-z0-9_]*)\("
)
_ACTION = re.compile(
    r"(?:调用|用|call)\s*`(?P<name>[a-z][a-z0-9_]*)`",
    re.IGNORECASE,
)

# Exact non-tool concepts that intentionally use tool-like spelling in skill
# prose. Keep this list narrow: every entry needs to identify what it really is.
_NON_TOOL_REFERENCES = {
}


def _frontmatter_and_body(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert isinstance(metadata, dict), path
    return metadata, body


def _visible_tool_names() -> set[str]:
    bootstrap(force=True)
    harness = load_harness("experiment")
    return {
        tool.name
        for tool in list_tools_for_node(
            harness.node_type,
            harness.tools,
            state=None,
        )
    }


def _skill_tool_references(path: Path) -> set[str]:
    metadata, body = _frontmatter_and_body(path)
    declared = metadata.get("tools_used") or []
    assert isinstance(declared, list), path
    references = {str(name) for name in declared}
    references.update(match.group("name") for match in _CALL.finditer(body))
    references.update(match.group("name") for match in _ACTION.finditer(body))
    return references.difference(_NON_TOOL_REFERENCES)


def test_every_skill_tool_reference_is_visible_to_experiment() -> None:
    visible = _visible_tool_names()
    stale: list[str] = []
    for path in sorted(_SKILLS_ROOT.glob("*/SKILL.md")):
        for name in sorted(_skill_tool_references(path).difference(visible)):
            stale.append(f"{path.relative_to(_SKILLS_ROOT)}: {name}")

    assert stale == [], "\n".join(stale)


def test_skill_reference_allowlist_is_precise_and_exercised() -> None:
    all_references = {
        reference
        for path in _SKILLS_ROOT.glob("*/SKILL.md")
        for reference in _skill_tool_references_without_allowlist(path)
    }
    assert set(_NON_TOOL_REFERENCES).issubset(all_references)


def _skill_tool_references_without_allowlist(path: Path) -> set[str]:
    metadata, body = _frontmatter_and_body(path)
    declared = metadata.get("tools_used") or []
    references = {str(name) for name in declared}
    references.update(match.group("name") for match in _CALL.finditer(body))
    references.update(match.group("name") for match in _ACTION.finditer(body))
    return references


def test_s2_scientific_skill_is_indexed_and_old_primary_skill_is_retired() -> None:
    bootstrap(force=True)
    harness = load_harness("experiment")
    indexed = set(harness.skills or [])
    assert "scientific-results" in indexed
    assert "primary-scientific-closure" not in indexed
    metadata, body = _frontmatter_and_body(_SKILLS_ROOT / "scientific-results" / "SKILL.md")
    assert metadata.get("status") == "proposed"
    assert "exploratory, no prereg" in body
    assert not (_SKILLS_ROOT / "primary-scientific-closure" / "SKILL.md").exists()


_RETIRED = re.compile(
    r"declared_route|route_step_id|route_type|\breceipt\b|\battempt\b|finalize_external_job|verify_external_job_execution"
    r"|record_operation_completion|classify_experiment_scope|declare_execution_(route|envelope)|reconcile_external_submission"
    r"|resolve_unknown_orphan|supersede_closure_draft|preview_experiment_contract|recovery_basis|analysis_eligible",
    re.I)


def test_indexed_skills_render_without_retired_procedural_vocabulary() -> None:
    """Codex 40：正确加载 skill 不能把刚从工具面删掉的旧行政协议重新注入模型。
    检查的是模型实际看到的东西：索引渲染文本 + 每个索引内 skill 显式加载后的正文。"""
    from core.skill_registry import render_skills
    bootstrap(force=True)
    harness = load_harness("experiment")
    index = render_skills(list(harness.skills or []), node_type="experiment", mode="index")
    assert not _RETIRED.search(index), _RETIRED.search(index).group(0)
    for name in harness.skills or []:
        path = _SKILLS_ROOT / name / "SKILL.md"
        if not path.exists():
            continue  # shared skill（claim_evidence_link 等）不归本节点
        _, body = _frontmatter_and_body(path)
        hit = _RETIRED.search(body)
        assert hit is None, f"{name}: {hit.group(0)!r}"

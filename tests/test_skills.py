"""Skill 系统 smoke test（v2.1 folder + SKILL.md，独立于 KB）。

覆盖：
  - SKILL.md frontmatter 解析
  - 文件夹扫描 + 加载到 registry
  - 3 个 framework-shipped skill 都正确加载
  - context_engine 渲染 skill body markdown
  - status=deprecated 不渲染
  - propose_skill → list_proposals → accept → SKILL.md 落地
  - propose_skill → reject
  - deprecate_skill 改 frontmatter status
  - record_skill_usage + skill_usage_stats
  - list_skills 过滤
  - assets 扫描
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import bootstrap

# Skill loader需要的 framework_root 是 sys.path 项目根，bootstrap 会从那里扫
bootstrap()
import shared.tools.library.skill_tools  # noqa: F401, E402

from core.skill_registry import (                          # noqa: E402
    Skill, all_skills, get_skill, clear_registry, register_skill, render_skills,
)
from core.skill_loader import (                            # noqa: E402
    parse_skill_md, load_skill_from_folder, load_all_skills,
)
from core.state import State                                # noqa: E402
from core.tool_registry import execute as ex                # noqa: E402


@contextlib.contextmanager
def isolated_home():
    """每个 test 用独立 HARNESS_FRAMEWORK_HOME，防 org 层 skill 污染。"""
    with tempfile.TemporaryDirectory() as fresh:
        old = os.environ.get("HARNESS_FRAMEWORK_HOME")
        os.environ["HARNESS_FRAMEWORK_HOME"] = fresh
        try:
            yield fresh
        finally:
            if old is None:
                os.environ.pop("HARNESS_FRAMEWORK_HOME", None)
            else:
                os.environ["HARNESS_FRAMEWORK_HOME"] = old


def make_state(td: Path) -> State:
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(td)
    return State.new(node_type="test", base_dir=td)


# ─────────────────────────────────────────────────────────────────────────────
# Parser + loader
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_skill_md():
    text = """---
name: test_skill
description: A test
applies_when:
  - case A
tools_used:
  - tool_a
---

## Workflow

1. Do X
"""
    fm, body = parse_skill_md(text)
    assert fm["name"] == "test_skill"
    assert fm["applies_when"] == ["case A"]
    assert "Workflow" in body
    print("  ✓ parse_skill_md 解析 frontmatter + body")


def test_parse_skill_md_no_frontmatter():
    text = "## Just a body"
    fm, body = parse_skill_md(text)
    assert fm == {}
    assert body == text
    print("  ✓ parse_skill_md 缺 frontmatter 时 fallback")


def test_load_skill_from_folder():
    with tempfile.TemporaryDirectory() as td:
        skill_dir = Path(td) / "test_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: test_skill
description: A test recipe
applies_when:
  - condition A
tools_used:
  - tool_x
---

## Workflow

1. Step one
""", encoding="utf-8")
        # Asset 文件
        (skill_dir / "examples").mkdir()
        (skill_dir / "examples" / "config.yaml").write_text("foo: bar")

        skill = load_skill_from_folder(skill_dir, origin="framework")
        assert skill is not None
        assert skill.name == "test_skill"
        assert skill.description == "A test recipe"
        assert skill.applies_when == ["condition A"]
        assert skill.tools_used == ["tool_x"]
        assert "Workflow" in skill.body_markdown
        assert "examples/config.yaml" in skill.assets
        assert skill.source_dir == str(skill_dir)
        print("  ✓ load_skill_from_folder + asset 扫描")


def test_load_all_three_framework_skills():
    clear_registry()
    counts = load_all_skills()
    skills = all_skills()
    framework_skills = [s for s in skills if s.origin == "framework"]
    names = {s.name for s in framework_skills}
    assert "systematic_literature_search" in names
    assert "claim_evidence_link" in names
    assert "falsifiability_pretest" in names
    print(f"  ✓ 加载 {counts['framework']} 个 framework skill")


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────

def test_render_skill_full_mode_keeps_body():
    """mode='full'（旧行为 / kill switch）仍然渲染正文。"""
    clear_registry()
    load_all_skills()
    out = render_skills(["systematic_literature_search"], mode="full")
    assert "Skill: systematic_literature_search" in out
    assert "适用场景" in out
    assert "涉及工具" in out
    assert "工作流" in out


def test_render_skill_index_mode_is_routing_signal_only():
    """默认索引模式：留路由信号（名字/描述/适用/取正文的方式），不留正文。

    取正文的方式是 `load_skill(name)`，**不是** read_file 绝对路径 —— 后者在
    平台上 100% 被项目读边界拒（见 tests/test_skill_body_reachable.py）。
    这条断言原本钉的就是那个坏契约：它绿着，而平台上没有一个 skill 读得到。
    """
    clear_registry()
    load_all_skills()
    idx = render_skills(["systematic_literature_search"])
    full = render_skills(["systematic_literature_search"], mode="full")

    assert "systematic_literature_search" in idx          # 名字在
    assert "load_skill('systematic_literature_search')" in idx   # 取正文的方式在
    assert "read_file(" not in idx                         # 且不是文件系统路径
    assert "工作流" not in idx                             # 正文不在
    assert len(idx) < len(full) / 2                        # 且显著更小


def test_always_load_skill_keeps_body_in_index_mode():
    """流程主干 skill 标 always_load 后，索引模式下依然全文常驻。"""
    clear_registry()
    register_skill(Skill(name="backbone", description="d",
                         body_markdown="BACKBONE BODY", always_load=True))
    register_skill(Skill(name="ordinary", description="d",
                         body_markdown="ORDINARY BODY"))
    out = render_skills(["backbone", "ordinary"])
    assert "BACKBONE BODY" in out
    assert "ORDINARY BODY" not in out


def test_render_mode_kill_switch(monkeypatch):
    """HARNESS_SKILLS_RENDER=full 一键退回旧行为。"""
    from core.skill_registry import render_mode

    monkeypatch.delenv("HARNESS_SKILLS_RENDER", raising=False)
    assert render_mode() == "index"
    monkeypatch.setenv("HARNESS_SKILLS_RENDER", "full")
    assert render_mode() == "full"
    monkeypatch.setenv("HARNESS_SKILLS_RENDER", "nonsense")
    assert render_mode() == "index"          # 非法值回落到默认，不炸

    clear_registry()
    register_skill(Skill(name="s", description="d", body_markdown="THE BODY"))
    monkeypatch.setenv("HARNESS_SKILLS_RENDER", "full")
    assert "THE BODY" in render_skills(["s"])


def test_applies_when_scalar_is_not_split_into_characters():
    """frontmatter 允许把 applies_when 写成单个字符串。

    以前 list("every run") 把它拆成一个字符一项，prompt 里渲染成一个字符一行。
    """
    from core.skill_loader import _as_list

    assert _as_list("every writing run") == ["every writing run"]
    assert _as_list(["a", "b"]) == ["a", "b"]
    assert _as_list(None) == []

    clear_registry()
    register_skill(Skill(name="s", description="d", body_markdown="b",
                         applies_when=["every writing run"]))
    idx = render_skills(["s"])
    assert "every writing run" in idx
    assert "e；v；e；r；y" not in idx


def test_node_local_skill_invisible_to_other_nodes():
    """origin='node:X' 的 skill 在别的 node 视角下不渲染。

    可见性是不变量，与渲染模式无关 —— 所以按 skill 名断言，两种模式都要成立。
    """
    for mode in ("index", "full"):
        clear_registry()
        register_skill(Skill(name="analysis_only", description="...",
                                body_markdown="A body", origin="node:analysis"))
        register_skill(Skill(name="writing_only", description="...",
                                body_markdown="W body", origin="node:writing"))
        register_skill(Skill(name="shared_one", description="...",
                                body_markdown="S body", origin="framework"))
        names = ["analysis_only", "writing_only", "shared_one"]

        out_a = render_skills(names, node_type="analysis", mode=mode)
        assert "analysis_only" in out_a and "writing_only" not in out_a \
            and "shared_one" in out_a, mode

        out_w = render_skills(names, node_type="writing", mode=mode)
        assert "writing_only" in out_w and "analysis_only" not in out_w \
            and "shared_one" in out_w, mode


def test_visible_skills_for_helper():
    from core.skill_registry import visible_skills_for
    clear_registry()
    register_skill(Skill(name="s1", description="...", body_markdown="",
                            origin="framework"))
    register_skill(Skill(name="s2", description="...", body_markdown="",
                            origin="imported"))
    register_skill(Skill(name="s3", description="...", body_markdown="",
                            origin="node:analysis"))
    register_skill(Skill(name="s4", description="...", body_markdown="",
                            origin="node:writing"))

    a = {s.name for s in visible_skills_for("analysis")}
    w = {s.name for s in visible_skills_for("writing")}
    all_v = {s.name for s in visible_skills_for(None)}
    assert a == {"s1", "s2", "s3"}
    assert w == {"s1", "s2", "s4"}
    assert all_v == {"s1", "s2", "s3", "s4"}
    print("  ✓ visible_skills_for 按 origin 过滤")


def test_list_skills_filters_by_caller_node():
    """list_skills 默认按 caller state.node_type 过滤。"""
    clear_registry()
    register_skill(Skill(name="shared", description="...", body_markdown="",
                            origin="framework"))
    register_skill(Skill(name="for_analysis", description="...",
                            body_markdown="", origin="node:analysis"))
    register_skill(Skill(name="for_writing", description="...",
                            body_markdown="", origin="node:writing"))

    with tempfile.TemporaryDirectory() as td:
        os.environ["HARNESS_FRAMEWORK_HOME"] = str(td)
        state = State.new(node_type="analysis", base_dir=Path(td))
        r = asyncio.run(ex("list_skills", state))
        names = {s["name"] for s in r["skills"]}
        assert names == {"shared", "for_analysis"}, names
        # for_node 覆盖
        r2 = asyncio.run(ex("list_skills", state, for_node="writing"))
        names2 = {s["name"] for s in r2["skills"]}
        assert names2 == {"shared", "for_writing"}
        # include_other_nodes 看全部
        r3 = asyncio.run(ex("list_skills", state, include_other_nodes=True))
        names3 = {s["name"] for s in r3["skills"]}
        assert names3 == {"shared", "for_analysis", "for_writing"}
    print("  ✓ list_skills 按 caller node 过滤 + for_node 覆盖 + include_other_nodes 全看")


def test_render_skips_deprecated():
    clear_registry()
    register_skill(Skill(
        name="dep_skill", description="deprecated test",
        body_markdown="body", status="deprecated",
    ))
    register_skill(Skill(
        name="active_skill", description="active test",
        body_markdown="active body", status="validated",
    ))
    out = render_skills(["dep_skill", "active_skill"])
    assert "active_skill" in out
    assert "dep_skill" not in out, "deprecated skill 不该渲染"
    # include_deprecated=True 时该出现
    out_all = render_skills(["dep_skill", "active_skill"], include_deprecated=True)
    assert "dep_skill" in out_all
    print("  ✓ render 默认跳 deprecated；include_deprecated=true 显示")


# ─────────────────────────────────────────────────────────────────────────────
# list_skills 工具
# ─────────────────────────────────────────────────────────────────────────────

def test_list_skills_default_hides_deprecated():
    clear_registry()
    register_skill(Skill(name="a", description="a", body_markdown="",
                            status="validated", origin="framework"))
    register_skill(Skill(name="b", description="b", body_markdown="",
                            status="deprecated", origin="framework"))
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        r = asyncio.run(ex("list_skills", state))
        names = {s["name"] for s in r["skills"]}
        assert "a" in names
        assert "b" not in names
    print("  ✓ list_skills 默认隐藏 deprecated")


def test_list_skills_filter_by_status():
    clear_registry()
    register_skill(Skill(name="proposed_one", description="...", body_markdown="",
                            status="proposed", origin="discovered"))
    register_skill(Skill(name="validated_one", description="...", body_markdown="",
                            status="validated", origin="framework"))
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        r = asyncio.run(ex("list_skills", state, status_filter="proposed"))
        assert r["count"] == 1
        assert r["skills"][0]["name"] == "proposed_one"
    print("  ✓ list_skills 按 status 过滤")


# ─────────────────────────────────────────────────────────────────────────────
# propose → accept 流程
# ─────────────────────────────────────────────────────────────────────────────

def test_propose_skill_via_unified_tool():
    """v2.1 瘦身：propose_skill → propose(proposal_type='skill_candidate', extra={skill:{...}})"""
    with isolated_home() as home:
        clear_registry()
        with tempfile.TemporaryDirectory() as base:
            state = make_state(Path(base))
            r = asyncio.run(ex("propose", state,
                                  proposal_type="skill_candidate",
                                  target_entity="skill",
                                  target_id="auto_discovered_test",
                                  proposed_action="create skill auto_discovered_test",
                                  reasoning="seen this pattern 5 times in transcripts",
                                  extra={"skill": {
                                      "name": "auto_discovered_test",
                                      "description": "A discovered recipe",
                                      "body_markdown": "## Steps\n1. Do X",
                                      "applies_when": ["condition X"],
                                      "tools_used": ["tool_y"],
                                  }}))
            assert r["status"] == "success"
            assert r["layer"] == "skill"
            # 统一 list_proposals 看到。compact（默认）用于 triage 一览，不含 extra；
            # 要看 skill body 全量用 detail='full'（find/read 分离，2026-07 有界投影）。
            ls = asyncio.run(ex("list_proposals", state, layer_filter="skill"))
            assert ls["count"] == 1
            assert "extra" not in ls["proposals"][0]           # compact 丢 extra
            full = asyncio.run(ex("list_proposals", state, layer_filter="skill",
                                   detail="full"))
            assert full["proposals"][0]["extra"]["skill"]["name"] == "auto_discovered_test"
    print("  ✓ propose（skill_candidate）→ list_proposals 看到")


def test_propose_skill_rejects_existing_name():
    with isolated_home() as home:
        clear_registry()
        register_skill(Skill(name="taken", description="...", body_markdown="",
                                origin="framework", source_dir="/fake/path"))
        with tempfile.TemporaryDirectory() as base:
            state = make_state(Path(base))
            r = asyncio.run(ex("propose", state,
                                  proposal_type="skill_candidate",
                                  target_entity="skill",
                                  target_id="taken",
                                  proposed_action="create skill taken",
                                  reasoning="trying to propose existing",
                                  extra={"skill": {
                                      "name": "taken",
                                      "description": "...",
                                      "body_markdown": "...",
                                  }}))
            assert r["status"] == "error"
            assert "已存在" in r["error"]
    print("  ✓ propose（skill_candidate）拒绝同名")


def test_accept_skill_proposal_writes_md():
    """v2.1 瘦身：accept_skill_proposal → resolve_proposal(decision='accepted')"""
    with isolated_home() as home:
        clear_registry()
        with tempfile.TemporaryDirectory() as base:
            state = make_state(Path(base))
            r1 = asyncio.run(ex("propose", state,
                                   proposal_type="skill_candidate",
                                   target_entity="skill",
                                   target_id="accepted_skill",
                                   proposed_action="create skill",
                                   reasoning="this is a valuable pattern worth keeping",
                                   extra={"skill": {
                                       "name": "accepted_skill",
                                       "description": "An accepted recipe",
                                       "body_markdown": "## Steps\n1. step one",
                                       "applies_when": ["case A"],
                                       "tools_used": ["tool_z"],
                                   }}))
            prop_id = r1["proposal_id"]
            # resolve as accepted
            r2 = asyncio.run(ex("resolve_proposal", state,
                                   proposal_id=prop_id,
                                   decision="accepted",
                                   reasoning="reviewed, looks correct"))
            assert r2["status"] == "success"
            md_path = Path(r2["side_effect"]["wrote_skill_md"])
            assert md_path.exists()
            content = md_path.read_text()
            assert "name: accepted_skill" in content
            assert "step one" in content
            # registry 立刻可用
            s = get_skill("accepted_skill")
            assert s is not None and s.origin == "imported"
            # proposal 状态变 accepted
            ls = asyncio.run(ex("list_proposals", state, status="accepted"))
            assert ls["count"] == 1
    print("  ✓ resolve_proposal(accepted, skill_candidate) → SKILL.md 落地")


def test_reject_skill_proposal():
    """v2.1 瘦身：reject_skill_proposal → resolve_proposal(decision='rejected')"""
    with isolated_home():
        clear_registry()
        with tempfile.TemporaryDirectory() as base:
            state = make_state(Path(base))
            r1 = asyncio.run(ex("propose", state,
                                   proposal_type="skill_candidate",
                                   target_entity="skill",
                                   target_id="rejected_skill",
                                   proposed_action="create skill",
                                   reasoning="proposing for test purposes",
                                   extra={"skill": {
                                       "name": "rejected_skill",
                                       "description": "x",
                                       "body_markdown": "...",
                                   }}))
            r2 = asyncio.run(ex("resolve_proposal", state,
                                   proposal_id=r1["proposal_id"],
                                   decision="rejected",
                                   reasoning="not useful"))
            assert r2["status"] == "success"
            ls = asyncio.run(ex("list_proposals", state, status="rejected"))
            assert ls["count"] == 1
    print("  ✓ resolve_proposal(rejected) 标 rejected")


# ─────────────────────────────────────────────────────────────────────────────
# deprecate_skill
# ─────────────────────────────────────────────────────────────────────────────

def test_deprecate_skill_updates_frontmatter():
    with isolated_home() as home:
        clear_registry()
        # Manually 写一个 skill folder
        org_skills = Path(home) / "org" / "skills" / "to_deprecate"
        org_skills.mkdir(parents=True)
        (org_skills / "SKILL.md").write_text("""---
name: to_deprecate
description: Will be deprecated
status: validated
---

## Steps
1. step
""", encoding="utf-8")

        # 加载进 registry
        skill = load_skill_from_folder(org_skills, origin="imported")
        register_skill(skill)

        with tempfile.TemporaryDirectory() as base:
            state = make_state(Path(base))
            r = asyncio.run(ex("skill_admin", state,
                                  action="deprecate",
                                  skill_name="to_deprecate",
                                  reasoning="superseded by better_skill_xyz"))
            assert r["status"] == "success"
            # 文件内容应包含 status: deprecated
            text = (org_skills / "SKILL.md").read_text()
            assert "status: deprecated" in text
            # in-memory 也变了
            assert get_skill("to_deprecate").status == "deprecated"
    print("  ✓ deprecate_skill 改 SKILL.md + in-memory")


# ─────────────────────────────────────────────────────────────────────────────
# Usage tracking
# ─────────────────────────────────────────────────────────────────────────────

def test_record_skill_usage_and_stats():
    with isolated_home() as home:
        clear_registry()
        load_all_skills()
        with tempfile.TemporaryDirectory() as base:
            state = make_state(Path(base))
            # Re-set HOME after make_state changed it
            os.environ["HARNESS_FRAMEWORK_HOME"] = home
            # 记 3 次成功 + 1 次失败
            for _ in range(3):
                r = asyncio.run(ex("skill_admin", state,
                                       action="record_use",
                                       skill_name="systematic_literature_search",
                                       outcome="success"))
                assert r["status"] == "success"
            r = asyncio.run(ex("skill_admin", state,
                                   action="record_use",
                                   skill_name="systematic_literature_search",
                                   outcome="failure"))
            # 看 stats
            s = asyncio.run(ex("skill_admin", state,
                                  action="stats",
                                  skill_name="systematic_literature_search"))
            stats = s["stats"]
            assert stats["usage_count"] == 4
            assert stats["success_count"] == 3
            assert stats["failure_count"] == 1
            assert abs(stats["success_rate"] - 0.75) < 0.01
    print("  ✓ record_skill_usage 4 次 → stats 准确")


def test_record_skill_usage_rejects_unknown_skill():
    with isolated_home():
        clear_registry()
        load_all_skills()
        with tempfile.TemporaryDirectory() as base:
            state = make_state(Path(base))
            r = asyncio.run(ex("skill_admin", state,
                                  action="record_use",
                                  skill_name="not_a_real_skill",
                                  outcome="success"))
            assert r["status"] == "error"
            assert "不存在" in r["error"]
    print("  ✓ record_skill_usage 拒绝未知 skill")


# ─────────────────────────────────────────────────────────────────────────────
# Integration: kb 不再有 skill entity
# ─────────────────────────────────────────────────────────────────────────────

def test_kb_no_longer_has_skill_entity():
    from shared.lib.kb_schema import ENTITIES
    assert "skills" not in ENTITIES, "skills 不该在 KB ENTITIES 里"
    assert ENTITIES == ("concepts", "claims", "experiments", "chunks"), \
        f"v3 entity 应为 4 个，实际 {ENTITIES}"
    print(f"  ✓ KB ENTITIES = {ENTITIES}（不含 skills）")


def test_create_skill_kb_tool_removed():
    from core.tool_registry import get_tool
    # 旧 KB skill 工具应该已被删
    assert get_tool("create_skill") is None, "create_skill (KB) 应已删除"
    print("  ✓ 旧 KB skill 工具 (create_skill) 已移除")


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("== Skills 系统 smoke tests (v2.1) ==\n")
    tests = [
        test_parse_skill_md,
        test_parse_skill_md_no_frontmatter,
        test_load_skill_from_folder,
        test_load_all_three_framework_skills,
        test_render_skill_in_system_prompt,
        test_node_local_skill_invisible_to_other_nodes,
        test_visible_skills_for_helper,
        test_list_skills_filters_by_caller_node,
        test_render_skips_deprecated,
        test_list_skills_default_hides_deprecated,
        test_list_skills_filter_by_status,
        test_propose_skill_via_unified_tool,
        test_propose_skill_rejects_existing_name,
        test_accept_skill_proposal_writes_md,
        test_reject_skill_proposal,
        test_deprecate_skill_updates_frontmatter,
        test_record_skill_usage_and_stats,
        test_record_skill_usage_rejects_unknown_skill,
        test_kb_no_longer_has_skill_entity,
        test_create_skill_kb_tool_removed,
    ]
    for t in tests:
        print(f"- {t.__name__}")
        t()
    print(f"\nALL {len(tests)} TESTS PASS ✓")

"""Skill 正文在 **project-bound run** 里必须取得到。

2026-08-17 平台实测的缺口：两级加载（#447）把 skill 正文换成索引里的一行
`read_file('<框架安装目录>/SKILL.md')`，而 v2.1 的项目读边界规定绑了
`project_worktree` 的 run 只能读 run root / project root 之内 —— 框架安装目录
永远在边界外。于是平台上**所有** project-bound run 的 skill 正文 100% 不可读，
literature 节点连撞两次后自述"Skill 文件在项目边界外无法直接读取"，凭理解硬跑。

为什么当时全绿：既有 skill 测试建的都是**没绑 project 的** state（CLI / fixture
路径），读侧不设边界。所以本文件的每个 test 都在**真 git worktree 上绑定**跑 ——
判据必须走模型真实走的那条路，否则护栏只在测试里成立。
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from core.bootstrap import bootstrap
from core.loader import list_harnesses, load_harness
from core.project_workspace import bind_project_workspace
from core.skill_registry import (
    Skill, clear_registry, get_skill, register_skill, render_skills,
)
from core.skill_loader import load_all_skills
from core.state import State
from core.tool_registry import execute as ex, get_tool

bootstrap()   # 注册工具（含 load_skill）+ 扫 skill folder


def _worktree(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=root, check=True, env=env)
    return root


def _bound(tmp_path, node_type, root):
    """跟平台一样绑 Project 的 state —— 缺了这一步就测不到这个 bug。"""
    state = State(run_id=f"r-{node_type}", node_type=node_type,
                  root=tmp_path / f"run-{node_type}")
    bind_project_workspace(state, root)
    return state


@pytest.fixture
def framework_skills():
    clear_registry()
    load_all_skills()
    yield
    clear_registry()
    load_all_skills()


# ─────────────────────────────────────────────────────────────────────────────
# 核心回归：绑了 Project 也要拿得到正文
# ─────────────────────────────────────────────────────────────────────────────

def test_read_file_on_skill_md_is_still_blocked(tmp_path, framework_skills):
    """先钉住**病根本身**：框架目录的绝对路径在绑定态下就是读不了。

    这条不是在测 bug，是在保证下一条测的是真东西 —— 哪天读边界被放宽（比如
    有人给框架目录开白名单），这条会先红，提醒"防线换了形状"。
    """
    root = _worktree(tmp_path)
    state = _bound(tmp_path, "literature", root)
    skill = get_skill("systematic_literature_search")
    assert skill is not None and skill.skill_md_path()

    result = asyncio.run(ex("read_file", state, path=skill.skill_md_path()))
    assert result["status"] == "error"
    assert "escaped the Project boundary" in result["error"]


def test_load_skill_returns_body_under_project_binding(tmp_path, framework_skills):
    """同一个 state、同一个 skill —— load_skill 必须把正文送进来。"""
    root = _worktree(tmp_path)
    state = _bound(tmp_path, "literature", root)

    result = asyncio.run(ex("load_skill", state, name="systematic_literature_search"))
    assert result["status"] == "success"
    body = result["body_markdown"]
    assert body.strip(), "正文为空 = 等于没送到"
    assert body == get_skill("systematic_literature_search").body_markdown


def test_index_advertises_load_skill_not_a_filesystem_path(framework_skills):
    """广告与默认必须一致：索引里写什么，模型就会调什么。"""
    idx = render_skills(["systematic_literature_search"], node_type="literature")
    assert "load_skill('systematic_literature_search')" in idx
    assert "read_file(" not in idx
    assert "SKILL.md" not in idx


def test_full_mode_assets_advertise_load_skill(framework_skills):
    """kill switch（HARNESS_SKILLS_RENDER=full）那条路上 assets 一样在边界外。"""
    clear_registry()
    register_skill(Skill(name="with_assets", description="d", body_markdown="B",
                          origin="framework", source_dir="/framework/skills/with_assets",
                          assets=["examples/a.md"]))
    out = render_skills(["with_assets"], mode="full")
    assert "load_skill(name='with_assets', asset='<下面的相对路径>')" in out
    assert "read_file" not in out


# ─────────────────────────────────────────────────────────────────────────────
# 白名单：声明了 skill 就必须够得着 load_skill
# ─────────────────────────────────────────────────────────────────────────────

def test_every_node_that_sees_skills_can_call_load_skill():
    """扫盘，不写名单 —— 名单式判据对新节点默认漏过。

    白名单里没有 load_skill，模型照着索引调只会拿到"工具不在本节点白名单内"，
    skill 等于只剩标题。
    """
    checked = 0
    for node_type in list_harnesses():
        harness = load_harness(node_type)
        sees_skills = bool(harness.skills) or "list_skills" in harness.tools
        if not sees_skills:
            continue
        checked += 1
        assert "load_skill" in harness.tools, (
            f"节点 {node_type} 看得见 skill（skills={harness.skills or '经 list_skills'}）"
            f"却没有 load_skill —— 正文取不到"
        )
    assert checked >= 6, f"只检到 {checked} 个节点，扫盘没扫到（判据自己失效了）"


def test_load_skill_is_registered_and_low_risk():
    tool = get_tool("load_skill")
    assert tool is not None
    assert tool.allowed_node_types is None, "取自己的 SOP 不该按节点类型设限"


def test_list_skills_tells_the_caller_how_to_get_the_body(tmp_path, framework_skills):
    """brief 里带 source_dir（框架安装路径），必须同时说清它不是能 read_file 的位置。"""
    root = _worktree(tmp_path)
    state = _bound(tmp_path, "literature", root)
    listed = asyncio.run(ex("list_skills", state))
    assert listed["status"] == "success" and listed["count"] > 0
    assert "load_skill" in listed["note"]


# ─────────────────────────────────────────────────────────────────────────────
# 可见性 / 报错契约 / assets
# ─────────────────────────────────────────────────────────────────────────────

def test_node_local_skill_stays_invisible_but_error_names_the_way_out(tmp_path,
                                                                      framework_skills):
    root = _worktree(tmp_path)
    state = _bound(tmp_path, "literature", root)
    result = asyncio.run(ex("load_skill", state, name="scientific-schematic"))
    assert result["status"] == "error"
    assert "不可见" in result["error"]
    assert "for_node" in result["error"], "只说不许、不说该怎么办 = 逼模型猜"


def test_for_node_override_lets_reviewer_read_the_sop_it_reviews(tmp_path,
                                                                 framework_skills):
    root = _worktree(tmp_path)
    reviewer = _bound(tmp_path, "_reviewer", root)
    result = asyncio.run(ex("load_skill", reviewer, name="scientific-schematic",
                             for_node="postprocess"))
    assert result["status"] == "success"
    assert result["body_markdown"].strip()


def test_unknown_skill_error_lists_legal_values(tmp_path, framework_skills):
    """合法取值只在运行时报错里出现是可以的 —— 但必须**真的列出来**。"""
    root = _worktree(tmp_path)
    state = _bound(tmp_path, "literature", root)
    result = asyncio.run(ex("load_skill", state, name="no_such_skill"))
    assert result["status"] == "error"
    assert "systematic_literature_search" in result["error"]


def test_asset_reachable_and_confined_to_the_skill_folder(tmp_path):
    """assets 跟 SKILL.md 住一起，一样在边界外 —— 走同一个工具，且只放行清单里的。"""
    clear_registry()
    skill_dir = tmp_path / "skills" / "with_assets"
    (skill_dir / "examples").mkdir(parents=True)
    (skill_dir / "examples" / "a.md").write_text("LINE1\nLINE2\nLINE3\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    register_skill(Skill(name="with_assets", description="d", body_markdown="B",
                          origin="framework", source_dir=str(skill_dir),
                          assets=["examples/a.md"]))

    root = _worktree(tmp_path)
    state = _bound(tmp_path, "literature", root)

    ok = asyncio.run(ex("load_skill", state, name="with_assets", asset="examples/a.md"))
    assert ok["status"] == "success"
    assert ok["content"] == "LINE1\nLINE2\nLINE3"
    assert ok["truncated"] is False

    # 未声明的相对路径 / 目录穿越都不放行：这个工具是读边界的合法出口，
    # 它自己失守就等于边界不存在。
    for bad in ("../../secret.txt", "examples/../../secret.txt", "secret.txt"):
        denied = asyncio.run(ex("load_skill", state, name="with_assets", asset=bad))
        assert denied["status"] == "error", f"{bad} 不该被放行"
        assert "TOP SECRET" not in str(denied)

    clear_registry()
    load_all_skills()


def test_asset_truncation_says_so_and_gives_the_continuation(tmp_path):
    """截断必须自己说出来 + 给续读方式，否则模型把半截当全文。"""
    clear_registry()
    skill_dir = tmp_path / "skills" / "long_asset"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "references" / "big.md").write_text(
        "\n".join(f"row {i}" for i in range(50)), encoding="utf-8")
    register_skill(Skill(name="long_asset", description="d", body_markdown="B",
                          origin="framework", source_dir=str(skill_dir),
                          assets=["references/big.md"]))

    root = _worktree(tmp_path)
    state = _bound(tmp_path, "literature", root)
    first = asyncio.run(ex("load_skill", state, name="long_asset",
                            asset="references/big.md", limit=10))
    assert first["truncated"] is True
    assert first["total_lines"] == 50
    assert "offset=10" in first["note"]

    rest = asyncio.run(ex("load_skill", state, name="long_asset",
                           asset="references/big.md", offset=10, limit=100))
    assert rest["truncated"] is False
    assert rest["content"].splitlines()[0] == "row 10"

    clear_registry()
    load_all_skills()

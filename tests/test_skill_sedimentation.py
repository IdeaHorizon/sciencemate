"""v3.5 skill 主动沉淀 + 工具可达性契约。

背景：三轮 E2E 零 skill 沉淀 —— 机制原因是根本没有"提议 skill"的工具
（skill_admin 只有 record_use/stats/deprecate）。该成为 skill 的执行经验全部
滞留在 memory prose，每个新 run 重新踩。准入门用**复发证据**（prompt 类 skill
无法跑 smoke test，但"在真实运行里重复出现"是可机械验证的复用价值信号）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core import memory as M
from core.state import State

bootstrap()

_BODY = (
    "## 触发条件\n连续 3 轮 `search_papers` 没有新增文献。\n\n"
    "## 步骤\n1. 停止微调查询词；2. 拆解技术组件做分面检索；"
    "3. 每轮后写 scratchpad 记录去重与相关性判断。\n\n"
    "## 失败信号\n继续返回同一批文献 = 查询空间已耗尽，改用泛化检索。\n"
)


def _st(tmp_path: Path, wt: Path, pid="p_skill") -> State:
    st = State.new(node_type="_curator", base_dir=tmp_path, project_id=pid,
                   project_worktree=wt)
    M.ensure_skeleton(st)
    return st


def _note(state: State, text: str) -> str:
    """写一条手册条目，返回可用作 source_entries 的正文前缀。"""
    M.append_manual(state, text=text, section=M.SECTION_METHOD,
                    nodes=["literature"], run_id="r_skill")
    return text[:20]


@pytest.mark.asyncio
async def test_single_occurrence_is_filed_with_its_evidence_strength(tmp_path, mem_worktree):
    """一次性经验照提，evidence_strength=single 钉在提案上给人审（判决拆除 O9：
    「复发够不够」是充分性判决，提案通道下一步就是人审）。墙若加回来这条转红。"""
    from shared.tools.library.skill_tools import _propose_skill_from_memory
    from shared.tools.library.proposals import _find_proposal
    st = _st(tmp_path, mem_worktree)
    c = _note(st, "niche 主题上反复微调查询词没有效果")
    r = await _propose_skill_from_memory(
        st, name="literature_faceted_search",
        description="文献检索连续多轮无新增时，改用分面检索与泛化检索的处置策略",
        body_markdown=_BODY, source_entries=[c])
    assert r["status"] == "success", r
    assert r["evidence_strength"] == "single" and r["evidence_verified"] is True
    stored, _, _ = _find_proposal(st, r["proposal_id"])
    assert stored["extra"]["evidence_strength"] == "single"
    assert stored["extra"]["recurrence_evidence"]["max_recurrence_count"] == 1


@pytest.mark.asyncio
async def test_no_source_entries_is_filed_with_evidence_none(tmp_path, mem_worktree):
    """没给 source_entries 也照提：evidence_strength=none 钉在提案上。"""
    from shared.tools.library.skill_tools import _propose_skill_from_memory
    st = _st(tmp_path, mem_worktree, "p_skill_none")
    r = await _propose_skill_from_memory(
        st, name="literature_faceted_search",
        description="文献检索连续多轮无新增时，改用分面检索与泛化检索的处置策略",
        body_markdown=_BODY)
    assert r["status"] == "success", r
    assert r["evidence_strength"] == "none"


@pytest.mark.asyncio
async def test_no_worktree_is_filed_unverified(tmp_path):
    """无 Project worktree：事实源不在场不是判决——记 evidence_verified=false。"""
    from shared.tools.library.skill_tools import _propose_skill_from_memory
    st = State.new(node_type="_curator", base_dir=tmp_path, project_id="p_skill_nowt")
    r = await _propose_skill_from_memory(
        st, name="literature_faceted_search",
        description="文献检索连续多轮无新增时，改用分面检索与泛化检索的处置策略",
        body_markdown=_BODY, source_entries=["某条没法核对的手册条目"])
    assert r["status"] == "success", r
    assert r["evidence_verified"] is False


@pytest.mark.asyncio
async def test_two_candidates_pass(tmp_path, mem_worktree):
    from shared.tools.library.skill_tools import _propose_skill_from_memory
    st = _st(tmp_path, mem_worktree, "p_skill2")
    a = _note(st, "niche 主题反复微调查询词无效，应拆解技术组件")
    b = _note(st, "检索停滞时应转向泛化检索与综述文献")
    r = await _propose_skill_from_memory(
        st, name="literature_faceted_search",
        description="文献检索连续多轮无新增时，改用分面检索与泛化检索的处置策略",
        body_markdown=_BODY, source_entries=[a, b])
    assert r["status"] == "success", r
    assert r["layer"] == "skill"


@pytest.mark.asyncio
async def test_single_candidate_with_recurrence_passes(tmp_path, mem_worktree):
    """近似复发聚合过的单条（recurrence_count≥2）也算证据。"""
    from shared.tools.library.skill_tools import _propose_skill_from_memory
    st = _st(tmp_path, mem_worktree, "p_skill3")
    base = "文献检索连续三轮无新增时必须拆解技术组件做分面检索而不是微调查询词"
    c = _note(st, base)
    again = M.append_manual(st, text=base + " 再次出现",
                            section=M.SECTION_METHOD, nodes=["literature"],
                            run_id="r2")            # 近似 → 聚合而非新增
    assert again["created"] is False and again["seen"] >= 2
    r = await _propose_skill_from_memory(
        st, name="literature_faceted_search",
        description="文献检索连续多轮无新增时，改用分面检索与泛化检索的处置策略",
        body_markdown=_BODY, source_entries=[c])
    assert r["status"] == "success", r


@pytest.mark.asyncio
async def test_hallucinated_tool_names_are_pinned_on_the_proposal(tmp_path, mem_worktree):
    """skill 会被注入所有相关节点 prompt —— 幻觉工具名会持续误导每个 run。
    探测是真的，但钉在提案上（unknown_tools）比拒绝更强：人审一眼可见。"""
    from shared.tools.library.skill_tools import _propose_skill_from_memory
    from shared.tools.library.proposals import _find_proposal
    st = _st(tmp_path, mem_worktree, "p_skill4")
    a = _note(st, "文献检索停滞时应拆解技术组件做分面检索")
    b = _note(st, "检索无新增时应转向综述与技术报告等泛化来源")
    bad = _BODY + "\n用 `magic_paper_fetcher()` 一步拿全文。\n"
    r = await _propose_skill_from_memory(
        st, name="literature_faceted_search",
        description="文献检索连续多轮无新增时，改用分面检索与泛化检索的处置策略",
        body_markdown=bad, source_entries=[a, b])
    assert r["status"] == "success", r
    assert "magic_paper_fetcher" in r["unknown_tools"]
    stored, _, _ = _find_proposal(st, r["proposal_id"])
    assert "magic_paper_fetcher" in stored["extra"]["unknown_tools"]


@pytest.mark.asyncio
async def test_real_tool_names_allowed(tmp_path, mem_worktree):
    from shared.tools.library.skill_tools import _propose_skill_from_memory
    st = _st(tmp_path, mem_worktree, "p_skill5")
    a = _note(st, "文献检索停滞时应拆解技术组件做分面检索")
    b = _note(st, "检索无新增时应转向综述与技术报告等泛化来源")
    ok = _BODY + "\n每轮后调 `write_scratchpad` 记录判断；用 `search_papers` 检索。\n"
    r = await _propose_skill_from_memory(
        st, name="literature_faceted_search",
        description="文献检索连续多轮无新增时，改用分面检索与泛化检索的处置策略",
        body_markdown=ok, source_entries=[a, b])
    assert r["status"] == "success", r


@pytest.mark.asyncio
async def test_rejects_thin_body_and_bad_name(tmp_path, mem_worktree):
    from shared.tools.library.skill_tools import _propose_skill_from_memory
    st = _st(tmp_path, mem_worktree, "p_skill6")
    a = _note(st, "文献检索停滞时应拆解技术组件做分面检索")
    b = _note(st, "检索无新增时应转向综述与技术报告等泛化来源")
    ids = [a, b]
    # 契约归 schema：name 格式（pattern）与 body 非空（minLength:1）在
    # parameters_schema 里，派发口核一次 —— 所以走 execute。
    from core.tool_registry import execute
    r1 = await execute(
        "propose_skill_from_memory", st, name="BadName",
        description="文献检索连续多轮无新增时，改用分面检索与泛化检索的处置策略",
        body_markdown=_BODY, source_entries=ids)
    assert r1["status"] == "error" and "name" in r1["error"]
    assert r1["parameter_violations"]
    # 判决拆除：字数闸删——短 body 放行（提案通道，下一步就是人审），空 body 拒。
    r2 = await execute(
        "propose_skill_from_memory", st, name="ok_skill_name",
        description="文献检索连续多轮无新增时，改用分面检索与泛化检索的处置策略",
        body_markdown="   ", source_entries=ids)
    assert r2["status"] == "error" and "body_markdown" in r2["error"]


def test_curator_has_sedimentation_tools():
    from core.loader import load_harness
    tools = set(load_harness("_curator").tools)
    assert "propose_skill_from_memory" in tools
    # 承重层已并入知识卡草稿区（同一个问题两套记账 → 一套）
    assert "draft_knowledge_card" in tools
    assert "promote_load_bearing" not in tools


# ── 工具可达性契约（防注册了却没人能用的漂移）──────────────────────────────

def test_unreachable_tool_set_does_not_grow():
    """注册了但不在任何节点白名单里的工具 = 谁都调不到。

    2026-07-27 基线 12 个（9 个 data 域工具由 cuib 决定是否接线，3-4 个框架级）。
    2026-08-06 基线 12→17（两笔 owner 主动解线，先记账不删除）：
    - PR #316（experiment 收尾闭环）移除 declare_job / job_progress
      （owner 契约测试断言 not in tools，以 submit_job / job_status +
      check_external_job_health 替代）。去留随 jobs 系统整合一并裁决。
    - PR #318（data 节点更新）移除 Designer/Critic planning gate 工具
      （analyze/design/critique_preprocessing_plan、get_preprocessing_plan_status、
      generate_preprocessing_artifact、list_scientific_disciplines），改为
      execute_preprocessing_python + 直接执行路径。同时把 data_web_search/
      data_web_download/inspect_scientific_asset 接回白名单（-3）。
    这条测试不强迫清零 —— 它防的是**静默增长**：新注册工具忘了加白名单时炸。
    """
    from core import tool_registry as tr
    from core.loader import list_harnesses, load_harness

    # 节点清单**扫盘**，不手写。原先是一份硬编码名单，于是新增节点时它名下的
    # 工具会被判成"不可达"—— 一道防幽灵工具的闸，自己犯了写名单的毛病，而且
    # 失效方向是**误报**：把可达的说成不可达，逼着后来的人去抬基线，真信号
    # 就此被掩盖。（2026-08-18 接入 observation 时现形。）
    reachable: set[str] = set()
    for n in list_harnesses():
        try:
            reachable |= set(load_harness(n).tools)
        except Exception:
            continue
    # 只统计**真实库**注册的工具：其它 test 会往全局注册表塞临时工具
    #（test_tool_registry_spec / test_pause_resume 等），不过滤会让本测试
    # 随执行顺序飘。按来源模块判定，比按名字黑名单稳。
    def _is_library(name: str) -> bool:
        src = tr._REGISTRY.tool_sources.get(name) or ""
        definition = tr._REGISTRY.tools.get(name)
        return (
            src.startswith(("shared.", "nodes.", "core."))
            and not getattr(definition, "required_runtime_capability", None)
            and not getattr(definition, "internal_only", False)
        )

    unreachable = {n for n in set(tr._REGISTRY.tools) - reachable if _is_library(n)}
    assert len(unreachable) <= 17, (
        f"不可达工具增至 {len(unreachable)} 个（基线 17，见 docstring 记账）："
        f"{sorted(unreachable)}\n新注册工具要么加进某节点白名单，要么明确它是"
        f"内部/测试专用。"
    )

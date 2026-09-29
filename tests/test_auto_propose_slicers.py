"""Phase I 测试：find_org_promotion_candidates + find_synthesis_candidates。

两个工具都是 curator Mode 2 dreaming 必跑的机械扫描器，默认 auto_propose=True
写 inbox。这里覆盖：
  - 命中条件 / 不命中条件分流正确
  - auto_propose=True 真写出 propose（org_promotion 走 artifact，synthesis 走
    kb_proposals.jsonl）
  - dedup：已有 pending propose → 跳过
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute as execute_tool


# ─────────────────────────────────────────────────────────────────────────────
# find_org_promotion_candidates
# ─────────────────────────────────────────────────────────────────────────────


async def _make_concept(state: State, name: str = "method_x",
                          concept_type: str = "method") -> str:
    res = await execute_tool(
        "create_concept", state,
        canonical_name=name, concept_type=concept_type,
        description="seed concept for tests",
    )
    return res["id"]


def _make_terminal(state: State) -> None:
    """终态：一份已冻结的 manuscript。晋升只在终态发生。

    冻结 = 账本上的 freeze 行（`state.mark_frozen`）；save 时手写
    `metadata.frozen` 会被剥掉。
    """
    saved = state.save_artifact("manuscript", "Paper", "# 终稿\n\n正文…")
    state.mark_frozen(saved["id"])


def _card(statement: str = "通用方法在该条件区间外推误差偏大") -> dict:
    """一张合格的去项目化知识卡。字段集以 KNOWLEDGE_CARD_FIELDS 为准。"""
    return {
        "domain": "physics.comp-ph",  # 骨架分类
        "statement": statement,
        "applicability": {"regime": "该条件区间", "tested_on": ["case A"]},
        "why": "训练分布不覆盖该区间，描述子外推导致失真",
        "practice": "该区间的结果需独立校验后再采信",
        "confidence_basis": "单项目实测，证据链含文献锚点",
        "evidence": [],
    }


async def _make_claim(
    state: State, *, concept_id: str, claim_type: str,
    replication_count: int = 0, confidence: float = 0.5,
    status: str | None = None, scope: str = "project",
    text_suffix: str = "",
) -> dict:
    """造一条 claim。methodological/theoretical 自动给 2 个 source 满足
    independent_source_count ≥ 2；status='validated' 用 update_claim_status 翻。"""
    sources = ["doi:10/a", "doi:10/b"] if claim_type in (
        "methodological", "theoretical",
    ) else ["doi:10/x"]
    if scope == "org":
        # org 没有出生通道，且 scope 一旦定了不漂移 —— 直接造一条**已晋升**的
        # org 记录（带出处），模拟"上一个项目晋升上来的"。真实路径是 curator
        # 终态晋升（P2）。
        rec, _ = state.write_kb("claims", {
            "claim_text": f"a {claim_type} claim {text_suffix or concept_id}",
            "claim_type": claim_type, "confidence": confidence,
            "replication_count": replication_count,
            "concept_ids": [concept_id], "sources": sources,
            "scope": "org",
            "promoted_from": {"project_id": "p_prev", "source_id": "claim_prev",
                              "approved_by": "wangd",
                              "at": "2026-08-21T00:00:00Z"},
        })
        return {"id": rec["id"], "status": "success"}

    res = await execute_tool(
        "create_claim", state,
        claim_text=f"a {claim_type} claim {text_suffix or concept_id}",
        claim_type=claim_type, confidence=confidence,
        replication_count=replication_count,
        concept_ids=[concept_id], sources=sources,
        scope=scope,
    )
    assert res.get("status") == "success", f"create_claim failed: {res}"
    if status == "validated":
        # v3.1：翻 validated 必须挂证据链接 —— fixture 造一个真实 chunk 当证据
        chunk, _ = state.write_kb("chunks", {
            "text": f"evidence text for {res['id']}",
            "source": "doi:10/evidence",
        })
        flip = await execute_tool(
            "update_claim_status", state,
            claim_id=res["id"], new_status="validated",
            evidence_ids=[chunk["id"]],
            reasoning="test fixture: pre-validate to test slicer thresholds",
        )
        assert flip.get("status") == "success", f"update_claim_status failed: {flip}"
    return res


@pytest.mark.asyncio
async def test_org_promotion_needs_terminal_and_a_card(tmp_path):
    """终态晋升扫盘：三查通过才进人批车道。

    这条原来钉的是 `replication_count ≥ 3 且 confidence ≥ 0.85`。那道门在
    两层重构后**不可能被满足** —— 复现记数只有跨项目 dreaming 归并才累加，
    首个做出结论的项目永远是 1。判据换成三查（终态/证据冻结/去项目化），
    复现记数移到晋升之后当 confidence_basis，不再是入场券。
    """
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_org_hit")
    cid = await _make_concept(state)
    good = await _make_claim(state, concept_id=cid, claim_type="methodological",
                             status="validated")

    # 未终态：一条都不出
    early = await execute_tool(
        "curator_scan", state, scan_type="org_promotion_candidates")
    assert early["terminal"] is False and early["candidates"] == []

    _make_terminal(state)

    # 终态但没草稿：列出来但被 deprojectified 挡住 —— "还差什么"要显式可见
    no_card = await execute_tool(
        "curator_scan", state, scan_type="org_promotion_candidates")
    assert no_card["terminal"] is True
    assert any(b["source_id"] == good["id"] for b in no_card["blocked"])
    assert no_card["proposals_created"] == 0

    # 带上去项目化知识卡：进人批车道并发 propose
    res = await execute_tool(
        "curator_scan", state, scan_type="org_promotion_candidates",
        auto_propose=True, drafts={good["id"]: _card()})
    assert res["status"] == "success"
    assert good["id"] in [c["source_id"] for c in res["human_batch"]]
    assert res["proposals_created"] >= 1

    # 提议进的是**组织的**待审（org 层），不是项目里的一件 artifact —— 那件 artifact
    # 从前谁也批不到（`resolve_proposal` 只认 jsonl 队列）。卡原样抄进待审。
    from core import kb_promotion as kp

    queued = [p for p in kp.review_queue() if p["source_id"] == good["id"]]
    assert len(queued) == 1 and queued[0]["status"] == "pending"
    assert queued[0]["project_id"] == "p_org_hit"
    assert queued[0]["card"]["statement"] == _card()["statement"]
    assert not state.list_artifacts(artifact_type="propose_org_promotion")


@pytest.mark.asyncio
async def test_only_declared_claim_types_map_to_org_kinds(tmp_path):
    """哪种 claim 晋升成哪种 org 条目，由 KIND_BY_CLAIM_TYPE 一处声明。

    这条原来叫 filters_empirical_and_low_replication，钉的是
    「empirical 不入候选 + replication/confidence 阈值」。两条判据都已删：
      - empirical **现在是主力**（→ verified_finding），旧白名单反而把它挡在外面
      - 阈值门在两层重构后不可满足（复现只在跨项目归并时累加）

    而且它在改动后一度变成**空转的假绿**：未终态 → 候选恒空 → 三条断言
    全部无意义地通过。重写成真的类型映射测试，并显式造终态。
    """
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_org_filter")
    cid = await _make_concept(state)

    emp = await _make_claim(state, concept_id=cid, claim_type="empirical",
                            status="validated", text_suffix="emp")
    meth = await _make_claim(state, concept_id=cid, claim_type="methodological",
                             status="validated", text_suffix="meth")
    _make_terminal(state)

    res = await execute_tool(
        "curator_scan", state, scan_type="org_promotion_candidates",
        auto_propose=False,
        drafts={emp["id"]: _card("A 类体系在该区间外推误差偏大"),
                meth["id"]: _card("该测法在这类体系上需要额外校正步骤")},
    )
    assert res["terminal"] is True
    by_id = {c["source_id"]: c for c in res["candidates"]}

    from core.kb_promotion import KIND_RECIPE, KIND_VERIFIED_FINDING

    assert by_id[emp["id"]]["kind"] == KIND_VERIFIED_FINDING
    assert by_id[meth["id"]]["kind"] == KIND_RECIPE
    assert res["proposals_created"] == 0, "auto_propose=False 不该写任何东西"


@pytest.mark.asyncio
async def test_org_promotion_dedup_skips_already_pending(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_org_dedup")
    cid = await _make_concept(state)
    good = await _make_claim(
        state, concept_id=cid, claim_type="methodological",
        status="validated",
    )
    _make_terminal(state)
    drafts = {good["id"]: _card()}

    first = await execute_tool(
        "curator_scan", state, scan_type="org_promotion_candidates",
        auto_propose=True, drafts=drafts,
    )
    assert first["proposals_created"] == 1

    # 再跑一次：dedup 应跳过 —— 终态扫盘会被跑多次（人批前重看清单、
    # curator 重跑），重复提议会把 inbox 淹掉
    second = await execute_tool(
        "curator_scan", state, scan_type="org_promotion_candidates",
        auto_propose=True, drafts=drafts,
    )
    assert second["proposals_created"] == 0
    assert good["id"] in second["skipped_already_pending"]
    assert all(c["source_id"] != good["id"] for c in second["candidates"])


@pytest.mark.asyncio
async def test_org_promotion_already_org_scope_not_listed(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_org_already")
    cid = await _make_concept(state)
    # 直接写 scope=org —— 不该被扫出来
    res_c = await _make_claim(
        state, concept_id=cid, claim_type="methodological",
        replication_count=5, confidence=0.95, status="validated",
        scope="org",
    )
    res = await execute_tool(
        "curator_scan", state, scan_type="org_promotion_candidates", auto_propose=False,
    )
    cand_ids = {c["source_id"] for c in res["candidates"]}
    assert res_c["id"] not in cand_ids


# ─────────────────────────────────────────────────────────────────────────────
# find_synthesis_candidates
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesis_hits_concept_with_enough_claims(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_syn_hit")
    cid = await _make_concept(state, name="GAP")
    # 3 条 anchor 在同一 concept 上 → 命中
    for i in range(3):
        await _make_claim(
            state, concept_id=cid, claim_type="empirical",
            text_suffix=f"e{i}",
        )

    res = await execute_tool(
        "curator_scan", state, scan_type="synthesis_candidates", auto_propose=True,
    )
    assert res["status"] == "success"
    cand_ids = {c["concept_id"] for c in res["candidates"]}
    assert cid in cand_ids
    assert res["proposals_created"] >= 1

    # PR#99 的不变量不变：proposal 按 **target 的真实 scope** 路由，
    # 不按触发它的项目（那次 89% 跨课题污染的根因）。变的是 target 的 scope：
    # concept 出生一律 project，所以这条 proposal 落**本项目**队列。
    # 换句话说这条测试守的还是同一条规矩，只是走到了另一个分支。
    from shared.tools.library.proposals import _kb_proposal_write_path
    proposals_path = _kb_proposal_write_path(
        state, state.get_kb_record("concepts", cid))
    assert proposals_path.exists()
    lines = [json.loads(l) for l in proposals_path.read_text().splitlines() if l.strip()]
    syn = [p for p in lines if p["proposal_type"] == "kb_synthesis_candidate"]
    assert any(p["target_id"] == cid for p in syn)
    # extra 带 source claim ids
    one = next(p for p in syn if p["target_id"] == cid)
    assert len(one["extra"]["candidate_source_claim_ids"]) >= 3
    # 反向不变量（PR#99 的另一半）：**org 共享队列**不该收到这条
    # project-scope 的提议 —— 路由永远看 target 的 scope，不看
    # "恰好触发这次扫描的是谁"。
    from shared.tools.library.proposals import _org_kb_proposals_path
    org_path = _org_kb_proposals_path()
    if org_path.exists():
        org_lines = [json.loads(l) for l in
                     org_path.read_text().splitlines() if l.strip()]
        assert not any(p.get("target_id") == cid for p in org_lines)


@pytest.mark.asyncio
async def test_synthesis_skips_when_under_threshold(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_syn_low")
    cid = await _make_concept(state)
    # 只 2 条 → 不到默认 threshold=3
    for i in range(2):
        await _make_claim(
            state, concept_id=cid, claim_type="empirical",
            text_suffix=f"only{i}",
        )

    res = await execute_tool(
        "curator_scan", state, scan_type="synthesis_candidates", auto_propose=True,
    )
    cand_ids = {c["concept_id"] for c in res["candidates"]}
    assert cid not in cand_ids
    assert res["proposals_created"] == 0


@pytest.mark.asyncio
async def test_synthesis_skips_when_already_covered(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_syn_covered")
    cid = await _make_concept(state)
    c_ids = []
    for i in range(3):
        c = await _make_claim(
            state, concept_id=cid, claim_type="empirical",
            text_suffix=f"e{i}",
        )
        c_ids.append(c["id"])

    # 写一条 synthesis claim 直接覆盖这个 concept
    await execute_tool(
        "create_claim", state,
        claim_text="synthesizing the pattern",
        claim_type="synthesis", confidence=0.7,
        concept_ids=[cid], sources=c_ids[:2],
    )

    res = await execute_tool(
        "curator_scan", state, scan_type="synthesis_candidates", auto_propose=True,
    )
    cand_ids = {c["concept_id"] for c in res["candidates"]}
    assert cid not in cand_ids


@pytest.mark.asyncio
async def test_synthesis_dedup_skips_already_pending(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_syn_dedup")
    cid = await _make_concept(state)
    for i in range(3):
        await _make_claim(
            state, concept_id=cid, claim_type="empirical",
            text_suffix=f"e{i}",
        )

    first = await execute_tool(
        "curator_scan", state, scan_type="synthesis_candidates", auto_propose=True,
    )
    assert first["proposals_created"] >= 1

    second = await execute_tool(
        "curator_scan", state, scan_type="synthesis_candidates", auto_propose=True,
    )
    assert second["proposals_created"] == 0
    assert cid in second["skipped_already_pending"]


@pytest.mark.asyncio
async def test_auto_propose_false_returns_candidates_without_writing(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_dry_run")
    cid = await _make_concept(state)
    for i in range(3):
        await _make_claim(
            state, concept_id=cid, claim_type="empirical",
            text_suffix=f"e{i}",
        )
    good_method = await _make_claim(
        state, concept_id=cid, claim_type="methodological",
        status="validated", text_suffix="goodm",
    )
    _make_terminal(state)

    syn = await execute_tool(
        "curator_scan", state, scan_type="synthesis_candidates", auto_propose=False,
    )
    org = await execute_tool(
        "curator_scan", state, scan_type="org_promotion_candidates",
        auto_propose=False, drafts={good_method["id"]: _card()},
    )
    assert syn["proposals_created"] == 0
    assert org["proposals_created"] == 0
    assert any(c["concept_id"] == cid for c in syn["candidates"])
    assert any(c["source_id"] == good_method["id"] for c in org["candidates"])

    # 组织的待审里没进任何东西，kb_proposals.jsonl 也没写
    from core import kb_promotion as kp

    assert kp.review_queue() == []
    proposals_path = (state.project_root or state.root) / "kb_proposals.jsonl"
    if proposals_path.exists():
        lines = [json.loads(l) for l in proposals_path.read_text().splitlines() if l.strip()]
        assert not any(p["proposal_type"] == "kb_synthesis_candidate"
                        for p in lines)


# ─────────────────────────────────────────────────────────────────────────────
# v0.6 K3 fix: synthesis 候选只对科学 concept_type propose
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesis_skips_metric_concept_type(tmp_path):
    """v0.6 K3 fix：metric / tool / dataset / domain / person / group 不该
    被 propose synthesis（v3 dogfood 实测：27 候选里 19 是 metric/param noise）。"""
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_syn_filter")
    # 造 5 个不同 concept_type，每个有 3 条 claim anchor
    metric_cid = await _make_concept(state, name="UPC_effective", concept_type="metric")
    tool_cid = await _make_concept(state, name="LAMMPS_v6", concept_type="tool")
    dataset_cid = await _make_concept(state, name="QM9_v6", concept_type="dataset")
    method_cid = await _make_concept(state, name="MAP_Elites_v6", concept_type="method")
    phenom_cid = await _make_concept(state, name="mode_collapse_v6", concept_type="phenomenon")

    for cid in [metric_cid, tool_cid, dataset_cid, method_cid, phenom_cid]:
        for i in range(3):
            await _make_claim(
                state, concept_id=cid, claim_type="empirical",
                text_suffix=f"{cid[-6:]}_e{i}",
            )

    res = await execute_tool(
        "curator_scan", state, scan_type="synthesis_candidates", auto_propose=False,
    )
    cand_ids = {c["concept_id"] for c in res["candidates"]}
    # method + phenomenon 应该入候选
    assert method_cid in cand_ids
    assert phenom_cid in cand_ids
    # metric / tool / dataset 不该入候选
    assert metric_cid not in cand_ids
    assert tool_cid not in cand_ids
    assert dataset_cid not in cand_ids
    # skipped_by_concept_type 应该统计被过滤的
    skipped = res["skipped_by_concept_type"]
    assert skipped.get("metric", 0) == 1
    assert skipped.get("tool", 0) == 1
    assert skipped.get("dataset", 0) == 1
    # filter 字段应反映默认
    assert set(res["filters"]["eligible_concept_types"]) == {
        "phenomenon", "theory", "method", "task"
    }


@pytest.mark.asyncio
async def test_synthesis_eligible_concept_types_override(tmp_path):
    """显式传 eligible_concept_types 可扩白名单（如想包含 metric 临时跑一次）。"""
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_syn_override")
    metric_cid = await _make_concept(state, name="UPC_override", concept_type="metric")
    for i in range(3):
        await _make_claim(
            state, concept_id=metric_cid, claim_type="empirical",
            text_suffix=f"m_e{i}",
        )

    # 默认：metric 被过滤
    res_default = await execute_tool(
        "curator_scan", state, scan_type="synthesis_candidates", auto_propose=False,
    )
    assert metric_cid not in {c["concept_id"] for c in res_default["candidates"]}

    # 显式扩白名单含 metric：应该入候选
    res_ext = await execute_tool(
        "curator_scan", state, scan_type="synthesis_candidates", auto_propose=False,
        eligible_concept_types=["phenomenon", "theory", "method", "task", "metric"],
    )
    assert metric_cid in {c["concept_id"] for c in res_ext["candidates"]}


def test_local_card_fixture_covers_the_whole_contract():
    """夹具跟不上字段集时，让**这一条**炸，而不是六条看不出所以然的断言。

    改 KNOWLEDGE_CARD_FIELDS 时漏的总是别处手工造这个对象的地方：
    本次一天内撞两次（test_kb_promotion / test_auto_propose_slicers），
    症状都是"候选清单空了"，指向的假原因是扫盘坏了。
    """
    from core.kb_promotion import KNOWLEDGE_CARD_FIELDS

    missing = [f for f in KNOWLEDGE_CARD_FIELDS if f not in _card()]
    assert not missing, (
        f"本文件的 _card() 夹具缺 {missing} —— 知识卡契约加了字段，"
        f"这里没跟上。补上，别把断言改松。")

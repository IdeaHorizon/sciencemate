"""KB 经验召回 primitive + memory_onboarding hook。

测试覆盖：
  - recall() 返回 6 类 bucket 的结构正确
  - 各 bucket categorization 正确（dead_end → active_dead_ends, etc.）
  - HARNESS_DISABLE_SEMANTIC_DEDUP=1 时 graceful（无 embedding 不抛）
  - 空 KB graceful
  - directive / observation 不靠 embedding 也能 retrieve
  - render_briefing 输出干净
  - pre_run_briefing hook 仅 turn 1 触发；degrades when query empty
  - 所有 yaml 启用了正确 hook + tool
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.recall import (
    RecallResult, recall, render_briefing,
    RESEARCH_PRODUCT_ARTIFACT_TYPES,
)
from core.state import State


def _make_state(tmp_path: Path, project_id: str = "p_recall") -> State:
    return State.new(node_type="literature", base_dir=tmp_path,
                       project_id=project_id)


# ─────────────────────────────────────────────────────────────────────────────
# 1. recall primitive
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recall_empty_query_returns_empty(tmp_path):
    bootstrap()
    state = _make_state(tmp_path)
    res = recall(state, "")
    assert res.total_items() == 0
    res = recall(state, "   ")
    assert res.total_items() == 0


@pytest.mark.asyncio
async def test_recall_empty_kb_graceful(tmp_path):
    bootstrap()
    state = _make_state(tmp_path)
    # KB 完全空，调 recall 不该抛
    res = recall(state, "fitness function comparison")
    assert res.total_items() == 0   # 全空，但不抛


def test_try_embed_query_uses_correct_client_api(monkeypatch):
    """Regression：v1.0 dogfood 发现 recall.py 调了不存在的 `client.encode_batch`，
    被 `except Exception` 静默吞掉 → 所有 embedding-based bucket 永远是空。

    contract：`_try_embed_query` 必须返回一个非 None 的 unit-norm ndarray
    （EmbeddingClient API 是 `.embed(texts) → ndarray`）。

    CI 上没装 sentence-transformers（base deps only），这里 monkeypatch
    get_default_embedding_client 换成确定性 dummy —— regression 保护不减弱：
    recall.py 若再调错 API（如 encode_batch），dummy 同样没有那个方法，
    exception 路径返 None，测试照样红。真模型 smoke 见 test_embeddings_infra
    （HARNESS_RUN_REAL_EMBED=1 opt-in）。"""
    import numpy as np
    from core import embeddings as emb_mod
    from core.embeddings import EmbeddingClient

    class _HashEmbed(EmbeddingClient):
        """确定性 hash 假向量。故意**不**归一化 —— L2-normalize 是
        _try_embed_query 自己的契约，得由它来做。"""
        model_id = "dummy:hash-8"
        dim = 8

        def embed(self, texts):
            out = np.zeros((len(texts), self.dim), dtype=np.float32)
            for i, t in enumerate(texts):
                for ch in t:
                    out[i][ord(ch) % self.dim] += 1.0
            return out

    monkeypatch.delenv("HARNESS_DISABLE_SEMANTIC_DEDUP", raising=False)  # 强制走 embed 路径
    monkeypatch.setattr(emb_mod, "get_default_embedding_client", lambda: _HashEmbed())

    from core.recall import _try_embed_query
    vec = _try_embed_query("benchmark dataset accuracy comparison")
    assert vec is not None, (
        "_try_embed_query 返 None —— 可能 client API 又改了 / 调用方式不对。"
        "应直接调 client.embed([text])[0] 然后 L2-normalize。"
    )
    assert isinstance(vec, np.ndarray)
    assert vec.ndim == 1 and vec.shape[0] > 0
    # L2 normalized
    n = float(np.linalg.norm(vec))
    assert abs(n - 1.0) < 1e-5, f"vec not unit-norm: |v|={n}"


@pytest.mark.asyncio
async def test_render_briefing_empty(tmp_path):
    res = RecallResult()
    md = render_briefing(res, query="foo")
    assert "BRIEFING" in md
    assert "尚无相关历史" in md or "全空" in md


def _mem_state(tmp_path, wt, node_type="experiment"):
    from core import memory as M
    from core.state import State

    st = State.new(node_type=node_type, base_dir=tmp_path / "runs",
                   project_id="mem_hook", project_worktree=wt)
    M.ensure_skeleton(st)
    M.append_manual(st, text="声明检查通过前必须实际读 QC 结果 CANARY",
                    section=M.SECTION_PITFALL, nodes=["experiment"],
                    tools=["freeze_artifact"], run_id="r1")
    return st


def _fire(st, node_type="experiment", turn=1):
    from core.loader import load_harness
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import _memory_onboarding_on_turn_start

    h = load_harness(node_type)
    return _memory_onboarding_on_turn_start(
        HookContext(state=st, harness=h, turn=turn, messages=[]))


def test_onboarding_fires_with_no_query_at_all(tmp_path, mem_worktree):
    """裸调度（node_inputs 空、kb_query 空）也必须送达 —— 这是重建的全部意义。"""
    bootstrap()
    st = _mem_state(tmp_path, mem_worktree)
    st.hook_state["node_inputs"] = {}
    out = _fire(st)
    assert out is not None
    assert "CANARY" in (out[0].content or "")


def test_onboarding_only_fires_on_turn_1(tmp_path, mem_worktree):
    bootstrap()
    st = _mem_state(tmp_path, mem_worktree)
    assert _fire(st, turn=2) is None


def test_onboarding_matches_by_applies_to_not_by_relevance(tmp_path, mem_worktree):
    """机械匹配：适用面不含本节点、且工具面不相交 → 不送。

    另一半（"什么都送"）由上一条挡住；这一条挡"什么都不送"之外的另一个
    失败方式 —— 无差别广播。
    """
    bootstrap()
    st = _mem_state(tmp_path, mem_worktree, node_type="literature")
    out = _fire(st, node_type="literature")
    assert out is None, "literature 既不在 nodes 里、工具面也不含 freeze_artifact"


def test_onboarding_is_always_on_for_every_node():
    """记忆没有节点例外 —— 上一代 `_curator`/`_orchestrator` 因下划线前缀
    被排除在 producing 默认之外，于是调度与策展从来收不到任何记忆。"""
    from core.agent_loop import resolve_enabled_hook_names
    from core.loader import load_harness

    for n in ("literature", "hypothesis", "data", "experiment", "postprocess",
              "writing", "_curator", "_reviewer", "_orchestrator"):
        assert "memory_onboarding" in resolve_enabled_hook_names(load_harness(n)), n


def test_no_query_gate_survives_anywhere():
    """墓碑：query 门控。它的死因是同一字段两个消费方语义相反。"""
    import inspect

    from core import loop_hooks_builtin

    src = inspect.getsource(loop_hooks_builtin._memory_onboarding_on_turn_start)
    for banned in ("research_question", "kb_query"):
        assert banned not in src, f"送达又挂上了 {banned} 门控"


# ─────────────────────────────────────────────────────────────────────────────
# nodes/hypothesis v0.3 重构（commit 51950f0）丢了两个框架契约：
# memory_onboarding hook（取代 pre_run_briefing）。
#
# **前者已被结构性解决**：记忆送达改成 `memory_onboarding`，它在
# `_ALWAYS_ON_HOOKS` 里、节点关不掉 —— 记忆没有节点例外，缺口无从产生。
# 后者（主动检索工具）仍是 owner 侧缺口，继续 xfail 记账（strict=False：
# owner 补回后自动转 XPASS，不拦 CI）。
_HYPOTHESIS_CONTRACT_XFAIL = pytest.mark.xfail(
    reason="nodes/hypothesis v0.3 丢了主动检索工具契约（owner: nidy 待补回）",
    strict=False,
)


def test_executor_stashes_node_inputs_in_hook_state():
    """v1.0：executor 把 node_inputs 落 state.hook_state，hook 能拿到。"""
    # 不跑 full executor（太重），看代码层面的契约
    from core import executor
    src = Path(executor.__file__).read_text(encoding="utf-8")
    # 锚在**契约**上，不锚在某个 hook 的名字上 —— 名字会变，契约不变。
    assert 'state.hook_state["node_inputs"]' in src


# ─────────────────────────────────────────────────────────────────────────────
# 6. consolidated_notes（v3.3：MemoryV2 已 consolidate 的 topic 进 briefing）
# ─────────────────────────────────────────────────────────────────────────────

def _write_sibling_summary(base_dir, run_id, *, node_type, project_id,
                            status="incomplete", missing=None):
    import json
    d = base_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "run_id": run_id, "node_type": node_type, "project_id": project_id,
        "status": status,
        "missing_required_outputs": missing or [],
    }), encoding="utf-8")
    return d


def test_prior_failed_checks_surfaces_last_same_node_failure(tmp_path):
    """同 node_type + 同 project 的最近一次"没交出必需产物"应进 briefing。"""
    _write_sibling_summary(
        tmp_path, "1700000000-old111", node_type="writing", project_id="p_pf",
        missing=["manuscript"],
    )
    state = State.new(node_type="writing", base_dir=tmp_path, project_id="p_pf")
    res = recall(state, "write the manuscript")
    assert len(res.prior_failed_checks) == 1
    assert res.prior_failed_checks[0]["missing_required_outputs"] == ["manuscript"]
    md = render_briefing(res, query="write the manuscript")
    assert "manuscript" in md


def test_prior_failed_checks_ignores_other_node_type(tmp_path):
    """不同 node_type 的失败 run 不应污染当前 node 的 briefing。"""
    _write_sibling_summary(
        tmp_path, "1700000000-exp111", node_type="experiment", project_id="p_pf2",
        missing=["dataset"],
    )
    state = State.new(node_type="writing", base_dir=tmp_path, project_id="p_pf2")
    res = recall(state, "write the manuscript")
    assert res.prior_failed_checks == []


def test_prior_failed_checks_ignores_completed_runs(tmp_path):
    """completed（无 missing output）的兄弟 run 不进 briefing。"""
    _write_sibling_summary(
        tmp_path, "1700000000-ok1111", node_type="writing", project_id="p_pf3",
        status="completed",
    )
    state = State.new(node_type="writing", base_dir=tmp_path, project_id="p_pf3")
    res = recall(state, "write the manuscript")
    assert res.prior_failed_checks == []


# ─────────────────────────────────────────────────────────────────────────────
# 8. v3.3 低垂果实：v2 observation 双读 / dedup / prior_runs 停用
# ─────────────────────────────────────────────────────────────────────────────

def test_ensure_index_warm_graceful_when_embeddings_disabled(tmp_path):
    """HARNESS_DISABLE_SEMANTIC_DEDUP=1（conftest 默认）时 _ensure_index_warm
    应是无害 no-op，recall 不因它抛错。"""
    from core.recall import _ensure_index_warm
    state = _make_state(tmp_path, project_id="p_warm")
    _ensure_index_warm(state)   # 不该抛
    res = recall(state, "anything")
    assert isinstance(res, RecallResult)


# ── 墓碑：为已删召回桶与 recall_experience 写的测试 ─────────────────────────
#
# 删掉的桶：pending_questions（判据引用 schema 里不存在的 claim_type="question"，
# 从 v3 合并起恒空却每次照常打印"0 条"）、user_directives / recent_observations /
# consolidated_notes（项目记忆，已归 core.memory_delivery 按适用面机械送达）。
#
# `recall_experience` 并进 `memory_recall`：主动路曾是被动路的**真子集**
# （_VALID_CATEGORIES 只有老六类，拿不到最可行动的三类），模型不查它是理性的。
#
# consolidated_notes 那三条尤其值得记：它们**一直是绿的**，而被测的东西
# 读 `memory/*.md`、curator 却写 `worktree/MEMORY.md` 分节 —— 写在 A 读在 B。
# 那个"把踩坑经验摆进 briefing"的补丁，自己变成了另一个不可见。


# ─────────────────────────────────────────────────────────────────────────────
# 年龄头 + 验证义务（抄 CC 的召回防幻觉，harness survey G8）
# ─────────────────────────────────────────────────────────────────────────────


def _iso_days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _briefing_line(md: str, needle: str) -> str:
    hits = [ln for ln in md.splitlines() if needle in ln]
    assert hits, f"briefing 里找不到含 {needle!r} 的行：\n{md}"
    return hits[0]


def test_briefing_age_renders_on_the_claim_line(tmp_path):
    """年龄必须落在**那条 claim 自己的行**上 —— 断言尾注文案还在证明不了这点。"""
    res = RecallResult()
    res.active_dead_ends.append({
        "claim_id": "claim_old", "claim_text": "X 方向走不通",
        "dont_repeat_reason": "", "scope": "org", "similarity": 0.9,
        "created_at": _iso_days_ago(47),
    })
    res.validated_methods.append({
        "claim_id": "claim_fresh", "claim_text": "用 Y 法收敛",
        "claim_type": "methodological", "confidence": 0.8,
        "replication_count": 2, "scope": "org", "similarity": 0.8,
        "created_at": _iso_days_ago(0),
    })
    md = render_briefing(res, query="q")
    assert "47 天前" in _briefing_line(md, "claim_old")
    assert "今天" in _briefing_line(md, "claim_fresh")


def test_briefing_age_absent_when_timestamp_missing_or_bad(tmp_path):
    """缺 created_at / 格式坏 → 那一行不标年龄（错的年龄比没有年龄更糟）。"""
    import re

    res = RecallResult()
    res.active_dead_ends.append({
        "claim_id": "claim_no_ts", "claim_text": "无时间戳",
        "dont_repeat_reason": "", "scope": "org", "similarity": 0.5,
    })
    res.validated_methods.append({
        "claim_id": "claim_bad_ts", "claim_text": "坏时间戳",
        "claim_type": "methodological", "confidence": 0.7,
        "replication_count": 1, "scope": "org", "similarity": 0.5,
        "created_at": "not-a-date",
    })
    md = render_briefing(res, query="q")
    assert not re.search(r"\d+ 天前", _briefing_line(md, "claim_no_ts"))
    assert not re.search(r"\d+ 天前", _briefing_line(md, "claim_bad_ts"))


def test_briefing_capital_carries_drafted_age(tmp_path):
    res = RecallResult()
    res.load_bearing_capital.append({
        "claim_id": "claim_cap", "claim_text": "承重结论",
        "decision_relevance": "", "why": "", "domain": None,
        "claim_type": "empirical", "status": "validated", "confidence": 0.9,
        "scope_dimensions": {}, "drafted_at": _iso_days_ago(12),
    })
    md = render_briefing(res, query="q")
    assert "12 天前" in _briefing_line(md, "claim_cap")


def test_briefing_verification_duty_only_when_items_exist(tmp_path):
    """验证义务跟着内容走：有召回才有义务；空 briefing 不背这段。"""
    res = RecallResult()
    res.active_dead_ends.append({
        "claim_id": "c1", "claim_text": "t", "dont_repeat_reason": "",
        "scope": "org", "similarity": 0.5, "created_at": _iso_days_ago(3),
    })
    md = render_briefing(res, query="q")
    assert "不是现状" in md

    empty = render_briefing(RecallResult(), query="q")
    assert "不是现状" not in empty


def test_age_label_edges():
    from core.recall import _age_label

    assert _age_label(None) is None
    assert _age_label("garbage") is None
    assert _age_label(_iso_days_ago(-2)) is None      # 未来时间：时钟漂移，不猜
    assert _age_label(_iso_days_ago(1)) == "1 天前"


def test_fill_claim_buckets_carries_created_at(monkeypatch, tmp_path):
    """接线测试：render 读的 created_at 必须是 _fill_claim_buckets 装进去的。

    上面的渲染测试全是手工构造 RecallResult —— 填充层漏装字段时它们照样绿。
    """
    from core import kb_vector_index
    from core.recall import _fill_claim_buckets

    ts = _iso_days_ago(9)
    records = {
        "k_dead": {"claim_type": "dead_end", "status": "validated",
                    "claim_text": "别走", "created_at": ts},
        "k_meth": {"claim_type": "methodological", "status": "validated",
                    "claim_text": "这么走", "replication_count": 1,
                    "confidence": 0.9, "created_at": ts},
    }

    monkeypatch.setattr(
        kb_vector_index, "query_across_scopes",
        lambda *a, **kw: [("k_dead", 0.9, "org"), ("k_meth", 0.8, "org")])

    class _St:
        project_id = "p1"

        def get_kb_record(self, entity, kid):
            return records.get(kid)

    res = RecallResult()
    _fill_claim_buckets(_St(), object(), res, k_per_category=5,
                        min_cosine=0.1,
                        include_categories=("active_dead_ends",
                                            "validated_methods"))
    assert res.active_dead_ends[0]["created_at"] == ts
    assert res.validated_methods[0]["created_at"] == ts
    # 一路到渲染：真填充产物上必须带年龄
    md = render_briefing(res, query="q")
    assert "9 天前" in _briefing_line(md, "k_dead")

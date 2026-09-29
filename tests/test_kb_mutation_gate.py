"""v3.2 KB 变更端加固回归测试（2026-07 KB 审计 Bug#1 + Bug#2）。

原则：概率性操作（embedding 近似 / 身份判断）不能对 KB 直接执行 mutation。
"""
from __future__ import annotations

from core.state import State


# ── Bug#1：person/group 禁语义自动合并 ──────────────────────────────────────

def test_person_concept_never_auto_merges(tmp_path):
    """同论文不同作者描述相似，但不能被当 aliases 合并。"""
    st = State.new(node_type="literature", base_dir=tmp_path, project_id="p1")
    whitmore = {
        "canonical_name": "Lindsey M. Whitmore", "concept_type": "person",
        "description": "Researcher on LJ cutoff treatment in alchemical free energy.",
    }
    ramezani = {
        "canonical_name": "Yalda Ramezani", "concept_type": "person",
        "description": "Researcher on LJ cutoff treatment in alchemical free energy.",
    }
    # 即便 target/new 描述几乎相同，_safe_to_auto_merge 对 person 必须返 False
    assert st._safe_to_auto_merge("concepts", whitmore, ramezani) is False
    assert st._safe_to_auto_merge("concepts",
                                  {**whitmore, "concept_type": "group"},
                                  {**ramezani, "concept_type": "group"}) is False


def test_non_identity_concept_can_still_merge(tmp_path):
    """method / theory 等非身份类，同 type 仍允许 auto-merge（不误伤原机制）。"""
    st = State.new(node_type="literature", base_dir=tmp_path, project_id="p1")
    a = {"canonical_name": "RDF", "concept_type": "method", "description": "x"}
    b = {"canonical_name": "radial distribution function",
         "concept_type": "method", "description": "x"}
    assert st._safe_to_auto_merge("concepts", a, b) is True
    # concept_type 不同仍拒
    assert st._safe_to_auto_merge(
        "concepts", a, {**b, "concept_type": "dataset"}) is False


# ── Bug#2：内网/伪造 host 不算外部来源 ──────────────────────────────────────

def test_fake_internal_host_not_external():
    from shared.lib.kb_schema import is_external_uri, smart_default_scope
    # 伪造的内部 host —— 曾被用来把实验数据塞进 org scope
    assert is_external_uri("https://project-internal/LJ/clean_results_123") is False
    assert is_external_uri("https://harness.internal/artifacts/survey") is False
    assert is_external_uri("https://internal/run/xxx/experiment_log") is False
    assert is_external_uri("http://localhost:8080/foo") is False
    assert is_external_uri("https://192.168.1.5/data") is False
    # 真外部来源仍然是外部
    assert is_external_uri("https://arxiv.org/abs/1910.05746") is True
    assert is_external_uri("doi:10.1063/1.5053714") is True
    assert is_external_uri("arxiv:1910.05746") is True

    # 锚点判据的真正用途已收敛到**准入**（kb_ingest 拒绝无外部锚的来源），
    # 不再兼任 scope 路由 —— 出生一律 project，有锚无锚都一样。
    # 伪内部 host 的危害因此从"混进 org 共享层"降级为"混进本项目 KB"，
    # 而后者仍由这道锚点闸拦住。
    fake = {"source": "https://project-internal/LJ/clean_results", "text": "..."}
    real = {"source": "arxiv:1910.05746", "text": "..."}
    assert smart_default_scope("chunks", fake) == "project"
    assert smart_default_scope("chunks", real) == "project"


def test_kb_ingest_rejects_fake_internal_source(tmp_path):
    """kb_ingest 对伪造 internal host 报错，逼 LLM 走 register_artifact_as_chunk。"""
    import asyncio
    from core.bootstrap import bootstrap
    from core.tool_registry import execute
    bootstrap()
    st = State.new(node_type="experiment", base_dir=tmp_path, project_id="p2")
    res = asyncio.run(execute(
        "kb_ingest", st,
        text="# LJ Cutoff Results\nP*=1.15 at rc=4.0",
        source="https://project-internal/LJ_cutoff_analysis/clean_results_1783347111",
    ))
    assert res.get("status") == "error"
    assert "外部 URI" in res.get("error", "") or "register_artifact_as_chunk" in res.get("error", "")

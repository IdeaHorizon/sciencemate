"""引用反查工具的回归 —— KB 里有真标识符，别再拿散文当参考文献。

E2E-3 审稿：论文 9 条参考文献全是 `eprint = {arXiv preprint}` 占位，无一可核对。
但标识符从来没丢 —— org 层 KB 里 8 条 chunk 全带真 arXiv ID。缺的是"去查"这一步。
"""
from __future__ import annotations

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute

bootstrap()


def _kb(tmp_path):
    st = State.new(node_type="writing", base_dir=tmp_path / "runs", project_id="p_cite")
    chunk, _ = st.write_kb("chunks", {
        "text": "LLM cascades and model routing promise lower inference cost.",
        "source": "arxiv:2605.18796", "scope": "org", "promoted_from": {"project_id": "p_prev", "source_id": "src_prev", "approved_by": "test", "at": "2026-08-21T00:00:00Z"}})
    internal, _ = st.write_kb("chunks", {
        "text": "本项目 survey 正文片段", "scope": "project",
        "source": "artifact:survey_report__x"})
    claim, _ = st.write_kb("claims", {
        "claim_text": "Query-level routing achieves 30-60% cost reduction.",
        "claim_type": "empirical", "concept_ids": ["c1"],
        "confidence": 0.7, "sources": [chunk["id"]], "scope": "project"})
    return st, chunk, internal, claim


@pytest.mark.asyncio
async def test_resolves_chunk_and_claim_to_external_ids(tmp_path):
    st, chunk, _internal, claim = _kb(tmp_path)
    res = await execute("resolve_citations", st, ids=[chunk["id"], claim["id"]])
    assert res["status"] == "success"
    by_id = {r["id"]: r for r in res["resolved"]}
    assert by_id[chunk["id"]]["external"] == ["arxiv:2605.18796"]
    # claim 沿 sources 递归解析到 chunk
    assert by_id[claim["id"]]["external"] == ["arxiv:2605.18796"]
    assert by_id[claim["id"]]["kind"] == "claim"


@pytest.mark.asyncio
async def test_bibtex_hint_is_field_specific(tmp_path):
    """别让每个节点自己猜 arxiv/doi 该填哪个字段。"""
    st, chunk, _i, _c = _kb(tmp_path)
    doi_chunk, _ = st.write_kb("chunks", {
        "text": "A survey of LLMs.", "source": "doi:10.1007/s11704-026-60308-3",
        "scope": "org", "promoted_from": {"project_id": "p_prev", "source_id": "src_prev", "approved_by": "test", "at": "2026-08-21T00:00:00Z"}})
    res = await execute("resolve_citations", st, ids=[chunk["id"], doi_chunk["id"]])
    hints = {r["id"]: r["bibtex"][0] for r in res["resolved"]}
    assert hints[chunk["id"]]["bibtex_field"] == "eprint"
    assert hints[chunk["id"]]["value"] == "2605.18796"
    assert hints[chunk["id"]]["extra"]["archivePrefix"] == "arXiv"
    assert hints[doi_chunk["id"]]["bibtex_field"] == "doi"
    assert hints[doi_chunk["id"]]["value"] == "10.1007/s11704-026-60308-3"


@pytest.mark.asyncio
async def test_internal_sources_are_reported_not_faked(tmp_path):
    """source 是内部 artifact 的，明确报 unresolved —— 不许糊一个占位条目。"""
    st, _c, internal, _cl = _kb(tmp_path)
    res = await execute("resolve_citations", st, ids=[internal["id"], "chunk_nope"])
    assert res["resolved"] == []
    ids = {r["id"] for r in res["unresolved"]}
    assert ids == {internal["id"], "chunk_nope"}
    assert "不可外部引用" in next(
        r["note"] for r in res["unresolved"] if r["id"] == internal["id"])
    assert "不要编" in res["note"]


@pytest.mark.asyncio
async def test_org_scope_chunks_are_visible(tmp_path):
    """论文类知识落 org 是对的 —— 反查必须跨 scope，否则永远查不到。

    第一次排查时我只看了 project 层的 kb_chunks.jsonl 就断言"标识符丢光了"，
    实际 8 条带真 arXiv ID 的 chunk 全在 org 层。
    """
    st, chunk, _i, _c = _kb(tmp_path)
    assert st.get_kb_record("chunks", chunk["id"])["scope"] == "org"
    res = await execute("resolve_citations", st, ids=[chunk["id"]])
    assert res["resolved"][0]["external"] == ["arxiv:2605.18796"]


@pytest.mark.asyncio
async def test_bad_input_is_rejected(tmp_path):
    st, *_ = _kb(tmp_path)
    assert (await execute("resolve_citations", st, ids=[]))["status"] == "error"
    assert (await execute("resolve_citations", st,
                          ids=[f"chunk_{i}" for i in range(101)]))["status"] == "error"


def test_resolve_citations_is_reachable_from_producing_nodes():
    """E2E-4：工具接到了 _reviewer/_curator，但**写引用的是 writing** ——
    三个 writing run 一次都没调过它，bib 照旧全是无 ID 占位条目。

    工具在框架里、写引用的节点够不着 = 等于没做。
    """
    from core.loader import _ALWAYS_ON_TOOLS, _with_always_on_tools

    assert "resolve_citations" in _ALWAYS_ON_TOOLS
    for nt in ("writing", "literature", "experiment"):
        assert "resolve_citations" in _with_always_on_tools(["save_artifact"], nt), nt
    # 系统节点走各自 yaml 显式声明（_reviewer/_curator 已有），不重复注入
    assert "resolve_citations" not in _with_always_on_tools(["save_artifact"], "_curator")

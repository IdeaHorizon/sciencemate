"""The targeted deliverable reconciles DOI metadata without trusting preprint years."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nodes.literature.tools import finalize_evidence_package as package


@pytest.mark.asyncio
async def test_official_journal_year_replaces_preprint_year(monkeypatch):
    async def official(_client, doi):
        assert doi == "10.3847/1538-4357/aca6e3"
        return {
            "title": ["A Standard Siren Measurement of the Hubble Constant Using Gravitational-wave Events"],
            "published-print": {"date-parts": [[2023, 1]]},
            "published-online": {"date-parts": [[2022, 12]]},
        }

    monkeypatch.setattr(package, "_fetch_crossref_record", official)
    papers = [{
        "title": "A Standard Siren Measurement of the Hubble Constant Using Gravitational-wave Events",
        "doi": "10.3847/1538-4357/aca6e3", "year": 2021,
    }]
    counts = await package._audit_doi_metadata(papers)
    assert counts["corrected_year"] == 1
    assert papers[0]["year"] == 2023
    assert papers[0]["metadata_verification"]["original_year"] == 2021


@pytest.mark.asyncio
async def test_title_mismatch_is_disclosed_without_changing_year(monkeypatch):
    async def official(_client, _doi):
        return {"title": ["A completely unrelated paper"],
                "published-print": {"date-parts": [[2024]]}}

    monkeypatch.setattr(package, "_fetch_crossref_record", official)
    papers = [{"title": "Gravitational-wave standard sirens", "doi": "10.1000/wrong", "year": 2021}]
    counts = await package._audit_doi_metadata(papers)
    assert counts["title_mismatch"] == 1
    assert papers[0]["year"] == 2021
    assert papers[0]["metadata_verification"]["official_title"] == "A completely unrelated paper"


@pytest.mark.asyncio
async def test_timeout_does_not_block_or_claim_verification(monkeypatch):
    async def stalled(_client, _doi):
        await asyncio.sleep(1)

    monkeypatch.setattr(package, "_fetch_crossref_record", stalled)
    monkeypatch.setattr(package, "_DOI_AUDIT_MAX_SECONDS", 0.01)
    papers = [{"title": "A paper", "doi": "10.1000/slow", "year": 2021}]
    counts = await package._audit_doi_metadata(papers)
    assert counts["unverified"] == 1
    assert papers[0]["metadata_verification"]["status"] == "unverified"


@pytest.mark.asyncio
async def test_finalizer_saves_corrected_year_and_audit(monkeypatch):
    async def official(_client, _doi):
        return {"title": ["A standard siren measurement"],
                "published-print": {"date-parts": [[2023]]}}

    monkeypatch.setattr(package, "_fetch_crossref_record", official)
    saved = {}

    def save_artifact(**kwargs):
        saved.update(kwargs)
        return {"id": "literature_evidence_package__sample"}

    state = SimpleNamespace(hook_state={"_request_mode": "targeted_lookup"},
                            save_artifact=save_artifact)
    result = await package._finalize_evidence_package(
        state, name="sample", queries_json='["standard siren"]',
        included_papers_json=json.dumps([{
            "title": "A standard siren measurement", "doi": "10.1000/sample", "year": 2021,
        }]),
        evidence_summary="One relevant paper.",
    )
    content = json.loads(saved["content"])
    assert result["status"] == "success"
    assert content["included_papers"][0]["year"] == 2023
    assert content["metadata_audit"]["corrected_year"] == 1


def test_targeted_prompt_requires_gap_driven_coverage_without_domain_dictionary():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "harness.yaml").read_text()
    assert "先从**用户原问题**提取必须覆盖的证据类型" in text
    assert "针对**缺失类型**最多补查 2 条" in text
    assert "不得凭模型记忆添加工具未返回的 DOI" in text
    assert "crossref_query_mode=\"title\"" in text
    assert "不能把同一事件或同一数据集的多篇再分析" in text
    assert "多事件汇总、合作组数据发布或后续综合研究" in text


@pytest.mark.asyncio
async def test_unanchored_paper_is_excluded_from_evidence_package():
    saved = {}

    def save_artifact(**kwargs):
        saved.update(kwargs)
        return {"id": "literature_evidence_package__sample"}

    state = SimpleNamespace(hook_state={"_request_mode": "targeted_lookup"},
                            save_artifact=save_artifact)
    result = await package._finalize_evidence_package(
        state, name="sample", queries_json='["standard siren"]',
        included_papers_json='[{"title":"No anchor", "year":2020}]',
        evidence_summary="One paper was returned.",
    )
    content = json.loads(saved["content"])
    assert result["included_count"] == 0
    assert content["excluded_papers"][0]["reason"] == "missing_doi_or_stable_url"
    assert any("稳定原文链接" in note for note in content["advisories"])


def test_arxiv_id_provides_stable_original_link():
    paper = {"title": "A preprint", "arxiv_id": "1307.2638"}
    assert package._has_stable_identifier(paper)
    assert paper["url"] == "https://arxiv.org/abs/1307.2638"

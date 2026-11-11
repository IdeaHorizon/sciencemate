"""Network facts must survive the real bibliography write/read/render path."""
import asyncio
import json

import httpx
import pytest

from nodes.writing.tests.test_w_rebuild import _pages, _state
from nodes.writing.tools import w_bib
from nodes.writing.tools.w_common import fragment_problems
from nodes.writing.tools.w_render import hard_lines


@pytest.fixture
def network(monkeypatch):
    client = httpx.AsyncClient

    def use(handler):
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(
            **kw, transport=httpx.MockTransport(handler)))
    return use


def test_http500_is_unknown_and_draft_is_preserved(tmp_path, network):
    st = _state(tmp_path)
    network(lambda req: httpx.Response(500))
    result = asyncio.run(w_bib._write_bibliography(st,
        '@misc{outage, title={Service report}, url={https://example.org/report}}'))
    assert result["status"] == "success_with_issues"
    assert result["unchecked"] == ["outage"] and result["bad"] == []
    restored = asyncio.run(w_bib._read_bibliography(st))
    assert restored["entries"][0]["check"]["status"] == "unavailable"
    mdir = st.workspace_root / "manuscript"
    assert "outage" in (mdir / "refs.bib").read_text()
    (mdir / "sections" / "introduction.tex").write_text(r"\cite{outage}")
    for p in (mdir / "sections").glob("*.tex"):
        if p.stem != "introduction":
            p.unlink()
    h5 = hard_lines(st, _pages(st), "")["H5_bibliography"]
    assert h5["passed"] and h5["unchecked_cited"] == ["outage"]


def test_real_doi_with_wrong_title_is_mismatch(tmp_path, network):
    st = _state(tmp_path)
    network(lambda req: httpx.Response(200, json={
        "DOI": "10.5555/real", "title": "Actual research paper",
        "author": [{"given": "Ada", "family": "Lovelace"}]}))
    result = asyncio.run(w_bib._write_bibliography(st,
        '@article{wrong, title={{Invented} paper}, author={Lovelace, Ada}, doi={10.5555/real}}'))
    assert result["bad"] == ["wrong"]
    assert result["entries"][0]["check"]["status"] == "mismatch"


def test_nested_title_is_not_truncated():
    assert w_bib._fields('title={{ASTRA-SIM}: A simulator}, year={2020}')["title"] == "{ASTRA-SIM}: A simulator"


def test_agent_research_terms_do_not_fail_fragment_or_fixture(tmp_path):
    text = "The orchestrator adapts each artifact in the agent harness. 调度器与审计是研究对象。"
    assert fragment_problems(text) == []
    st = _state(tmp_path)
    pages = _pages(st)
    pages[1] += "\n" + text
    facts = hard_lines(st, pages, "")
    assert all(v["passed"] for k, v in facts.items() if k.startswith("H2"))
    # A concrete unresolved reference remains observable, regardless of vocabulary.
    pages[1] += "\n参见图 ??。"
    assert not hard_lines(st, pages, "")["H3_references_resolved"]["passed"]


@pytest.mark.parametrize("code,expected", [(200, "resolved"), (404, "invalid"), (410, "invalid"),
                                         (401, "unavailable"), (403, "unavailable"),
                                         (429, "unavailable"), (503, "unavailable")])
def test_network_facts_keep_absent_and_unavailable_distinct(network, code, expected):
    from nodes.literature.tools.reference_metadata import resolve_reference
    network(lambda req: httpx.Response(code))
    fact = asyncio.run(resolve_reference("url", "https://example.org/paper", {}))
    assert fact["status"] == expected


def test_timeout_can_be_rechecked_without_losing_draft(tmp_path, network):
    st = _state(tmp_path)
    bib = '@misc{retry, title={A report}, url={https://example.org/paper}}'

    def timeout(req):
        raise httpx.ReadTimeout("offline", request=req)

    network(timeout)
    first = asyncio.run(w_bib._write_bibliography(st, bib))
    assert first["unchecked"] == ["retry"] and first["bad"] == []
    network(lambda req: httpx.Response(200))
    second = asyncio.run(w_bib._write_bibliography(st, bib))
    assert second["status"] == "success" and second["unchecked"] == []


def test_legitimate_nested_title_and_author_initials_match(tmp_path, network):
    st = _state(tmp_path)
    network(lambda req: httpx.Response(200, json={"DOI": "10.5555/real",
        "title": "ASTRA-SIM: A simulator", "author": [{"given": "Ada", "family": "Lovelace"}]}))
    result = asyncio.run(w_bib._write_bibliography(st,
        '@article{valid, title={{ASTRA-SIM}: A simulator}, author={A. Lovelace}, doi={10.5555/real}}'))
    assert result["status"] == "success"
    assert result["entries"][0]["check"]["comparisons"] == {"title": "match", "author": "match"}


def test_author_mismatch_is_not_hidden_by_real_title(tmp_path, network):
    st = _state(tmp_path)
    network(lambda req: httpx.Response(200, json={"title": "A real title",
        "author": [{"given": "Ada", "family": "Lovelace"}]}))
    result = asyncio.run(w_bib._write_bibliography(st,
        '@article{wrong, title={A real title}, author={Other, Person}, doi={10.5555/real}}'))
    assert result["bad"] == ["wrong"]
    assert result["entries"][0]["check"]["comparisons"]["author"] == "mismatch"


def test_legacy_outage_is_derived_from_observation_without_rewriting(tmp_path):
    st = _state(tmp_path)
    mdir = st.workspace_root / "manuscript"
    (mdir / "refs.bib").write_text('@misc{legacy, title={Report}, url={https://example.org}}')
    path = mdir / "bib_check.json"
    old = json.dumps({"entries": [{"key": "legacy", "check": "http 500"}], "bad": [], "duplicates": []})
    path.write_text(old)
    result = asyncio.run(w_bib._read_bibliography(st))
    assert result["unchecked"] == ["legacy"] and result["bad"] == []
    (mdir / "sections" / "introduction.tex").write_text(r"\cite{legacy}")
    assert hard_lines(st, _pages(st), "")["H5_bibliography"]["unchecked_cited"] == ["legacy"]
    assert path.read_text() == old


def test_legacy_resolved_is_not_upgraded_to_metadata_verification():
    old = {"entries": [{"key": "old", "check": "resolved"}]}
    fact = w_bib.bibliography_facts(old)[0]["check"]
    assert fact == {"status": "resolved", "scope": "identifier", "legacy_observation": "resolved"}


@pytest.mark.parametrize("body,expected", [
    ("not XML", "unavailable"),
    ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2401.12345v2</id>'
     '<title>Real paper</title><author><name>Ada Lovelace</name></author></entry></feed>', "resolved"),
])
def test_arxiv_uses_literature_parser(network, body, expected):
    from nodes.literature.tools.reference_metadata import resolve_reference
    network(lambda req: httpx.Response(200, text=body))
    result = asyncio.run(resolve_reference("arxiv", "2401.12345", {"title": "Real paper"}))
    assert result["status"] == expected


@pytest.mark.parametrize("author,canonical", [
    ("A. Lovelace and others", [{"family": "Lovelace"}, {"family": "Babbage"}]),
    ("{The Research and Development Group}", [{"literal": "The Research and Development Group"}]),
])
def test_partial_and_corporate_authors_are_not_rejected(tmp_path, network, author, canonical):
    st = _state(tmp_path)
    network(lambda req: httpx.Response(200, json={"title": "A real title", "author": canonical}))
    result = asyncio.run(w_bib._write_bibliography(st,
        '@article{valid, title={A real title}, author={' + author + '}, doi={10.5555/real}}'))
    assert result["bad"] == []


def test_close_title_variant_is_unknown_not_false_mismatch(tmp_path, network):
    st = _state(tmp_path)
    network(lambda req: httpx.Response(200, json={"title": "A simulator for distributed agent systems"}))
    result = asyncio.run(w_bib._write_bibliography(st,
        '@article{variant, title={A simulator for distributed agent system}, doi={10.5555/real}}'))
    assert result["bad"] == [] and result["unchecked"] == ["variant"]


def test_review_contract_does_not_reintroduce_vocabulary_verdict():
    from pathlib import Path
    spec = (Path(__file__).resolve().parents[1] / "review_spec.md").read_text()
    assert "这类过程话 → major" not in spec


def test_bibliography_without_check_file_remains_unknown(tmp_path):
    st = _state(tmp_path)
    mdir = st.workspace_root / "manuscript"
    (mdir / "bib_check.json").unlink()
    (mdir / "refs.bib").write_text('@misc{imported, title={Imported report}, url={https://example.org}}')
    result = asyncio.run(w_bib._read_bibliography(st))
    assert result["unchecked"] == ["imported"]


def test_malformed_url_does_not_discard_bibliography(tmp_path):
    st = _state(tmp_path)
    result = asyncio.run(w_bib._write_bibliography(st,
        '@misc{invalid, title={Bad URL}, url={http://[invalid}}'))
    assert result["bad"] == ["invalid"]


def test_referee_receives_unknown_facts_not_only_passed_boolean(tmp_path, monkeypatch, network):
    from nodes.writing.tools import w_referee
    st = _state(tmp_path)
    network(lambda req: httpx.Response(500))
    asyncio.run(w_bib._write_bibliography(st,
        '@misc{outage, title={A report}, url={https://example.org/paper}}'))
    (st.workspace_root / "render_report.json").write_text(json.dumps({
        "status": "success", "hard_lines": {"passed": True}}))
    seen = []

    async def referee(*args, **kwargs):
        seen.append(kwargs["user"])
        return '{"verdict":"minor", "comments":[]}'

    monkeypatch.setattr(w_referee, "bounded_llm", referee)
    assert asyncio.run(w_referee._referee_review(st))["status"] == "success"
    assert '"status": "unavailable"' in seen[0] and "outage" in seen[0]

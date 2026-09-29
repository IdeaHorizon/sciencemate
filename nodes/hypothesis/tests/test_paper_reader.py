"""hypothesis 节点 read_reference_paper 工具单测。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.bootstrap import bootstrap
from core.state import State
from nodes.hypothesis.tools.paper_reader import (
    doi_to_fixture_filename,
    resolve_paper_pdf_path,
)
from nodes.hypothesis.tools.conclusion_audit import _audit_hypothesis_vs_conclusions


DOI = "10.1039/D2TA00652A"
PDF_NAME = "doi.org10.1039D2TA00652A.pdf"
FIXTURES = ROOT / "nodes" / "hypothesis" / "fixtures"

# issue #138：下面 3 条测试要真读一篇**已发表的受版权保护论文** PDF
# （RSC J. Mater. Chem. A, DOI 10.1039/D2TA00652A）。这份 PDF 不在仓库里、
# 也**不能**提交（版权），所以干净 checkout / CI 上它必然缺席。
# 以前它们缺文件就硬 fail → CI 常态红 → 谁都分不清自己有没有引入真回归
# （lujy 在 PR #150 里就把别的失败误判成"main 既有问题"）。
# 改成缺 fixture 就 skip：有这篇 PDF 的机器（节点 owner 本地）照常跑全量断言，
# 没有的机器如实报 skip，不再污染基线。
_PDF_PRESENT = (FIXTURES / PDF_NAME).is_file()
_needs_paper_pdf = pytest.mark.skipif(
    not _PDF_PRESENT,
    reason=(f"缺 fixture {PDF_NAME}（受版权保护的已发表论文，不入库）；"
            f"把该 PDF 放到 {FIXTURES} 即可跑这几条"),
)


@pytest.fixture(scope="module", autouse=True)
def _boot():
    bootstrap(force=True)


def test_doi_to_fixture_filename():
    assert doi_to_fixture_filename("10.1039/D2TA00652A") == PDF_NAME
    assert doi_to_fixture_filename("doi:10.1039/D2TA00652A") == PDF_NAME
    assert doi_to_fixture_filename("https://doi.org/10.1039/D2TA00652A") == PDF_NAME


@_needs_paper_pdf
def test_resolve_paper_pdf_path():
    path = resolve_paper_pdf_path(DOI, search_dirs=[FIXTURES])
    assert path is not None
    assert path.name == PDF_NAME


@_needs_paper_pdf
def test_read_reference_paper_extracts_oC46_text(tmp_path):
    from nodes.hypothesis.tools.paper_reader import _read_reference_paper

    state = State.new(node_type="hypothesis", base_dir=tmp_path)
    state.hook_state["node_inputs"] = {
        "reference_papers": [{"doi": DOI, "pdf_fixture": PDF_NAME}],
    }

    result = asyncio.run(_read_reference_paper(
        state, doi=DOI, section="abstract", page_limit=3, max_chars=8000,
    ))
    assert result["status"] == "success"
    assert "oC46" in result["content"]
    assert "303" in result["content"] or "mA h" in result["content"]
    assert DOI in state.hook_state["reference_paper_texts"]


@_needs_paper_pdf
def test_audit_flags_paper_conclusion_restatement(tmp_path):
    from nodes.hypothesis.tools.paper_reader import _read_reference_paper

    state = State.new(node_type="hypothesis", base_dir=tmp_path)
    state.hook_state["node_inputs"] = {
        "reference_papers": [{"doi": DOI, "pdf_fixture": PDF_NAME}],
    }
    asyncio.run(_read_reference_paper(state, doi=DOI, section="full", page_limit=10))

    result = asyncio.run(_audit_hypothesis_vs_conclusions(
        state,
        hypotheses=[{
            "label": "H1",
            "claim_text": (
                "oC46 作为钠离子电池负极具有 303 mAh g-1 可逆容量、"
                "0.05 eV 扩散势垒、0.43 V 平均电压和 2.0% 体积变化，"
                "性能源于有序孔隙和 Dirac nodal net。"
            ),
        }],
        save_report=False,
    ))
    assert result["status"] == "success"
    assert result["passed"] is False
    assert result["flagged"]

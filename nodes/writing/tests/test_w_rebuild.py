"""重建后的 writing 节点：每道机械判据都对应一个夹具里真跑出来的缺陷。

样本全部来自 research_runs/astra-sim-yuankk-20260918 的真实运行（iter4–iter11），
不自造：pages.txt / sections / brief / outline / 审读报告是 iter9 的成品，svg 是用户交来的
原图（带 case6 标签），pdf 是图表服务交的矢量图。每条测试的 docstring 写明它抓的是哪一轮的哪个缺陷。
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml

from core.state import State

FIX = Path(__file__).parent / "fixtures" / "rebuild"
HAS_POPPLER = bool(shutil.which("pdftotext") and shutil.which("pdfinfo"))


def _state(tmp_path: Path, *, with_prompt: bool = True) -> State:
    ws = tmp_path / "ws"
    shutil.copytree(FIX / "paper", ws / "paper")
    shutil.copytree(FIX / "figures", ws / "figures")
    shutil.copytree(FIX / "sources", ws / "sources")
    (ws / "paper" / "manuscript" / "figures").mkdir(parents=True, exist_ok=True)
    for pdf in (ws / "figures").glob("*.pdf"):   # 渲染时会暂存到这里；测试直接摆好
        shutil.copy2(pdf, ws / "paper" / "manuscript" / "figures" / pdf.name)
    st = State(run_id="t", node_type="writing", root=tmp_path / "run",
               project_worktree=ws, workspace_root=ws / "paper")
    if with_prompt:
        st.hook_state["node_inputs"] = {"research_question": (FIX / "user_prompt.txt").read_text(encoding="utf-8")}
    return st


def _brief(st: State) -> dict:
    return yaml.safe_load((Path(st.workspace_root) / "brief.yaml").read_text(encoding="utf-8"))


def _pages(st: State) -> list[str]:
    return (Path(st.workspace_root) / "pages.txt").read_text(encoding="utf-8").split("\f")


# ── 简报 ────────────────────────────────────────────────────────────────────

def test_brief_rejects_unknown_rule_clause(tmp_path):
    """iter2：模型自造规则语法（require=拓扑1..拓扑6 且 …）→ 写入时按语法拒绝并回给语法。"""
    from nodes.writing.tools.w_brief import RULE_GRAMMAR, _write_brief
    st = _state(tmp_path)
    r = asyncio.run(_write_brief(st, genre="sci_article_zh", title="t",
                                 must_haves=[{"id": "R1", "text": "x", "check": "mechanical", "rule": "min_refs=40"}]))
    assert r["status"] == "error" and r["grammar"] == RULE_GRAMMAR


def test_brief_rename_requirement_must_be_mechanical_forbid(tmp_path):
    """iter7：模型把 case→拓扑 标成 check=referee 绕过只查 mechanical 的闸 → 按这件事判，不按写法判。"""
    from nodes.writing.tools.w_brief import _write_brief
    st = _state(tmp_path)
    r = asyncio.run(_write_brief(st, genre="sci_article_zh", title="t",
                                 must_haves=[{"id": "R2", "text": "拓扑重命名：case6→拓扑1、case2→拓扑2", "check": "referee"}]))
    assert r["status"] == "error" and "forbid=" in r["error"]
    r = asyncio.run(_write_brief(st, genre="sci_article_zh", title="t",
                                 must_haves=[{"id": "R2", "text": "拓扑重命名：case6→拓扑1", "check": "mechanical",
                                              "rule": "forbid=case[1-6];require=拓扑1"}]))
    assert r["status"] == "success"


def test_brief_empty_must_haves_rejected_when_prompt_has_numbered_requirements(tmp_path):
    """iter10：模型第一次调用就把 must_haves 交空，H6 整轮无牙 → 用户原话有编号要求时不能为空。"""
    from nodes.writing.tools.w_brief import _write_brief
    st = _state(tmp_path)
    r = asyncio.run(_write_brief(st, genre="sci_article_zh", title="t", must_haves=None))
    assert r["status"] == "error" and len(r["candidates"]) >= 5
    st2 = _state(tmp_path / "b", with_prompt=False)
    assert asyncio.run(_write_brief(st2, genre="sci_article_zh", title="t", must_haves=None))["status"] == "success"


def test_brief_language_free_text_normalized(tmp_path):
    """iter2：language 写成「中文」「English」导致 H8 被跳过 → 统一成 zh/en 代码。"""
    from nodes.writing.tools.w_brief import _write_brief
    st = _state(tmp_path, with_prompt=False)
    r = asyncio.run(_write_brief(st, genre="sci_article_zh", title="t", language_body="中文", language_figures="English"))
    assert r["brief"]["language"] == {"body": "zh", "figures": "en"}


# ── 卷宗 ────────────────────────────────────────────────────────────────────

def test_dossier_figure_ids_stable_and_records_not_docs(tmp_path):
    """iter4：IMG 编号按文件排序现编，图表服务多交文件后 IMG6 漂成另一张图 → 编号按路径持久化；
    iter2：figures/figure__*.md 被当成文档使 D 编号错位 → 图记录不算文档。"""
    from nodes.writing.tools.w_dossier import build_dossier
    st = _state(tmp_path)
    d1 = build_dossier(st, force=True)
    ids1 = {f["name"]: f["id"] for f in d1["figures"]}
    assert not any(str(x.get("path", "")).startswith("figures/figure__") for x in d1["docs"] if x.get("doc_id"))
    (Path(st.project_worktree) / "figures" / "aaa_new.png").write_bytes(b"png")
    d2 = build_dossier(st, force=True)
    ids2 = {f["name"]: f["id"] for f in d2["figures"]}
    assert all(ids2[n] == ids1[n] for n in ids1)
    assert ids2["aaa_new.png"] not in ids1.values()


def test_dossier_caption_links_even_when_record_sorts_after_file(tmp_path):
    """iter4：一遍扫按路径排序，fig1_*.pdf 排在 figure__*.md 前面，图题挂空 → 先收记录再挂文件，记录名与文件名互为前缀也认。"""
    from nodes.writing.tools.w_dossier import build_dossier
    st = _state(tmp_path)
    figs = Path(st.project_worktree) / "figures"
    (figs / "a_chart.pdf").write_bytes((figs / "gpu_server_topologies_1_3.pdf").read_bytes())
    (figs / "figure__a_chart_dbt_ring_ratio.md").write_text("![alt](figures/nothing.png)\n\nCaption: DBT over Ring ratio.\n", encoding="utf-8")
    d = build_dossier(st, force=True)
    by = {f["name"]: f.get("caption") for f in d["figures"]}
    assert by["a_chart.pdf"] == "DBT over Ring ratio."
    assert by["gpu_server_topologies_1_3.pdf"]  # 记录显式链接的文件


# ── 提纲 ────────────────────────────────────────────────────────────────────

def test_outline_accepts_id_name_stem_and_path_and_resolves_file(tmp_path):
    """iter8：写手把来源写成 figures/xxx.pdf（带路径），旧闸只认 id/名/去后缀 → 六张图全拒。"""
    from nodes.writing.tools.w_brief import _write_outline, load_outline
    from nodes.writing.tools.w_dossier import build_dossier
    st = _state(tmp_path)
    d = build_dossier(st, force=True)
    f = next(x for x in d["figures"] if x["name"] == "gpu_server_topologies_1_3.pdf")
    ol = yaml.safe_load((Path(st.workspace_root) / "outline.yaml").read_text(encoding="utf-8"))
    ol["figures"] = [
        {"key": "fig:a", "source": f["id"], "message": "m"},
        {"key": "fig:b", "source": f["name"], "message": "m"},
        {"key": "fig:c", "source": "gpu_server_topologies_1_3", "message": "m"},
        {"key": "fig:d", "source": f["path"], "message": "m"},
    ]
    r = asyncio.run(_write_outline(st, ol))
    assert not [i for i in r["issues"] if i.startswith("图 ")], r["issues"]   # 证据 id 的问题来自夹具只带了数据表节选
    assert {x["file"] for x in load_outline(st)["figures"]} == {"gpu_server_topologies_1_3.pdf"}


def test_outline_prescans_figure_text_for_forbidden_terms(tmp_path):
    """iter5：用户原图 svg 带 case6 标签被原样搬进稿子，正文干净成品违约 → 提纲阶段抽图内文字过 forbid。"""
    from nodes.writing.tools.w_brief import _write_outline
    st = _state(tmp_path)
    ol = yaml.safe_load((Path(st.workspace_root) / "outline.yaml").read_text(encoding="utf-8"))
    ol["figures"] = [{"key": "fig:t1", "source": "topology1_single_gpu_1nic_32nodes.svg", "message": "m"}]
    r = asyncio.run(_write_outline(st, ol))
    assert any("case[1-6]" in i and "fig:t1" in i for i in r["issues"]), r["issues"]


# ── 起草收据 ────────────────────────────────────────────────────────────────

def test_math_letters_normalized_and_flagged():
    """iter4：正文贴了 Unicode 数学字母 𝑇，字体没有，渲染成 U+FFFD → 落盘前归一化，全角标点不动。"""
    from nodes.writing.tools.w_common import normalize_math_letters
    t, n = normalize_math_letters("即 𝑇 = 𝑇 wall time/𝑇6，全角，保留")
    assert t == "即 T = T wall time/T6，全角，保留" and n == 3


def test_fragment_problems_catch_process_vocab_and_preamble():
    """iter0（生产稿）：正文出现「上游 artifact」「用户交来」；片段里塞 \\documentclass。"""
    from nodes.writing.tools.w_common import fragment_problems
    p = fragment_problems("\\documentclass{article} 本文基于一份用户交来的数据（列入 revision items）。")
    assert any("documentclass" in x for x in p) and any("用户交来" in x for x in p) and any("revision item" in x for x in p)


def test_figure_env_mismatch_against_outline(tmp_path):
    """iter4：图 1/图 2 文件互换 → 图环境 label 对应的提纲图键，其文件必须就是 includegraphics 的文件。"""
    from nodes.writing.tools.w_draft import _figure_env_mismatches
    outline = {"figures": [{"key": "fig:radar", "file": "radar.pdf"}, {"key": "fig:topo", "file": "topo.pdf"}]}
    tex = ("\\begin{figure}\\includegraphics{figures/topo.pdf}\\caption{雷达图}\\label{fig:radar}\\end{figure}"
           "\\begin{figure}\\includegraphics{figures/radar.pdf}\\caption{拓扑}\\label{fig:topo}\\end{figure}"
           "\\begin{figure}\\includegraphics{figures/x.pdf}\\caption{无标签}\\end{figure}")
    p = _figure_env_mismatches(tex, outline, {"figures": []})
    assert sum("fig:radar" in x for x in p) == 1 and sum("fig:topo" in x for x in p) == 1 and any("没有 \\label" in x for x in p)


# ── 渲染硬线（文字层，不依赖 poppler）────────────────────────────────────────

def test_hard_lines_text_layer_on_real_pages(tmp_path):
    """iter9 终稿的 pages.txt：H1–H6、H10、H11 在真样本上全过；然后逐条注入缺陷看它红。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    pages = _pages(st)
    hl = hard_lines(st, pages, "", None)
    for k in ("H1_title_block", "H2_process_vocab", "H3_references_resolved", "H4_figures_tables",
              "H6_must_haves", "H7_layout", "H10_no_mojibake", "H11_figure_files_match_outline"):
        assert hl[k]["passed"], (k, hl[k])


def test_h3_literal_section_out_of_range_and_undefined_ref_location(tmp_path):
    """iter4：结论手写「第 7 节」而全文只有 5 节；iter9：undefined label 在 introduction，写手改错了两次部件。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    pages = _pages(st)
    pages[-1] += "\n具体边界见第 9 节局限性。"
    sec = Path(st.workspace_root) / "manuscript" / "sections" / "introduction.tex"
    sec.write_text(sec.read_text(encoding="utf-8") + "\n见图~\\ref{fig:gone}。\n", encoding="utf-8")
    log = "LaTeX Warning: Reference `fig:gone' on page 3 undefined on input line 11.\n"
    h3 = hard_lines(st, pages, log, None)["H3_references_resolved"]
    assert not h3["passed"]
    assert h3["literal_section_out_of_range"][0]["text"].startswith("第 9")
    assert h3["undefined_refs_in_parts"] == {"fig:gone": ["introduction"]}


def test_h4_requires_caption_line_for_each_referenced_number(tmp_path):
    """iter6：竖长条图把图题挤出页外，正文提到「图 2」却没有图题行，旧 H4 照样放行。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    pages = [p.replace("图1 ", "图X ").replace("图 1 ", "图 X ") for p in _pages(st)]
    import re
    pages = [re.sub(r"^(\s*)图\s*1(\s)", r"\1图 X\2", p, flags=re.M) for p in pages]
    h4 = hard_lines(st, pages, "", None)["H4_figures_tables"]
    assert not h4["passed"] and 1 in h4["figures"]["no_caption"]


def test_h6_forbid_counts_figure_text_in_delivered_pdf(tmp_path):
    """iter5：正文干净、图内文字带 case6 → 验收按交付的 PDF 算，命中即红并指出在图里。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    pages = _pages(st)
    pages[4] += "\ncase6: Single-GPU single-NIC server ×32\n"
    r4 = next(x for x in hard_lines(st, pages, "", None)["H6_must_haves"]["results"] if "forbid" in x["rule"])
    assert not r4["passed"] and "图内文字" in r4["detail"] and "页 [5]" in r4["detail"]


def test_h7_float_too_large_from_log(tmp_path):
    """iter6：2812×8863 的竖长条图 Float too large 688pt，图题挤出页外。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    h7 = hard_lines(st, _pages(st), "LaTeX Warning: Float too large for page by 688.00136pt on input line 57.\n", None)["H7_layout"]
    assert not h7["passed"] and h7["floats_too_large"] == ["688.00136pt @ line 57"]


def test_h10_checks_author_source_not_pdf_text(tmp_path):
    """iter6：规范的 $T_B$ 数学斜体被 pdftotext 抽成 U+FFFD，旧 H10 假阳性 → 只查作者源码与日志 Missing character。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    pages = _pages(st)
    pages[8] += " (\ufffd\ufffdB − \ufffd\ufffdA)/\ufffd\ufffdA "
    assert hard_lines(st, pages, "", None)["H10_no_mojibake"]["passed"]
    sec = Path(st.workspace_root) / "manuscript" / "sections" / "methods.tex"
    sec.write_text(sec.read_text(encoding="utf-8") + "\n相对开销 (𝑇B − 𝑇A)/𝑇A\n", encoding="utf-8")
    h10 = hard_lines(st, pages, "", None)["H10_no_mojibake"]
    assert not h10["passed"] and h10["hits"][0]["part"] == "methods"


def test_h11_flags_figure_file_not_matching_outline(tmp_path):
    """iter4：IMG 漂移后图环境放的文件与提纲图键不一致 → H11 对账。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    ol_path = Path(st.workspace_root) / "outline.yaml"
    ol = yaml.safe_load(ol_path.read_text(encoding="utf-8"))
    for f in ol["figures"]:
        if f["key"] == "fig:topology_1_3":
            f["file"] = "somewhere_else.pdf"
    ol_path.write_text(yaml.safe_dump(ol, allow_unicode=True, sort_keys=False), encoding="utf-8")
    h11 = hard_lines(st, _pages(st), "", None)["H11_figure_files_match_outline"]
    assert not h11["passed"] and h11["issues"][0]["label"] == "fig:topology_1_3"


# ── 渲染硬线（需要 poppler）─────────────────────────────────────────────────

@pytest.mark.skipif(not HAS_POPPLER, reason="pdftotext/pdfinfo 不在 PATH")
def test_h8_h9_measure_only_included_figures(tmp_path):
    """iter7：H8 扫暂存目录里换掉的旧图误判 → 只查稿子实际引用的；H9 按宽/高上限量矢量字号。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    mfig = Path(st.workspace_root) / "manuscript" / "figures"
    src = Path(st.project_worktree) / "figures"
    shutil.copy2(src / "gpu_server_topologies_1_3.pdf", mfig / "gpu_server_topologies_1_3.pdf")
    # 暂存目录里留一张带中文文字的旧图（稿子没引用它）：不该被 H8 判红
    shutil.copy2(src / "gpu_server_topologies_1_3.pdf", mfig / "stale.pdf")
    hl = hard_lines(st, _pages(st), "", None)
    assert hl["H8_figure_text_language"]["passed"]
    measured = [x for x in hl["H9_figure_legibility"]["figures"] if x.get("measured")]
    assert measured and all(x["median_text_pt"] >= 7 for x in measured), measured


@pytest.mark.skipif(not HAS_POPPLER, reason="pdftotext/pdfinfo 不在 PATH")
def test_h9_raster_only_figure_fails_unless_raster_ok(tmp_path):
    """iter7：写手换成 PNG-only 图，字号与语言都核不了却只留一条 note → 默认红，提纲标 raster_ok 才放行。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    sec = Path(st.workspace_root) / "manuscript" / "sections" / "results.tex"
    sec.write_text(sec.read_text(encoding="utf-8") + "\n\\begin{figure}\\includegraphics[width=0.9\\linewidth]{figures/only.png}\\caption{c}\\label{fig:only}\\end{figure}\n", encoding="utf-8")
    (Path(st.workspace_root) / "manuscript" / "figures" / "only.png").write_bytes(b"png")
    h9 = hard_lines(st, _pages(st), "", None)["H9_figure_legibility"]
    assert not h9["passed"]
    ol_path = Path(st.workspace_root) / "outline.yaml"
    ol = yaml.safe_load(ol_path.read_text(encoding="utf-8"))
    ol["figures"].append({"key": "fig:only", "file": "only.png", "message": "photo", "raster_ok": True})
    ol_path.write_text(yaml.safe_dump(ol, allow_unicode=True, sort_keys=False), encoding="utf-8")
    assert hard_lines(st, _pages(st), "", None)["H9_figure_legibility"]["passed"]


@pytest.mark.skipif(not HAS_POPPLER, reason="pdftotext/pdfinfo 不在 PATH")
def test_h12_measures_the_manuscript_not_the_last_figure(tmp_path):
    """iter5：H9 循环里的局部变量 pdf 遮住了参数，H12 量成最后一张图的 PDF、报「第 1 页」。"""
    from nodes.writing.tools.w_render import hard_lines
    st = _state(tmp_path)
    fig = Path(st.project_worktree) / "figures" / "gpu_server_topologies_1_3.pdf"
    h12 = hard_lines(st, _pages(st), "", fig)["H12_print_size"]
    # 图 PDF 原生字号大，作为「稿子」来量应当全过；关键是它量的是传入的文件而不是别的
    assert h12["passed"] and h12["pages"] == []


# ── 暂存与装配 ──────────────────────────────────────────────────────────────

def test_stage_prefers_pdf_twin_and_caps_height(tmp_path):
    """iter6：写手用了 png 而同名 pdf 存在，H9 量不到；竖长条图把图题挤出页外 → 同名 pdf 优先、height 兜底。"""
    from nodes.writing.tools.w_brief import load_outline
    from nodes.writing.tools.w_render import _stage_figures
    st = _state(tmp_path)
    figs = Path(st.project_worktree) / "figures"
    (figs / "gpu_server_topologies_1_3.png").write_bytes(b"png")
    sec = Path(st.workspace_root) / "manuscript" / "sections" / "methods.tex"
    sec.write_text(sec.read_text(encoding="utf-8").replace("gpu_server_topologies_1_3.pdf", "gpu_server_topologies_1_3.png"), encoding="utf-8")
    _stage_figures(st, load_outline(st))
    t = sec.read_text(encoding="utf-8")
    assert "gpu_server_topologies_1_3.pdf" in t and ".png}" not in t
    assert "height=0.8\\textheight,keepaspectratio" in t


# ── 审读官与提交 ────────────────────────────────────────────────────────────

def test_figure_crosswalk_gives_record_or_figure_text(tmp_path):
    """iter4：审读官只读文字看不见图，判了「R5 已兑现」→ 给它文件/记录图题/稿件图题对照；示意图无记录时给图内文字。"""
    from nodes.writing.tools.w_referee import _figure_crosswalk
    st = _state(tmp_path)
    mdir = Path(st.workspace_root) / "manuscript"
    (mdir / "main.tex").write_text("\\input{sections/methods}\n\\input{sections/results}\n", encoding="utf-8")
    cw = _figure_crosswalk(st)
    assert "图 1" in cw and "gpu_server_topologies_1_3" in cw
    assert ("图表服务记录：" in cw) and ("稿件图题：" in cw)


def test_submit_requires_verdict_on_the_delivered_pdf(tmp_path):
    """iter9：第一轮 minor 后改稿、重渲染、直接提交，交付的 PDF 从没被审读 → 结论绑定 PDF 指纹。"""
    from nodes.writing.tools.w_deliver import _submit_manuscript
    from nodes.writing.tools.w_referee import _pdf_sha
    st = _state(tmp_path)
    pdf = Path(st.workspace_root) / "manuscript.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    (Path(st.workspace_root) / "render_report.json").write_text(json.dumps(
        {"status": "success", "pdf_path": str(pdf), "pages": 23, "hard_lines": {"passed": True}}), encoding="utf-8")
    (Path(st.workspace_root) / "author_notes.md").write_text("# 作者备注\n无", encoding="utf-8")
    rp = Path(st.workspace_root) / "referee_report.json"
    rep = json.loads(rp.read_text(encoding="utf-8"))
    rep["pdf_sha256"] = "sha256:stale"
    rp.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    r = asyncio.run(_submit_manuscript(st))
    assert r["status"] == "error" and "另一版 PDF" in r["error"]
    rep["pdf_sha256"] = _pdf_sha(str(pdf))
    rp.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    r = asyncio.run(_submit_manuscript(st))
    assert r["status"] == "success" and r["artifact"]


# ── 派图 ────────────────────────────────────────────────────────────────────

def test_request_figures_injects_brief_policy(tmp_path):
    """iter11：模型直接调 run_node，15 条请求的 constraints 全是散文、没有 text_language，七张图画成中文。"""
    from nodes.writing.tools.w_figures import _normalize_requests
    st = _state(tmp_path)
    out, problems = _normalize_requests(_brief(st), [
        {"intent": "让读者看出通信占比", "asset_kind": "quantitative", "constraints": "16:9 比例，4K"},
        {"intent": "拓扑 1 结构", "asset_kind": "schematic", "spec": {"nodes": [], "edges": []}},
        {"intent": "没有 spec 的示意图", "asset_kind": "schematic"},
    ])
    assert any("spec" in p for p in problems)
    c = out[0]["constraints"]
    assert c["text_language"] == "en" and c["min_font_pt"] == 7 and set(c["formats"]) == {"pdf", "png"}
    assert c["notes"] == "16:9 比例，4K" and c["forbid_text"] == ["case[1-6]"] and c["rename"]["case6"].startswith("拓扑")


# ── 派审对象 ────────────────────────────────────────────────────────────────

def test_review_target_is_the_declared_deliverable():
    """生产事故：writing 先 stage 图再存稿，产物列表第一项是图收据，终稿两次 reviewer 审的都是那张收据。"""
    from shared.tools.run_node import _review_target
    ids = ["writing_asset_receipt__figs", "manuscript__moe_gpu"]
    assert _review_target("writing", ids) == "manuscript__moe_gpu"
    assert _review_target("writing", ["writing_asset_receipt__only"]) == "writing_asset_receipt__only"


def test_brief_says_whether_pdf_works(tmp_path, monkeypatch):
    """2026-09-23 干净 Windows：writing 写了 45 分钟，render_manuscript 才发现出不了 PDF。
    开工那一刻（write_brief）就要把实测结果交到 writing 手里，连同该怎么办。"""
    from nodes.writing.tools.w_brief import _write_brief
    from shared.lib import pdf_toolchain

    async def broken(state=None, *, timeout=900):
        return {"works": False, "status": "broken", "reason": "error: program not found",
                "identity": {"compiler": "tectonic", "biber": None}}

    monkeypatch.setattr(pdf_toolchain, "ensure_measured", broken)
    st = _state(tmp_path, with_prompt=False)
    r = asyncio.run(_write_brief(st, genre="sci_article_zh", title="t"))
    assert r["status"] == "success", "出不了 PDF 不该拦住简报：源码照样能写、能交付"
    assert "实测**不可用**" in r["pdf_toolchain"] and "program not found" in r["pdf_toolchain"]
    assert "report_blocker" in r["next"] and "write_outline" in r["next"]

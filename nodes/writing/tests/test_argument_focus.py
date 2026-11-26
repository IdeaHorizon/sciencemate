"""Writing selects an argument from existing results without replanning research."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.writing.tools import w_brief, w_dossier, w_draft, w_figures

RESULTS = {
    "Q1": "共用图库与枚举方法已经建立。",
    "Q2": "谱与度平方和确定独立三元组数量。",
    "Q3": "另一命题的分类检验完成，独立结论另行报告。",
    "Q4": "补充命题得到边界条件，尚不属于本文论证。",
    "Q5": "两个规模的随机图测量完成，属于项目总体成果。",
    "Q6": "两张六顶点图谱与度平方和相同，三元组均为7，四元组分别为2和1。",
}


@pytest.fixture
def writing(tmp_path, monkeypatch):
    ws = tmp_path / "project"
    (ws / "sources").mkdir(parents=True)
    source = ws / "sources" / "completed-results.md"
    source.write_text(
        "# 已有项目成果\n\n" + "\n\n".join(f"## {key}\n{text}" for key, text in RESULTS.items()),
        encoding="utf-8",
    )
    state = State(
        run_id="argument-focus",
        node_type="writing",
        root=tmp_path / "run",
        project_worktree=ws,
        workspace_root=ws / "paper",
    )
    state.hook_state["node_inputs"] = {
        "research_question": "请用已有成果写研究论文，不重新开展研究。"
    }

    async def measured(_state):
        return {"works": True}

    monkeypatch.setattr("shared.lib.pdf_toolchain.ensure_measured", measured)
    calls = []

    async def model(_state, **kwargs):
        calls.append(kwargs)
        return "\\section{结果}\n本节呈现所选证据与适用边界。"

    monkeypatch.setattr(w_draft, "bounded_llm", model)
    dossier = w_dossier.build_dossier(state)
    ids = {
        entry["heading"]: key
        for key, entry in dossier["entries"].items()
        if entry.get("heading") in RESULTS
    }
    assert set(ids) == set(RESULTS)
    return state, ids, calls, source


def outline(ids, *, summary=False):
    selected = list(RESULTS) if summary else ["Q2", "Q6"]
    included = set(RESULTS) if summary else {"Q1", "Q2", "Q6"}
    parts = (
        ["introduction", "results", "conclusion"]
        if summary
        else ["abstract", "introduction", "methods", "results", "discussion", "conclusion"]
    )
    return {
        "parts": [
            {
                "id": part,
                "claims": (
                    [{"text": RESULTS[q], "evidence": [ids[q]]} for q in selected]
                    if part == "results"
                    else (
                        [{"text": RESULTS["Q1"], "evidence": [ids["Q1"]]}]
                        if part == "methods"
                        else []
                    )
                ),
            }
            for part in parts
        ],
        "editorial_focus": {
            "main_question": "项目各项工作取得了什么？"
            if summary
            else "谱信息能确定到哪一阶独立集计数？",
            "main_finding": "逐项回顾六项已有成果。" if summary else RESULTS["Q6"],
            "dependent_questions": ["Q2", "Q6"] if not summary else list(RESULTS),
            "evidence_selection": [
                {
                    "evidence": [ids[q]],
                    "placement": "body" if q in included else "project_only",
                    "reason": "支撑本文主张或必要方法"
                    if q in included
                    else "独立成果保留在项目，不并入本文正文",
                }
                for q in RESULTS
            ],
        },
        "figures": [],
        "tables": [],
    }


def test_brief_retains_original_request_and_explicit_questions(writing):
    state, _ids, _calls, _source = writing
    request = "用户明确要求同时回答Q1、Q2、Q3，允许相依子问，不授权删去任何一问。"
    state.hook_state["node_inputs"]["research_question"] = request
    questions = [{"id": q, "text": f"必须回答{q}原问题"} for q in ["Q1", "Q2", "Q3"]]
    result = asyncio.run(
        w_brief._write_brief(
            state, genre="sci_article_zh", title="工作标题", required_questions=questions
        )
    )
    assert result["status"] == "success"
    assert w_brief.load_brief(state)["source_request"] == request
    assert w_brief.load_brief(state)["required_questions"] == questions


def test_same_materials_are_selected_differently_for_article_and_project_summary(writing):
    state, ids, calls, source = writing
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    for genre, summary in [("sci_article_zh", False), ("project_summary", True)]:
        state.hook_state["node_inputs"]["research_question"] = (
            "请逐项总结项目的六项已有成果。"
            if summary
            else "请用已有成果写研究论文，不重新开展研究。"
        )
        result = asyncio.run(w_brief._write_brief(state, genre=genre, title="已有成果成稿"))
        assert result["status"] == "success", result
        written = asyncio.run(w_brief._write_outline(state, outline(ids, summary=summary)))
        assert written["status"] == "success", written
        drafted = asyncio.run(w_draft._draft_section(state, "results"))
        assert drafted["status"] == "success", drafted
    article, summary = calls
    assert "谱信息能确定到哪一阶独立集计数" in article["user"]
    assert RESULTS["Q2"] in article["user"] and RESULTS["Q6"] in article["user"]
    assert RESULTS["Q3"] not in article["user"] and RESULTS["Q5"] not in article["user"]
    assert all(text in summary["user"] for text in RESULTS.values())
    assert "项目总结" in summary["system"] and "逐项" in summary["system"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    stored = w_dossier.build_dossier(state)
    assert all(
        text in {entry.get("text") for entry in stored["entries"].values()}
        for text in RESULTS.values()
    )


def test_required_questions_cannot_be_silently_deferred_by_editorial_focus(writing):
    state, ids, calls, _source = writing
    questions = [{"id": q, "text": f"请回答{q}"} for q in ["Q2", "Q3", "Q6"]]
    asyncio.run(
        w_brief._write_brief(
            state, genre="sci_article_zh", title="多问论文", required_questions=questions
        )
    )
    plan = outline(ids)
    plan["question_coverage"] = {
        "Q2": {"placement": "body", "part": "results"},
        "Q6": {"placement": "body", "part": "results"},
        "Q3": {"placement": "project_only"},
    }
    result = asyncio.run(w_brief._write_outline(state, plan))
    assert any("Q3" in issue and "用户" in issue for issue in result["issues"])
    plan["question_coverage"]["Q3"] = {
        "placement": "appendix",
        "part": "appendix",
        "reason": "用户允许附录回答",
    }
    assert any(
        "Q3" in issue for issue in asyncio.run(w_brief._write_outline(state, plan))["issues"]
    )
    plan["parts"].append(
        {"id": "appendix", "claims": [{"text": RESULTS["Q3"], "evidence": [ids["Q3"]]}]}
    )
    next(
        selection
        for selection in plan["editorial_focus"]["evidence_selection"]
        if selection["evidence"] == [ids["Q3"]]
    )["placement"] = "appendix"
    assert asyncio.run(w_brief._write_outline(state, plan))["status"] == "success"
    asyncio.run(w_draft._draft_section(state, "results"))
    assert all(question["text"] in calls[-1]["user"] for question in questions)
    assert "appendix" in calls[-1]["user"]
    asyncio.run(w_draft._draft_section(state, "appendix"))
    assert RESULTS["Q3"] in calls[-1]["user"]


def test_proof_and_counterexample_are_valid_evidence_without_figures(writing):
    state, ids, calls, _source = writing
    asyncio.run(w_brief._write_brief(state, genre="sci_article_zh", title="证明与反例"))
    assert asyncio.run(w_brief._write_outline(state, outline(ids)))["status"] == "success"
    result = asyncio.run(w_draft._draft_section(state, "results"))
    assert result["status"] == "success"
    instructions = calls[-1]["system"] + calls[-1]["user"]
    assert "证明" in instructions and "反例" in instructions
    assert "研究问题，编号列出（3 到 5 个）" not in instructions
    assert "第一小节：总览。用总览图" not in instructions


@pytest.mark.parametrize(
    "part,forbidden",
    [
        ("abstract", "关键结果：两到四条"),
        ("introduction", "研究问题，编号列出（3 到 5 个）"),
        ("conclusion", "三到五条，直接回答引言里编号的研究问题"),
    ],
)
def test_active_part_craft_does_not_generate_a_fixed_question_quota(writing, part, forbidden):
    state, ids, calls, _source = writing
    asyncio.run(w_brief._write_brief(state, genre="sci_article_zh", title="同一主张的完整论文"))
    asyncio.run(w_brief._write_outline(state, outline(ids)))
    assert asyncio.run(w_draft._draft_section(state, part))["status"] == "success"
    assert forbidden not in calls[-1]["system"]
    assert "谱信息能确定到哪一阶独立集计数" in calls[-1]["user"]


def test_declared_material_disposition_matches_actual_claim_placement(writing):
    state, ids, _calls, _source = writing
    asyncio.run(w_brief._write_brief(state, genre="sci_article_zh", title="材料取舍"))
    plan = outline(ids)
    next(
        selection
        for selection in plan["editorial_focus"]["evidence_selection"]
        if selection["evidence"] == [ids["Q2"]]
    )["placement"] = "project_only"
    result = asyncio.run(w_brief._write_outline(state, plan))
    assert any(ids["Q2"] in issue and "冲突" in issue for issue in result["issues"])


def test_figure_dispatch_consumes_persisted_claim_and_comparison(writing, monkeypatch):
    state, ids, _calls, _source = writing
    asyncio.run(w_brief._write_brief(state, genre="sci_article_zh", title="反例比较"))
    from shared.tools.library.artifact_intake import _import_artifact
    from shared.tools.run_node import _resolve_forward_artifacts

    imported = asyncio.run(
        _import_artifact(
            state,
            artifact_type="source_material",
            name="completed_results",
            source_path="sources/completed-results.md",
        )
    )
    assert imported["status"] == "success", imported
    plan = outline(ids)
    plan["figures"] = [
        {
            "key": "fig:counterexample",
            "source": "planned",
            "message": "看出相同信息下的差别",
            "claim": RESULTS["Q6"],
            "comparison": "并排比较两张六顶点图与独立四元组计数",
            "reader_takeaway": "相同谱和度平方和不能确定独立四元组数量",
            "evidence": [ids["Q6"]],
        }
    ]
    assert asyncio.run(w_brief._write_outline(state, plan))["status"] == "success"
    dispatched = []

    async def postprocess(_state, **kwargs):
        dispatched.append(kwargs)
        # Exercise the existing forwarding boundary, not only the input keys.
        available, missing = _resolve_forward_artifacts(_state, kwargs["forward_artifact_ids"])
        assert not missing
        assert len(available) == 1 and available[0]["provenance"]["kind"] == "imported"
        assert RESULTS["Q6"] in available[0]["content"]
        return {"status": "success"}

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", postprocess)
    result = asyncio.run(
        w_figures._request_figures(
            state,
            [
                {
                    "figure_key": "fig:counterexample",
                    "intent": "请表达已选定的比较",
                    "asset_kind": "schematic",
                    "spec": {"nodes": [{"id": "left"}, {"id": "right"}], "edges": []},
                }
            ],
        )
    )
    assert result["status"] == "success", result
    payload = json.dumps(dispatched[0]["node_inputs"], ensure_ascii=False)
    for field in ["claim", "comparison", "reader_takeaway"]:
        assert plan["figures"][0][field] in payload
    assert ids["Q6"] in payload
    assert RESULTS["Q6"] in payload
    assert "sources/completed-results.md" in payload
    request = dispatched[0]["node_inputs"]["visual_requests"][0]
    assert request["source_artifact_ids"] == [imported["artifact_id"]]
    assert dispatched[0]["forward_artifact_ids"] == [imported["artifact_id"]]


def test_unregistered_dossier_source_reaches_child_as_readable_material(writing, monkeypatch):
    state, ids, _calls, source = writing
    asyncio.run(w_brief._write_brief(state, genre="sci_article_zh", title="已有材料比较"))
    asyncio.run(w_brief._write_outline(state, outline(ids)))
    dispatched = []

    async def postprocess(_state, **kwargs):
        dispatched.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", postprocess)
    asyncio.run(
        w_figures._request_figures(
            state,
            [
                {
                    "intent": "比较已有反例",
                    "asset_kind": "quantitative",
                    "source_artifact_ids": [ids["Q6"]],
                }
            ],
        )
    )
    request = dispatched[0]["node_inputs"]["visual_requests"][0]
    assert str(source) in request["intent"] and RESULTS["Q6"] in request["intent"]
    assert not request.get("source_artifact_ids") and not dispatched[0]["forward_artifact_ids"]
    assert not state.list_artifacts()  # No fabricated writing-owned evidence.


def test_selected_figures_do_not_expand_to_all_project_figures(writing):
    state, ids, calls, _source = writing
    figures = Path(state.project_worktree) / "figures"
    figures.mkdir()
    original = Path(__file__).parent / "fixtures/rebuild/figures/fig1_wall_time.pdf"
    (figures / "independent_project_result.pdf").write_bytes(original.read_bytes())
    w_dossier.build_dossier(state, force=True)
    asyncio.run(w_brief._write_brief(state, genre="sci_article_zh", title="有取舍的图"))
    asyncio.run(w_brief._write_outline(state, outline(ids)))
    asyncio.run(w_draft._draft_section(state, "results"))
    assert "independent_project_result.pdf" not in calls[-1]["user"]
    assert (figures / "independent_project_result.pdf").read_bytes() == original.read_bytes()


def test_project_summary_uses_existing_renderer_and_validation(writing):
    from nodes.writing.tools.w_common import load_genre
    from nodes.writing.tools.w_render import _assemble, hard_lines

    state, ids, _calls, _source = writing
    asyncio.run(w_brief._write_brief(state, genre="project_summary", title="项目总结"))
    asyncio.run(w_brief._write_outline(state, outline(ids, summary=True)))
    for part in ["introduction", "results", "conclusion"]:
        asyncio.run(w_draft._draft_section(state, part))
    brief = w_brief.load_brief(state)
    main = _assemble(state, brief, load_genre(brief["genre"]))
    text = main.read_text()
    assert all(
        rf"\input{{sections/{part}}}" in text for part in ["introduction", "results", "conclusion"]
    )
    assert r"\documentclass[scheme=chinese,12pt,a4paper]{ctexart}" in text
    checks = hard_lines(state, ["项目总结\n项目目标与范围\n工作与成果回顾\n总结与后续"], "")
    assert checks["H1_title_block"]["passed"]


def test_existing_brief_and_outline_remain_usable_without_new_fields(writing):
    state, ids, calls, _source = writing
    asyncio.run(w_brief._write_brief(state, genre="sci_article_zh", title="旧稿续写"))
    old = outline(ids)
    old.pop("editorial_focus")
    assert asyncio.run(w_brief._write_outline(state, old))["status"] == "success"
    assert asyncio.run(w_draft._draft_section(state, "results"))["status"] == "success"
    assert calls

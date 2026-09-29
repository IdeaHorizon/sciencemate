"""审读官：拿着体裁的审读规则读 PDF 文字，出带页码的意见。独立上下文，一次有界调用。"""
from __future__ import annotations

import json
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

from .w_brief import load_brief, load_outline
from .w_common import (
    bounded_llm, clip, extract_json, figure_text_sample, graphics_files, load_genre, manuscript_dir, paper_dir,
    read_json, worktree_root, write_json,
)
from .w_dossier import build_dossier


def _figure_crosswalk(state: Any) -> str:
    """审读官看不见图：把每个图环境的文件、图表服务记录的图题、稿件图题并排给它。"""
    import re
    from pathlib import Path
    mdir = manuscript_dir(state)
    main = mdir / "main.tex"
    order = (re.findall(r"\\input\{sections/([^}]+)\}", main.read_text(encoding="utf-8")) if main.is_file()
             else sorted(p.stem for p in (mdir / "sections").glob("*.tex")))
    caps = {Path(f["name"]).stem: f["caption"] for f in build_dossier(state).get("figures", []) if f.get("caption")}
    rows: list[str] = []
    n = 0
    for pid in order:
        p = mdir / "sections" / f"{pid}.tex"
        if not p.is_file():
            continue
        for env in re.finditer(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", p.read_text(encoding="utf-8"), re.S):
            n += 1
            body = env.group(1)
            files = graphics_files(body)
            cap = re.search(r"\\caption\{((?:[^{}]|\{[^{}]*\})*)\}", body)
            rec = "；".join(caps[Path(x).stem] for x in files if caps.get(Path(x).stem))
            if not rec:
                root = worktree_root(state)
                samples = [figure_text_sample(Path(x).stem, [mdir / "figures", (root / "figures") if root else None])
                           for x in files]
                samples = [t for t in samples if t]
                rec = ("（无记录；图内文字：" + " ‖ ".join(samples) + "）") if samples else "（无记录，也抽不到图内文字：无法核对这张图画的是什么）"
            rows.append(f"图 {n}：文件 {files}\n  图表服务记录：{rec[:320]}\n  稿件图题：{(cap.group(1) if cap else '（无）')[:200]}")
    return "\n".join(rows) or "（稿件里没有图环境）"

MAX_ROUNDS = 3


def _pdf_sha(path: str | None) -> str | None:
    import hashlib
    from pathlib import Path
    if not path or not Path(path).is_file():
        return None
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def _referee_review(state: Any, focus: str = "", **_: Any) -> dict:
    brief = load_brief(state)
    if not brief:
        return {"status": "error", "error": "先 write_brief"}
    pages_path = paper_dir(state) / "pages.txt"
    render = read_json(paper_dir(state) / "render_report.json") or {}
    if not pages_path.is_file() or render.get("status") != "success":
        return {"status": "error", "error": "先 render_manuscript 成功"}
    rounds = int(state.hook_state.get("writing_referee_rounds") or 0)
    if rounds >= MAX_ROUNDS:
        return {"status": "error", "error": f"审读已达 {MAX_ROUNDS} 轮上限；按最近一份报告修完后 submit_manuscript，未采纳的意见写进作者备注"}
    genre = load_genre(brief["genre"])
    pages = pages_path.read_text(encoding="utf-8").split("\f")
    text = "\n".join(f"===== 第 {i} 页 =====\n{p}" for i, p in enumerate(pages, start=1))
    hl = render.get("hard_lines") or {}
    system = "\n\n".join([
        "你是独立审稿人。你读到的是排好版的 PDF 逐页文字。按规则先判硬线，再打软维，再给带页码、可执行的意见。只输出 JSON。",
        genre["rubric"],
        genre["reader"],
    ])
    user = "\n\n".join([
        f"# 简报\n{json.dumps(brief, ensure_ascii=False, indent=1)}",
        f"# 框架机械硬线结果（供参考，你仍要自己核）\n{json.dumps({k: {'passed': v.get('passed')} for k, v in hl.items() if k.startswith('H')}, ensure_ascii=False)}",
        (f"# 本轮重点\n{focus}" if focus else ""),
        ("# 图文件对照（你看不见图。图表服务记录说明每个文件画的是什么；稿件图题与正文对这张图的描述必须与记录说的是同一张图，"
         f"文件放错、图题张冠李戴一律 critical）\n{_figure_crosswalk(state)}"),
        f"# 稿件（{len(pages)} 页）\n{clip(text, 90000)}",
        ("# 输出格式（严格 JSON）\n"
         '{"verdict":"accept|minor|major|reject","hard_lines":{"H1":true,"H2":true,"H3":true,"H4":true,"H5":true,"H6":true},'
         '"scores":{"contribution_clarity":1,"argument_chain":1,"evidence_support":1,"figures_tables":1,"writing_quality":1,"brief_compliance":1},'
         '"must_haves":[{"id":"R1","met":true,"evidence":"…"}],'
         '"comments":[{"id":1,"page":3,"location":"引言第二段","severity":"major","issue":"…","fix":"…"}],'
         '"summary":"三句话"}'),
    ])
    raw = await bounded_llm(state, phase=f"referee:{rounds+1}", system=system, user=user,
                            max_tokens=6000, temperature=0.2)
    report = extract_json(raw)
    if not isinstance(report, dict) or "verdict" not in report:
        return {"status": "error", "error": "审读官没有返回合法 JSON", "raw": clip(raw, 1500)}
    rounds += 1
    state.hook_state["writing_referee_rounds"] = rounds
    report["round"] = rounds
    report["mechanical_hard_lines_passed"] = bool(hl.get("passed"))
    # 结论绑定它读的那一版 PDF：改稿重渲染后这份结论就不再算数（验收按交付的 PDF 算）
    report["pdf_sha256"] = _pdf_sha(render.get("pdf_path"))
    report["pages"] = len(pages)
    write_json(paper_dir(state) / "referee_report.json", report)
    comments = report.get("comments") or []
    by_sev = {s: sum(1 for c in comments if c.get("severity") == s) for s in ("critical", "major", "minor")}
    state.append_transcript("writing_referee", round=rounds, verdict=report.get("verdict"), **by_sev)
    return {"status": "success", "round": rounds, "verdict": report.get("verdict"), "scores": report.get("scores"),
            "hard_lines": report.get("hard_lines"), "n_comments": by_sev, "comments": comments[:25],
            "summary": report.get("summary"), "rounds_left": MAX_ROUNDS - rounds,
            "next": ("verdict 为 accept 且硬线全过 → submit_manuscript。verdict 为 minor：按 comments 改完、render_manuscript 后"
                     "必须再 referee_review 一次拿到对新 PDF 的结论才能提交（结论绑定它读的那版 PDF）；"
                     "轮次用完还想改，只能 force 提交并在备注里写明。major/reject：逐条 revise_section → render → referee_review。")}


register_tool(
    ToolDefinition(
        name="referee_review",
        description=(
            "请独立审读官读渲染出的 PDF（逐页文字），按体裁审读规则判硬线、打六个软维、给带页码的意见。"
            "最多三轮。verdict 是 accept/minor 才能提交。"
        ),
        parameters_schema={"type": "object", "properties": {"focus": {"type": "string", "description": "本轮想让审读官重点看的地方（可选）"}}},
        allowed_node_types=["writing"],
    ),
    _referee_review,
)

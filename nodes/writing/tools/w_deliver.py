"""作者备注与提交：账本走备注，成品走 manuscript 记录。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

from .w_brief import load_brief, load_outline
from .w_common import manuscript_dir, paper_dir, read_json

NOTES_SECTIONS = ["数据与稿件来源", "待作者补充", "缺口与未兑现", "图表事项", "未采纳的审读意见", "AI 参与说明"]


async def _write_author_notes(state: Any, markdown: str, **_: Any) -> dict:
    text = markdown.strip() + "\n"
    missing = [s for s in NOTES_SECTIONS if s not in text]
    path = paper_dir(state) / "author_notes.md"
    path.write_text(text, encoding="utf-8")
    state.append_transcript("writing_author_notes", chars=len(text), missing_sections=missing)
    return {"status": "success", "path": str(path), "missing_sections": missing,
            "note": "备注随包交付、不进 PDF；上面缺的小节没有内容也请写一句「无」。"}


async def _submit_manuscript(state: Any, force: bool = False, reason: str = "", **_: Any) -> dict:
    from .w_referee import _pdf_sha
    brief = load_brief(state)
    render = read_json(paper_dir(state) / "render_report.json") or {}
    referee = read_json(paper_dir(state) / "referee_report.json") or {}
    notes = paper_dir(state) / "author_notes.md"
    blockers = []
    if not brief:
        blockers.append("没有简报")
    if render.get("status") != "success":
        blockers.append("最近一次渲染没有成功")
    hl = render.get("hard_lines") or {}
    if not hl.get("passed"):
        blockers.append("机械硬线没过：" + ", ".join(k for k, v in hl.items() if k.startswith("H") and not v.get("passed")))
    if not referee:
        blockers.append("没有审读报告")
    elif referee.get("verdict") not in ("accept", "minor"):
        blockers.append(f"审读结论是 {referee.get('verdict')}")
    else:
        from .w_referee import MAX_ROUNDS
        current = _pdf_sha(render.get("pdf_path"))
        reviewed = referee.get("pdf_sha256")
        if not reviewed or reviewed != current:
            rounds = int(referee.get("round") or 0)
            left = MAX_ROUNDS - rounds
            blockers.append(
                f"审读结论（第 {rounds} 轮，{referee.get('verdict')}）针对的是另一版 PDF，改稿重渲染后没再审读；"
                + (f"再 referee_review 一次（还剩 {left} 轮）拿到对当前 PDF 的结论" if left > 0
                   else "轮次已用完：要么不再改稿，要么 force 提交并在备注里写明哪些改动未经审读"))
    if not notes.is_file():
        blockers.append("没有作者备注（write_author_notes）")
    if blockers and not (force and reason.strip()):
        return {"status": "error", "error": "不能提交：" + "；".join(blockers),
                "hint": "修完再来；确有理由跳过时传 force=true 并写 reason（会记进备注与记录）"}
    mdir = manuscript_dir(state)
    main = (mdir / "main.tex").read_text(encoding="utf-8") if (mdir / "main.tex").is_file() else ""
    parts = []
    for p in sorted((mdir / "sections").glob("*.tex")):
        parts.append(f"% === included file: sections/{p.name} ===\n{p.read_text(encoding='utf-8')}")
    bib = (mdir / "refs.bib").read_text(encoding="utf-8") if (mdir / "refs.bib").is_file() else ""
    content = main + "\n\n" + "\n\n".join(parts) + "\n\n% === refs.bib ===\n" + bib
    pdf_path = render.get("pdf_path")
    metadata = {
        "format": "latex", "genre": brief.get("genre"), "title": brief.get("title"),
        "pdf_path": pdf_path, "pages": render.get("pages"), "hard_lines_passed": bool(hl.get("passed")),
        "referee_verdict": referee.get("verdict"), "referee_round": referee.get("round"),
        "referee_pdf_matches": bool(referee.get("pdf_sha256")) and referee.get("pdf_sha256") == _pdf_sha(render.get("pdf_path")),
        "referee_scores": referee.get("scores"), "author_notes_path": str(notes) if notes.is_file() else None,
        "sections": [p.stem for p in sorted((mdir / "sections").glob("*.tex"))],
        "bibliography_entries": (hl.get("H5_bibliography") or {}).get("n_entries"),
        "forced": bool(force), "force_reason": reason or None,
        "deliverable": True,
    }
    rec = state.save_artifact("manuscript", brief.get("title") or "manuscript", content, metadata=metadata)
    state.append_transcript("writing_submitted", artifact_id=rec.get("id") if isinstance(rec, dict) else None,
                            forced=bool(force), pdf=pdf_path)
    return {"status": "success", "artifact": rec, "pdf_path": pdf_path, "author_notes": str(notes),
            "note": "成品已登记；接下来结束本轮，把 PDF 路径和作者备注路径写进收尾文字。"}


register_tool(
    ToolDefinition(
        name="write_author_notes",
        description=(
            "写作者备注（Markdown）。这是给作者看的第二份文档，所有不该进论文正文的东西都放这里："
            "数据与稿件来源、待作者补充（作者、机构、基金、致谢、利益冲突）、缺口与未兑现、图表事项、"
            "未采纳的审读意见、AI 参与说明。六个小节都要有。"
        ),
        parameters_schema={"type": "object", "properties": {"markdown": {"type": "string"}}, "required": ["markdown"]},
        allowed_node_types=["writing"],
    ),
    _write_author_notes,
)

register_tool(
    ToolDefinition(
        name="submit_manuscript",
        description=(
            "提交成品：要求最近一次渲染成功、六条机械硬线全过、审读结论为 accept 或 minor、作者备注已写。"
            "登记 manuscript 记录（源码快照 + 元数据含 PDF 路径）。不满足时报错列出缺什么；确有理由可 force 并写 reason。"
        ),
        parameters_schema={"type": "object", "properties": {"force": {"type": "boolean"}, "reason": {"type": "string"}}},
        allowed_node_types=["writing"],
    ),
    _submit_manuscript,
)

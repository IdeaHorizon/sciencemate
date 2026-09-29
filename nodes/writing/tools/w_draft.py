"""逐节起草与修订：每节一次有界 LLM 调用，正文落盘，主循环只拿收据。"""
from __future__ import annotations

import json
import re
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

from .w_brief import load_brief, load_outline
from .w_common import (
    PART_CRAFT, bib_keys, bounded_llm, cite_keys, clip, craft, fragment_problems,
    figure_text_sample, graphics_files, load_genre, manuscript_dir, normalize_math_letters, worktree_root,
)
from .w_dossier import build_dossier

_MAX_EVIDENCE_CHARS = 30000


def _part_spec(genre: dict, part: str) -> dict:
    return (genre["structure"].get("parts") or {}).get(part) or {}


def _outline_part(outline: dict | None, part: str) -> dict | None:
    for p in (outline or {}).get("parts") or []:
        if str(p.get("id")) == part:
            return p
    return None


def _evidence_ids(op: dict | None) -> list[str]:
    ids: list[str] = []
    if not op:
        return ids
    for sub in op.get("subsections") or [op]:
        for c in sub.get("claims") or []:
            for e in c.get("evidence") or []:
                if e not in ids:
                    ids.append(str(e))
    return ids


def _sections_state(state: Any) -> list[dict]:
    d = manuscript_dir(state) / "sections"
    out = []
    for p in sorted(d.glob("*.tex")):
        t = p.read_text(encoding="utf-8")
        heads = re.findall(r"\\(?:sub)*section\{([^}]*)\}", t)
        out.append({"part": p.stem, "chars": len(t), "headings": heads[:12],
                    "labels": re.findall(r"\\label\{([^}]*)\}", t)})
    return out


def _figure_env_mismatches(tex: str, outline: dict | None, dossier: dict) -> list[str]:
    """图环境里 label 对应的提纲图键，其 source 文件必须就是 \\includegraphics 放的文件。"""
    from pathlib import Path as _P
    id_to_name = {f["id"]: f["name"] for f in dossier.get("figures", [])}
    by_key: dict[str, str] = {}
    for f in (outline or {}).get("figures") or []:
        src = f.get("file") or f.get("source")
        if src and src != "planned":
            by_key[str(f.get("key"))] = _P(id_to_name.get(src, src)).stem
    out: list[str] = []
    for env in re.finditer(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", tex, re.S):
        body = env.group(1)
        files = [_P(x).stem for x in graphics_files(body)]
        labels = re.findall(r"\\label\{([^}]*)\}", body)
        if files and not labels:
            out.append(f"图环境（文件 {files}）没有 \\label，无法与提纲的图键对上")
            continue
        for lab in labels:
            want = by_key.get(lab)
            if want and files and want not in files:
                out.append(f"图 {lab} 在提纲里的文件是 {want}，片段里放的却是 {files}：图题说的必须是文件里画的东西；改片段或先改提纲")
    return out


def _figure_table_text(outline: dict | None, available: list[dict], fig_dirs: list | None = None) -> str:
    lines = ["## 可用的图（只能用这些文件名，放在 figures/ 下）"]
    names = {f["name"]: f for f in available}
    for f in (outline or {}).get("figures") or []:
        src = f.get("file") or f.get("source")
        fname = None
        if src in names:
            fname = src
        else:
            for a in available:
                if a["id"] == src:
                    fname = a["name"]
        if fname and fname.lower().endswith(".svg"):
            fname = fname[:-4] + ".pdf"   # 框架会把 svg 转成 pdf 再放进 figures/
        lines.append(f"- {f.get('key')}: file={fname or '（尚无文件，先不放图，用文字说明）'} · 信息：{f.get('message')} · 图题：{f.get('caption') or ''}")
    others = [a for a in available if a["name"].lower().endswith((".png", ".pdf", ".jpg"))]
    if others:
        lines.append("## 目前盘上全部可用的图文件（svg 已按同名 .pdf 提供）。「记录」是图表服务铸的图题，说明这个文件画的是什么；"
                     "你写的图题必须说同一张图，放错文件就是张冠李戴")
        from pathlib import Path as _P
        for a in sorted(others, key=lambda x: x["name"])[:60]:
            cap = (a.get("caption") or "").replace("\n", " ")
            if cap:
                lines.append(f"- {a['name']} · 记录：{cap[:180]}")
            else:
                sample = figure_text_sample(_P(a["name"]).stem, [_P(d) for d in (fig_dirs or []) if d], 160)
                lines.append(f"- {a['name']}" + (f" · 无记录；图内文字：{sample}" if sample else " · 无记录（用户交来的原图，按提纲里的用途放）"))
    lines.append("## 计划的表")
    for t in (outline or {}).get("tables") or []:
        lines.append(f"- {t.get('key')}: 来源 {t.get('source')} · 表题：{t.get('caption') or ''}")
    return "\n".join(lines)


async def _compose(state: Any, part: str, notes: str, *, existing: str | None,
                   instructions: str | None) -> dict:
    brief = load_brief(state)
    if not brief:
        return {"status": "error", "error": "先 write_brief"}
    outline = load_outline(state)
    if not outline:
        return {"status": "error", "error": "先 write_outline"}
    genre = load_genre(brief["genre"])
    spec = _part_spec(genre, part)
    if not spec and part not in ("appendix",):
        return {"status": "error", "error": f"部件 {part!r} 不在体裁 {brief['genre']} 里"}
    op = _outline_part(outline, part)
    if op is None and part != "abstract":
        return {"status": "error", "error": f"提纲里没有部件 {part!r}，先补提纲"}
    dossier = build_dossier(state)
    entries = dossier.get("entries", {})
    ev_ids = _evidence_ids(op)
    if part == "abstract":
        # 摘要只看已起草的各节
        ev_text = ""
    else:
        chunks = []
        for i in ev_ids:
            e = entries.get(i)
            if e:
                chunks.append(f"### {i} · {e.get('path_titles') or e.get('heading') or e['kind']}\n{e.get('text') or ''}")
        ev_text = clip("\n\n".join(chunks), _MAX_EVIDENCE_CHARS)
    mdir = manuscript_dir(state)
    bib_text = (mdir / "refs.bib").read_text(encoding="utf-8") if (mdir / "refs.bib").is_file() else ""
    keys = bib_keys(bib_text)
    key_lines = "\n".join(f"- {k}: {t[:80]}" for k, t in keys.items()) or "（参考文献尚未写入，本节暂不引用；先 write_bibliography）"
    drafted = _sections_state(state)
    drafted_text = "\n".join(f"- {s['part']}: {s['chars']} 字符；标题 {s['headings']}；labels {s['labels']}" for s in drafted) or "（尚无）"
    drafted_text += "\n\\ref 只能引用上面列出的 label 或你在本片段里新定义的 label；引用不存在的 label 会渲染成 ??。"
    lang = brief.get("language") or {}
    system = "\n\n".join(x for x in [
        f"你是这篇稿子的作者，正在写「{part}」这一部件。语言：正文{lang.get('body','zh')}，图内文字{lang.get('figures','en')}。",
        craft(PART_CRAFT.get(part, "introduction")),
        genre["style"],
        craft("latex_conventions"),
    ] if x)
    moves = "\n".join(f"- {m}" for m in (spec.get("moves") or []))
    forbid = "；".join(spec.get("forbid") or [])
    user_parts = [
        f"# 稿件\n标题：{brief.get('title')}\n体裁：{brief.get('genre')}（变体 {brief.get('structure_variant')}）",
        f"# 这一部件的要求\n标题：{spec.get('heading') or '（摘要，无标题）'}；长度 {spec.get('chars')} 字\n动作：\n{moves}\n禁止：{forbid}",
        f"# 用户的硬性要求\n" + "\n".join(f"- {m.get('id')}: {m.get('text')}" for m in brief.get("must_haves") or []),
        f"# 提纲（本部件）\n{json.dumps(op, ensure_ascii=False, indent=1) if op else '（摘要：概括全文）'}",
        _figure_table_text(outline, dossier.get("figures", []),
                           [mdir / "figures", (worktree_root(state) / "figures") if worktree_root(state) else None]),
        f"# 参考文献 key（只能用这些）\n{key_lines}",
        f"# 已起草的其他部件（保持术语与编号一致）\n{drafted_text}",
    ]
    if part == "abstract":
        alltext = []
        for s in drafted:
            t = (mdir / "sections" / f"{s['part']}.tex").read_text(encoding="utf-8")
            alltext.append(f"## {s['part']}\n{clip(t, 6000)}")
        user_parts.append("# 已起草的正文（据此写摘要）\n" + "\n\n".join(alltext))
    else:
        user_parts.append(f"# 证据（卷宗条目全文，数字只能来自这里）\n{ev_text or '（本部件不需要证据条目）'}")
    if existing is not None:
        user_parts.append(f"# 当前版本（整段重写并返回完整片段）\n{existing}")
    if instructions:
        user_parts.append(f"# 修订指令\n{instructions}")
    if notes:
        user_parts.append(f"# 作者附注\n{notes}")
    user_parts.append("只输出这一部件的 LaTeX 片段。")
    user = "\n\n".join(user_parts)
    max_tokens = 2500 if part == "abstract" else 7000
    tex = await bounded_llm(state, phase=f"draft:{part}", system=system, user=user,
                            max_tokens=max_tokens, temperature=0.4)
    if not tex.strip():
        return {"status": "error", "error": "模型没有返回内容"}
    tex, n_math = normalize_math_letters(tex)
    problems = fragment_problems(tex)
    if n_math:
        problems.append(f"片段里有 {n_math} 个 Unicode 数学字母（如 𝑇），已改成普通字母；数学变量请写 $T_A$ 这种形式")
    my_labels = set(re.findall(r"\\label\{([^}]*)\}", tex))
    known_labels = my_labels | {lab for s in drafted if s["part"] != part for lab in s["labels"]}
    refs_used = re.findall(r"\\(?:ref|eqref|autoref|cref|Cref|pageref)\{([^}]*)\}", tex)
    unknown_labels = sorted({r.strip() for r in refs_used if r.strip() not in known_labels})
    if unknown_labels:
        problems.append(f"引用了不存在的 label {unknown_labels}（渲染会出 ??）；已有 label：{sorted(known_labels)[:40]}")
    problems.extend(_figure_env_mismatches(tex, outline, dossier))
    used = cite_keys(tex)
    unknown_keys = sorted({k for k in used if k not in keys})
    gfx = graphics_files(tex)
    available_names = {f["name"] for f in dossier.get("figures", [])} | {p.name for p in (mdir / "figures").glob("*")}
    unknown_gfx = sorted({g for g in gfx if g.split("/")[-1] not in available_names})
    path = mdir / "sections" / f"{part}.tex"
    path.write_text(tex.strip() + "\n", encoding="utf-8")
    state.append_transcript("writing_section_drafted", part=part, chars=len(tex),
                            cites=len(used), unknown_keys=unknown_keys, unknown_graphics=unknown_gfx,
                            problems=problems, revised=existing is not None)
    return {
        "status": "success" if not (problems or unknown_keys or unknown_gfx or unknown_labels) else "success_with_issues",
        "part": part, "path": str(path.relative_to(mdir.parent)), "chars": len(tex),
        "headings": re.findall(r"\\(?:sub)*section\{([^}]*)\}", tex)[:12],
        "cites_used": sorted(set(used)), "unknown_cite_keys": unknown_keys,
        "graphics": gfx, "unknown_graphics": unknown_gfx, "unknown_labels": unknown_labels, "problems": problems,
        "preview": clip(tex, 500),
    }


async def _draft_section(state: Any, part: str, notes: str = "", **_: Any) -> dict:
    return await _compose(state, part, notes, existing=None, instructions=None)


async def _revise_section(state: Any, part: str, instructions: str, **_: Any) -> dict:
    p = manuscript_dir(state) / "sections" / f"{part}.tex"
    if not p.is_file():
        return {"status": "error", "error": f"{part} 还没起草"}
    return await _compose(state, part, "", existing=p.read_text(encoding="utf-8"), instructions=instructions)


register_tool(
    ToolDefinition(
        name="draft_section",
        description=(
            "起草一个部件（introduction / methods / results / discussion / conclusion / background / abstract）。"
            "框架把该部件的技艺、体裁风格、提纲、证据条目全文、可用图表、参考文献 key 交给一次独立的模型调用，"
            "正文落到 paper/manuscript/sections/<part>.tex；返回收据（字数、引用、图、问题）。"
            "顺序建议：introduction → methods → results → discussion → conclusion → abstract（摘要最后写）。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {"part": {"type": "string"}, "notes": {"type": "string", "description": "给这一节的作者附注（可选）"}},
            "required": ["part"],
        },
        allowed_node_types=["writing"],
    ),
    _draft_section,
)

register_tool(
    ToolDefinition(
        name="revise_section",
        description="按修订指令重写一个已起草的部件（整段重写、落盘、返回收据）。用于自审与审读意见的落实。",
        parameters_schema={
            "type": "object",
            "properties": {"part": {"type": "string"}, "instructions": {"type": "string"}},
            "required": ["part", "instructions"],
        },
        allowed_node_types=["writing"],
    ),
    _revise_section,
)

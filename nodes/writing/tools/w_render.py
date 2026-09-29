"""渲染：框架装配 main.tex、放图、编译、抽页文字、算硬线。模型第一次看见自己的成品。"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

from .w_brief import load_brief, load_outline
from .w_common import (
    PROCESS_VOCAB, RENDERERS_DIR, bib_keys, cite_keys, clip, load_genre, manuscript_dir,
    paper_dir, read_json, write_json, worktree_root,
)
from .w_dossier import build_dossier

_EMPTY_REF_PATTERNS = [
    (r"第\s+节", "第 节"), (r"第\s*节的", "第节的"), (r"(?<![图表])图\s+(?=[所与和、，。])", "图 所示"),
    (r"表\s+(?=[所与和、，。])", "表 所示"), (r"\?\?", "??"), (r"\[\?\]", "[?]"),
]


def _tex_escape(s: str) -> str:
    return s.replace("&", r"\&").replace("%", r"\%").replace("_", r"\_")


def _authors_block(brief: dict) -> str:
    authors = brief.get("authors") or []
    if not authors:
        return ""
    parts = []
    for a in authors:
        if isinstance(a, dict):
            name = _tex_escape(str(a.get("name", "")))
            aff = a.get("affiliation")
            parts.append(name + (rf"\thanks{{{_tex_escape(str(aff))}}}" if aff else ""))
        else:
            parts.append(_tex_escape(str(a)))
    return r" \and ".join(parts)


def _convert_svg(src: Path, dst_pdf: Path) -> bool:
    for cmd in (["rsvg-convert", "-f", "pdf", "-o", str(dst_pdf), str(src)],
                ["inkscape", str(src), "--export-type=pdf", f"--export-filename={dst_pdf}"]):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode == 0 and dst_pdf.is_file():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _stage_figures(state: Any, outline: dict | None) -> dict:
    """把提纲里用到的图从卷宗位置复制到 manuscript/figures/（svg 转 pdf）。"""
    root = worktree_root(state)
    dossier = build_dossier(state)
    by_id = {f["id"]: f for f in dossier.get("figures", [])}
    by_name = {f["name"]: f for f in dossier.get("figures", [])}
    for f in dossier.get("figures", []):
        by_name.setdefault(f["name"].rsplit(".", 1)[0], f)   # 不带后缀也认（优先 pdf，字典序）
    mdir = manuscript_dir(state)
    staged, missing, converted = [], [], []
    wanted = []
    for f in (outline or {}).get("figures") or []:
        src = f.get("file") or f.get("source")
        if src and src != "planned":
            wanted.append(src)
    # 也把 sections 里 includegraphics 引用的文件收进来
    for p in (mdir / "sections").glob("*.tex"):
        for g in re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", p.read_text(encoding="utf-8")):
            wanted.append(g.split("/")[-1])
    for src in dict.fromkeys(wanted):
        f = by_id.get(src) or by_name.get(src) or by_name.get(Path(src).name)
        if not f or root is None:
            # 已在 figures/ 目录里的也算
            if (mdir / "figures" / Path(src).name).is_file():
                staged.append(Path(src).name)
            else:
                missing.append(src)
            continue
        s = root / f["path"]
        if f["format"] == "svg":
            dst = mdir / "figures" / (Path(f["name"]).stem + ".pdf")
            if not dst.is_file():
                if _convert_svg(s, dst):
                    converted.append(dst.name)
                else:
                    missing.append(f"{src}（svg 转 pdf 失败）")
                    continue
            staged.append(dst.name)
        else:
            dst = mdir / "figures" / f["name"]
            if not dst.is_file():
                shutil.copy2(s, dst)
            staged.append(dst.name)
    # 机械修正：片段里引用的 .svg 一律改成转换后的 .pdf（LaTeX 装不了 svg，这不该让模型知道）；
    # .png 若有同名 .pdf（图表服务通常两种都交），也换成 pdf：矢量印刷清晰，H9 才量得到字号
    pdf_twins = {Path(f["name"]).stem for f in dossier.get("figures", []) if f["format"] == "pdf"}
    for stem in pdf_twins:
        f = by_name.get(stem + ".pdf")
        if f and root is not None and not (mdir / "figures" / (stem + ".pdf")).is_file():
            shutil.copy2(root / f["path"], mdir / "figures" / (stem + ".pdf"))
    rewritten = []
    for p in (mdir / "sections").glob("*.tex"):
        t = p.read_text(encoding="utf-8")
        t2 = re.sub(r"(\\includegraphics(?:\[[^\]]*\])?\{[^}]*?)\.svg\}", r"\1.pdf}", t)
        t2 = re.sub(r"(\\includegraphics(?:\[[^\]]*\])?\{(?:[^}]*/)?)([^}/]+)\.png\}",
                    lambda m: f"{m.group(1)}{m.group(2)}.pdf}}" if m.group(2) in pdf_twins else m.group(0), t2)
        # 高度兜底：没写 height 的加 height=0.8\textheight,keepaspectratio（iter6 实测：竖长条图把图题挤出页面）
        def _cap_height(m: "re.Match[str]") -> str:
            opts = m.group(1) or ""
            if "height" in opts:
                return m.group(0)
            extra = "height=0.8\\textheight,keepaspectratio"
            opts = f"{opts},{extra}" if opts else extra
            return f"\\includegraphics[{opts}]{{{m.group(2)}}}"
        t2 = re.sub(r"\\includegraphics(?:\[([^\]]*)\])?\{([^}]*)\}", _cap_height, t2)
        if t2 != t:
            p.write_text(t2, encoding="utf-8")
            rewritten.append(p.name)
    return {"staged": staged, "missing": missing, "converted": converted, "svg_rewritten_in": rewritten}


_TABULAR_RE = re.compile(r"(\\begin\{tabular\}.*?\\end\{tabular\})", re.S)


def _fit_tables(mdir: Path) -> int:
    """表格超出版心是排版问题，不是作者的问题：所有 tabular 机械包进 adjustbox 限宽（幂等）。"""
    n = 0
    for p in (mdir / "sections").glob("*.tex"):
        t = p.read_text(encoding="utf-8")
        if "\\begin{adjustbox}" in t:
            continue
        t2, k = _TABULAR_RE.subn(r"\\begin{adjustbox}{max width=\\linewidth}\1\\end{adjustbox}", t)
        if k:
            p.write_text(t2, encoding="utf-8")
            n += k
    return n


def _assemble(state: Any, brief: dict, genre: dict) -> Path:
    mdir = manuscript_dir(state)
    _fit_tables(mdir)
    renderer = (genre["structure"].get("renderer") or "zh_article")
    tmpl = (RENDERERS_DIR / renderer / "main.tex.tmpl").read_text(encoding="utf-8")
    variants = genre["structure"].get("variants") or {}
    parts = variants.get(brief.get("structure_variant") or "", {}).get("parts") or genre["structure"].get("required_parts") or []
    includes = []
    for pid in parts:
        if pid == "abstract":
            continue
        if (mdir / "sections" / f"{pid}.tex").is_file():
            includes.append(rf"\input{{sections/{pid}}}")
    appendix = r"\appendix" + "\n" + r"\input{sections/appendix}" if (mdir / "sections" / "appendix.tex").is_file() else ""
    if not (mdir / "sections" / "abstract.tex").is_file():
        (mdir / "sections" / "abstract.tex").write_text("", encoding="utf-8")
    if not (mdir / "refs.bib").is_file():
        (mdir / "refs.bib").write_text("", encoding="utf-8")
    main = (tmpl.replace("<<TITLE>>", _tex_escape(brief.get("title") or ""))
            .replace("<<AUTHORS>>", _authors_block(brief))
            .replace("<<SECTIONS>>", "\n".join(includes))
            .replace("<<APPENDIX>>", appendix))
    (mdir / "main.tex").write_text(main, encoding="utf-8")
    return mdir / "main.tex"


def _pdf_pages(pdf: Path) -> list[str]:
    try:
        r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            pages = r.stdout.split("\f")
            return [p for p in pages if p.strip()]
    except Exception:  # noqa: BLE001
        pass
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(pdf))
        return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    except Exception:  # noqa: BLE001
        return []


def _log_text(state: Any, output_name: str) -> str:
    from core import paths
    try:
        scratch = paths.latex_scratch_dir(state, output_name=output_name)
        for cand in (scratch / "main.log", scratch / f"{output_name}.log"):
            if cand.is_file():
                return cand.read_text(encoding="utf-8", errors="replace")
        logs = sorted(scratch.rglob("*.log"), key=lambda p: p.stat().st_mtime)
        return logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""
    except Exception:  # noqa: BLE001
        return ""


def hard_lines(state: Any, pages: list[str], log_text: str, main_pdf: Path | None = None) -> dict:
    brief = load_brief(state) or {}
    mdir = manuscript_dir(state)
    whole = "\n".join(pages)
    norm = re.sub(r"\s+", "", whole)
    out: dict[str, Any] = {}
    # H1 标题块
    first = pages[0] if pages else ""
    lines = [l.strip() for l in first.splitlines() if l.strip()]
    before_abs = []
    for l in lines:
        if re.match(r"^(摘\s*要|Abstract)\b", l):
            break
        before_abs.append(l)
    title_norm = re.sub(r"\s+", "", brief.get("title") or "")
    out["H1_title_block"] = {"passed": bool(before_abs) and (not title_norm or title_norm[:12] in norm[:600]),
                            "first_lines": lines[:3]}
    # H2 过程词汇
    hits = []
    for i, p in enumerate(pages, start=1):
        for kw in PROCESS_VOCAB:
            for m in re.finditer(re.escape(kw), p, re.I):
                s = p[max(0, m.start() - 30): m.end() + 30].replace("\n", " ")
                hits.append({"page": i, "word": kw, "context": s})
                if len(hits) > 40:
                    break
    out["H2_process_vocab"] = {"passed": not hits, "hits": hits[:20], "n": len(hits)}
    # H3 空引用 + 编译警告
    empty = []
    for i, p in enumerate(pages, start=1):
        for pat, label in _EMPTY_REF_PATTERNS:
            for m in re.finditer(pat, p):
                empty.append({"page": i, "pattern": label, "context": p[max(0, m.start()-25): m.end()+25].replace("\n", " ")})
    undefined_refs = re.findall(r"LaTeX Warning: Reference `([^']+)' on page", log_text)
    undefined_cites = re.findall(r"LaTeX Warning: Citation `([^']+)' on page", log_text)
    # 手写的「第 N 节」指向不存在的章节（模型不知道渲染后的编号）
    # 一级标题行：短行、编号后跟非数字；表格里的「40   …」也会长这样，所以只取从 1 起连续的那一段
    top_nums = {int(n) for n, rest in re.findall(r"^\s*(\d{1,2})\s{2,}([^\d\s][^\n]{0,30})$", whole, re.M)}
    max_sec = 0
    while (max_sec + 1) in top_nums:
        max_sec += 1
    bad_literal = []
    for i, p in enumerate(pages, start=1):
        for m in re.finditer(r"第\s*(\d{1,2})(?:\.(\d{1,2}))?\s*节", p):
            n = int(m.group(1))
            if max_sec and n > max_sec:
                bad_literal.append({"page": i, "text": m.group(0), "max_section": max_sec})
    # 把位置送到写手手里：每个未定义的 label / key 在哪个部件里被引用（iter9 实测写手改错了两次部件）
    def _where(pattern: str) -> list[str]:
        hits = []
        for p_ in sorted((mdir / "sections").glob("*.tex")):
            if re.search(pattern, p_.read_text(encoding="utf-8")):
                hits.append(p_.stem)
        return hits
    undefined_ref_where = {lab: _where(r"\\(?:ref|eqref|autoref|cref|Cref|pageref)\{" + re.escape(lab) + r"\}")
                           for lab in sorted(set(undefined_refs))[:20]}
    undefined_cite_where = {k: _where(r"\\(?:cite|citep|citet|supercite|parencite|textcite)\{[^}]*\b" + re.escape(k) + r"\b")
                            for k in sorted(set(undefined_cites))[:20]}
    out["H3_references_resolved"] = {"passed": not (empty or undefined_refs or undefined_cites or bad_literal),
                                     "empty_in_pdf": empty[:20], "undefined_refs": sorted(set(undefined_refs))[:20],
                                     "undefined_refs_in_parts": undefined_ref_where,
                                     "undefined_cites": sorted(set(undefined_cites))[:20],
                                     "undefined_cites_in_parts": undefined_cite_where,
                                     "literal_section_out_of_range": bad_literal[:10],
                                     "hint": "按 *_in_parts 指的部件 revise_section；label 被拆/改名后要把所有引用它的部件都改掉。"}
    # H4 图表编号连续且被引用
    def _seq(label: str) -> dict:
        nums = sorted({int(n) for n in re.findall(rf"{label}\s*(\d{{1,2}})(?![\d.])", whole)})
        cap_nums = sorted({int(n) for n in re.findall(rf"^\s*{label}\s*(\d{{1,2}})(?:[:：]|\s+\S)", whole, re.M)})
        contiguous = nums == list(range(1, len(nums) + 1)) if nums else True
        mentions = {n: len(re.findall(rf"{label}\s*{n}(?![\d.])", whole)) for n in cap_nums}
        unreferenced = [n for n, c in mentions.items() if c < 2]
        # 正文提到了「图 N」却没有「图 N ……」的图题行：浮动体没排出来（超页）或图题被挤出页外
        no_caption = [n for n in nums if n not in cap_nums]
        return {"numbers": nums, "captions": cap_nums, "contiguous": contiguous, "unreferenced": unreferenced,
                "no_caption": no_caption}
    figs, tabs = _seq("图"), _seq("表")
    out["H4_figures_tables"] = {"passed": figs["contiguous"] and tabs["contiguous"] and not figs["unreferenced"]
                                          and not tabs["unreferenced"] and not figs["no_caption"] and not tabs["no_caption"],
                                "figures": figs, "tables": tabs}
    # H5 参考文献：每篇被引用、每个 cite 都在 bib 里、bad 为空
    bib = (mdir / "refs.bib").read_text(encoding="utf-8") if (mdir / "refs.bib").is_file() else ""
    keys = set(bib_keys(bib))
    used: set[str] = set()
    for p in (mdir / "sections").glob("*.tex"):
        used |= set(cite_keys(p.read_text(encoding="utf-8")))
    check = read_json(mdir / "bib_check.json") or {}
    bad = set(check.get("bad") or [])
    out["H5_bibliography"] = {"passed": bool(keys) and not (used - keys) and not (keys - used) and not (bad & used),
                              "n_entries": len(keys), "n_cited": len(used & keys), "unknown_cites": sorted(used - keys)[:20],
                              "uncited": sorted(keys - used)[:20], "unresolvable_cited": sorted(bad & used)[:20]}
    # H6 简报硬性要求（机械项）。验收按交付的 PDF 算：forbid 在正文、图题、矢量图内文字里命中都是命中
    # （iter5 实测：用户原图 svg 带 case6 标签被原样搬进稿子，正文干净、成品违约）。detail 指出命中在哪，
    # 在图里就得带 rename 约束重派图表服务。
    body_tex = "\n".join(p.read_text(encoding="utf-8") for p in sorted((mdir / "sections").glob("*.tex")))
    body_visible = re.sub(r"%[^\n]*", "", body_tex)
    body_visible = re.sub(r"\\(?:includegraphics|label|ref|cite|input)\{[^}]*\}", " ", body_visible)
    mh_results = []
    for m in brief.get("must_haves") or []:
        if str(m.get("check")) != "mechanical":
            continue
        rule = str(m.get("rule") or "")
        ok, details = True, []
        # 复合规则：用 ; 分隔多条；require= 里用 , 分隔多个都必须出现的片段
        for clause in [c.strip() for c in rule.split(";") if c.strip()]:
            if clause.startswith("refs>="):
                n = int(re.sub(r"\D", "", clause) or 0)
                c_ok = len(used & keys) >= n
                details.append(f"cited={len(used & keys)} need>={n}")
            elif clause.startswith("forbid="):
                pat = clause[len("forbid="):]
                try:
                    in_body = re.findall(pat, body_visible)
                    in_pdf = re.findall(pat, whole)
                except re.error:
                    in_body, in_pdf = [x for x in [pat] if pat in body_visible], [x for x in [pat] if pat in whole]
                c_ok = not in_body and not in_pdf
                where = ""
                if not in_body and in_pdf:
                    hit_pages = sorted({i for i, pg in enumerate(pages, start=1) if re.search(pat, pg)})
                    where = f"（正文干净；命中在图内文字里，页 {hit_pages[:8]}：带 rename 约束重派图表服务重画，不能用带旧名字的图）"
                details.append(f"forbid {pat}: in_body={len(in_body)} in_pdf_total={len(in_pdf)}{where}")
            elif clause.startswith("require="):
                pats = [x.strip() for x in clause[len("require="):].split(",") if x.strip()]
                missing = []
                for pat in pats:
                    try:
                        present = bool(re.search(pat, body_visible)) or bool(re.search(pat, whole))
                    except re.error:
                        present = pat in body_visible or pat in whole
                    if not present:
                        missing.append(pat)
                c_ok = not missing
                details.append("require: all present" if c_ok else f"require missing: {missing}")
            else:
                c_ok = True
                details.append(f"unknown clause {clause!r} (referee)")
            ok = ok and c_ok
        mh_results.append({"id": m.get("id"), "rule": rule, "passed": ok, "detail": "; ".join(details)})
    out["H6_must_haves"] = {"passed": all(r["passed"] for r in mh_results), "results": mh_results}
    # H7 版面：超出版心的行（表格超宽是常见形态）不得超过 15pt
    overs = [float(x) for x in re.findall(r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide", log_text)]
    bad_overs = [x for x in overs if x > 15.0]
    where = re.findall(r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\) (?:in (?:paragraph|alignment) at lines (\d+--\d+)|detected at line (\d+))", log_text)
    too_large = re.findall(r"Float too large for page by ([\d.]+)pt on input line (\d+)", log_text)
    out["H7_layout"] = {"passed": not bad_overs and not too_large, "overfull_gt15pt": len(bad_overs),
                        "worst_pt": max(overs) if overs else 0,
                        "locations": [f"{w[0]}pt @ lines {w[1] or w[2]}" for w in where if float(w[0]) > 15.0][:8],
                        "floats_too_large": [f"{a}pt @ line {b}" for a, b in too_large][:6],
                        "hint": "超宽多半是表格：减列、缩位数、长文字列用 p{}、整表 \\small，或拆表。"
                                "浮动体超页多半是竖长条的图（六面板竖着堆）：让图表服务按 3×2 横排重画，或拆成多张图。"}
    # H8 图内文字语言：简报说图内英文，就不许图里出现中日韩文字（从图 PDF 的矢量文字机械抽）
    lang_fig = str(((brief.get("language") or {}).get("figures")) or "").lower()
    fig_lang_hits: list[dict] = []
    if lang_fig == "en":
        root = worktree_root(state)
        # 只查稿子里实际 \includegraphics 的文件（暂存目录里会留下换掉的旧图，iter7 实测被旧图误判）
        used_stems = set()
        for p_ in (mdir / "sections").glob("*.tex"):
            for g in re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", p_.read_text(encoding="utf-8")):
                used_stems.add(Path(g).stem)
        for f in sorted((mdir / "figures").glob("*")):
            if f.stem not in used_stems:
                continue
            cand = f if f.suffix.lower() == ".pdf" else None
            if cand is None and root is not None:
                alt = list((root / "figures").glob(f.stem + ".pdf"))
                cand = alt[0] if alt else None
            if cand is None or not cand.is_file():
                continue
            try:
                txt = subprocess.run(["pdftotext", str(cand), "-"], capture_output=True, text=True, timeout=60).stdout
            except Exception:  # noqa: BLE001
                continue
            cjk = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", txt)
            if cjk:
                sample = re.findall(r"[\u4e00-\u9fff]{2,}", txt)[:6]
                fig_lang_hits.append({"file": f.name, "cjk_chars": len(cjk), "sample": sample})
    out["H8_figure_text_language"] = {"passed": not fig_lang_hits, "policy": lang_fig or "unspecified",
                                      "hits": fig_lang_hits[:10],
                                      "hint": "图内文字要英文：带着具体文件与中文片段重派图表服务。"}
    # H9 图内文字在印刷尺寸下可读：按 \includegraphics 的实际宽度缩放，取矢量文字高度的分布
    # （iter2 实测：六拓扑合成图中位数 6.1pt/最小 4.4pt 看不清；干净柱状图 9.7pt）
    TEXT_WIDTH_PT = 455.0
    TEXT_HEIGHT_PT = 700.0   # A4、2.5cm 边距
    raster_outline = (load_outline(state) or {}).get("figures") or []
    fig_leg: list[dict] = []
    incl: dict[str, float] = {}
    incl_h: dict[str, float] = {}   # height=X\textheight 的上限（keepaspectratio 时实际缩放取小的那个）
    for p in (mdir / "sections").glob("*.tex"):
        for m in re.finditer(r"\\includegraphics(?:\[([^\]]*)\])?\{([^}]*)\}", p.read_text(encoding="utf-8")):
            opts, path = m.group(1) or "", Path(m.group(2)).name
            frac = 1.0
            mw = re.search(r"width\s*=\s*([\d.]*)\s*\\(?:line|text|column)width", opts)
            if mw:
                frac = float(mw.group(1) or 1.0)
            else:
                mcm = re.search(r"width\s*=\s*([\d.]+)\s*(cm|mm|pt|in)", opts)
                if mcm:
                    val, unit = float(mcm.group(1)), mcm.group(2)
                    pts = val * {"cm": 28.45, "mm": 2.845, "pt": 1.0, "in": 72.27}[unit]
                    frac = pts / TEXT_WIDTH_PT
            incl[path] = frac
            mh = re.search(r"height\s*=\s*([\d.]*)\s*\\textheight", opts)
            if mh:
                incl_h[path] = float(mh.group(1) or 1.0)
    for name, frac in incl.items():
        fig_pdf = mdir / "figures" / (Path(name).stem + ".pdf")
        if not fig_pdf.is_file():
            # 位图量不到字号也查不了语言：只有提纲在这张图上写了 raster_ok（照片/截图这类必须用位图的）才放行
            ok_raster = any(bool(f.get("raster_ok")) and Path(str(f.get("file") or f.get("source") or "")).stem == Path(name).stem
                            for f in raster_outline)
            fig_leg.append({"file": name, "measured": False, "passed": ok_raster,
                            "note": ("位图，提纲标了 raster_ok" if ok_raster else
                                     "位图：字号与图内语言都核不了。数据图/示意图让图表服务重画并交 pdf（同名 pdf 会自动优先）；"
                                     "照片/截图这类必须用位图的，在提纲该图上写 raster_ok: true 并在 message 里说明")})
            continue
        try:
            info = subprocess.run(["pdfinfo", str(fig_pdf)], capture_output=True, text=True, timeout=30).stdout
            m = re.search(r"Page size:\s+([\d.]+) x ([\d.]+)", info)
            native_w = float(m.group(1)) if m else None
            native_h = float(m.group(2)) if m else None
            bbox = subprocess.run(["pdftotext", "-bbox", str(fig_pdf), "-"], capture_output=True, text=True, timeout=60).stdout
            hs = [float(b) - float(a) for a, b in re.findall(r'yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)"', bbox)]
            hs = sorted(x for x in hs if x > 0.5)
        except Exception:  # noqa: BLE001
            continue
        if not native_w or not hs:
            fig_leg.append({"file": name, "measured": False, "note": "图里没有矢量文字（可能是位图或无文字）"})
            continue
        scale = frac * TEXT_WIDTH_PT / native_w
        if name in incl_h and native_h:
            scale = min(scale, incl_h[name] * TEXT_HEIGHT_PT / native_h)   # 竖长条图由高度决定缩放
        median = hs[len(hs) // 2] * scale
        p10 = hs[len(hs) // 10] * scale
        ok = median >= 7.0 and p10 >= 6.0   # 审读官实测 p10=5.4 的合成图里 GPU 编号已不可读
        fig_leg.append({"file": name, "measured": True, "width_frac": round(frac, 2), "scale": round(scale, 3),
                        "median_text_pt": round(median, 1),
                        "p10_text_pt": round(p10, 1), "passed": ok})
    out["H9_figure_legibility"] = {"passed": all(x.get("passed", True) for x in fig_leg), "figures": fig_leg,
                                   "hint": "中位数<7pt 的图在印刷尺寸下看不清：拆面板、加大字号、或缩小合成规模后重派；"
                                           "位图没法核，用图表服务重画交 pdf。"}
    # H10 作者源码里没有字体渲染不了的字：Unicode 数学字母（𝑇、𝐴）、U+FFFD、私用区。
    # 不拿 pdftotext 的 U+FFFD 判（iter6 实测：规范的 $T_B$ 数学斜体也会被抽成 U+FFFD，假阳性）；
    # 日志里的 Missing character 才是字体真缺字。
    moji = []
    for p in sorted((mdir / "sections").glob("*.tex")):
        t = p.read_text(encoding="utf-8")
        for m in re.finditer(r"[\U0001D400-\U0001D7FF\ufffd\ue000-\uf8ff]", t):
            moji.append({"part": p.stem, "char": m.group(0), "codepoint": f"U+{ord(m.group(0)):04X}",
                         "context": t[max(0, m.start() - 30): m.end() + 20].replace("\n", " ")})
            if len(moji) > 12:
                break
    missing_chars = re.findall(r"Missing character: There is no (\S+) in font", log_text)
    out["H10_no_mojibake"] = {"passed": not moji and not missing_chars, "hits": moji[:12],
                              "missing_in_font": sorted(set(missing_chars))[:12],
                              "pdf_replacement_chars": sum(p.count("\ufffd") for p in pages),
                              "hint": "数学变量写成 $T_A$ 这种 LaTeX 数学，不要贴 Unicode 数学字母；missing_in_font 列的字换成字体有的写法。"}
    # H11 图文件与提纲图键一致（审读官看不见图，靠这道对账挡住张冠李戴）
    outline = load_outline(state) or {}
    dossier = read_json(paper_dir(state) / "dossier.json") or {}
    id_to_name = {f["id"]: f["name"] for f in dossier.get("figures", [])}
    by_key: dict[str, str] = {}
    for f in outline.get("figures") or []:
        src = f.get("file") or f.get("source")
        if src and src != "planned":
            by_key[str(f.get("key"))] = Path(id_to_name.get(src, src)).stem
    fig_issues: list[dict] = []
    seen_files: dict[str, str] = {}
    for p in sorted((mdir / "sections").glob("*.tex")):
        t = p.read_text(encoding="utf-8")
        for env in re.finditer(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", t, re.S):
            body = env.group(1)
            files = [Path(x).stem for x in re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", body)]
            labels = re.findall(r"\\label\{([^}]*)\}", body)
            if files and not labels:
                fig_issues.append({"part": p.stem, "files": files, "issue": "图环境没有 \\label"})
            for lab in labels:
                want = by_key.get(lab)
                if want and files and want not in files:
                    fig_issues.append({"part": p.stem, "label": lab, "outline_file": want, "included": files,
                                       "issue": "文件与提纲里该图键的 source 不一致"})
            first = labels[0] if labels else ""
            for f_ in files:
                if f_ in seen_files and seen_files[f_] != first:
                    fig_issues.append({"part": p.stem, "file": f_, "issue": f"同一文件被两个图环境使用（{seen_files[f_]} 与 {first}）"})
                seen_files.setdefault(f_, first)
    out["H11_figure_files_match_outline"] = {"passed": not fig_issues, "issues": fig_issues[:10],
                                             "hint": "每个图环境要有 \\label；\\includegraphics 的文件必须是提纲里该图键的 source；"
                                                     "图题说的必须是文件里画的东西。改片段，或先改提纲。"}
    # H12 成品文字在印刷尺寸下 ≥7pt（被 adjustbox 缩过头的表、矢量图里的小字都在这里现形）
    small_pages: list[dict] = []
    if main_pdf is not None and main_pdf.is_file():
        try:
            bbox = subprocess.run(["pdftotext", "-bbox", str(main_pdf), "-"], capture_output=True, text=True, timeout=120).stdout
            for i, pg in enumerate(re.split(r"<page\b", bbox)[1:], start=1):
                hs = [float(d) - float(b) for _a, b, _c, d in
                      re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)"', pg)]
                small = [h for h in hs if 0.5 < h < 7.0]
                if len(small) >= 8:
                    small_pages.append({"page": i, "words_below_7pt": len(small), "min_pt": round(min(small), 1)})
        except Exception:  # noqa: BLE001
            pass
    out["H12_print_size"] = {"passed": not small_pages, "pages": small_pages[:10],
                             "hint": "两种来源：超宽的表被整体缩小（减列、缩位数、长文字列用 p{}、拆表，表格字号不得低于 7pt）；"
                                     "或矢量图里的小字（看 H9 同页的图，拆面板/加大字号后重派）。"}
    out["passed"] = all(v.get("passed") for k, v in out.items() if k.startswith("H"))
    return out


async def _render_manuscript(state: Any, **_: Any) -> dict:
    brief = load_brief(state)
    if not brief:
        return {"status": "error", "error": "先 write_brief"}
    genre = load_genre(brief["genre"])
    outline = load_outline(state)
    mdir = manuscript_dir(state)
    if not list((mdir / "sections").glob("*.tex")):
        return {"status": "error", "error": "还没有任何部件，先 draft_section"}
    fig = _stage_figures(state, outline)
    _assemble(state, brief, genre)
    from shared.tools.library.latex import _compile_latex, tex_errors
    output_name = "manuscript"
    res = await _compile_latex(state, source_dir="manuscript", main_tex="main.tex",
                               output_name=output_name, engine="xelatex", timeout=420)
    log_text = _log_text(state, output_name)
    report: dict[str, Any] = {"figures": fig, "compile_status": res.get("status")}
    if res.get("status") != "success":
        # 是稿子的错还是这台机器的事，由编译那一层定（_compile_latex 的失败归属：没有 TeX
        # 报错时它会拿已知能编过的样本在同一条路上重编）。这里只转述，不另下判断 ——
        # 曾经这里写死「改那一节再 render_manuscript」，干净 Windows 上 tectonic 根本起不来
        # 时 writing 照样去改了一份没毛病的稿子。
        errs = res.get("latex_errors") or tex_errors(log_text or "")
        report.update({"status": "error", "error": res.get("error") or "编译失败",
                       "latex_errors": errs, "log_tail": clip(str(log_text[-3000:]), 3000),
                       "hint": res.get("recovery") or (
                           "编译超时：稿件太大，或第一次编译取宏包太慢；稍后再 render_manuscript。"
                           if res.get("status") == "timeout" else "看 log_tail 找原因。")})
        for key in ("error_code", "toolchain"):
            if res.get(key):
                report[key] = res[key]
        write_json(paper_dir(state) / "render_report.json", report)
        state.append_transcript("writing_render", ok=False, errors=errs[:4])
        return report
    pdf = Path(res.get("pdf_path") or "")
    pages = _pdf_pages(pdf) if pdf.is_file() else []
    (paper_dir(state) / "pages.txt").write_text("\f".join(pages), encoding="utf-8")
    hl = hard_lines(state, pages, log_text, pdf if pdf.is_file() else None)
    # 交付物副本：paper/manuscript.pdf
    final_pdf = paper_dir(state) / "manuscript.pdf"
    try:
        shutil.copy2(pdf, final_pdf)
    except Exception:  # noqa: BLE001
        final_pdf = pdf
    overfull = len(re.findall(r"Overfull \\hbox", log_text))
    report.update({
        "status": "success", "pdf_path": str(final_pdf), "pages": len(pages), "overfull_hboxes": overfull,
        "hard_lines": hl,
        "page_1": clip(pages[0] if pages else "", 3500),
        "page_2": clip(pages[1] if len(pages) > 1 else "", 1500),
        "layout_audit": (res.get("layout_audit") or {}),
        "next": ("硬线全过 → self-review 后 referee_review。" if hl.get("passed")
                 else "硬线没过：按 hard_lines 里的位置改对应部件，再 render_manuscript。"),
    })
    write_json(paper_dir(state) / "render_report.json", report)
    state.append_transcript("writing_render", ok=True, pages=len(pages), hard_lines_passed=hl.get("passed"),
                            failed=[k for k, v in hl.items() if k.startswith("H") and not v.get("passed")])
    return report


async def _read_pages(state: Any, pages: list[int] | None = None, max_chars: int = 20000, **_: Any) -> dict:
    p = paper_dir(state) / "pages.txt"
    if not p.is_file():
        return {"status": "error", "error": "还没渲染，先 render_manuscript"}
    all_pages = p.read_text(encoding="utf-8").split("\f")
    want = pages or list(range(1, len(all_pages) + 1))
    out = []
    for n in want:
        if 1 <= n <= len(all_pages):
            out.append(f"===== 第 {n} 页 =====\n{all_pages[n-1]}")
    return {"status": "success", "n_pages": len(all_pages), "text": clip("\n".join(out), max(2000, min(int(max_chars), 60000)))}


register_tool(
    ToolDefinition(
        name="render_manuscript",
        description=(
            "装配并编译成品：框架用体裁的渲染器生成 main.tex（标题块、摘要环境、部件顺序、参考文献），"
            "把提纲与正文引用的图放进 figures/，用 xelatex+biber 编译，抽出每页文字，算六条硬线"
            "（标题块、过程词汇、空引用、图表编号与引用、参考文献闭合、简报机械项）。返回首页文字与硬线结果。"
        ),
        parameters_schema={"type": "object", "properties": {}},
        allowed_node_types=["writing"],
    ),
    _render_manuscript,
)

register_tool(
    ToolDefinition(
        name="read_pages",
        description="读上一次渲染出的 PDF 逐页文字（自审用）。pages 不填则全读。",
        parameters_schema={
            "type": "object",
            "properties": {"pages": {"type": "array", "items": {"type": "integer"}}, "max_chars": {"type": "integer"}},
        },
        allowed_node_types=["writing", "_reviewer"],
        replayable_read=True,
    ),
    _read_pages,
)

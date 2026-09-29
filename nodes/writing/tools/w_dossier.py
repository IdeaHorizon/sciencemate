"""证据卷宗：把用户材料与外来件机械拆成可引用的条目，模型按 id 取用。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

from .w_common import clip, paper_dir, read_json, worktree_root, write_json

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>)\]]+")
_ARXIV_RE = re.compile(r"(?:arXiv[:\s]*|arxiv\.org/abs/)(\d{4}\.\d{4,5}(?:v\d+)?)", re.I)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FIG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_REF_ITEM_RE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)[\.、\)])\s*(.+)$")
_TEXT_EXT = {".md", ".tex", ".txt", ".markdown"}
_FIG_EXT = {".png", ".jpg", ".jpeg", ".pdf", ".svg"}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _parse_markdown(doc_id: str, text: str, rel: str) -> tuple[dict[str, dict], list[dict]]:
    entries: dict[str, dict] = {}
    refs: list[dict] = []
    lines = text.splitlines()
    sec_n = tab_n = fig_n = 0
    stack: list[str] = []
    cur_id: str | None = None
    cur_lines: list[str] = []
    in_refs = False
    table_buf: list[str] = []

    def flush_section() -> None:
        nonlocal cur_lines
        if cur_id is not None:
            body = "\n".join(cur_lines).strip()
            entries[cur_id]["text"] = body
            entries[cur_id]["chars"] = len(body)
        cur_lines = []

    def flush_table(section_id: str | None) -> None:
        nonlocal table_buf, tab_n
        if len(table_buf) >= 2:
            tab_n += 1
            tid = f"{doc_id}.T{tab_n}"
            entries[tid] = {
                "id": tid, "kind": "table", "doc": doc_id, "path": rel,
                "section": section_id, "heading": (entries.get(section_id) or {}).get("heading"),
                "text": "\n".join(table_buf), "chars": sum(len(x) for x in table_buf),
                "rows": len([x for x in table_buf if not re.match(r"^\s*\|?\s*:?-{2,}", x)]) - 1,
            }
        table_buf = []

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            flush_table(cur_id)
            flush_section()
            level = len(m.group(1))
            title = m.group(2).strip()
            stack = stack[: level - 1] + [title]
            sec_n += 1
            cur_id = f"{doc_id}.S{sec_n}"
            entries[cur_id] = {"id": cur_id, "kind": "section", "doc": doc_id, "path": rel,
                               "level": level, "heading": title, "path_titles": " > ".join(stack)}
            in_refs = bool(re.search(r"参考文献|references|bibliography|文献", title, re.I))
            continue
        if line.strip().startswith("|"):
            table_buf.append(line)
        else:
            flush_table(cur_id)
        for fm in _FIG_RE.finditer(line):
            fig_n += 1
            fid = f"{doc_id}.F{fig_n}"
            entries[fid] = {"id": fid, "kind": "figure_ref", "doc": doc_id, "path": rel,
                            "section": cur_id, "alt": fm.group(1), "src": fm.group(2)}
        if in_refs:
            rm = _REF_ITEM_RE.match(line)
            if rm:
                num = rm.group(1) or rm.group(2)
                body = rm.group(3).strip()
                doi = _DOI_RE.search(body)
                arx = _ARXIV_RE.search(body)
                rid = f"{doc_id}.R{num}"
                entry = {"id": rid, "kind": "reference", "doc": doc_id, "num": int(num),
                         "text": body, "doi": doi.group(0).rstrip(".,;`'\"") if doi else None,
                         "arxiv": arx.group(1) if arx else None}
                entries[rid] = entry
                refs.append(entry)
        cur_lines.append(line)
    flush_table(cur_id)
    flush_section()
    return entries, refs


def _caption_for(stem: str, caps: dict[str, str]) -> str | None:
    """记录名与文件名常互为前缀（fig1_collective_tp ↔ fig1_collective_tp_dbt_ring_ratio；xxx ↔ xxx_v2）。"""
    if stem in caps:
        return caps[stem]
    best: tuple[str, str] | None = None
    for k, v in caps.items():
        if len(k) >= 6 and (stem.startswith(k) or k.startswith(stem)):
            if best is None or len(k) > len(best[0]):
                best = (k, v)
    return best[1] if best else None


def build_dossier(state: Any, *, force: bool = False) -> dict[str, Any]:
    out_path = paper_dir(state) / "dossier.json"
    root = worktree_root(state)
    # 缓存失效看盘：sources/notes/figures 下任何文件比缓存新（图表服务刚交了图）就重建
    if not force:
        cached = read_json(out_path)
        if cached:
            try:
                newest = 0.0
                for d in (root / "sources", root / "notes", root / "figures") if root else ():
                    if d.is_dir():
                        for q in d.rglob("*"):
                            if q.is_file():
                                newest = max(newest, q.stat().st_mtime)
                if newest <= out_path.stat().st_mtime + 1:
                    return cached
            except Exception:  # noqa: BLE001
                return cached
    docs: list[dict] = []
    entries: dict[str, dict] = {}
    refs: list[dict] = []
    figures: list[dict] = []
    seen_sha: dict[str, str] = {}
    if root is None:
        dossier = {"docs": [], "entries": {}, "refs": [], "figures": [], "note": "no project worktree"}
        write_json(out_path, dossier)
        return dossier
    # 文档 id 按路径持久化：重建（图表服务交图、用户再传文件）不许挪动已发出去的 id ——
    # 提纲里的 evidence id 指着它们。新文档拿下一个空号。
    previous = read_json(out_path) or {}
    doc_ids: dict[str, str] = dict(previous.get("doc_ids") or {})
    used_nums = {int(v[1:]) for v in doc_ids.values() if v.startswith("D") and v[1:].isdigit()}
    # IMG 编号同样按路径持久化：图表服务多交几个文件，已写进提纲的 IMG6 不能变成另一张图（iter4 实测张冠李戴）
    figure_ids: dict[str, str] = dict(previous.get("figure_ids") or {})
    used_img = {int(v[3:]) for v in figure_ids.values() if v.startswith("IMG") and v[3:].isdigit()}
    scan_dirs = [root / "sources", root / "notes", root / "figures"]
    candidates: list[Path] = []
    for d in scan_dirs:
        if d.is_dir():
            candidates.extend(p for p in d.rglob("*") if p.is_file() and not p.name.endswith(".ref"))
    figure_captions: dict[str, str] = {}
    # 第一遍：先把图表服务记录里的图题收齐（按路径排序时 fig1_*.png 排在 figure__*.md 前面，一遍扫会挂空）
    for p in sorted(candidates):
        rel = p.relative_to(root).as_posix()
        if rel.startswith("figures/") and p.suffix.lower() in _TEXT_EXT and p.name.startswith("figure__"):
            rec = p.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^Caption:\s*(.+)$", rec, re.M)
            if m:
                cap = m.group(1).strip()[:300]
                stems = {p.name[len("figure__"):-3]}
                # 记录里显式指向的文件（markdown 图片链接）也挂上
                stems |= {Path(x).stem for x in re.findall(r"\]\(([^)\s]+\.(?:png|pdf|svg|jpe?g))\)", rec, re.I)}
                for s_ in stems:
                    figure_captions[s_] = cap
    for p in sorted(candidates):
        rel = p.relative_to(root).as_posix()
        # figures/ 下的 .md 是图表服务的记录与说明，不是证据文档
        if rel.startswith("figures/") and p.suffix.lower() in _TEXT_EXT:
            continue
        if p.suffix.lower() in _TEXT_EXT and p.stat().st_size < 4_000_000:
            sha = _sha(p)
            if sha in seen_sha:
                docs.append({"doc_id": None, "path": rel, "duplicate_of": seen_sha[sha]})
                continue
            doc_id = doc_ids.get(rel)
            if not doc_id:
                n = 1
                while n in used_nums:
                    n += 1
                used_nums.add(n)
                doc_id = f"D{n}"
                doc_ids[rel] = doc_id
            seen_sha[sha] = doc_id
            text = p.read_text(encoding="utf-8", errors="replace")
            ents, rs = _parse_markdown(doc_id, text, rel)
            entries.update(ents)
            refs.extend(rs)
            docs.append({"doc_id": doc_id, "path": rel, "chars": len(text), "sha": sha,
                         "sections": sum(1 for e in ents.values() if e["kind"] == "section"),
                         "tables": sum(1 for e in ents.values() if e["kind"] == "table"),
                         "refs": len(rs)})
        elif p.suffix.lower() in _FIG_EXT and "/build/" not in rel and "latex_build" not in rel \
                and "/figure_src/" not in rel and not p.name.startswith("_preview"):
            img_id = figure_ids.get(rel)
            if not img_id:
                n = 1
                while n in used_img:
                    n += 1
                used_img.add(n)
                img_id = f"IMG{n}"
                figure_ids[rel] = img_id
            figures.append({"id": img_id, "path": rel, "name": p.name,
                            "format": p.suffix.lower().lstrip("."), "bytes": p.stat().st_size,
                            "caption": _caption_for(p.stem, figure_captions)})
    dossier = {"docs": docs, "doc_ids": doc_ids, "figure_ids": figure_ids, "entries": entries, "refs": refs, "figures": figures}
    write_json(out_path, dossier)
    return dossier


def _index_text(dossier: dict, max_chars: int) -> str:
    lines = ["## 文档"]
    for d in dossier.get("docs", []):
        if d.get("doc_id"):
            lines.append(f"- {d['doc_id']}: {d['path']}（{d['chars']} 字符，{d['sections']} 节，{d['tables']} 表，{d['refs']} 篇文献）")
        else:
            lines.append(f"- （重复，同 {d.get('duplicate_of')}）{d['path']}")
    lines.append("\n## 条目（id | 类型 | 标题 | 字符数）")
    for e in dossier.get("entries", {}).values():
        if e["kind"] == "section":
            lines.append(f"- {e['id']} | 节 | {e.get('path_titles') or e.get('heading')} | {e.get('chars', 0)}")
        elif e["kind"] == "table":
            lines.append(f"- {e['id']} | 表 | 在「{e.get('heading')}」下，{e.get('rows')} 行 | {e.get('chars', 0)}")
    n_refs = len(dossier.get("refs", []))
    with_doi = sum(1 for r in dossier.get("refs", []) if r.get("doi") or r.get("arxiv"))
    lines.append(f"\n## 参考文献：{n_refs} 条，其中 {with_doi} 条带 DOI/arXiv（用 part='refs' 取全文）")
    lines.append(f"## 图文件：{len(dossier.get('figures', []))} 个（用 part='figures' 取清单）")
    return clip("\n".join(lines), max_chars)


async def _read_dossier(state: Any, part: str = "index", ids: list[str] | None = None,
                        query: str = "", max_chars: int = 24000, rebuild: bool = False,
                        **_: Any) -> dict:
    dossier = build_dossier(state, force=bool(rebuild))
    max_chars = max(2000, min(int(max_chars or 24000), 60000))
    if part == "index":
        return {"status": "success", "index": _index_text(dossier, max_chars)}
    if part == "refs":
        text = "\n".join(
            f"- {r['id']} [{r['num']}] {r['text']}" + (f"  (doi:{r['doi']})" if r.get("doi") else "")
            + (f"  (arXiv:{r['arxiv']})" if r.get("arxiv") else "")
            for r in dossier.get("refs", []))
        return {"status": "success", "refs": clip(text, max_chars), "count": len(dossier.get("refs", []))}
    if part == "figures":
        text = "\n".join(f"- {f['id']} {f['path']} ({f['format']}, {f['bytes']} B)" + (f" · 图题：{f['caption']}" if f.get("caption") else "")
                         for f in dossier.get("figures", []))
        return {"status": "success", "figures": clip(text, max_chars), "count": len(dossier.get("figures", []))}
    if part == "search":
        q = (query or "").strip()
        if not q:
            return {"status": "error", "error": "search 需要 query"}
        hits = []
        for e in dossier.get("entries", {}).values():
            t = e.get("text") or ""
            i = t.find(q)
            if i >= 0:
                hits.append(f"- {e['id']}（{e.get('heading') or e['kind']}）…{t[max(0, i-120):i+200].replace(chr(10), ' ')}…")
        return {"status": "success", "hits": clip("\n".join(hits) or "（无命中）", max_chars), "n": len(hits)}
    if part == "entries":
        ids = [str(x) for x in (ids or []) if str(x).strip()]
        if not ids:
            return {"status": "error", "error": "entries 需要 ids 列表"}
        chunks, missing = [], []
        for i in ids:
            e = dossier.get("entries", {}).get(i)
            if not e:
                missing.append(i)
                continue
            head = f"### {i} · {e.get('path_titles') or e.get('heading') or e['kind']}"
            chunks.append(head + "\n" + (e.get("text") or ""))
        text = "\n\n".join(chunks)
        return {"status": "success", "entries": clip(text, max_chars), "missing": missing,
                "total_chars": len(text)}
    return {"status": "error", "error": "part 只能是 index / entries / search / refs / figures"}


register_tool(
    ToolDefinition(
        name="read_dossier",
        description=(
            "读证据卷宗。卷宗由框架从用户材料（sources/）与外来件（notes/）机械拆出：节、表、"
            "图引用、参考文献、图文件，每条有 id。先 part='index' 看目录，再 part='entries' 按 id 取正文，"
            "part='refs' 取参考文献，part='figures' 取图文件清单，part='search' 按关键词找。"
            "提纲里的每条主张都要指向这里的 id；正文里的数字只能来自这里。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "part": {"type": "string", "enum": ["index", "entries", "search", "refs", "figures"]},
                "ids": {"type": "array", "items": {"type": "string"}},
                "query": {"type": "string"},
                "max_chars": {"type": "integer"},
                "rebuild": {"type": "boolean"},
            },
            "required": ["part"],
        },
        allowed_node_types=["writing"],
        replayable_read=True,
    ),
    _read_dossier,
)

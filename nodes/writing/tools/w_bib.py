"""参考文献：模型写 BibTeX，框架核每条能不能解析（DOI / arXiv）。"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

from .w_common import bib_keys, craft, manuscript_dir, read_json, write_json

_HEAD_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,")
_FIELD_RE = re.compile(r"(\w+)\s*=\s*[{\"]([^}\"]*)[}\"]")


class _Entry:
    def __init__(self, etype: str, key: str, body: str) -> None:
        self.etype, self.key, self.body = etype, key, body

    def group(self, i: int) -> str:
        return (None, self.etype, self.key, self.body)[i]


def _split_entries(text: str) -> list[_Entry]:
    """按花括号配平切 BibTeX 条目（正则对单行闭合的条目会漏）。"""
    out: list[_Entry] = []
    for m in _HEAD_RE.finditer(text):
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        out.append(_Entry(m.group(1), m.group(2), text[start:i - 1]))
    return out


def _fields(body: str) -> dict[str, str]:
    return {k.lower(): v.strip() for k, v in _FIELD_RE.findall(body)}


async def _resolve(url: str) -> str:
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as c:
            r = await c.head(url, headers={"User-Agent": "harness-writing/1.0"})
            if r.status_code in (403, 405):
                r = await c.get(url, headers={"User-Agent": "harness-writing/1.0"})
            return "resolved" if 200 <= r.status_code < 400 else f"http {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return f"unchecked ({type(exc).__name__})"


async def _write_bibliography(state: Any, bibtex: str, mode: str = "replace", **_: Any) -> dict:
    mdir = manuscript_dir(state)
    path = mdir / "refs.bib"
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    text = bibtex.strip() + "\n"
    if mode == "append" and old:
        text = old.rstrip() + "\n\n" + text
    entries = _split_entries(text)
    if not entries:
        return {"status": "error", "error": "没有解析到任何 @entry{key, ...}；检查花括号是否闭合"}
    report: list[dict] = []
    tasks = []
    for m in entries:
        etype, key, body = m.group(1), m.group(2), m.group(3)
        f = _fields(body)
        doi = f.get("doi")
        eprint = f.get("eprint") if "arxiv" in (f.get("eprinttype", "") + f.get("archiveprefix", "")).lower() else None
        url = f.get("url")
        if doi:
            tasks.append((key, "doi", f"https://doi.org/{doi}"))
        elif eprint:
            tasks.append((key, "arxiv", f"https://arxiv.org/abs/{eprint}"))
        elif url:
            tasks.append((key, "url", url))
        else:
            tasks.append((key, "none", ""))
        report.append({"key": key, "type": etype, "title": f.get("title", "")[:80],
                       "year": f.get("year"), "id_kind": "doi" if doi else ("arxiv" if eprint else ("url" if url else "none"))})
    results = await asyncio.gather(*[
        _resolve(u) if u else asyncio.sleep(0, result="missing identifier") for (_k, _kind, u) in tasks
    ])
    bad = []
    for r, res in zip(report, results):
        r["check"] = res
        if res == "missing identifier" or res.startswith("http 404"):
            bad.append(r["key"])
    dup = [k for k in set(r["key"] for r in report) if sum(1 for r in report if r["key"] == k) > 1]
    path.write_text(text, encoding="utf-8")
    check = {"entries": report, "bad": bad, "duplicates": dup}
    write_json(mdir / "bib_check.json", check)
    state.append_transcript("writing_bibliography_written", entries=len(report), bad=bad, duplicates=dup)
    return {"status": "success" if not (bad or dup) else "success_with_issues", "count": len(report),
            "bad": bad, "duplicates": dup, "unchecked": [r["key"] for r in report if str(r["check"]).startswith("unchecked")],
            "entries": [{k: r[k] for k in ("key", "type", "year", "id_kind", "check")} for r in report]}


async def _read_bibliography(state: Any, **_: Any) -> dict:
    mdir = manuscript_dir(state)
    path = mdir / "refs.bib"
    if not path.is_file():
        return {"status": "success", "count": 0, "keys": {}, "guide": craft("bibliography")}
    keys = bib_keys(path.read_text(encoding="utf-8"))
    check = read_json(mdir / "bib_check.json") or {}
    return {"status": "success", "count": len(keys), "keys": keys, "bad": check.get("bad", []),
            "guide": craft("bibliography")}


register_tool(
    ToolDefinition(
        name="write_bibliography",
        description=(
            "写参考文献（BibTeX 全文；mode=replace 覆盖，append 追加）。每条必须带 doi 或 arXiv eprint 或 url，"
            "框架会逐条联网核解析结果；解析失败或缺标识的 key 会被列在 bad 里，不许留在稿子里。"
            "只写你确定存在的文献。写之前先 read_bibliography 看写法。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {"bibtex": {"type": "string"}, "mode": {"type": "string", "enum": ["replace", "append"]}},
            "required": ["bibtex"],
        },
        allowed_node_types=["writing"],
    ),
    _write_bibliography,
)

register_tool(
    ToolDefinition(
        name="read_bibliography",
        description="看当前参考文献的 key 与题名、核验结果，以及 BibTeX 写法约定。",
        parameters_schema={"type": "object", "properties": {}},
        allowed_node_types=["writing"],
        replayable_read=True,
    ),
    _read_bibliography,
)

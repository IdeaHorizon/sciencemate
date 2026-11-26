"""派图：写作节点自己的出口，把简报里的政策机械地灌进每一条请求再交给图表服务。

为什么不让模型直接调 run_node（iter11 实测）：15 条请求的 constraints 全是一段散文，
没有 text_language，图表服务不知道这篇稿子的语言政策，七张示意图全画成中文，渲染后
H8 判红、再派一轮。语言、印刷宽度、字号下限、格式、重命名映射这些都写在简报里，
是框架手里的事实；每次派图都靠模型记得抄进去，就一定会有忘的时候。
模型只负责说「这张图要让读者看出什么」和结构 spec；政策由这里带上。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.artifact_provenance import hash_file
from core.tool_registry import ToolDefinition, register_tool

from .w_brief import load_brief, load_outline
from .w_common import clip, worktree_root
from .w_dossier import build_dossier

_RENAME_PAIR = re.compile(r"([A-Za-z0-9_\-]+)\s*(?:→|->|=>|改为|改成|改叫|替换为|重命名为)\s*([^\s,，;；、]+)")


def _figure_sources(state: Any, identities: list[str], dossier: dict
                    ) -> tuple[list[str], list[dict]]:
    """Dossier aliases are reading locations, not artifact IDs or new evidence."""
    root = worktree_root(state)
    docs = {doc["doc_id"]: doc for doc in dossier.get("docs", []) if doc.get("doc_id")}
    sources, context = [], []
    records = None
    hashes = {}
    for identity in dict.fromkeys(identities):
        if identity == "user_prompt":
            context.append({"dossier_id": identity,
                            "text": (load_brief(state) or {}).get("source_request", "")})
            continue
        entry = dossier.get("entries", {}).get(identity) or docs.get(identity)
        if not entry:
            sources.append(identity)  # run_node checks real artifact references.
            continue
        path = root / entry["path"] if root else None
        text = entry.get("text")
        if text is None and path and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
        context.append({"dossier_id": identity, "source_path": str(path) if path else entry["path"],
                        "text": clip(text or "", 30000)})
        # An imported source may already have a real artifact identity. Preserve
        # it and its provenance; never mint writing-owned scientific evidence.
        if records is None:
            records = [(item["id"], state.read_artifact(item["id"]))
                       for item in state.list_artifacts()]
        for artifact_id, record in records:
            if not record or path is None:
                continue
            imported_from = ((record.get("provenance") or {}).get("source_path")
                             or (record.get("metadata") or {}).get("imported_from"))
            if imported_from and Path(imported_from).resolve() == path.resolve():
                # A prior import of a now changed file must not replace the
                # selected dossier text with stale scientific content.
                if path not in hashes:
                    hashes[path] = hash_file(path)[0] if path.is_file() else None
                if hashes[path] == (record.get("provenance") or {}).get("source_sha256"):
                    sources.append(artifact_id)
    return list(dict.fromkeys(sources)), context


def _policy_from_brief(brief: dict) -> dict[str, Any]:
    """简报 → 图表服务要遵守的合同项。"""
    lang = str(((brief.get("language") or {}).get("figures")) or "en")
    forbid: list[str] = []
    rename: dict[str, str] = {}
    for m in brief.get("must_haves") or []:
        rule = str(m.get("rule") or "")
        for clause in rule.split(";"):
            clause = clause.strip()
            if clause.startswith("forbid="):
                forbid.append(clause[len("forbid="):])
        for old, new in _RENAME_PAIR.findall(str(m.get("text") or "")):
            rename[old] = new
    return {
        "text_language": lang,
        "formats": ["pdf", "png"],
        "min_font_pt": 7,
        "print_width_mm": {"single_column": 84, "double_column": 170},
        "forbid_text": forbid,          # 图内任何文字都不许命中这些正则（用户的硬性要求）
        "rename": rename,               # 旧名 → 新名；图内文字为英文时用英文等价名（拓扑1 → Topology 1）
    }


def _normalize_requests(brief: dict, requests: list[Any]) -> tuple[list[dict], list[str]]:
    policy = _policy_from_brief(brief)
    out: list[dict] = []
    problems: list[str] = []
    for i, r in enumerate(requests or [], start=1):
        if not isinstance(r, dict):
            problems.append(f"第 {i} 条不是对象")
            continue
        req = dict(r)
        intent = str(req.get("intent") or req.get("message") or "").strip()
        if not intent:
            problems.append(f"第 {i} 条缺 intent（这张图要让读者看出什么）")
        req["intent"] = intent
        req.setdefault("asset_kind", "quantitative")
        req.setdefault("purpose", "publication")
        c = req.get("constraints")
        if isinstance(c, str):
            c = {"notes": c}
        elif not isinstance(c, dict):
            c = {}
        c = dict(c)
        # 政策项：简报说了算；模型可以补充，不能改掉
        c["text_language"] = policy["text_language"]
        c["min_font_pt"] = max(int(c.get("min_font_pt") or 0), policy["min_font_pt"])
        fmts = c.get("formats") or []
        c["formats"] = sorted(set([*(fmts if isinstance(fmts, list) else [fmts]), *policy["formats"]]))
        c.setdefault("width", "double_column")
        c.setdefault("aspect", "landscape")
        c["print_width_mm"] = policy["print_width_mm"][c["width"]] if c["width"] in policy["print_width_mm"] else policy["print_width_mm"]["double_column"]
        if policy["forbid_text"]:
            c["forbid_text"] = policy["forbid_text"]
        if policy["rename"]:
            merged = dict(policy["rename"])
            if isinstance(c.get("rename"), dict):
                merged.update(c["rename"])
            c["rename"] = merged
        req["constraints"] = c
        if req["asset_kind"] == "schematic" and not (req.get("spec") or req.get("source_artifact_ids")):
            problems.append(f"第 {i} 条是示意图但没有 spec（节点与边）：图表服务不该猜结构")
        out.append(req)
    if not out:
        problems.append("没有任何请求")
    return out, problems


async def _request_figures(state: Any, requests: list[Any], **_: Any) -> dict:
    brief = load_brief(state)
    if not brief:
        return {"status": "error", "error": "先 write_brief：派图要带简报里的语言与命名政策"}
    normalized, problems = _normalize_requests(brief, requests)
    if problems:
        return {"status": "error", "error": "请求不完整：" + "；".join(problems),
                "policy_applied": _policy_from_brief(brief)}
    outline = load_outline(state) or {}
    planned = {str(figure.get("key")): figure for figure in outline.get("figures") or []}
    focus = outline.get("editorial_focus") or {}
    dossier = build_dossier(state)
    forwarded = []
    for request in normalized:
        figure = planned.get(str(request.pop("figure_key", ""))) or {}
        context = {key: figure[key]
                   for key in ("claim", "comparison", "reader_takeaway", "evidence")
                   if figure.get(key)}
        if isinstance(focus, dict):
            context.update({key: focus[key]
                            for key in ("main_question", "main_finding") if focus.get(key)})
        identities = [*(request.get("source_artifact_ids") or []), *(figure.get("evidence") or [])]
        sources, evidence = _figure_sources(state, identities, dossier)
        if sources:
            request["source_artifact_ids"] = sources
            forwarded.extend(sources)
        else:
            request.pop("source_artifact_ids", None)
        if evidence:
            context["source_material"] = evidence
        if context:
            request["intent"] += (
                "\n本稿已选定的主张与比较目标（据此服务读者，不新增研究问题）：\n"
                + json.dumps(context, ensure_ascii=False))
    from shared.tools.run_node import _run_node_tool
    result = await _run_node_tool(
        state, node_type="postprocess", node_inputs={"visual_requests": normalized},
        forward_artifact_ids=list(dict.fromkeys(forwarded)))
    try:
        state.append_transcript("writing_figures_requested", n=len(normalized),
                                schematics=sum(1 for r in normalized if r.get("asset_kind") == "schematic"),
                                text_language=_policy_from_brief(brief)["text_language"])
    except Exception:  # noqa: BLE001
        pass
    if isinstance(result, dict):
        result = dict(result)
        result["policy_applied"] = _policy_from_brief(brief)
        result["next"] = "read_dossier(part='figures', rebuild=true) 看新文件；再 write_outline 把每张图的 source 指到新文件。"
    return result


register_tool(
    ToolDefinition(
        name="request_figures",
        description=(
            "把图的需求交给图表服务（postprocess）。你只写每张图要让读者看出什么（intent）、asset_kind"
            "（quantitative / schematic / composite）、数据来源 source_artifact_ids、示意图的 spec（节点与边）；"
            "语言政策、印刷宽度、字号下限、pdf+png、用户的禁用词与重命名映射由框架按简报自动带上，不用你写。"
            "figure_key 可对应持久提纲中的图，框架带上该图的主张、比较目标、读者所得信息。"
            "卷宗证据解析为可读原文和路径，已有artifact身份通过source_artifact_ids转发。"
            "是否制图由论证需要决定，证明/反例/表格无需逐题配图；多面板 ≤4 且横向。"
            "返回图表服务的结果与实际带上的政策。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "intent": {"type": "string"},
                            "figure_key": {
                                "type": "string",
                                "description": "对应outline.figures.key的主张/比较/读者目标",
                            },
                            "asset_kind": {"type": "string", "enum": ["quantitative", "schematic", "composite"]},
                            "source_artifact_ids": {"type": "array", "items": {"type": "string"}},
                            "spec": {"type": ["object", "string"]},
                            "constraints": {"type": ["object", "string"]},
                            "style_intent": {"type": "string"},
                            "purpose": {"type": "string"},
                        },
                        "required": ["intent", "asset_kind"],
                    },
                },
            },
            "required": ["requests"],
        },
        allowed_node_types=["writing"],
    ),
    _request_figures,
)

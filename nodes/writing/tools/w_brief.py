"""简报与提纲：两份小的声明文件，各自有机械闸。"""
from __future__ import annotations

import re
from typing import Any

from core.tool_registry import ToolDefinition, register_tool

from .w_common import (
    figure_text_sample, genre_brief_summary, list_genres, load_genre, manuscript_dir, paper_dir, read_yaml,
    worktree_root, write_yaml,
)
from .w_dossier import build_dossier


RULE_GRAMMAR = (
    "机械规则只认三种子句，用 ; 连接多条：refs>=N（被引用的参考文献数）；forbid=<正则>（作者正文里不许出现）；"
    "require=<片段1>,<片段2>（每个片段都必须出现在正文或成品里）。例：'refs>=40'、"
    "'forbid=case[1-6];require=拓扑1,拓扑2,拓扑3,拓扑4,拓扑5,拓扑6'。语言政策、图内文字、格式这类交给审读官：check='referee'，不写 rule。"
)


def _rule_problems(rule: str) -> str:
    import re as _re
    if not rule.strip():
        return "check=mechanical 必须带 rule"
    for clause in [c.strip() for c in rule.split(";") if c.strip()]:
        if _re.fullmatch(r"refs>=\d+", clause):
            continue
        if clause.startswith("forbid="):
            try:
                _re.compile(clause[len("forbid="):])
            except _re.error as exc:
                return f"forbid 正则无效：{exc}"
            continue
        if clause.startswith("require=") and clause[len("require="):].strip():
            continue
        return f"不认识的子句 {clause!r}"
    return ""


def _lang_code(value: str | None, default: str) -> str:
    v = (value or "").strip().lower()
    if not v:
        return default
    if v in ("en", "english") or "英文" in v or "english" in v:
        return "en"
    if v in ("zh", "cn", "chinese") or "中文" in v or "chinese" in v:
        return "zh"
    return v[:8]


def load_brief(state: Any) -> dict | None:
    return read_yaml(paper_dir(state) / "brief.yaml")


def load_outline(state: Any) -> dict | None:
    return read_yaml(paper_dir(state) / "outline.yaml")


async def _write_brief(state: Any, genre: str, title: str, must_haves: list | None = None,
                       language_body: str = "zh", language_figures: str = "en",
                       authors: list | None = None, venue: str | None = None,
                       structure_variant: str | None = None, keywords: list | None = None,
                       notes: str = "", **_: Any) -> dict:
    try:
        g = load_genre(genre)
    except ValueError as exc:
        return {"status": "error", "error": str(exc), "genres": list_genres()}
    variants = g["structure"].get("variants") or {}
    if structure_variant and structure_variant not in variants:
        return {"status": "error", "error": f"骨架变体 {structure_variant!r} 不在体裁里；可选：{list(variants)}"}
    if not structure_variant:
        structure_variant = next(iter(variants), None)
    if not (title or "").strip():
        return {"status": "error", "error": "title 不能为空：简报阶段就要有一个具体的工作标题"}
    mh: list[dict] = []
    bad_rules: list[str] = []
    for i, item in enumerate(must_haves or [], start=1):
        if isinstance(item, str):
            mh.append({"id": f"R{i}", "text": item, "check": "referee"})
        elif isinstance(item, dict) and item.get("text"):
            check = str(item.get("check") or "referee")
            rule = item.get("rule")
            if check == "mechanical":
                problems = _rule_problems(str(rule or ""))
                if problems:
                    bad_rules.append(f"{item.get('id') or f'R{i}'}: {problems}")
            # 按「这件事」判，不按写法判：重命名/替换类要求不管标成什么 check，都必须 mechanical + forbid=旧名
            # （iter7 实测：模型把它标成 check=referee 绕过了只查 mechanical 的闸）
            if re.search(r"(→|->|替换|改名|重命名|rename|改叫|改为|改成)", str(item["text"])) and "forbid=" not in str(rule or ""):
                check = "mechanical"
                bad_rules.append(f"{item.get('id') or f'R{i}'}: 这是重命名/替换类要求，必须 check='mechanical' 且 rule 带 forbid=<旧名正则>"
                                 "（例 'forbid=case[1-6];require=拓扑1,拓扑2,拓扑3,拓扑4,拓扑5,拓扑6'）；"
                                 "交给审读官它看不见图里的旧名字")
            mh.append({"id": str(item.get("id") or f"R{i}"), "text": str(item["text"]),
                       "check": check, "rule": rule})
    if bad_rules:
        return {"status": "error", "error": "must_haves 里的机械规则写法不对：" + "；".join(bad_rules),
                "grammar": RULE_GRAMMAR}
    # 用户原话里的编号要求必须逐条登记（iter10 实测：模型第一次调用就把 must_haves 交了空，H6 整轮无牙）
    task = str((getattr(state, "hook_state", {}) or {}).get("node_inputs", {}).get("research_question") or "")
    numbered = [l.strip() for l in task.splitlines() if re.match(r"^\s*\d{1,2}[\.、．)]\s*\S", l)]
    if numbered and not mh:
        return {"status": "error",
                "error": f"用户提示里有 {len(numbered)} 条编号要求，must_haves 不能为空：每条要么登记为一条 must_have"
                         "（能机械验的写 rule），要么在 notes 里写明为什么它不是硬性要求",
                "candidates": numbered[:12], "grammar": RULE_GRAMMAR}
    language_body = _lang_code(language_body, "zh")
    language_figures = _lang_code(language_figures, "en")
    brief = {
        "genre": genre, "structure_variant": structure_variant, "title": title.strip(),
        "language": {"body": language_body, "figures": language_figures},
        "authors": [a for a in (authors or []) if a] or None,
        "venue": venue or None, "keywords": [k for k in (keywords or []) if k] or None,
        "must_haves": mh, "notes": notes or "",
    }
    write_yaml(paper_dir(state) / "brief.yaml", brief)
    state.append_transcript("writing_brief_written", genre=genre, variant=structure_variant,
                            must_haves=len(mh), authors=bool(brief["authors"]))
    dossier = build_dossier(state)
    # 这台机器能不能出 PDF，在**开工这一刻**就知道（shared/lib/pdf_toolchain：已知能编过的
    # 样本走稿子要走的同一条路真编一次；工具没换、上次编过就直接用记录）。2026-09-23 那台
    # Windows 上 writing 写了 45 分钟才在 render_manuscript 发现出不了 PDF。
    from shared.lib import pdf_toolchain

    pdf = await pdf_toolchain.ensure_measured(state)
    result = {
        "status": "success",
        "brief": brief,
        "genre_summary": genre_brief_summary(g, structure_variant),
        "reader": g["reader"],
        "dossier_docs": [d for d in dossier.get("docs", []) if d.get("doc_id")],
        "pdf_toolchain": pdf_toolchain.describe(pdf),
        "next": "读 read_dossier(part='index')，然后 write_outline。",
    }
    if not pdf.get("works"):
        result["next"] = (
            "这台机器现在出不了 PDF（见 pdf_toolchain）——这不是稿件能解决的，也别自己排查平台。"
            "任务要的是 PDF 而调度器没交代过这种情形：先 report_blocker(category='environment')"
            "，让调度器去问用户；调度器已经说明照写源码的：照常 read_dossier → write_outline，"
            "最后交付 LaTeX 源码。")
    return result


register_tool(
    ToolDefinition(
        name="write_brief",
        description=(
            "登记这次写作的简报（只记一次，验收逐条对）：体裁、工作标题、语言政策、用户的硬性要求"
            "（must_haves，每条 {id,text,check:'mechanical'|'referee',rule}；机械可验的写 rule，语法只有三种子句、"
            "用 ; 连接：'refs>=40'、'forbid=case[1-6]'、'require=拓扑1,拓扑2'；语言政策与格式交审读官，check='referee'）、"
            "作者（没有就不填，不写占位）、语言用代码：zh / en、"
            "刊物、骨架变体。返回体裁摘要与读者画像。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "genre": {"type": "string", "description": "体裁包 id，如 sci_article_zh"},
                "title": {"type": "string"},
                "must_haves": {"type": "array", "items": {"type": ["object", "string"]}},
                "language_body": {"type": "string"},
                "language_figures": {"type": "string"},
                "authors": {"type": "array", "items": {"type": "string"}},
                "venue": {"type": "string"},
                "structure_variant": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "string"},
            },
            "required": ["genre", "title"],
        },
        allowed_node_types=["writing"],
    ),
    _write_brief,
)


# ── 提纲 ────────────────────────────────────────────────────────────────────

async def _write_outline(state: Any, outline: dict, **_: Any) -> dict:
    brief = load_brief(state)
    if not brief:
        return {"status": "error", "error": "先 write_brief"}
    g = load_genre(brief["genre"])
    s = g["structure"]
    variants = s.get("variants") or {}
    parts_expected = variants.get(brief.get("structure_variant") or "", {}).get("parts") or s.get("required_parts") or []
    dossier = build_dossier(state)
    known_ids = set(dossier.get("entries", {}).keys())
    known_figs = ({f["id"] for f in dossier.get("figures", [])} | {f["name"] for f in dossier.get("figures", [])}
                  | {f["name"].rsplit(".", 1)[0] for f in dossier.get("figures", [])}
                  | {f["path"] for f in dossier.get("figures", [])})   # 卷宗列的就是 path，写 path 也合法
    issues: list[str] = []
    if not isinstance(outline, dict):
        return {"status": "error", "error": "outline 必须是对象"}
    parts = outline.get("parts")
    if not isinstance(parts, list) or not parts:
        return {"status": "error", "error": "outline.parts 必须是非空列表"}
    seen = []
    n_claims = 0
    for p in parts:
        pid = str(p.get("id") or "")
        seen.append(pid)
        if pid not in (s.get("parts") or {}):
            issues.append(f"部件 {pid!r} 不在体裁里：{list((s.get('parts') or {}).keys())}")
        for sub in p.get("subsections") or [p]:
            for c in sub.get("claims") or []:
                n_claims += 1
                ev = c.get("evidence") or []
                if not ev and pid not in ("abstract", "introduction", "conclusion"):
                    issues.append(f"{pid}: 主张「{str(c.get('text'))[:40]}」没有 evidence")
                for e in ev:
                    if e not in known_ids and e != "user_prompt":
                        issues.append(f"{pid}: evidence id {e!r} 不在卷宗里")
    for pid in parts_expected:
        if pid not in seen:
            issues.append(f"缺部件 {pid}（体裁要求：{parts_expected}）")
    id_to_name = {f["id"]: f["name"] for f in dossier.get("figures", [])}
    stem_to_name = {f["name"].rsplit(".", 1)[0]: f["name"] for f in dossier.get("figures", [])}
    path_to_name = {f["path"]: f["name"] for f in dossier.get("figures", [])}
    forbid_pats: list[str] = []
    for m in brief.get("must_haves") or []:
        if str(m.get("check")) == "mechanical":
            for clause in str(m.get("rule") or "").split(";"):
                clause = clause.strip()
                if clause.startswith("forbid="):
                    forbid_pats.append(clause[len("forbid="):])
    for f in outline.get("figures") or []:
        src = f.get("source") or f.get("file")
        if src and src not in known_figs and src != "planned":
            issues.append(f"图 {f.get('key')}: source {src!r} 不在图文件清单里（用 read_dossier(part='figures')），或写 'planned'")
        elif src and src != "planned":
            # 把 IMG 编号解析成文件名一并存下：后面起草、对账都按文件名走，不再依赖编号
            f["file"] = id_to_name.get(src) or path_to_name.get(src) or stem_to_name.get(src) or src
        if not f.get("message"):
            issues.append(f"图 {f.get('key')}: 缺 message（这张图要让读者看出什么）")
        # 图内文字提前过一遍 forbid：用户原图常带旧名字（case6），进了稿子就是违约，起草前就报
        if f.get("file") and forbid_pats:
            import re as _re
            from pathlib import Path as _P
            root = worktree_root(state)
            sample = figure_text_sample(_P(f["file"]).stem,
                                        [manuscript_dir(state) / "figures", (root / "figures") if root else None], 600)
            for pat in forbid_pats:
                try:
                    hit = _re.search(pat, sample)
                except _re.error:
                    hit = pat in sample
                if hit:
                    issues.append(f"图 {f.get('key')}（{f['file']}）图内文字命中简报禁用项 {pat!r}：这张图不能用，"
                                  "带 rename 约束让图表服务重画（示意图给 spec，别让它复制用户原图）")
    write_yaml(paper_dir(state) / "outline.yaml", outline)
    state.append_transcript("writing_outline_written", parts=seen, claims=n_claims, issues=len(issues))
    return {"status": "success" if not issues else "success_with_issues", "parts": seen,
            "claims": n_claims, "figures": len(outline.get("figures") or []), "issues": issues,
            "next": "issues 清零后逐节 draft_section；abstract 最后写。"}


register_tool(
    ToolDefinition(
        name="write_outline",
        description=(
            "写论证提纲（机械闸：主张的 evidence 必须是卷宗 id；部件必须是体裁部件；图必须有 message）。"
            "结构：{title, parts:[{id, subsections:[{title, claims:[{text, evidence:[dossier ids]}], "
            "figures:[fig keys], tables:[tab keys]}]}], figures:[{key:'fig:xxx', source:'IMG3'|'planned', "
            "message, caption}], tables:[{key:'tab:xxx', source:'D1.T2', caption}], must_haves_map:{R1:'怎么兑现'}}。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {"outline": {"type": "object"}},
            "required": ["outline"],
        },
        allowed_node_types=["writing"],
    ),
    _write_outline,
)

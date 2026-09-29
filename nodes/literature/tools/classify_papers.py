"""classify_papers —— AI 文献分类（L2 摘要级聚类 + survey_report）
注册后 llm 可自主调用分类能力。"""
from __future__ import annotations

import json, re
from typing import Any
from collections import Counter

from core.state import State
from core.tool_registry import ToolDefinition, register_tool
from core.llm import LLMClient, LLMMessage


def _pj(text):
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m: return {}
    raw = m.group()
    raw = re.sub(r"'([^']*?)':", r'"\1":', raw)
    raw = re.sub(r',\s*}', '}', raw)
    raw = re.sub(r',\s*]', ']', raw)
    raw = re.sub(r'//.*?\n', '\n', raw)
    try: return json.loads(raw)
    except: return {}

async def _call_llm_json(prompt: str, max_tokens: int = 2000) -> dict:
    """Call the framework-configured OpenAI-compatible LLM and parse JSON.

    Uses the same LLM_API_KEY / LLM_BASE_URL / LLM_MODEL path as the node
    agent. No provider-specific key lookup or hardcoded DeepSeek endpoint.
    """
    client = LLMClient()
    resp = await client.chat(
        [LLMMessage(role="user", content=prompt)],
        tools=None,
        max_tokens=max_tokens,
        temperature=0.3,
    )
    return _pj(resp.content or "{}")


def _parse_papers_json(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        text = str(value or "").lstrip()
        try:
            parsed, end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            raise exc
        if text[end:].strip() != "}" or not isinstance(parsed, list):
            raise exc
    if not isinstance(parsed, list):
        raise ValueError("papers_json 必须是论文数组")
    return parsed


async def _classify_papers(
    state: State,
    papers_json: str,
    max_clusters: int = 4,
    **_: Any,
) -> dict:
    """将一批论文按摘要内容聚类为若干研究主题，并生成 survey_report。

    **Use when**：
      - 搜索完论文后，想自动分主题整理
      - 需要快速了解一批论文覆盖了哪些研究方向
      - 需要生成结构化的 survey_report（含关键发现和 open questions）

    **Do NOT use when**：
      - 论文太少（< 5 篇）→ 手动阅读更高效
      - 需要精确的全文级分类 → 当前基于摘要，适合粗略分主题

    **参数**：
      - papers_json: 论文列表的 JSON 字符串。每篇至少含 title 字段，含 abstract 效果更好。
        格式：[{"title": "...", "abstract": "...", "doi": "..."}, ...]
      - max_clusters: 最大聚类数（2-6，默认 4）

    **返回**：clusters（每类名称+论文列表）+ survey_report（markdown）
    """
    try:
        papers = _parse_papers_json(papers_json)
    except Exception as e:
        return {"status": "error", "error": f"papers_json 解析失败: {e}"}

    # 「至少 2 篇」数量下限已删（判决拆除三波，classify_papers:87 D）：
    # 1 篇→1 个主题照样可算，阈值是任意的；0 篇是如实的空结果。
    n = len(papers)
    max_clusters = max(2, min(6, max_clusters))

    # ── L2: 摘要多标签分类 ──
    # 每个批次先独立阅读完整摘要并提出临时主题；不在阅读摘要前
    # 预生成全局类别，避免用标题臆测主题。之后再用小请求统一跨批次
    # 的同义类别。原始论文和摘要始终保留，不做截断后交付。
    batch_size = 20
    local_assignments: dict[int, list[str]] = {}
    local_themes: dict[str, str] = {}
    batch_reports: list[dict[str, Any]] = []
    unassigned_defaulted: list[int] = []
    for start in range(0, n, batch_size):
        batch = papers[start:start + batch_size]
        batch_lines = "\n\n".join(
            f"PAPER_ID: {start + offset}\nTITLE: {p.get('title') or ''}\nABSTRACT:\n{p.get('abstract') or '[NO ABSTRACT]'}"
            for offset, p in enumerate(batch)
        )
        batch_prompt = (
            f"Read the complete abstracts of these {len(batch)} papers and derive "
            "2-4 generic, overlapping research themes from their actual content. "
            "Do not use a hard-coded domain taxonomy. Assign every paper to one or "
            "more themes. Return JSON only: {\"themes\":[{\"id\":1,\"name\":\"...\"}], "
            "\"assignments\":[{\"paper_id\":int,\"theme_ids\":[int,...]}]}.\n\n"
            f"{batch_lines}"
        )
        try:
            batch_result = await _call_llm_json(batch_prompt, 1400)
        except Exception as e:
            return {"status": "error", "error": f"LLM 分批分类失败（{start}-{start + len(batch) - 1}）：{type(e).__name__}: {str(e)[:300]}"}
        raw_themes = batch_result.get("themes", [])
        if isinstance(raw_themes, dict):
            raw_themes = [{"id": key, "name": value} for key, value in raw_themes.items()]
        if not isinstance(raw_themes, list) or not raw_themes:
            return {"status": "error", "error": f"分类结果缺少 themes（批次 {start}-{start + len(batch) - 1}）"}
        theme_ids: set[int] = set()
        for theme in raw_themes:
            if not isinstance(theme, dict):
                continue
            try:
                tid = int(theme.get("id"))
            except (TypeError, ValueError):
                continue
            name = str(theme.get("name") or "").strip()
            if tid > 0 and name:
                theme_ids.add(tid)
                local_themes[f"{start}:{tid}"] = name
        if not theme_ids:
            return {"status": "error", "error": f"分类结果 themes 无有效主题（批次 {start}-{start + len(batch) - 1}）"}
        rows = batch_result.get("assignments")
        if not isinstance(rows, list):
            return {"status": "error", "error": f"分类结果缺少 assignments（批次 {start}-{start + len(batch) - 1}）"}
        expected, seen = set(range(start, start + len(batch))), set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                paper_id = int(row.get("paper_id"))
            except (TypeError, ValueError):
                continue
            if paper_id not in expected or paper_id in seen:
                continue
            values = row.get("theme_ids", [])
            if not isinstance(values, list):
                values = [values]
            clean = []
            for value in values:
                try:
                    tid = int(value)
                except (TypeError, ValueError):
                    continue
                key = f"{start}:{tid}"
                if tid in theme_ids and key not in clean:
                    clean.append(key)
            local_assignments[paper_id] = clean or [f"{start}:{min(theme_ids)}"]
            seen.add(paper_id)
        missing = sorted(expected - seen)
        for paper_id in missing:
            # 模型漏派的论文按确定性回退落进本批首主题（与空 theme_ids 的回退
            # 同一条路），并在结果里如实记 unassigned_paper_ids_defaulted；
            # 一篇漏派不再杀掉整次分类（判决拆除三波，classify_papers:166 降格）。
            local_assignments[paper_id] = [f"{start}:{min(theme_ids)}"]
        unassigned_defaulted.extend(missing)
        batch_reports.append({"start": start, "paper_ids": sorted(expected), "theme_keys": sorted(k for k in local_themes if k.startswith(f"{start}:")), "unassigned_paper_ids_defaulted": missing})

    # 小型跨批次合并请求：只传每个临时主题的名称和少量代表论文摘要，
    # 不再把全部论文摘要重新发送一遍。
    merge_material = []
    for report in batch_reports:
        for key in report["theme_keys"]:
            ids = [i for i in report["paper_ids"] if key in local_assignments.get(i, [])]
            representatives = []
            for i in ids[:3]:
                representatives.append({"title": papers[i].get("title", ""), "abstract": papers[i].get("abstract", "")})
            merge_material.append({"local_theme": key, "name": local_themes[key], "representative_papers": representatives})
    merge_prompt = (
        "Unify independently derived research themes. Use the representative paper titles and "
        "complete abstracts to merge synonyms, while preserving genuinely distinct themes. "
        f"Return no more than {max_clusters} global themes as JSON only: "
        "{\"theme_labels\":{\"1\":\"...\"},\"mapping\":{\"batch_start:local_id\":1}}\n\n"
        + json.dumps(merge_material, ensure_ascii=False)
    )
    merged: dict[str, Any] = {}
    if merge_material:      # 零篇论文时没有可合并的主题，不为空材料打一次模型
        try:
            merged = await _call_llm_json(merge_prompt, 1600)
        except Exception:
            merged = {}
    raw_labels = merged.get("theme_labels", {}) if isinstance(merged, dict) else {}
    raw_mapping = merged.get("mapping", {}) if isinstance(merged, dict) else {}
    labels: dict[str, str] = {}
    if isinstance(raw_labels, dict):
        for key, value in raw_labels.items():
            try:
                cid = int(key)
            except (TypeError, ValueError):
                continue
            if 1 <= cid <= max_clusters and str(value).strip():
                labels[str(cid)] = str(value).strip()
    mapping: dict[str, int] = {}
    if isinstance(raw_mapping, dict):
        for key, value in raw_mapping.items():
            try:
                cid = int(value)
            except (TypeError, ValueError):
                continue
            if key in local_themes and 1 <= cid <= max_clusters:
                mapping[key] = cid
    # 服务短暂失败时仍返回完整覆盖：按临时主题顺序稳定落入有限类别，
    # 并在结果中保留真实临时名称，避免丢论文或阻断归档。
    for key in local_themes:
        if key not in mapping:
            cid = (len(mapping) % max_clusters) + 1
            mapping[key] = cid
        labels.setdefault(str(mapping[key]), local_themes[key])
    labels = {str(i): labels.get(str(i), f"Theme {i}") for i in range(1, max_clusters + 1) if str(i) in labels}
    assignments = []
    for i in range(n):
        ids = sorted({mapping[key] for key in local_assignments.get(i, []) if key in mapping})
        assignments.append(ids or [1])
    review_applied = bool(merged)
    review_label_changes = {}
    review_additions = []
    # （原「主题复核后论文覆盖校验」不可达——assignments 按 range(n) 构造且每项
    # `ids or [1]`，恒满足；判决拆除三波 classify_papers:228 X 删。）

    # Build overlapping themes: the same paper can appear in several themes.
    result_clusters = {}
    for i, theme_ids in enumerate(assignments[:n]):
        for cid in theme_ids:
            result_clusters.setdefault(cid, {"name": labels.get(str(cid), f"Theme {cid}"), "papers": []})
            result_clusters[cid]["papers"].append(papers[i])
    result_clusters = dict(sorted(result_clusters.items()))

    # ── Survey report ──
    # Preserve complete source titles in the report; only normalize embedded
    # line breaks so each paper remains one Markdown list item.
    def _report_title(paper: dict) -> str:
        return str(paper.get("title") or "").replace("\r", " ").replace("\n", " ").strip()

    cluster_texts = []
    for nid, cl in result_clusters.items():
        papers_str = "\n".join(f"  - {_report_title(p)}" for p in cl["papers"])
        cluster_texts.append(f"## Theme {nid}: {cl['name']}\n{papers_str}\n")

    survey_summary = "\n".join(cluster_texts)

    # Generate key findings + open questions (optional, call DeepSeek)
    cluster_names = "; ".join(f"{nid}. {cl['name']}({len(cl['papers'])}篇)" for nid, cl in result_clusters.items())
    t1 = len(papers)
    q_prompt = (
        f"A survey of {t1} papers was clustered into these themes: {cluster_names}\n\n"
        f"Produce 2-3 key findings across all papers and 2-3 open questions.\n"
        f"IN CHINESE.\n\n"
        f"JSON: {{\"key_findings\": [str,...], \"open_questions\": [str,...]}}"
    )
    qr: dict[str, Any] = {}
    if papers:      # 零篇论文没有可总结的东西，不为空集打一次模型
        try:
            qr = await _call_llm_json(q_prompt, 1000)
        except Exception:
            qr = {}
    key_findings = qr.get("key_findings", ["待补充"])
    open_questions = qr.get("open_questions", ["待补充"])

    return {
        "status": "success",
        "total_papers": len(papers),
        "cluster_count": len(result_clusters),
        "clusters": [
            {"id": nid, "name": cl["name"], "paper_count": len(cl["papers"]),
             "papers": [{"title": _report_title(p), "doi": p.get("doi","")} for p in cl["papers"]]}
            for nid, cl in result_clusters.items()
        ],
        "multi_label": True,
        "unassigned_paper_ids_defaulted": unassigned_defaulted,
        "theme_review_applied": review_applied,
        "theme_review_label_changes": review_label_changes,
        "theme_review_additions": review_additions,
        "paper_theme_assignments": [
            {"paper_index": i, "theme_ids": theme_ids}
            for i, theme_ids in enumerate(assignments[:n])
        ],
        "survey_report": {
            "summary": survey_summary,
            "key_findings": key_findings,
            "open_questions": open_questions,
        },
    }


register_tool(
    ToolDefinition(
        name="classify_papers",
        description=(
            "将一批论文按摘要内容自动聚类为若干研究主题，并生成结构化 survey_report。\n\n"
            "**Use when**：\n"
            "  - 搜索完论文后想自动分主题整理\n"
            "  - 需要快速了解一批论文覆盖的研究方向\n"
            "  - 需要生成 survey_report（带关键发现和 open questions）\n\n"
            "**Do NOT use when**：\n"
            "  - 论文 < 5 篇 → 手动阅读更高效\n"
            "  - 需要全文级精确分类 → 当前基于摘要\n\n"
            "**关键参数**：\n"
            "  - papers_json: JSON 论文列表，每篇必含 title。含 abstract 效果更好\n"
            "    格式：[{\"title\":\"...\",\"abstract\":\"...\",\"doi\":\"...\"}, ...]\n"
            "  - max_clusters: 最大聚类数（2-6，默认 4）\n\n"
            "**返回**：clusters（每类名称+论文）+ survey_report（关键发现+open questions）"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "papers_json": {
                    "type": "string",
                    "description": (
                        "论文 JSON 列表，每篇必含 title 字段。"
                        "可从 search_papers 工具的返回中提取 papers 数组传入。"
                    ),
                },
                "max_clusters": {
                    "type": "integer",
                    "default": 4,
                    "minimum": 2,
                    "maximum": 6,
                    "description": "最大聚类数。",
                },
            },
            "required": ["papers_json"],
        },
        allowed_node_types=["literature"],
        risk_level="low",
    ),
    _classify_papers,
)

"""Structured finalization for targeted literature lookups."""
from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import html
import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from core.state import State
from core.tool_registry import ToolDefinition, register_tool


def _json_list(raw: str, field: str) -> tuple[list[Any] | None, str | None]:
    try:
        value = json.loads(raw or "[]")
    except Exception as exc:
        return None, f"{field} 必须是 JSON 数组: {exc}"
    if not isinstance(value, list):
        return None, f"{field} 必须是 JSON 数组"
    return value, None


def _has_stable_identifier(paper: Any) -> bool:
    if not isinstance(paper, dict):
        return False
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(paper.get("doi") or "").strip(), flags=re.I)
    if re.match(r"^10\.\d{4,9}/\S+$", doi, flags=re.I):
        return True
    for field in ("url", "arxiv_url", "source_url"):
        if re.match(r"^https?://\S+$", str(paper.get(field) or "").strip(), flags=re.I):
            return True
    arxiv_id = str(paper.get("arxiv_id") or "").strip()
    if re.match(r"^(?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?$", arxiv_id, flags=re.I):
        paper["url"] = "https://arxiv.org/abs/" + arxiv_id
        return True
    return False


_DOI_AUDIT_MAX_SECONDS = 18
_DOI_AUDIT_PER_REQUEST_SECONDS = 6
_DOI_AUDIT_CONCURRENCY = 3


def _normalise_title(title: str) -> str:
    plain = html.unescape(re.sub(r"<[^>]*>", " ", title or ""))
    return " ".join(re.findall(r"[\w]+", plain.casefold()))


def _official_year(record: dict) -> int | None:
    # The journal issue year takes precedence over an earlier preprint/online date.
    for field in ("published-print", "published-online", "published", "issued"):
        parts = (record.get(field) or {}).get("date-parts") or []
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


async def _fetch_crossref_record(client: httpx.AsyncClient, doi: str) -> dict | None:
    try:
        response = await client.get(
            "https://api.crossref.org/works/" + quote(doi, safe="/"),
            timeout=_DOI_AUDIT_PER_REQUEST_SECONDS,
        )
        if response.status_code != 200:
            return None
        return response.json().get("message") or None
    except (httpx.HTTPError, ValueError):
        return None


async def _audit_doi_metadata(papers: list[Any]) -> dict[str, int]:
    """Bounded DOI check; never silently label an unchecked field as verified."""
    doi_to_papers: dict[str, list[dict]] = {}
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        raw = str(paper.get("doi") or "").strip()
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw, flags=re.I).lower()
        if doi.startswith("10."):
            doi_to_papers.setdefault(doi, []).append(paper)
        else:
            paper["metadata_verification"] = {"status": "no_doi"}

    counts = {"verified": 0, "corrected_year": 0, "title_mismatch": 0,
              "unverified": 0, "no_doi": sum(
                  isinstance(p, dict)
                  and p.get("metadata_verification", {}).get("status") == "no_doi"
                  for p in papers
              )}
    if not doi_to_papers:
        return counts

    semaphore = asyncio.Semaphore(_DOI_AUDIT_CONCURRENCY)
    async with httpx.AsyncClient(headers={"User-Agent": "SurveyHarness/1.0 (literature DOI audit)"}) as client:
        async def fetch(doi: str) -> dict | None:
            async with semaphore:
                return await _fetch_crossref_record(client, doi)

        tasks = {doi: asyncio.create_task(fetch(doi)) for doi in doi_to_papers}
        done, pending = await asyncio.wait(tasks.values(), timeout=_DOI_AUDIT_MAX_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for doi, group in doi_to_papers.items():
            task = tasks[doi]
            record = None
            if task in done and not task.cancelled():
                try:
                    record = task.result()
                except Exception:
                    pass
            titles = (record or {}).get("title") or [""]
            raw_title = titles[0] if isinstance(titles, list) else titles
            official_title = _normalise_title(str(raw_title))
            official_year = _official_year(record or {})
            for paper in group:
                supplied_title = _normalise_title(str(paper.get("title") or ""))
                if not record or not official_title or not supplied_title:
                    status = {"status": "unverified", "source": "crossref"}
                elif SequenceMatcher(None, supplied_title, official_title).ratio() < 0.78:
                    status = {"status": "title_mismatch", "source": "crossref",
                              "official_title": raw_title}
                else:
                    status = {"status": "verified", "source": "crossref"}
                    if official_year is not None:
                        previous_year = paper.get("year")
                        if str(previous_year or "") != str(official_year):
                            paper["year"] = official_year
                            status.update({"status": "corrected_year",
                                           "original_year": previous_year,
                                           "official_year": official_year})
                paper["metadata_verification"] = status
                counts[status["status"]] += 1
    return counts


async def _finalize_evidence_package(
    state: State,
    name: str,
    queries_json: str,
    included_papers_json: str,
    evidence_summary: str,
    excluded_papers_json: str = "[]",
    unknowns_json: str = "[]",
    **_: Any,
) -> dict:
    """Save the mandatory compact artifact for targeted_lookup mode."""
    mode = state.hook_state.get("_request_mode")
    # 「只在三种模式下可用」模式适用域闸已删（判决拆除批 3w，
    # finalize_evidence_package.py:34 档一：事前审批链不是科学）。
    if not name.strip():
        return {"status": "error", "error": "name 必填"}

    parsed = {}
    for field, raw in (("queries", queries_json), ("included_papers", included_papers_json),
                       ("excluded_papers", excluded_papers_json), ("unknowns", unknowns_json)):
        value, error = _json_list(raw, field)
        if error:
            return {"status": "error", "error": error}
        parsed[field] = value

    # L-OB1 空结果披露（判决拆除批 3w，finalize_evidence_package.py:49 降格）：
    # 意图是 S2 本身——空结果也要说清查了什么、为什么没有纳入。照存盘，
    # 缺说明如实记进 advisories 随包走，不再拒绝收尾。
    advisories: list[str] = []
    # evidence_summary 空同属空结果披露义务（判决拆除三波，:38 降格）：
    # 照存盘、如实记 advisory 随包走，不再拒绝收尾。
    if not evidence_summary.strip():
        advisories.append(
            "evidence_summary 为空——没有证据时也要明确写「证据不足」以及查了什么"
            "（空结果披露义务）"
        )
    anchored = []
    unanchored = []
    for paper in parsed["included_papers"]:
        (anchored if _has_stable_identifier(paper) else unanchored).append(paper)
    if unanchored:
        parsed["included_papers"] = anchored
        parsed["excluded_papers"].extend(
            {"paper": paper, "reason": "missing_doi_or_stable_url"} for paper in unanchored
        )
        advisories.append(f"{len(unanchored)} 篇缺少 DOI 或稳定原文链接，已移出纳入清单")
    if not parsed["included_papers"] and not parsed["unknowns"]:
        advisories.append(
            "included_papers 为空且未填写 unknowns——空结果披露义务：补写查了哪些"
            "查询、为什么没有可纳入的论文（unknowns 是它的落点）"
        )

    metadata_audit = await _audit_doi_metadata(parsed["included_papers"])
    if metadata_audit["unverified"] or metadata_audit["title_mismatch"]:
        advisories.append(
            "部分 DOI 未能核验或题名不匹配；不得将这些条目标为已核实："
            + json.dumps(metadata_audit, ensure_ascii=False)
        )

    content = {
        "schema_version": 1,
        "mode": mode or "targeted_lookup",
        "queries": parsed["queries"],
        "included_papers": parsed["included_papers"],
        "excluded_papers": parsed["excluded_papers"],
        "evidence_summary": evidence_summary.strip(),
        "unknowns": parsed["unknowns"],
        "advisories": advisories,
        "metadata_audit": metadata_audit,
    }
    artifact = state.save_artifact(
        artifact_type="literature_evidence_package",
        name=name.strip(),
        content=json.dumps(content, ensure_ascii=False, indent=2),
        metadata={
            "mode": content["mode"],
            "included_count": len(parsed["included_papers"]),
            "excluded_count": len(parsed["excluded_papers"]),
            "unknown_count": len(parsed["unknowns"]),
            "advisories": advisories,
            "metadata_audit": metadata_audit,
        },
    )
    result = {"status": "success", "artifact_id": artifact["id"],
              "included_count": len(parsed["included_papers"]),
              "unknown_count": len(parsed["unknowns"]),
              "metadata_audit": metadata_audit}
    if advisories:
        result["advisories"] = advisories
    return result


register_tool(
    ToolDefinition(
        name="finalize_evidence_package",
        description=(
            "定向文献检索的强制收尾：把查询、纳入/排除论文、证据总结和未知项"
            "保存为 literature_evidence_package。没有找到论文时必须在 unknowns 中说明原因。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "artifact 名称"},
                "queries_json": {"type": "string", "description": "查询条件 JSON 数组"},
                "included_papers_json": {"type": "string", "description": "纳入论文 JSON 数组"},
                "excluded_papers_json": {"type": "string", "default": "[]", "description": "排除论文及原因 JSON 数组"},
                "evidence_summary": {"type": "string", "description": "基于检索结果的简短证据结论；证据不足也要明确写出"},
                "unknowns_json": {"type": "string", "default": "[]", "description": "未知项/未解决问题 JSON 数组"},
            },
            "required": ["name", "queries_json", "included_papers_json", "evidence_summary"],
        },
        allowed_node_types=["literature"],
        risk_level="low",
    ),
    _finalize_evidence_package,
)

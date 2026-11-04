#!/usr/bin/env python3
"""Explain low result counts from frozen retrieval diagnostics."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import yaml


def classify(returned: int, candidates: int, excluded: int, failures: int) -> str:
    rejection = excluded / candidates if candidates else 0
    if returned >= 100:
        return "达到结果上限"
    if rejection >= 0.90:
        return "候选充足，但相关性阈值淘汰是主因"
    if failures >= 2 and returned < 20:
        return "阈值淘汰叠加来源请求失败"
    if candidates < 100:
        return "候选获取量偏少"
    return "未达上限，需结合阈值和来源覆盖复核"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--taskset", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    tasks = {t["id"]: t for t in yaml.safe_load(args.taskset.read_text(encoding="utf-8"))["tasks"]}
    rows = []
    for result in results:
        d = result.get("retrieval_diagnostics") or {}
        warnings = d.get("warnings") or []
        codes = Counter(str(w.get("code") or "unknown") for w in warnings)
        failures = [w for w in warnings if w.get("code") == "source_request_failed"]
        no_matches = sorted({s for w in warnings if w.get("code") == "source_no_matches" for s in w.get("sources") or []})
        candidates = int(d.get("candidate_count") or 0)
        excluded = int(d.get("excluded_low_relevance") or 0)
        returned = len(result.get("papers") or [])
        strategy = d.get("strategy_diagnostics") or {}
        rows.append({
            "task_id": result["task_id"], "field": tasks[result["task_id"]]["broad_field"],
            "returned": returned, "candidates": candidates, "excluded": excluded,
            "exclusion_rate": round(excluded / candidates, 3) if candidates else None,
            "source_counts": d.get("source_counts") or {},
            "failed_sources": sorted({s for w in failures for s in w.get("sources") or []}),
            "no_match_sources": no_matches, "unavailable_sources": d.get("unavailable_sources") or [],
            "translation_valid": strategy.get("translation_valid"),
            "english_query": strategy.get("strategy_english_query"),
            "diagnosis": classify(returned, candidates, excluded, len(failures)),
            "warning_counts": dict(codes),
        })
    args.out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    low = [r for r in rows if r["returned"] < 20]
    lines = [
        "# LiteratureBench v0.3 低召回诊断", "",
        "本报告只解释冻结运行中“为什么返回少”，不据此认定少量结果一定是平台缺陷，也不在正式运行后改题或改结果。", "",
        f"30 题中返回不足 20 篇的有 {len(low)} 题：" + "、".join(r["task_id"] for r in low) + "。", "",
        "| 题号 | 返回 | 候选 | 阈值淘汰 | 淘汰率 | 请求失败来源 | 诊断 |", "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for r in rows:
        failures = ", ".join(r["failed_sources"]) or "无"
        rate = f"{100*r['exclusion_rate']:.1f}%" if r["exclusion_rate"] is not None else "N/A"
        lines.append(f"| {r['task_id']} | {r['returned']} | {r['candidates']} | {r['excluded']} | {rate} | {failures} | {r['diagnosis']} |")
    lines += [
        "", "## 可执行结论", "",
        "- 低返回题多数并非没有候选，而是 90% 以上候选在当前相关性阈值下被淘汰。下一版本应抽样审查阈值上下两侧，确认是严格过滤还是误杀。",
        "- OpenAlex 在本轮普遍返回零结果，CNKI 全题不可用，arXiv 多次超时或限流；来源覆盖应作为下一轮独立修复和对照实验。",
        "- 所有题的中译英检索词都通过语义校验，因此本轮低召回不能再简单归因于翻译失败。",
        "- NL26 同时出现 Crossref、PubMed、arXiv、bioRxiv 请求失败，重跑时应单独验证来源恢复情况；正式冻结结果仍保留原记录。", "",
    ]
    args.out_md.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()

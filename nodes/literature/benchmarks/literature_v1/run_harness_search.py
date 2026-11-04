#!/usr/bin/env python3
"""以统一结果格式运行 Harness 学术搜索主任务集。

使用环境变量而非任何固定端口、账号或口令：
  BENCHMARK_API_BASE=http://127.0.0.1:28081/api/v1
  BENCHMARK_ACCESS_TOKEN=...               # 可直接提供 token
  # 或 BENCHMARK_EMAIL / BENCHMARK_PASSWORD 由脚本登录
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent


def api_base() -> str:
    value = os.environ.get("BENCHMARK_API_BASE", "").rstrip("/")
    if not value:
        raise RuntimeError("缺少 BENCHMARK_API_BASE，例如 http://127.0.0.1:28081/api/v1")
    return value


def request_json(base: str, path: str, body: dict[str, Any], token: str | None, timeout: int) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def access_token(base: str, timeout: int) -> str:
    token = os.environ.get("BENCHMARK_ACCESS_TOKEN")
    if token:
        return token
    email = os.environ.get("BENCHMARK_EMAIL")
    password = os.environ.get("BENCHMARK_PASSWORD")
    if not email or not password:
        raise RuntimeError("请设置 BENCHMARK_ACCESS_TOKEN，或同时设置 BENCHMARK_EMAIL / BENCHMARK_PASSWORD")
    return str(request_json(base, "/auth/login", {"email": email, "password": password}, None, timeout)["access_token"])


def abstract_status(paper: dict[str, Any]) -> tuple[str, str]:
    provenance = str((paper.get("metadata_provenance") or {}).get("abstract") or "")
    if paper.get("abstract"):
        if provenance and provenance not in {paper.get("source"), "local_index"}:
            return "enriched_source", provenance
        return "source", provenance or str(paper.get("source") or "unknown")
    if paper.get("ai_summary"):
        return "ai_summary", "ai_summary"
    return "missing", provenance


def normalize_papers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    papers = []
    for rank, paper in enumerate(payload.get("papers") or [], start=1):
        status, provenance = abstract_status(paper)
        papers.append({
            "rank": rank,
            "title": paper.get("title") or "",
            "doi": paper.get("doi") or "",
            "url": paper.get("url") or paper.get("doi_url") or "",
            "authors": paper.get("authors") or [],
            "year": paper.get("year"),
            "venue": paper.get("venue") or "",
            "abstract": paper.get("abstract") or "",
            "abstract_status": status,
            "abstract_provenance": provenance,
            "source": paper.get("source") or "",
            "publication_category": paper.get("publication_category") or "",
            "citations": paper.get("citations"),
            "score": paper.get("score"),
            "score_breakdown": paper.get("score_breakdown") or {},
            "ranking_score": paper.get("ranking_score"),
            "cas_quartile": paper.get("cas_quartile"),
            "impact_factor": paper.get("impact_factor"),
            "metadata_provenance": paper.get("metadata_provenance") or {},
        })
    return papers


def classify_response(response: dict[str, Any]) -> tuple[str, str]:
    """A valid HTTP response can still be an unusable retrieval result."""
    if not isinstance(response.get("papers"), list) or response.get("status") == "error":
        return "error", "invalid_paper_response"
    if not response["papers"]:
        return "partial", "empty_results"
    return "success", "success"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--taskset", type=Path, default=HERE / "tasks" / "esi22_topics_v1.yaml")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ids", help="逗号分隔的题号；缺省运行任务集中的全部题目")
    parser.add_argument("--pilot", action="store_true", help="只运行 taskset 中 pilot=true 的题目")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--remote-refresh", action="store_true", help="显式要求每题访问远端源，不复用查询缓存")
    parser.add_argument("--system-name", default="Harness 学术搜索")
    parser.add_argument("--system-mode", default="academic_search")
    args = parser.parse_args()
    if args.out.exists():
        parser.error('结果文件已存在，请选择新的运行文件名')

    # YAML 只用于读取任务；缺少 PyYAML 时给出可操作错误，而不是静默跑错题集。
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("运行任务集需要 PyYAML") from exc
    taskset = yaml.safe_load(args.taskset.read_text(encoding="utf-8")) or {}
    tasks = taskset.get("tasks") or []
    wanted = set(args.ids.split(",")) if args.ids else None
    if args.pilot:
        tasks = [task for task in tasks if task.get("pilot")]
    if wanted is not None:
        tasks = [task for task in tasks if task.get("id") in wanted]
    if not tasks:
        raise RuntimeError("没有匹配到任务")

    base = api_base()
    token = access_token(base, args.timeout)
    results = []
    raw_dir = args.out.parent / (args.out.stem + '_raw')
    raw_dir.mkdir(parents=True, exist_ok=False)
    for task in tasks:
        started = time.monotonic()
        attempt: dict[str, Any] = {"started_at_utc": datetime.now(UTC).isoformat()}
        try:
            response = request_json(
                base,
                "/literature/search",
                {"query": task["prompt_zh"], "limit": args.limit, "remote_refresh": args.remote_refresh},
                token,
                args.timeout,
            )
            raw_path = raw_dir / (task['id'] + '.json')
            raw_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding='utf-8')
            attempt['raw_evidence_path'] = str(raw_path)
            status, outcome = classify_response(response)
            attempt["elapsed_seconds"] = round(time.monotonic() - started, 3)
            attempt["outcome"] = outcome
            papers = normalize_papers(response) if status != "error" else []
            diagnostics = {
                "total": response.get("total"),
                "candidate_count": response.get("candidate_count"),
                "excluded_low_relevance": response.get("excluded_low_relevance"),
                "source_counts": response.get("source_counts") or {},
                "requested_sources": response.get("requested_sources") or [],
                "attempted_sources": response.get("attempted_sources") or [],
                "unavailable_sources": response.get("unavailable_sources") or [],
                "warnings": response.get("warnings") or [],
                "strategy_diagnostics": response.get("strategy_diagnostics") or {},
                "search_mode": response.get("search_mode"),
                "from_cache": response.get("from_cache"),
                "timings": response.get("timings") or {},
            }
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout, RuntimeError, ValueError) as exc:
            attempt["elapsed_seconds"] = round(time.monotonic() - started, 3)
            attempt["outcome"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            status = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "error"
            if isinstance(exc, urllib.error.HTTPError):
                attempt['http_status'] = exc.code
                attempt['retry_after'] = exc.headers.get('Retry-After')
                error_path = raw_dir / (task['id'] + '_error.json')
                error_path.write_text(json.dumps({
                    'http_status': exc.code,
                    'retry_after': attempt['retry_after'],
                    'response_body': exc.read(4096).decode('utf-8', errors='replace'),
                }, ensure_ascii=False, indent=2), encoding='utf-8')
                attempt['raw_evidence_path'] = str(error_path)
                attempt['error_body_captured'] = True
                if exc.code == 429:
                    status = 'rate_limited'
            papers = []
            diagnostics = {}
        results.append({
            "benchmark_version": str(taskset.get("version") or "unknown"),
            "system": {"name": args.system_name, "mode": args.system_mode},
            "task_id": task["id"],
            "prompt": task['prompt_zh'],
            "request_settings": {"limit": args.limit, "timeout": args.timeout, "remote_refresh": args.remote_refresh},
            "status": status,
            "attempts": [attempt],
            "retrieval_diagnostics": diagnostics,
            "papers": papers,
        })
        print(f"{task['id']} {status}: {len(papers)} papers, {attempt['elapsed_seconds']}s", flush=True)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

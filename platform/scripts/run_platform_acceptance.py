#!/usr/bin/env python3
"""Run named live-product acceptance cases through the canonical Session API.

This runner deliberately uses the same Project Session and SSE endpoints as the
browser.  It is not a model mock and it does not decide scientific quality; its
JSON report gives a reviewer stable Session URLs, Run ids, messages, and staged
outputs for browser/content inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_API = "http://127.0.0.1:8000/api/v1"
DEFAULT_WEB = "http://127.0.0.1:3000"
DEFAULT_EMAIL = "researcher@atrium.local"


@dataclass(frozen=True)
class Case:
    title: str
    prompt: str


CASES: dict[str, Case] = {
    "QA-01": Case(
        "QA-01 · concise instruction following",
        "请只用一句简洁中文回答，不要调用任何工具：为什么科研报告必须区分观测与推断？",
    ),
    "CTX-01": Case(
        "CTX-01 · authorized project context",
        "只根据当前项目上下文，告诉我项目名称、当前模型后端和基线修订号；不要猜测缺失信息。",
    ),
    "LIT-01": Case(
        "LIT-01 · heterogeneous MoE literature",
        (
            "调研 heterogeneous Mixture-of-Experts（异构 MoE）的专家并行、路由与负载均衡。"
            "给出至少 5 篇主题真正相关且可核验的论文，区分已核验事实与综合判断，形成简短调研报告"
            "和文献索引。没有全文不等于没有可核验元数据，不要因为缺少全文停下来问我。"
        ),
    ),
    "CALC-01": Case(
        "CALC-01 · transparent calculation",
        "用 Python 计算 [1,2,3,4,5] 的均值和总体标准差，明确公式、标准差约定和精确结果；不要生成无关文件。",
    ),
    "EXP-01": Case(
        "EXP-01 · reproducible Monte Carlo experiment",
        (
            "做一个固定随机种子 42、样本数 10000 的 Monte Carlo 圆周率估计实验。记录方法、参数、"
            "结果、绝对误差、Python 版本和依赖环境，并保存可复现实验记录。"
        ),
    ),
    "FIG-01": Case(
        "FIG-01 · convergence figure",
        (
            "先用固定随机种子 42 运行一组 Monte Carlo 圆周率估计，再绘制样本数量增加时估计值的"
            "收敛图。坐标轴、图例和中文图注完整，保存为 PNG 并说明数据来源。"
        ),
    ),
    "PDF-01": Case(
        "PDF-01 · technical PDF",
        (
            "运行一个固定随机种子 42、样本数 10000 的 Monte Carlo 圆周率估计，把实验方法、结果、"
            "收敛图、局限性和可复现信息整理成简洁技术报告 PDF；不要编造参考文献。"
        ),
    ),
    "ENV-01": Case(
        "ENV-01 · bounded environment deployment",
        (
            "在隔离目录创建一个最小 Python 虚拟环境，运行只依赖标准库的健康检查，记录 Python "
            "版本、平台、命令、退出码和环境清单；不要启动持久服务。"
        ),
    ),
}


class Client:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, timeout: int = 900
    ) -> tuple[Any, dict[str, str]]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                return (json.loads(raw) if raw else None), dict(response.headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc

    def stream(self, path: str, body: dict[str, Any], timeout: int = 1800) -> list[dict]:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method="POST"
        )
        events: list[dict] = []
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"POST {path} -> HTTP {exc.code}: {detail}") from exc
        return events


def run_case(
    client: Client, *, case_id: str, project_id: str, web_url: str
) -> dict[str, Any]:
    case = CASES[case_id]
    started = time.time()
    session, _ = client.request(
        "POST",
        "/chat/conversations",
        {"project_id": project_id, "title": case.title},
    )
    session_id = session["id"]
    events = client.stream(
        f"/chat/projects/{project_id}/stream",
        {"answer": {"kind": "text", "text": case.prompt}, "conversation_id": session_id},
    )
    terminal = next(
        (event for event in reversed(events) if event.get("type") in {"done", "error"}),
        None,
    )
    messages, _ = client.request(
        "GET", f"/projects/{project_id}/sessions/{session_id}/messages"
    )
    changes, _ = client.request(
        "GET", f"/projects/{project_id}/sessions/{session_id}/change-set"
    )
    run_id = terminal.get("run_id") if isinstance(terminal, dict) else None
    run = None
    if run_id:
        run, _ = client.request("GET", f"/runs/{run_id}")
    return {
        "caseId": case_id,
        "title": case.title,
        "prompt": case.prompt,
        "sessionId": session_id,
        "sessionUrl": f"{web_url.rstrip('/')}/projects/{project_id}/sessions/{session_id}",
        "elapsedSeconds": round(time.time() - started, 3),
        "terminal": terminal,
        "eventCount": len(events),
        "messages": messages,
        "changeSet": changes,
        "run": run,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--web-url", default=DEFAULT_WEB)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--case", action="append", choices=sorted(CASES))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    password = os.environ.get("IEIT_ACCEPTANCE_PASSWORD")
    if not password:
        parser.error("set IEIT_ACCEPTANCE_PASSWORD; credentials are never accepted on argv")

    client = Client(args.api_url)
    auth, _ = client.request(
        "POST", "/auth/login", {"email": args.email, "password": password}
    )
    client.token = auth["access_token"]
    selected = args.case or list(CASES)
    report = {
        "projectId": args.project_id,
        "apiUrl": args.api_url,
        "cases": [],
    }
    for case_id in selected:
        print(f"[{case_id}] running real Session...", file=sys.stderr, flush=True)
        try:
            result = run_case(
                client, case_id=case_id, project_id=args.project_id, web_url=args.web_url
            )
        except Exception as exc:  # retain partial multi-case report for triage
            result = {"caseId": case_id, "runnerError": str(exc)}
        report["cases"].append(result)
        terminal = result.get("terminal") or {}
        print(
            f"[{case_id}] {terminal.get('type', 'runner-error')} "
            f"run={terminal.get('run_id', '-')}",
            file=sys.stderr,
            flush=True,
        )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if any("runnerError" in case for case in report["cases"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())

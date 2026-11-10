"""User-scoped CNKI/school source adapter; standalone runs report unavailability."""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import time


async def call(op: str, payload: dict) -> dict:
    config_path = os.environ.get("HARNESS_LITERATURE_ACCESS_FILE")
    if not config_path:
        raise ConnectionError("platform_source_access_not_configured")
    config = json.loads(Path(config_path).read_text())
    if config.get("host") != "127.0.0.1":
        raise ValueError("source_endpoint_must_be_loopback")
    writer = None
    try:
        async with asyncio.timeout(35):
            reader, writer = await asyncio.open_connection("127.0.0.1", int(config["port"]), limit=16*1024*1024)
            writer.write(json.dumps({"token": config["token"], "op": op, "payload": payload}).encode() + b"\n")
            await writer.drain()
            response = json.loads(await reader.readline())
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "platform_source_failed"))
        return response["result"]
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()


from shared.contrib.literature_federation import merge_channels as merge_results


async def collect_sources(query: str, plan: list[dict], *, cnki: bool, school: bool) -> dict:
    result = {"papers": [], "warnings": [], "sources": {}}
    if not cnki and not school:
        return result
    if not os.environ.get("HARNESS_LITERATURE_ACCESS_FILE"):
        result["warnings"].append({"code": "platform_sources_unavailable", "sources":
            [name for name, enabled in (("cnki", cnki), ("school", school)) if enabled],
            "detail": "当前运行未连接平台私有来源；知网和学校库未执行，不代表没有匹配文献。"})
        return result

    async def school_search():
        if not school:
            return []
        try:
            response = await call("school", {"query": query})
            papers = response.get("papers", [])
            result["sources"]["school"] = {"state": "completed", "count": len(papers)}
            return papers
        except Exception as exc:
            result["warnings"].append({"code": "school_unavailable", "sources": ["school"], "detail": type(exc).__name__})
            return []

    async def chinese_search():
        if not cnki:
            return []
        queries = list(dict.fromkeys(str(group.get("zh") or "").strip() for group in plan))
        queries = [q for q in queries if q][:6]
        jobs = {}
        deadline = time.monotonic() + 120
        try:
            available = await call("cnki_availability", {})
            if not available.get("available"):
                result["sources"]["cnki"] = {"state": "unavailable", "detail": available.get("detail", "")}
                result["warnings"].append({"code": "cnki_unavailable", "sources": ["cnki"],
                    "detail": available.get("detail", "知网采集不可用")})
                return []
            for item in queries:
                job = await call("cnki_submit", {"query": item, "limit": math.ceil(200 / len(queries))})
                jobs[job["id"]] = job
            while jobs and time.monotonic() < deadline:
                pending = [j for j in jobs.values() if j.get("state") in {"queued", "working"}]
                if not pending:
                    break
                await asyncio.sleep(2.5)
                for job in pending:
                    jobs[job["id"]] = await call("cnki_status", {"job_id": job["id"]})
        except Exception as exc:
            result["warnings"].append({"code": "cnki_unavailable", "sources": ["cnki"], "detail": type(exc).__name__})
        result["sources"]["cnki"] = {"jobs": [
            {k: job.get(k) for k in ("id", "query", "state", "count", "detail")} for job in jobs.values()]}
        incomplete = [j for j in jobs.values() if j.get("state") != "completed"]
        if incomplete:
            result["warnings"].append({"code": "cnki_pending", "sources": ["cnki"],
                "detail": "知网任务尚未全部完成；已保留任务与题录，不能据此判断无匹配文献。"})
        return merge_results(*(job.get("result", {}).get("papers", []) for job in jobs.values()))

    async with asyncio.TaskGroup() as group:
        school_task = group.create_task(school_search())
        chinese_task = group.create_task(chinese_search())
    result["papers"] = merge_results(chinese_task.result(), school_task.result())
    result["cnki_papers"] = chinese_task.result()
    result["school_papers"] = school_task.result()
    return result

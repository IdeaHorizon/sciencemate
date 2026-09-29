#!/usr/bin/env python3
"""把一条真实 run 的**状态轨迹**导成回放用例。

RFC 异步运行时 D11 的验收条款写死了一句：

    验收必须是真实历史回放 —— 单测造不出这个形状（要连着 pause/resume/
    多轮 turn 才显形）。

8-21 那条 run 收到两次终态、8-21 晚那条 run「出生 9 秒即死」，都是构造单测
抓不到的：它们要的不是某一次调用的返回值，是**一条 run 一生的状态序列**。

导出的不是全量事件（born-dead 那条 1673 条、3.3MB），只留判 D11 需要的两样：

  1. 每条 run 的生命周期事件（`run.*`）—— 投影器说过的话；
  2. 每条 run 最终落库的 status + summary 里平台写下的猜测字段 ——
     平台自己动手写的那部分。

不变量就在这两样的**差集**里：status 上出现过、事件流里却找不到依据的那些
取值，全是平台凭空写的（D11 要拆掉的写点）。

用法（在能连到目标库的机器上）：
    python scripts/export_run_replay.py run_9aaa6b79… out.json --host node20
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import subprocess

#: 平台侧"替 worker 收尾"时写进 summary 的字段。它们的存在本身就是证据：
#: 有人在投影器之外对这条 run 下了判决。
PLATFORM_VERDICT_FIELDS = ("staleReason", "staleFromStatus", "staleDetectedAt", "pauseAbandonedAt")

_SQL = """select json_build_object(
 'rootRunId', $Q${run}$Q$,
 'runs', (select json_agg(json_build_object('id', r.id, 'parentRunId', r.parent_run_id,
     'nodeType', r.node_type, 'status', r.status, 'summary', r.summary) order by r.started_at)
   from runs r where r.id like $Q${run}%$Q$),
 'lifecycle', (select json_agg(json_build_object('runId', e.run_id, 'sequence', e.sequence,
     'kind', e.kind, 'payload', e.payload) order by e.sequence)
   from execution_events e
   where e.run_id like $Q${run}%$Q$ and e.kind like $Q$run.%$Q$))"""


def export(run_id: str, host: str, container: str) -> dict:
    sql = _SQL.replace("{run}", run_id)
    remote = (
        f"docker exec {shlex.quote(container)} psql -U postgres -d research_platform "
        f"-A -t -c {shlex.quote(sql)}"
    )
    raw = subprocess.run(["ssh", host, remote], capture_output=True, text=True, check=True)
    payload = json.loads(raw.stdout.strip())
    for run in payload.get("runs") or []:
        summary = run.get("summary") or {}
        # 只留判决字段：研究正文、模型名、成本都不是 D11 的判据，也不该进仓库。
        # 值为 None 不算判决痕迹 —— 字段存在但没写，等于没写。
        run["summary"] = {
            k: summary[k] for k in PLATFORM_VERDICT_FIELDS if summary.get(k) is not None
        }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("out")
    parser.add_argument("--host", default="node20")
    parser.add_argument("--container", default="ieit-review-postgres")
    args = parser.parse_args()
    payload = export(args.run_id, args.host, args.container)
    pathlib.Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{args.out}: runs={len(payload['runs'] or [])} "
          f"lifecycle={len(payload['lifecycle'] or [])}")


if __name__ == "__main__":
    main()

"""`declare_job` / `job_progress` —— 后台计算作业的登记与进度回报。

设计动机与不变量见 `core/jobs.py` 的模块文档。这里只做工具层的壳。

两个工具刻意分开：
  - `declare_job` 起作业时调一次（回答「这是什么、占什么、要多久、怎么查进度」）
  - `job_progress` 每次醒来查一眼时调（把实测进度写回登记表）

第二个才是让「预计 vs 实际「这条判据活起来的那一半：只登记不回报，登记表里的
ETA 就永远是开跑时那个静态估计，**跟「该不该干预「没关系了**。
"""
from __future__ import annotations

from typing import Any

from core import jobs as _jobs
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


async def _declare_job(
    state: State, purpose: str, expected_duration_s: float,
    resources: str = "", progress_probe: str = "",
    pid: int | None = None, scheduler_job_id: str | None = None,
    note: str = "", **_: Any,
) -> dict:
    # purpose 非空、expected_duration_s > 0 由 parameters_schema 声明
    # （minLength:1 / exclusiveMinimum:0），派发口核一次。> 0 有真实机械
    # 消费者：overrun_ratio（「跑超时了该不该干预」的唯一判据）。
    try:
        exp = float(expected_duration_s or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if getattr(state, "project_root", None) is None:
        return {"status": "error",
                "error": "本 run 没有 project_root，作业登记表无处持久化"}

    rec = _jobs.declare(
        state, purpose=purpose, resources=resources,
        expected_duration_s=exp, progress_probe=progress_probe,
        pid=pid, scheduler_job_id=scheduler_job_id, note=note)
    state.append_transcript(
        "job_declared", job_id=rec.job_id, purpose=purpose,
        resources=resources, expected_duration_s=exp, pid=pid)
    return {
        "status": "success", "job_id": rec.job_id,
        "note": ("已登记。醒来查进度时调 job_progress(job_id, progress=...) 回报，"
                 "作业结束调 job_progress(job_id, status='done')。"
                 "**登记表跨 run 存活** —— 本 run 撞 max_turns 结束后，"
                 "下一个 run 靠它才知道这个作业还在跑。"),
    }


async def _job_progress(
    state: State, job_id: str = "", progress: str = "",
    status: str = "", **_: Any,
) -> dict:
    open_jobs = _jobs.load(state, only_open=True)
    if not job_id:
        # 只有一个在跑时允许省略 —— 常见场景，不必让模型去记 id
        if len(open_jobs) == 1:
            job_id = open_jobs[0].job_id
        else:
            return {"status": "error",
                    "error": f"job_id 必填（当前 open 作业 {len(open_jobs)} 个）",
                    "open_jobs": [j.job_id for j in open_jobs]}
    # status 枚举由 parameters_schema 声明，派发口核一次。

    # 2026-08-05 e2e9 实测：declare_job 调了 2 次，job_progress 调了 10 次 ——
    # 有人只报进度没登记，登记表里就凭空长出一条
    #   purpose=None  resources=None  expected_duration_s=None
    # 的无头作业。而 expected_duration_s 正是 overrun_ratio 的唯一依据，
    # 没有它「该不该干预」就不可判。半条记录比没有记录更坏：调度器以为自己
    # 看得见，其实什么都不知道。
    known = {j.job_id for j in _jobs.load(state)}
    if job_id not in known:
        return {
            "status": "error",
            "error": (
                f"作业 {job_id!r} 没登记过 —— 先 declare_job(purpose=..., "
                f"resources=..., expected_duration_s=...) 再报进度。\n"
                f"只报进度会在登记表里留下一条不知道用途、不知道占什么、"
                f"不知道该跑多久的半截记录，overrun_ratio 算不出来，"
                f"「该不该干预」就没了判据。"
            ),
            "known_jobs": sorted(known)[:10],
        }

    ok = _jobs.update(state, job_id,
                      status=status or None, last_progress=progress or None)
    if not ok:
        return {"status": "error", "error": "登记表写入失败"}
    rec = next((j for j in _jobs.load(state) if j.job_id == job_id), None)
    out: dict = {"status": "success", "job_id": job_id}
    if rec:
        out.update({
            "elapsed_h": round(rec.elapsed_s / 3600, 2),
            "eta_h": None if rec.eta_s is None else round(rec.eta_s / 3600, 2),
            "overrun_ratio": rec.overrun_ratio,
            "process_alive": rec.alive(),
        })
        if rec.overrun_ratio and rec.overrun_ratio > 1.5:
            out["warning"] = (
                f"已跑到预计时长的 {rec.overrun_ratio:.1f} 倍。要么修正预计"
                f"（重新 declare_job），要么认真判断是不是卡住了 —— "
                f"别继续闷头等。")
    return out


register_tool(
    ToolDefinition(
        name="declare_job",
        description=(
            "起了后台长作业就登记一次（nohup / setsid / & 起的计算、训练、"
            "推理服务、HPC 作业）。登记后调度器和用户能直接看到：这是什么、"
            "占什么算力、还要多久 —— 不用去翻你的日志。而且登记表跨 run 存活："
            "本 run 撞 max_turns 结束后，下一个 run 靠它才知道作业还在跑。\n"
            "expected_duration_s 必填：粗估也行，但没有它，跑超时了该不该干预"
            "就没有任何机械判据。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "purpose": {"type": "string", "minLength": 1,
                            "description": "这个作业干什么用的（人话，给调度器看）"},
                "expected_duration_s": {
                    "type": "number", "exclusiveMinimum": 0,
                    "description": ("预计跑多久（秒）。粗估也行（数量级对就够），"
                                    "但必须 > 0：没有它「跑超时了该不该干预」"
                                    "就没有任何机械判据")},
                "resources": {"type": "string",
                              "description": "占什么算力，如 4×A100 (GPU4,5,6,7)"},
                "progress_probe": {
                    "type": "string",
                    "description": "怎么查进度：一条命令或一个会变长的文件路径"},
                "pid": {"type": "integer", "description": "后台进程 pid（有就填）"},
                "scheduler_job_id": {"type": "string",
                                     "description": "HPC 调度器作业号（走 submit_job 时）"},
                "note": {"type": "string"},
            },
            "required": ["purpose", "expected_duration_s"],
        },
    ),
    _declare_job,
)

register_tool(
    ToolDefinition(
        name="job_progress",
        description=(
            "回报后台作业的实测进度 / 收尾。每次醒来查一眼就调一次 —— "
            "只登记不回报的话，登记表里的 ETA 永远是开跑时那个静态估计，"
            "预计 vs 实际这条判据就废了。\n"
            "作业结束时调 status=done（或 failed / cancelled）。"
            "只有一个在跑的作业时 job_id 可省略。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string",
                           "description": "只有一个 open 作业时可省略"},
                "progress": {"type": "string",
                             "description": "实测进度，如 41/118 shard，约 1.2/min"},
                "status": {"type": "string",
                           "enum": ["running", "done", "failed", "cancelled", "unknown"]},
            },
        },
    ),
    _job_progress,
)

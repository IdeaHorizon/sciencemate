"""主 harness 控制 child run 的两个运行时工具：inject_into_node + cancel_node。

主要场景：
  - 主 harness（orchestrator）拿到 user 中途反馈 → 跑决策 → 用这两个工具直接
    操作正在跑（或已 paused）的 child run 的 state.hook_state
  - child agent loop 每轮 turn_start 自动检测 hook_state 里的 kill_signal /
    injected_messages，反应（取消退出 / 把内容 inject 成 system message）

这是把"user 中途打断 → orchestrator decide → child 调整"这条路径走通的关键。

注：工具本身不调 LLM —— 它们只**应用决策**。决策（要 inject 什么 / 是否 cancel）
是 orchestrator LLM 跑出来的，由 orchestrator system prompt 教它什么时候用。

测试模式（run_node.py 单节点）：owner 跳过 orchestrator 直接当 inject 内容写，
即"fake-orchestrator"模式。
"""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from core.pause import find_child_state, list_active_runs
from core.tool_registry import ToolDefinition, register_tool

# ── "找不到 child_run_id" 要说清是哪一种找不到 ───────────────────────────
# registry 是**进程内**的：平台一重启，在跑的 run 全部从 registry 消失，但它们
# 的目录和 transcript 还在盘上。此前三处只回一句"未在 active/paused registry"
# ＋一份 active 列表（重启后恒为空），调度器据此无法区分"打错了 id"、"它已经
# 跑完了"和"进程没了但活儿还在"，于是原地重试。判据全部从盘上现算。


def _child_disk_status(state, child_run_id: str) -> tuple[str, str]:
    """(kind, 人话说明)。kind ∈ unknown / finished / interrupted。"""
    try:
        transcript = Path(state.root).parent / child_run_id / "transcript.jsonl"
        if not transcript.is_file():
            return ("unknown", "本会话磁盘上没有这个 run 目录 —— 多半是 run id 记错了。")
        finished = False
        with transcript.open(encoding="utf-8") as fh:
            for line in fh:
                if '"run_end"' in line:
                    finished = True
        if finished:
            return ("finished",
                    "该 run **已经结束**（transcript 里有 run_end）。registry 只登记在跑的 run，"
                    "结束的不在里面 —— 它的产物和 summary 直接读盘即可。")
        return ("interrupted",
                "该 run 有 run_start 没有 run_end，且进程已不在 —— 平台重启或被停止打断了它。"
                "registry 是进程内的，重启不会恢复。要接着跑它："
                f"run_node(node_type=..., resume_run_id={child_run_id!r})。")
    except Exception as exc:  # 盘读不了也要给个说法，不能静默退化成老文案
        return ("unknown", f"无法判读该 run 的盘上状态：{type(exc).__name__}: {exc}")


def _missing_child_error(state, child_run_id: str) -> dict:
    kind, detail = _child_disk_status(state, child_run_id)
    active = [r.run_id for r in list_active_runs()]
    return {
        "status": "error",
        "error": (
            f"找不到 child_run_id={child_run_id!r}（未在 active/paused registry）。\n"
            f"{detail}\n"
            f"当前 registry 里在跑的：{active or '（空）'}"
        ),
        "child_run_kind": kind,
        "active_run_ids": active,
    }


# ── inject_into_node ─────────────────────────────────────────────────────────

async def _inject_into_node(
    *, state, child_run_id: str, content: str,
    source: str = "orchestrator_relay", **_,
) -> dict:
    # child_run_id / content 的必填由 dispatcher `_runtime_control` 一处查；
    # 本函数只经它到达。
    child_state = find_child_state(child_run_id)
    if child_state is None:
        return _missing_child_error(state, child_run_id)

    queue = child_state.hook_state.setdefault("injected_messages", [])
    queue.append({
        "content": content.strip(),
        "source": source,
        "injected_at": datetime.now(UTC).isoformat(),
        "injected_by_run_id": state.run_id,
        "injected_by_node_type": state.node_type,
    })
    state.append_transcript(
        "inject_into_node",
        child_run_id=child_run_id,
        content_preview=content[:200],
        source=source,
    )
    return {
        "status": "success",
        "child_run_id": child_run_id,
        "injected_queue_depth": len(queue),
        "note": (
            "下一轮 child agent_loop turn_start 会消费 injected_messages，"
            "作 system message append 进 child messages 流"
        ),
    }

# ── cancel_node ──────────────────────────────────────────────────────────────

async def _cancel_node(
    *, state, child_run_id: str, reason: str, **_,
) -> dict:
    # reason 非空由 runtime_control 的 parameters_schema 声明（reasoning
    # minLength:1），派发口核一次；本函数只经 dispatcher 到达。
    child_state = find_child_state(child_run_id)
    if child_state is None:
        return _missing_child_error(state, child_run_id)

    child_state.hook_state["kill_signal"] = {
        "reason": reason.strip(),
        "requested_by": f"{state.node_type}:{state.run_id}",
        "requested_at": datetime.now(UTC).isoformat(),
    }
    # 2026-07-14：光写 hook_state 不够——child 若正阻塞在 safe_run_bash 等子进程
    # 里，agent_loop 的 turn_start 检查要等这个工具调用返回才轮到，实测卡了 9 分钟
    # （直到内层 timeout 参数自然到期）。kill_event 让阻塞中的 run_bash 立刻响应。
    child_state.kill_event.set()
    state.append_transcript(
        "cancel_node_requested",
        child_run_id=child_run_id,
        reason=reason,
    )
    return {
        "status": "success",
        "child_run_id": child_run_id,
        "note": (
            "kill_signal 已写入 child state.hook_state 且 kill_event 已 set；"
            "若 child 正阻塞在 run_bash 类调用中会立刻中断，否则下一轮 turn_start "
            "检测到退出，status='cancelled'。"
            "已 paused 的 child：要先 resume 一次（如答 'cancelled'）才会触发 kill 检查。"
        ),
    }

# ── list_active_child_runs（辅助查询）────────────────────────────────────────

async def _list_active_child_runs(*, state, **_) -> dict:
    runs = list_active_runs()
    return {
        "status": "success",
        "active_runs": [
            {
                "run_id": r.run_id,
                "node_type": r.node_type,
                "parent_run_id": r.parent_run_id,
                "started_at": r.started_at,
                "sub_run_id": r.sub_run_id,
            }
            for r in runs
        ],
        "count": len(runs),
    }

# ── progress（辅助查询：偷看正在跑 child 的最近 transcript 事件）──────────────

def _tail_transcript_events(transcript_path, n: int) -> list[dict]:
    """读 transcript.jsonl 最后 n 条有效 JSON 行。文件不存在/为空返回 []。"""
    if not transcript_path.exists():
        return []
    lines = transcript_path.read_text(encoding="utf-8").splitlines()
    events = []
    for line in lines[-max(n, 1) * 3:]:   # 多读几行防个别行解析失败仍凑够 n 条
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events[-n:]


def _format_progress_event(ev: dict) -> str:
    event = ev.get("event", "?")
    at = (ev.get("at") or "")[11:19]   # 只留 HH:MM:SS
    # experiment_progress 事件（nodes/experiment/hooks.py）有 stage/detail，最有信息量
    if event == "experiment_progress":
        stage = ev.get("stage", "?")
        detail = ev.get("detail", "")
        return f"[{at}] {stage}: {detail}"[:200]
    # 通用兜底：event 名 + 前几个非元字段
    skip = {"event", "at"}
    extras = ", ".join(
        f"{k}={v}" for k, v in ev.items()
        if k not in skip and v not in ("", None, [], {})
    )
    return f"[{at}] {event}" + (f" ({extras})" if extras else "")[:200]


def _event_ts(ev: dict) -> float:
    """事件时间 → epoch 秒。解析不了返回 0。"""
    import datetime as _dt
    raw = ev.get("at") or ""
    try:
        return _dt.datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _pace_summary(events: list[dict]) -> dict:
    """这个 child 现在等了多久 / 它平常每轮多久。

    v3.9（E2E-4 实测，wangd 指出"别加规矩，先看是什么造成的"）：
    progress 只给事件名 + HH:MM:SS，**不给任何时间尺度**。调度器看到最后一条是
    `llm_request`（= 已发出、在等模型回复），无从判断这是正常还是卡死，就自己
    脑补成"卡住了"，然后 cancel。writing 节点被这样掐了 **10 次**，每次理由都是
    同一句 "llm_request stuck for extended period" —— 而 "extended period" 是猜的。
    真实情况是机器 load 130、writing 在编译 LaTeX，等两三分钟完全正常。

    根因不是"调度器没耐心"，是**没给它判断快慢所需的信息**。同一条契约的又一次
    违背：决策者需要的信息不给全，它就只能猜，猜错了不怪它。
    这里补上时间维度，不加任何机械拦截 —— 先给信息，看它自己会不会好。
    """
    import time as _time

    ts = [_event_ts(e) for e in events]
    ts = [x for x in ts if x > 0]
    if not ts:
        return {}
    now = _time.time()
    last = max(ts)
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b >= a]
    typical = sorted(gaps)[len(gaps) // 2] if gaps else None
    out = {
        "seconds_since_last_event": round(now - last, 1),
        "last_event": (events[-1].get("event") if events else None),
    }
    if typical is not None:
        out["typical_gap_seconds"] = round(typical, 1)
        out["max_gap_seconds"] = round(max(gaps), 1)
    return out


async def _peek_child_progress(*, state, child_run_id: str, n: int = 12, **_) -> dict:
    # child_run_id 必填由 dispatcher 一处查；本函数只经它到达。
    child_state = find_child_state(child_run_id)
    if child_state is None:
        return _missing_child_error(state, child_run_id)

    events = _tail_transcript_events(child_state.transcript_path, n)
    pace = _pace_summary(events)
    waiting = (pace.get("last_event") == "llm_request")
    since = pace.get("seconds_since_last_event")
    typical = pace.get("typical_gap_seconds")

    if waiting:
        hint = (
            f"最后一条是 `llm_request` —— child **已经把请求发给模型、正在等回复**，"
            f"这不是卡死，是正常等待。已等 {since}s"
            + (f"，该 run 事件间隔中位数 {typical}s、最长 {pace.get('max_gap_seconds')}s。"
               if typical is not None else "。")
            + " 判断依据用这些数字，不要凭感觉。集群繁忙 / 长上下文 / LaTeX 编译时"
              "单轮几分钟很常见。"
        )
    elif since is not None:
        hint = (
            f"距最后一条事件 {since}s"
            + (f"（该 run 间隔中位数 {typical}s）。" if typical is not None else "。")
        )
    else:
        hint = ""

    # 长 tool_call 期间 transcript 静默（事件要等工具返回才落）——这时唯一诚实
    # 的活性信号是 workspace 输出在不在长。15 小时僵死事故里 orchestrator 每次
    # 检查只看 transcript，只能得出"没动静"。
    ws_files = _workspace_activity(child_state)
    ws_note = ""
    if pace.get("last_event") == "tool_call":
        ws_note = (
            "最后一条是 `tool_call` —— 子节点正在一次工具调用**内部**"
            "（编译/下载/训练/HPC 作业可合法跑数小时～数天，期间 transcript "
            "静默是正常的）。像科学家盯集群作业那样判断它：看上面 "
            "workspace_recent_files 里输出文件在不在长；用你自己的 shell "
            "看命令里写的日志尾部（时间戳在不在推进、有没有 error/nan/OOM、"
            "吞吐合不合理）。**输出在长就继续等**；输出停了很久才考虑干预。")

    return {
        "status": "success",
        "child_run_id": child_run_id,
        "node_type": child_state.node_type,
        "n_events_returned": len(events),
        "recent_progress": [_format_progress_event(e) for e in events],
        # v3.9：时间维度。没有它，调度器只能凭"最后一条是 llm_request"猜卡死，
        # 实测把在干活的 writing 掐了 10 次。见 _pace_summary docstring。
        "pace": pace,
        "waiting_on_model": waiting,
        "workspace_recent_files": ws_files,
        **({"long_tool_call_note": ws_note} if ws_note else {}),
        "note": (
            "这是 child 自己 transcript 的最近事件，不是完整历史；"
            "想看更多把 n 调大。没有专门 progress hook 的节点类型也能看到 "
            "tool_call/tool_result 等通用事件，只是没有 experiment_progress "
            "那种带 stage/detail 的精炼摘要。"
            + (" " + hint if hint else "")
        ),
    }

# ─────────────────────────────────────────────────────────────────────────────
# v1.8 refine: runtime_control —— 合并 inject_into_node + cancel_node + list_active_child_runs
# v1.9: 加 progress —— 偷看正在跑 child 最近的 transcript 事件（无需等它完成）
# ─────────────────────────────────────────────────────────────────────────────

# ── 科学家式检查：长任务在飞时"看一眼输出"（E2E-5 实测需求）────────────────
#
# HPC/训练/采集类工具调用可以合法地跑几小时到几天。盯这种作业的正确动作不是
# 猜，是**看输出**：日志在不在滚、数据合不合理、大概还要多久 —— 跟科学家盯
# 集群作业一模一样。transcript 在长 tool_call 期间是静默的（事件要等工具返回
# 才落），所以 progress 只看 transcript 会得出"没动静"的误判 —— 15 小时僵死
# 事故里 orchestrator 每次检查都只能看到这个。这两个动作补上"看输出"：
#   - progress 附带 workspace 最近改动的文件（在不在长 = 最直接的活性信号）

_SKIP_DIRS = frozenset({"__pycache__", ".git", "node_modules", "site-packages"})


def _workspace_activity(child_state, top: int = 8) -> list[dict]:
    """child 项目 workspace 里最近修改的文件。纯观测，失败返空。

    venv/缓存树可能有上万文件 —— 有界遍历：跳过依赖目录、深度 ≤4。
    """
    root = getattr(child_state, "project_root", None)
    if not root:
        return []
    ws = Path(root) / "workspace"
    if not ws.is_dir():
        return []
    out: list[tuple[float, int, str]] = []
    base_depth = len(ws.parts)
    try:
        for cur, dirs, files in os.walk(ws):
            dirs[:] = [d for d in dirs
                       if d not in _SKIP_DIRS and not d.endswith("venv")]
            if len(Path(cur).parts) - base_depth >= 4:
                dirs[:] = []
            for f in files:
                p = Path(cur) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append((st.st_mtime, st.st_size, str(p.relative_to(ws))))
    except OSError:
        return []
    out.sort(reverse=True)
    now = time.time()
    return [{"file": rel, "size": sz,
             "modified_seconds_ago": max(0, int(now - mt))}
            for mt, sz, rel in out[:top]]



_RC_ACTIONS = ("inject", "cancel", "list_active", "progress", "jobs")


async def _runtime_control(
    *,
    state,
    action: str,
    child_run_id: str = "",
    content: str = "",
    reasoning: str = "",
    source: str = "orchestrator_relay",
    n: int = 12,
    include_done: bool = False,
    **_,
) -> dict:
    """统一 child run 控制入口。`action` 决定操作。

    Args:
      action ∈
        - `inject`：把 system message 注入正在跑 / paused 的 child。
          必填 child_run_id + content。
        - `cancel`：中止 child run。必填 child_run_id + reasoning。
        - `list_active`：列当前所有 active + paused child runs。无其它参数。
        - `progress`：偷看正在跑 child 最近的 transcript 事件（不用等它完成）。
          必填 child_run_id，可选 n（默认 12 条）。

    action 枚举、content / reasoning 非空由 parameters_schema 声明、派发口核一次。
    条件必填（哪些 action 需要 child_run_id / content）schema 表达不了，只在
    这里查**一次**——内层 _inject_into_node / _cancel_node / _peek_child_progress
    不再各自重查。
    """
    if action not in ("list_active", "jobs") and not child_run_id:
        return {"status": "error",
                "error": f"action={action!r} 需要 child_run_id（list_active / jobs 不用）"}
    if action == "inject" and not (content or "").strip():
        return {"status": "error", "error": "action='inject' 需要 content"}

    if action == "jobs":
        # 后台计算作业登记表 —— 子 run 之外的那一层（见 core/jobs.py）。
        # 调度器不该为了知道实验在跑什么、占什么卡、还要多久，去翻 transcript 猜。
        from core import jobs as _jobs
        recs = _jobs.load(state, only_open=(not include_done))
        return {"status": "success",
                "jobs": [r.as_dict() for r in recs],
                "summary": (_jobs.render_for_orchestrator(state)
                            or "（当前没有在跑的后台作业）")}

    if action == "list_active":
        return await _list_active_child_runs(state=state)

    if action == "progress":
        return await _peek_child_progress(state=state, child_run_id=child_run_id, n=n)

    if action == "inject":
        return await _inject_into_node(
            state=state, child_run_id=child_run_id,
            content=content, source=source,
        )

    # action == "cancel"
    return await _cancel_node(
        state=state, child_run_id=child_run_id, reason=reasoning,
    )


register_tool(
    ToolDefinition(
        name="runtime_control",
        description=(
            "**控制正在跑 / paused 的 child run 的统一入口**。`action` 决定操作：\n\n"
            "  - `inject`：注入 system message 到 child 的 message 流。child 下轮\n"
            "    LLM 看到，自调整方向。必填 child_run_id + content。\n"
            "  - `cancel`：中止 child run。child 下轮 turn_start 立刻退出 status='cancelled'。\n"
            "    必填 child_run_id + reasoning（非空，说清为什么 cancel）。\n"
            "  - `list_active`：列当前所有 active + paused child runs（run_id /\n"
            "    node_type / parent_run_id / started_at）。\n"
            "  - `progress`：**用户问'跑得怎么样了/进展如何'时用这个**——偷看正在跑\n"
            "    child 最近的 transcript 事件（stage/detail 或 tool_call 摘要），不用\n"
            "    等它完成、不用 cancel/pause 才能看。必填 child_run_id，可选 n（默认 12 条）。\n"
            "    返回里还带 `workspace_recent_files`（输出文件在不在长）。\n"
            "  - `jobs`：**看后台计算作业** —— 子 run 之外的那一层。在跑什么、\n"
            "    占什么算力、已跑多久、预计还要多久、有没有跑超预计（overrun_ratio）。\n"
            "**长 tool_call 期间 child 的 transcript 是静默的**（事件要等工具返回才落）。\n"
            "编译 / 下载 / 训练 / HPC 作业跑几小时～几天都合理，这时别拿"
            "'没有新事件'当卡死。像科学家盯集群作业那样判断：先 `progress` 看\n"
            "workspace 输出文件在不在长，再用**你自己的 shell** tail 日志尾部（时间戳有没有推进、\n"
            "吞吐合不合理、有没有 error/nan/OOM）。**输出还在长就继续等。**\n\n"
            "**只 _orchestrator 节点白名单含本工具**（producing 节点不该操控兄弟节点）。"

            "\n⚠️ cancel 前先用 action='progress' 看 `pace`："
            "`waiting_on_model=true` 只是在等模型回复，不是卡死；"
            "拿 seconds_since_last_event 跟该 run 自己的 typical_gap_seconds 比。"
            "child 还在产生新事件就别 cancel —— 要调方向用 inject。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_RC_ACTIONS)},
                "include_done": {
                    "type": "boolean",
                    "description": "action=jobs 时：连已结束的作业一起列（默认只列在跑的）",
                },
                "child_run_id": {
                    "type": "string",
                    "description": "inject / cancel / progress 必填；list_active 不用",
                },
                "content": {
                    "type": "string", "minLength": 1,
                    "description": "inject 必填，要注入的 system message",
                },
                "reasoning": {
                    "type": "string", "minLength": 1,
                    "description": "cancel 必填：非空，说清为什么 cancel（高影响操作）",
                },
                "source": {
                    "type": "string", "default": "orchestrator_relay",
                    "description": "inject 时标记来源（user_interrupt / orchestrator_relay / ...）",
                },
                "n": {
                    "type": "integer", "default": 12,
                    "description": "progress 用，要看最近几条事件",
                },
            },
            "required": ["action"],
        },
        risk_level="medium",     # inject low / cancel high → 折中
    ),
    _runtime_control,
)

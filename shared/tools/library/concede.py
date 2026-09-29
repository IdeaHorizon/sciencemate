"""concede_obligation —— 对一条 blocking 义务的公开让步（判决拆除战役批 0）。

见 core/concessions.py 的模块说明。这里只做面向模型的参数校验与文案；
形状与落盘归 core/concessions，匹配判据归 core/obligations（同一真相源）。
"""
from __future__ import annotations

from typing import Any

from core import concessions, obligations
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


async def _concede_obligation(
    state: State,
    key_hash: str,
    reason: str,
    compensation: str = "",
    **_: Any,
) -> dict:
    key_hash = str(key_hash or "").strip()
    reason = str(reason or "").strip()
    # reason 非空是语义必需（没有理由的让步无物可审），由 parameters_schema
    # 的 minLength:1 声明、派发口核一次；不设长度闸——出口不得由字数/关键词
    # 把守（docs/verdict_demolition/README.md 档二约束）。
    current = {
        concessions.obligation_key_hash(o.kind, o.owed_by, o.what): o
        for o in obligations.collect(state)
        if o.blocking
    }
    target = current.get(key_hash)
    if target is None:
        listing = "\n".join(
            f"  {kh}  [{o.kind}] {o.what[:100]}" for kh, o in current.items()
        ) or "  （当前没有 blocking 义务——无需让步）"
        return {
            "status": "error",
            "error": (
                f"key_hash={key_hash!r} 不对应任何当前 blocking 义务。"
                f"合法取值：\n{listing}"
            ),
        }
    rec = concessions.record(
        state,
        key_hash=key_hash,
        kind=target.kind,
        owed_by=target.owed_by,
        what=target.what,
        reason=reason,
        compensation=compensation,
    )
    if rec is None:
        return {"status": "error",
                "error": "让步没能落盘（本 run 没有可写的项目 worktree/run 根）。"}
    state.append_transcript(
        "obligation_conceded", key_hash=key_hash, kind=target.kind,
        owed_by=target.owed_by, what=target.what[:200], reason=reason[:400],
    )
    return {
        "status": "success",
        "conceded": rec,
        "next_step": (
            "该义务已解除拦截但**没有消失**：它会继续渲染在账上（🤝）、进收尾"
            "清单，并由终审逐条裁决。让步改不了任何产物 status——欠着的仍然"
            "如实写着欠。继续推进剩余工作。"
        ),
    }


register_tool(
    ToolDefinition(
        name="concede_obligation",
        description=(
            "对一条当前 blocking 义务公开让步：声明「我知道欠着它，因为 reason "
            "决定带着这笔账继续」。让步进永久账本与终审视野，改不了任何 status。"
            "key_hash 来自义务清单里的应答方式一行。补齐永远是首选；让步用于"
            "补救不在你力所能及内（平台能力缺席、上游起不来、外部证据不可得）"
            "或你判断带账继续优于停摆的情形。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "key_hash": {
                    "type": "string",
                    "description": "义务清单里给出的 8 位让步码",
                },
                "reason": {
                    "type": "string", "minLength": 1,
                    "description": "为什么让步——写给终审看，自由文本，原样记录",
                },
                "compensation": {
                    "type": "string",
                    "description": "（可选）做了什么补偿，如降级产物/额外自查",
                },
            },
            "required": ["key_hash", "reason"],
        },
        replayable_read=False,
    ),
    _concede_obligation,
)

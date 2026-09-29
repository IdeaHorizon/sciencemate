"""reviewer 的收尾不能靠"记得" —— 轮次耗尽也必须留下已经做出的判断。

## 现场

_reviewer 打满 max_turns、一个 `review_critique` 都没产出，被独立报过三次：
#194（nidy 2026-07-28）、#301（nidy 2026-08-05）、#395-Issue6（jicq 2026-08-11）。
原话："reviewer 做了思考但没保存 review_critique，orchestrator 决策逻辑就看不见"、
"重复 reviewer 尝试会浪费轮次"。一轮约一小时，三轮零进展。

## 为什么光有收尾闸不够

v2.1 的收尾闸 `on_before_finish` 只守"模型主动收手"这一条路 —— 它整个在
`agent_loop` 的 `if not response.tool_calls:` 里面。**耗尽轮数是它的绕过路径，
而且不吭声**。这条教训 hypothesis 的 hooks 里已经写下来了（E2E v16：烧满 40 轮，
`on_before_finish` 0 次拦截记录）。`on_end` 在 for 循环之外，两条出口都会跑。

## 三层，全部机械，不需要 LLM

  1. `on_turn_end`       —— 还剩几轮时就把"现在就 finalize"摆到面前。
                            拦在**还来得及做**的时候，这是唯一真能省下重跑的一层。
  2. `on_before_finish`  —— 主动收手却没 finalize：否决一次，指名那一步。
  3. `on_end`            —— 兜底。攒了东西却没落盘 → 框架替它落。

## 第 3 层的红线：落证据，不落判决

绝不替模型编 verdict。模型自己 `set_verdict` 过就用它 —— 那是它真做出的判断，
只是没走完 finalize，属于**恢复已完成的工作**。没有 verdict 就写 `verdict: null`
＋ `review_incomplete` 块，由 `decision_package` 接到既有的"review 不可用"状态
（`retry_reviewer` 打头、动作集**不含 PROCEED**）。

一份被截断的审查冒充一次通过的审查，比白跑一轮贵得多 —— 那正是"连实验都不做
也能 approve"那一类事故。所以本 hook 的产物**永远不会**让下游拿到 PROCEED。
"""
from __future__ import annotations

import json

from core.llm import LLMMessage
from core.loop_hooks import LoopHook, register_loop_hook

from .tools.critique_builder import _DRAFT_KEY, _summarize


# 剩多少轮开始催 finalize。与 hypothesis 的欠账提醒同口径：
# max(3, 上限的 1/5) —— 上限小的时候至少留 3 轮，大的时候按比例。
def _nudge_threshold(max_turns: int) -> int:
    return max(3, max_turns // 5)


def _draft(state) -> dict:
    d = state.hook_state.get(_DRAFT_KEY)
    return d if isinstance(d, dict) else {}


def _draft_has_material(d: dict) -> bool:
    """草稿里有没有值得抢救的东西。"""
    return bool(
        d.get("verdict")
        or d.get("summary")
        or (d.get("concerns") or [])
        or (d.get("strengths") or [])
        or (d.get("recommended_action") or {}).get("action")
        or d.get("per_dimension_scores")
    )


def _critique_landed(state) -> bool:
    """本 run 是否已经产出 review_critique（own_only：只认自己产的）。"""
    try:
        return any(a.get("type") == "review_critique"
                   for a in state.list_artifacts(own_only=True))
    except Exception:
        return False


def _finalize_call_hint(d: dict) -> str:
    missing = _summarize(d)["missing_before_finalize"]
    if not missing:
        return ("现在就调 `compose_review_critique(action='finalize', name=..., "
                "artifact_under_review=..., source_node_type=...)` 落盘。")
    return ("先补齐 " + "、".join(missing)
            + "，再调 `compose_review_critique(action='finalize', name=...)` 落盘。")


# ── 第 1 层：还来得及的时候就说 ────────────────────────────────────────────
def _finalize_budget_nudge(ctx) -> list | None:
    """轮次快用完 + 还没落盘 → 把 finalize 摆到面前。

    没轮数的时候再拦也拦不住什么（hypothesis 那条教训的原话）。真正能省下
    "又一个一小时"的只有这一层：趁还有 3-8 轮，让它把已有判断先落地。
    """
    state = ctx.state
    if _critique_landed(state):
        return None
    max_turns = getattr(ctx.harness, "max_turns", 0) or 0
    if max_turns <= 0:
        # yaml 写 0（开发模式）时框架另有默认上限，这里读不到 → 不催，
        # 由第 2、3 层兜。不猜一个上限出来。
        return None
    remaining = max_turns - ctx.turn
    if remaining > _nudge_threshold(max_turns):
        return None
    if state.hook_state.get("_critique_budget_nudged"):
        return None       # 只催一次，不跟模型拉锯

    d = _draft(state)
    state.hook_state["_critique_budget_nudged"] = True
    state.append_transcript(
        "review_critique_budget_nudge", turn=ctx.turn, remaining=remaining,
        draft_has_material=_draft_has_material(d),
    )
    body = (
        f"⏳ **本轮只剩约 {remaining} 轮**，而 `review_critique` 还没落盘。\n\n"
        "审查做到什么程度就落什么程度 —— 一份说明"
        "「审到哪、还缺什么」的 critique，远好过打满轮次一个产物都没有"
        "（那样 orchestrator 看不到任何独立审查信号，只能把 reviewer 整个重派，"
        "一轮约一小时）。\n\n"
        + _finalize_call_hint(d)
        + "\n\n落盘之后若还有余轮，可以继续 `add_concern` 再 finalize 一次。"
    )
    return [LLMMessage(role="user", content=body)]


# ── 第 2 层：主动收手但没落盘 ──────────────────────────────────────────────
def _critique_before_finish(ctx) -> list | None:
    """模型准备结束却没产 critique —— 否决一次，指名那一步。

    只在草稿里确实有东西时否决：什么都没做就结束是另一个问题（该由决策层
    看 stop_reason 判），在这儿拦只会平白烧一轮。
    """
    state = ctx.state
    if _critique_landed(state):
        return None
    d = _draft(state)
    if not _draft_has_material(d):
        return None
    state.append_transcript("review_critique_finish_gate", turn=ctx.turn)
    return [LLMMessage(
        role="user",
        content=(
            "⛔ 还不能结束：你已经攒了审查内容，但 `review_critique` **没有落盘** "
            "—— 就这样结束等于这一轮全部白做，orchestrator 拿不到任何独立审查信号。\n\n"
            + _finalize_call_hint(d)
        ),
    )]


# ── 第 3 层：两条出口都会跑的兜底 ──────────────────────────────────────────
def _land_partial_critique(ctx, _result) -> None:
    """loop 结束时还没落盘 → 框架把草稿落成**证据**。

    落的不是判决：模型没 set_verdict 就写 null，绝不替它填一个。
    """
    state = ctx.state
    if _critique_landed(state):
        return
    d = _draft(state)
    if not _draft_has_material(d):
        return          # 真的什么都没做，没有可抢救的东西 —— 不造产物

    verdict = d.get("verdict")           # 有就是模型自己下的，没有就是 None
    rec = d.get("recommended_action") or {}
    missing = _summarize(d)["missing_before_finalize"]
    incomplete = {
        "reason": "reviewer run 结束时 critique 未 finalize（轮次耗尽或提前收手）",
        "missing_before_finalize": missing,
        "turns_used": ctx.turn,
        "landed_by": "framework",
        "verdict_is_reviewer_own": bool(verdict),
    }
    content_obj = {
        "verdict": verdict,
        "confidence": d.get("confidence"),
        "summary": d.get("summary", ""),
        "concerns": d.get("concerns") or [],
        "strengths": d.get("strengths") or [],
        "recommended_action": rec or None,
        "review_incomplete": incomplete,
        "_composed_by": "reviewer_critique_lands_hook",
    }
    if d.get("per_dimension_scores"):
        content_obj["per_dimension_scores"] = d["per_dimension_scores"]

    concerns = content_obj["concerns"]
    metadata = {
        "verdict": verdict,
        "confidence": d.get("confidence"),
        "n_concerns": len(concerns),
        "n_critical_concerns": sum(1 for c in concerns
                                   if c.get("severity") == "critical"),
        "recommended_action": rec.get("action"),
        "recommended_target_node": rec.get("target_node"),
        # 机械读取者（decision_package 的 metadata 回退）必须一眼看见这是残件，
        # 否则它会把一份截断的审查当成一次正常审查用。
        "review_incomplete": True,
    }
    try:
        from core.artifact_capabilities import save_typed_artifact

        saved = save_typed_artifact(
            state,
            artifact_type="review_critique",
            name=f"partial_critique_{state.run_id}",
            content=json.dumps(content_obj, ensure_ascii=False, indent=2),
            metadata=metadata,
        )
    except Exception:
        # 兜底层自己不能成为新的失败源 —— 落不下就算了，行为退回改动前。
        return
    state.append_transcript(
        "review_critique_landed_partial",
        artifact_id=saved["id"],
        turns_used=ctx.turn,
        has_reviewer_verdict=bool(verdict),
        n_concerns=len(concerns),
    )


register_loop_hook(
    LoopHook(
        name="reviewer_critique_lands",
        description=(
            "reviewer 的审查结论必须留下来：轮次将尽时催 finalize、主动收手时"
            "否决一次、loop 结束仍未落盘则由框架把草稿落成证据（绝不代填 verdict）。"
        ),
        on_turn_end=_finalize_budget_nudge,
        on_before_finish=_critique_before_finish,
        on_end=_land_partial_critique,
        emits=(
            "review_critique_budget_nudge",
            "review_critique_finish_gate",
            "review_critique_landed_partial",
        ),
    )
)

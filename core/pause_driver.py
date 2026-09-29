"""Pause driver —— 给 chat.py + run_node.py 共用的 pause/resume 交互回路。

设计：
  - pause_event 透传给 user（**不经过 orchestrator 翻译**）—— 因为 child 主动 pause
    问的就是它要的精确技术细节（如 "NHC chain length = 3 还是 5?"），多一层翻译
    只会失真。chat.py 跟 run_node.py 都用这个。
  - 多级嵌套：找最深一层（叶子）paused ctx 先回答，cascade 自动 resume 父级
    run_node 调用（让父 loop 看到子 final_text 后继续）。
  - I/O 后端可换：默认走 stdin (`_default_ask`)，未来 web 部署只换 ask_fn。

调用者负责：
  - 已经把 pause 触发的 child run **注册到 pause registry**（agent_loop 内自动做）
  - 想要"测试模式 fake-orchestrator"也可以拦截 ask_fn，让 owner 写死答复。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from .agent_loop import resume_loop, run_loop
from .llm import LLMMessage
from .pause import (
    PauseEvent,
    clear_pause,
    get_deepest_paused,
    get_paused_run,
)

log = logging.getLogger("pause_driver")


# Ask 函数签名：拿到 PauseEvent，返 user 的回答（async）。
#
# 回答有两种形态，**都要能原样穿过这条链**：
#   · str  —— 自由文本 / 裸序号（stdin、老前端）
#   · dict —— `{offer_id, choice_id, note}`，人点的那个按钮的**身份**
#
# 结构化那种是主路径：带上它，答复就是一次集合成员判定；压成文本就只能拿
# 文案去撞合法集，选项集一变就撞不上（2026-08-19 那次静默丢弃的引擎）。
AnswerValue = str | dict[str, Any]
AskFn = Callable[[PauseEvent], Awaitable[AnswerValue]]

# Finalize 函数签名：拿到 (state, harness, loop_result)，校验必需产物 + 写 summary
# 通常由 caller 传入 `core.executor.finalize_run` 的 partial。
FinalizeFn = Callable[..., Awaitable[dict]]


def auto_approve_answer(pause_event: PauseEvent) -> str:
    """Return the answer selected by auto-approve for a pause event.

    Decision packages carry a 0-indexed recommended option in metadata.  Other
    human-input pauses keep the historical policy: choose the first option, or
    provide a generic "use your judgement" answer when no options exist.

    Keeping this policy independent from stdin is important: ``chat.py`` uses
    an asyncio queue for interactive input, while ``run_node.py`` uses stdin.
    Both frontends must resolve the same pause in the same way.
    """
    # structured_question 一并读推荐项：request_human_input 现在强制要求它
    # （台账 #3）。此前只有 decision_package 走这条，裸提问落到"选第一个"——
    # 那是任意值，实测选中过 "A: 等平台修复合约门"，autonomous 自己选了停摆。
    # capability_grant 一并读推荐项（#1068 补充三第 1 条）。
    #
    # 这张卡**带着** `recommended_option_index=1`（拒绝），设计上就是"没答就是没批"。
    # 可它此前落到下面那段"没带推荐项"的文案里，回给模型的是：
    #   「这个提问没有带 recommended_option_index……带上它重新提问」
    # 而 `request_network_access` 的参数里根本没有这个字段，照做只会拿到同一张卡、
    # 同一段话。**回给模型的话与事实不符，还叫它重问** —— 一个每次都重问的模型
    # 因此在一次 `_auto_resume_pauses` 里烧掉几十次 LLM 调用。
    #
    # 这里不是改成自动批准（无人值守下批不下来是设计本意），是让那条设计假定的
    # 「按推荐项自动拒绝」真的发生。
    if (pause_event.metadata or {}).get("type") in {
        "decision_package", "structured_question", "capability_grant",
    }:
        recommended_index = pause_event.recommended_index()
        if not pause_event.options:
            recommended_index = 0
        return str(recommended_index + 1)

    if pause_event.options:
        # 没带推荐项的（历史）提问：**不猜**。
        #
        # 曾经的兜底是"选第一个" —— 那是任意值，实测选中过
        # "A: 等平台修复合约门"，autonomous 自己选择了停摆（台账 #3）。
        # 试过改成"跳过看起来像等待的选项"，但那是关键词名单，"先等等" 就漏了：
        # 名单式护栏必然漏新写法，而这里是**承重**判据，漏一次就是一次卡死。
        #
        # 结构判据：没有推荐项 = 提问方自己也没说清哪个能推进 → 把这个事实
        # 如实回给它，让它带推荐项重问。这既不猜、也不卡（run 照常继续），
        # 而且 request_human_input 现在强制要求推荐项，所以循环必然收敛。
        return (
            "这个提问没有带 recommended_option_index，无人值守模式无法替你在"
            "选项间做选择（选任意一个都可能是让研究停摆的那个）。\n"
            "请重新评估后二选一：① 想清楚哪个选项最优，带上 "
            "recommended_option_index 重新提问；② 如果确实没有能推进的选项，"
            "那不是提问而是阻塞 —— 改调 report_blocker 如实登记。"
        )
    return "请按你的最佳专业判断推进，不需要更多输入。"


def _normalize_decision_override(answer: AnswerValue, options: list[str]) -> AnswerValue:
    """Normalize the special ``skip`` override used by decision packages.

    结构化答复（`{offer_id, choice_id, note}`）原样透传 —— 它已经带着按钮身份，
    没有可"归一化"的东西，而 `.strip()` 会当场炸（dict 没有这个方法）。
    """
    if isinstance(answer, dict):
        return answer
    answer = (answer or "").strip()
    if answer.lower() != "skip":
        return answer
    for i, option in enumerate(options, 1):
        if "abort" in str(option).lower():
            return str(i)
    return "ABORT"


def _clean_answer(value: AnswerValue) -> AnswerValue:
    """字符串去空白；结构化答复原样返回。

    整条链上但凡有一处写 `(answer or "").strip()`，人点的那个按钮的身份就在
    那里被抹掉 —— 而抹掉之后没人报错，只是退化成拿文案去撞合法集。
    """
    if isinstance(value, dict):
        return value
    return (value or "").strip()


async def resolve_pause_answer(
    pause_event: PauseEvent,
    manual_ask: AskFn,
) -> AnswerValue:
    """Resolve a pause consistently for stdin, chat queues, and future UIs.

    With auto-approve disabled, this simply awaits ``manual_ask``.  With it
    enabled, decision packages retain a short override window and then select
    the reviewer's recommended action.  Generic pauses resolve immediately
    using :func:`auto_approve_answer`.

    返回值可能是 str，也可能是 `{offer_id, choice_id, note}` —— 后者是人点
    按钮时的**主路径**，必须原样交给 `resolve_answer` 做集合成员判定。
    2026-08-22 实测：这里曾经无条件 `.strip()`，平台前端传来的结构化答复
    直接 `AttributeError: 'dict' object has no attribute 'strip'`，异常被上层
    `except Exception` 兜住 → 表现成"答复没被受理"，决策卡片反复回来。
    """
    # 登记"我在等这个 pause 的答复" —— 于是"没人管"变成注册表里查得到的事实，
    # 不是靠假设。这里是**所有前端共用的入口**（chat 的异步队列 / run_node 的
    # stdin 都走它），登记接在这一处才不会漏掉某个前端。
    # 见 core/pause.py 的 _DRIVEN 说明（E2E-5a 82 分钟死锁）。
    from core import pause as _pause
    _rid = getattr(pause_event, "asking_run_id", "") or ""
    if _rid:
        _pause.claim_driver(_rid)
    try:
        if not AUTO_APPROVE_ENABLED or _is_high_risk(pause_event):
            return _clean_answer(await manual_ask(pause_event))

        automatic = auto_approve_answer(pause_event)
        if (pause_event.metadata or {}).get("type") not in {
            "decision_package",
        }:
            return automatic

        try:
            answer = await asyncio.wait_for(
                manual_ask(pause_event),
                timeout=AUTO_APPROVE_COUNTDOWN_SEC,
            )
        except (TimeoutError, EOFError, KeyboardInterrupt):
            return automatic

        answer = _normalize_decision_override(answer, pause_event.options or [])
        return answer or automatic

    finally:
        if _rid:
            _pause.release_driver(_rid)


#: 收拾一批孤儿 pause 的上界。超了就清注册表放行 —— 宁可丢掉这次 resume，
#: 也不能让"防死锁"的机制自己变成死锁。
ORPHAN_PAUSE_RESOLVE_MAX_S = 600.0


async def _immediate(value: str) -> str:
    return value


async def resolve_orphan_pauses(state, orphans: list) -> bool:
    """把**没人管**的 pause 用 auto-approve 答掉，让无人值守真的能无人值守。

    "有没有人管"是注册表里的事实（`core.pause` 的 _DRIVEN），不是假设：
    创建 pause 的那一轮可能已经结束，没有任何人在等答复 —— 这时
    auto-approve 永远不会被调用，continuous/autonomous 也不敢动，于是无限期
    停住。E2E-5a 实测死锁 82 分钟。

    ⚠️ 这份实现原本只存在于 `chat.py`（CLI 路径）。平台路径
    （platform_runtime）只调了 `claim_driver` 登记、从不查 `undriven_pauses`，
    于是**同一个死锁在 UI 上原样复现**：2026-08-09 全表普查 19 个非终态
    session 无活进程、最久 63.9 小时。提到 core 里做单一真相源，两条路径
    共用 —— 防死锁机制自己"存在但没接到路径"，是这个仓库最贵的一次。

    返回是否真的处理了孤儿。做不成**绝不能把调用方拖住** —— 那就退化回
    它要治的死锁。失败就如实留痕、清注册表、放行。
    """
    import asyncio

    from core import pause as _pause

    if not orphans:
        return False
    ids = [c.run_id for c in orphans]
    _t(state, "orphan_pause_detected", paused_runs=ids,
       note="无人在等答复，用 auto-approve 自动答复")
    try:
        prev = AUTO_APPROVE_ENABLED
        set_auto_approve(True, 0)          # 无人值守：不留倒计时
        try:
            from core.executor import finalize_run

            await asyncio.wait_for(
                drive_pause_chain(
                    ask_fn=lambda pe: _immediate(auto_approve_answer(pe)),
                    # 跟正常路径同一套收尾：resume 完的 run 必须跟一次跑完的
                    # 写出一样的 summary，否则收拾掉孤儿
                    # 之后留下的是半截 run。
                    finalize_fn=finalize_run,
                ),
                timeout=ORPHAN_PAUSE_RESOLVE_MAX_S,
            )
        finally:
            set_auto_approve(prev, 5)
        _t(state, "orphan_pause_resolved", paused_runs=ids)
        return True
    except Exception as e:
        # 兜底：无论如何把注册表清干净，否则下一轮又被同一批孤儿卡住
        for rid in ids:
            _pause.clear_pause(rid)
        _t(state, "orphan_pause_resolve_failed", paused_runs=ids,
           error=f"{type(e).__name__}: {e}")
        return True


def undriven_now() -> list:
    """当前"一个有人管的都没有"时返回全部 pause；否则返回空。

    判据放这里而不是各调用方各写一遍：只要还有**任何**一个 pause 有人在等，
    就说明控制权还在别人手上，抢方向盘会造出第二个控制平面
    （false-PROCEED 事故的第一环）。
    """
    from core.pause import list_paused, undriven_pauses

    paused = list_paused()
    if not paused:
        return []
    orphans = undriven_pauses()
    return orphans if len(orphans) == len(paused) else []


def _t(state, event: str, **fields) -> None:
    try:
        state.append_transcript(event, **fields)
    except Exception:
        pass

async def _default_ask(pause_event: PauseEvent) -> str:
    """默认 stdin 询问方式（chat.py + run_node.py 默认 backend）。

    特化：pause_event.metadata.type == 'decision_package' 时，调
    `_ask_decision_package` 走 ASCII 报告 + auto-approve countdown 路径。

    AUTO_APPROVE_ENABLED 时通用 request_human_input 路径也自动选：
      - 有 options → 选 options[0]
      - 无 options → 返回 "请按你的最佳专业判断推进，不需要更多输入。"
    """
    if (pause_event.metadata or {}).get("type") == "decision_package":
        return await _ask_decision_package(pause_event)

    # 通用 request_human_input 路径
    print()
    print("=" * 60)
    asking = pause_event.asking_node_type or "?"
    print(f"[需要人工输入]（来自节点：{asking}）")
    print(f"问题：{pause_event.question}")
    if pause_event.context:
        print(f"\n背景：\n{pause_event.context}")
    if pause_event.options:
        print("\n选项：")
        for i, opt in enumerate(pause_event.options, 1):
            print(f"  [{i}] {opt}")
        print("  （或自由输入任意文本）")
    print("=" * 60)

    if AUTO_APPROVE_ENABLED and not _is_high_risk(pause_event):
        picked = auto_approve_answer(pause_event)
        print(f"⚡ AUTO-APPROVE ON. 自动回复：{picked}")

    async def ask_stdin(_: PauseEvent) -> str:
        try:
            return await asyncio.to_thread(input, "Your answer: ")
        except (EOFError, KeyboardInterrupt):
            return ""

    return await resolve_pause_answer(pause_event, ask_stdin)


# ── decision_package 特化路径（auto-approve countdown）──────────────────────

# 进程级开关：chat.py / run_node.py 直接设；平台侧走 HARNESS_AUTO_APPROVE 环境
# 变量（子进程起来时读一次）——平台从来没有过设置它的路径，于是 UI 上的
# "Autonomous" 只影响自动 publish 版本，决策包照旧每个都停人。
AUTO_APPROVE_ENABLED: bool = os.getenv("HARNESS_AUTO_APPROVE", "").strip() in {"1", "true", "yes"}
AUTO_APPROVE_COUNTDOWN_SEC: int = 5

# **永不自动放行**的暂停类型。autonomous 的语义是"system-led, pauses only at
# high-risk points"（v21-collaboration-model.md），不是"什么都不问"。
# experiment 的批量写入 HITL、外部作业提交门都走这两个 type —— 把它们一起
# 自动放行等于把安全门拆了。
NEVER_AUTO_APPROVE_TYPES = frozenset({"highrisk_confirm", "permission"})


def _is_high_risk(pause_event) -> bool:
    """这个暂停是不是"永不自动放行"那一类 —— **连续档除外**。

    ## 为什么连续档能放行高危

    三档语义（UI 侧的翻译见 SessionWorkspace）：

        assisted    常问
        autonomous  只在高危点停          ← NEVER_AUTO_APPROVE_TYPES 在这里生效
        连续        不停，直到研究真正结束

    「连续」在配置里就是 `autonomous + 预授权全部高危类别（"*"）` —— 用户已经
    逐类授权过了，再停下来问是自相矛盾。wangd 2026-08-19：「连续模式下，就不
    应该出现任何需要人操作的行为，必须连续进行下去，除非彻底结束完整的研究了，
    才能停止。」

    判据不是新开关，是**用户已经声明的那份全类别授权**（`BYPASS_ENABLED` 由
    `session_driver.apply_continuous_mode` 按 state 每轮施加）。autonomous 档
    没有 `"*"`，走到这里仍然为真、仍然停人 —— 那一档一个字没动。
    """
    from shared.lib import dangerous_commands as _dc

    if getattr(_dc, "BYPASS_ENABLED", False):
        return False
    return (pause_event.metadata or {}).get("type") in NEVER_AUTO_APPROVE_TYPES


def set_auto_approve(enabled: bool, countdown_sec: int = 5) -> None:
    """设置"要不要自动放行"这个进程级开关。

    **唯一合法的调用方是 `session_driver.apply_autonomy`** —— 它按会话声明的
    档位推导这个值。别的地方直接调，就等于绕开档位另开一个真相源（那正是
    `/auto_approve` 这条命令被删掉的原因）。
    """
    global AUTO_APPROVE_ENABLED, AUTO_APPROVE_COUNTDOWN_SEC
    AUTO_APPROVE_ENABLED = bool(enabled)
    AUTO_APPROVE_COUNTDOWN_SEC = max(1, int(countdown_sec))


async def _ask_decision_package(pause_event: PauseEvent) -> str:
    """渲染 decision package（已经在 pause_event.context 里） + 处理选项 / auto-approve。

    返答案字符串，格式：`<option_number>` (1-4) 或自由文本（user override / 加备注）。
    """
    print()
    # decision package 文本已经在 context 里（present_decision_package 工具构造好）
    print(pause_event.context)

    meta = pause_event.metadata or {}
    options = pause_event.options or []
    recommended_index = pause_event.recommended_index()
    recommended_number = recommended_index + 1
    recommended_label = (
        options[recommended_index]
        if 0 <= recommended_index < len(options)
        else "(unknown)"
    )

    if AUTO_APPROVE_ENABLED:
        # 倒计时 + 自动选 recommended（user 按 enter 立刻执行，按数字 override）
        print()
        print(f"⚡ AUTO-APPROVE ON. Auto-selecting [{recommended_number}] {recommended_label} in "
              f"{AUTO_APPROVE_COUNTDOWN_SEC}s (input 1-{len(options)} to override, "
              "enter 'skip' to abort):")

    async def ask_stdin(_: PauseEvent) -> str:
        prompt = "  > " if AUTO_APPROVE_ENABLED else "Your choice: "
        try:
            return await asyncio.to_thread(input, prompt)
        except (EOFError, KeyboardInterrupt):
            return ""

    answer = await resolve_pause_answer(pause_event, ask_stdin)
    if AUTO_APPROVE_ENABLED and answer == str(recommended_number):
        print(f"  → auto-executing [{recommended_number}]")
    return answer



#: 同一次呈递允许重问几次。超过就保持 pause —— 再问下去也是同一个答案，
#: 而无限追问会把一个非交互前端（auto-approve / 平台无人值守）钉死在循环里。
_DECISION_REASK_MAX = 3


async def _execute_authorized_action(ctx, entry: dict | None) -> bool:
    """人选了 REVISE / REDIRECT → 运行时直接把指定节点起起来。

    只执行**人明确授权**的那一个目标：`authorized_target_node` 由
    `record_decision_answer` 从人的选择推导，不是模型说了算。执行完 flow entry
    由 `_finish_child` 的既有路径推进（替代产物跑完才算数）。

    **派发失败不把控制权交回去**：起不来说明运行时没能执行人的决定，那是框架的
    失败，不是"轮到调度器自己想办法"。此时返回 False，由调用方保持 pause 并把
    失败带进下一次呈递 —— 否则就又出现一个「调度器手握控制权且 flow 开着」的
    窗口，而那正是这一整轮要消灭的东西。

    返回 True = 已执行（或不适用）；False = 该执行但没执行成。
    """
    if not entry or entry.get("decision_state") != "action_authorized":
        return True
    target = str(entry.get("authorized_target_node") or "").strip()
    if not target:
        return True
    from shared.tools.run_node import _run_node_tool

    feedback = entry.get("recommended_feedback") or ""
    # 人的直接指示必须跟着这次重跑走。选项标签写的是 "re-run source_node with
    # **reviewer** feedback"，实现也确实只转发 reviewer 那一份 —— 于是协作档下
    # 「打回去并说明为什么」的后半句结构上到不了节点：人说的话停在调度器的
    # 会话里，节点照着 reviewer 的意见重做一遍，调度器再把这一轮总结成
    # "你的意见已被采纳"。产物里没有，报告里有。
    #
    # 两份分开标注、不混成一句：人的指示优先级高于 reviewer 建议，
    # 节点必须能分辨谁说的。
    human_note = str(entry.get("human_note") or "").strip()
    if human_note:
        feedback = (
            "【课题负责人的直接指示 —— 优先级高于下面的 reviewer 意见，必须逐条落到产物里】\n"
            f"{human_note}\n\n"
            "【reviewer 意见】\n"
            f"{feedback}"
        )

    def _note_dispatch_failure(reason: str) -> bool:
        # 名字刻意不叫 `_failed`：拒绝点扫盘（scripts/scan_refusal_sites.py）按
        # `_fail*` 前缀认「给模型的拒绝信封工厂」。这个函数不拒绝任何人，它是
        # **记一笔失败**并回答"起没起来"，登记成拒绝点会往棘轮里塞两条假账。
        ctx.state.append_transcript(
            "authorized_action_dispatch_failed",
            producing_run_id=entry.get("producing_run_id"),
            authorized_action=entry.get("authorized_action"),
            target_node=target,
            error=reason,
        )
        entry["action_last_failure"] = reason[:400]
        return False

    # 重跑带着**原任务**走，不是只带反馈（#1082 第 2 条，选的是 experiment owner
    # 倾向的 (b)）。此前这里只传 mode / reviewer_feedback / human_directive 三个
    # 框架键，而 experiment / observation / writing 声明的 expected_inputs 与这三
    # 个键**零交集** —— run_node 的输入契约检查据此拒绝，于是人选的 REVISE 对这
    # 三个节点从来没有真的执行过。补上原 node_inputs 之后交集自然成立，节点也拿
    # 得到 experiment_spec / prereg_artifact_id 这类任务锚点。
    #
    # 拿不到原输入时（升级前写下的旧 flow entry）不伪造一个：照旧只发三个框架
    # 键，该被拒就被拒 —— 但现在拒绝说得出口，见下面对返回值的判断。
    original_inputs = entry.get("producing_node_inputs")
    dispatch_inputs = {
        **(dict(original_inputs) if isinstance(original_inputs, dict) else {}),
        "mode": "revise",
        "reviewer_feedback": str(feedback)[:4000],
        "human_directive": human_note[:2000],
    }
    try:
        result = await _run_node_tool(
            ctx.state,
            target,
            node_inputs=dispatch_inputs,
            user_note=(f"人工决定 {entry.get('authorized_action')!r} → 由运行时执行："
                       f"重跑 {target}"),
        )
    except Exception as exc:                      # noqa: BLE001
        return _note_dispatch_failure(f"{type(exc).__name__}: {exc}")
    # `_run_node_tool` 被**拒绝**时不抛异常，它 return 一个
    # `{"status": "error"}`（输入契约不满足、目标节点不可派发、绑定冲突……都走
    # 这条）。只 except 异常等于只认崩溃那一种失败形态：拒绝原样落到下面的
    # `return True`，于是「没起来」被记成「起来了」——pause 照常 resume、
    # `authorized_action_dispatch_failed` 不写、`action_last_failure` 为空，
    # 人选的 REVISE / REDIRECT 从此无声无息（#1082：experiment / observation /
    # writing 三个目标都能复现）。
    #
    # 判据落在**这个函数问的那个问题**上：目标节点这一轮到底跑没跑。失败的
    # 返回值和抛出的异常是同一个答案的两种写法，两种都要认。
    if isinstance(result, dict) and str(result.get("status") or "") == "error":
        return _note_dispatch_failure(str(result.get("error") or "run_node 返回 status=error，无错误正文"))
    return True


def _cascade_tool_result(ctx, parent_ctx, result, child_summary: dict | None) -> dict:
    """级联恢复时回填给父 run 的那条 tool_result —— 与非暂停路径同一口径。

    有子 run 定稿后的 summary 就走 `_finish_child`（非暂停路径用的同一个函数），
    让 status/child_status/blockers、`subagent_call_end`、task 置 blocked、
    post-producing flow 登记全部按同一份规则发生。

    拿不到 summary（caller 没给 finalize_fn，或它自己抛了）才退回旧形状，并**说明
    这一份是降级的** —— 否则"按 loop 状态编出来的 success"和"子 run 真的成功了"
    在父 LLM 眼里一模一样，而那正是这条缺陷的形状。
    """
    fallback = {
        "status": "success" if result.status == "completed" else result.status,
        "child_run_id": ctx.run_id,
        "child_node_type": ctx.harness.node_type,
        "child_turns": result.turns,
        "final_text_preview": (result.final_text or "")[:500],
        "resumed_from_pause": True,
    }
    if child_summary is None:
        fallback["child_status"] = None
        fallback["child_summary_unavailable"] = (
            "拿不到子 run 定稿后的 summary，上面的 status 是按 loop 状态推的 —— "
            "它分不出 blocked / incomplete，别拿它当「任务做成了」")
        return fallback
    try:
        from shared.tools.run_node import _finish_child

        out = _finish_child(
            parent_ctx.state,
            ctx.harness.node_type,
            (ctx.state.hook_state or {}).get("node_inputs"),
            child_summary,
            ctx.harness,
            None,
        )
    except Exception as exc:                      # noqa: BLE001
        log.warning("cascade _finish_child failed for child %s: %s", ctx.run_id, exc)
        fallback["child_status"] = child_summary.get("status")
        fallback["child_summary_unavailable"] = f"{type(exc).__name__}: {exc}"[:300]
        return fallback
    out["resumed_from_pause"] = True
    return out


def _resolve_offered_choice(
    pause_event: "PauseEvent", answer: dict,
) -> tuple[str, str] | None:
    """把 App Server 的结构化答复 `{offer_id, choice_id, note}` 解成权威文本。

    返回 `(交给 LLM 的答复文本, 权威动作描述)`；**解不出返回 None**，调用方
    据此保持 pause。

    为什么需要它：`AnswerValue = str | dict` 是这条链上正式的契约，App Server
    带 choice 作答时给的就是 dict。但此前只有 `decision_package` 一支
    （`_settle_decision_answer`）会把 dict 结算成文本，其它 pause 类型
    —— 包括调度器实际最常发的 `structured_question` —— 把 dict 原样交给
    `resume_loop(ctx, response_text: str)`，而它第一件事就是
    `response_text[:200]`：`unhashable type: 'slice'`。
    也就是说**点任何一个结构化选项都会把整个 run 崩掉**。

    解不出就拒绝而不是猜：choice_id 不在本次呈递的选项集里、或回答的是上一次
    呈递（offer_id 对不上），都意味着这次授权没有发生。按 2026-08-19 的规矩，
    拿一个没被受理的答复去 resume，就是"点了没反应、卡片又回来了"的形状。
    """
    choice_id = str(answer.get("choice_id") or "").strip()
    if not choice_id:
        return None
    offered_id = pause_event.offer_id
    given_id = str(answer.get("offer_id") or "").strip()
    if offered_id and given_id and given_id != offered_id:
        return None
    chosen = next(
        (d for d in pause_event.option_details() if str(d.get("id") or "") == choice_id),
        None,
    )
    if chosen is None:
        return None
    label = str(chosen.get("label") or choice_id)
    description = str(chosen.get("description") or "").strip()
    note = str(answer.get("note") or "").strip()
    parts = [label]
    if description:
        parts.append(description)
    if note:
        parts.append(note)
    return "\n\n".join(parts), label


async def _settle_decision_answer(
    ctx, ask: "AskFn", answer: AnswerValue,
) -> tuple[str, str | None, bool]:
    """把 decision package 的答复记进账本；认不出就**带着合法出口重问**。

    返回 `(最终答复, 权威动作描述, 是否被受理)`。

    为什么重问而不是"记一笔就继续"：认不出选择意味着这次授权根本没发生。
    继续 resume 等于让 run 在"人没做决定"的前提下往下走 —— 而下游那道门禁
    仍然拦着，于是它只能回到同一个收尾点重新呈递。人看到的就是"点了没反应，
    卡片又回来了"。**答复没被受理 ≠ 人没回答，它是契约违规，必须当场说出来。**
    """
    from shared.tools.library.decision_package import (
        describe_recorded_decision,
        record_decision_answer,
    )

    payload = ctx.pause_event.to_dict()
    for attempt in range(_DECISION_REASK_MAX):
        try:
            entry = record_decision_answer(ctx.state, payload, answer)
        except Exception as e:          # 记账炸了不该把 run 拖下水
            log.warning("record_decision_answer failed: %s", e)
            return answer, None, True
        rejection = (entry or {}).get("decision_rejection")
        if not rejection:
            # ── 人的决定由**运行时**执行（Move 1d 续）─────────────────────
            #
            # 此前这里只记账就 resume，把"去起哪个节点"还给调度器 —— 于是它又
            # 拿回了排序权，于是又需要一排墙防它起错：「上一个 flow 没走完」、
            # 「同一个 flow 重复授权」、「redirect 踢皮球」全是这个窗口的产物。
            #
            # 这一刻是框架**唯一确知人选了什么**的时刻，也是唯一知道该起谁的
            # 时刻（`authorized_target_node` 就在手上）。把执行也放在这里，那个
            # 窗口就不存在了 —— 不是拦住调度器起错，是它根本没有起的机会。
            if not await _execute_authorized_action(ctx, entry):
                # 运行时没能执行人的决定 —— 保持 pause，把失败带回给人重新裁决。
                # 交回调度器等于把一个不一致的状态甩给它，然后再用一道墙拦它。
                answer = await ask(PauseEvent.from_payload({
                    **payload,
                    "question": "上一次授权的动作没能执行，请重新裁决。",
                    "context": (f"⚠️ 运行时尝试执行你选的 "
                                f"{(entry or {}).get('authorized_action')!r}"
                                f"（目标 {(entry or {}).get('authorized_target_node')!r}）"
                                f"但失败了：{(entry or {}).get('action_last_failure')}\n\n"
                                + str(payload.get("context") or "")),
                }, pending_tool_call_id=ctx.pause_event.pending_tool_call_id))
                if answer:
                    continue
                return answer, None, False
            if (entry or {}).get("decision_state") == "awaiting_manual_edit":
                # EDIT 的文案自己写着 **pauses for you to edit** —— 那它就不该
                # resume。此前它 resume 回调度器，只为了让 hook 告诉它"等着"，
                # 而那正是最后一个**调度器手握控制权却有 flow 开着**的窗口：
                # 「上一个 flow 没走完」那道墙唯一剩下的可达触发条件。
                #
                # 现在这一级保持 pause，人改完外部产物后对**同一次呈递**再作答
                # （选 PROCEED / REVISE）。调度器全程不参与，那道墙随之无处可撞。
                ctx.state.append_transcript(
                    "decision_manual_edit_holds_the_pause",
                    producing_run_id=(entry or {}).get("producing_run_id"))
                answer = await ask(PauseEvent.from_payload({
                    **payload,
                    "question": "编辑完成了吗？改完外部产物后，对同一次呈递重新作答。",
                    "context": ("你选择了 EDIT —— 本 run 停在这里等你。\n"
                                "改完 artifact / KB 之后，选 PROCEED（接受并继续）"
                                "或 REVISE（带反馈重跑）。\n\n"
                                + str(payload.get("context") or "")),
                }, pending_tool_call_id=ctx.pause_event.pending_tool_call_id))
                if answer:
                    continue
                return answer, None, False
            # resume 回填给 LLM 的必须是文本。结构化答复在这里收敛 —— 权威动作
            # 由 describe_recorded_decision 单独带过去，LLM 不需要再解读一遍。
            if isinstance(answer, dict):
                answer = " ".join(
                    x for x in (str(answer.get("choice_id") or ""),
                                str(answer.get("note") or "")) if x).strip()
            return answer, describe_recorded_decision(entry), True

        legal = ", ".join(rejection.get("legal_choice_ids") or []) or "（本次呈递没有可选项）"
        note = (f"⚠️ 上一个答复没被受理：{rejection.get('error')}\n"
                f"本次呈递的合法选项：{legal}")
        log.warning("decision answer rejected (%s): %s", rejection.get("code"), legal)
        if attempt == _DECISION_REASK_MAX - 1:
            break
        # 重问的是**同一次呈递**（offer_id 不变），只在前面加上为什么被拒。
        reask = PauseEvent.from_payload(
            {**payload, "context": f"{note}\n\n{payload.get('context') or ''}"},
            pending_tool_call_id=ctx.pause_event.pending_tool_call_id,
        )
        answer = await ask(reask)
        if not answer:
            break
    return answer, None, False


async def drive_pause_chain(
    *,
    ask_fn: AskFn | None = None,
    finalize_fn: FinalizeFn | None = None,
) -> str:
    """处理所有 paused run，cascade resume 直到回到顶层 completed / 再次 paused。

    流程：
      1. 找到 pause registry 里**最深**那级 paused ctx（叶子）
      2. ask_fn 显示 question 给 user，读答复
      3. resume_loop(ctx, answer) → 继续那级 run
      4. 如果那级又 pause → 回到 1
      5. 如果那级 completed / cancelled / failed → 它的 parent 的 run_node 工具结果需要更新
         为 child 完成状态，然后 resume parent（已经在 pause 状态）
      6. 最终：顶层完成 → 返回 final_text

    finalize_fn：若提供，**leaf child resume 完成后**会调它校验必需产物 + 写
        summary.json（确保 paused→resumed→completed 的 run 跟 1-shot run 写一致）。
        签名：`async finalize_fn(state, harness, loop_result, llm, depth=, sub_run_id=)`
        通常 caller 传 `core.executor.finalize_run`。

    返回最终（最顶层）assistant 文本。如果什么 paused 都没有立即返 ""。
    """
    ask = ask_fn or _default_ask
    final_text: str = ""

    while True:
        ctx = get_deepest_paused()
        if ctx is None:
            break

        # 1+2. 问 user
        answer = await ask(ctx.pause_event)
        if not answer:
            answer = "(user 取消 / 没回答)"

        # 2.5（#155）：decision package 的答复，在这里**机械记账**人工的真实选择。
        # 这是框架唯一确知"用户选了哪个"的时机 —— present_decision_package 只
        # unwind 出 pause，答复本身是回给 orchestrator LLM 的文本。没有这步，
        # "人工是否授权本次 reviewer retry / 是否 PROCEED"就只能靠 LLM 自述。
        _meta = (ctx.pause_event.metadata or {})
        # #183：记账拿到的**权威动作**要一路带到 resume 回填里 —— 只把人工原始
        # 文本（常是裸数字 "1"）交回 LLM，它会按最常见的选项集重新解读，与账本
        # 分叉（实测：review-failed 集里 [1]=RETRY REVIEWER 被复述成 PROCEED）。
        _authoritative: str | None = None
        # 结构化答复（dict）在 decision_package 之外没有任何一层结算它 ——
        # 直接进 resume_loop 就是 `dict[:200]`。见 _resolve_offered_choice。
        if isinstance(answer, dict) and _meta.get("type") != "decision_package":
            _resolved = _resolve_offered_choice(ctx.pause_event, answer)
            if _resolved is None:
                ctx.state.append_transcript(
                    "pause_answer_not_offered",
                    offer_id=ctx.pause_event.offer_id,
                    answered_offer_id=str(answer.get("offer_id") or ""),
                    choice_id=str(answer.get("choice_id") or ""),
                    offered=list(ctx.pause_event.choice_ids()),
                )
                return final_text
            answer, _authoritative = _resolved
        if _meta.get("type") == "decision_package":
            answer, _authoritative, _accepted = await _settle_decision_answer(ctx, ask, answer)
            if not _accepted:
                # 答复没被受理（不在本次呈递的选项集里 / 回答的是上一次呈递）。
                #
                # 这一级**保持 pause**，绝不拿一个没被受理的答复去 resume ——
                # 那正是 2026-08-19 死循环的形状：答复被静默丢弃，run 照样往下走，
                # 走到同一个收尾点再呈递一次，人再点一次再丢一次。
                # 决定没做出来，就该如实停在"等人"上。
                return final_text
        elif _meta.get("type") == "capability_grant":
            # 把人的答复**落成授权**（#1068 第二条补充）。
            #
            # 此前 `grant_from_answer` 在生产代码里零调用方：确认卡照常弹、人照常
            # 点，`is_granted` 仍然为假。模型再取被同一道墙拦住，再申请就再弹一张
            # 卡 —— 整条通道看起来在工作，实际上一次都没生效过。
            #
            # 和 highrisk_confirm 同一个位置、同一个理由：判定必须在**框架同时握有
            # 原始答复和那张选项表**的这一刻做完；到了 hook 那边只剩一条 JSON 信封。
            try:
                from core.capability_grants import grant_from_answer, read_answer

                _host = str(_meta.get("host") or "")
                _verdict = read_answer(answer)
                _granted = grant_from_answer(
                    ctx.state, _host, answer, reason=str(_meta.get("reason") or ""))
                ctx.state.append_transcript(
                    "capability_grant_answered",
                    host=_host,
                    granted=bool(_granted),
                    # 「读不出来」和「拒绝」在记账上是两件事：后者是人做了决定，
                    # 前者是这条链某处坏了，而把它记成拒绝会让那处永远查不到。
                    answer_unreadable=(_verdict is None),
                )
            except Exception as e:      # 记账失败绝不能打断 resume
                log.warning("grant_from_answer failed: %s", e)
        elif _meta.get("type") == "highrisk_confirm":
            # #422：判定必须在**框架同时握有原始答复和选项表**的这一刻做完。
            # 到了 hook 那边只剩一条 JSON 信封（{"status","response","asked_by"}），
            # 界面印的 `[1] 批准执行` 已经无从反解 —— 于是人输入的 `1` 被判成
            # 未批准，通行证不发，模型再也不会重调那个工具。
            try:
                from shared.lib.dangerous_commands import record_highrisk_answer
                record_highrisk_answer(
                    ctx.state, answer, ctx.pause_event.options or None)
            except Exception as e:      # 记账失败绝不能打断 resume
                log.warning("record_highrisk_answer failed: %s", e)
        # pause 类型 writing_gate_override 已随 writing-gate 整套退场（判决拆除第三波）。

        # 3. resume 这级
        result = await resume_loop(ctx, answer, recorded_decision=_authoritative)

        if result.status == "paused":
            # 同一级又 pause 了（agent 在 resume 之后又调了 request_human_input）
            # ctx 已被 clear，run_loop 内重新注册了新 ctx → 下次循环捡起来
            continue

        # 4a. leaf 完成 / cancel / 失败：写 summary（如果 caller 提供了 finalize_fn）
        #
        # **接住返回值**（#1083 第 2 条）。此前这里把它丢掉，于是级联回填给父 run
        # 的是 `LoopResult.status` —— 那个词表只有 completed/failed/cancelled/
        # void/paused 五种，`blocked` 和 `incomplete` 的子 run 到父 run 眼里
        # 一律变成 `success`。定稿后的终态只在这份 summary 里。
        child_summary: dict | None = None
        if finalize_fn is not None:
            try:
                child_summary = await finalize_fn(
                    ctx.state, ctx.harness, result, ctx.llm,
                    depth=ctx.state.depth, sub_run_id=ctx.state.sub_run_id,
                )
            except Exception as e:
                log.warning("finalize_fn failed for run %s: %s", ctx.run_id, e)
        if not isinstance(child_summary, dict):
            child_summary = None

        # 4b. cascade 给 parent（如果有）
        if ctx.parent_run_id and ctx.parent_tool_call_id:
            parent_ctx = get_paused_run(ctx.parent_run_id)
            if parent_ctx is not None:
                # 子 run 的收尾**与非暂停路径同一口径**（#1083 第 2 条）。
                #
                # 非暂停路径走 `run_node` → `_finish_child`：写
                # `subagent_call_end(child_status=…)`、子 run 非 completed 时把绑定的
                # task 置为 blocked、按 summary 返回 status/child_status/blockers/
                # missing_required_outputs，completed 时登记 post-producing flow。
                # 级联路径以前一样都不做 —— 于是「暂停过一次」这件事本身改变了
                # 收尾语义：父 LLM 看到 success、blockers 丢失、审查义务不登记。
                # 同一个问题两条路答不一样，就一定会分叉。
                new_payload = _cascade_tool_result(ctx, parent_ctx, result, child_summary)
                for i in range(len(parent_ctx.messages) - 1, -1, -1):
                    m = parent_ctx.messages[i]
                    if (m.role == "tool"
                            and m.tool_call_id == ctx.parent_tool_call_id):
                        parent_ctx.messages[i] = LLMMessage(
                            role="tool",
                            tool_call_id=ctx.parent_tool_call_id,
                            name=m.name,
                            content=json.dumps(new_payload, ensure_ascii=False),
                        )
                        break
                # 恢复这件事要**记在父 run 自己身上**（#1083 第 1 条）。
                # `loop_resume` 在 core/ 里只有 `resume_loop` 一个写点，级联这条路
                # 不经过它 —— 而平台把 `run.paused` 一律投成 waiting_human，能把它
                # 改回 running 的只有 run.started / run.resumed / run.retrying。
                # 于是连续档自动作答之后，父 run 永远停在「Needs your answer」，
                # 而 worker 里早已没有待答的 pause：**视图说的和事实相反**。
                parent_ctx.state.append_transcript(
                    "loop_resume",
                    pending_tool_call_id=ctx.parent_tool_call_id,
                    response_preview=f"子 run {ctx.run_id} 恢复后已收尾"[:200],
                    recorded_decision=None,
                    cascaded_from_run_id=ctx.run_id,
                )
                clear_pause(parent_ctx.run_id)
                parent_result = await run_loop(
                    parent_ctx.harness, parent_ctx.state,
                    parent_ctx.messages, parent_ctx.llm,
                )
                if parent_result.status == "paused":
                    continue  # 又 pause 了，下轮处理
                # parent 完成 / cancel：也用 finalize_fn 写 summary（如果 parent
                # 之前是 execute_node 起的；不是 execute_node 起的则 caller 自己处理）
                if finalize_fn is not None and parent_ctx.state.depth > 0:
                    try:
                        await finalize_fn(
                            parent_ctx.state, parent_ctx.harness, parent_result,
                            parent_ctx.llm,
                            depth=parent_ctx.state.depth,
                            sub_run_id=parent_ctx.state.sub_run_id,
                        )
                    except Exception as e:
                        log.warning("finalize_fn failed for parent %s: %s",
                                     parent_ctx.run_id, e)
                if parent_ctx.parent_run_id and parent_ctx.parent_tool_call_id:
                    # 3+ 层嵌套：当前简化处理 —— 返 parent final_text，不深递归
                    final_text = parent_result.final_text or ""
                    continue   # v3.1：可能还有并行兄弟 paused —— 继续 drain
                final_text = parent_result.final_text or ""
                continue       # v3.1：同上
        # 没有 parent → 这就是顶层 run。完成了记录 final_text，但**不立即退出**：
        # v3.1（审计 高危#13）：run_nodes_parallel 的多个子 run 可能各自 paused
        # —— 以前处理完一条链就 break，其余兄弟永远无人问。现在 drain 到
        # registry 清空为止（get_deepest_paused 返 None 才退出）。
        final_text = result.final_text or ""
        continue

    return final_text


async def make_scripted_ask(scripted_answers: list[str]) -> AskFn:
    """测试用：返回一个 ask_fn 顺序消费预定义答案。

    用法（pytest 里）：
      ask = await make_scripted_ask(["yes", "ok", "ship it"])
      text = await drive_pause_chain(ask_fn=ask)
    """
    answers = list(scripted_answers)

    async def _ask(pause_event: PauseEvent) -> str:
        if not answers:
            log.warning(
                "Scripted answers exhausted; pause from %s asking %r returning ''",
                pause_event.asking_node_type, pause_event.question[:80],
            )
            return ""
        return answers.pop(0)
    return _ask

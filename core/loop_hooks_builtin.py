"""框架内置 hook：memory_delta（默认启用）+ scratchpad（opt-in 范例）。

- memory_delta：每轮 LLM 调用前，把"自上轮以来新写入的 memory"作为 system message
                inject 进消息流。让 LLM 实时看到自己 / 其它机制写入的新 memory。
                **agent_loop 永远调用它（不靠 yaml 启用）**。

- scratchpad：每轮 inject state.scratchpad 全部笔记。pair with `write_scratchpad`
              工具（在 tools/builtin.py）。**opt-in**：要在 harness yaml 写
              `loop_hooks: [scratchpad]` 才生效，否则不动。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import closure as _closure
from .llm import LLMMessage
from .loop_hooks import HookContext, LoopHook, register_loop_hook

#: 本模块的四处 `except` 都用它记账。它此前**根本不存在** —— 于是那几个
#: 处理器自己会抛 NameError，把"注入失败但不打断这一轮"变成"这一轮当场炸"。
#: 一个从来没跑过的错误处理器，和没有错误处理器是一回事。
log = logging.getLogger(__name__)


# ── memory_delta ─────────────────────────────────────────────────────────────

_MEMORY_DELTA_KEY = "memory_delta_last_check"


def _v2_memory_records(state) -> list[dict]:
    """memory v2 里"这一轮之后可能新增"的东西 —— 目前就是候选队列。

    topic 文件（directives / pitfalls / …）是 curator 整理后的成品，按行 diff
    没有稳定的 created_at 可比；候选是带时间戳的追加流，正好是这个 hook 要的
    "自上次以来新写了什么"。
    """
    if not getattr(state, "project_worktree", None):
        return []
    try:
        from core import memory as M

        return [
            {"text": e.text, "kind": ("method" if e.section == M.SECTION_METHOD
                                       else "pitfall"),
             "seen": e.seen, "defect": e.defect}
            for e in M.manual_entries(state)
        ]
    except Exception:      # 记忆读不到不该拖垮一整轮
        return []


def _memory_delta_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """检查自上次调用以来 memory.jsonl 是否有新增 / 状态变更。新增就 inject。

    v2.1 schema：4 kinds × lifecycle (active/promoted/archived/superseded)。
    - 新写入（created_at > last_check）：按 kind 分组展示
    - lifecycle 变更（status_changed_at > last_check）：单独提醒（promote/archive/supersede）
    """
    last_check = ctx.state.hook_state.get(_MEMORY_DELTA_KEY)
    now_iso = datetime.now(timezone.utc).isoformat()

    if last_check is None:
        # Turn 1：建立基线时间戳。这之前的 memory 已经在 user message 段 3 里了，
        # 不重复 inject。
        ctx.state.hook_state[_MEMORY_DELTA_KEY] = now_iso
        return None

    # v2 存储：候选队列（agent 写的观察）。此前这里读 `state.list_memory()`
    # —— v1 的 memory.jsonl，自 2026-05 起没有任何写入方。这个 hook 是
    # always-on（每个节点每一轮都跑），于是三个月来每轮都在读一个空文件、
    # 每轮都静默返回 None：坏了跟"确实没有新记忆"看起来一模一样。
    all_mem = ctx.state.list_memory() + _v2_memory_records(ctx.state)
    new_mems = [
        m for m in all_mem
        if m.get("created_at", "") > last_check
    ]
    lifecycle_changed = [
        m for m in all_mem
        if (m.get("status_changed_at") or "") > last_check
        and m.get("created_at", "") <= last_check  # 本轮新建的不算 lifecycle 变更
    ]

    ctx.state.hook_state[_MEMORY_DELTA_KEY] = now_iso

    if not new_mems and not lifecycle_changed:
        return None

    lines: list[str] = []
    if new_mems:
        lines.append(
            f"📌 Memory 新增（turn {ctx.turn}）：自上轮以来新写入 {len(new_mems)} 条："
        )
        for m in new_mems:
            lines.append(_format_memory_brief(m))

    if lifecycle_changed:
        lines.append("")
        lines.append(
            f"🔄 Memory 状态变更（turn {ctx.turn}）：{len(lifecycle_changed)} 条 lifecycle 有更新："
        )
        for m in lifecycle_changed:
            reason = m.get("status_change_reasoning") or ""
            extra = ""
            if m.get("status") == "promoted" and m.get("promoted_to"):
                extra = f" → promoted_to={m['promoted_to']}"
            elif m.get("status") == "superseded" and m.get("superseded_by"):
                extra = f" → superseded_by={m['superseded_by']}"
            lines.append(
                f"- `{m['id']}` [{m.get('status', '?')}{extra}] "
                f"{(m.get('text') or '')[:100]}"
                + (f"  ({reason[:60]})" if reason else "")
            )

    lines.append("")
    lines.append(
        "（active directive 会自动 inject 进 system_prompt。其它经验沉淀在 "
        "项目记忆（目标/铁律/手册），用 `memory_recall`；"
        "agent 写候选用 `memory_note`，用户立的规矩用 `memory_write(section='law')`。）"
    )

    return [LLMMessage(role="system", content="\n".join(lines))]


def _format_memory_brief(m: dict) -> str:
    """单条 memory 的一行渲染（v2.1 schema）。"""
    tags = ",".join(m.get("tags") or [])
    kind = m.get("kind", "?")
    applies = m.get("applies_to_node")
    scope = f"→{applies}" if applies else ""
    return (
        f"- `{m.get('id', '?')}` [{kind}{scope}, tags=[{tags}]] "
        f"{(m.get('text') or '')[:140]}"
    )


def _memory_delta_on_turn_end(ctx: HookContext) -> list[LLMMessage] | None:
    """工具调度后再检查一次 —— 这是为了让 turn N 写入的 memory 在 turn N+1 之前被注入。

    on_turn_start 在 turn N+1 开始时跑，所以会看到 turn N 写的 memory。这里其实
    不需要再做一次 —— 但保留这个钩子作为对称性参考（未来如果要在工具结果后立刻
    inject 而不等下一 turn）。
    """
    return None


register_loop_hook(LoopHook(
    name="memory_delta",
    description=(
        "每轮 LLM 调用前 inject 自上轮以来新写入的 memory（作为 system message）。"
        "默认启用，让 LLM 实时看到自己写的 memory，不靠主动查 topic 文件。"
    ),
    on_turn_start=_memory_delta_on_turn_start,
    on_turn_end=_memory_delta_on_turn_end,
))


# ── scratchpad ───────────────────────────────────────────────────────────────

_SCRATCHPAD_FIRST_PRINCIPLES = """📝 你的白板 —— run 内跨轮工作状态（开场引导，只讲一次）

这次任务可能跑几十上百轮。messages 历史会被压缩成摘要，但**白板不会** ——
它每轮原样注入回来。它是你唯一能跨压缩保留、且完全由你掌握的东西。

**它是一块板子，不是一本日志。** `write_scratchpad(content=...)` 会用你传的
全文**整块替换**白板。没有"第 N 条笔记"，只有"板子现在长什么样"。

**第一性原理**：写"如果下一轮的我没看到现在的 messages、只看到压缩摘要 +
这块板子，他/她需要知道什么才能继续推进？"

**板上该有**：
  - 我在哪：当前 working hypothesis / 手头这一步做到哪了
  - 下一步：具体要做什么，用哪个工具
  - 哪些路堵死了：试过什么、为什么不 work、**别再重试**
  - 关键决策的暗线："为啥选 A 不选 B"

**板上不该有**（各有归宿，写这儿等于浪费板面）：
  - 可复用的经验 → memory_note（直接入册，跨 run 活）
  - 结论 / 发现 / 数据 → save_artifact、research_state
  - 用户立的规矩 → memory_write(section='law')
  - 已经做完的事的流水账 —— 做完了就从板上擦掉

**容量有硬上限**，超了会被拒绝写入且原板不动。写满时删什么由你定：
先删已完成的，再删已排除的，最后才动"我在哪 / 下一步"。

（白板只对你可见，不进 memory，不影响下游节点。）
"""


_SCRATCHPAD_INTRO_KEY = "_scratchpad_intro_shown"


def _scratchpad_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """首次（本 state 生命周期内）注入 first-principles 引导（教 LLM 该用
    scratchpad 做啥）；有笔记时注入已存笔记；空 scratchpad 每 ~10 turn 提醒一次。

    2026-07-09 fix（P0-4）：引导原来用 `ctx.turn == 1` 触发。producing 节点每次
    run 是 fresh state（turn 从 1 起，引导正好一次），没问题。但 _orchestrator 是
    **长 session、单一 state**，chat.py 每条用户消息都新起一次 run_loop → ctx.turn
    每条消息都从 1 重置，而 state.scratchpad 一直空（对话场景很少写 scratchpad），
    于是这 400 字引导**每条消息都重注入一次**（实测：叠加端点复读，引导全文被
    逐字抄进回复三次）。改用 state 级持久 flag（hook_state 会随 conversation
    持久化）→ 每个 state 生命周期只注入一次，对两类节点都正确。

    pair with write_scratchpad 工具。渲染与容量在 core/whiteboard.py，这里不重复
    实现 —— 注入的样子和写入的判据必须同一个真相源，否则两边会各自演化。"""
    from core import whiteboard

    board = ctx.state.scratchpad

    # 首次且空 → 注入完整引导（每 state 只一次，不按 turn）
    if not board and not ctx.state.hook_state.get(_SCRATCHPAD_INTRO_KEY):
        ctx.state.hook_state[_SCRATCHPAD_INTRO_KEY] = True
        return [LLMMessage(role="system", content=_SCRATCHPAD_FIRST_PRINCIPLES)]

    rendered = whiteboard.render(ctx.state, turn=ctx.turn)
    if rendered:
        return [LLMMessage(role="system", content=rendered)]

    # 仍空 → 每 10 轮提醒一次（轻量）
    if ctx.turn > 1 and ctx.turn % 10 == 0:
        return [LLMMessage(role="system", content=(
            "📝 提醒：白板还是空的。每轮都从零推理可能在浪费 token —— "
            "考虑用 write_scratchpad 写下「我在哪 / 下一步」。"
        ))]

    return None


register_loop_hook(LoopHook(
    name="scratchpad",
    description=(
        "每轮 inject 白板（run 内跨轮工作状态，来自 write_scratchpad 工具）。"
        "容量有硬上限，注入体积因此有界；附一行'上次改写在第几轮'的事实。"
        "首次注入覆盖语义的引导；空白板每 10 轮提醒。"
        "opt-in：要在 harness.loop_hooks 里启用，并把 write_scratchpad 加进工具白名单。"
        "用途：让 LLM 跨压缩保持'我在哪 / 下一步 / 哪些路堵死'。"
    ),
    on_turn_start=_scratchpad_on_turn_start,
))


# ── callee_contracts ─────────────────────────────────────────────────────────

_CALLEE_CONTRACTS_KEY = "_callee_contracts_shown"


def _callee_contracts_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """把可调子节点的**输入契约**注入调用方 —— 别让它靠背参数名。

    实测事故（2026-08-07 UI 真机）：orchestrator 直调 postprocess 服务，先传
    `figure_spec`、再传 `research_question`，都不对；该节点声明的是
    `visual_requests`。它没处可查 —— 契约声明在被调节点的 harness 里，调用方
    的 context 里没有。writing 能调对，是因为 owner 在 writing 的 prompt 里
    硬写了一句 `visual_requests`；换个调用方就没有这份知识。

    v2.1 把服务化推开之后，调用方从"少数几个 owner 写死"变成"任何拿到
    callable_nodes 的节点"，靠 prompt 传递契约不再成立。这里改成机械注入：
    契约本来就是声明数据（harness.expected_inputs），谁能调就给谁看。

    每个 state 只注入一次（契约不随轮次变），避免每轮刷屏。
    """
    if ctx.state.hook_state.get(_CALLEE_CONTRACTS_KEY):
        return None
    from core.loader import list_harnesses, load_harness

    declared = [n for n in (ctx.state.hook_state.get("_callable_nodes") or [])
                if isinstance(n, str)]
    if "*" in declared:
        # orchestrator 用通配符 —— 展开成真实节点清单（排除自己和架构私有节点）
        callees = [n for n in list_harnesses()
                   if n != ctx.state.node_type and not n.startswith("_")]
    else:
        callees = [n for n in declared if n != "*"]
    if not callees:
        return None
    ctx.state.hook_state[_CALLEE_CONTRACTS_KEY] = True
    lines = ["🔌 **你可以调起的子节点及其输入契约**（机械读自各节点 harness 声明）："]
    found = False
    for node_type in callees:
        try:
            child = load_harness(node_type)
        except Exception:
            continue
        expected = dict(child.expected_inputs or {})
        if not expected:
            continue
        found = True
        kind = "service" if child.is_service else "producing"
        lines.append("")
        lines.append(f"### `{node_type}`（{kind}）")
        for key, desc in expected.items():
            lines.append(f"  - `{key}`：{str(desc).strip()}")
    if not found:
        return None
    lines.append("")
    lines.append(
        "调用时 `node_inputs` 必须用上面声明的键。用别的键会被派发处直接拒绝 —— "
        "拒绝信息里会再列一遍正确的键，别原样重试。"
    )
    return [LLMMessage(role="system", content="\n".join(lines))]


register_loop_hook(LoopHook(
    name="callee_contracts",
    description=(
        "每个 state 注入一次：可调子节点（callable_nodes）的 expected_inputs 契约。"
        "用途：调用方不必靠 prompt 背参数名 —— 契约是声明数据，机械注入给能调的人。"
    ),
    on_turn_start=_callee_contracts_on_turn_start,
))


# ── kb_delta ─────────────────────────────────────────────────────────────────

_KB_DELTA_KEY = "kb_delta_last_check"
# v3：只有 concepts + claims 进 delta inject（experiments / chunks 量大噪音多）
_KB_ENTITIES = ("concepts", "claims")


def _kb_delta_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """每轮检查 KB 是否有新写入（concept/claim/synthesis），
    新增就 inject system message。跟 memory_delta 对称。"""
    last_check = ctx.state.hook_state.get(_KB_DELTA_KEY)
    now_iso = datetime.now(timezone.utc).isoformat()

    if last_check is None:
        ctx.state.hook_state[_KB_DELTA_KEY] = now_iso
        return None

    # v3.1（审计 规模项）：kb_delta 曾每 turn 全量读+解析所有 KB jsonl —— KB
    # 到几千条后成为每轮固定开销。绝大多数 turn 没有 KB 写入：先用
    # (mtime_ns, size) 指纹判断文件组是否变过，没变直接跳过全量扫描。
    fp_key = _KB_DELTA_KEY + "_file_fps"
    old_fp: dict = ctx.state.hook_state.get(fp_key) or {}
    new_fp: dict = {}
    for entity in _KB_ENTITIES:
        for scope in ("org", "project"):
            try:
                p = ctx.state._kb_path(entity, scope)
            except Exception:
                continue
            if p is None:
                continue
            try:
                st_ = p.stat()
                new_fp[str(p)] = (st_.st_mtime_ns, st_.st_size)
            except OSError:
                new_fp[str(p)] = None
    ctx.state.hook_state[fp_key] = new_fp
    if old_fp and new_fp == old_fp:
        ctx.state.hook_state[_KB_DELTA_KEY] = now_iso
        return None

    # 区分两类变化：新建（created_at > last_check）vs. 状态变更（updated_at > last_check 但 created_at < last_check）
    all_new: list[tuple[str, list[dict]]] = []
    all_updated: list[tuple[str, list[dict]]] = []
    for entity in _KB_ENTITIES:
        records = ctx.state.list_kb(entity)
        new: list[dict] = []
        updated: list[dict] = []
        for r in records:
            created = r.get("created_at", "")
            updated_at = r.get("updated_at", "") or created
            if created > last_check:
                new.append(r)
            elif updated_at > last_check:
                updated.append(r)
        if new:
            all_new.append((entity, new))
        if updated:
            all_updated.append((entity, updated))

    ctx.state.hook_state[_KB_DELTA_KEY] = now_iso

    if not all_new and not all_updated:
        return None

    lines: list[str] = []

    if all_new:
        total = sum(len(n) for _, n in all_new)
        lines.append(f"📚 KB 新增（turn {ctx.turn}）：自上轮以来新写入 {total} 条 KB 条目：")
        for entity, new_entries in all_new:
            for r in new_entries:
                lines.append(_format_kb_brief(entity, r))

    if all_updated:
        total = sum(len(u) for _, u in all_updated)
        lines.append(f"📝 KB 变更（turn {ctx.turn}）：{total} 条 KB 条目状态有更新（含合并 / 状态机变更）：")
        for entity, updated_entries in all_updated:
            for r in updated_entries:
                extra = ""
                if entity == "claims":
                    extra = (
                        f" → status={r.get('status', '?')}"
                        f" (review_history={len(r.get('review_history') or [])})"
                    )
                lines.append(_format_kb_brief(entity, r) + extra)

    if lines:
        lines.append("")
        lines.append(
            "（用这些 id 做 cross-link：create_claim(..., concept_ids=[...]) /"
            " update_claim_status(claim_id=..., new_status=...) / "
            "create_claim(claim_type='synthesis', sources=[<claim_ids>], ...)）"
        )

    return [LLMMessage(role="system", content="\n".join(lines))]


def _format_kb_brief(entity: str, r: dict) -> str:
    rid = r.get("id", "?")
    if entity == "concepts":
        return (f"- [concept {rid}] {r.get('canonical_name', '?')} "
                f"({r.get('concept_type', '?')}): {(r.get('description') or '')[:100]}")
    if entity == "claims":
        srcs = r.get("sources") or []
        ct = r.get("claim_type") or "?"
        return (f"- [claim {rid}] [{ct}/{r.get('status', 'provisional')}] "
                f"{(r.get('claim_text') or '')[:120]} "
                f"(sources={len(srcs)}, conf={r.get('confidence', 0.5)})")
    return f"- [{entity} {rid}]"


register_loop_hook(LoopHook(
    name="kb_delta",
    description=(
        "每轮 LLM 调用前 inject 自上轮以来新写入的 KB 条目（concept/claim/"
        "synthesis），作为 system message。默认启用，跟 memory_delta "
        "对称。让 LLM 实时看到自己写的 KB 内容，便于后续 cross-link。"
    ),
    on_turn_start=_kb_delta_on_turn_start,
))


# ── reflection（opt-in） ─────────────────────────────────────────────────────

_REFLECTION_PROMPT_DEFAULT = (
    "你已经跑了 {turn} 轮。在继续之前，请简短自检：\n"
    "  1. 我刚刚这 {window} turn 实际推进了什么？哪些 artifact 真的写出来了？\n"
    "  2. 有没有走过死胡同？把它用 `memory_note(category='pitfall')` 记一条。\n"
    "  3. 我当前的假设 / 工作方向还成立吗？不成立的话考虑换一个；关键方向决策"
    "可以写进 artifact 的 reasoning 段或 `memory_note(category='pitfall')`。\n"
    "  4. 离 required_outputs 还差什么？最有效的下一步是什么？\n"
    "回答这 4 个问题（一两句话即可），然后继续。"
)


def _reflection_on_turn_end(ctx: HookContext) -> list[LLMMessage] | None:
    """每 N 轮在 turn 末注入一段自检 system message。

    配置（harness yaml）：
      hook_config:
        reflection:
          every_n_turns: 4         # 默认 4
          prompt: |                  # 可选自定义 prompt（含 {turn} / {window} 占位）
            ...你自己的反思指令...
    """
    cfg = (ctx.harness.hook_config or {}).get("reflection", {})
    n = int(cfg.get("every_n_turns", 4))
    if n <= 0 or ctx.turn == 0 or ctx.turn % n != 0:
        return None
    # 不在最后一轮触发（max_turns 那轮反思也来不及）
    if ctx.turn >= ctx.harness.max_turns:
        return None

    template = cfg.get("prompt") or _REFLECTION_PROMPT_DEFAULT
    try:
        content = template.format(turn=ctx.turn, window=n)
    except (KeyError, IndexError):
        # 自定义 prompt 没有 {turn}/{window} 占位也 ok
        content = template

    return [LLMMessage(role="system", content=f"🔁 反思检查\n{content}")]


# ── producing_integration_reminder（opt-in，主要给 _orchestrator 用） ────────
#
# curator 已不在 post-producing flow 里（wangd 2026-08-19）——它是按需调取的
# 后台节点。原先这里有一个 legacy hook 每轮强提醒"pending_curator_integrations
# 非空，先去整合"；那份镜像连同这条提醒一起删除。
# ── post_node_review_flow_reminder (v0.4 完整 3-step flow) ───────────────────
#
# 取代 producing_integration_reminder。当 hook_state["pending_post_node_flow"]
# 非空时，按每条 entry 的 review_state / decision_state 两个
# 字段判断下一步该做啥，并强提醒 orchestrator。

def _post_node_review_flow_reminder_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    flow = ctx.state.hook_state.get("pending_post_node_flow") or []
    if not flow:
        return None

    # 取最早一条未完成的 entry 提醒（按顺序处理）
    target = None
    for entry in flow:
        # 只要任何一个 state 仍是 pending 就拿来处理
        if (entry.get("review_state") == "pending"
            or entry.get("decision_state") in (
                "pending",
                "awaiting_human",
                "action_authorized",
                "action_in_progress",
                "awaiting_manual_edit",
            )):
            target = entry
            break
    if target is None:
        return None

    # ── 空转计数：记在**跳不过去的这一侧**（2026-09-17）────────────────────
    #
    # 原来的熔断计数在 run_node 里，只有 flow entry 真的绑上了才 +1；而绑定挂在
    # `node_owes_post_node_flow(target)` 上。目标是服务节点时绑定被跳过，计数器
    # 一次都不加 —— 熔断器恰好在最需要它的那一档缺席，于是这里每轮照喊"去起
    # target"，模型每轮照做，账本一动不动，转了 40 轮（yuankk 2026-09-17）。
    #
    # 注入是跳不过去的：不管目标是什么类型、绑没绑上，这条 entry 被摆到调度器面前
    # 这件事都发生了。所以计数搬到这里，对**任何**将来的关不掉形态都有效。
    stall_rounds = _closure.note_flow_was_put_to_the_orchestrator(target)
    if _closure.flow_is_stalled(target):
        ctx.state.append_transcript(
            "post_node_flow_stalled",
            producing_node=target.get("producing_node"),
            producing_run_id=target.get("producing_run_id"),
            decision_state=target.get("decision_state"),
            authorized_target_node=target.get("authorized_target_node"),
            stall_rounds=stall_rounds,
        )
        return [LLMMessage(role="system", content="\n".join([
            f"🛑 **这条 post-producing flow 已经 {stall_rounds} 轮没动过**"
            f"（{target.get('producing_node')} run {target.get('producing_run_id')}，"
            f"decision_state={target.get('decision_state')!r}"
            + (f"，授权目标 {target.get('authorized_target_node')!r}"
               if target.get("authorized_target_node") else "") + "）。",
            "",
            "反复做同一件事而账本一动不动，是空转不是进展 —— **别再起那个节点了**。",
            "改为下面二选一：",
            f"  • present_decision_package(producing_run_id="
            f"{target.get('producing_run_id')!r}, ...) 重新裁决这一轮；",
            "  • report_blocker(...) 把这个闭合失败交给人。",
            "",
            "如果你已经判断这一轮推不动了，现在可以如实申报 "
            "`CONTINUOUS_STATUS: blocked` —— 这条卡住的 flow 不会再驳回它。",
        ]))]

    source_node = target.get("producing_node", "?")
    producing_run_id = target.get("producing_run_id", "?")
    artifact_ids = target.get("artifact_ids") or []
    artifact_preview = (", ".join(artifact_ids[:5])
                          + (f" ...(+{len(artifact_ids) - 5})" if len(artifact_ids) > 5 else ""))

    r_state = target.get("review_state", "pending")
    d_state = target.get("decision_state", "pending")
    review_critique_id = target.get("review_critique_artifact_id")
    review_failed = target.get("review_failed_reason")

    lines = [
        f"🚨 **POST-PRODUCING FLOW PENDING** ({source_node} 刚完，run_id={producing_run_id})",
        f"产出 artifact: [{artifact_preview}]",
        "",
        "Post-producing flow（每个 producing 节点完后必走，严格按顺序）：",
    ]

    # Step 1/2（reviewer、决策包呈递）不再提醒 —— 运行时自己走完（Move 1d）。
    #
    # 这两段原本把下一步调用**连参数都打印**给调度器照抄。那正是"框架已经知道
    # 答案却让模型抄一遍，再用一排墙防它抄错"：能被打印出来的东西不是判断，是
    # 手续。手续归运行时之后，这里没有可提醒的东西 —— 调度器发起一次
    # `run_node(<producing>)`，下一次醒来看到的就是人的答复。
    #
    # 留下的只有**真需要调度器动手**的那两个状态：人选了 REVISE/REDIRECT 之后
    # 要起指定节点（action_authorized），和人选了 EDIT 之后要等人改完
    # （awaiting_manual_edit）。那两件事运行时替不了 —— 它们是人的决定的执行面。
    if r_state == "failed":
        lines.append(f"  ⚠️  reviewer FAILED: {(review_failed or '')[:160]}")
    elif r_state == "done":
        lines.append(f"  ✅ reviewer done, review_critique={review_critique_id}")

    # 人选了 REVISE / REDIRECT 之后要起指定节点。这一步运行时替不了 ——
    # 它是**人的决定的执行面**，目标节点由人的选择决定。The flow
    # remains pending until the replacement producer actually completes.
    if d_state == "action_authorized":
        target_node = target.get("authorized_target_node")
        # 存量 entry 里可能躺着一个**起了也关不掉**的授权（2026-09-17 之前授权侧
        # 不校验这件事；yuankk 那两条就是）。同一个判据在这里再问一次 —— 催它去起
        # 一个结构上关不掉的节点，就是在制造那 40 轮。判决归 decision_package
        # 自己（重新呈递时会把它拦成 awaiting_human），这里只负责**别再催**。
        from core.loader import node_owes_post_node_flow

        if not node_owes_post_node_flow(str(target_node or "")):
            ctx.state.append_transcript(
                "post_node_flow_authorization_is_unexecutable",
                producing_node=source_node, producing_run_id=producing_run_id,
                authorized_target_node=target_node,
            )
            lines += [
                "",
                f"  ⛔ **这条授权执行不了**：目标 {target_node!r} 是服务节点/系统节点，",
                "  跑完不进 post-node flow 账本 —— 起它多少次，这条义务都关不掉，",
                "  而空转熔断在这一档看不见任何东西（实测空转 40 轮）。",
                "  **不要起它。** 重新裁决这一轮：",
                "  ```",
                f"  present_decision_package(source_node_type='{source_node}',",
                f"    producing_run_id='{producing_run_id}', ...)",
                "  ```",
                "  症结若确实在某个服务（检索/前处理/出图），选 REVISE 让产出节点",
                "  自己重新调用它，并把要改什么写进反馈。",
            ]
            return [LLMMessage(role="system", content="\n".join(lines))]
        lines.append("")
        lines.append("  ⏳ **NEXT: execute the authorized review action**")
        lines.append("  ```")
        lines.append(f"  run_node(node_type={target_node!r}, node_inputs={{")
        lines.append("    # original research inputs + reviewer feedback")
        lines.append("  })")
        lines.append("  ```")
        lines.append(
            f"  action={target.get('authorized_action')!r}; only {target_node!r} is authorized."
        )
        if target.get("recommended_feedback"):
            lines.append(
                "  reviewer feedback: "
                + str(target.get("recommended_feedback"))[:500]
            )
        return [LLMMessage(role="system", content="\n".join(lines))]
    if d_state == "action_in_progress":
        lines.append("")
        lines.append("  ⏳ Authorized revision is running; do not start a duplicate child.")
        return [LLMMessage(role="system", content="\n".join(lines))]
    if d_state == "awaiting_manual_edit":
        lines.append("")
        lines.append("  ⏸️ EDIT was selected; wait for the human edit and a new decision.")
        return [LLMMessage(role="system", content="\n".join(lines))]

    # 决策包的**恢复**路径（只此一种，Move 1d 之后）。
    #
    # 正常路径由运行时在 `run_node(<producing>)` 内部呈递，调度器不经手，所以
    # `d_state == "pending"` 时这里不再提醒 —— 提醒它去做一件运行时已经做了的事，
    # 只会换来一次重复呈递。
    #
    # 但**进程重启会丢掉 in-memory pause**：账本上 decision_state 停在
    # awaiting_human，而现场那个 pause 已经不在了。这时确实需要调度器用同一个
    # producing_run_id 重新呈递一次。这条路运行时替不了 —— 它没有"上一轮呈递过
    # 但现在没了"这个信息，只有账本有。
    if d_state == "awaiting_human":
        lines.append("")
        lines.append("  ⏳ **NEXT: 重新呈递决策包（进程重启丢了 in-memory pause）**")
        lines.append("  用同一 producing_run_id 重新呈递，让 auto-approve 按 reviewer")
        lines.append("  recommended action 机械作答。")
        lines.append("  ```")
        lines.append("  present_decision_package(")
        lines.append(f"    source_node_type='{source_node}',")
        lines.append(f"    producing_run_id='{producing_run_id}',")
        lines.append(f"    artifact_ids_produced={artifact_ids},")
        if review_critique_id:
            lines.append(f"    review_critique_artifact_id='{review_critique_id}',")
        if review_failed:
            lines.append(f"    review_failed_reason={review_failed[:100]!r},")
        lines.append("    producing_summary='<short summary of producing run>',")
        lines.append("  )")
        lines.append("  ```")
        lines.append("  user 看包后选 1-4，或开 auto-approve 倒计时自动选 recommended。")
        lines.append("  这是 user 唯一的决策点——做完才能跑下一 producing 节点。")
        return [LLMMessage(role="system", content="\n".join(lines))]

    # 全 done → 不该 reach 这里（present_decision_package 清了 entry），但 defensive
    return None


register_loop_hook(LoopHook(
    name="post_node_review_flow_reminder",
    description=(
        "producing 节点完后强制提醒 orchestrator 走完整 3-step post-producing flow："
        "_reviewer → _curator → present_decision_package。"
        "数据源 state.hook_state['pending_post_node_flow']（run_node 工具自动维护）。"
        "每条 entry 的 3 个 state 都 done 后被 present_decision_package 清掉。"
        "主要给 _orchestrator 用。"
    ),
    on_turn_start=_post_node_review_flow_reminder_on_turn_start,
    emits=("post_node_flow_stalled",
           "post_node_flow_authorization_is_unexecutable"),
))


# ── dreaming_due_reminder（opt-in，主要给 _orchestrator 用） ─────────────────
#
# v2.1 fix #5：检查上次 curator dreaming 距今多久。超过 7 天（可配）→ 每轮
# inject 提醒。

_DREAMING_DUE_LAST_CHECK = "dreaming_due_last_check"


def _check_dreaming_due(state, max_age_days: int = 7) -> tuple[bool, str | None]:
    """上次 dreaming 多久前 —— 问 run 账本，不问审计文件。

    这个 hook 曾经读 `core.curator_audit` 的 `curator_runs.jsonl`，而
    `CuratorAudit(` 全仓**零构造点** —— 那个文件从来没有写入方，于是本 hook
    永远返回"从未跑过"，每个派发过子节点的 session 都被永久唠叨一遍。
    `dreaming_scheduler` 自己那一路在 v3.2 已经诊断并绕开了这条死链，
    但这个 hook 没跟上 —— **同一个病例修了一处，兄弟处等了半年**。
    """
    from core.dreaming_scheduler import last_dreaming_at

    last_at = last_dreaming_at(getattr(state, "project_id", None) or "")
    if not last_at:
        return True, None
    try:
        last_dt = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last_dt).days >= max_age_days, last_at
    except (ValueError, TypeError):
        return True, last_at


def _dreaming_due_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    # ── 相关性门控（2026-08-07 实测）─────────────────────────────────────
    # 这是**项目级维护**提醒，却曾在每个 session 第 1 轮无条件注入 —— 用户
    # 问一句"负载均衡为什么重要"，回复结尾就被推销 KB dreaming。
    #
    # 第一性原理：维护提醒该在用户**处在能行动的位置**时出现 —— 项目工作的
    # 自然边界，不是新会话的第一句。判据取"本 session 是否真的在做项目工作"：
    # 起过子节点才算。纯问答/闲聊永远不会触发，也就不会被打扰。
    if not ctx.state.hook_state.get("_dispatched_any_child"):
        return None

    # 跨多 turn 缓存：每会话只在第一轮和每 5 轮检查一次（不是每轮都扫文件）
    last_check_turn = ctx.state.hook_state.get(_DREAMING_DUE_LAST_CHECK + "_turn", 0)
    if ctx.turn != 1 and ctx.turn - last_check_turn < 5:
        # 缓存 dreaming_due 状态
        cached = ctx.state.hook_state.get("dreaming_due_cached", False)
        if cached:
            return _build_dreaming_reminder(ctx.state.hook_state.get("dreaming_last_at"))
        return None

    cfg = (ctx.harness.hook_config or {}).get("dreaming_due_reminder", {})
    max_age_days = int(cfg.get("max_age_days", 7))
    is_due, last_at = _check_dreaming_due(ctx.state, max_age_days)

    ctx.state.hook_state[_DREAMING_DUE_LAST_CHECK + "_turn"] = ctx.turn
    ctx.state.hook_state["dreaming_due_cached"] = is_due
    ctx.state.hook_state["dreaming_last_at"] = last_at

    if not is_due:
        return None
    return _build_dreaming_reminder(last_at)


def _build_dreaming_reminder(last_at: str | None) -> list[LLMMessage]:
    if last_at:
        msg = (f"💤 **dreaming_due**：上次 curator Mode 2 (dreaming) 是 {last_at}，"
                f"已超过 7 天没扫 KB。")
    else:
        msg = "💤 **dreaming_due**：从未跑过 curator Mode 2 (dreaming)。"
    msg += (
        "\n\nMode 2 是 KB 长期质量的保证：扫过期 claim、找 synthesis 候选、"
        "生成 opportunity、自审 Mode 3 决策。"
        "\n\n建议主动 propose 给 user 或直接调起："
        "\n```"
        "\nrun_node(node_type='_curator', node_inputs={'mode': 'dreaming'})"
        "\n```"
    )
    return [LLMMessage(role="system", content=msg)]


register_loop_hook(LoopHook(
    name="dreaming_due_reminder",
    description=(
        "检查上次 curator Mode 2 (dreaming) 多久前，超过 N 天 (默认 7) 强提醒。"
        "主要给 _orchestrator 用。"
        "配置：hook_config.dreaming_due_reminder.max_age_days = 7"
    ),
    on_turn_start=_dreaming_due_on_turn_start,
))


register_loop_hook(LoopHook(
    name="reflection",
    description=(
        "每 N 轮在 turn 末注入一段自检 system message，让 LLM 总结进展、记录死胡同、"
        "重新审视假设、推进 required_outputs。opt-in。"
        "通过 `hook_config.reflection.every_n_turns` 控制频率，"
        "`hook_config.reflection.prompt` 自定义内容。"
    ),
    on_turn_end=_reflection_on_turn_end,
))


# ── external_signal_check (v0.7 — file-based control signal 注入) ────────────
#
# 任何外部进程通过 `core.signal.write_signal(project_id, action, content)` 写
# `<project_root>/control_signal.json`，本 hook 每 turn_start 读 + 注入 system
# message + 删文件。
#
# 设计动机：v4 dogfood 实测 experiment 节点 LLM 自我 scale up 跑 2.5h 无停手段
# —— chat.py stdin queue 仅 chat.py 用；e2e_dogfood / cron / 自动化场景没任何
# I/O 通道。signal file = 通用 driver-无关的"外部 → 跑中 agent"通信通道。
#
# 仅 _orchestrator 默认启用（要给其它节点用，yaml `loop_hooks:` 加上即可）。

def _external_signal_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    if not ctx.state.project_id:
        return None
    try:
        from .signal import read_signal, clear_signal
    except ImportError:
        return None
    sig = read_signal(ctx.state.project_id)
    if sig is None:
        return None

    action = sig.get("action", "inject")
    content = sig.get("content", "")
    written_at = sig.get("written_at", "")
    if not content.strip():
        clear_signal(ctx.state.project_id)
        return None

    # 消费 signal，下次 turn 不重复
    clear_signal(ctx.state.project_id)
    ctx.state.append_transcript(
        "external_signal_received",
        turn=ctx.turn,
        action=action,
        content_preview=content[:300],
        written_at=written_at,
    )
    return [LLMMessage(
        role="system",
        content=(
            f"📨 外部 signal 注入 (action={action}, 写入于 {written_at})：\n"
            f"{content}"
        ),
    )]


# ── citation_integrity_check (v0.9 — 防 manuscript / experiment_log 引用编造 claim_id) ──
#
# v5 dogfood 实测：writing LLM 在 manuscript 引 `claim_9f8f2f80` 4+ 次但 KB
# 不存在 = phantom citation。reviewer 看到但没卡 PROCEED。机械层根因：节点
# qc 只数 claim_id 出现次数，不验证每个 id 在 KB 真存在。
#
# 本 hook 在 on_turn_end 自动扫本 turn 新写的 manuscript / experiment_log
# artifact，对每条 cited claim_id 跟 KB 对比；任何 phantom 写一条
# `citation_validation` 事件到 transcript。qc 读 transcript 看结果。
#
# Opt-in via yaml `loop_hooks: [citation_integrity_check]`。默认 writing /
# analysis 都启用（在 yaml 里）。

def _citation_check_target_types() -> set[str]:
    """哪些工件要过引用诚信闸 —— 查类型注册表，不维护名单。

    曾是一份硬编码 set。漏改它的后果是**这道闸对新证据类型整个不存在**，而且
    不报错：幻觉引用照样进 KB，谁也不知道少查了一类。
    """
    from shared.lib.artifact_policy import citation_checked_types

    return set(citation_checked_types())


def _citation_integrity_on_turn_end(ctx: HookContext) -> list[LLMMessage] | None:
    """每 turn 末扫**本 turn 新写**的 manuscript / experiment_log：引用诚信 +
    （v3.2 item#3）manuscript 的 verdict ↔ KB status 一致性。"""
    try:
        from shared.lib.citation_integrity import (
            validate_artifact_citations, check_verdict_consistency,
        )
    except ImportError:
        return None

    # 找本 turn 新写的 artifact —— hook_state 里 last_seen_artifact_ids 记上 turn 末
    # 见过的 id 集合；本 turn 末跟现状 diff
    seen = ctx.state.hook_state.get("_citation_check_last_seen", set())
    if not isinstance(seen, set):
        seen = set(seen)
    current_ids = {a["id"] for a in ctx.state.list_artifacts()}
    new_ids = current_ids - seen
    ctx.state.hook_state["_citation_check_last_seen"] = current_ids

    verdict_warnings: list[str] = []
    target_types = _citation_check_target_types()
    for aid in new_ids:
        rec = ctx.state.read_artifact(aid)
        if rec is None:
            continue
        atype = rec.get("type")
        if atype not in target_types:
            continue
        result = validate_artifact_citations(aid, ctx.state)
        ctx.state.append_transcript(
            "citation_validation",
            turn=ctx.turn,
            artifact_id=aid,
            artifact_type=atype,
            passed=result["passed"],
            n_cited=result.get("n_cited", 0),
            n_phantom=result.get("n_phantom", 0),
            phantom_ids=result.get("phantom_ids", []),
            phantom_with_counts=result.get("phantom_with_counts", []),
        )

        # v3.2 item#3：manuscript 的 verdict 一致性（analysis_report 也查）
        if atype in ("manuscript", "analysis_report"):
            vc = check_verdict_consistency(rec.get("content") or "", ctx.state)
            ctx.state.append_transcript(
                "verdict_consistency", turn=ctx.turn, artifact_id=aid,
                passed=vc["passed"],
                undecided_but_concluded=vc["undecided_but_concluded"],
                direct_contradictions=vc["direct_contradictions"],
            )
            if not vc["passed"]:
                parts = []
                for u in vc["undecided_but_concluded"]:
                    parts.append(
                        f"  • {u['id']}：正文下了 '{u['prose_verdict']}' 结论，但 KB "
                        f"里它的 status={u['status']}（尚未定论）—— 要么先 "
                        f"update_claim_status 记录真实判定+证据，要么正文改为'未定论'")
                for c in vc["direct_contradictions"]:
                    parts.append(
                        f"  • {c['id']}：正文说 '{c['prose_verdict']}'，但 KB status"
                        f"={c['status']}（直接矛盾）—— 论文结论必须与 KB 判定一致")
                verdict_warnings.append(
                    f"⛔ manuscript {aid} 的 hypothesis 结论与 KB 判定不一致"
                    f"（'每个结论可溯源'是硬约束）：\n" + "\n".join(parts))

    if verdict_warnings:
        return [LLMMessage(role="system", content="\n\n".join(verdict_warnings))]
    return None


# ── memory_onboarding（turn 1：本节点该知道的既往教训）────────────────────
#
# 取代 `pre_run_briefing`。删掉的不是"注入历史"这件事，是它的**门控**：
# 旧 hook 要求非空 query（`node_inputs.research_question` 或 `harness.kb_query`），
# 而 `kb_query: ""` 在两个消费方语义相反 —— context_engine 读作"没填就派生
# 一个，不许绕开"，这里读作 falsy 直接 return None。实测 9 节点裸调度
# **8 个收不到任何东西**，其中 hypothesis 连主动检索工具都没有。
#
# 现在按 `applies_to` 机械匹配，无 query、无 opt-in：讲的是你这个节点、
# 或你能调到的工具上，本项目付过学费的教训。


def _memory_onboarding_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    if ctx.turn != 1:
        return None    # 后续轮由首用附单在动手时刻送达，不重复灌
    try:
        from core.memory_delivery import onboarding_slice

        names = [getattr(t, "name", "") for t in (getattr(ctx.harness, "tools", None) or [])]
        md = onboarding_slice(ctx.state, ctx.harness.node_type, names)
    except Exception as e:
        log.debug("memory onboarding failed: %s", e)
        return None
    if not md:
        return None
    ctx.state.append_transcript(
        "memory_onboarding_injected", turn=ctx.turn,
        node_type=ctx.harness.node_type, bytes=len(md.encode("utf-8")))
    # role=user：中段以 system 注入实测会让模型复述注入文本、输出膨胀 3×
    # （PR#462）。turn 1 虽在段首，但保持与其余注入一致的角色语义。
    return [LLMMessage(role="user", content=md)]


# ── user_files（用户交来的文件：这是机械事实，不是靠前端塞一行字）──────────
#
# 2026-09-04 的事故形状：用户从界面传了一个 800MB 的日志包，agent 全盘 find
# 找不到，于是让用户"把文件放到固定地址"—— 而用户没有 shell，放不了。
#
# 真身不只是那次上传失败。**即使传成功了，模型也没有任何机制知道盘上多了
# 一个文件**：唯一的告知通道是前端往用户的输入草稿里塞一行「📎 附件：路径」，
# 用户一删就没了；材料清单在框架侧零引用。
#
# "用户给了我什么"是纯机械事实（读一个目录），按本仓的分工它就该由框架每轮
# 算好递过去，而不是让模型去猜、去问、去 find。给**绝对路径**：相对路径锚在
# 节点自己的目录上，给它等于把"锚点是谁"这个已经错过一次的问题再交给模型。

_USER_FILES_KEY = "_user_files_fingerprint"


def _user_files_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    worktree = getattr(ctx.state, "project_worktree", None)
    if worktree is None:
        return None
    try:
        from core import materials

        present = materials.inventory(worktree)
    except Exception as e:                      # 仪表盘缺一格不该让整轮注入失败
        log.debug("user files inventory failed: %s", e)
        return None

    seen: list = ctx.state.hook_state.get(_USER_FILES_KEY) or []
    known = {tuple(item) for item in seen}
    current = [(item.name, item.sha256) for item in present]
    ctx.state.hook_state[_USER_FILES_KEY] = [list(item) for item in current]

    first_look = not seen
    fresh = [item for item in present if (item.name, item.sha256) not in known]
    if not present or (not first_look and not fresh):
        return None

    if first_look:
        lines = [f"## 📎 用户交来的文件（{len(present)} 份，在这个工作区里）", ""]
        lines.extend(materials.describe(present))
    else:
        lines = [f"## 📎 用户刚交来 {len(fresh)} 份文件", ""]
        lines.extend(materials.describe(fresh))
    lines += [
        "",
        "这些是**用户给的输入**，路径就是身份：`read_file` 直接读，`run_bash` 里"
        "按这个绝对路径解压/统计（sandbox 里同路径可见）。要当研究证据用就走 "
        "`import_artifact`（出处会永久标成 imported）。",
        "别让用户自己「把文件放到某个地址」—— 他手上没有 shell，"
        "文件已经在上面了。",
    ]
    ctx.state.append_transcript(
        "user_files_injected", turn=ctx.turn,
        node_type=ctx.harness.node_type,
        n_total=len(present), n_new=len(present) if first_look else len(fresh))
    return [LLMMessage(role="user", content="\n".join(lines))]


register_loop_hook(LoopHook(
    name="user_files",
    description=(
        "每轮 LLM 调用前把「用户交来了哪些文件」作为机械事实注入（绝对路径 + "
        "大小 + sha256 + 备注）。首轮给全量，之后只给新增。"
        "替代前端往输入草稿里塞路径的老告知通道。"
    ),
    on_turn_start=_user_files_on_turn_start,
    emits=("user_files_injected",),
))


# ── law_review_gate（reviewer 进场：把用户铁律展开成必答检查项）────────────
#
# 铁律要有牙齿，只能靠它在**审查时变成必须逐条回答的问题**，而不是背景散文。
# 本仓的教训：prompt 里写三遍的"必须"不是机制（v21 收尾闸）。
#
# 强度是**红旗不是硬闸**（wangd 2026-08-21 拍板）：硬闸误伤会卡死主路径，
# 先跑两个 dogfood 再论。所以这里只保证"审查时必须逐条回答"，
# 回答"违反了"并不阻断产出 —— 它进 review 结论供人看。


def _law_review_gate_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    if ctx.turn != 1 or ctx.harness.node_type != "_reviewer":
        return None
    try:
        from core.memory_delivery import law_checklist

        laws = law_checklist(ctx.state)
    except Exception as e:
        log.debug("law checklist failed: %s", e)
        return None
    if not laws:
        return None
    lines = ["## ⚖️ 用户所立铁律 —— 本次审查必须**逐条回答**", ""]
    for i, law in enumerate(laws, 1):
        lines.append(f"{i}. {law}")
    lines += [
        "",
        "对每一条给出：**遵守 / 违反 / 不适用**，并给出判据（指向产物里的具体位置）。",
        "「不适用」也要说清为什么不适用 —— 略过不答等于没审。",
        "",
        "这些是**用户亲口立的**，不是我建议的检查项。发现违反时亮红旗写进 "
        "concerns，不必因此直接 revise —— 由人决定怎么处理。",
    ]
    ctx.state.append_transcript("law_checklist_injected", turn=ctx.turn,
                                n_laws=len(laws))
    return [LLMMessage(role="user", content="\n".join(lines))]


register_loop_hook(LoopHook(
    name="law_review_gate",
    description=(
        "reviewer 进场时把 MEMORY.md 的科研铁律逐条展开成**必答检查项**。"
        "铁律是用户亲口立的规矩，散文式地混在背景里等于没有 —— 它必须变成"
        "审查时不能略过的问题。红旗强度：发现违反写进 concerns 供人决断，"
        "不直接阻断产出。"
    ),
    on_turn_start=_law_review_gate_on_turn_start,
))


register_loop_hook(LoopHook(
    name="memory_onboarding",
    description=(
        "turn 1 注入本项目手册中**适用于本节点**的既往教训。匹配按 "
        "`applies_to`（nodes 命中本节点，或 tools 与本节点工具面相交）"
        "机械判定，不做语义相关性判断 —— 相关性会漏，且解释不了为什么没送到。\n\n"
        "无 query 门控、无 opt-in：这些是本项目付过学费买来的东西。"
    ),
    on_turn_start=_memory_onboarding_on_turn_start,
))


register_loop_hook(LoopHook(
    name="citation_integrity_check",
    description=(
        "on_turn_end 自动扫**本 turn 新写**的 manuscript / "
        "experiment_log artifact，找 cited claim_id 是 phantom (KB 不存在) "
        "的。结果写 `citation_validation` 事件到 transcript（qc 读它判）。\n"
        "默认 writing / experiment 节点启用。Hook 本身不卡 —— 真挡是节点 qc "
        "`cited_claim_ids_all_exist_in_kb` 配合做的。"
    ),
    on_turn_end=_citation_integrity_on_turn_end,
    emits=("citation_validation", "verdict_consistency"),
))


# ── author_wiring_check (v1.1 — judge 数错 kb_ingest 参数，照 citation_integrity_check
#    的模式改：Python 先算好确定数字，判断题变算术题) ──
#
# 根因（2026-07 实测复现）：literature 的 papers_have_author_wiring 这条 qc
# 让 judge 自己从 state_summary 里 N 条 kb_ingest（当年还有已下架的
# kb_register_artifact_as_chunk）的 args dump 里数有几条 author_concept_ids
# 非空——judge 数错了（5/7 条明明
# 非空，判成"均未提及"）。check 自己的 description 里写着"v3 dogfood 实测 4
# 个项目都漏 author wiring"，说明这个误判模式反复发生，不是偶然。跟
# citation_integrity_check 解决 phantom citation 误判是同一类根因：判断题
# 伪装成"要从一堆 JSON 里自己数东西"，纯 LLM judge 容易数错或漏看。
#
# 本 hook 在 on_turn_end 直接读 KB（不靠 judge 从 tool_call args 里数），
# 统计**本 run** 新写入的 chunk 里有几条带非空 author_concept_ids，写
# `chunk_author_wiring` 事件到 transcript（只在数字变化时写，避免刷屏）。
# qc 读这里的 n_chunks_this_run / n_with_author_wiring 两个数，判断题变
# "n_with_author_wiring >= threshold" 的算术题。
#
# Opt-in via yaml `loop_hooks: [author_wiring_check]`。

def _author_wiring_on_turn_end(ctx: HookContext) -> list[LLMMessage] | None:
    """每 turn 末统计**本 run** 写入的 chunk 里有几条带 author_concept_ids。"""
    chunks_this_run = [
        c for c in ctx.state.list_kb("chunks")
        if c.get("created_by_run_id") == ctx.state.run_id
    ]
    n_total = len(chunks_this_run)
    with_author = [c["id"] for c in chunks_this_run if c.get("author_concept_ids")]
    missing = [c["id"] for c in chunks_this_run if not c.get("author_concept_ids")]

    current = (n_total, len(with_author))
    if ctx.state.hook_state.get("_author_wiring_last") == current:
        return None    # 数字没变化 → 不重复写事件（避免刷屏）
    ctx.state.hook_state["_author_wiring_last"] = current

    ctx.state.append_transcript(
        "chunk_author_wiring",
        turn=ctx.turn,
        n_chunks_this_run=n_total,
        n_with_author_wiring=len(with_author),
        chunk_ids_with_author=with_author,
        chunk_ids_missing_author=missing,
    )
    return None


register_loop_hook(LoopHook(
    name="author_wiring_check",
    description=(
        "on_turn_end 直接查 KB 统计**本 run** 写入的 chunk 里有几条带非空 "
        "author_concept_ids，写 `chunk_author_wiring` 事件到 transcript（字段 "
        "n_chunks_this_run / n_with_author_wiring / chunk_ids_with_author / "
        "chunk_ids_missing_author）。只在数字变化时写，避免刷屏。\n"
        "解决的问题：让 judge 自己从一堆 kb_ingest 之类的写入调用 "
        "的 args dump 里数有几条非空 author_concept_ids 容易数错（2026-07 实测：5/7 "
        "条明明非空，judge 判成'均未提及'）。现在判断题变算术题——qc 直接读 "
        "n_with_author_wiring 跟 threshold 比大小，不用自己数。"
    ),
    on_turn_end=_author_wiring_on_turn_end,
    emits=("chunk_author_wiring",),
))


register_loop_hook(LoopHook(
    name="external_signal_check",
    description=(
        "每 turn_start 读 <project_root>/control_signal.json，存在则把 content "
        "注入 system message + 删文件。任何外部进程 (CLI / driver / cron) 调"
        " `core.signal.write_signal(...)` 即可触发。\n"
        "action ∈ {inject (普通 system msg), pause (LLM 调 request_human_input), "
        "abort (LLM 立刻 wrap up)}。"
        "默认仅 _orchestrator 启用（其它节点 yaml `loop_hooks:` 加上才生效）。"
    ),
    on_turn_start=_external_signal_on_turn_start,
))


# ── highrisk_confirm（2026-07，always-on，安全相关不走 opt-in）───────────────

def _highrisk_confirm_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """高危工具调用（run_bash / execute_python / submit_job …）会 pause（见
    shared/lib/dangerous_commands.py）。pause 恢复后，人的原始回答文本会替换那条
    pause 占位 tool_result；本 hook 在下一轮 turn_start 读这条回答，判定是否为
    "批准"，是则给这条命令打一次性确认标记。

    **确认标记只有在模型重新调一次同一个工具时才会被消费。** 批准本身不执行
    任何东西 —— 这半条契约以前只写在这里的注释里，从没送到模型手上，于是模型
    普遍误以为"人批准了 = 框架替我跑了"。E2E v19 实测：submit_job 批准后模型
    直接去 job_status 查状态，查不到就断言"local scheduler 无法启动进程"，接
    着换 run_node / safe_run_bash / check_external_job_health 连试三条路，
    唯独没重调 submit_job（真实故障：seq 581→585）。所以这里必须显式告诉它。

    还有一处坑同样要讲明：确认标记绑定在**逐字相同的参数文本**上
    （`_cmd_key(text)`）。同一次实测里模型重试时把 job_name 从
    `ka_lj_slow_rep1` 改成了 `ka_lj_slow_rep1_v2`，通行证随即失效、又弹一次
    审批。这是对的（批准的是那一条具体命令，不是那个工具），但得说出来。
    """
    from shared.lib import dangerous_commands as _dc

    pending = _dc.pop_pending_ask(ctx.state)
    if pending is None:
        return None

    tool = pending.get("tool") or "该工具"
    category = pending.get("category") or "高危操作"

    # 找刚才 pause 那条 tool_call 对应的 tool_result（人回答已替换进去）
    answer_text = None
    for m in reversed(ctx.messages):
        if m.role == "tool":
            answer_text = m.content or ""
            break

    # 批准与否的**权威判定**在 pause_driver 做（那里同时有原始答复和界面印的
    # 选项表，见 record_highrisk_answer / issue #422）。这里优先读那条记账；
    # 没有记账的路径（老 resume、直接调 hook 的测试）退回文本判定，判定本身
    # 也已经会先拆 JSON 信封，不再只认自由文本。
    recorded = pending.get("approved")
    approved = bool(recorded) if isinstance(recorded, bool) \
        else _dc.looks_like_approval(answer_text)
    if approved:
        _dc.mark_confirmed(ctx.state, pending["text"])
        ctx.state.append_transcript(
            "highrisk_confirm_approved", tool=tool, category=category,
        )
        return [LLMMessage(role="system", content=(
            f"人已批准这次 {category}（工具：{tool}）。\n\n"
            f"批准**不会**替你执行那次调用 —— 它只发了一张一次性通行证。\n"
            f"现在请立刻用**与上次逐字相同的参数**重新调用一次 {tool}，"
            f"那一次才会真正执行。\n"
            f"改动任何一个参数（哪怕只是 job_name 之类的名字）都会作废这张"
            f"通行证并重新触发审批。要改参数，就当成一次新的调用重新申请。"
        ))]

    ctx.state.append_transcript(
        "highrisk_confirm_denied_or_unclear", tool=tool, category=category,
        answer_preview=(pending.get("answer_text")
                        or _dc.response_text(answer_text))[:100],
    )
    return [LLMMessage(role="system", content=(
        f"这次 {category}（工具：{tool}）**没有**获得批准"
        f"（人的回答：{(pending.get('answer_text') or _dc.response_text(answer_text) or '（空）')[:200]}）。\n"
        f"通行证没有发出，用相同参数重调只会再被挡一次。换方案，或者用 "
        f"request_human_input 问清楚该怎么做。"
    ))]


register_loop_hook(LoopHook(
    name="highrisk_confirm",
    description=(
        "高危工具 pause 恢复后，读人的回答判定批准/拒绝，给命令打一次性确认标记，"
        "并把「批准不等于已执行，必须用逐字相同的参数重调一次」这条契约注入给模型。"
        "**always-on，不走 harness.loop_hooks opt-in**（安全相关，owner 不能误关）。"
    ),
    on_turn_start=_highrisk_confirm_on_turn_start,
))


# ── project_orientation（v2.1：Workspace-First 的定向层）─────────────────────
#
# Workspace-First 三层里的第三层。材料层（全读/各写各的）和叙事层
# （research_state / MEMORY.md 单写者）都已成立，但"项目现在什么样"仍要
# 每个节点自己去翻 —— 而实测反复证明：**只要还需要模型主动去查，它就有
# 一半概率不查**（契约不查、validate 不调、reviewer 猜 artifact id 猜出 404、
# orchestrator 每轮花几个工具调用"先看看现状"）。
#
# 本 hook 把三样机械事实开局摆到**每个绑定了 worktree 的节点**面前：
#   1. 项目目录里各节点交付了什么（一行一节点，带产物数与最新几个 id）
#   2. 最新 research_state 的裁决摘要（version/verdict/未裁决假说）
#   3. MEMORY.md 的前几行（索引头 —— 想深读自己 read_file）
# 全部来自扫盘，不问模型、不猜。约 1KB，每 state 注入一次。

_ORIENTATION_KEY = "_project_orientation_shown"

#: 「哪些 artifact 对下游有价值」的判据在类型注册表里声明一次
#: （`shared.lib.artifact_policy.framework_internal`），这里只查表。
#: 仓库初始化模板（"Owner: `x`." 之类），不算节点自述。
_BOILERPLATE_README = __import__("re").compile(r"^(Owner:\s*`?\w+`?\.?|The owner controls)")


def _rank_for_downstream(ids: list[str]) -> list[str]:
    """预览要挑**下游用得上**的，不是字母序靠前的。

    E2E v13 实测：hypothesis 交付 16 个产物，预览显示的却是
    `compression_log__compression_turn_24, compression_log__compression_turn_39,
    hypothesis_cluster_report__…` —— 按字母序取前三，把
    pre_registration / research_state 这两个真正的交付物挤掉了。
    字母序不是重要性；定向层给的线索是噪音，等于没给。
    """
    from shared.lib.artifact_policy import rank_for_downstream

    return rank_for_downstream(list(ids))


#: 定向注入的统一前缀 —— summarizer 压缩后按它识别并**取代**旧定向
#: （同 notice 的 superseded 纪律：合并了内容就要删被取代的那条，PR #351 的教训）。
ORIENTATION_PREFIX = "🗺️ **项目现状"


def build_orientation_snapshot(state) -> str | None:
    """扫盘生成项目现状文本（纯函数，无 hook 状态副作用）。

    两个调用方：turn-1 的定向 hook；summarizer 压缩后的事实重扫（P4）——
    摘要只管叙事连续性，事实以压缩当刻的重扫为准，不让账本以权威口吻
    断言可能已过期的状态。
    """
    worktree = getattr(state, "project_worktree", None)
    if worktree is None:
        return None

    from pathlib import Path

    root = Path(worktree)

    # 1) 各节点交付面（机械事实）+ 节点自述 README 头（叙事，自愿维护 ——
    #    写了就会被带给所有后续节点看：可见性激励，不是又一道门）
    # 各目录下有哪些记录：问账本，不扫盘（正文文件本身不带类型和出处）。
    from core.ledger import workspace_store

    ids_by_dir: dict[str, list[str]] = {}
    for head in workspace_store(root).heads().values():
        top = head.path.split("/", 1)[0] if head.path else ""
        if top:
            ids_by_dir.setdefault(top, []).append(head.artifact_id)
    rows = []
    for node_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name != ".git"):
        ids = sorted(ids_by_dir.get(node_dir.name, []))
        readme_line = ""
        readme = node_dir / "README.md"
        if readme.is_file():
            try:
                first = next((l.strip() for l in
                              readme.read_text(encoding="utf-8").splitlines()
                              if l.strip() and not l.startswith("#")), "")
                # 仓库初始化写的模板不是自述。E2E v13 实测：每个节点都挂着一句
                # "Owner: `x`." —— 零信息量，还掩盖了"这个节点还没写自述"。
                if first and not _BOILERPLATE_README.match(first):
                    readme_line = f" ｜ 自述：{first[:150]}"
            except OSError:
                pass
        if not ids and not readme_line:
            continue
        preview = _rank_for_downstream(ids)
        head = ", ".join(preview[:3]) + (" …" if len(ids) > 3 else "")
        # 路径写成**可以直接粘进工具的形式**。
        #
        # 这一行原来写 `figures/`，那是**工作区根**坐标；而文件工具的相对
        # 路径锚在**节点自己的目录**（working_directory）。于是读到地图的
        # writing 节点照抄 `figures/figures`，被解析成
        # `paper/postprocess/figures` —— 不存在。
        #
        # v22 实测：`list_files` 89 次"找不到目录"，**全部**是这个形状
        # （writing/postprocess/…、writing/writing/latex_build、
        #  experiment/writing/latex_build）。89 次里 89 次都收到了"下现有：…"
        # 的邻居提示，仍然纠正不过来 —— 因为 agent 没有猜，它在**忠实使用地图
        # 给的坐标**，只是那套坐标工具不认。
        #
        # 地图给什么，agent 就用什么。所以地图必须给能直接用的字符串：绝对路径。
        # 这与"报错必须列出正确答案"是同一条规矩 —— 提示不能只说"不对"，
        # 要给对的那个。
        rows.append(f"  - `{node_dir.resolve()}/`：{len(ids)} 个产物"
                    + (f"（{head}）" if ids else "") + readme_line)

    # 空项目（画图/调研一次性任务的开局）什么都不注入 —— 没内容的定向是噪音。
    memory = root / "MEMORY.md"
    has_memory = False
    if memory.is_file():
        try:
            has_memory = any(l.strip() for l in
                             memory.read_text(encoding="utf-8").splitlines()[:8])
        except OSError:
            pass
    if not rows and not has_memory:
        return None

    lines = [
        f"{ORIENTATION_PREFIX}（机械扫描）** —— 整个目录你都可读；写只限自己的作用域。",
        "",
        # 坐标系必须写明：文件工具的相对路径**锚在你自己的节点目录**，不是工作区根。
        # 下面列的是绝对路径，可以直接粘进 list_files / read_file。跨节点取料时
        # 请用它们，别自己拼相对路径（v22 实测：拼错 89 次，全是把节点名贴重了）。
        "**路径怎么写**：相对路径锚在你自己的节点目录；跨节点请直接用下面的绝对路径。",
        "",
    ]
    if rows:
        lines.append("**各节点已交付**：")
        lines.extend(rows)

    # 2) research_state 摘要（叙事层权威：Analysis）
    try:
        from core import research_state_reader as _rs

        best = _rs.latest_located(root)
        if best is not None:
            version, record, source = best
            meta = _rs.metadata_of(record)
            unresolved = [str(r.get("id")) for r in (meta.get("hypotheses") or [])
                          if isinstance(r, dict) and str(r.get("status")) == "active"]
            try:
                shown = source.relative_to(root)
            except ValueError:
                shown = source
            lines += ["", f"**研究状态**（research_state v{version}，"
                          f"verdict=`{meta.get('verdict')}`）：未裁决假说 "
                          f"{', '.join(unresolved) if unresolved else '（无）'}；"
                          f"详读 `{shown}`"]
    except Exception:
        # 观察层不打断主流程，但**不能静默** —— 这一段消失过一次（读取口径
        # 改造时返回值形状变了），只有一条断言正文的测试抓到了。
        log.warning("orientation: research_state 摘要生成失败", exc_info=True)

    # 3) MEMORY.md 索引头（叙事层权威：curator）
    if has_memory:
        try:
            head_lines = memory.read_text(encoding="utf-8").splitlines()[:8]
            lines += ["", "**项目记忆**（MEMORY.md 头部；深读自己 read_file）：",
                      *[f"  {l}" for l in head_lines if l.strip()]]
        except OSError:
            pass

    # 4) 这台执行主机提供什么算力软件（部署声明，机器生成）
    #
    # 加这一段的理由是一次真实事故：experiment 用 `which lammps || which lmp`
    # 探测，而二进制叫 `lmp_serial`，两个都没中 → 判定"没装" → 去 conda
    # install。LAMMPS 一直在 `/opt/homebrew/bin/lmp_serial`，上一轮还用它跑成
    # 功过。**平台不知道自己有什么**，于是每轮会话都靠模型猜二进制名。
    #
    # 空登记表也出这一段：沉默会被读成"这里什么都没有"。
    try:
        from .host_capabilities import render_section

        lines += ["", render_section()]
    except Exception:
        pass

    return "\n".join(lines)


def _project_orientation_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    state = ctx.state
    if getattr(state, "project_worktree", None) is None \
            or state.hook_state.get(_ORIENTATION_KEY):
        return None
    state.hook_state[_ORIENTATION_KEY] = True
    text = build_orientation_snapshot(state)
    if text is None:
        return None
    return [LLMMessage(role="system", content=text)]


_ORG_ORIENTATION_KEY = "_org_orientation_shown"

#: 哪些节点该在开局收到 org 知识。判据是"它在**立问题/定方向**"——
#: 那是组织已知最该起作用的时刻。执行类节点开局不需要（它们的输入由
#: 派发决定），配方注入在实验阶段另行接入。
_ORG_ORIENTATION_NODES = ("hypothesis", "_orchestrator")


def _org_orientation_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """开题注入：立问题之前先摆出"本组已知什么"。

    为什么是 hook 而不是"让模型自己 search_kb"：**只要还需要模型主动去查，
    它就有一半概率不查**（实测）。复利不押注在模型的主动性上 ——
    org KB 若只提供检索接口，它就是个没人再看的抽屉。

    每个 state 注入一次；org 空时静默（"本组没读过这个方向"由
    org_delivery 决定要不要说，这里不编造）。
    """
    state = ctx.state
    if str(getattr(state, "node_type", "")) not in _ORG_ORIENTATION_NODES:
        return None
    if state.hook_state.get(_ORG_ORIENTATION_KEY):
        return None
    state.hook_state[_ORG_ORIENTATION_KEY] = True
    try:
        from core.org_delivery import org_orientation

        text = org_orientation(state)
    except Exception:
        return None
    if not text:
        return None
    return [LLMMessage(role="system", content=text)]


register_loop_hook(LoopHook(
    name="org_orientation",
    description=(
        "开局注入组织知识（验证结论 / 死路 / 适用条件 / 复现记数），机械触发、"
        "常数上限。终态沉淀 ↔ 开题注入构成复利飞轮，缺送达侧则 org KB 退化成"
        "没人看的抽屉。"
    ),
    on_turn_start=_org_orientation_on_turn_start,
))


register_loop_hook(LoopHook(
    name="project_orientation",
    description=(
        "开局注入项目现状（各节点交付面 / research_state 摘要 / MEMORY.md 索引头），"
        "全部机械扫盘。Workspace-First 的定向层：全局可读 ≠ 全局了解。"
    ),
    on_turn_start=_project_orientation_on_turn_start,
))


# ── turn_budget ──────────────────────────────────────────────────────────────

def _turn_budget_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    """快没轮次时告诉它，让它来得及收尾。

    E2E v23 实测：experiment 在第 40 轮被切断，14 个模拟全跑完但 MSD 分析
    一行没做 —— 它是**事后**才知道有上限的。知道"还剩 8 轮"的节点可以先把
    已算出的结果落成产物、把长任务拆两轮；不知道的只能一路往前，剪在哪儿
    全看运气。

    上限从 hook_state 读（`agent_loop` 每轮记），不从 `ctx.harness.max_turns`
    读 —— 后者是 yaml 声明值，可能是 0（"由框架决定"），和**实际生效的**上限
    是两个数。同一个问题两个来源，取错那个这条提示就永远算不对。
    """
    from core.turn_budget import budget_notice

    note = budget_notice(turn=ctx.turn,
                         max_turns=ctx.state.hook_state.get("_max_turns"))
    return [LLMMessage(role="system", content=note)] if note else None


register_loop_hook(LoopHook(
    name="turn_budget",
    description=(
        "快没轮次时提醒节点收尾（过半 / 剩 1/4 / 剩 5 轮三档，越紧越具体）。"
        "用途：撞上限从'意外'变成'可以提前避开的事'——被切断在半途的产物"
        "对下游几乎没用。"
    ),
    on_turn_start=_turn_budget_on_turn_start,
))



# ── situation_router（v1，orchestrator 专用）────────────────────────────────
#
# 调度器是用户交互的主入口，但它此前只有一处真正的"局面路由"：中途打断模式
# （chat.py fork 出专用小 prompt + 单工具）。其余所有局面 —— 新项目开题 / 收尾
# 三步欠账 / 研究推进中 —— 共用同一份大 system prompt，模型要自己从十几个段落
# 里翻出"我现在处于哪种局面、这一轮该怎么解读用户输入"。
#
# 2026-08-17 实测（英国饮食文化开题）：新项目第一条消息，orchestrator 按训练
# 先验走了 literature→hypothesis→writing 老管线 —— v2.1 的"开题入口是
# hypothesis(Analysis)"写在 prompt 第 200 行上下，没竞争过第 75 行的老示例。
#
# 本 hook 把"局面"变成机械判定 + 每轮一条**覆盖式**注入：
#   - 局面(flow_debt / fresh / in_research)框架判，语义意图仍归模型 ——
#     分界线就是"框架能机械回答的判断别交给模型"。
#   - 覆盖不追加：同一 marker 的旧消息先移除再注入新的；内容没变且旧消息还在
#     时整体 no-op（保 byte-identity → 驻定重放与 prompt cache 不被打扰）。

_SITUATION_MARKER = "📍 **局面**"
_SITUATION_LAST_KEY = "_situation_router_last"


def _latest_research_state(state) -> tuple[str | None, str | None]:
    """返回 (最新 research_state 的绝对路径, verdict)；没有则 (None, None)。

    worktree 是权威（v2.1 产物落节点 Git 目录）；未绑 Project 的 run 退回
    本 run artifacts。路径给**绝对路径** —— 地图必须给能直接粘进工具的坐标
    （E2E v22：给工作区根坐标，89/89 次被解析成不存在的路径）。
    """
    from pathlib import Path

    worktree = getattr(state, "project_worktree", None)
    if worktree is not None:
        # 唯一读取实现在 core/research_state_reader —— 这里此前按 mtime 挑文件，
        # 与那边按版本号挑是两条会分叉的"最新"规则。
        from core import research_state_reader as _rs

        try:
            found = _rs.latest_located(worktree)
        except _rs.UnmigratedWorkspaceError:
            return None, None
        if found is None:
            return None, None
        _version, record, path = found
        verdict = (record.get("metadata") or {}).get("verdict")
        return str(Path(path).resolve()), verdict
    # 未绑 Project 的 run（CLI / fixture）：退回本 run artifacts 查存在性
    try:
        for a in state.list_artifacts():
            if a.get("type") == "research_state":
                return f"artifact:{a.get('id')}（read_artifact 可读）", None
    except Exception:
        pass
    return None, None


def _detect_situation(state) -> tuple[str, list[str]]:
    """机械判定当前局面。返回 (situation_key, 注入正文行)。"""
    lines: list[str] = []

    # 1) 收尾欠账优先 —— 判据与 post_node_review_flow_reminder 同源
    flow = state.hook_state.get("pending_post_node_flow") or []
    debt = None
    for entry in flow:
        if (entry.get("review_state") == "pending"
                    or entry.get("decision_state") in (
                    "pending", "awaiting_human", "action_authorized",
                    "action_in_progress", "awaiting_manual_edit")):
            debt = entry
            break
    if debt is not None:
        node = debt.get("producing_node", "?")
        rid = debt.get("producing_run_id", "?")
        lines.append(f"上一个 producing 节点（{node}，run={rid}）的收尾三步还没走完。")
        lines.append("- 本轮合法推进 = 按 🚨 POST-PRODUCING FLOW 提醒给的下一步调用（参数照抄提醒即可，机械参数框架自动补齐）。")
        lines.append("- 收尾完成前起新 producing 节点会被框架硬拒。")
        lines.append("- 用户新指令与收尾冲突时：先答复用户并说明欠账，再按用户意思办。")
        return "flow_debt", lines

    # 2) 有没有生效的研究计划
    rs_path, verdict = _latest_research_state(state)
    if rs_path is None:
        lines.append("本项目还没有研究计划（无 research_state）。")
        lines.append("- 用户提出研究目标 → 起 `hypothesis`（Analysis）开题：定研究问题、冻结预注册；literature/data 它自己按需调，不要先替它铺一轮 landscape 综述。")
        lines.append("- 用户明确只要一次独立服务（调研/画图/备数据）→ 直接调对应 service 节点。")
        lines.append("- 用户带来外部已完成的材料 → `import_artifact` 登记，别起节点重做一遍。")
        lines.append("- 闲聊 / 问答 / 状态查询 → 直接回答，不起节点。")
        lines.extend(_research_facts(state))
        return "fresh", lines

    lines.append("研究进行中（research_state 已存在"
                 + (f"，verdict={verdict}" if verdict else "") + "）。")
    lines.append("- 下一步看 research_state 的 verdict / next_steps，不按固定管线顺序。")
    hint = "" if rs_path.startswith("artifact:") else "（read_file 可读）"
    lines.append(f"- 最新 research_state：`{rs_path}`{hint}")
    lines.append("- 每轮 experiment 之后回到 `hypothesis`（Analysis）更新裁决，再决定下一步。")
    lines.extend(_research_facts(state, rs_path=rs_path, verdict=verdict))
    return "in_research", lines


def _research_facts(state, *, rs_path: str | None = None,
                    verdict: str | None = None) -> list[str]:
    """研究面的机械事实 —— 目标 / 承诺账进度 / 证据库存。

    局面注入原先只回答"**流程**走到哪了"（收尾欠不欠账、有没有计划），回答不了
    "**研究**走到哪了"。于是调度器只能按默认剧本派发：2026-08-18 英国饮食那趟，
    一份全是文献裁决的 research_plan 被派给了计算实验节点，而"盘上零证据产物"
    是 experiment 进场后自己发现的 —— 那本该是派发前就该知道的事。

    算在 `core/research_situation`（可单测、不依赖 hook）；这里只调用。任何
    异常都吞掉：仪表盘缺一格不该让整轮注入失败。
    """
    try:
        from core.research_situation import compute_situation, render_situation_facts

        return render_situation_facts(
            compute_situation(state, research_state_path=rs_path,
                              research_state_verdict=verdict)
        )
    except Exception:
        return []


def _situation_overlays(state) -> list[str]:
    """跨局面的附加事实（后台子节点 / continuous），都便宜、都机械。"""
    out: list[str] = []
    try:
        from core.pause import list_active_runs

        others = [r for r in list_active_runs()
                  if getattr(r, "run_id", None) != state.run_id]
        if others:
            kinds = ", ".join(sorted({r.node_type for r in others}))
            out.append(f"- ⏳ {len(others)} 个子节点还在跑（{kinds}）—— 不要重复起同类节点，不要轮询。")
    except Exception:
        pass
    if (state.hook_state.get("continuous_loop")
            and state.hook_state.get("continuous_phase", "running") == "running"):
        out.append("- ♾️ continuous mode 生效：按协议推进，每轮末尾单独一行 CONTINUOUS_STATUS。")
    return out


def _situation_router_on_turn_start(ctx: HookContext) -> list[LLMMessage] | None:
    state = ctx.state
    if state.node_type != "_orchestrator":
        return None
    if ctx.turn != 1:          # 每条用户消息只路由一次；工具轮之间不动 messages（保 cache）
        return None

    situation, lines = _detect_situation(state)
    lines += _situation_overlays(state)
    text = (f"{_SITUATION_MARKER}（框架机械判定：`{situation}`）\n"
            + "\n".join(lines))

    marker_alive = any(
        m.role == "system" and (m.content or "").startswith(_SITUATION_MARKER)
        for m in ctx.messages)
    if marker_alive and state.hook_state.get(_SITUATION_LAST_KEY) == text:
        return None            # 局面没变且旧消息还在 —— 整体 no-op，保 byte-identity

    # 覆盖式：移除旧的局面消息（不管几条），再注入新的
    ctx.messages[:] = [
        m for m in ctx.messages
        if not (m.role == "system" and (m.content or "").startswith(_SITUATION_MARKER))
    ]
    state.hook_state[_SITUATION_LAST_KEY] = text
    state.append_transcript("situation_routed", situation=situation)
    return [LLMMessage(role="system", content=text)]


register_loop_hook(LoopHook(
    name="situation_router",
    description=(
        "orchestrator 专用局面路由（v1）：每条用户消息前机械判定当前局面"
        "（flow_debt / fresh / in_research + 后台子节点、continuous overlay），"
        "覆盖式注入一条局面消息 —— 局面归框架判，意图归模型判。"
        "同内容不重复注入（保驻定重放与 prompt cache）。"
    ),
    on_turn_start=_situation_router_on_turn_start,
))


def _integration_targets(artifact_ids: list[str]) -> list[str]:
    """整合目标按同一个判据算 —— 与 run_node 建 flow entry 同源。"""
    from shared.lib.artifact_policy import integration_targets

    return integration_targets(list(artifact_ids or []))


# ── closing_manifest（2026-08-23：收尾时把交付物清单递到手上）───────────────
#
# 缺口与分工见 `core/closing_manifest.py` 的模块说明。这里只解决"什么时候递"。
#
# ## 为什么触发条件是「它自己宣称做完了」
#
# `on_before_finish` 对 continuous orchestrator 来说**每一次回用户话都会跑**
# （不调工具就是一次 finish）。而框架侧的闸只放一次
# （`_finish_gate_used`，且只在 hook 真的返回了消息时置位）。两件事合起来：
# 判据一旦选宽了，它就会在研究中段某次普通回复上把这唯一一次机会烧掉，
# 真正的收尾反而拿不到清单 —— 而且不报错。
#
# 所以判据取**它自己写下的终态声明** `CONTINUOUS_STATUS: complete`：这是
# 调度器机器可读的"我做完了"，chat.py 的终态门禁本来就按它判
# （那道门是否定向的：闭环账不干净就驳回这句声明）。两道合起来才完整 ——
# 一道管"没做完不许说完成"，一道管"说了完成就得把东西交出来"。
#
# ⚠️ 非 continuous 的会话没有这个标记，因而拿不到清单。这是**已知边界**，
# 不是漏判：那种会话里"做完了"只是一句自然语言，机械地认它就要去正则匹配
# 中文措辞 —— 那正是[[框架在制造症状]]反过来的一半（模型能判断的事别硬编码）。
# 收尾模板仍在 prompt 里对两种模式都生效，只是少了这道机械送达。

_CLOSING_COMPLETE_RE = __import__("re").compile(
    r"(?im)^\s*(?:[-*]\s*)?CONTINUOUS_STATUS\s*:\s*complete\b"
)


def _outgoing_text(ctx: HookContext) -> str:
    """本轮**即将交出去**的正文。

    `agent_loop` 在跑收尾闸之前已经把这条 assistant 消息 append 进去了，
    所以取最后一条 assistant 即可 —— 不能取 `ctx.messages[-1]`：hook 之间
    会互相追加 system 消息。
    """
    for message in reversed(ctx.messages):
        if getattr(message, "role", "") == "assistant":
            return getattr(message, "content", "") or ""
    return ""


def _closing_manifest_before_finish(ctx: HookContext) -> list[LLMMessage] | None:
    from core import closing_manifest as _manifest

    text = _outgoing_text(ctx)
    if not _CLOSING_COMPLETE_RE.search(text):
        return None
    deliverables = _manifest.frozen_deliverables(getattr(ctx.state, "project_worktree", None))
    if not deliverables:
        # 宣称完成却一件冻结产物都没有，是另一个问题（v28 实测：两审 approve、
        # run 报 completed、manuscript 根本没冻）。那归收尾闭环账管
        # （`core/closure.py` + 冻结义务），不在这里补第二道判决 ——
        # 同一件事两个地方判，迟早分叉。
        return None
    missing = _manifest.unpresented(deliverables, text)
    if not missing:
        return None
    ctx.state.append_transcript(
        "closing_manifest_delivered",
        n_deliverables=len(deliverables),
        n_missing=len(missing),
        artifact_ids=[d.artifact_id for d in missing][:_manifest.MAX_LISTED],
    )
    return [LLMMessage(role="system", content=_manifest.render(missing, total=len(deliverables)))]


def _presentation_gate_before_finish(ctx: HookContext) -> list[LLMMessage] | None:
    """摆进对话的路径必须在研究记录里 —— 只对锚在 scratch 的节点（调度器）判。

    2026-09-09 现场：论文 PDF 被编译在调度器的私人目录里，用户找不到。私人
    目录已经取消（它写 notes/、草稿落 run 目录），这道闸守的是剩下那半句：
    草稿区里的东西不许直接摆给用户。判据机械：正文里 `[..](path)` /
    `![..](path)` 的 target 落在 `.research/` 下就递回去，直到它入档。
    """
    from core import closing_manifest as _manifest
    from core.project_workspace import _SCRATCH_ANCHORED

    if str(getattr(ctx.state, "node_type", "") or "") not in _SCRATCH_ANCHORED:
        return None
    if getattr(ctx.state, "project_worktree", None) is None:
        return None
    targets = _manifest.unpresentable_targets(_outgoing_text(ctx))
    if not targets:
        return None
    ctx.state.append_transcript("presentation_gate_held", targets=targets[:20])
    return [LLMMessage(role="system", content=_manifest.render_unpresentable(targets))]


register_loop_hook(LoopHook(
    name="presentation_gate",
    description=(
        "呈现即入档：调度器把 run 运行时目录（scratch）里的路径摆进正文时，"
        "把话递回去让它先入档（notes/ 或 save_artifact）。用户点不开草稿区，"
        "而且它随 run 消失。"
    ),
    on_before_finish=_presentation_gate_before_finish,
    emits=("presentation_gate_held",),
))


register_loop_hook(LoopHook(
    name="closing_manifest",
    description=(
        "宣称完成时，把**扫盘现算**的冻结交付物清单（含 PDF / 图这些伴随文件的"
        "工作区相对路径）递到调度器手上，并指出哪几件它刚才没摆给用户。"
        "只递不拦：摆哪几件、怎么写全归模型。触发条件是它自己写下的 "
        "`CONTINUOUS_STATUS: complete` —— 判据放宽就会在研究中段把"
        "「只放一次」的收尾闸烧掉。"
    ),
    on_before_finish=_closing_manifest_before_finish,
    emits=("closing_manifest_delivered",),
))


# ── 做完了就交给组织（2026-09-24，RFC_ORGANISATION_PAGE C 批）─────────────────
#
# 晋升的判据（到终态了吗 / 证据冻了吗 / 知识卡写得出来吗）全是机械的，唯一入口
# `kb_promotion.offer_to_the_organisation`。它从前只挂在 curator 的 dreaming 上 ——
# 而 dreaming 由调度器**决定要不要跑**：跑不跑、什么时候跑是模型的事，于是一个做完
# 了的项目，它的死路和结论进不进组织，取决于模型那一轮想没想起来。[[框架在制造症状]]：
# 框架能机械答的别交给模型。
#
# 触发与 closing_manifest 同一个判据（它自己写下的 `CONTINUOUS_STATUS: complete`），
# 理由也一样。终态闸在函数里面（`check_terminal_batch` 读冻结事实），所以说早了
# 什么都不会发生；说了好几次也只交一次（出处判重）。
#
# 只交不拦、不回话：交出去的是组织的事（机械车道直落，人批车道等管理员），不该
# 让调度器为它再多跑一轮。出了错也只留痕 —— 收尾不能被组织知识卡住。


def _offer_to_the_organisation_before_finish(ctx: HookContext) -> list[LLMMessage] | None:
    if not _CLOSING_COMPLETE_RE.search(_outgoing_text(ctx)):
        return None
    project_id = str(getattr(ctx.state, "project_id", "") or "")
    if not project_id:
        return None
    from datetime import datetime, timezone

    from core import kb_promotion

    try:
        out = kb_promotion.offer_to_the_organisation(
            ctx.state, project_id=project_id,
            at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:  # noqa: BLE001 —— 收尾不能被它卡住，留痕即可
        ctx.state.append_transcript("organisation_offer_failed",
                                    error=f"{type(exc).__name__}: {exc}"[:500])
        return None
    if out["terminal"]:
        ctx.state.append_transcript(
            "organisation_offered",
            landed=[x["source_id"] for x in out["landed"]][:50],
            queued=[x["proposal_id"] for x in out["queued"]][:50],
            already=len(out["already"]),
            blocked=[b["source_id"] for b in out["blocked"]][:50],
            failed=out["failed"][:20],
        )
    return None


register_loop_hook(LoopHook(
    name="offer_to_the_organisation",
    description=(
        "宣称完成（`CONTINUOUS_STATUS: complete`）时跑一次终态晋升："
        "书目与死路直落组织知识，验证结论与方法配方进组织的待审（管理员裁）。"
        "终态判据读冻结事实，说早了什么都不发生；重复宣称只交一次。只交不拦、不回话。"
    ),
    on_before_finish=_offer_to_the_organisation_before_finish,
    emits=("organisation_offered", "organisation_offer_failed"),
))


# ── 说了只读就得对得上（#973）──────────────────────────────────────────────


def _read_only_promise_before_finish(ctx: HookContext) -> list[LLMMessage] | None:
    """用户要求只读、而本次真改了用户的文件 —— 在写最终陈述**之前**把事实递回去。

    2026-09 现场：用户明说「不要修改文件」，实际写了 24 个 Project 文件，
    而最终回复和独立审稿**双双称「全程只读」**，审稿还给了 0.92 通过。

    事实一直在盘上（每次改动都有 `workspace_changed` 事件），缺的是没人把它和
    那句承诺放在一起、并且**在模型下笔的那一刻**送到它手里
    （[[project_mechanical_bookkeeping_to_the_framework]]）。

    这里只递事实、不拦：矛盾了要怎么说是模型的事，但它**不可能再声称自己没改**
    —— 这句话就在它上一条消息里。口径见 `core.side_effect_contract`：
    只算用户带进来的文件，框架自己的 `.research/` 记账不算。
    """
    from core.side_effect_contract import reconcile

    result = reconcile(ctx.state)
    if not result.contradiction:
        return None
    try:
        ctx.state.append_transcript(
            "read_only_promise_contradicted",
            user_files_changed=len(result.user_paths),
            paths=result.user_paths[:50],
        )
    except Exception:
        pass
    return [LLMMessage(role="user", content=(
        f"{result.sentence()}\n\n"
        "写最终陈述之前必须处理这一条：**不要说「未修改文件」**。"
        "如实说明改了哪些、为什么改，以及要不要撤回。"
        "如果这些改动是必要的，就说清楚必要在哪；如果是失误，就说是失误。"
    ))]


register_loop_hook(LoopHook(
    name="read_only_promise",
    description=(
        "用户要求只读、而本次真改了用户的文件时，在写最终陈述前把事实递回去。"
        "只递事实不拦 —— 但模型不可能再声称自己没改。"
    ),
    on_before_finish=_read_only_promise_before_finish,
    emits=("read_only_promise_contradicted",),
))

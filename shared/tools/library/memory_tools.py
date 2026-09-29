"""记忆工具面 —— 四个。

    memory_write     宪法 / 叙事（节级所有制 + 引文防伪）
    memory_note      手册入册（零门禁 + 机械约束）
    memory_maintain  维护（合并 / 退休 / 呈现矛盾）—— 不是准入
    memory_recall    主动检索（补充，不是任何一层的送达依赖）

上一代是 14 个，其中 3 个已从模型面撤下却仍被 prompt 点名（幽灵工具），
1 个在平台模式下是空操作还回报 success。工具数不是问题本身，问题是
**每个工具对应的语义没想清楚** —— 现在四个各对一层。
"""
from __future__ import annotations

import logging
from typing import Any

from core import memory as M
from core.tool_registry import ToolDefinition, register_tool

log = logging.getLogger(__name__)

#: 引文核对时忽略的空白差异 —— 用户复述时空格/换行必然不同，
#: 但**内容**必须逐字。
def _norm_quote(text: str) -> str:
    return "".join((text or "").split())


def _err(code: str, msg: str, **extra: Any) -> dict:
    return {"status": "error", "code": code, "error": msg, **extra}


# ── 1. memory_write：宪法 / 叙事 ────────────────────────────────────────────


async def _memory_write(state: Any, section: str, text: str = "",
                        derived_from: list | None = None,
                        retire: str | None = None, **_: Any) -> dict:
    # section 的合法值只在 schema enum 里声明（手册节不在其中——整节覆写会把别人
    # 写的教训一次抹掉，去处见 description）；派发口按 schema 核，这里不再手写。
    section = str(section or "").strip()

    said = [str(u) for u in (state.hook_state.get("_user_utterances") or [])]

    # ── 铁律：模型抽象正文，框架核验出处 ──────────────────────────────────
    if section == M.SECTION_LAW:
        if retire:
            if not _user_asked_to_drop(said, retire):
                return _err(
                    "only_user_can_retire_a_law",
                    "撤铁律得用户说了才算 —— 本 session 的用户消息里没有"
                    "要撤这条的意思。立法与废法都归用户。")
            return M.retire_law(state, str(retire))

        srcs = [str(s) for s in (derived_from or []) if str(s).strip()]
        if not srcs:
            return _err(
                "derived_from_required",
                "立铁律必须给 derived_from —— 列出用户**说过的原话片段**"
                "（可以有好几条，跨轮的也算）。正文由你抽象成干练可判定的规矩，"
                "但它得有出处：没有出处的正文等于你凭空立法。")
        missing = [s for s in srcs
                   if not any(_norm_quote(s) and _norm_quote(s) in _norm_quote(u)
                              for u in said)]
        if missing:
            return _err(
                "derived_from_not_found",
                "这些片段在本 session 的用户消息里找不到 —— 出处必须是"
                "用户真说过的话，不能是你的转述或概括。"
                "（正文可以也应该是你抽象的；核验的是出处，不是正文。）",
                not_found=[s[:80] for s in missing])
        try:
            res = M.append_law(state, text=str(text or ""), derived_from=srcs)
        except M.MemoryError_ as e:
            return _err("refused", str(e))
        state.append_transcript("law_written", n_sources=len(srcs),
                                created=res.get("created"))
        if res.get("created"):
            res["next_step"] = (
                "**告诉用户你立了这条**，并附上依据的原话 —— 他没批准过这次"
                "抽象，看得见才改得动。他若说不对，用 "
                "memory_write(section='law', retire=<正文前缀>) 撤掉。")
        return res

    # ── 研究目标：用户的表述，整节覆写 ────────────────────────────────────
    if section == M.SECTION_GOAL:
        srcs = [str(s) for s in (derived_from or []) if str(s).strip()]
        if not srcs:
            return _err(
                "derived_from_required",
                "写研究目标必须给 derived_from（用户说过的原话片段）—— "
                "课题是什么只有用户说了算。")
        missing = [s for s in srcs
                   if not any(_norm_quote(s) and _norm_quote(s) in _norm_quote(u)
                              for u in said)]
        if missing:
            return _err("derived_from_not_found",
                        "这些片段在本 session 的用户消息里找不到。",
                        not_found=[s[:80] for s in missing])

    owner = M.SECTION_OWNER[section]
    if owner not in ("user", "*") and state.node_type != owner:
        return _err(
            "not_the_owner",
            f"「{M.SECTION_TITLE[section]}」的写者是 {owner}，"
            f"你是 {state.node_type}。单写者是为了让这一节只有一份真相。")

    try:
        res = M.write_section(state, section, str(text or ""))
    except M.MemoryError_ as e:
        return _err("refused", str(e))
    state.append_transcript("memory_section_written", section=section,
                            bytes=res["bytes"], by=state.node_type)
    return {"status": "success", "section": section, "bytes": res["bytes"],
            "note": "已落盘（覆写语义：一块板子，不是一本日志）。"}


#: 用户表达"撤掉/不要这条"的词面信号。刻意宽松 —— 判错的代价是少撤一条，
#: 用户再说一次就好；而**多撤**一条是把他立的规矩弄丢了。
_DROP_WORDS = ("撤", "删", "去掉", "不要了", "取消", "作废", "别再",
               "drop", "remove", "retire", "revoke")


def _user_asked_to_drop(said: list[str], target: str) -> bool:
    blob = _norm_quote(" ".join(said))
    return any(_norm_quote(w) in blob for w in _DROP_WORDS)


register_tool(
    ToolDefinition(
        name="memory_write",
        description=(
            "写 MEMORY.md 的**非手册**节：研究目标 / 科研铁律 / 叙事。\n\n"
            "## `law`（科研铁律）—— 抽象是你的活，出处是你的凭据\n\n"
            "用户很少会说「这条你要长期遵守」。更常见的是：说得零散、口语、"
            "跨好几轮，**同一件事反复强调**。把它变成一条能在 review 时逐条"
            "回答的规矩，需要你去抽象、去写得干练明确 —— 那正是该你做的事。\n\n"
            "**什么时候立**：\n"
            "  · 用户明说「以后都要 / 永远不准 / 定几条规矩」\n"
            "  · 或者他**第二、三次**为同一件事纠正你 —— 那就是一条规矩，"
            "只是他没用规矩的语气说。别等他明说。\n\n"
            "**怎么写正文**：短、准、**可判定**。它会被机械展开成 review 时"
            "必须逐条回答的检查项 —— 答不出「遵守/违反」的措辞就是废的。\n"
            "  ❌「永远架构级思考，不准打补丁」（判不了）\n"
            "  ✅「任何修复必须回答：改的是产生问题的那一层还是症状层？"
            "修完同类待办会不会自己消失？」\n\n"
            "**`derived_from` 必填**：用户说过的**原话片段**，可以有好几条、"
            "跨轮的也算。框架核验它们确实出现在本 session 的用户消息里。\n"
            "  核验的是**出处**，不是正文 —— 正文该是你抽象的。\n"
            "  没有出处的正文 = 你凭空立法；有出处但正文照抄 = 没做抽象。\n\n"
            "**立完要告诉用户**：附上你依据的原话。他没批准过这次抽象，"
            "看得见才改得动。抽错了他会说，用 `retire=<正文前缀>` 撤 ——"
            "撤也只有用户说了才算。\n\n"
            "## `goal`（研究目标）\n"
            "课题是什么只有用户说了算，同样必须给 `derived_from`。"
            "整节覆写。\n\n"
            "## `narrative`（叙事）\n"
            "只有调度器能写，**覆写**语义（一块板子，不是一本日志）。只写盘面"
            "推不出来的东西：为什么走这条路、放弃了什么、下一步。什么冻了 / "
            "兑现几条 / 有多少证据由框架现算，不要手写 —— 手写的现状必然漂移。\n\n"
            "坑与做法用 `memory_note`，不走这里。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "section": {"type": "string",
                            "enum": [s for s in M.SECTIONS
                                     if s not in M.MANUAL_SECTIONS],
                            "description": ("goal / law / narrative。手册节"
                                            "（manual_*）不在此列：用 memory_note "
                                            "逐条追加，整节覆写会抹掉别人写的教训")},
                "text": {"type": "string",
                         "description": "law: 你抽象出的一条规矩（追加）；"
                                        "goal/narrative: 该节完整新内容（覆写）"},
                "derived_from": {
                    "type": "array", "items": {"type": "string"},
                    "description": "law/goal 必填：用户说过的原话片段（可多条、"
                                   "可跨轮）。框架核验它们真出自本 session"},
                "retire": {"type": "string",
                           "description": "law: 撤一条铁律，给正文前缀"},
            },
            "required": ["section"],
        },
        risk_level="medium",
    ),
    _memory_write,
)


# ── 2. memory_note：手册入册 ────────────────────────────────────────────────


async def _memory_note(state: Any, text: str, category: str = "pitfall",
                       tools: list | None = None, nodes: list | None = None,
                       **_: Any) -> dict:
    section = (M.SECTION_METHOD if str(category) in ("method", "workflow", "workflow_hint")
               else M.SECTION_PITFALL)
    try:
        res = M.append_manual(
            state, text=str(text or ""), section=section,
            tools=[str(t) for t in (tools or [])],
            nodes=[str(n) for n in (nodes or [])] or [state.node_type],
            run_id=str(getattr(state, "run_id", "") or ""),
        )
    except M.MemoryError_ as e:
        return _err("refused", str(e))
    state.append_transcript("memory_note", section=section,
                            created=res.get("created"), by=state.node_type)
    return res


register_tool(
    ToolDefinition(
        name="memory_note",
        description=(
            "把一条**可复用的教训**写进本项目手册。**写下来就是入册** ——"
            "没有候选队列、没有人审核、不用等 curator 加工。\n\n"
            "**Use when**：踩了一个下次还会踩的坑；发现一个该复用的做法。\n"
            "**Do NOT use when**：\n"
            "  · 这次任务的进度/结论 → 那是产物，用 save_artifact\n"
            "  · 带真值的科学断言 → 那是 KB，用 create_claim\n"
            "  · 用户立的规矩 → 那是宪法，用 memory_write\n\n"
            "**`nodes` / `tools`（适用面，至少给一个）**：这条教训在**谁开工时**、"
            "**调哪个工具前**该被弹出来。它同时是遗忘判据 —— 适用面失效即可机械"
            "退休。写不出适用面，说明这条还没想清楚在什么情况下成立。\n\n"
            "**近似重复不会新增条目**，只给已有条目的复发计数 +1；复发到阈值会"
            "标成系统性 defect 并劝你停止记录、去修根因 —— 同一个问题记第五遍"
            "不产生任何价值。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "教训正文。带条件与后果，别写成待办"},
                "category": {"type": "string", "enum": ["pitfall", "method"],
                             "description": "坑（默认）/ 方法"},
                "nodes": {"type": "array", "items": {"type": "string"},
                          "description": "适用于哪些节点。不填默认本节点"},
                "tools": {"type": "array", "items": {"type": "string"},
                          "description": "适用于哪些工具（首次调用时会附在结果上）"},
            },
            "required": ["text"],
        },
        risk_level="low",
    ),
    _memory_note,
)


# ── 3. memory_maintain：维护 ────────────────────────────────────────────────


async def _memory_maintain(state: Any, action: str,
                           entries: list | None = None,
                           text: str | None = None, **_: Any) -> dict:
    from core.memory_forget import (
        merge_entries, retire_entries, scan_for_forgetting,
    )

    action = str(action or "").strip()
    if action == "scan":
        return scan_for_forgetting(state)
    if action == "retire":
        return retire_entries(state, [str(x) for x in (entries or [])])
    if action == "merge":
        return merge_entries(state, [str(x) for x in (entries or [])],
                             str(text or ""))
    return _err("unknown_action", f"未知 action {action!r}",
                allowed=["scan", "retire", "merge"])


register_tool(
    ToolDefinition(
        name="memory_maintain",
        description=(
            "维护本项目手册。**维护不是准入** —— 条目进来不需要你批准，"
            "你的活是让它别烂掉。\n\n"
            "  · `scan`：跑三项机械作业，返回**提议**（不直接改）——\n"
            "      引用失效（条目点名的工具/节点已不存在）、\n"
            "      触发衰减（适用面久未命中）、\n"
            "      矛盾（两条对着干）。**矛盾只呈现不裁决**：哪条对要靠新证据，"
            "      不靠谁写得晚。\n"
            "  · `merge`：把几条合成一条（`entries` 给正文前缀，`text` 给新正文）\n"
            "  · `retire`：退休几条（`entries` 给正文前缀）\n\n"
            "销毁类动作代价不对称，所以 scan 只留证据不判决；真正删之前"
            "先把清单给人看。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["scan", "merge", "retire"]},
                "entries": {"type": "array", "items": {"type": "string"},
                            "description": "目标条目的正文前缀（判据是唯一匹配，不是长度）"},
                "text": {"type": "string", "description": "merge 用：合并后的新正文"},
            },
            "required": ["action"],
        },
        risk_level="medium",
    ),
    _memory_maintain,
)


# ── 4. memory_recall：主动检索 ──────────────────────────────────────────────


async def _memory_recall(state: Any, query: str = "", **_: Any) -> dict:
    q = " ".join(str(query or "").lower().split())
    entries = M.manual_entries(state)
    if q:
        toks = [t for t in q.split() if t]
        entries = [e for e in entries
                   if any(t in e.text.lower() for t in toks)] or entries
    entries.sort(key=lambda e: (not e.defect, -e.seen))
    hits = entries[:20]
    # KB 经验桶（死路 / 已验证方法 / 承重结论 / 上次挂在哪些 QC）委托
    # core.recall —— 那是 KB 的召回逻辑，不该在记忆层再实现一遍。
    kb: dict = {}
    try:
        from core.recall import recall as _kb_recall

        if q:
            kb = _kb_recall(state, str(query), k_per_category=5).summary_counts()
            kb = {k: v for k, v in kb.items() if v}
    except Exception as e:
        log.debug("kb recall failed: %s", e)

    return {
        "status": "success",
        "goal": M.read_section(state, M.SECTION_GOAL),
        "law": M.read_section(state, M.SECTION_LAW),
        "narrative": M.read_section(state, M.SECTION_NARRATIVE),
        "manual": [e.as_dict() for e in hits],
        "manual_total": len(M.manual_entries(state)),
        "kb_experience_counts": kb,
        "note": ("手册按正文匹配返回，上限 20 条。开工时框架已按适用面"
                 "自动送过一批 —— 这里是补充深查，不是主路径。"),
    }


register_tool(
    ToolDefinition(
        name="memory_recall",
        description=(
            "主动查本项目记忆：目标、铁律、叙事、手册。\n\n"
            "同时回一份 KB 经验桶的计数（死路 / 已验证方法 / 承重结论）——"
            "要细节用 search_kb。\n\n"
            "**先看你已经收到的**：开工时框架已按适用面机械送过一批教训，"
            "宪法与局面每轮都在。这个工具是**补充深查** —— 决策前想确认"
            "「这个方向以前是不是撞过墙」时用。\n\n"
            "查不到不代表没发生过 —— 也可能是当时那条教训的适用面没写上你"
            "现在这个场景。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "关键词。留空 = 返回全部（按复发次数排序）"},
            },
        },
        replayable_read=True,
        risk_level="low",
    ),
    _memory_recall,
)

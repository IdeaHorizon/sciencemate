"""遗忘 = 可证伪性，不是 TTL。

## 为什么不是 TTL

上一代有 `expires_at` 字段，但它从来没有写入方 —— 而且思路本身是错的：
经验不按日历过期。一条 2026-05 写的「先跑 `preview_experiment_contract`」
在那个工具改名之后就**当天失效**了；一条「网格加密到 128 以下有限尺度效应
会吃掉临界指数」可能十年都对。日历分不出这两者。

分得出的是**适用面**。`applies_to` 让遗忘变成机械作业：

  1. 引用失效   条目点名的工具/节点已经不存在了 → 它指着一个不存在的世界
  2. 触发衰减   适用面久未被任何送达通道命中 → 它可能已经不适用了
  3. 矛盾       两条对着干 → **呈现，不裁决**

## 只留证据，不判决

三项作业全部只产出**提议**。销毁类动作的代价不对称（错删一条真教训 vs
多留一条废条目），本仓为此付过学费。所以机械层的输出是一张清单，
删不删由人在批次里定。

见 docs/RFC_MEMORY_REBUILD_20260821.md §5。
"""
from __future__ import annotations

import logging
from typing import Any

from core import memory as M

log = logging.getLogger(__name__)

#: 适用面连续多少个 run 没被命中就提议退休。
#: 取 run 数而不是天数：项目可能停摆三个月，那期间的"没命中"不说明任何事。
DECAY_RUNS = int(__import__("os").getenv("HARNESS_MEMORY_DECAY_RUNS", "20"))

#: 判定"讲的是同一件事"的措辞近似度下界。低于它说明两条在说不同的事，
#: 极性相反也不构成矛盾。上界是近似去重阈值（`M.NEAR_DUP_JACCARD`）——
#: 再高它们早该被合并成一条了。
#: 这个带是拿真数据定的：4-shingle 那版在 175 条手册上报出 166 对误报。
CONTRADICTION_MIN_JACCARD = float(
    __import__("os").getenv("HARNESS_MEMORY_CONTRADICTION_MIN_JACCARD", "0.45"))

#: 判定两条矛盾的词面信号。刻意保守 —— 真正的矛盾判断需要语义，
#: 机械层只负责把**可疑对**摆到人面前。
_NEGATORS = ("不要", "别", "禁止", "不能", "不应", "避免", "勿",
             "must not", "never", "avoid", "don't", "do not")


def _known_tools() -> set[str]:
    """当前注册表里有哪些工具。拿不到就返回空集 —— 空集会让引用失效扫描
    **一条都不报**，这比拿一份残缺名单去误杀真教训安全。"""
    try:
        from core.tool_registry import all_tool_names

        return set(all_tool_names())
    except Exception as e:
        log.debug("tool registry unavailable: %s", e)
        return set()


def _known_nodes() -> set[str]:
    """当前有哪些节点。同上：拿不到就不报。"""
    try:
        from core.loader import list_harnesses

        return set(list_harnesses())
    except Exception as e:
        log.debug("node list unavailable: %s", e)
        return set()


def scan_for_forgetting(state: Any) -> dict:
    """三项机械作业。**只产出提议**，不动盘。"""
    entries = M.manual_entries(state)
    tools, nodes = _known_tools(), _known_nodes()

    dangling: list[dict] = []
    if tools or nodes:
        for e in entries:
            gone_t = [t for t in e.tools if tools and t not in tools]
            gone_n = [n for n in e.nodes if nodes and n not in nodes]
            if gone_t or gone_n:
                dangling.append({
                    "text": e.text[:120], "section": e.section,
                    "missing_tools": gone_t, "missing_nodes": gone_n,
                    "why": ("这条教训点名的东西已经不存在了 —— 它指着一个"
                            "不存在的世界，会被无限期注入给读不懂它的节点。"),
                })

    stale: list[dict] = []
    try:
        current = int(state.hook_state.get("_run_ordinal") or 0)
    except Exception:
        current = 0
    if current > DECAY_RUNS:
        for e in entries:
            if e.seen <= 1 and not e.defect:
                stale.append({
                    "text": e.text[:120], "section": e.section,
                    "seen": e.seen,
                    "why": f"入册后适用面一直没再被命中（阈值 {DECAY_RUNS} 个 run）。",
                })

    contradictions: list[dict] = []
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if a.section != b.section:
                continue
            if not (set(a.tools) & set(b.tools) or set(a.nodes) & set(b.nodes)):
                continue
            na = any(w in a.text.lower() for w in _NEGATORS)
            nb = any(w in b.text.lower() for w in _NEGATORS)
            if na == nb:
                continue
            # 必须**讲的是同一件事**，只是极性相反。
            #
            # 早先的判据是「共享 ≥4 个 shingle」—— 在真数据上直接崩了：
            # 175 条手册报出 166 对"矛盾"。中文 bigram 里同领域两句话轻易
            # 共享十几个字对，于是"同节点 + 一句带否定词"几乎恒真。
            # 而这条作业的产出是给人看的清单：**误报会让人学会忽略红旗**，
            # 那比没有红旗更糟（dead_end 红旗踩过同一个坑）。
            #
            # 正确判据是措辞近似度落在一个**带**里：高到说明在讲同一件事，
            # 又低于近似去重阈值（否则它们早该被合并成一条）。
            sa = M._shingles(M._norm(a.text))
            sb = M._shingles(M._norm(b.text))
            if not sa or not sb:
                continue
            jac = len(sa & sb) / len(sa | sb)
            if not (CONTRADICTION_MIN_JACCARD <= jac < M.NEAR_DUP_JACCARD):
                continue
            contradictions.append({
                "a": a.text[:120], "b": b.text[:120],
                "shared_scope": sorted(set(a.tools) & set(b.tools)
                                       | set(a.nodes) & set(b.nodes)),
                "why": ("同一适用面下，一条在劝做、一条在劝别做。"
                        "**这里不裁决** —— 哪条对要靠新证据，不靠谁写得晚。"),
            })

    return {
        "status": "success",
        "total_entries": len(entries),
        "dangling_reference": dangling,
        "decayed": stale,
        "contradictions": contradictions,
        "note": ("这三张单子是**提议**，盘上什么都没改。要落地用 "
                 "memory_maintain(action='retire'|'merge')。"
                 "错删一条真教训比多留一条废条目贵得多 —— 所以机械层"
                 "只留证据，判决归人。"),
    }


#: 定位手册条目的判据：**唯一匹配**，与 `memory.retire_law` 同一把尺。
#:
#: 从前这里是 `len(_norm(p)) < 6` 静默丢弃短前缀 —— 长度阈值是个 Latin 中心的
#: 坏判据（中文五个字往往已经唯一，英文二十个字符可能还匹配三条），而且
#: 「静默丢弃」让调用方以为退休了其实没有。真正要防的是「一个前缀动了好几条」，
#: 那就直接判它：命中多条 → 一条都不动、把 matches 摆出来让调用方缩小。
def _resolve(entries: list[M.ManualEntry], prefixes: list[str]
             ) -> tuple[list[M.ManualEntry], list[str], list[dict]]:
    """把每个前缀解析成**恰好一条**条目。返回 (命中, 未命中前缀, 歧义前缀)。"""
    hits: list[M.ManualEntry] = []
    missed: list[str] = []
    ambiguous: list[dict] = []
    for raw in prefixes:
        p = M._norm(raw)
        found = [e for e in entries if p and M._norm(e.text).startswith(p)]
        if len(found) == 1:
            if found[0] not in hits:
                hits.append(found[0])
        elif not found:
            missed.append(raw[:60])
        else:
            ambiguous.append({"prefix": raw[:60],
                              "matches": [e.text[:60] for e in found]})
    return hits, missed, ambiguous


def retire_entries(state: Any, prefixes: list[str]) -> dict:
    """退休若干条 —— 从手册里移除。账本层面它们仍在 Git 历史里。

    每个前缀必须**唯一**指向一条；歧义的前缀一条都不动、把候选列出来
    （`ambiguous`），没命中的进 `not_found` —— 静默失败会让人以为删掉了。
    """
    entries = M.manual_entries(state)
    targets, missed, ambiguous = _resolve(entries, prefixes)
    removed: list[str] = []
    for section in M.MANUAL_SECTIONS:
        section_entries = [e for e in entries if e.section == section]
        keep = [e for e in section_entries if e not in targets]
        if len(keep) != len(section_entries):
            removed.extend(e.text[:80] for e in section_entries if e in targets)
            M.replace_manual(state, section, keep)
    return {"status": "success", "retired": removed, "not_found": missed,
            "ambiguous": ambiguous,
            "note": "退休 = 不再被送达。Git 历史里仍查得到，遗忘不是销毁。"
                    + ("歧义前缀一条都没动 —— 给更长的前缀，撤错一条比撤不掉贵得多。"
                       if ambiguous else "")}


def merge_entries(state: Any, prefixes: list[str], text: str) -> dict:
    """把几条合成一条：新条目继承并集适用面与最大复发计数。

    每个前缀唯一指向一条（判据同 `retire_entries`）；同一节里凑不齐 2 条
    就没有合并的对象 —— 那会变成静默改写单条。
    """
    text = (text or "").strip()
    entries = M.manual_entries(state)
    targets, missed, ambiguous = _resolve(entries, prefixes)
    for section in M.MANUAL_SECTIONS:
        hits = [e for e in targets if e.section == section]
        if len(hits) < 2:
            continue
        merged = M.ManualEntry(
            text=text, section=section,
            tools=tuple(dict.fromkeys(t for e in hits for t in e.tools)),
            nodes=tuple(dict.fromkeys(n for e in hits for n in e.nodes)),
            run_id=next((e.run_id for e in hits if e.run_id), ""),
            commit=next((e.commit for e in hits if e.commit), ""),
            seen=max(e.seen for e in hits),
            last_seen=max((e.last_seen for e in hits), default=""),
            defect=any(e.defect for e in hits),
        )
        keep = [e for e in entries if e.section == section and e not in hits] + [merged]
        M.replace_manual(state, section, keep)
        return {"status": "success", "merged_count": len(hits),
                "section": section, "entry": merged.as_dict()}
    return {"status": "error", "code": "need_two_matches",
            "error": "同一节里至少要唯一命中 2 条才谈得上合并",
            "hint": "每个前缀要唯一指向一条已有条目（判据是唯一匹配，不是长度）；"
                    "命中多条的前缀见 ambiguous，没命中的见 not_found",
            "not_found": missed, "ambiguous": ambiguous}

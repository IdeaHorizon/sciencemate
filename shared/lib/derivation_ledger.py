"""推导验证账本：本 run 真调过哪些验证、结论是什么。

## 为什么需要它

冻结门第一版的判据是 `verification.tool in TRUSTED_VERIFIERS` —— **只查名字**。
模型只要在 metadata 里写一句 `{"status": "verified", "tool": "check_step"}`
就能过，一次工具也不用调。那道闸挡的是"随手编个工具名"，挡不住"照着合法
格式编一个章"。

**报告不是事实** —— 判据必须落在"这件事真发生过"上，不是"它自称发生过"。

## 机制：式子指纹 + 只增账本

验证工具每次返回，做两件事：

  1. 在 verification 块里放一个 `probe` —— 被验式子的指纹
     （`sha256(lhs | relation | rhs)` 前 16 位）
  2. 往 transcript 写一条同 `probe` 的 `derivation_check` 事件

冻结时，声称 verified / numerically_supported 的每一步，它的 `probe` 必须
在本 run 的账本里找得到，且账本里的结论与它自称的一致。

伪造的三条路各自堵死：

  · 不写 probe        → 拦（报错让它把工具返回的整块原样贴进来）
  · 编一个 probe      → 账本里没有 → 拦
  · 抄别处的真 probe  → 账本里有，但那条记录的式子跟这一步的 claim 无关
                        → 机械层拦不住，**留给 reviewer**（review_spec 第 1 维
                        要求抽验承重步骤、自己重跑 check_step，重跑就现形）

第三条是本层的边界，如实记在 `nodes/derivation/tests/test_planted_error_detection.py`。

## 一处实现，两处消费

冻结门核账、白板渲染进度，读的是同一份账本 —— 抄两份就会各自演化，
而分叉时两边都不报错。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: transcript 里的事件名。工具写它，冻结门与白板读它。
EVENT = "derivation_check"

#: 逐行过滤用的子串（先粗筛再 json.loads）。
_NEEDLE = f'"{EVENT}"'

# 这里曾有 `_SCAN_LIMIT = 20000`：只看 transcript 末尾两万行（#985）。理由是
# "长推导的 transcript 可以很长"—— 但那份实现先 `read_text().splitlines()` 把
# **整份**读进内存，再丢掉前面的部分：省下的只有 json.loads，内存一分没省，代价
# 却是长 run 早期的验证从账本里消失，冻结门把它们判成"查无此项"（伪造），而
# 那几步是真调过工具的。现在逐行流式读全文件、只留本事件的行：内存上界是
# 验证记录的条数，不是 transcript 的长度。


def probe_id(lhs: str, rhs: str, relation: str = "eq") -> str:
    """被验式子的指纹。

    规范化只做 strip —— **不做 sympy 级的规范化**：两个数学上等价但写法
    不同的式子，指纹本就该不同。这个指纹回答的是"你验的是不是**这一行**"，
    不是"你验的是不是这个数学事实"。
    """
    raw = f"{(lhs or '').strip()}|{(relation or 'eq').strip()}|{(rhs or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def record(state: Any, *, probe: str, tool: str, status: str,
           lhs: str = "", rhs: str = "", relation: str = "eq",
           assumptions: dict | None = None) -> None:
    """把一次验证写进账本。state 缺失（单测直调工具）时静默跳过。"""
    append = getattr(state, "append_transcript", None)
    if append is None:
        return
    try:
        # lhs/rhs 截 400 字只影响**展示**（白板再截到 60–70 字）：身份是 probe，
        # 它在调用方按完整式子算好了，账本里的式子文本不参与任何判据（#985）。
        append(
            EVENT, probe=probe, tool=tool, status=status,
            lhs=(lhs or "")[:400], rhs=(rhs or "")[:400], relation=relation,
            assumptions=dict(assumptions or {}),
        )
    except Exception:
        # 记账失败不该让验证本身失败 —— 但它会让这一步在冻结时被判"没真调过"，
        # 而那正是安全的方向（fail-closed 到"未验"，不是 fail-open 到"已验"）。
        pass


def _scan(state: Any) -> list[dict] | None:
    """读一遍本 run 的账本。**None = 读不到；[] = 读到了、一条也没有。**

    两者必须分开（见 `entries`）：此前读盘失败返回 `[]`，而 `ledger_is_readable`
    只看文件在不在 —— 文件在、读失败时两边合起来说"账本可读且是空的"，冻结门
    就把每一个真章都判成伪造。
    """
    path = getattr(state, "transcript_path", None)
    if path is None or not getattr(path, "exists", lambda: False)():
        return None
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if _NEEDLE not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue      # 写到一半的末行 / 坏行：跳过这一行，不是整本读不到
                if isinstance(rec, dict) and rec.get("event") == EVENT and rec.get("probe"):
                    out.append(rec)
    except OSError:
        return None
    return out


def entries(state: Any) -> list[dict]:
    """本 run 账本里的全部验证记录（旧→新）。读不到就返回空表。

    读不到 ≠ 没验过：非 Project 独立运行、transcript 还没落盘时都可能读不到。
    **调用方必须把"账本读不到"与"账本里没有这一条"区别对待** ——
    前者不该拦（拦了就是把一次读盘失败变成"这个节点交不了差"），
    后者才是伪造。两件事都要的调用方用 `snapshot`（一次读出两个答案）。
    """
    return _scan(state) or []


def _table(records: list[dict]) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for rec in records:
        table[str(rec["probe"])] = rec
    return table


def by_probe(state: Any) -> dict[str, dict]:
    """probe → **最后一次**该式子的验证记录。

    取最后一次是有意的：同一个式子可以在补了假设之后重验
    （`sqrt(x**2) = x` 加上 x>0 就从 failed 变 verified），
    后一次才是当前结论。
    """
    return _table(entries(state))


def snapshot(state: Any) -> tuple[dict[str, dict], bool]:
    """`(by_probe 表, 账本可读)` —— **同一次读盘**给出的两个答案。

    冻结门两样都要。分两次调（`by_probe` + `ledger_is_readable`）就是同一个事实
    两个采样时刻：中间文件状态一变，两个答案就各说各的。
    """
    records = _scan(state)
    return _table(records or []), records is not None


def ledger_is_readable(state: Any) -> bool:
    """账本这个事实来源本身在不在、读不读得出来。

    区分"账本空"与"账本读不到"：前者是真的一次没验过（该拦），
    后者是取证手段失灵（不该拦）。文件在但读失败也算读不到（#985）。
    """
    return _scan(state) is not None

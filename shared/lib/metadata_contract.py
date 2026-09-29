"""metadata 契约的判据原语 —— 「欠不欠」这个问题的唯一实现。

## 为什么是一个模块而不是各写各的

2026-08-23 清点：同一个问题「这个 metadata 字段算不算填了」在仓库里有**三份
实现，两个互相矛盾的答案**：

  · `shared/tools/builtin.py`         `not _md.get(k)`  → `0` / `False` 判为缺
  · `nodes/observation/.../_is_filled`                  → `0` / `False` 是合法取值
  · `nodes/derivation/.../_is_filled`                   → 同上（逐字抄的另一份）

两个答案分歧的那个值不是假想的：真跑产物里就有
`adversarial_search.n_contradicting_found: 0`（找过反证、一条都没找到）。
observation 契约里写得很清楚：`n_excluded: 0` 是**合法的 0**，把它当"没填"
就是逼模型编一个非零数字出来 —— 闸反过来制造它要防的行为。

有几份抄件就有几个会各自演化的答案，而且分叉时两边都不报错。

## 两档判据，是语义之别不是宽严之别

    required（查值）      必须**有内容**。缺了说明这一趟没做这件事。
    may_be_empty（查 key）必须**在场**，允许为空。

    · `assumptions: []` —— 纯代数恒等式的推导确实不需要额外假设
    · `n_excluded: 0`  —— 真的什么都没排除时，那就是合法的 0

把空值当"没填"就是逼模型编一条出来。但 key 本身必须在场：key 都不写，
说明这一趟压根没做这件事。

两档**怎么组合**归各类型的契约模块（它们才知道哪档取决于 `mode`）；这里只
提供两个判据原语，不替它们决定。
"""
from __future__ import annotations

from typing import Any


def dig(metadata: Any, path: str) -> Any:
    """按 `a.b.c` 取值，中途不是 dict 就返回 None。"""
    cursor: Any = metadata
    for part in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def is_filled(value: Any) -> bool:
    """有内容？None/""/[]/{} 算缺；**`0` 与 `False` 是合法取值**。"""
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple, set)):
        return len(value) > 0
    return True

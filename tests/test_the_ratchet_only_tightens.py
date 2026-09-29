"""棘轮的基线只能降 —— 闸开的药方不该把闸的另一半松掉（#912）。

## 缺陷

`tests/test_refusal_sites_are_classified.py` 有两半：

* **涨**：`n - baseline[f] > registered[f]` → 红。`registered[f]` 是 registry 里该
  文件的累计条目数，也就是「从基线起算的免声明额度」。
* **落**：`current[f] < baseline[f]` → 红，信息里写「跑 `--write` 更新基线」。

而 `--write` 曾经是**整份重生**：把所有文件的基线都抬到实测值，**包括那些"涨了、但
已被 registry 条目顶掉"的文件**。基线一抬，那些条目就从"已经付过账"变回"还没花的
额度"，同一批声明被重复计一次。

实测（#909 当时）：剩余免声明额度 **0 处 → 跑一次 `--write` 后 16 处**。而"静默新增
拒绝点"正是这道闸存在的唯一理由。

#909 就撞上了：拆了两处墙触发"落"那一半，照指示跑 `--write` 会顺手把
`operation_completion` 26→30、`file_digests` 0→3、`materials_extract` 0→5 一起吞进
基线。当时靠"我恰好看了一眼 diff"没被松掉 —— 那不是机制。

## 判据

`--write` 只做一件事：把基线往**实测值方向下调**。涨的不碰、新文件不写进去
（写进去 = 发一份不用声明的额度）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "_scan_refusal_sites_for_test", ROOT / "scripts" / "scan_refusal_sites.py")
scan_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan_mod)


def test_a_wall_that_was_deleted_lowers_the_baseline():
    """删墙 → 基线跟着降。这是 `--write` 的**全部**用途。"""
    out = scan_mod.tightened({"a.py": 5}, {"a.py": 3})
    assert out == {"a.py": 3}


def test_a_wall_that_was_added_does_not_raise_the_baseline():
    """加墙 → 基线纹丝不动。抬它 = 把已声明的 registry 条目退回成没花的额度。"""
    out = scan_mod.tightened({"a.py": 5}, {"a.py": 9})
    assert out == {"a.py": 5}


def test_a_file_that_reached_zero_leaves_the_baseline():
    """墙全拆光的文件整条删掉，别在基线里留一个 0 当额度。"""
    assert scan_mod.tightened({"a.py": 5}, {}) == {}
    assert scan_mod.tightened({"a.py": 5}, {"a.py": 0}) == {}


def test_a_brand_new_file_never_enters_the_baseline():
    """新文件不写进基线 —— 写进去就等于发一份不用声明的额度。"""
    out = scan_mod.tightened({"a.py": 5}, {"a.py": 5, "b.py": 4})
    assert out == {"a.py": 5}, "新文件被顺手吞进基线了"


def test_the_regression_from_909_cannot_happen_again():
    """#909 那张表逐条重放：跑一次 `--write` 之后，可静默新增的额度必须仍是 0。

    表里的数字是当时的实测值（基线 / 实测 / registry 条目数）。
    """
    baseline = {
        "nodes/data/data_agent_loop.py": 0,
        "nodes/data/tools/execute_preprocessing_plan.py": 5,
        "nodes/data/tools/preprocessing_capabilities.py": 16,
        "nodes/experiment/tools/operation_completion.py": 26,
        "shared/tools/library/file_digests.py": 0,
        "shared/tools/library/materials_extract.py": 0,
    }
    current = {
        "nodes/data/data_agent_loop.py": 1,
        "nodes/data/tools/execute_preprocessing_plan.py": 7,
        "nodes/data/tools/preprocessing_capabilities.py": 17,
        "nodes/experiment/tools/operation_completion.py": 30,
        "shared/tools/library/file_digests.py": 3,
        "shared/tools/library/materials_extract.py": 5,
    }
    registered = {
        "nodes/data/data_agent_loop.py": 1,
        "nodes/data/tools/execute_preprocessing_plan.py": 2,
        "nodes/data/tools/preprocessing_capabilities.py": 1,
        "nodes/experiment/tools/operation_completion.py": 4,
        "shared/tools/library/file_digests.py": 3,
        "shared/tools/library/materials_extract.py": 5,
    }
    after = scan_mod.tightened(baseline, current)
    slack = sum(
        max(0, after.get(f, 0) + registered.get(f, 0) - current.get(f, 0))
        for f in set(baseline) | set(current)
    )
    assert slack == 0, (
        f"跑一次 --write 之后凭空多出 {slack} 处可以静默新增的拒绝点 —— "
        "这正是 #912 的缺陷（当时实测 16 处）"
    )


def test_the_growth_report_names_the_files():
    """涨了的文件要被点名，人才知道该去 registry 声明哪几个。"""
    growth = scan_mod.grew({"a.py": 5}, {"a.py": 9, "b.py": 2})
    assert growth == {"a.py": (9, 5), "b.py": (2, 0)}

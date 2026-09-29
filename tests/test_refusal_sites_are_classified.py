"""防复发闸（判决拆除战役）：新增拒绝点必须分类登记，不许静默造墙。

规矩（docs/verdict_demolition/RFC_VERDICT_DEMOLITION.md §4）：
新增拒绝分支必须声明属 A（安全/资源）/ B（记录完整性）/ C（物理/协议）哪类；
声明不了的只能写成义务 collector。充分性判决（D）不再合法。

机械执行：per-file 拒绝点计数对基线做棘轮 ——
  计数升高 → 必须在 refusal_registry.yaml 给新增数量的分类条目；
  计数降低 → 在同一 PR 里跑 `scripts/scan_refusal_sites.py --write` 收紧基线。
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts.scan_refusal_sites import BASELINE, ROOT, scan

REGISTRY = ROOT / "docs" / "verdict_demolition" / "refusal_registry.yaml"
_LEGAL = {"A", "B", "C", "collector"}


def test_no_unclassified_new_refusal_sites():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = scan()
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    entries = registry.get("entries") or []
    for e in entries:
        assert e.get("class") in _LEGAL, f"registry 条目缺合法 class：{e}"
    registered: dict[str, int] = {}
    for e in entries:
        registered[e["file"]] = registered.get(e["file"], 0) + 1

    grown = {
        f: (n, baseline.get(f, 0))
        for f, n in current.items()
        if n > baseline.get(f, 0) and n - baseline.get(f, 0) > registered.get(f, 0)
    }
    assert not grown, (
        "新增了未分类登记的拒绝点（文件: 现值/基线）："
        f"{ {f: f'{n}/{b}' for f, (n, b) in grown.items()} }\n"
        "→ 在 docs/verdict_demolition/refusal_registry.yaml 逐条声明 "
        "class ∈ {A,B,C,collector}；声明不了 = 它是充分性判决，改写成义务。"
    )

    shrunk = {f: (current.get(f, 0), b) for f, b in baseline.items()
              if current.get(f, 0) < b}
    assert not shrunk, (
        f"拒绝点减少了但基线没跟着收紧：{ {f: f'{n}/{b}' for f, (n, b) in shrunk.items()} }\n"
        "→ 同一 PR 里跑 `scripts/scan_refusal_sites.py --write` 把基线降到现值。\n"
        "  它**只降不升**（#912）：涨了的文件不会被顺手吞进基线 —— 那会把已声明的\n"
        "  registry 条目退回成没花的额度，把闸的另一半松掉。\n"
        "\n"
        "⚠️ 这句话**不是**在说你做错了什么（#955 第三条）。删掉一堵墙是这个仓库\n"
        "想要的方向；棘轮只是要求「减少之后基线也要降下来」，否则下一个人重新加\n"
        "回来时这道闸不会响。旧文案写成 `删墙 ✅` 却仍然报红，读起来像在\n"
        "「批准又惩罚同一件事」，实测把人劝退过两次。"
    )

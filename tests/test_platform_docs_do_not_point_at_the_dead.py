"""platform/docs/ 里的相对链接必须指到真的存在的文件（#959）。

`TODO.md` 停在 2026-04-23、与现状严重不符，删掉了；它的 D1–D4 决策表迁进了
`design_decisions.md`。删的时候发现有两处文档还在指着它 —— 而**没有任何东西
会因此报错**，读者点过去才发现是死的。

这道闸扫盘、不写名单（`护栏要扫盘不要写名单`）：把 platform/docs/ 下每个
markdown 链接的目标解析一遍，指不到的就红。新加一份文档、删一份文档，
都自动被它管到，不需要有人记得来更新这里。
"""
from __future__ import annotations

import re
from pathlib import Path

_DOCS = Path(__file__).resolve().parents[1] / "platform" / "docs"

#: markdown 的 `[文字](目标)`。只管相对路径 —— 外链的死活不归这道闸。
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _relative_targets(text: str):
    for raw in _LINK.findall(text):
        target = raw.split("#", 1)[0].strip()
        if not target or "://" in target or target.startswith(("#", "mailto:")):
            continue
        yield target


def test_every_relative_link_in_platform_docs_resolves():
    dangling: list[str] = []
    for doc in sorted(_DOCS.rglob("*.md")):
        for target in _relative_targets(doc.read_text(encoding="utf-8")):
            if not (doc.parent / target).exists():
                dangling.append(f"{doc.relative_to(_DOCS.parent.parent)} → {target}")

    assert not dangling, (
        "platform/docs/ 里这些链接指到了不存在的文件 —— 读者点过去才会发现，"
        "没有任何东西会先报错：\n  " + "\n  ".join(dangling)
    )

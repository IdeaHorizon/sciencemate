"""writing 的门只能有一个出处 —— 文档不许自己再长一道。

2026-09-17 之前，同一个问题在 writing 里有三份答案：

- `artifact_expectations.yaml`：字段缺口不拦，开写与否问闭合账本；
- `skills/submission_readiness/SKILL.md`：没有 `experiment_log` 就 `blocked`；
- `venues/*/checklist.md`：少于 5 条 KB claim 不算完成。

三份规则在不同阶段驱动同一个模型，于是它为了让清单变绿去补不必要的章节和流程
说明。几份抄件就有几个各自演化的答案，而且**分叉不报错** —— 所以这里加一道扫盘。

判据的形状（护栏要扫盘不要写名单）：**先把合法那条路命名出来，剩下一律违规**。
合法 = 判 blocked 的那段话点名唯一出处（`audit_writing_inputs` /
`validate_writing_manuscript` / `blocking_items` / `preflight_status` / `GATES.md`）；
并且这段话**不自带条件清单** —— 一旦它开始逐条列"满足 X 就 blocked"，那就是第二
道门在长出来，无论它嘴上引用了谁。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WRITING_ROOT = REPO_ROOT / "nodes" / "writing"

#: 唯一出处的名字。段落点名了其中之一，才算"我在复述那道门"。
AUTHORITY_TOKENS = (
    "audit_writing_inputs",
    "validate_writing_manuscript",
    "blocking_items",
    "preflight_status",
    "GATES.md",
)

#: 判决句：把 blocked 当**结论**在下的写法（不是提到这个词就算）。
VERDICT_RE = re.compile(
    r"set\s+[^\n]{0,80}\bblocked\b"
    r"|\bblocked\b\s+(?:when|if)\b"
    r"|(?:->|→)\s*[\"'`]?blocked"
    r"|status[^\n]{0,40}=\s*[\"'`]?blocked"
    r"|hard gate"
    r"|判(?:为|成)\s*blocked"
    r"|就\s*blocked"
    r"|不得开写",
    re.IGNORECASE,
)

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")


def _documents() -> list[Path]:
    docs = sorted(WRITING_ROOT.glob("skills/*/SKILL.md"))
    docs += sorted(WRITING_ROOT.glob("venues/*/checklist.md"))
    docs.append(WRITING_ROOT / "review_spec.md")
    return [path for path in docs if path.is_file()]


def _blocks(text: str) -> list[str]:
    """按空行切块，并把以冒号结尾的引导句与紧随其后的清单**并成一块**。

    判据要看的是"这段话在判 blocked，而且自己列了条件"——引导句和条件清单被
    空行分开时，分开看两边都无辜。
    """
    raw: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            raw.append("\n".join(current))
            current = []
    if current:
        raw.append("\n".join(current))

    merged: list[str] = []
    for block in raw:
        if (
            merged
            and merged[-1].rstrip().endswith((":", "：'", "：", "，"))
            and _LIST_ITEM_RE.match(block.splitlines()[0])
        ):
            merged[-1] = merged[-1] + "\n" + block
        else:
            merged.append(block)
    return merged


def _cites_authority(text: str) -> bool:
    return any(token in text for token in AUTHORITY_TOKENS)


def _label(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:  # 变异用例写在 tmp_path 里
        return path.name


def gate_violations(path: Path) -> list[str]:
    """这份文档里自己长出来的门。

    两种违规形状，对应两种长法：

    1. **判 blocked 却不说依据** —— 这段话在下结论，而结论没有出处。
    2. **判决句后面挂着自己的条件清单** —— "Set status blocked when …:" 后面逐条
       列条件，就是一道手写的门，哪怕段落别处引用了真出处。判决句必须是
       **引导清单的那一行**（以冒号结尾）才算这一种；普通流程步骤里提到 blocked
       不算 —— 误报一次的代价，是这条判据以后没人再看。
    """
    findings: list[str] = []
    for block in _blocks(path.read_text(encoding="utf-8")):
        match = VERDICT_RE.search(block)
        if not match:
            continue
        lines = block.splitlines()
        head = lines[0]
        if not _cites_authority(block):
            findings.append(f"{_label(path)}: 判 blocked 却没点名唯一出处 — {head[:90]}")
            continue
        verdict_line_index = block[: match.start()].count("\n")
        verdict_line = lines[verdict_line_index]
        if not verdict_line.rstrip().endswith((":", "：")):
            continue
        own_conditions = [
            line
            for line in lines[verdict_line_index + 1 :]
            if _LIST_ITEM_RE.match(line) and not _cites_authority(line)
        ]
        if own_conditions:
            findings.append(
                f"{_label(path)}: 判 blocked 的那句后面挂着自己的条件清单 — "
                + "; ".join(item.strip()[:60] for item in own_conditions[:3])
            )
    return findings


def test_writing_docs_do_not_grow_a_second_gate() -> None:
    violations = [finding for path in _documents() for finding in gate_violations(path)]
    assert violations == [], "\n".join(violations)


def test_the_scan_catches_the_gate_that_was_removed(tmp_path: Path) -> None:
    """变异：把 2026-09-17 删掉的那条门原样写回去，扫盘必须转红。

    判据不会自己证明自己有效 —— 这条测试就是它的证明。
    """
    mutated = tmp_path / "SKILL.md"
    mutated.write_text(
        "## Status Rules\n\n"
        "Set `status: blocked` when any hard gate fails:\n\n"
        "- no `experiment_log` artifact is available\n"
        "- KB claim count is below the manuscript threshold\n",
        encoding="utf-8",
    )
    findings = gate_violations(mutated)
    assert findings, "扫盘放过了被删掉的那道门"


def test_the_scan_allows_quoting_the_single_source(tmp_path: Path) -> None:
    """正常反例：复述唯一出处不该被拦（误报的代价是没人再理这条判据）。"""
    quoted = tmp_path / "SKILL.md"
    quoted.write_text(
        "## Status Rules\n\n"
        "`status: blocked` has one legal source: `preflight_status` is already\n"
        "`blocked_*`, as decided by `audit_writing_inputs`. Copy it and record the\n"
        "gate that fired in `blocking_items`.\n",
        encoding="utf-8",
    )
    assert gate_violations(quoted) == []


def test_the_scan_catches_a_gate_that_hides_behind_a_citation(tmp_path: Path) -> None:
    """变异补的洞：引用了唯一出处，却在下面挂自己的条件清单。

    第一版只测了"完全不提出处"那一半，于是"自带条件清单"那条判据从没被任何测试
    走到过 —— 把它打瞎，全套仍然全绿。一道没人走过的判据等于不存在。
    """
    disguised = tmp_path / "SKILL.md"
    disguised.write_text(
        "## Status Rules\n\n"
        "Mirror `audit_writing_inputs`. Set `status: blocked` when:\n\n"
        "- no `experiment_log` artifact is available\n"
        "- the abstract is too generic\n",
        encoding="utf-8",
    )
    findings = gate_violations(disguised)
    assert findings, "引用出处之后自己列条件，扫盘也得抓住"
    assert "自己的条件清单" in findings[0]

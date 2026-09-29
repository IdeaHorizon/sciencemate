"""账本判读的双实现契约：harness `core.ledger.RecordStore.pinned` 与平台镜像
`app.services.frozen_register.apply_rows` 必须对同一份账本给出同一张钉死表。

分叉的后果是静默的：一边认为修订已解除钉死、另一边照旧回滚 —— 合法的修订被
闸吃掉，而且报错指向假原因。所以两边钉在同一份 fixture 上，且这条测试在
**两边的 CI** 里都要跑得动（平台镜像零 app 依赖）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "backend"))

from app.services import frozen_register  # noqa: E402

from core.ledger import RecordStore  # noqa: E402

A, B, C, D = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)

ROWS = [
    # a：冻结 v1，然后带 amendment 存 v2 → 钉死解除（冻结的字节在 git 历史里）
    {"event": "save", "id": "pre_registration__a", "type": "pre_registration", "name": "a",
     "path": "plan/pre_registration__a.md", "version": 1, "sha256": A, "created_at": "t1"},
    {"event": "freeze", "id": "pre_registration__a", "version": 1,
     "path": "plan/pre_registration__a.md", "sha256": A, "frozen_at": "t2",
     "metadata_patch": {"frozen": True, "frozen_at": "t2"}},
    {"event": "save", "id": "pre_registration__a", "type": "pre_registration", "name": "a",
     "path": "plan/pre_registration__a.md", "version": 2, "sha256": B, "created_at": "t3",
     "amendment": {"from_version": 1, "reason": "补判据"}},
    # b：冻结 v1，没再动 → 钉死
    {"event": "save", "id": "manuscript__b", "type": "manuscript", "name": "b",
     "path": "paper/manuscript__b.tex", "version": 1, "sha256": C, "created_at": "t4"},
    {"event": "freeze", "id": "manuscript__b", "version": 1,
     "path": "paper/manuscript__b.tex", "sha256": C, "frozen_at": "t5",
     "metadata_patch": {"frozen": True}},
    # c：存了又撤 → 什么都不钉
    {"event": "save", "id": "note__c", "type": "note", "name": "c",
     "path": "notes/note__c.md", "version": 1, "sha256": D, "created_at": "t6"},
    {"event": "retire", "id": "note__c", "version": 1, "path": "notes/note__c.md", "reason": "x"},
    # 坏行：跳过，不炸
    {"event": "freeze", "id": "ghost", "version": 1, "path": "x", "sha256": "short"},
]
EXPECTED = {"paper/manuscript__b.tex": C}


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / ".research" / "ledger" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join([*(json.dumps(r) for r in ROWS), "not json at all"]) + "\n",
                    encoding="utf-8")
    return path


def test_core_pinned_semantics(tmp_path: Path, ledger: Path) -> None:
    assert RecordStore(tmp_path, ledger).pinned() == EXPECTED


def test_platform_gate_reads_the_same_ledger_identically(tmp_path: Path, ledger: Path) -> None:
    assert frozen_register.read_register(ledger) == EXPECTED
    assert frozen_register.scan_worktree(tmp_path) == EXPECTED
    # 平台镜像必须零 app 依赖：否则 harness 的 CI job 里这条测试 import 不进来，
    # 守卫只能 skip —— 而 skip 不是红色。
    assert "app.config" not in sys.modules

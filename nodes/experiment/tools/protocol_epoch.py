"""Experiment 协议纪元标记（v3 governing document §5、§9）。

只放常量与纯函数，不带行为：账本与意图记录写 ``PROTOCOL_EPOCH`` 与
``LEDGER_SCHEMA_VERSION``，旧协议 run（没有带这两个标记的账本）在新代码里
确定性地进入只读 / drain / blocked，不被误识别成新 run。
"""

from __future__ import annotations

#: 当前协议纪元。v2 = 路线 / 收据 / verify / finalize 手续协议（被取代）。
PROTOCOL_EPOCH = "v3"

#: 账本（provenance / event index）与意图记录的 schema 标记。
LEDGER_SCHEMA_VERSION = "experiment.ledger.v3"

#: 本分支的发布边界：operation-only 实验集成分支，不可合并、不可发布。
BRANCH_RELEASE_BOUNDARY = "operation_only_non_mergeable"


def record_marker() -> dict[str, str]:
    """每条账本 / 意图记录都带的纪元与 schema 标记。"""
    return {"protocol_epoch": PROTOCOL_EPOCH, "schema_version": LEDGER_SCHEMA_VERSION}


def is_current_epoch(record: dict | None) -> bool:
    """记录属于当前纪元当且仅当两个标记都逐字匹配；缺任一即旧协议或损坏。"""
    if not isinstance(record, dict):
        return False
    return (
        record.get("protocol_epoch") == PROTOCOL_EPOCH
        and record.get("schema_version") == LEDGER_SCHEMA_VERSION
    )

"""一份坏的 artifact 记录不该让整次派发崩掉，而且报错要指向真原因。

## 病例（2026-08-21 实测）

orchestrator 派 experiment 修复 run，`tool_exception`：traceback 落在
`_resolve_forward_artifacts` 的 `rec["type"]` KeyError。`read_artifact()` 是
`json.loads` 裸透传，对记录形状零契约，而转发侧直接按 key 取 —— 一个手写或
写了一半的产物文件，就能让一次派发整个作废。

## 为什么它躲过了上一次修复

同一个类的 bug 在**观察侧**修过（issue #299）：`list_artifacts` 缺字段时不再
抛，改成标 `malformed=True`，注释写着"让上层看得见"。但那个标记**全仓零消费
方**。于是坏记录以「type=(unknown)、可以转发」的样子出现在模型眼前，模型按 id
转发它，动作侧照炸。

**观察侧修了，动作侧没跟着扫盘** —— 这正是"机制存在但没接到路径"。所以这条
测试同时钉住两侧：列举不抛、转发不抛。

## 报错必须指向真原因

坏记录如果被塞进"找不到这个 id"，模型会去 `list_artifacts` 核对，而那里 id
明明在 —— 报错指向假原因，模型只能原地打转。两种处境要分开说：id 不存在
（换一个）vs 记录坏了（换 id 没用，得重新产出）。
"""
from __future__ import annotations

import json

from core.state import State


def _write_raw_artifact(state: State, aid: str, payload: dict) -> None:
    """绕过 save_artifact 直接落一份记录 —— 模拟手写/半截的账本行
    （正文文件在，账本 save 行缺 type/name）。"""
    path = state.root / "artifacts" / f"{aid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(payload.get("content") or ""), encoding="utf-8")
    row = {"event": "save", "id": aid, "path": path.name, "version": 1,
           "created_at": "2026-08-21T00:00:00+00:00"}
    with (state.root / "records.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_listing_survives_a_malformed_record(tmp_path) -> None:
    """观察侧：列举不许被坏数据打断（issue #299 的既有契约，防回归）。"""
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="bad1")
    _write_raw_artifact(st, "half_written", {"content": "只写了一半"})

    entries = st.list_artifacts()
    bad = [e for e in entries if e["id"] == "half_written"]
    assert bad, "坏记录不该让列举丢条目"
    assert bad[0]["type"] == "(unknown)", "缺字段要如实标注"


def test_forwarding_a_malformed_record_does_not_raise(tmp_path) -> None:
    """动作侧：转发坏记录不许抛 —— 它该进 missing 并说清为什么。"""
    from shared.tools.run_node import _resolve_forward_artifacts

    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="bad2")
    _write_raw_artifact(st, "half_written", {"content": "只写了一半"})

    ok, missing = _resolve_forward_artifacts(st, ["half_written"])
    assert ok == []
    assert len(missing) == 1
    assert missing[0]["id"] == "half_written"
    assert "type" in missing[0]["reason"], "要说清是记录缺字段，不是 id 找不到"


def test_the_message_tells_a_bad_record_from_a_missing_id(tmp_path) -> None:
    """两种处境的下一步完全不同，报错必须分开说。"""
    from shared.tools.run_node import (
        _resolve_forward_artifacts, _unusable_forward_message,
    )

    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="bad3")
    _write_raw_artifact(st, "half_written", {"content": "只写了一半"})

    _ok, missing = _resolve_forward_artifacts(st, ["half_written", "never_existed"])
    message = _unusable_forward_message(missing)

    assert "half_written" in message and "never_existed" in message
    # 坏记录那条不许被说成"找不到"
    bad_line = next(ln for ln in message.splitlines() if "half_written" in ln)
    assert "找不到" not in bad_line, "记录坏了被说成找不到 → 模型会去核对 id 然后原地打转"
    assert "重新产出" in message or "重跑" in message, "要给出真正的下一步"


def test_auto_resolution_skips_malformed_records(tmp_path) -> None:
    """自动选料不许挑中读不出来的记录。"""
    from shared.tools.run_node import _auto_resolve_required_inputs

    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="bad4")
    _write_raw_artifact(st, "half_written", {"content": "只写了一半"})

    auto_ids, _ambiguous, missing_types = _auto_resolve_required_inputs(
        st, ["research_plan"])
    assert auto_ids == [], "坏记录不该被选进来"
    assert "research_plan" in missing_types, "该如实报告这个 type 没有可用产物"

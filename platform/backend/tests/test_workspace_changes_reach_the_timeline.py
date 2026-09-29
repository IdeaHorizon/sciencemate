"""workspace_changed 必须落成 durable 事件 —— 内联"Edited foo.py +17 -0"卡的数据源。

此前它只走瞬时 progress 通道（local_execution 转发一条 transient），会话一
刷新就没了；UI 只能在顶部挂一块全量 diff 面板。2026-08-17 用户点名要 Claude
Code 那种原位内联卡 —— 前提就是这条事件 durable、带每文件 ±行数。

同日追加：卡片要**能点开看 diff 正文**，所以 patch 也得随事件落库。它不能等
点开再回查 —— 工作区是脏的，下一次工具调用就把那一刻覆盖了。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.execution import ExecutionEvent
from app.services.execution_ingest import ExecutionIngestService

from .test_execution_foundation import AT, _context, _ingest, execution_db  # noqa: F401

pytestmark = pytest.mark.asyncio


def _patch_for(path: str, lines: int) -> str:
    body = "".join(f"+line {index}\n" for index in range(lines))
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{lines} @@\n"
        f"{body}"
    )


async def test_a_workspace_change_becomes_a_durable_event_with_per_file_stats(
    execution_db,  # noqa: F811
) -> None:
    service = ExecutionIngestService()
    outcome = await _ingest(
        service,
        execution_db,
        _context(),
        {
            "event": "workspace_changed",
            "at": AT,
            "tool_name": "save_artifact",
            "node_type": "hypothesis",
            "files_changed": 2,
            "additions": 36,
            "deletions": 4,
            "file_stats": [
                {
                    "path": "plan/pre_registration.md",
                    "additions": 17,
                    "deletions": 0,
                    "status": "added",
                },
                {
                    "path": "plan/research_plan.md",
                    "additions": 19,
                    "deletions": 4,
                    "status": "modified",
                },
            ],
            "patch": _patch_for("plan/pre_registration.md", 3),
            "patch_truncated": False,
            "fingerprint": "abc123",
        },
        offset=10,
    )
    assert outcome.event is not None
    assert outcome.event.kind == "workspace.changed"
    payload = outcome.event.payload
    assert payload["tool"] == "save_artifact"
    assert payload["filesChanged"] == 2
    assert payload["additions"] == 36
    assert payload["deletions"] == 4
    assert payload["files"] == [
        {
            "path": "plan/pre_registration.md",
            "additions": 17,
            "deletions": 0,
            "status": "added",
        },
        {
            "path": "plan/research_plan.md",
            "additions": 19,
            "deletions": 4,
            "status": "modified",
        },
    ]
    assert payload["patchTruncated"] is False
    assert "+line 0" in payload["patch"], "没有正文，卡片点开还是空的"

    stored = await execution_db.scalar(
        select(ExecutionEvent).where(ExecutionEvent.kind == "workspace.changed")
    )
    assert stored is not None, "必须 durable —— 瞬时通道刷新即失忆"
    assert "+line 0" in stored.payload["patch"], "正文必须落库，不能只活在内存里"


async def test_a_long_diff_survives_the_generic_string_ceiling(
    execution_db,  # noqa: F811
) -> None:
    """通用 4000 字符上限是给**任意**字符串兜底的，不该把 diff 剪掉。

    这条是回归：脱敏层原来对所有字符串一刀 4000，diff 会被静默剪成一小截，
    而 patchTruncated 照样报 false —— 读者会把断口当成"改动到此为止"。
    """
    service = ExecutionIngestService()
    patch = _patch_for("paper/report.tex", 900)      # 远超 4000 字符
    assert len(patch) > 8_000
    outcome = await _ingest(
        service,
        execution_db,
        _context(),
        {
            "event": "workspace_changed",
            "at": AT,
            "tool_name": "write_file",
            "node_type": "writing",
            "files_changed": 1,
            "additions": 900,
            "deletions": 0,
            "file_stats": [
                {"path": "paper/report.tex", "additions": 900, "deletions": 0, "status": "added"}
            ],
            "patch": patch,
            "patch_truncated": False,
            "fingerprint": "long-1",
        },
        offset=30,
    )
    assert outcome.event is not None
    assert "+line 899" in outcome.event.payload["patch"], "diff 被脱敏层剪掉了"
    assert outcome.event.payload["patchTruncated"] is False


async def test_the_boundary_caps_an_oversized_diff_and_says_so(
    execution_db,  # noqa: F811
) -> None:
    """边界不替上游许诺体积：真截了就必须标 truncated，不能静默剪。"""
    service = ExecutionIngestService()
    outcome = await _ingest(
        service,
        execution_db,
        _context(),
        {
            "event": "workspace_changed",
            "at": AT,
            "tool_name": "write_file",
            "node_type": "writing",
            "files_changed": 1,
            "additions": 20_000,
            "deletions": 0,
            "file_stats": [
                {"path": "paper/huge.tex", "additions": 20_000, "deletions": 0, "status": "added"}
            ],
            "patch": _patch_for("paper/huge.tex", 20_000),   # 约 260KB
            "patch_truncated": False,
            "fingerprint": "huge-1",
        },
        offset=40,
    )
    assert outcome.event is not None
    payload = outcome.event.payload
    assert payload["patchTruncated"] is True, "截了就得说"
    assert len(payload["patch"].encode("utf-8")) < 100_000
    assert payload["additions"] == 20_000, "统计数是独立事实，不随正文截断缩水"


async def test_an_old_harness_without_file_stats_still_yields_paths(
    execution_db,  # noqa: F811
) -> None:
    service = ExecutionIngestService()
    outcome = await _ingest(
        service,
        execution_db,
        _context(),
        {
            "event": "workspace_changed",
            "at": AT,
            "tool_name": "write_file",
            "node_type": "writing",
            "files_changed": 1,
            "additions": 5,
            "deletions": 0,
            "paths": ["paper/draft.md"],
            "fingerprint": "def456",
        },
        offset=20,
    )
    assert outcome.event is not None
    assert outcome.event.kind == "workspace.changed"
    assert outcome.event.payload["files"] == [{"path": "paper/draft.md"}]
    # 老事件没有 patch —— 前端据此决定"不画那个假的展开箭头"。
    assert "patch" not in outcome.event.payload


async def test_a_dispatch_announcement_becomes_a_conversation_event(
    execution_db,  # noqa: F811
) -> None:
    """调度器派发前说的那句话必须落成**独立 kind**，不能混进 agent.message。

    混进去的后果实测过：调度器 8 句话和子节点 51 句内部独白同样式渲染，用户
    在对话里根本分不出哪句是说给他听的（2026-08-17「完全是混乱的」）。
    """
    service = ExecutionIngestService()
    outcome = await _ingest(
        service,
        execution_db,
        _context(),
        {
            "event": "node_dispatch_announced",
            "at": AT,
            "node_type": "literature",
            "user_note": "先做文献调研：项目里还没有证据基础，我需要先摸清既有研究。",
            "background": False,
        },
        offset=30,
    )
    assert outcome.event is not None
    assert outcome.event.kind == "orchestrator.said", "必须与 agent.message 分开"
    assert outcome.event.payload["aboutNodeType"] == "literature"
    assert "文献调研" in outcome.event.payload["text"]
    assert outcome.event.visibility == "summary", "这是对话，不该藏在 trace 档里"

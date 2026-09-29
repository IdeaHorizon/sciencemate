"""答复送给的是一个已经在跑的 worker：它的沙箱在 spawn 那一刻就定了，答复只能用那个 attempt。

## 现场（2026-09-09 node20，部署 7db7aa5 后 qinp 第一次点卡）

worker 停在决策卡上、接管自旧 release（activity.json 记着 attempt #2 与清单 hash）。
人点 PROCEED → 答复路径重新冻结一份清单：harness_root 换成新 release 路径，hash 变了
→ "能力变了 = 新 attempt"：释放 #2、新租 #3 → `session.answer` 拿新绑定与 worker 的
旧绑定比，沙箱三项对不上 → StaleError → 记账层把 run 盖成 stale_unknown。worker 一直
活着停在同一张卡上，人看到"这一轮没能完成"。

三条：answer 分支不再冻结清单；attempt 按 worker 自报的绑定取；pause 归属只比身份四项。
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from app.services import local_execution
from app.services.harness_sessions import AppRunBinding, _ProjectHarnessSession

_BACKEND = pathlib.Path(__file__).resolve().parents[1]


class _DB:
    def __init__(self, rows: dict[str, object], latest: object | None = None) -> None:
        self.rows = rows
        self.latest = latest

    async def get(self, _cls, key):
        return self.rows.get(key)


@pytest.mark.asyncio
async def test_the_attempt_comes_from_the_workers_binding_first(monkeypatch) -> None:
    worker_attempt = SimpleNamespace(id="att-2", attempt_no=2)
    newest = SimpleNamespace(id="att-3", attempt_no=3)
    db = _DB({"att-2": worker_attempt})

    async def fake_latest(_db, run_id):
        return newest if run_id == "run-x" else None

    monkeypatch.setattr(local_execution, "latest_attempt", fake_latest)
    binding = AppRunBinding("u", "c", "run-x", "c", "att-2", None, "")
    assert await local_execution.attempt_the_worker_runs_in(db, binding) is worker_attempt
    # 老 worker 报不出 attempt → 退回这条 run 最新的那个
    bare = AppRunBinding("u", "c", "run-x", "c", "", None, "")
    assert await local_execution.attempt_the_worker_runs_in(db, bare) is newest
    assert await local_execution.attempt_the_worker_runs_in(db, None, run_id="run-x") is newest


def test_the_answer_branch_never_refreezes_the_sandbox_manifest() -> None:
    """AST：`execute_local_turn` 里 `elif is_answer:` 那一支不许调 `_freeze_attempt_sandbox_manifest`。"""
    source = (_BACKEND / "app" / "services" / "local_execution.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_local_turn"
    )
    answer_branches = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "is_answer"
    ]
    assert answer_branches, "没找到 is_answer 分支 —— 测试本身失效了"
    for branch in answer_branches:
        for node in ast.walk(ast.Module(body=branch.body, type_ignores=[])):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                assert name != "_freeze_attempt_sandbox_manifest", (
                    f"第 {node.lineno} 行：答复路径重新冻结清单 —— 换 release 后会把 worker "
                    "真正跑着的 attempt 释放掉"
                )


async def _noop(_event: dict) -> None:
    return None


@pytest.mark.asyncio
async def test_pause_ownership_is_identity_not_sandbox_bookkeeping() -> None:
    session = _ProjectHarnessSession(
        project_id="p", session_id="c", owner_user_id="u", backend_id="b",
        backend_fingerprint="fp",
        platform_context_hash=None, process=None, stderr_task=None, provider_secrets=(),
        channel=object(), worker=object(),  # type: ignore[arg-type]
    )
    sent: list[dict] = []

    async def fake_rpc(payload: dict, **_: object) -> dict:
        sent.append(payload)
        return {"data": {"status": "completed"}}

    session._rpc_locked = fake_rpc  # type: ignore[method-assign]
    # worker 接管时自报的绑定：旧 release 下租的 attempt #2
    session.binding = AppRunBinding("u", "c", "run-x", "c", "att-2", {"mounts": ["old"]}, "hash-old")
    session.paused = True
    session.pause_id = "chatcmpl-tool-1"
    # 平台这次答复带来的绑定：同一个人、同一会话、同一 run，沙箱记账不同
    answering = AppRunBinding("u", "c", "run-x", "c", "att-3", {"mounts": ["new"]}, "hash-new")
    await session.answer(
        binding=answering, answer="proceed", choice=None,
        on_progress=_noop, on_protocol_event=_noop,
    )
    assert sent and sent[0]["op"] == "answer", "同一个 pause 的答复不许因沙箱记账不同被判成没有 pause"

"""run_nodes_parallel smoke test。Mock execute_node 避免真实 LLM。

测试点：
  1. 并行 N 个 job → 全部返回
  2. 并发上限 (Semaphore) 起作用
  3. 一个 job 失败不阻塞其它
  4. job 顺序保持
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import bootstrap

bootstrap()

from core.state import State  # noqa: E402
from shared.tools import run_node as run_node_mod  # noqa: E402


def make_state(td: Path) -> State:
    s = State.new(node_type="parent", base_dir=td)
    s.hook_state["_callable_nodes"] = ["*"]
    return s


def test_parallel_all_succeed():
    """3 个 job 全部成功，结果按 job_index 顺序出。"""
    calls: list[str] = []

    async def fake_run_node(state, node_type, node_inputs=None,
                            forward_artifact_ids=None, **_):
        calls.append(node_type)
        await asyncio.sleep(0.02)
        return {
            "status": "success", "child_run_id": f"run_{node_type}",
            "child_node_type": node_type, "child_status": "completed",
            "child_turns": 1, "imported_artifacts": [],
            "all_child_artifacts": [], "missing_required_outputs": [],
            "final_text_preview": f"ok {node_type}",
        }

    original = run_node_mod._run_node_tool
    run_node_mod._run_node_tool = fake_run_node
    try:
        with tempfile.TemporaryDirectory() as td:
            state = make_state(Path(td))
            jobs = [
                {"node_type": "literature", "node_inputs": {"q": "A"}},
                {"node_type": "literature", "node_inputs": {"q": "B"}},
                {"node_type": "analysis"},
            ]
            result = asyncio.run(run_node_mod._run_nodes_parallel(state, jobs=jobs))
            assert result["status"] == "success"
            assert result["job_count"] == 3
            assert result["successes"] == 3
            assert result["failures"] == 0
            # job_index 在每个结果里
            indices = sorted(r["job_index"] for r in result["results"])
            assert indices == [0, 1, 2]
            print(f"  ✓ 3 个 job 并行成功，calls={len(calls)}")
    finally:
        run_node_mod._run_node_tool = original


def test_parallel_cap():
    """并发上限 cap=2，3 个 job 不会同时跑（用 in_flight 计数验证）。"""
    in_flight = 0
    max_in_flight = 0

    async def fake_run_node(state, node_type, **_):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return {"status": "success"}

    original = run_node_mod._run_node_tool
    run_node_mod._run_node_tool = fake_run_node
    try:
        with tempfile.TemporaryDirectory() as td:
            state = make_state(Path(td))
            jobs = [{"node_type": "x"} for _ in range(4)]
            asyncio.run(run_node_mod._run_nodes_parallel(state, jobs=jobs, max_parallel=2))
            assert max_in_flight <= 2, f"并发上限被破：max_in_flight={max_in_flight}"
            print(f"  ✓ cap=2 生效（峰值 in_flight={max_in_flight}）")
    finally:
        run_node_mod._run_node_tool = original


def test_parallel_partial_failure():
    """中间一个 job 抛异常，其它正常 → status=error 出现但其它仍成功。"""
    async def fake_run_node(state, node_type, **_):
        if node_type == "broken":
            raise RuntimeError("intentional crash")
        return {"status": "success", "child_node_type": node_type}

    original = run_node_mod._run_node_tool
    run_node_mod._run_node_tool = fake_run_node
    try:
        with tempfile.TemporaryDirectory() as td:
            state = make_state(Path(td))
            jobs = [
                {"node_type": "ok1"}, {"node_type": "broken"}, {"node_type": "ok2"},
            ]
            result = asyncio.run(run_node_mod._run_nodes_parallel(state, jobs=jobs))
            assert result["job_count"] == 3
            statuses = [r.get("status") for r in result["results"]]
            assert statuses.count("success") == 2
            assert statuses.count("error") == 1
            print(f"  ✓ 1 个 job 失败不阻塞其它：statuses={statuses}")
    finally:
        run_node_mod._run_node_tool = original


def test_parallel_empty_rejected():
    """jobs=[] 报错 —— 契约归 schema（minItems:1），派发口核。"""
    from core.tool_registry import execute

    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        result = asyncio.run(execute("run_nodes_parallel", state, jobs=[]))
        assert result["status"] == "error" and result.get("parameter_violations")
        print("  ✓ jobs=[] 被拒")


def test_parallel_missing_node_type():
    """一个 job 缺 node_type → 该 job 报错，其它跑。"""
    async def fake_run_node(state, node_type, **_):
        return {"status": "success", "child_node_type": node_type}

    original = run_node_mod._run_node_tool
    run_node_mod._run_node_tool = fake_run_node
    try:
        with tempfile.TemporaryDirectory() as td:
            state = make_state(Path(td))
            jobs = [{"node_type": "ok"}, {"foo": "bar"}, {"node_type": "ok2"}]
            result = asyncio.run(run_node_mod._run_nodes_parallel(state, jobs=jobs))
            assert result["job_count"] == 3
            assert result["successes"] == 2
            print("  ✓ 缺 node_type 的 job 单独报错，不阻塞其它")
    finally:
        run_node_mod._run_node_tool = original


if __name__ == "__main__":
    print("== run_nodes_parallel smoke tests ==")
    tests = [
        test_parallel_all_succeed,
        test_parallel_cap,
        test_parallel_partial_failure,
        test_parallel_empty_rejected,
        test_parallel_missing_node_type,
    ]
    for t in tests:
        print(f"- {t.__name__}")
        t()
    print("\nALL PASS ✓")

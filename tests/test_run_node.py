"""run_node 工具的单元 smoke test。不联网，不调真实 LLM。

测试点：
  1. callable_nodes 白名单生效（空列表拒绝，"*" 通过，列表精确匹配）
  2. 递归深度上限拒绝
  3. forward_artifact_ids 解析
  4. 子节点 required_output 回填到父 artifacts
  5. read_external_artifact 能读到非 required output 的子产物
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.artifact_provenance import produced
from core.bootstrap import bootstrap
from core.ledger import RecordStore
from core.state import State
from core.tool_registry import execute as execute_tool

bootstrap()


def _fabricate_record(run_root: Path, *, node_type: str, artifact_type: str,
                      name: str, content: str, frozen_at: str | None = None) -> str:
    """往另一个 run 的 run 本地账本里落一份记录（正文文件 + records.jsonl 一行）。

    子/兄弟 run 的产物不再是 `<run>/artifacts/<id>.json` 信封：正文是原生文件，
    事实在 `<run>/records.jsonl`。夹具走同一个 RecordStore，读方才读得到。
    `frozen_at` 给出即再追一行 freeze（冻结只出自账本的 freeze 行）。
    """
    artifact_id = f"{artifact_type}__{name}"
    store = RecordStore(run_root / "artifacts", run_root / "records.jsonl")
    store.save(
        artifact_id=artifact_id, artifact_type=artifact_type, name=name,
        content=content, metadata={}, directory=run_root / "artifacts",
        created_at="2026-09-12T00:00:00+00:00",
        provenance=produced(node_type, run_root.name),
        produced_by_node_type=node_type, produced_by_run_id=run_root.name,
        by_node=node_type, by_run=run_root.name,
    )
    if frozen_at:
        store.freeze(artifact_id, metadata_patch={}, by_node=node_type, by_run=run_root.name,
                     frozen_at=frozen_at)
    return artifact_id


def make_state(tmp: Path, depth: int = 0, callable_nodes=None) -> State:
    s = State.new(node_type="parent_test", base_dir=tmp)
    s.depth = depth
    s.hook_state["_callable_nodes"] = callable_nodes or []
    return s


def test_callable_nodes_empty_rejects():
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td), callable_nodes=[])
        result = asyncio.run(execute_tool(
            "run_node", state, node_type="literature", user_note="测试派发",
        ))
        assert result["status"] == "error"
        assert "callable_nodes" in result["error"]
        print("  ✓ 空 callable_nodes 拒绝调起子节点")


def test_callable_nodes_wildcard_passes_whitelist():
    """通过白名单后由后续的 harness 加载继续。我们这里只测白名单层。"""
    from shared.tools.run_node import _is_allowed_callee
    assert _is_allowed_callee(["*"], "literature")
    assert _is_allowed_callee(["literature"], "literature")
    assert not _is_allowed_callee(["analysis"], "literature")
    assert not _is_allowed_callee([], "literature")
    print("  ✓ _is_allowed_callee 白名单语义正确")


def test_depth_cap_rejects():
    """depth >= MAX_DEPTH 直接拒绝。"""
    from shared.tools.run_node import MAX_DEPTH
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td), depth=MAX_DEPTH, callable_nodes=["*"])
        result = asyncio.run(execute_tool(
            "run_node", state, node_type="literature", user_note="测试派发",
        ))
        assert result["status"] == "error"
        assert "深度" in result["error"]
        print(f"  ✓ depth={MAX_DEPTH} 触发递归上限拒绝")


def test_forward_artifact_ids_missing():
    """forward_artifact_ids 找不到 → 返回 missing 列表。"""
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td), callable_nodes=["literature"])
        result = asyncio.run(execute_tool(
            "run_node", state, node_type="literature", user_note="测试派发",
            forward_artifact_ids=["nonexistent__x"],
        ))
        assert result["status"] == "error"
        assert "nonexistent" in result["error"]
        print("  ✓ forward_artifact_ids 不存在时报错")


def test_resolve_forward_artifacts():
    """父 state 里有 artifact 时，forward 能正确读到并打 metadata。"""
    from shared.tools.run_node import _resolve_forward_artifacts
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        state.save_artifact(
            artifact_type="pre_registration",
            name="h1",
            content="hypothesis 1 文本",
            metadata={"frozen": False},
        )
        ok, missing = _resolve_forward_artifacts(state, ["pre_registration__h1"])
        assert not missing
        assert len(ok) == 1
        assert ok[0]["type"] == "pre_registration"
        assert ok[0]["metadata"]["forwarded_from_run_id"] == state.run_id
        print("  ✓ _resolve_forward_artifacts 把 artifact 打包正确")


def test_import_required_outputs():
    """模拟子 summary，验证只复制 required type 的 artifact。"""
    from shared.tools.run_node import _import_required_outputs
    from core.harness import NodeHarness

    with tempfile.TemporaryDirectory() as td:
        # 模拟一个子 run 目录：两份记录进它的 run 本地账本 —— 一个 required，
        # 一个 intermediate
        child_dir = Path(td) / "child_run_xyz"
        _fabricate_record(child_dir, node_type="literature", artifact_type="survey_report",
                          name="x", content="...survey body...")
        _fabricate_record(child_dir, node_type="literature", artifact_type="scratch_note",
                          name="y", content="...scratch...")

        # 父 state（output 在父 child_dir 的 parent）
        parent = State.new(node_type="parent", base_dir=Path(td))

        child_summary = {
            "run_id": "child_run_xyz",
            "node_type": "literature",
            "status": "completed",   # v3.1：只有 completed 的子 run 才自动回填
            "state_dir": str(child_dir),
            "artifacts": [
                {"id": "survey_report__x", "type": "survey_report", "name": "x"},
                {"id": "scratch_note__y", "type": "scratch_note", "name": "y"},
            ],
        }
        child_harness = NodeHarness(
            node_type="literature",
            required_output_artifact_types=["survey_report"],
        )

        imported = _import_required_outputs(parent, child_summary, child_harness)
        assert len(imported) == 1
        assert imported[0]["type"] == "survey_report"

        parent_artifacts = parent.list_artifacts()
        assert len(parent_artifacts) == 1
        assert parent_artifacts[0]["type"] == "survey_report"

        # provenance metadata
        rec = parent.read_artifact(parent_artifacts[0]["id"])
        assert rec["metadata"]["source_run_id"] == "child_run_xyz"

        # v3.1（审计）：qc 失败 / incomplete 的子 run —— artifact 隔离，不回填
        quarantined = _import_required_outputs(
            parent,
            {**child_summary, "run_id": "child_run_bad", "status": "incomplete"},
            child_harness,
        )
        assert quarantined == []
        assert len(parent.list_artifacts()) == 1   # 没有新增
        assert rec["metadata"]["source_node_type"] == "literature"
        print("  ✓ _import_required_outputs 只复制 survey_report，scratch_note 不带")
        print("  ✓ 复制的 artifact 有 source_run_id / source_node_type metadata")


def test_import_required_outputs_carries_the_freeze():
    """回填也是转发：子 run 冻结过的记录，进父 run 本地账本后仍是冻结的，时刻沿用上游。

    save 行一律剥掉 frozen 键（冻结只出自 freeze 行），回填若只 save 不 freeze，
    转进来的预注册/实验日志就永远"未冻结"，下游防篡改门把它们全拦。
    """
    from shared.tools.run_node import _import_required_outputs
    from core.harness import NodeHarness

    with tempfile.TemporaryDirectory() as td:
        child_dir = Path(td) / "child_run_frz"
        _fabricate_record(child_dir, node_type="literature", artifact_type="survey_report",
                          name="f", content="...frozen survey...",
                          frozen_at="2026-09-01T00:00:00+00:00")
        parent = State.new(node_type="parent", base_dir=Path(td))
        child_summary = {
            "run_id": "child_run_frz", "node_type": "literature", "status": "completed",
            "state_dir": str(child_dir),
            "artifacts": [{"id": "survey_report__f", "type": "survey_report", "name": "f"}],
        }
        child_harness = NodeHarness(node_type="literature",
                                    required_output_artifact_types=["survey_report"])
        imported = _import_required_outputs(parent, child_summary, child_harness)
        assert [a["id"] for a in imported] == ["survey_report__f"]
        head = parent.artifact_head("survey_report__f")
        assert head is not None and head.frozen and head.frozen_version == head.version
        assert head.frozen_at == "2026-09-01T00:00:00+00:00", "冻结时刻沿用上游"
        rec = parent.read_artifact("survey_report__f")
        assert rec["metadata"]["frozen"] is True
        assert rec["metadata"]["source_run_id"] == "child_run_frz"


def test_read_external_artifact():
    """能读到兄弟 run 的 artifact。"""
    with tempfile.TemporaryDirectory() as td:
        # 兄弟 run：记录在它自己的 run 本地账本上
        sibling_dir = Path(td) / "sibling_run_42"
        _fabricate_record(sibling_dir, node_type="literature", artifact_type="x",
                          name="y", content="hello sibling")

        state = make_state(Path(td))
        result = asyncio.run(execute_tool(
            "read_external_artifact", state, run_id="sibling_run_42", artifact_id="x__y",
        ))
        assert result["status"] == "success"
        assert result["artifact"]["content"] == "hello sibling"
        print("  ✓ read_external_artifact 能跨 run 读")


def test_query_project_status():
    """status 工具返回项目级总览。"""
    with tempfile.TemporaryDirectory() as td:
        state = make_state(Path(td))
        # 写一个本地 artifact
        state.save_artifact("plan", "p1", "plan body", metadata={})
        # 写一个兄弟 run summary
        sibling = Path(td) / "sibling_run_99"
        sibling.mkdir()
        (sibling / "summary.json").write_text(json.dumps({
            "run_id": "sibling_run_99",
            "node_type": "literature",
            "status": "completed",
            "turns": 5,
            "depth": 1,
            "project_id": None,
            "artifacts": [{"type": "survey_report", "id": "survey_report__x", "name": "x"}],
        }, ensure_ascii=False))
        result = asyncio.run(execute_tool("query_project_status", state))
        assert result["status"] == "success"
        assert len(result["artifacts"]) == 1
        assert any(r["node_type"] == "literature" for r in result["recent_runs"])
        print(f"  ✓ query_project_status: {len(result['artifacts'])} artifacts, "
              f"{len(result['recent_runs'])} sibling runs")


def test_forward_artifact_tool_is_gone():
    """forward_artifact 工具已删（判决拆除第三波）：它登记的 hook_state 表全仓零读者，
    真机制是 run_node(forward_artifact_ids=[...])。"""
    from core.tool_registry import get_tool
    assert get_tool("forward_artifact") is None


if __name__ == "__main__":
    print("== run_node tool smoke tests ==")
    tests = [
        test_callable_nodes_empty_rejects,
        test_callable_nodes_wildcard_passes_whitelist,
        test_depth_cap_rejects,
        test_forward_artifact_ids_missing,
        test_resolve_forward_artifacts,
        test_import_required_outputs,
        test_read_external_artifact,
        test_query_project_status,
        test_forward_artifact_tool_is_gone,
    ]
    for t in tests:
        print(f"- {t.__name__}")
        t()
    print("\nALL PASS ✓")

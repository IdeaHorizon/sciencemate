"""派发证据生产节点时，模态与还欠的闭合条目类型对不上 —— 记账，不拦。

## 现场（2026-08-18 英国饮食）

一份 S0-S17 全是文献裁决的 research_plan 被派给 `experiment` —— 当时账本只认
experiment 的产物，调度器也看不见"还欠的账全是陈述条"这个事实。整趟 1353 万
tokens，63% 烧在让一个文化史课题去满足计算实验的契约上。

## 判据机械，结论不机械

"还没兑现的闭合条目全是陈述条"是可数的事实。但"所以不该派 experiment"是判断
—— 某条陈述条完全可能需要先跑一次模拟才能兑现。

## 判决拆除第三波（run_node:1217 降格）

这道闸原来「不拦死、只要一句理由」：缺 modality_rationale 就 return error 让调用方
重发。「理由」是申报不是准入，申报缺席本身就是可持久化的事实 —— 往返一次只多烧
一轮。现在：派发照跑，偏离事实 + 理由（没写就是 not_declared）进 transcript 事件
`modality_deviation`，并随派发返回值带回。墙加回去，这里的用例转红。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.prereg_commitments import ClosureTally
from core.research_situation import ResearchSituation, EvidenceInventory
from shared.tools.run_node import _modality_deviation


def _situation(open_numeric: int, open_statement: int):
    return ResearchSituation(
        goal=None,
        closure=ClosureTally(
            total=open_numeric + open_statement + 3,
            fulfilled=3,
            open_numeric=open_numeric,
            open_statement=open_statement,
        ),
        evidence=EvidenceInventory(records={}, supporting=0, anchored_claims=0),
        research_state_path=None,
        research_state_verdict=None,
    )


def _check(node_type, *, open_numeric, open_statement, node_inputs=None):
    with patch("core.research_situation.compute_situation",
               return_value=_situation(open_numeric, open_statement)):
        return _modality_deviation(object(), node_type, node_inputs or {})


class TestTheMismatchIsSeen:
    def test_statements_only_plan_dispatched_to_experiment_is_a_deviation(self):
        """英国饮食那次的机械形态：全陈述条却派计算实验 —— 事实被摆出来。"""
        result = _check("experiment", open_numeric=0, open_statement=6)
        assert result is not None
        assert result["suggested_node"] == "observation"
        assert result["modality_rationale"] == "not_declared"
        assert "status" not in result and "error" not in result   # 事实，不是拒绝

    def test_numbers_only_plan_dispatched_to_observation_is_seen_too(self):
        result = _check("observation", open_numeric=4, open_statement=0)
        assert result is not None
        assert result["suggested_node"] == "experiment"

    def test_the_fact_reports_the_counts_it_was_judged_on(self):
        result = _check("experiment", open_numeric=0, open_statement=6)
        assert result["closure_open"] == {"numeric": 0, "statement": 6}

    def test_a_stated_reason_is_recorded_alongside_the_deviation(self):
        """理由不是出口，是账的一部分：写了就原样记下，没写就记 not_declared。"""
        result = _check(
            "experiment", open_numeric=0, open_statement=6,
            node_inputs={"modality_rationale": "Q2#5 的贸易量级要先按史料重建估算模型"},
        )
        assert result is not None
        assert result["modality_rationale"] == "Q2#5 的贸易量级要先按史料重建估算模型"

    def test_a_blank_reason_is_not_declared(self):
        assert _check("experiment", open_numeric=0, open_statement=6,
                      node_inputs={"modality_rationale": "   "})["modality_rationale"] == "not_declared"


class TestNothingToRecord:
    def test_a_mixed_plan_is_not_a_deviation(self):
        assert _check("experiment", open_numeric=2, open_statement=4) is None
        assert _check("observation", open_numeric=2, open_statement=4) is None

    def test_a_fully_closed_plan_is_not_a_deviation(self):
        assert _check("experiment", open_numeric=0, open_statement=0) is None

    def test_non_evidence_nodes_are_never_touched(self):
        for node in ("literature", "writing", "_reviewer", "hypothesis", "data"):
            assert _check(node, open_numeric=0, open_statement=6) is None

    def test_it_yields_when_the_situation_cannot_be_computed(self):
        with patch("core.research_situation.compute_situation",
                   side_effect=RuntimeError("no state")):
            assert _modality_deviation(object(), "experiment", {}) is None

    def test_a_plan_without_frozen_closure_items_is_not_a_deviation(self):
        situation = ResearchSituation(
            goal=None, closure=None,
            evidence=EvidenceInventory(records={}, supporting=0, anchored_claims=0),
            research_state_path=None, research_state_verdict=None,
        )
        with patch("core.research_situation.compute_situation", return_value=situation):
            assert _modality_deviation(object(), "experiment", {}) is None


class TestDispatchRecordsAndProceeds:
    def test_the_deviation_lands_in_the_ledger_and_the_dispatch_goes_through(
        self, tmp_path, monkeypatch,
    ):
        """从前：status=error 要求带 modality_rationale 重发。现在：照派，
        transcript 里有 `modality_deviation`（rationale=not_declared），返回值带回。"""
        from core import bootstrap
        from core.state import State
        from core.tool_registry import execute

        bootstrap.bootstrap()
        state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="pm")
        state.hook_state["_callable_nodes"] = ["experiment"]
        captured: dict = {}

        async def fake_execute_node(**kw):
            captured["node_type"] = kw["node_type"]
            return {
                "run_id": "fake", "node_type": kw["node_type"], "project_id": "pm",
                "status": "completed", "missing_required_outputs": [], "turns": 1,
                "tool_call_count": 0, "artifacts": [], "final_text_preview": "",
                "state_dir": str(Path(tempfile.mkdtemp())), "project_root": None,
                "depth": 1, "sub_run_id": "t",
            }

        async def no_flow(*a, **k):
            return None

        import core.executor
        monkeypatch.setattr(core.executor, "execute_node", fake_execute_node)
        monkeypatch.setattr("shared.tools.run_node._run_post_producing_flow", no_flow)
        monkeypatch.setattr("core.llm.LLMClient", lambda: MagicMock())
        # 派 experiment 现在必须带任务身份（#1080 第 3 条）。这条测试验的是
        # modality 偏离那件事，前置照最短路径建。
        from core.task_contract import TaskContractLog
        from core.tasks import TaskList

        _tl = TaskList(state.project_root / "tasks")
        _t = _tl.create("测试任务", "", "experiment", state.run_id)
        TaskContractLog(state.project_root / "tasks").append(
            task_instance_uuid=_t.task_instance_uuid, objective="测试任务", actor="test")

        with patch("core.research_situation.compute_situation",
                   return_value=_situation(0, 6)):
            res = asyncio.run(execute(
                "run_node", state, node_type="experiment", user_note="测试派发",
                node_inputs={"experiment_spec": "x"},
                task_instance_uuid=_t.task_instance_uuid,
            ))
        assert res.get("status") == "success", res
        assert captured["node_type"] == "experiment"
        assert res["dispatch_deviations"][0]["kind"] == "modality_deviation"
        assert res["dispatch_deviations"][0]["modality_rationale"] == "not_declared"
        events = [json.loads(l) for l in state.transcript_path.read_text(encoding="utf-8").splitlines()]
        hit = [e for e in events if e.get("event") == "modality_deviation"]
        assert hit and hit[-1]["suggested_node"] == "observation"


class TestTheContractReachesTheCaller:
    def test_the_tool_schema_explains_both_modalities(self):
        """合法取值与选择判据必须在工具契约里，不能只在运行时报错时出现。"""
        from core import bootstrap, tool_registry as tr

        bootstrap.bootstrap()
        definition = tr._REGISTRY.tools["run_node"]
        schema = definition.parameters_schema["properties"]["node_type"]["description"]
        assert "observation" in schema
        assert "干预式" in schema and "检视式" in schema
        assert "modality_rationale" in schema
        # 文案与判据同源：不再说「会要一句理由」
        assert "会要一句" not in schema

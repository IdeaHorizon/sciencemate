"""observation 节点的闸防的是摘樱桃，不是伪造执行。

## 两种取证，两种原罪

experiment 是**干预式**取证：让世界发生一件本不会发生的事。原罪是伪造执行 ——
没跑说跑了、改了输出。所以它的闸围绕"这个数真是这次运行产的"：可回放解析、
输出 hash、执行记录。

observation 是**检视式**取证：世界已经把记录留下了，系统性地去看。原罪是
**摘樱桃**。而伪造执行那套闸在这里**一条也拦不住摘樱桃**：每条引用都真实存在、
每个 DOI 都解析得开，照样可以只挑对自己有利的那七条。

2026-08-18 英国饮食那趟就是现场：全部裁决建立在 7 件证据、0 件一手史料上，
而框架拿"可回放解析"这种防伪造执行的闸去要求它 —— **防错了罪，所以全是空转**。

## 这组测试守什么

守取样纪律的四条痕迹（协议先冻 / 覆盖度有分母 / 反证搜过 / 算术自洽），以及
它们的**判据形态**：问数据本身有没有，而不是问某个自证布尔是不是 true。

⚠️ 这些闸只保证纪律的痕迹在，不保证取样做得好 —— 后者是语义判断归 reviewer。
闸一旦开始查内容质量，就会重演"门要一个形式、模型就生产这个形式"。
"""
from __future__ import annotations

import asyncio

from nodes.observation.tools.observation_contract import (
    STRUCTURAL_REQUIRED_PATHS,
    CONFIRMATORY_REQUIRED_PATHS,
    _observation_log_freeze_gate,
    audit_observation_log,
)


def _freeze_observation_log(state, artifact_id):
    """测门本身：六合一后冻结门是类型性质，经 freeze_artifact 自动执行。

    这里直接调门函数（generic freeze 的其余层——flow 检查/所有权/账本——各有
    自己的测试）。返回形状对齐旧断言：有 failures 即 error。"""
    async def _run():
        gate = _observation_log_freeze_gate(state, artifact_id,
                                            state.read_artifact(artifact_id) or {})
        if gate and gate.get("failures"):
            return {"status": "error", **gate}
        return {"status": "success"}
    return _run()


def _findings(n=1):
    return [
        {
            "id": f"F{i}",
            "statement": "声誉与实际饮食多样性之间存在张力",
            "evidence": ["claim_975e683f2eff", "claim_3b59a09013dd"],
            "inference_type": "descriptive",
        }
        for i in range(n)
    ]


def _confirmatory(**overrides):
    metadata = {
        "mode": "confirmatory",
        "search_protocol": {
            "frozen_ref": "chunk_abc123",
            "inclusion_criteria": ["19 世纪英国饮食一手史料"],
            "exclusion_criteria": ["非英语二手转述"],
        },
        "coverage": {
            "sources_consulted": ["JSTOR", "British Library"],
            "n_screened": 120, "n_included": 18, "n_excluded": 102,
        },
        "adversarial_search": {"queries": ["英国饮食 声誉 前工业 丰盛"]},
        "findings": _findings(),
    }
    metadata.update(overrides)
    return metadata


def _exploratory(**overrides):
    metadata = {
        "mode": "exploratory",
        "coverage": {"sources_consulted": ["JSTOR"]},
        "adversarial_search": {"queries": ["反向 query"]},
        "findings": _findings(),
    }
    metadata.update(overrides)
    return metadata


class _FakeState:
    def __init__(self, record):
        self._record = record
        self.transcript: list[dict] = []

    def read_artifact(self, artifact_id):
        return self._record

    def append_transcript(self, kind, **payload):
        self.transcript.append({"kind": kind, **payload})


def _log(metadata):
    return {"type": "observation_log", "content": "…", "metadata": metadata}


class TestTwoModesHaveDifferentDiscipline:
    """纪律不能只有一套 —— 那会把这个节点做成"只会做系统性综述的节点"。

    第一版把"协议先冻"当成所有 run 的硬闸，等于默认"你开工前就知道自己在找
    什么"。扎根理论明确要求范畴从材料涌现而非预设；史学考据常顺着线索走；
    数据探索的价值恰恰在撞见没预期的模式。那正是 experiment 那个病的同款
    （它默认"研究 = 跑计算实验"）。
    """

    def test_exploration_needs_no_frozen_protocol(self):
        """发现模式不欠协议 ——"什么算相关"本来就是这一趟要找的。"""
        assert audit_observation_log(_FakeState(_log(_exploratory())), "a1")["passed"] is True

    def test_confirmation_without_a_frozen_protocol_is_surfaced(self):
        """判决拆除批 3w（obs 310 降格）：兑现模式缺冻结协议引用不再拒绝——
        缺项如实进 missing + advisories（勾账键对不上冻结 prereg 的账真闸
        仍拦，另有测试钉着），reviewer 看得见协议是事后补的。"""
        metadata = _confirmatory()
        metadata["search_protocol"] = {"inclusion_criteria": ["x"], "exclusion_criteria": ["y"]}
        result = audit_observation_log(_FakeState(_log(metadata)), "a1")
        assert result["passed"] is True, result["reasons"]
        assert "search_protocol.frozen_ref" in result["missing"]
        assert "search_protocol.frozen_ref" in result["advisories"]["sampling_discipline"]

    def test_missing_denominator_is_surfaced(self):
        """判决拆除批 3w（obs 310 降格）：分母缺席如实记 advisory——
        「看不见分母的比例没有意义」这句话现在由 reviewer 拿着 advisory 说。"""
        metadata = _confirmatory()
        metadata["coverage"] = {"sources_consulted": ["JSTOR"], "n_included": 18}
        result = audit_observation_log(_FakeState(_log(metadata)), "a1")
        assert result["passed"] is True, result["reasons"]
        assert "coverage.n_screened" in result["missing"]
        assert "coverage.n_screened" in result["advisories"]["sampling_discipline"]

    def test_exploration_is_not_asked_for_a_denominator(self):
        """探索时还不知道总体边界在哪，要分母是无意义的形式。"""
        metadata = _exploratory()
        assert "n_screened" not in metadata.get("coverage", {})
        assert audit_observation_log(_FakeState(_log(metadata)), "a1")["passed"] is True

    def test_an_undeclared_mode_is_refused_by_name(self):
        """模式决定欠哪些纪律、也决定能不能勾账，不许不声明。"""
        metadata = _confirmatory()
        metadata.pop("mode")
        result = audit_observation_log(_FakeState(_log(metadata)), "a1")
        assert result["passed"] is False
        assert "mode" in result["reasons"]


class TestExplorationCannotCloseQuestions:
    """**不能用生成假设的材料去确证同一个假设** —— anti-HARKing 在取样这一侧。

    第一版要求人人先冻协议，反而把这条红线糊掉了：它让"先探索后确证"这条唯一
    诚实的路径无法表达，于是探索只能伪装成确证。
    """

    def test_an_exploratory_run_may_not_discharge_closure_items(self):
        metadata = _exploratory(closure_discharges={
            "Q1#3": {"status": "discharged", "evidence": "chunk_x"},
        })
        result = audit_observation_log(_FakeState(_log(metadata)), "a1")
        assert result["passed"] is False
        assert "exploratory_cannot_close" in result["reasons"]
        # 报错要给出正路，不能只说不行
        assert "Analysis" in result["reasons"]["exploratory_cannot_close"]

    def test_an_exploratory_run_may_not_report_measured_metrics(self):
        metadata = _exploratory(measured_metrics={"upf_share": {"status": "measured"}})
        result = audit_observation_log(_FakeState(_log(metadata)), "a1")
        assert result["passed"] is False
        assert "exploratory_cannot_measure" in result["reasons"]

    def test_a_confirmatory_run_may_discharge(self):
        metadata = _confirmatory(closure_discharges={
            "Q1#3": {"status": "discharged", "evidence": "chunk_x"},
        })
        assert audit_observation_log(_FakeState(_log(metadata)), "a1")["passed"] is True


class TestFindingsAreFirstClass:
    """这一趟**看到了什么**必须落成产物。

    英国饮食那趟最有价值的一段思考（两条 claim 矛盾、第三条是协调两者的理论
    中介）当时只活在 prose 里 —— 不是产物、不进 KB、writing 读不到。
    """

    def test_a_log_with_only_checkboxes_is_surfaced(self):
        """判决拆除批 3w（obs 252 降格）：findings 空如实记 advisory。"""
        metadata = _confirmatory(findings=[])
        result = audit_observation_log(_FakeState(_log(metadata)), "a1")
        assert result["passed"] is True, result["reasons"]
        assert "no_findings" in result["advisories"]

    def test_a_finding_without_evidence_is_surfaced(self):
        """判决拆除批 3w（obs 182 降格）：缺 evidence 如实记 advisory。"""
        metadata = _confirmatory(findings=[{"id": "F1", "statement": "有张力"}])
        result = audit_observation_log(_FakeState(_log(metadata)), "a1")
        assert result["passed"] is True, result["reasons"]
        assert "evidence" in result["advisories"]["findings_structure"]

    def test_an_abductive_finding_without_alternatives_is_surfaced(self):
        """判决拆除批 3w（obs 185 降格）：**"我想到的唯一解释"不是结论**——
        这句话现在以 advisory 形态随产物走，reviewer 终审。"""
        metadata = _confirmatory(findings=[{
            "id": "F1", "statement": "声誉源于外部书写", "evidence": ["claim_x"],
            "inference_type": "abductive",
        }])
        result = audit_observation_log(_FakeState(_log(metadata)), "a1")
        assert result["passed"] is True, result["reasons"]
        assert "competing_explanations" in result["advisories"]["findings_structure"]

    def test_a_descriptive_finding_needs_no_alternatives(self):
        """只声称"我看到了什么"就不欠竞争解释 —— 闸只在声称解释时才要求结构。

        模型当然可以把所有 finding 标成 descriptive 来绕开；那样它就没有在声称
        解释任何事，而一份满是描述性发现却在下结论的 log，reviewer 看得见。
        机械层保证声称了就得有结构，语义层判声称得诚不诚实。
        """
        assert audit_observation_log(_FakeState(_log(_confirmatory())), "a1")["passed"] is True


class TestNumbersAndArithmetic:
    def test_zero_exclusions_is_a_legal_value(self):
        """把 0 当"没填"会逼模型编一个非零数字 —— 闸反过来制造它要防的行为。"""
        metadata = _confirmatory()
        metadata["coverage"].update({"n_excluded": 0, "n_included": 120})
        assert audit_observation_log(_FakeState(_log(metadata)), "a1")["passed"] is True

    def test_coverage_arithmetic_must_be_self_consistent(self):
        """筛过的不能少于纳入+排除 —— 对不上说明账是拼的，不是记的。"""
        metadata = _confirmatory()
        metadata["coverage"].update({"n_screened": 10, "n_included": 18, "n_excluded": 102})
        result = audit_observation_log(_FakeState(_log(metadata)), "a1")
        assert result["passed"] is False
        assert "coverage_arithmetic" in result["reasons"]


class TestFreezeIsTheEnforcementPoint:
    def test_freeze_is_blocked_and_names_what_is_missing(self):
        """闸放在冻结这一步：冻结之后它就能勾账、能被 writing 引用。

        事后审计不行 —— 证据一旦被下游引用，再发现取样有问题只能追回，代价
        完全不对称。
        """
        metadata = _confirmatory()
        metadata["coverage"].pop("n_screened")
        state = _FakeState(_log(metadata))
        result = asyncio.run(_freeze_observation_log(state, "a1"))
        # 判决拆除批 3w（obs 310 降格）：分母缺席不再拦冻结——缺项进
        # transcript 见证 + advisories；勾账键账真闸（unknown_closure_keys）
        # 与 anti-HARKing 仍拦。
        assert result is None or not (result or {}).get("failures")
        events = [e for e in state.transcript
                  if e["kind"] == "observation_pre_freeze_gate"]
        assert events and "coverage.n_screened" in events[0]["missing"]
        assert "coverage.n_screened" in events[0]["advisories"]["sampling_discipline"]

    def test_the_exploratory_rejection_points_at_the_legal_path(self):
        """拒绝必须给出正路：交给 Analysis 立问题、再起 confirmatory run。"""
        state = _FakeState(_log(_exploratory(closure_discharges={"Q1#1": {"status": "discharged"}})))
        result = asyncio.run(_freeze_observation_log(state, "a1"))
        assert result["status"] == "error"
        assert "confirmatory" in result["hint"]

    def test_the_gate_verdict_and_mode_are_recorded(self):
        """通过与否都留痕（含 mode 与降格 advisories），供机械 QC 判定。"""
        state = _FakeState(_log(_exploratory(findings=[])))
        asyncio.run(_freeze_observation_log(state, "a1"))
        events = [e for e in state.transcript if e["kind"] == "observation_pre_freeze_gate"]
        assert len(events) == 1
        assert events[0]["mode"] == "exploratory"
        # findings 空已降格（obs 252）：passed=True 但 advisory 留痕。
        assert events[0]["passed"] is True
        assert "no_findings" in events[0]["advisories"]


class TestNodeContractIsWiredIntoTheFramework:
    def test_observation_log_is_a_first_class_evidence_record(self):
        """注册表一行声明，闭合账本 / 引用诚信闸 / writing 上游同时认它。

        这是类型能力注册表那次重构的兑现：新增证据类型不再需要改三处机制。
        """
        from shared.lib import artifact_policy as ap

        assert ap.is_evidence_record("observation_log")
        assert ap.carries_discharge_ledger("observation_log")
        assert ap.cites_kb_claims("observation_log")

    def test_ownership_protection_needs_no_code_change(self):
        """所有权保护由扫盘推导 —— 节点一声明 required_output 就自动受保护。

        没有它，orchestrator 可以自己 save_artifact 造一份 observation_log，
        绕开本节点全部取样纪律。
        """
        from shared.tools.builtin import _producing_output_owners

        owners = _producing_output_owners()
        assert owners.get("observation_log") == {"observation"}
        assert owners.get("search_protocol") == {"observation"}

    def test_observation_goes_through_the_full_review_flow(self):
        """它产的是科学证据，必须过 reviewer —— 不能是 service 类。"""
        from core.loader import load_harness

        assert load_harness("observation").post_run_flow == "full"

    def test_it_can_commission_literature_unlike_experiment(self):
        """能调 literature 是这个节点存在的实际意义之一。

        英国饮食那趟 experiment 的 callable_nodes 只有 [data]，它明知缺一手
        史料却调不动 literature，只能把"该补什么证据"写成收尾时的 TODO。
        """
        from core.loader import load_harness

        assert "literature" in (load_harness("observation").callable_nodes or [])


class TestBothGatesAcceptTheSameRecord:
    """写入门与冻结门必须对同一份记录给同一个答案。

    2026-08-23 之前它们不是：本文件的两个 fixture 都被断言"冻结门 passed"，
    而当时的写入面（`artifact_policy.required_metadata`）把 credibility /
    verdicts 判成全模式必需 —— **这两份记录一份都存不下来**，测试却全绿。

    绿的原因是测试只走 `audit_observation_log`，写入面从不在场：
    「替身遮住被测实现」的教科书形态。所以这些用例走**真入口 save_artifact**。
    """

    @staticmethod
    def _save(metadata):
        from shared.tools.library.artifacts_extra import run_save_gate
        return asyncio.run(run_save_gate(
            _FakeState(None),
            {"type": "observation_log", "name": "x",
             "metadata": metadata, "content": "正文"}))

    def test_the_fixtures_this_file_calls_legal_can_actually_be_written(self):
        assert self._save(_exploratory()) is None
        assert self._save(_confirmatory()) is None

    def test_an_exploratory_run_is_not_asked_for_verdicts_it_may_not_make(self):
        """exploratory 被硬禁勾账 → 天然没有闭合条件可裁决。

        要求它非空就是**框架先禁止模型做某件事，再罚它没做**，模型唯一的出路
        是编一份裁决 —— 闸反过来制造它要防的行为（同 `assumptions: []`）。
        """
        metadata = _exploratory()
        assert "verdicts" not in metadata and "credibility" not in metadata
        assert self._save(metadata) is None

    def test_the_write_face_stops_harking_without_waiting_for_freeze(self):
        """anti-HARKing 不能只长在冻结门上。

        `core.prereg_commitments._scan_result_metadata` 收兑现记录时**不看产物
        冻没冻**，按类型扫全部产物的 metadata。所以"写了勾账、永不冻结"这条路
        绕过冻结门之后，那些勾账照样进账本。
        """
        out = self._save(_exploratory(closure_discharges={"Q1#1": {"status": "discharged"}}))
        assert out is not None
        assert "exploratory_cannot_close" in out["failed_checks"]

    def test_removing_the_save_gate_lets_it_through(self):
        """变异检验：把门摘掉，上一条必须转绿 —— 不转就说明挡住它的不是这道门。"""
        from shared.tools.library import artifacts_extra as ax

        gate = ax.SAVE_GATES.pop("observation_log")
        try:
            assert self._save(
                _exploratory(closure_discharges={"Q1#1": {"status": "discharged"}})) is None
        finally:
            ax.SAVE_GATES["observation_log"] = gate

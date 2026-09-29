"""勾账的键对不上冻结预注册，是一种**静默**失效。

## 现场（2026-08-19，英国饮食 e2e 首跑）

observation 节点老老实实裁决了 12 条闭合条件、每条挂了真 DOI 证据，键写成：

    Q1_DISCOURSE_EVIDENCE, Q1_OBJECTIVE_LEVEL_TEST, Q2_TIMELINE, …

而冻结预注册里的 id 是：

    DISCOURSE_EVIDENCE, OBJECTIVE_LEVEL_TEST, TIMELINE, …

模型自己加了问题号前缀。**交集为空 —— 12 条兑现，账本一条都不认。**

最糟的是它一路都不报错：metadata 语法合法、冻结闸放行、节点报告"11/12 已兑现"、
run 判 completed。直到下游 writing 对账时才表现为"零兑现、无可报告"，而那时
错误已经冻进不可变记录里了。

## 病根不是模型乱来

是**契约没送到勾账的人手上**：harness 只说"勾除挂证据"，从没说键必须逐字抄
prereg 的 `id`。这是「契约必须送到调用方」的又一例 —— 合法取值只在下游对账时
才隐式暴露，等于逼模型猜。

## 两道防线

1. harness 里说清楚（开工第一件事就是抄 id）—— 让它不必撞；
2. 冻结闸机械核对，键对不上当场拒绝**并列出全部合法键** —— 撞了也能直接照着改。

本文件守第 2 道。
"""
from __future__ import annotations

import asyncio

from nodes.observation.tools.observation_contract import (
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

_PREREG = """# Pre-Registration
## Research Questions
### Q1: 英国饮食声誉如何形成
- output_kind: 一条命题的裁决
- proposition: 声誉是话语建构
- 闭合条件:
```yaml
- id: DISCOURSE_EVIDENCE
  statement: "列出至少 3 条支持话语建构论的可核验证据"
- id: OBJECTIVE_LEVEL_TEST
  statement: "对客观烹饪水平论做对等检视"
```
"""


class _FakeState:
    """带冻结 prereg 的最小 state —— 键校验要读得到它。"""

    def __init__(self, log_record):
        self._log = log_record
        self._prereg = {"type": "pre_registration", "content": _PREREG,
                        "metadata": {"frozen": True}}
        self.transcript: list[dict] = []
        self.project_root = None

    def list_artifacts(self, artifact_type=None):
        items = [{"id": "prereg1", "type": "pre_registration"},
                 {"id": "log1", "type": "observation_log"}]
        if artifact_type:
            items = [i for i in items if i["type"] == artifact_type]
        return items

    def read_artifact(self, artifact_id):
        return self._prereg if artifact_id == "prereg1" else self._log

    def append_transcript(self, kind, **payload):
        self.transcript.append({"kind": kind, **payload})


def _log(discharges):
    return {
        "type": "observation_log",
        "content": "## Observation Log\n## Credibility\nquestionable",
        "metadata": {
            "mode": "confirmatory",
            "search_protocol": {"frozen_ref": "sp1",
                                "inclusion_criteria": ["a"], "exclusion_criteria": ["b"]},
            "coverage": {"sources_consulted": ["JSTOR"],
                         "n_screened": 24, "n_included": 24, "n_excluded": 0},
            "adversarial_search": {"queries": ["q"]},
            "findings": [{"statement": "s", "evidence": ["doi:10.1/x"],
                          "inference_type": "descriptive"}],
            "closure_discharges": discharges,
        },
    }


class TestKeysAreValidatedAtFreeze:
    def test_the_real_incident_shape_is_refused(self):
        """加了问题号前缀 —— 正是 2026-08-19 那次的形态。"""
        state = _FakeState(_log({
            "Q1_DISCOURSE_EVIDENCE": {"status": "discharged", "evidence": ["doi:10.1/x"]},
            "Q1_OBJECTIVE_LEVEL_TEST": {"status": "discharged", "evidence": ["doi:10.1/y"]},
        }))
        result = audit_observation_log(state, "log1")
        assert result["passed"] is False
        assert "unknown_closure_keys" in result["reasons"]

    def test_the_error_lists_every_legal_key(self):
        """报错要能直接照着改 —— 只说"键不对"等于让它再猜一次。"""
        state = _FakeState(_log({"Q1_DISCOURSE_EVIDENCE": {"status": "discharged",
                                                           "evidence": ["doi:10.1/x"]}}))
        reason = audit_observation_log(state, "log1")["reasons"]["unknown_closure_keys"]
        assert "DISCOURSE_EVIDENCE" in reason
        assert "OBJECTIVE_LEVEL_TEST" in reason      # 连没用到的合法键也要列
        assert "Q1_DISCOURSE_EVIDENCE" in reason     # 并点名错在哪个

    def test_verbatim_keys_pass(self):
        state = _FakeState(_log({
            "DISCOURSE_EVIDENCE": {"status": "discharged", "evidence": ["doi:10.1/x"]},
            "OBJECTIVE_LEVEL_TEST": {"status": "failed", "evidence": ""},
        }))
        assert audit_observation_log(state, "log1")["passed"] is True

    def test_freeze_is_blocked_by_a_bad_key(self):
        """闸放在冻结这一步：冻完就不可改了，那时才发现只能追回。"""
        state = _FakeState(_log({"Q1_TIMELINE": {"status": "discharged",
                                                 "evidence": ["doi:10.1/x"]}}))
        result = asyncio.run(_freeze_observation_log(state, "log1"))
        assert result["status"] == "error"
        assert "unknown_closure_keys" in result["reasons"]


class TestTheGateNeverBlocksWhatItCannotJudge:
    def test_no_frozen_prereg_means_no_key_check(self):
        """读不到合法键 ≠ 键错了。

        非 Project 独立运行、协议尚未冻结时都可能读不到。往"拦住"方向兜底，
        会把一次读盘失败变成"这个节点交不了差"。
        """
        class _NoPrereg(_FakeState):
            def list_artifacts(self, artifact_type=None):
                items = [{"id": "log1", "type": "observation_log"}]
                return [i for i in items if not artifact_type or i["type"] == artifact_type]

        state = _NoPrereg(_log({"WHATEVER_KEY": {"status": "discharged",
                                                 "evidence": ["doi:10.1/x"]}}))
        assert audit_observation_log(state, "log1")["passed"] is True

    def test_an_empty_discharge_block_is_not_a_key_error(self):
        """没勾账的 run（比如只交清单）不该被键校验碰。"""
        state = _FakeState(_log({}))
        result = audit_observation_log(state, "log1")
        assert "unknown_closure_keys" not in result["reasons"]

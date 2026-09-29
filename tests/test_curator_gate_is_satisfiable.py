"""curator 整合门禁必须**可满足** —— 2026-08-19 死锁的回归。

事故：`integration_targets_actually_scanned` 要求传入的每个 artifact 都被
`scan_artifact_disagreements` 真正扫到。卡住它的是
`compression_log__compression_turn_21` —— summarizer 自动落盘的压缩日志，
而那个工具结构上扫不到它（它扫的是 claim 分歧）。

于是：**框架把一个自己扫不到的东西列成目标，再用一道 fail-closed 的门禁卡住
"这个目标没被扫"。**`n_unintegrated` 恒为 1，curator_state 恒 pending，下游永远
被拦，人点 PROCEED 点三次都解不开 —— 因为答案根本不在人手里。

一道 fail-closed 的门禁必须能回答"怎样才能过"，且那个答案必须可达。这里钉住的
就是这条：判据只看**该整合的东西**，而"该不该整合"从声明推导，不看调用方传了什么。
"""
from nodes._curator.hooks import build_integration_receipt


class _FakeState:
    hook_state: dict = {}

    def list_artifacts(self, *a, **k):
        return []


def _receipt(artifact_ids, tool_records=None):
    return build_integration_receipt(
        _FakeState(),
        {"mode": "integration", "artifact_ids": artifact_ids},
        tool_records=tool_records or [],
    )


def test_a_machine_byproduct_cannot_block_the_gate():
    """压缩日志混进清单 → 不该被算成"没扫到"。"""
    r = _receipt(["compression_log__compression_turn_21"])

    assert r["n_unintegrated"] == 0, (
        "运行时内务被算成未整合目标 = 造出一个人工也解不开的死锁"
    )
    assert r["verdict"] == "ok_nothing_to_integrate"
    # 排除了什么必须看得见 —— 静默吞掉会让调用方以为它整合过了。
    assert r["ignored_non_integrable"] == ["compression_log__compression_turn_21"]


def test_a_real_deliverable_still_has_to_be_scanned():
    """别把门拆了：真交付物没扫到，仍然拦。"""
    r = _receipt(["hypothesis_conclusion_audit__Conclusion_Audit"])

    assert r["n_unintegrated"] == 1
    assert r["verdict"] == "incomplete_scan"


def test_a_mixed_list_only_counts_the_deliverables():
    """混着传：只对真交付物计数，内务不参与判定。"""
    real = "hypothesis_conclusion_audit__Conclusion_Audit"
    r = _receipt([real, "compression_log__compression_turn_9"], tool_records=[
        {"name": "read_artifact", "args": {"artifact_id": real}, "result": {"status": "success"}},
        {"name": "scan_artifact_disagreements", "args": {},
         "result": {"status": "success", "scanned_artifacts": [real]}},
    ])

    assert r["n_unintegrated"] == 0
    assert r["target_artifact_ids"] == [real]
    assert r["ignored_non_integrable"] == ["compression_log__compression_turn_9"]


def test_an_empty_call_is_still_a_failure():
    """真的什么都没传 —— 那仍然是不可放行的（#229，别把它和 no-op 混为一谈）。"""
    r = _receipt([])

    assert r["n_unintegrated"] == 1
    assert r["verdict"] == "empty_targets"

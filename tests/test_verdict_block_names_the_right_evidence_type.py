"""裁决权拒绝文案里的证据类型，必须是**这个节点产得出来的**那一个。

## 现场

三处报错原来硬编码 `experiment_log`：

    "把测量与理由写进 experiment_log，由 Analysis 出新一版 research_state"

对 observation / derivation 来说那是它们**产不出来的类型** —— 报错把调用方
指向一个它够不着的东西，等于让它去做一件做不到的事。
契约要送到调用方，而且得是**对的那一份**。

## 判据

从两处现算的交集来：节点 harness 声明的 required_output_artifact_types
∩ 注册表里的 evidence_record 类型。**新模态一声明就自动正确** ——
护栏要扫盘，不要写名单。

取不到退回泛称："报错降级成不那么精确"可以接受，"指向错误的类型"不行。
"""
from __future__ import annotations

from core.bootstrap import bootstrap
from core.verdict_authority import _evidence_record_type_of


def test_each_evidence_modality_gets_its_own_type():
    bootstrap()
    assert _evidence_record_type_of("experiment") == "experiment_log"
    assert _evidence_record_type_of("observation") == "observation_log"
    assert _evidence_record_type_of("derivation") == "derivation_log"


def test_non_evidence_nodes_fall_back_to_a_generic_phrase():
    """hypothesis / literature 不产证据记录 —— 不该被指向任何具体类型。"""
    bootstrap()
    for node in ("hypothesis", "literature"):
        got = _evidence_record_type_of(node)
        assert got == "你的证据记录", f"{node} 被指向了 {got}"


def test_unknown_or_missing_node_does_not_crash():
    """取不到就泛称 —— 一次读盘失败不该变成"这个节点交不了差"。"""
    bootstrap()
    for node in (None, "", "nonexistent_node"):
        assert _evidence_record_type_of(node) == "你的证据记录"


def test_no_hardcoded_experiment_log_remains_in_the_messages():
    """★ 扫盘：源码里不许再有写死的 experiment_log 文案。

    这条守的是"下一个模态加进来时不会重蹈覆辙" —— 判据是源码里没有那个
    字面量，不是"我现在改对了三处"。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "core" / "verdict_authority.py"
    text = src.read_text(encoding="utf-8")
    # 注释里说明历史可以留（那是解释），f-string 文案里不行
    offenders = [line.strip() for line in text.splitlines()
                 if "experiment_log" in line
                 and line.strip().startswith(("f\"", "f'", '"', "'"))]
    assert not offenders, "报错文案里还有写死的 experiment_log：\n" + "\n".join(offenders)

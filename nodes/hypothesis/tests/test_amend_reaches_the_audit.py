"""回放 2026-08-19 卡死 run：补上缺的判据之后，审计必须看到修订版。

现场（英国饮食 run 306cd643/6148bf80，当时的 .versions/ 快照里 v2–v5 五个版本）：
四条定性判据里一条缺可观察判据（criterion）。节点修了五次，五次都失败 ——
审计回放 transcript 拿**内容**当身份去重，而修复动作恰好改内容：改一次多
一条"新承诺"，旧版永远在。KB 里躺着修好的那版（claim_ba58e91fcfb4），
没人读。

新契约下这个死锁**无法表达**：承诺住在预注册 head，amend 出新 head，审计
读 head。旧版不是"被去重掉"，是**根本不在任何被读的位置**。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.state import State
from nodes.hypothesis.committed import committed_falsifiers
from nodes.hypothesis.tools.threshold_grounding import _audit_threshold_grounding


def _make_state(tmp_path: Path) -> State:
    return State.new(node_type="hypothesis", base_dir=tmp_path)


def _prereg(criteria_by_label: dict[str, str | None]) -> str:
    """复刻现场形状：四条 qualitative 判据，criterion 可缺。"""
    hyps = []
    for label, criterion in criteria_by_label.items():
        item: dict = {"label": label, "claim_text": f"{label} 的命题",
                      "falsification_criteria_structured": {
                          "metric": label.lower(),
                          "comparison": "qualitative",
                      }}
        if criterion:
            item["falsification_criteria_structured"]["criterion"] = criterion
        hyps.append(item)
    return ("## Inquiry Contract\n\n```json\n"
            + json.dumps({"hypotheses": hyps}, ensure_ascii=False)
            + "\n```\n")


_CRITERION = "若 1950 年代家庭日记中匮乏提及频率不高于战前，则该条不成立"


def test_the_stuck_run_shape_fails_once_and_heals_by_amend(tmp_path: Path) -> None:
    state = _make_state(tmp_path)

    # v1：四条定性判据，一条缺 criterion —— 审计必须抓住（判据本身是对的）
    saved = state.save_artifact("pre_registration", "P", _prereg({
        "Q1": None,
        "Q2": "若战后食谱数量未显著多于战前，则该条不成立",
        "Q3": "若移民菜系渗透率在档案中无上升趋势，则该条不成立",
        "Q4": "若营养指标未随配给制结束回升，则该条不成立",
    }))
    state.mark_frozen(saved["id"])      # 冻结是账本上的一行，不是 metadata 里的一个键
    first = asyncio.run(_audit_threshold_grounding(state))
    assert first["passed"] is False
    assert first["n_qualitative"] == 4 and first["n_numeric"] == 0
    # 报错必须指向真正触发的模式：这里没有任何数值阈值，不许再喊"拍脑袋数字"
    assert "定性/存在性判据缺失" in first["reason"]
    assert "数值阈值缺依据" not in first["reason"]

    # 修复 = amend 出新 head（不解冻、不绕 KB、不另起新条目）
    state.save_artifact("pre_registration", "P", _prereg({
        "Q1": _CRITERION,
        "Q2": "若战后食谱数量未显著多于战前，则该条不成立",
        "Q3": "若移民菜系渗透率在档案中无上升趋势，则该条不成立",
        "Q4": "若营养指标未随配给制结束回升，则该条不成立",
    }), amendment_reason="补 Q1 的可观察判据")

    # 审计与修订看同一份 head：一次 amend，立即变绿
    second = asyncio.run(_audit_threshold_grounding(state))
    assert second["passed"] is True, second["reason"]

    # 死锁的引擎不存在了：承诺集里就是 4 条，没有"旧版第五条"
    falsifiers = committed_falsifiers(state)
    assert len(falsifiers) == 4
    criteria = [f.get("criterion") for f in falsifiers if f.get("metric") == "q1"]
    assert criteria == [_CRITERION]


def test_the_bypass_that_created_the_kb_orphan_is_closed(tmp_path: Path) -> None:
    """现场的另一半：修好的那版当时被写进了 KB（claim_ba58e91fcfb4，
    provisional、无修订语义、审计不读）。这条路必须是关死的。"""
    from shared.tools.library.kb import _create_claim

    state = _make_state(tmp_path)
    result = asyncio.run(_create_claim(
        state, claim_text="修好的承诺", claim_type="hypothesis",
        falsification_criteria_structured={"metric": "q1", "comparison": "qualitative",
                                           "criterion": _CRITERION},
    ))
    # 第三波（一审 O1）：「hypothesis 节点不许写 claim」的角色闸删。现在挡住这条
    # 路的是身份契约（C 类，呈裁 b 判 keep）：hypothesis 类 claim 必须带 hypothesis_id
    # 作为原地更新的身份锚——承诺仍只能经预注册进来，但拒绝的是契约不成形，不是身份。
    assert result["status"] == "error"
    assert "hypothesis_id" in result["error"]

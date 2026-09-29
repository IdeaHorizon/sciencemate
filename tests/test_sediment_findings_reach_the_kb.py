"""写下「claim: …」不等于它进了 KB —— 缺口要在产生的那一刻就吵。

## 病例（2026-08-22 验尸）

一个跑了 6.5 小时、Q1-Q4 全部裁决完成的课题，`kb_claims.jsonl` **文件都不
存在**：全程 `create_claim` 调用次数为 0。而科学发现确实产出了 —— 它们以散文
形式躺在 experiment_log 的 `## Sediment（本 run 沉淀的发现，供 curator）`
一节里：

    - **claim**: 传统英国菜营养品质分维度非对称。证据：Gibson & Ashwell 2011…

读起来像记录了一条 claim，其实只是文本。curator 三次 dreaming 读了
112/102/144 次文件、烧掉 48.9M token，一条都没搬进 KB。

六小时后在 writing 那里显形：论文要引用 KB claim，KB 里一条都没有，模型于是
**编了两个 id**（claim_f85f85f81d / claim_ff99cef6502，全盘不存在），被引用
完整性检查拦下。报错指向「引用了不存在的 claim」，而真正的断点在六小时之外。

## 现在的契约（wangd 拍板，2026-08-22）

发现 → proposal → curator，三段分工：产出节点只写清楚发现（Sediment 段）；
**框架在 freeze 时把每条发现机械搬进 proposal 收件箱**（target = 刚登记的
frozen chunk，证据锚点天然在）；curator 是 KB claim 的唯一写入口，审 proposal
决定收不收。生产方零 schema 学习成本，语义判断归 curator。

这组测试守搬运的机械部分：条数对、进了收件箱、锚对了 chunk、失败不拖垮
freeze、没搬全要吵。
"""
from __future__ import annotations

import asyncio

import pytest

from core.state import State

SEDIMENT_LOG = """\
## Verdict
- Q1 discharged

## Sediment（本 run 沉淀的发现，供 curator / Analysis）
- **claim**: 传统英国菜营养品质分维度非对称。证据：doi:10.1016/s1368980011000875
- **claim**: 传统版与商业版在加工度上存在系统差异。证据：doi:10.1017/s0007114524000096
"""


def _freeze(state: State, artifact_id: str) -> dict:
    from core.tool_registry import execute

    return asyncio.run(execute("freeze_artifact", state, artifact_id=artifact_id))


@pytest.fixture()
def experiment_state(tmp_path):
    from core.bootstrap import bootstrap

    bootstrap()
    return State.new(node_type="experiment", base_dir=tmp_path, project_id="sed1")


def test_sediment_findings_are_filed_as_proposals(experiment_state) -> None:
    state = experiment_state
    state.save_artifact(
        artifact_type="experiment_log", name="ledger",
        content=SEDIMENT_LOG, metadata={},
    )
    result = _freeze(state, "experiment_log__ledger")

    assert result["status"] == "success", "搬运不许影响 freeze"
    assert result.get("sediment_findings") == 2
    assert result.get("sediment_proposals_filed") == 2, (
        f"两条发现都该进收件箱，实际 {result.get('sediment_proposals_filed')}；"
        f"hint={result.get('sediment_hint')}")

    from shared.tools.library.proposals import _list_proposals
    import asyncio as _a
    listing = _a.run(_list_proposals(state, status="pending"))
    cands = [p for p in (listing.get("proposals") or [])
             if p.get("proposal_type") == "kb_claim_candidate"]
    assert len(cands) == 2, f"收件箱里该有 2 条 kb_claim_candidate：{listing}"
    assert all(p.get("target_id") == result["chunk_id"] for p in cands), (
        "每条 proposal 必须锚在刚冻结登记的 chunk 上 —— 证据链从这来")
    assert any("非对称" in str(p.get("proposed_action")) for p in cands), (
        "发现原文要进 proposed_action，curator 靠它判断收不收")


def test_it_reports_but_never_blocks(experiment_state) -> None:
    """freeze 已落盘且不可逆；「该不该立 claim」是科学判断，不归框架判决。"""
    state = experiment_state
    state.save_artifact(
        artifact_type="experiment_log", name="ledger2",
        content=SEDIMENT_LOG, metadata={},
    )
    result = _freeze(state, "experiment_log__ledger2")

    assert result["status"] == "success"
    assert "error" not in result


def test_no_sediment_section_means_nothing_to_say(experiment_state) -> None:
    state = experiment_state
    state.save_artifact(
        artifact_type="experiment_log", name="plain",
        content="## Verdict\n- Q1 discharged\n", metadata={},
    )
    result = _freeze(state, "experiment_log__plain")

    assert "sediment_findings_not_in_kb" not in result


def test_filing_failure_is_loud_but_never_fails_the_freeze(experiment_state, monkeypatch) -> None:
    """搬运挂了：freeze 照常成功，但必须吵 —— 静默少搬比报错难查得多。"""
    state = experiment_state
    state.save_artifact(
        artifact_type="experiment_log", name="ledger3",
        content=SEDIMENT_LOG, metadata={},
    )

    async def _explode(*_a, **_k):
        raise RuntimeError("收件箱挂了")

    monkeypatch.setattr("shared.tools.library.proposals._propose", _explode)
    result = _freeze(state, "experiment_log__ledger3")

    assert result["status"] == "success", "搬运失败不许拖垮 freeze"
    assert result.get("sediment_proposals_filed") == 0
    assert "sediment_hint" in result, "没搬全必须吵，不许静默"

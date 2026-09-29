"""写入面执行类型上声明的契约 —— 引用诚信这一刀。

## 为什么在写入面

`cites_kb_claims: True` 这个能力早就声明在类型上（artifact_policy 的注释原文：
"正文会引用 KB claim id，因此必须过引用诚信闸"），只是执法点被放在三跳之外：
写产物 → turn 末 hook 扫 → 落 transcript 事件 → QC 读事件。

三跳每一跳都可能断（hook 没跑、事件被压缩掉、QC 判据改了），而且最后那跳什么
也决定不了（2026-08-10 起 QC 不判 run 死活）。声明在类型上的契约，就该在类型的
写入口执行。

## metadata 必需字段的测试为什么不在这里了（2026-08-23）

`required_metadata` / `present_but_may_be_empty` 整层已删。它们是**模式盲**的
类型级近似，而 observation / derivation 的 metadata 契约本来就取决于 `mode` ——
近似必然对至少一种模式是错的（实测三例，见 artifact_policy 的删除说明）。

契约的唯一声明权收回到类型 owner 的契约模块，由**可注册的写入门**执行。
对应的测试跟着判据走：
  · tests/test_observation_defends_against_cherry_picking.py
  · nodes/derivation/tests/test_derivation_save_gate.py
"""
from __future__ import annotations

import asyncio

from shared.tools.builtin import _save_artifact


class _State:
    """save 之前就该被拦下 —— 拦不下会 AttributeError，那也是一种失败信号。"""


def _save(**kw):
    return asyncio.run(_save_artifact(_State(), content="c", **kw))


# ── 引用诚信：同一把刀的第二刀 ──────────────────────────────────────────

class _KBState:
    """带一个假 KB 的 state —— 只够 find_phantom_citations 用。"""

    node_type = "writing"

    def __init__(self, known: set[str]):
        self._known = known

    def read_artifact(self, aid):        # pragma: no cover - 本测试不走这条
        return None

    def list_artifacts(self, *a, **k):
        return []


def test_a_phantom_citation_is_written_and_recorded(monkeypatch, tmp_path):
    """引一个 KB 里不存在的 claim → 照写，但 metadata.phantom_claim_ids 如实记下。

    检测仍在写入口（三跳之外的 hook/QC 链每一跳都可能断）；判决不在（判决拆除
    第三波，builtin:237 降格）：放行后 KB 一字不变，产物带着框架观测的幽灵清单，
    引用完整性由 referee 终审（宪法 S2：「引用清单不一致，记录即可」）。
    墙加回去这条转红。
    """
    import json

    import shared.tools.builtin as b
    from core.state import State

    monkeypatch.setattr(
        b, "_phantom_citations",
        lambda state, atype, content: ["claim_deadbeef"] if atype == "manuscript" else [])

    st = State.new(node_type="writing", base_dir=tmp_path / "runs", project_id="p_ph")
    out = asyncio.run(_save_artifact(
        st, artifact_type="manuscript", name="m", content="见 claim_deadbeef",
        metadata={"phantom_claim_ids": ["模型自述不算数"]}))
    assert out["status"] == "success", out
    rec = st.read_artifact(out["id"])
    assert rec["metadata"]["phantom_claim_ids"] == ["claim_deadbeef"]   # 框架观测盖过自述
    events = [json.loads(l) for l in st.transcript_path.read_text(encoding="utf-8").splitlines()]
    hit = [e for e in events if e.get("event") == "phantom_citations_recorded"]
    assert hit and hit[-1]["phantom_claim_ids"] == ["claim_deadbeef"]


def test_a_type_that_does_not_cite_kb_is_unaffected():
    """只对声明了 cites_kb_claims 的类型生效 —— 判据是声明，不是名单。"""
    from shared.lib.artifact_policy import cites_kb_claims
    import shared.tools.builtin as b

    assert cites_kb_claims("manuscript") and not cites_kb_claims("compression_log")
    assert b._phantom_citations(_KBState(set()), "compression_log", "claim_deadbeef 引了") == []


def test_a_broken_validator_does_not_kill_the_write():
    """判定层自己崩了不该把写入判死 —— 证据可持久化，判决不由这里做出。"""
    import shared.tools.builtin as b

    class _Boom:
        node_type = "writing"

        def list_artifacts(self, *a, **k):
            raise RuntimeError("KB 读不到")

    assert b._phantom_citations(_Boom(), "manuscript", "claim_abc") == []

"""derivation_log 的**写入门**：结构纪律站在必经之路上。

## 这些测试为什么存在

2026-08-23 benchmark 真跑（damped_resonance_width，A1 臂）的事故：

  · 模型**真的**调了 9 次 check_step，每一步都验过
  · 却把 step.verification 写成字符串 `"verified"`，把工具返回的验证章丢了
  · 而且发明了自己的 schema：用 `statement` 而不是 `claim`、把 `tool` 平铺在
    step 上、完全没有 `justification`
  · 然后 save_artifact 两次、**从头到尾没调 freeze_artifact**
  · run 判 `completed`，产物带着伪造的验证章交付

冻结门查的正是这件事，判据一个字都没写错 —— **它只是一次都没跑**。
把类型的结构纪律挂在"模型自愿调用的动作"上，等于留了一条
"不冻结就什么都不查"的近路（闸放哪层，按**对手是谁**推）。

## 判据落在哪

**必须走真入口 `_save_artifact`**，不是直接调门函数 —— 要验的恰恰是
「builtin 里那行接线真的在」。直接调门函数的测试，在接线被撤掉之后依然全绿
（查名字出现 ≠ 查接线）。

最后一条是**变异检验**：把门从注册表里摘掉，同一份产物必须写得出来。
它不转红，就说明上面那些测试根本不是这道门挡的，而是别的机制顺手挡的。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.bootstrap import bootstrap  # noqa: E402
from core.state import State  # noqa: E402
from shared.tools.builtin import _save_artifact  # noqa: E402
from shared.tools.library import derivation_check as D  # noqa: E402


@pytest.fixture
def state(tmp_path) -> State:
    bootstrap()          # 注册工具 + import 节点 tools（写入门在那里注册）
    import nodes.derivation.tools.derivation_contract  # noqa: F401
    return State(run_id="r-save-gate", node_type="derivation",
                 root=tmp_path / "run")


def _metadata(steps: list) -> dict:
    """一份 confirmatory 的完整 metadata —— 隔离出"这道门只查链结构"。

    2026-08-23 起 metadata 契约由本节点的写入门自己执行（`audit_record_shape`），
    不再有 `artifact_policy.required_metadata` 那一层：它是模式盲的，把
    credibility / verdicts / counterexample_search 判成全模式必需，而本文件的
    冻结门明写着它们只有 confirmatory 才欠 —— 一趟合法的 exploratory 推导
    因此连产物都存不下来。
    """
    return {
        "mode": "confirmatory",
        "steps": steps,
        "assumptions": [],
        "credibility": "主结果符号确证。",
        "verdicts": {"P1": "derived"},
        "counterexample_search": {"budget": 400, "found": False},
        "findings": [{"statement": "无"}],
        "main_result": {"statement": "完全平方展开", "expression": "a**2+2*a*b+b**2"},
    }


def _save(state, steps: list) -> dict:
    return asyncio.run(_save_artifact(
        state, "derivation_log", "t", content="正文",
        metadata=_metadata(steps)))


# ── 事故现场：字符串验证章 ──────────────────────────────────────────────────

#: 2026-08-23 那份产物里 step 的**真实形状**（逐字段照抄，不是我编的）。
#: 「自造样本只证明我的理解自洽」—— 这一条必须长得跟真事故一模一样。
_INCIDENT_STEP = {
    "id": "S1",
    "statement": "稳态复振幅 X = F/(w0^2 - w^2 + i*gamma*w)",
    "verification": "verified",
    "tool": "check_step",
}


#: 伪造的**结构化**验证章：dict 形状合法、status=verified、工具名是编的。
#: 这是 B（伪造章）——判决拆除后仍拦死；与下面被降格的「字符串自述」分开钉。
_FORGED_STEP = {
    "id": "S1",
    "claim": "稳态复振幅 X = F/(w0^2 - w^2 + i*gamma*w)",
    "justification": "algebra",
    "verification": {"method": "symbolic", "status": "verified",
                     "tool": "my_own_checker"},
}


def test_the_incident_shape_is_recorded_as_unverified(state):
    """判决拆除批 3w（deriv 248 降格）：真事故形状（手抄 "verified" 字符串）
    照存盘——但那个字符串**从不被当作验证章**：框架把这一步按 unverified
    如实入账，未过项写进 metadata.advisories。账真（S1）由「读章的唯一实现
    只认 dict + 工具署名 + 账本 probe」承载，而不是拒绝存盘。"""
    result = _save(state, [_INCIDENT_STEP])
    assert result["status"] == "success", result.get("error")
    record = state.read_artifact("derivation_log__t")
    assert record is not None
    advisories = record["metadata"].get("advisories") or {}
    assert "steps_structure" in advisories, "未验状态必须如实写进产物 metadata"


def test_a_forged_structured_stamp_still_cannot_be_written(state):
    """B 保留：dict 形状的伪造章（未注册工具署名）仍拒——报错说清出路。"""
    result = _save(state, [_FORGED_STEP])
    assert result["status"] == "error", "伪造的结构化验证章被写进去了"
    assert "steps_structure" in result["reasons"]
    assert state.read_artifact("derivation_log__t") is None
    hint = result["hint"]
    assert "原样贴" in hint
    assert "probe" in hint
    assert "未验不是罪" in hint, "要写明未验是合法的，否则模型会去给每一步编个章"


def test_a_real_verification_block_passes(state):
    """真调一次工具、把返回的验证章原样贴进来 —— 写得出去。

    这里**真的调 check_step**（不是手搓一个像样的 dict）：账本里因此有这次
    调用，probe 核对才走的是真路径。
    """
    verified = asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))
    step = {"id": "S1", "claim": "(a+b)^2 = a^2+2ab+b^2",
            "justification": "algebra",
            "verification": verified["verification"]}
    result = _save(state, [step])
    assert result["status"] == "success", result.get("error")


def test_an_honestly_unverified_step_passes(state):
    """未验不是罪：标成 cited_theorem / prose / definition 就能写。

    判据错在这一侧的代价更大 —— 模型会学会给每一步编一个章。
    """
    step = {"id": "S1", "claim": "由 Parseval 定理",
            "justification": "cited_theorem"}
    assert _save(state, [step])["status"] == "success"


def test_the_contract_reaches_the_model_before_the_call(state):
    """契约要渲染进 save_artifact 的说明 —— 契约和拒绝是同一件事的两面。

    模型发明自己的 schema，正是因为唯一写着 step 该长什么样的地方是冻结门的
    **报错**，而那道报错在事故里从没触发过。
    """
    from core.tool_registry import _REGISTRY

    description = _REGISTRY.tools["save_artifact"].description or ""
    assert "derivation_log" in description
    assert "steps[].verification" in description


# ── 变异检验 ────────────────────────────────────────────────────────────────

def test_removing_the_gate_lets_the_forged_stamp_through(state):
    """★ 把门摘掉，伪造的结构化章必须写得出去。

    这一条转红（= 摘掉门之后**依然写不出去**），说明上面那些测试挡住东西的
    不是这道门，而是别的机制顺手挡的 —— 那些测试就等于没写。
    """
    from shared.tools.library.artifacts_extra import SAVE_GATES

    removed = SAVE_GATES.pop("derivation_log", None)
    assert removed is not None, "门根本没注册"
    try:
        result = _save(state, [_FORGED_STEP])
        assert result["status"] == "success", (
            "摘掉写入门之后伪造章仍然写不出去 —— "
            "那么拦住它的是别的机制，上面的测试测的不是这道门")
    finally:
        SAVE_GATES["derivation_log"] = removed


def test_the_gate_is_scoped_to_its_own_type(state):
    """别的类型不受影响 —— 门跟类型走，不是给 save_artifact 加了道全局闸。"""
    result = asyncio.run(_save_artifact(
        state, "scratch_note", "n", content="随便",
        metadata={"steps": [_INCIDENT_STEP]}))
    assert result["status"] == "success", (
        "derivation_log 的门拦了别的类型 —— 门的射程超出了它的对手")


# ── exploratory：这道门不许把"按设计不做的事"算成缺 ──────────────────────────

def test_an_exploratory_derivation_can_be_written(state):
    """exploratory=找猜想找形式，按设计**不勾账、不裁决** —— 它得能存下来。

    2026-08-23 之前存不下来：`required_metadata` 把 credibility / verdicts /
    counterexample_search 判成全模式必需，而本节点冻结门把它们放在
    confirmatory-only。**框架先禁止模型做某件事，再罚它没做**，模型唯一的出路
    是编一份裁决 —— 闸反过来制造它要防的行为（同 `assumptions: []` 那例）。
    """
    out = asyncio.run(_save_artifact(
        state, "derivation_log", "e", content="正文",
        metadata={"mode": "exploratory",
                  "steps": [{"id": "S1", "claim": "由 Parseval 定理",
                             "justification": "cited_theorem"}],
                  "assumptions": [], "findings": []}))
    assert out["status"] == "success", out.get("error")


def test_an_exploratory_run_still_may_not_discharge(state):
    """anti-HARKing 不能只长在冻结门上。

    `core.prereg_commitments._scan_result_metadata` 收兑现记录时**不看产物冻没冻**,
    按类型扫全部产物的 metadata —— 写了勾账、永不冻结，那些勾账照样进账本。
    """
    out = asyncio.run(_save_artifact(
        state, "derivation_log", "e2", content="正文",
        metadata={"mode": "exploratory",
                  "steps": [{"id": "S1", "claim": "由 Parseval 定理",
                             "justification": "cited_theorem"}],
                  "assumptions": [], "findings": [],
                  "closure_discharges": {"Q1#1": {"status": "discharged"}}}))
    assert out["status"] == "error"
    assert "exploratory_cannot_close" in out["failed_checks"]


def test_a_missing_assumptions_key_is_surfaced_not_refused(state):
    """判决拆除批 3w（deriv 678 降格，D-OB1：save gate 一律不再拒存盘）：
    assumptions key 缺席照存盘，缺项如实写进 metadata.advisories。"""
    out = asyncio.run(_save_artifact(
        state, "derivation_log", "e3", content="正文",
        metadata={"mode": "exploratory",
                  "steps": [{"id": "S1", "claim": "由 Parseval 定理",
                             "justification": "cited_theorem"}],
                  "findings": []}))
    assert out["status"] == "success", out.get("error")
    record = state.read_artifact("derivation_log__e3")
    advisories = record["metadata"].get("advisories") or {}
    assert "assumptions" in advisories.get("derivation_record", "")

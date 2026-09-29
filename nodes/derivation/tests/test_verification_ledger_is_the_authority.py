"""验证章的判据是「真调过」，不是「工具名对」。

## 升级前的洞

冻结门第一版查的是 `verification.tool in TRUSTED_VERIFIERS` —— **只查名字**。
模型在 metadata 里写一句：

    {"method": "symbolic", "status": "verified", "tool": "check_step"}

就能过，一次工具也不用调。那道闸挡的是"随手编个工具名"，挡不住"照着合法
格式编一个章"。**报告不是事实** —— 判据必须落在"这件事真发生过"上。

## 现在

验证工具每次返回都在章里盖一个式子指纹（probe），同时往本 run 的 transcript
写一条同指纹的记录。冻结时反查账本：查无此项 = 没真调过。

## 这份测试的重心

三条伪造路径各自被堵（无 probe / 编 probe / 改结论），外加两条**不该误伤**
的：账本读不到时不拦（取证手段失灵 ≠ 伪造），补假设重验后以最后一次为准。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nodes.derivation.tools.derivation_contract import audit_derivation_log  # noqa: E402
from shared.lib import derivation_ledger as ledger  # noqa: E402
from shared.tools.library import derivation_check as D  # noqa: E402


class _StateWithLedger:
    """带真 transcript 文件的 state —— 账本读写都走真盘。"""

    def __init__(self, tmp_path: Path, record: dict):
        self.transcript_path = tmp_path / "transcript.jsonl"
        self.transcript_path.touch()
        self._record = record

    def append_transcript(self, event_type: str, **payload):
        line = json.dumps({"event": event_type, **payload}, ensure_ascii=False,
                          default=str)
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read_artifact(self, artifact_id):
        return self._record

    def list_artifacts(self):
        return []


class _StateWithoutLedger:
    """没有 transcript 的 state —— 账本读不到。"""

    def __init__(self, record):
        self._record = record

    def read_artifact(self, artifact_id):
        return self._record

    def append_transcript(self, *a, **kw):
        pass

    def list_artifacts(self):
        return []


def _metadata(steps):
    return {
        "mode": "confirmatory", "steps": steps, "assumptions": [],
        "credibility": "主结果经符号验证。", "verdicts": {"P1": "derived"},
        "counterexample_search": {"budget": 400, "found": False},
        "findings": [{"statement": "链已闭合"}],
        "main_result": {"expression": "a**2 + 2*a*b + b**2",
                        "statement": "完全平方展开式"},
    }


def _record(steps):
    return {"type": "derivation_log", "metadata": _metadata(steps)}


def _step(verification, step_id="S1"):
    return {"id": step_id, "claim": "(a+b)^2 = a^2+2ab+b^2",
            "justification": "algebraic", "verification": verification}


# ── 三条伪造路径 ────────────────────────────────────────────────────────────

def test_a_fabricated_certificate_without_probe_is_rejected(tmp_path):
    """伪造 1：格式完全合法的章，但没调过工具（没有 probe）。

    这正是升级前能溜过去的那一种。
    """
    forged = {"method": "symbolic", "status": "verified", "tool": "check_step"}
    state = _StateWithLedger(tmp_path, _record([_step(forged)]))
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    assert "probe" in audit["reasons"]["steps_structure"]


def test_an_invented_probe_is_rejected(tmp_path):
    """伪造 2：probe 是编的 —— 账本里查无此项。"""
    forged = {"method": "symbolic", "status": "verified", "tool": "check_step",
              "probe": "deadbeefdeadbeef"}
    state = _StateWithLedger(tmp_path, _record([_step(forged)]))
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    assert "查无此项" in audit["reasons"]["steps_structure"]


def test_upgrading_the_verdict_on_a_real_probe_is_rejected(tmp_path):
    """伪造 3：真调过工具（probe 真实），但把结论从 inconclusive 改成 verified。

    账本是权威 —— 章可以被改写，账本不能。
    """
    state = _StateWithLedger(tmp_path, _record([]))
    real = asyncio.run(D._check_step(state, "sin(x)*cos(y)", "tan(x)/sec(y)"))
    章 = dict(real["verification"])
    真实结论 = 章["status"]
    章["status"] = "verified"          # ← 篡改

    state._record = _record([_step(章)])
    audit = audit_derivation_log(state, "a1")
    if 真实结论 != "verified":
        assert audit["passed"] is False
        assert "以账本为准" in audit["reasons"]["steps_structure"]


# ── 两条不该误伤 ────────────────────────────────────────────────────────────

def test_a_real_certificate_passes(tmp_path):
    """真调工具、原样贴回 —— 必须通过。

    没有这条，上面三条测的可能只是"闸爱报错"。
    """
    state = _StateWithLedger(tmp_path, _record([]))
    real = asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))
    assert real["verification"]["status"] == "verified"
    assert real["verification"].get("probe"), "工具必须在章里盖指纹"

    state._record = _record([_step(real["verification"])])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]


def test_an_unreadable_ledger_does_not_block(tmp_path):
    """账本读不到时不拦 —— 取证手段失灵 ≠ 伪造。

    非 Project 独立运行、fixture 回放、transcript 还没落盘，都会读不到。
    拦了就是把一次读盘失败变成"这个节点交不了差"。
    """
    forged = {"method": "symbolic", "status": "verified", "tool": "check_step"}
    state = _StateWithoutLedger(_record([_step(forged)]))
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, (
        "账本不可读时这道闸必须让路 —— fail-closed 的代价落在诚实的 run 上")


def test_the_last_verdict_wins_after_re_verification(tmp_path):
    """补上假设重验之后，以**最后一次**为准。

    `sqrt(x**2) = x` 不声明适用域时 failed，声明 x>0 后 verified。
    账本按 probe 取最后一次 —— 否则模型永远无法通过补假设来修一步。
    """
    state = _StateWithLedger(tmp_path, _record([]))
    first = asyncio.run(D._check_step(state, "sqrt(x**2)", "x"))
    assert first["verification"]["status"] == "failed"

    second = asyncio.run(D._check_step(state, "sqrt(x**2)", "x",
                                       assumptions={"x": "positive"}))
    assert second["verification"]["status"] == "verified"
    # 两次是同一个式子 → 同一个 probe
    assert first["verification"]["probe"] == second["verification"]["probe"]

    table = ledger.by_probe(state)
    assert table[second["verification"]["probe"]]["status"] == "verified", (
        "账本必须取最后一次 —— 取第一次的话，补假设修好的步骤永远过不了闸")

    state._record = _record([_step(second["verification"])])
    # 注意：这一步现在挂着 x>0，assumptions 该记进账本 —— 但那是 §3 的判据，
    # 这里只验章与账本对得上。
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]


def test_probe_identifies_the_expression_not_the_math_fact():
    """指纹认的是"你验的是不是**这一行**"，不是"是不是这个数学事实"。

    两个数学上等价但写法不同的式子，指纹本就该不同 —— 否则"验了简化版、
    贴到完整版上"会被指纹意外放行，而那是 reviewer 该抓的事，
    机械层不该假装自己抓住了。
    """
    a = ledger.probe_id("(a+b)**2", "a**2+2*a*b+b**2")
    b = ledger.probe_id("(b+a)**2", "a**2+2*a*b+b**2")
    assert a != b
    assert a == ledger.probe_id(" (a+b)**2 ", " a**2+2*a*b+b**2 "), "只做 strip 规范化"

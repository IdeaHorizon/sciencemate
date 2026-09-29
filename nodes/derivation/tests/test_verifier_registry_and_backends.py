"""验证器注册表 + 两个新后端（区间算术 / Lean）。

## 这些测试在守什么

2026-08-23 查出来的事故：`TRUSTED_VERIFIERS` 是手写元组，里面躺着
`check_lean` —— 一个**从未实现**的工具；`KNOWN_METHODS` 里的 `interval`
与 `formal` 同样是空位子。

后果不是"少个功能"，是门上多了个后门：`_audit_steps` 先查工具名在不在白名单、
再拿 probe 去账本反查，而账本读不到时后一段刻意跳过（取证失灵 ≠ 伪造）。
于是白名单里一个永远不会写账本的名字，成了只在降级路径上生效的近路。

判据因此改成：**有资格署名 ⟺ 这个工具真的注册了自己会往账本写**。

## 两个后端的语义边界（比"能跑"重要得多）

- `interval_check` 只能**严格证否**。区间算术是外包围，差值区间含 0
  什么都证明不了 —— 实测 sin²+cos²−1 在 [0.4,1] 上算出 [±0.732]，
  而它数学上恒等于 0。把 contains(0) 当验证通过，就是拿不严格的判据
  冒充严格，那正是这个节点的原罪。
- `check_lean` 是唯一的严格证成，但**信任边界在陈述不在证明**，
  而且 `sorry` 占位符必须被识别成"没证"。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.bootstrap import bootstrap  # noqa: E402
from shared.lib import verifier_registry as R  # noqa: E402
from shared.tools.library import derivation_check as D  # noqa: E402


@pytest.fixture(autouse=True)
def _boot():
    bootstrap()


class _State:
    def __init__(self, tmp_path: Path):
        self.transcript_path = tmp_path / "transcript.jsonl"
        self.transcript_path.touch()
        self.hook_state: dict = {}

    def append_transcript(self, event_type: str, **payload):
        import json

        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event_type, **payload},
                               ensure_ascii=False, default=str) + "\n")


# ── 注册表 ──────────────────────────────────────────────────────────────────

def test_the_whitelist_is_derived_not_handwritten():
    """★ 白名单必须来自注册表，且**只含真的会写账本的工具**。

    判据不是"名单里有几个"，是"名单里的每一个都能在本进程里找到实现"。
    """
    from core.tool_registry import _REGISTRY

    for name in R.trusted_verifiers():
        assert name in _REGISTRY.tools, (
            f"{name} 在验证器白名单里，但 registry 里没有这个工具 —— "
            f"这正是 2026-08-23 的后门形态：门认一个不存在的署名")


def test_methods_come_from_registered_verifiers():
    """method 清单是各验证器声明的并集 + 不需要工具的 none。"""
    methods = set(R.known_methods())
    assert "none" in methods, "未验是合法状态，它不依赖任何工具"
    assert "symbolic" in methods and "numeric" in methods
    union = {m for name in R.trusted_verifiers() for m in R.methods_of(name)}
    assert methods == union | {"none"}, (
        "method 清单与验证器声明对不上 —— 又是一份会各自演化的抄件")


def test_an_unregistered_tool_name_is_refused():
    """★ 手写一个没注册的工具名，必须当场被拒。

    这是后门的直接回归：以前 `check_lean` 只是名单里的一个字符串，
    模型手写它能溜过第一道判据。
    """
    from nodes.derivation.tools.derivation_contract import _audit_steps

    problems, _, _, _ = _audit_steps([{
        "id": "S1", "claim": "x = x", "justification": "algebra",
        "verification": {"tool": "check_by_vibes", "status": "verified",
                         "method": "symbolic", "probe": "deadbeef"},
    }])
    assert any("check_by_vibes" in p for p in problems)


def test_the_gate_lists_what_is_actually_available():
    """拒绝时要列出**本环境**可用的来源 —— 契约必须送到调用方。"""
    from nodes.derivation.tools.derivation_contract import _audit_steps

    problems, _, _, _ = _audit_steps([{
        "id": "S1", "claim": "x = x", "justification": "algebra",
        "verification": {"tool": "", "status": "verified", "method": "symbolic"},
    }])
    joined = "；".join(problems)
    assert "check_step" in joined


# ── 三个辅助工具现在会盖章（只在证否侧）──────────────────────────────────────

def test_dimensional_check_seals_only_when_it_refutes(tmp_path):
    """★ 量纲**错**盖章（强结论），量纲**对**不盖章（必要条件≠充分条件）。

    盖成 verified 就是语义膨胀：一个量纲正确、系数错一倍的式子照样通过，
    而模型会拿它当"这步验过了"。
    """
    state = _State(tmp_path)
    bad = asyncio.run(D._dimensional_check(
        state, "m*v", {"m": "kilogram", "v": "meter/second"}, "joule"))
    assert bad["matches_expected"] is False
    assert bad["verification"]["status"] == "failed"
    assert bad["verification"]["probe"], "证否的章也要有指纹，否则贴不进去"

    good = asyncio.run(D._dimensional_check(
        state, "m*v**2", {"m": "kilogram", "v": "meter/second"}, "joule"))
    assert good["matches_expected"] is True
    assert "verification" not in good, (
        "量纲对不构成对这一步的验证 —— 盖章就是语义膨胀")


def test_find_counterexample_hands_back_the_block(tmp_path):
    """找到反例是最强的结论，必须有合法的记录方式。

    此前这里把内部 check_step 盖好的章扔了，于是"找到反例"反而没法写进链。
    """
    state = _State(tmp_path)
    hit = asyncio.run(D._find_counterexample(state, "exp(x+y)", "exp(x)+exp(y)"))
    assert hit["found"] is True
    assert hit["verification"]["status"] == "failed"
    assert hit["verification"]["probe"]


# ── 区间算术 ────────────────────────────────────────────────────────────────

_HAS_FLINT = D._flint() is not None
_HAS_LEAN = D._lean_binary() is not None


@pytest.mark.skipif(not _HAS_FLINT, reason="本环境没有 python-flint")
def test_interval_check_refutes_strictly(tmp_path):
    """★ 差值区间不含 0 → 严格证否，且盖章。"""
    state = _State(tmp_path)
    out = asyncio.run(D._interval_check(
        state, "exp(x+y)", "exp(x)+exp(y)", {"x": [1.0, 1.01], "y": [1.0, 1.01]}))
    assert out["status"] == "success"
    assert out["contains_zero"] is False
    assert out["verification"]["status"] == "failed"
    assert out["verification"]["method"] == "interval"


@pytest.mark.skipif(not _HAS_FLINT, reason="本环境没有 python-flint")
def test_interval_check_never_claims_success(tmp_path):
    """★★ 最重要的一条：区间含 0 **绝不能**盖成验证通过。

    sin²+cos²−1 恒等于 0，区间算术在 [0.4,1.0] 上给出 [±0.732] ——
    含 0 既可能是真恒等，也可能只是包裹效应。把它当 verified，
    就是拿一个不严格的判据冒充严格。
    """
    state = _State(tmp_path)
    out = asyncio.run(D._interval_check(
        state, "sin(x)**2+cos(x)**2", "1", {"x": [0.4, 1.0]}))
    assert out["contains_zero"] is True
    assert "verification" not in out, "含 0 什么都没证明，绝不能盖章"
    assert "什么都没证明" in out["note"]


@pytest.mark.skipif(not _HAS_FLINT, reason="本环境没有 python-flint")
def test_interval_check_demands_a_domain(tmp_path):
    """不给区间就退化成点估值 —— 报错要说清怎么给。"""
    out = asyncio.run(D._interval_check(_State(tmp_path), "x", "x+1", {}))
    assert out["status"] == "error"
    assert "domain" in out["error"]


@pytest.mark.skipif(not _HAS_FLINT, reason="本环境没有 python-flint")
def test_the_dependency_problem_is_real_and_not_papered_over(tmp_path):
    """★ 区间太宽 → 证否**失败**，而工具必须诚实地不盖章。

    同一个变量出现多次时，区间算术把每次出现当独立变量处理（依赖问题），
    区间被严重放大。实测 exp(x+y) vs exp(x)+exp(y)：

        x,y ∈ [1, 2]     → [±49.2]     含 0，证否失败
        x,y ∈ [1, 1.01]  → [2±0.103]   严格证否

    两边差得很远（exp(3)≈20 vs 2e≈5.4），**数学上根本不含 0** ——
    但区间算术看不出来。这就是这个后端的真实能力边界，
    不能因为"我知道它其实不等"就把宽区间那次也当成证否。
    """
    state = _State(tmp_path)
    wide = asyncio.run(D._interval_check(
        state, "exp(x+y)", "exp(x)+exp(y)", {"x": [1.0, 2.0], "y": [1.0, 2.0]}))
    assert wide["contains_zero"] is True, "宽区间本就该含 0 —— 这是依赖问题"
    assert "verification" not in wide, "证否失败就不能盖章，哪怕我们知道它其实不等"
    assert "依赖问题" in wide["note"], "要告诉模型该怎么办：收窄区间"

    narrow = asyncio.run(D._interval_check(
        state, "exp(x+y)", "exp(x)+exp(y)", {"x": [1.0, 1.01], "y": [1.0, 1.01]}))
    assert narrow["contains_zero"] is False
    assert narrow["verification"]["status"] == "failed"


# ── Lean ────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_LEAN, reason="本环境没有 Lean 工具链")
def test_lean_accepts_a_real_proof(tmp_path):
    """★ 真的把命题交给 Lean 内核，通过了才盖 formal 章。"""
    state = _State(tmp_path)
    out = asyncio.run(D._check_lean(
        state, "∀ n : Nat, n + 0 = n", "fun n => rfl"))
    assert out["status"] == "success", out.get("error")
    assert out["exit_code"] == 0, out.get("lean_output")
    assert out["verification"]["status"] == "verified"
    assert out["verification"]["method"] == "formal"
    assert out["verification"]["formal_statement"], (
        "章里必须带形式化陈述原文 —— 信任边界在陈述不在证明")


@pytest.mark.skipif(not _HAS_LEAN, reason="本环境没有 Lean 工具链")
def test_lean_rejects_a_false_statement(tmp_path):
    """假命题证不出来，且**不盖章**。"""
    out = asyncio.run(D._check_lean(
        _State(tmp_path), "∀ n : Nat, n + 1 = n", "fun n => rfl"))
    assert out["exit_code"] != 0
    assert "verification" not in out


@pytest.mark.skipif(not _HAS_LEAN, reason="本环境没有 Lean 工具链")
def test_sorry_is_not_a_proof(tmp_path):
    """★ `sorry` 是占位符 —— Lean 以 0 退出，但那**等于没证**。

    不识别它，模型就有了一条"让 Lean 给我盖章"的零成本近路。
    """
    out = asyncio.run(D._check_lean(
        _State(tmp_path), "∀ n : Nat, n + 0 = n", "by sorry"))
    assert "verification" not in out, "带 sorry 的证明绝不能盖章"
    assert "sorry" in out["note"]


def test_capability_absence_removes_both_tool_and_signature():
    """★ 能力缺席就不给工具 —— 而且连署名资格一起收走。

    这条不依赖本机装没装：断言的是两者**同进同退**。
    留一个够不着的工具名，模型会围着它空转；而在这里更糟 ——
    门会把它当成合法的验证来源。
    """
    from core.tool_registry import _REGISTRY

    for name, available in (("interval_check", _HAS_FLINT),
                            ("check_lean", _HAS_LEAN)):
        in_tools = name in _REGISTRY.tools
        in_verifiers = name in R.trusted_verifiers()
        assert in_tools == in_verifiers == available, (
            f"{name}: 工具={in_tools} 署名资格={in_verifiers} 能力={available} "
            f"—— 三者必须一致")


# ── 病态点采样 ──────────────────────────────────────────────────────────────

def test_pathological_points_are_sampled_first(tmp_path):
    """★ 边界点（±1、极小量）必须被采到，而且排在最前面。

    随机采样天然采不到边界，而错误的等式最常在这里破裂。
    借鉴 math-rigor 的 pathological cases 要求。
    """
    state = _State(tmp_path)
    out = asyncio.run(D._check_step(state, "Abs(x)", "x"))
    assert out["verification"]["status"] == "failed"
    point = out["counterexample"]["point"]
    assert str(list(point.values())[0]).startswith("-1"), (
        "x=-1 是病态点，应该最先被检出")
    assert out["counterexample"]["pathological"] is True, (
        "反例来自病态点要标注 —— 读的人需要知道它是不是边界情形")


def test_catastrophic_cancellation_is_not_reported_as_a_counterexample(tmp_path):
    """★★ 相消误差**绝不能**被报成反例。

    `sqrt(1+x)-1` 在 x→0 时灾难性相消（有效位大量丢失），而它与
    `x/(sqrt(1+x)+1)` 是**恒等**的。病态点采样把 x=1e-12 送了进去 ——
    如果不做高精度复验，一个教科书恒等式就会被判 failed。

    2026-08-22 真跑里这类误判发生过一次（无穷几何级数在 x≈1.5e-7 处），
    代价是模型去"修"一个没错的东西。方向是死的：**宁可漏报一个真反例，
    也不能造一个假反例。**
    """
    state = _State(tmp_path)
    out = asyncio.run(D._check_step(
        state, "sqrt(1+x) - 1", "x/(sqrt(1+x)+1)"))
    assert out["verification"]["status"] != "failed", (
        f"恒等式被判成有反例：{out.get('counterexample')}")


def test_declared_assumptions_still_shape_pathological_points(tmp_path):
    """病态点也要尊重声明的约束 —— 否则它成了假反例的新来源。

    x>0 下成立的等式，拿 x=-1 去否定它是闸自己制造的反例。
    """
    state = _State(tmp_path)
    out = asyncio.run(D._check_step(
        state, "sqrt(x**2)", "x", assumptions={"x": "positive"}))
    assert out["verification"]["status"] != "failed", (
        "声明了 x>0 还拿负的病态点去否定 —— 假反例是最坏的一类错误")


# ── rigor_level：派发时的承诺要么兑现，要么如实降级 ──────────────────────────

class _StateWithInputs:
    """带 node_inputs 的 state —— 真实来源是 hook_state（见 executor）。"""

    def __init__(self, tmp_path: Path, **node_inputs):
        self.transcript_path = tmp_path / "transcript.jsonl"
        self.transcript_path.touch()
        self.hook_state: dict = {"node_inputs": dict(node_inputs)}
        self._record: dict = {}

    def append_transcript(self, *a, **kw):
        pass

    def read_artifact(self, artifact_id):
        return self._record

    def list_artifacts(self):
        return []


def _log_with(tmp_path, *, rigor, steps, credibility="主结果符号确证。",
              main_expression="C"):
    state = _StateWithInputs(tmp_path, rigor_level=rigor)
    state._record = {"type": "derivation_log", "metadata": {
        "mode": "confirmatory", "steps": steps, "assumptions": [],
        "credibility": credibility, "verdicts": {"P1": "derived"},
        "counterexample_search": {"budget": 400, "found": False},
        "findings": [{"statement": "无"}],
        "main_result": {"statement": "热容闭式解", "expression": main_expression},
    }}
    return state


def _step(method, claim="C = k_B*x**2*exp(x)/(exp(x)-1)**2"):
    return {"id": "S1", "claim": claim, "justification": "algebra",
            "verification": {"tool": "check_step", "status": "verified",
                             "method": method, "probe": "abc123"}}


def test_the_real_node_inputs_path_is_hook_state(tmp_path):
    """★ node_inputs 来自 hook_state，不是 State 的属性。

    第一版按直觉写了 `getattr(state, "node_inputs")` —— 那样这道闸永远读到空、
    永远不触发。今天已经在同形的坑里栽过一次（list_artifacts 不带 metadata）。
    """
    from nodes.derivation.tools.derivation_contract import _node_inputs

    state = _StateWithInputs(tmp_path, rigor_level="l1.5")
    assert _node_inputs(state).get("rigor_level") == "l1.5"


def test_an_unmet_rigor_promise_is_refused(tmp_path):
    """★ 要了 L1.5（区间认证），链上只有符号验证，credibility 又不交代 → 拒。

    2026-08-23 真跑：调度器认真选了 rigor_level="L1.5"，参数确实传进了节点，
    而整个代码库里没有一个地方读它 —— 那趟 6 条 numerically_supported 原样通过。
    调度器以为要到了区间认证，节点以为做完了，两边都不报错。
    """
    from nodes.derivation.tools.derivation_contract import audit_derivation_log

    state = _log_with(tmp_path, rigor="l1.5", steps=[_step("symbolic")])
    audit = audit_derivation_log(state, "x")
    assert audit["passed"] is False
    assert "rigor_promise" in audit["reasons"]
    reason = audit["reasons"]["rigor_promise"]
    assert "interval_check" in reason, "要说清怎么补 —— 契约必须送到调用方"
    assert "如实降级" in reason, "拿不到是合法的，必须给出这条出路"


def test_a_met_rigor_promise_passes(tmp_path):
    """真做了区间验证 → 放行。"""
    from nodes.derivation.tools.derivation_contract import audit_derivation_log

    state = _log_with(tmp_path, rigor="l1.5", steps=[_step("interval")])
    assert "rigor_promise" not in audit_derivation_log(state, "x")["reasons"]


def test_an_honestly_downgraded_promise_passes(tmp_path):
    """★ 拿不到就说出来 —— 如实降级必须放行。

    「未验不是罪，装作验过才是」在严格度上的同一条：闸要的是**说出来**，
    不是硬凑。不给这条出路，模型就会去给不适合区间验证的式子硬编一个 domain。
    """
    from nodes.derivation.tools.derivation_contract import audit_derivation_log

    # 判决拆除批 3w（516 一处修）：出口从 credibility 散文关键词改为
    # metadata.concessions 的结构化让步 —— 英文/任意措辞的诚实降级同样有效，
    # 空话再也骗不过（出口不得由关键词把守）。
    state = _log_with(tmp_path, rigor="l1.5", steps=[_step("symbolic")])
    state._record["metadata"]["concessions"] = [{
        "check": "rigor_promise",
        "achieved": "l1",
        "reason": ("requested L1.5 interval certification; main result carries "
                   "the symbolic parameter ω, so only L1 CAS verification was "
                   "attainable"),
    }]
    assert "rigor_promise" not in audit_derivation_log(state, "x")["reasons"]


def test_no_rigor_request_means_no_check(tmp_path):
    """没要求就不查 —— 闸的射程不该超出它的对手。"""
    from nodes.derivation.tools.derivation_contract import audit_derivation_log

    state = _log_with(tmp_path, rigor="", steps=[_step("symbolic")])
    assert "rigor_promise" not in audit_derivation_log(state, "x")["reasons"]

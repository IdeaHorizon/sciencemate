"""推导进度板：框架现算，不是模型自报。

## 这块板子的判据

板子的价值全在**它说的是不是当前真相**。一块永远挂着已经修好的东西的板子，
两轮之后就没人看了 —— 所以最重要的一条测试不是"failed 会显示"，
而是"**补上适用域重验之后 failed 会从板上消失**"。

体积也是判据的一部分：verified 的式子不上板（它们是历史，占板面不产生
决策），这与"局面只出计数与单行摘要"是同一条硬约束。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.bootstrap import bootstrap  # noqa: E402
from shared.tools.library import derivation_check as D  # noqa: E402


class _Ctx:
    """够 hook 用的最小 HookContext 替身。"""

    def __init__(self, state, turn=1):
        self.state = state
        self.turn = turn


class _State:
    def __init__(self, tmp_path: Path):
        self.transcript_path = tmp_path / "transcript.jsonl"
        self.transcript_path.touch()
        self.hook_state: dict = {}

    def append_transcript(self, event_type: str, **payload):
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event_type, **payload},
                               ensure_ascii=False, default=str) + "\n")


def _hooks():
    """拿到本节点注册的 hook（bootstrap 会 import nodes/derivation/hooks.py）。"""
    bootstrap()
    from core.loop_hooks import get_loop_hook

    hook = get_loop_hook("derivation_board")
    assert hook is not None, "derivation_board 没注册 —— bootstrap 没 import 到 hooks.py"
    return hook


def _inject(hook, state, turn=1) -> str:
    msgs = hook.on_turn_start(_Ctx(state, turn))
    return "\n".join(m.content for m in (msgs or []))


def test_empty_ledger_injects_nothing(tmp_path):
    """一次没验过 → 不占板面。空板子比没有板子更糟：它教模型忽略这块区域。"""
    assert _inject(_hooks(), _State(tmp_path)) == ""


def test_the_board_counts_what_was_verified(tmp_path):
    hook, state = _hooks(), _State(tmp_path)
    asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))
    board = _inject(hook, state)
    assert "已验式子 1 条" in board
    assert "verified=1" in board


def test_failures_are_listed_but_successes_are_not(tmp_path):
    """还红着的上板，验过的不上 —— 板面留给要做的事。"""
    hook, state = _hooks(), _State(tmp_path)
    asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))   # verified
    asyncio.run(D._check_step(state, "exp(x+y)", "exp(x)+exp(y)"))     # failed
    board = _inject(hook, state)
    assert "不成立" in board
    assert "exp(x + y)" in board or "exp(x+y)" in board
    assert "(a+b)**2" not in board and "a**2+2*a*b+b**2" not in board, (
        "verified 的式子不该占板面 —— 它是历史，不产生决策")


def test_a_repaired_step_leaves_the_board(tmp_path):
    """★ 最重要的一条：补上适用域重验之后，它必须从板上消失。

    取"曾经 failed 过"会让板子永远挂着已经修好的东西 ——
    而一块永远在喊狼来了的板子，两轮之后就没人看了。
    """
    hook, state = _hooks(), _State(tmp_path)
    asyncio.run(D._check_step(state, "sqrt(x**2)", "x"))
    assert "不成立" in _inject(hook, state), "先得真的红起来"

    asyncio.run(D._check_step(state, "sqrt(x**2)", "x",
                              assumptions={"x": "positive"}))
    state.hook_state.clear()          # 清签名，强制重渲染
    repaired = _inject(hook, state)
    assert "不成立" not in repaired, "补假设重验之后不该还挂在板上"
    assert "verified=1" in repaired


def test_the_board_does_not_repeat_itself(tmp_path):
    """账本没变就不再注入 —— 每轮重推同一块板子是在给 context 交租金。"""
    hook, state = _hooks(), _State(tmp_path)
    asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))
    assert _inject(hook, state, turn=1) != ""
    assert _inject(hook, state, turn=2) == "", "第二轮账本没变，不该重复注入"

    asyncio.run(D._check_step(state, "exp(x+y)", "exp(x)+exp(y)"))
    assert _inject(hook, state, turn=3) != "", "新验了一条，板子该更新"


def test_numeric_only_steps_are_flagged(tmp_path):
    hook, state = _hooks(), _State(tmp_path)
    result = asyncio.run(D._check_step(state, "sin(x)**2+cos(x)**2", "1"))
    if result["verification"]["status"] == "numerically_supported":
        assert "只有数值支持" in _inject(hook, state)


# ── 收尾闸 ──────────────────────────────────────────────────────────────────

def test_finish_gate_is_silent_when_nothing_is_red(tmp_path):
    hook, state = _hooks(), _State(tmp_path)
    asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))
    assert hook.on_before_finish(_Ctx(state)) is None


def test_finish_gate_asks_for_an_account_and_names_the_exits(tmp_path):
    """有红的就在收工前要一次交代，并且**把合法出口列全**。

    这道闸框架侧只放一次，所以它必须一次说清可以怎么做 —— 只说"不许走"
    而不说"怎么才能走"，调用方唯一能做的就是重试（契约必须送到调用方）。
    """
    hook, state = _hooks(), _State(tmp_path)
    asyncio.run(D._check_step(state, "exp(x+y)", "exp(x)+exp(y)"))
    msgs = hook.on_before_finish(_Ctx(state))
    assert msgs, "有验不过的式子却放行了"
    text = msgs[0].content
    assert "重验" in text          # 出口 1：修
    assert "撤掉" in text          # 出口 2：撤
    assert "findings" in text      # 出口 3：它本身是个发现
    assert "证否是结果，不是失败" in text


def test_finish_gate_clears_after_repair(tmp_path):
    """修好了就该放行 —— 闸不认"曾经红过"。"""
    hook, state = _hooks(), _State(tmp_path)
    asyncio.run(D._check_step(state, "sqrt(x**2)", "x"))
    assert hook.on_before_finish(_Ctx(state)), "先得真的拦一次"
    asyncio.run(D._check_step(state, "sqrt(x**2)", "x",
                              assumptions={"x": "positive"}))
    assert hook.on_before_finish(_Ctx(state)) is None


def test_the_hook_is_wired_into_the_harness():
    """机制存在 ≠ 接到路径：harness.yaml 里要真的启用了它。"""
    from core.loader import load_harness

    bootstrap()
    harness = load_harness("derivation")
    assert "derivation_board" in (harness.loop_hooks or []), (
        "hook 注册了但节点没启用 —— 那它等于不存在")
    assert "write_scratchpad" in (harness.tools or []), (
        "启用了 scratchpad hook 就得配 write_scratchpad 工具，"
        "否则引导里教的做法模型做不到（文案许诺的能力，API 得给得出）")


# ── 收尾闸的第二条：你答的是题目问的那个问题吗 ──────────────────────────────
#
# 2026-08-23 三臂对照的真实案例：一道题问**振幅**共振曲线的半高全宽，
# 模型算出了**功率**的半高宽。它的账本 **10 次验证全 verified、0 次 failed** ——
# 每一步都对，算出的量本身也没错，错在**回答的不是题目问的那个问题**。
#
# `check_step` 保证"这一步的变换成立"，验不出"你在回答另一个问题"。
# 框架能做的是**把两句话并排摆出来**（机械），判断切不切题归模型（语义）。


class _StateWithPrereg(_State):
    """带冻结 prereg 与一份 derivation_log 的 state。

    ⚠️ 替身必须遵守真 State 的契约（2026-08-23 血的教训）：
    `list_artifacts()` 返回的条目**只有 {id, type, name}**，不带 metadata；
    要读内容必须 `read_artifact(id)`（见 core/state.py 里 entry 的构造）。

    上一版替身的 list_artifacts 直接返回了带 metadata 的完整记录 —— 于是
    被测代码里 `art.get("metadata")` 这个**永远读到空**的写法在测试里一路绿灯，
    真跑时每次都误判成"没有 main_result"。**替身遮住了被测的真实契约**：
    替身比真货宽容一分，就有一分的错漏测不出来。

    末尾 `test_the_stub_matches_the_real_state_contract` 用真 State 钉住这条，
    防止替身再次漂移。
    """

    def __init__(self, tmp_path, proposition: str, main_statement: str,
                 main_expression: str = "", frozen: bool = False):
        super().__init__(tmp_path)
        self._prereg = f"""## Research Questions

### Q1: 测试问题
- output_kind: 一条命题的裁决
- proposition: {proposition}
- 闭合条件:
```yaml
- id: X
  statement: "随便一条"
```
"""
        self._main = {"statement": main_statement, "expression": main_expression}
        self._frozen = frozen

    def _records(self) -> list[dict]:
        """完整记录 —— 只有 read_artifact 看得到这一层。"""
        log_metadata: dict = {"frozen": self._frozen}
        if self._main.get("statement") or self._main.get("expression"):
            log_metadata["main_result"] = self._main
        return [
            {"id": "pre_registration__T", "type": "pre_registration",
             "name": "T", "content": self._prereg, "metadata": {"frozen": True}},
            {"id": "derivation_log__T", "type": "derivation_log",
             "name": "T", "metadata": log_metadata},
        ]

    def list_artifacts(self):
        # 与真 State 一致：**只有摘要三件套**。
        return [{k: r[k] for k in ("id", "type", "name")} for r in self._records()]

    def read_artifact(self, artifact_id):
        for r in self._records():
            if r["id"] == artifact_id:
                return r
        return None


def test_finish_gate_asks_about_alignment_even_with_zero_failures(tmp_path):
    """★ 关键：账本零 failed 时**仍然**要问切题。

    那道真实错题正是 0 次 failed —— 只在有 failed 时才问的话，
    这道闸对它根本不会触发。
    """
    hook = _hooks()
    state = _StateWithPrereg(
        tmp_path,
        proposition="受迫阻尼谐振子**振幅**共振曲线的半高全宽为 sqrt(3)*gamma",
        main_statement="半高全宽 Δω ≈ γ = ω₀/Q",
        main_expression="gamma")
    # 账本干净：没有任何 failed
    asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))
    msgs = hook.on_before_finish(_Ctx(state))
    assert msgs, "零 failed 时也必须问切题 —— 真实错题正是这个形态"
    text = msgs[0].content
    assert "你答的是题目问的那个问题吗" in text
    assert "振幅" in text, "题目原文要摆出来"
    assert "Δω ≈ γ" in text or "gamma" in text, "模型的答案也要摆出来"
    assert "验不出" in text, "要说清工具帮不了这件事"


def test_alignment_check_lists_the_discriminating_qualifiers(tmp_path):
    """提示要点名**限定词**，不能只说一句「请检查是否切题」。

    "是振幅还是功率、是首阶修正还是完整表达式" 这类才是可操作的；
    泛泛一句"确认切题"等于没说。
    """
    hook = _hooks()
    state = _StateWithPrereg(tmp_path, proposition="某命题",
                             main_statement="某结果", main_expression="x")
    asyncio.run(D._check_step(state, "x", "x"))
    text = hook.on_before_finish(_Ctx(state))[0].content
    for qualifier in ("振幅还是功率", "首阶修正", "精确闭式"):
        assert qualifier in text, f"缺少限定词提示：{qualifier}"


def test_both_finish_checks_appear_together(tmp_path):
    """有 failed 时，切题自查与欠账交代**都要出现** —— 闸只放一次，
    漏掉任何一条就没有第二次机会。
    """
    hook = _hooks()
    state = _StateWithPrereg(tmp_path, proposition="某命题",
                             main_statement="某结果", main_expression="x")
    asyncio.run(D._check_step(state, "exp(x+y)", "exp(x)+exp(y)"))   # failed
    text = hook.on_before_finish(_Ctx(state))[0].content
    assert "你答的是题目问的那个问题吗" in text
    assert "验下来**不成立**" in text
    assert "证否是结果，不是失败" in text


def _unfinished_log(tmp_path, *, frozen: bool) -> _StateWithPrereg:
    """有 derivation_log、但 main_result 缺失（可选：还没冻结）—— 真事故形态。"""
    return _StateWithPrereg(tmp_path, proposition="振幅共振曲线的半高全宽",
                            main_statement="", main_expression="", frozen=frozen)


def test_a_log_without_main_result_is_never_silent(tmp_path):
    """★ 有日志却读不到 main_result 时**必须说话**。

    2026-08-23：这道切题自查上线后第一次真跑完全没出现 —— 那趟的产物根本没写
    main_result，而当时的代码在这里直接 `return None` 走人。
    「静默跳过」让一道防线的缺席长得和"检查通过了"一模一样：日志里没有它、
    报告里也没有它，我差点据此得出"这道提示对模型无效"的结论。
    """
    hook = _hooks()
    state = _unfinished_log(tmp_path, frozen=True)
    asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))
    msgs = hook.on_before_finish(_Ctx(state))
    assert msgs, "有 derivation_log 却没 main_result —— 这时候静默等于防线不在场"
    text = msgs[0].content
    assert "没有 main_result" in text
    assert "振幅共振曲线" in text, "题目原文仍要摆出来 —— 那是对照的另一半"
    assert "末步" in text, "要说清为什么不能让下游从末步 claim 里猜"


def test_an_unfrozen_log_is_named_at_finish(tmp_path):
    """未冻结要点名：冻结门查的是承诺兑现，不冻结等于那些检查一次没跑。

    真事故里模型 save 了两次、从没调 freeze_artifact，于是冻结门（连同它查的
    闭合项对账、主结果验证水平）全程缺席。
    """
    hook = _hooks()
    state = _unfinished_log(tmp_path, frozen=False)
    asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))
    text = hook.on_before_finish(_Ctx(state))[0].content
    assert "冻结" in text
    assert "derivation_log__T" in text, "要点名是哪一份没冻"


def test_a_frozen_complete_log_gets_no_freeze_nag(tmp_path):
    """已冻结 + 有 main_result → 只问切题，不再唠叨冻结。

    闸每多说一句不适用的话，真正要读的那句就被稀释一分。
    """
    hook = _hooks()
    state = _StateWithPrereg(tmp_path, proposition="某命题",
                             main_statement="某结果", main_expression="x",
                             frozen=True)
    asyncio.run(D._check_step(state, "x", "x"))
    text = hook.on_before_finish(_Ctx(state))[0].content
    assert "你答的是题目问的那个问题吗" in text
    assert "还没冻结" not in text


def test_no_prereg_means_no_alignment_check(tmp_path):
    """读不到冻结 prereg 就不问 —— 没有"题目"可对照时，
    强行问一句是噪音（同 fixture 回放 / 独立运行的场景）。
    """
    hook = _hooks()
    state = _State(tmp_path)          # 没有 prereg
    asyncio.run(D._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2"))
    assert hook.on_before_finish(_Ctx(state)) is None


# ── 替身与真货的契约 ────────────────────────────────────────────────────────

def test_the_stub_matches_the_real_state_contract(tmp_path):
    """★ 用**真 State** 钉住 list_artifacts 的返回形状。

    2026-08-23：上一版替身的 list_artifacts 返回了带 metadata 的完整记录，
    而真 State 只返回 {id, type, name}。被测代码里 `art.get("metadata")`
    因此永远读到空 —— 单元测试一路绿灯，真跑每次都误判成"没有 main_result"。

    **替身比真货宽容一分，就有一分的错漏测不出来。** 这条测试不测业务，
    只测"我的替身有没有在撒谎"：真 State 存一份产物，列举出来的条目必须
    **不含** metadata，内容必须靠 read_artifact 才拿得到。
    """
    from core.state import State

    bootstrap()
    real = State(run_id="r-contract", node_type="derivation",
                 root=tmp_path / "run")
    real.save_artifact("derivation_log", "t", "正文",
                       metadata={"main_result": {"statement": "结论"}})

    listed = [a for a in real.list_artifacts() if a.get("type") == "derivation_log"]
    assert listed, "存进去了却列不出来"
    assert "metadata" not in listed[0], (
        "真 State 的 list_artifacts 开始带 metadata 了 —— "
        "那么 hooks 里那句 read_artifact 可以简化；"
        "但在此之前，依赖 list_artifacts 拿 metadata 的代码全是空读")

    full = real.read_artifact(listed[0]["id"])
    assert (full.get("metadata") or {}).get("main_result"), (
        "read_artifact 也读不到 metadata —— 那 hook 无论怎么写都拿不到主结果")


def test_the_finish_gate_leaves_a_trace_of_what_it_checked(tmp_path):
    """★ 收尾闸执行过要留痕 —— 「缺席」和「跑了且通过」不能长得一样。

    框架的 finish_gate_blocked 只记 n_messages，而本闸把多条检查合并成一条
    消息，n_messages 恒为 1。2026-08-23 诊断真事故时，我因此完全无法从
    transcript 判断切题自查跑没跑，只好去翻 messages_checkpoint。
    """
    hook = _hooks()
    state = _StateWithPrereg(tmp_path, proposition="某命题",
                             main_statement="某结果", main_expression="x",
                             frozen=True)
    asyncio.run(D._check_step(state, "exp(x+y)", "exp(x)+exp(y)"))   # failed
    hook.on_before_finish(_Ctx(state))

    events = [json.loads(l) for l in state.transcript_path.read_text().splitlines()
              if l.strip()]
    traces = [e for e in events if e.get("event") == "derivation_finish_checks"]
    assert traces, "收尾闸跑了却没留下任何痕迹"
    assert set(traces[-1]["fired"]) == {"alignment", "open_failures"}, (
        "留痕要说清**哪几条**检查触发了，只记一个总数等于没记")

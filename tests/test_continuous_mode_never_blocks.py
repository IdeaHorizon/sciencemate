"""连续模式：不出现任何需要人操作的行为（2026-08-19 wangd 定的语义）。

## 出了什么事

UI 上选「连续」，平台照样弹窗要人选。查下来：`set_auto_approve` 在
`chat.py`（CLI 前端）里被调 4 次，在 `platform_runtime.py`（平台前端）里
**0 次** —— 平台只写了"预授权哪些高危类别"，而真正决定"要不要停下来问人"的
那个开关从来没人打开。UI 白纸黑字的承诺，在这条路径上只兑现了一半。

两个前端共用的是**一轮怎么跑**（`session_driver.run_turn`），不是整个前端。
开关落在各自的外壳里，就一定会有一边漏 —— 名单式修法（"平台侧也补上这两行"）
挡不住下一个开关。所以收进脊柱：状态是真相源，开关每轮按状态重放。

## 三档语义

    assisted    常问
    autonomous  只在高危点停
    连续        不停，直到研究真正结束

「连续」= autonomous + 预授权**全部**类别（`["*"]`）。用户已经逐类授权过，
再停下来问是自相矛盾。autonomous 档不变。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import pause_driver
from core.session_driver import apply_autonomy
from shared.lib import dangerous_commands as dc

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _restore():
    """这些是进程级全局 —— 测完必须**全部**还原。

    第一版只还原了两个，漏了预授权类别，于是 `test_dangerous_commands` 那批
    在我之后跑的用例全红（单独跑却绿）。跟我今天在 CI 上追的那类"本地绿、
    CI 红"是同一个形状 —— 全局状态没还原干净。所以这里按**施加函数碰过的
    每一项**还原，别再手挑。
    """
    before = (
        pause_driver.AUTO_APPROVE_ENABLED,
        pause_driver.AUTO_APPROVE_COUNTDOWN_SEC,
        dc.BYPASS_ENABLED,
        list(getattr(dc, "PREAUTHORIZED_CATEGORIES", []) or []),
    )
    yield
    pause_driver.set_auto_approve(before[0], before[1])
    dc.set_bypass_mode(before[2])
    dc.set_preauthorized_categories(before[3])


def _state(**hook):
    return SimpleNamespace(hook_state=dict(hook))


def _pause(kind: str):
    return SimpleNamespace(metadata={"type": kind})


# ── 施加：状态 → 运行时开关 ──────────────────────────────────────────────────

def test_continuous_opens_both_switches():
    """连续 = 自动放行 + 高危绕行。少一个就是"选了却没生效"。"""
    assert apply_autonomy(
        _state(authorized_risk_classes=["*"])) is True
    assert pause_driver.AUTO_APPROVE_ENABLED is True
    assert dc.BYPASS_ENABLED is True


def test_autonomous_without_full_authorization_still_stops_at_high_risk():
    """autonomous 档：授权了的那些类别放行，没授权的高危点仍然停人。

    档位现在**只**由授权范围表达（`continuous_enabled` 那份抄件已删）。所以
    "部分授权"就是 autonomous：不是全类别，就不绕行高危确认。
    """
    apply_autonomy(_state(authorized_risk_classes=["shell_write"]))
    assert dc.BYPASS_ENABLED is False
    assert pause_driver._is_high_risk(_pause("highrisk_confirm")) is True
    assert dc.preauthorized("shell_write") is True


def test_assisted_closes_both():
    apply_autonomy(_state())
    assert pause_driver.AUTO_APPROVE_ENABLED is False
    assert dc.BYPASS_ENABLED is False


def test_switching_off_mid_session_takes_effect_next_turn():
    """开关按状态重放 —— 中途关掉连续，立刻恢复停人。

    这正是"施加"而不是"设置一次"的理由：谁都可能在别处改过全局。
    2026-08-23 之后施加点有两个（每轮开跑前、以及**声明变化的那一刻**），
    因为一轮无人值守可以跑几十分钟不产生任何轮边界。
    """
    apply_autonomy(_state(authorized_risk_classes=["*"]))
    assert dc.BYPASS_ENABLED is True
    apply_autonomy(_state())
    assert dc.BYPASS_ENABLED is False
    assert pause_driver._is_high_risk(_pause("permission")) is True


# ── 行为：连续档下没有人工阻断点 ────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["highrisk_confirm", "permission"])
def test_continuous_leaves_no_blocking_pause(kind):
    """「不应该出现任何需要人操作的行为」—— 两种永不自动放行的类型都放行。"""
    apply_autonomy(_state(authorized_risk_classes=["*"]))
    assert pause_driver._is_high_risk(_pause(kind)) is False


@pytest.mark.parametrize("kind", ["highrisk_confirm", "permission"])
def test_those_same_pauses_still_block_outside_continuous(kind):
    """反向：不是连续档，这两类照旧停人。放行的边界必须是"用户授权了全部"。"""
    apply_autonomy(_state())
    assert pause_driver._is_high_risk(_pause(kind)) is True


# ── 扫盘：防下一个"只接一边"的开关 ──────────────────────────────────────────

def test_no_runtime_switch_is_wired_to_only_one_frontend():
    """**这条才是真修复。**

    今天的 bug 不是"忘了写两行"，是"两个前端各自记账"这个结构。任何改变全局
    运行时行为的 setter，只被一个前端调用 = 另一个前端上那个功能是死的，而且
    不报错。判据是**扫出来的**，不是我写的名单 —— 名单对新开关默认漏过，
    那正是这个 bug 的成因（[[feedback_guardrails_must_scan_not_list]]）。
    """
    cli = (ROOT / "chat.py").read_text(encoding="utf-8")
    plat = (ROOT / "platform_runtime.py").read_text(encoding="utf-8")
    spine = (ROOT / "core" / "session_driver.py").read_text(encoding="utf-8")

    # 扫出所有"设置进程级全局"的 setter：模块级 def set_*(...) 且函数体里 global。
    switches: set[str] = set()
    for path in (ROOT / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"^def (set_[a-z_]+)\(", text, re.MULTILINE):
            body = text[match.end(): match.end() + 600]
            if re.search(r"^\s+global\s", body, re.MULTILINE):
                switches.add(match.group(1))
    for path in (ROOT / "shared" / "lib").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"^def (set_[a-z_]+)\(", text, re.MULTILINE):
            body = text[match.end(): match.end() + 600]
            if re.search(r"^\s+global\s", body, re.MULTILINE):
                switches.add(match.group(1))
    assert switches, "一个全局开关都没扫到 —— 扫盘判据本身坏了"

    def calls(text: str, name: str) -> int:
        return len(re.findall(rf"(?<!def ){re.escape(name)}\s*\(", text))

    # **显式豁免**：单边是对的，且理由写在这里给评审看。与"名单式检测"不同 ——
    # 检测仍然是扫出来的，这里只是给扫到的结果一个有据可查的出口。
    EXEMPT = {
        # CLI 的 panic event 注册。平台不需要：`_generation_abort_requested`
        # 同时看**当前绑定 run 的取消状态**（kill_signal），那条不依赖任何前端
        # 记得注册（core/llm.py 有说明，2026-08-17 实测过）。
        "set_stream_abort_check",
    }
    lopsided = []
    for name in sorted(switches - EXEMPT):
        in_cli, in_plat, in_spine = calls(cli, name), calls(plat, name), calls(spine, name)
        if in_spine:
            continue          # 收进脊柱了 —— 两个前端自动都有，正是我们要的形态
        if (in_cli > 0) != (in_plat > 0):
            lopsided.append(f"{name}: CLI={in_cli} 平台={in_plat}")

    assert not lopsided, (
        "这些运行时开关只接了一个前端 —— 另一边那个功能是死的，且不报错：\n  "
        + "\n  ".join(lopsided)
        + "\n把它收进 core/session_driver（脊柱按 state 每轮施加），"
          "别在另一个前端里再补一行 —— 名单挡不住下一个。"
    )


# ── 接到路径了吗（本次 bug 的同款形状）────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_turn_actually_applies_it():
    """**机制存在 ≠ 接到路径。**

    上面那些测试直接调 `apply_continuous_mode`，所以就算 `run_turn` 里那一行
    被删掉它们也全绿 —— 而"函数写好了没人调"正是这次 bug 本身
    （[[feedback_mechanism_exists_but_unwired]]）。这条走**真的** run_turn。

    走的是最短的一条真实路径：挂着 pause 且前端不能驱动 → run_turn 在守卫处
    抛 PausePendingError。守卫在施加之后，所以抛出来时开关必须已经生效。
    """
    from core import pause as pause_mod
    from core.session_driver import PausePendingError, run_turn

    original = pause_mod.get_deepest_paused
    pause_mod.get_deepest_paused = lambda: SimpleNamespace(pause_event=None)
    try:
        frontend = SimpleNamespace(ask_pause=None, waits_for_background=False,
                                   emit=lambda *a, **k: None)
        state = _state(authorized_risk_classes=["*"])
        with pytest.raises(PausePendingError):
            await run_turn(state, None, [], None, "hi", frontend=frontend)
    finally:
        pause_mod.get_deepest_paused = original

    assert pause_driver.AUTO_APPROVE_ENABLED is True, "run_turn 没有施加连续模式开关"
    assert dc.BYPASS_ENABLED is True, "run_turn 没有施加高危绕行"

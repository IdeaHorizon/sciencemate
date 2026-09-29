"""改了档位，**正在跑的那一轮**必须立刻按新档位走。

## 现场（2026-08-23，会话 e46448f0）

人在一轮跑到一半时把项目从「协作」切成「连续」。19 分钟后，hypothesis 跑完，
平台照样弹出 post-node 决策卡等人。直到人手动答复 —— 那次答复顺带产生了一次
派发 —— 后面的决策点才开始自动放行。

三处断线，方向各不相同：

1. **没人推**：`PATCH /projects/{id}/config` 只写库行。worker 手里的档位来自
   **派发时**的快照，而一轮无人值守可以跑几十分钟不产生任何派发。
2. **降级被忽略**：接收端写着 `if declared and …` —— 空列表当"这次不谈授权"。
   可"协作"档在后端算出来就是空列表，于是「连续 → 协作」在任何路径上都不生效。
3. **只在轮首施加**：`declare_authorization` 只写 state，开关由脊柱每轮投影一次。
   一轮之内没有第二次"轮首"。

这条测试守的是**送达**，不是"函数写好了"。三处只要有一处退回去，它就红。
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import pause_driver
from core.session_driver import apply_autonomy
from shared.lib import dangerous_commands as dc

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _restore():
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


def _calls(source: str, func: str) -> set[str]:
    """这个函数里**实际调用**了哪些东西（点号全名）。

    撤了调用留下注释、留下 import，都照样命中字符串扫描 —— 所以判据取 AST 的
    Call 节点。这条教训写在 [[feedback_grep_for_a_name_is_not_wiring]]。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func:
            return {
                ast.unparse(call.func)
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            }
    raise AssertionError(f"找不到 {func} —— 它搬走了？先确认送达链还在")


def _body(source: str, func: str) -> str:
    """取这个函数的**原始源码**。

    用 AST 定位（改名/挪位置会当场说不出话，而不是静默匹配到别处），但取的是
    原文而不是 `ast.unparse` —— unparse 会把引号、括号都规范化一遍，断言里写的
    字面量便与它对不上（实测：`if op == "sync"` 被 unparse 成单引号）。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func:
            segment = ast.get_source_segment(source, node)
            if segment:
                return segment
    raise AssertionError(f"找不到 {func} —— 它搬走了？先确认送达链还在")


# ── 1. 收下即生效（不等下一个轮首）──────────────────────────────────────────

def test_a_declaration_takes_effect_the_moment_it_arrives():
    """`declare_authorization` 收下之后，开关必须已经变了。

    这是三处里最容易悄悄退回去的一处：只写 state 也能让所有"直接调
    apply_autonomy"的测试全绿，而真实症状是"这一轮剩下的决策点仍按旧档走"。
    """
    source = (ROOT / "platform_runtime.py").read_text(encoding="utf-8")
    body = _body(source, "declare_authorization")
    assert "apply_autonomy" in body, (
        "declare_authorization 只落事实、不施加 —— 那么一轮无人值守中途改档，"
        "要等到下一个轮首才生效，而这一轮可能根本没有下一个轮首"
    )


def test_switch_flips_both_ways_without_a_turn_boundary():
    """升级和降级都要立刻生效 —— 降级方向是 2026-08-23 完全不生效的那个。"""
    state = SimpleNamespace(hook_state={})

    state.hook_state["authorized_risk_classes"] = ["*"]
    assert apply_autonomy(state) is True
    assert pause_driver.AUTO_APPROVE_ENABLED is True and dc.BYPASS_ENABLED is True

    state.hook_state["authorized_risk_classes"] = []
    assert apply_autonomy(state) is False
    assert pause_driver.AUTO_APPROVE_ENABLED is False, "切回协作之后还在自动放行"
    assert dc.BYPASS_ENABLED is False, "切回协作之后高危仍然绕行"


# ── 2. 显式给了就是权威，包括空 ────────────────────────────────────────────

def test_an_empty_declaration_revokes_instead_of_being_ignored():
    """空列表 = 协作档，不是"这次不谈授权"。

    判据看的是**接收端的条件**：`if declared and …` 会把空吞掉。缺席（None）
    与空是两件事，只有前者才是"没谈"。
    """
    source = (ROOT / "platform_runtime.py").read_text(encoding="utf-8")
    body = _body(source, "_apply_request_scope")
    assert "if declared and declared !=" not in body, (
        "空声明又被当成「这次不谈授权」了 —— 「连续 → 协作」会再次完全不生效"
    )
    assert "if declared != session._authorized_risk_classes" in body, (
        "显式给了就该照做（含空）；只有字段缺席才是「这条请求没谈档位」"
    )


# ── 3. 配置一改就推给活着的会话 ────────────────────────────────────────────

def test_changing_the_setting_pushes_to_live_sessions():
    """`PATCH …/config` 必须把新档位推出去，而不是等下一次有人说话。"""
    endpoint = (ROOT / "platform" / "backend" / "app" / "api" / "v1" / "projects.py")
    body = _body(endpoint.read_text(encoding="utf-8"), "update_project_config")
    assert "broadcast_autonomy" in body, (
        "改完设置没有推给正在跑的 worker —— 它手里还是出发时那份快照"
    )

    manager = (ROOT / "platform" / "backend" / "app" / "services" / "harness_sessions.py")
    text = manager.read_text(encoding="utf-8")
    assert "async def broadcast_autonomy" in text
    assert "async def sync_scope" in text, (
        "推送要走一条真的请求（管理面 op），因为档位挂在派发的必经点上"
    )


def test_the_push_goes_through_the_same_choke_point():
    """推送不许另起一套"改档位"的通道 —— 那就会有第二个写点。

    `sync` 在 worker 侧必须是空操作：它存在的意义只是让
    `_apply_request_scope` 这个必经点被走一次。
    """
    source = (ROOT / "platform_runtime.py").read_text(encoding="utf-8")
    body = _body(source, "_handle_management_op")
    branch = body[body.index('if op == "sync"'):]
    branch = branch[: branch.index("return")]
    assert "declare_authorization" not in branch, (
        "sync 自己去改档位了 —— 那是第二个写点。它只该回执，"
        "档位由必经点在它进门时已经施加过了"
    )


# ── 4. 两件事两个名字 ──────────────────────────────────────────────────────

def test_the_unattended_loop_never_rewrites_the_users_mode():
    """`run_unattended` 只开自动续轮，不碰档位。

    从前它调 `_set_continuous_mode(state, True)` —— 那个函数同时表达"这趟自己
    续轮"和"用户选了连续档"。于是平台上每次 autonomous 运行都把档位悄悄提到
    连续，而下一次派发又按真实档位改回来：两个写者方向相反，谁最后写谁赢。
    """
    source = (ROOT / "platform_runtime.py").read_text(encoding="utf-8")
    called = _calls(source, "run_unattended")
    # 查**调用**，不查名字出现：注释里记着"从前这里写的是 _set_continuous_mode"
    # 是有价值的历史，而按字符串扫会把它判成违规，逼人删掉教训。
    assert "chat._set_continuous_loop" in called, "续轮循环没被打开，无人值守跑一轮就停"
    assert "chat._set_continuous_mode" not in called
    forbidden = {
        "_dc.set_preauthorized_categories",
        "dangerous_commands.set_preauthorized_categories",
        "pause_driver.set_auto_approve",
        "_dc.set_bypass_mode",
        "self.declare_authorization",
    }
    assert not (forbidden & called), (
        "run_unattended 又在自己设档位了：" + ", ".join(sorted(forbidden & called))
        + " —— 它会用出发时的快照盖掉中途送达的新档位"
    )


def test_mode_and_loop_are_separate_facts():
    """全仓不该再有 `continuous_enabled` 这份抄件。"""
    # 只找**真拿它当键用**的地方：`hook_state["continuous_enabled"]` 那种。
    #
    # 注释和文档串里留着这段历史是有用的（下一个人才知道为什么拆开），扫文本
    # 就等于逼人把教训删掉。判据盯行为：一个字符串字面量出现在代码里（不是
    # 文档串里），才说明有人还在按这个名字取值。
    offenders = []
    candidates = [*(ROOT / "core").rglob("*.py"), ROOT / "chat.py", ROOT / "platform_runtime.py"]
    for path in candidates:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and node.value == "continuous_enabled"
                and id(node) not in docstrings
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "`continuous_enabled` 又回来了 —— 它同时回答「用户选了哪一档」和"
        "「这趟要不要自己续轮」两个问题：" + ", ".join(offenders)
    )

"""writing 的 manuscript 审稿 spec 的**机械不变量**。

review_spec.md 是给模型读的散文，判据本身（切题不切题、值不值得成文）归模型。
但 spec 里有几处**机械可判**的契约，此前正因为无人守而各自漂：

- `redirect_upstream` 被正文要求使用（"打回上游重做"），却漏在 metadata 的
  `recommended_action` enum 里 —— 广告与默认不一致（2026-08-23 修）。
- 第 133 行残留"orchestrator 在 user accept 后 freeze" —— 与 #617/#635 冲突
  （冻结是作者签字、writing 自己冻、无 user-accept 步），会把交付路由带偏。
  这跟 PR#635 解开的三处矛盾指令是同一个病，只是那次没扫到这份 spec。

这条测试把这几处钉住，防它们悄悄漂回去。切题维度本身不写正则 —— 只断言
"这一维在 spec 里被声明、且 reviewer 拿得到判它的材料"。
"""
from __future__ import annotations

from pathlib import Path

from core.loader import node_dir


def _spec() -> str:
    return Path(node_dir("writing") / "review_spec.md").read_text(encoding="utf-8")


def test_redirect_upstream_is_in_the_action_enum():
    """正文要求用 redirect_upstream 打回上游，enum 就必须列它，否则模型不敢用。"""
    spec = _spec()
    assert "redirect_upstream" in spec, "spec 里根本没提 redirect_upstream？"
    # 找 recommended_action 的 enum 行
    enum_lines = [ln for ln in spec.splitlines()
                  if "recommended_action" in ln and "|" in ln]
    assert enum_lines, "找不到 recommended_action 的 enum 声明"
    assert any("redirect_upstream" in ln for ln in enum_lines), (
        "redirect_upstream 在正文被要求使用，却不在 recommended_action enum 里 —— "
        "广告与默认不一致，模型会不敢填这个值"
    )


def test_no_stale_orchestrator_freeze_instruction():
    """spec 不得再教人"orchestrator 冻结 / 等 user accept" —— 那与 #617/#635 冲突。

    冻结是作者签字：writing 自己冻，orchestrator 代签会被机械拒。这份 spec 曾残留
    旧流程"orchestrator 在 user accept 后 freeze"，PR#635 解开别处三份同类矛盾时
    没扫到它。
    """
    spec = _spec()
    # 允许出现 "orchestrator 不能代签 / 不冻结" 这类否定说明；禁止出现把冻结
    # 责任派给 orchestrator / user-accept 的正向指令。
    lowered = spec
    bad_patterns = [
        "orchestrator 在 user accept",
        "orchestrator 在 user 接受",
        "由 orchestrator freeze",
        "orchestrator freeze manuscript",
        "等 user accept 后",
    ]
    hit = [p for p in bad_patterns if p in lowered]
    assert not hit, (
        f"spec 残留把冻结派给 orchestrator/user-accept 的旧指令：{hit}。"
        f"应改为「approve 后 writing 自己 freeze」（作者签字，见 #617/#635）"
    )
    # 正面契约：spec 必须说明 writing 自己冻
    assert ("writing 自己" in spec and "freeze" in spec), (
        "spec 必须点明 approve 后由 writing 自己 freeze"
    )


def test_responsiveness_dimension_is_declared_with_its_material():
    """切题维度要在册，且审稿前必做里给了它的判据来源（research_state / brief / 意图）。

    加一个模型判不了的维度（没喂材料）= 无材料空判。这里只断言"维度声明 + 材料
    通道在 spec 里"，判得对不对归模型。
    """
    spec = _spec()
    assert "responsiveness" in spec, "responsiveness 维度没在 metadata schema 里声明"
    assert "回应用户诉求" in spec or "切题" in spec, "responsiveness 维度没在正文展开"
    # 材料通道：审稿前必做里要读 research_state（注册的研究问题）+ PROJECT.md（brief）
    assert "research_state" in spec and "PROJECT.md" in spec, (
        "审稿前必做没让 reviewer 读 research_state / PROJECT.md —— "
        "切题维度就没有判据来源，等于让模型空判"
    )


def test_anti_dilution_rule_is_present():
    """红线优先于平均 —— 一维 critical 不能被别维高分拉平（review 门反复栽的坑）。"""
    spec = _spec()
    assert "anti-dilution" in spec or "红线优先于平均" in spec, (
        "缺 anti-dilution 规则：verdict 纯按平均分推，一条红线会被稀释"
    )
    # responsiveness / honesty 的 critical 必须能顶到最高只能 major/block
    assert "一票否决" in spec or "最高只能" in spec, (
        "anti-dilution 规则没说清红线怎么封顶 verdict"
    )

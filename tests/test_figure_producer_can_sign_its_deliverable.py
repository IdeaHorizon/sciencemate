"""frozen-gated 交付物的产出节点必须自己有签字（冻结）通道。

2026-09-01 本机 E2E 实拍死锁（B刀 之后第一次真跑收尾）：

  figure 在 artifact_policy 里是 retention=permanent 的交付物类型，
  平台发布判据是 "frozen by producing node"；
  orchestrator 代调 freeze_artifact 被 owner 闸**正确**拒绝（冻结=作者签字，
  这道闸保护的正是 #621/#754 修的那类身份语义，不能松）；
  而 postprocess —— figure 的唯一产出节点 —— 的工具面在 B刀 瘦身后
  没有 freeze_artifact。

  三张图记录完整（三重血缘、审计全过、300 DPI），却没有任何人有权冻结：
  交付物页面永远缺图，而唯一的绕行路（代签）会破坏作者身份语义。

  「figure 升永久交付物」的管道建了，给管道供货的签字通道没建 ——
  机制存在但没接到路径的又一例。

判据锚在 policy 上：**只要** figure 还是 permanent 交付物、且冻结语义还是
作者签字，postprocess 的工具面就必须包含 freeze_artifact。哪天发布判据改了
（比如 render_figure 铸造即冻结），这条测试连同它的理由一起删。
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _policy_entry(artifact_type: str) -> dict:
    import sys
    sys.path.insert(0, str(REPO))
    from shared.lib.artifact_policy import POLICY  # type: ignore
    return POLICY.get(artifact_type) or {}


def test_figure_is_still_a_frozen_gated_permanent_deliverable() -> None:
    """前提自检：前提变了这套测试就该删，而不是空转。"""
    entry = _policy_entry("figure")
    assert entry.get("retention") == "permanent", (
        f"figure 的 retention 变成 {entry.get('retention')!r} —— "
        "本测试的前提失效，请连同理由一起处理这条闸")


def test_the_producing_node_holds_the_pen() -> None:
    spec = yaml.safe_load(
        (REPO / "nodes" / "postprocess" / "harness.yaml").read_text(encoding="utf-8"))
    tools = [t if isinstance(t, str) else str(t) for t in (spec.get("tools") or [])]
    assert "render_figure" in tools, "postprocess 不再产出 figure？前提变了"
    assert "freeze_artifact" in tools, (
        "postprocess 产出 frozen-gated 的 figure 交付物，却没有 freeze_artifact ——\n"
        "orchestrator 代签会被 owner 闸正确拒绝，于是没有任何人能冻结它：\n"
        "交付物页面永远缺图（2026-09-01 实测死锁）。")

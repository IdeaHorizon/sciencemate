"""引导许诺的交接自述，工具面必须给得出。

2026-09-01 本机 E2E 实拍：hypothesis 节点读到自己 harness 里的

    ## 交接自述（可选但有回报）
    你的目录里可以维护一个 `README.md`：…定向层会把它自动带给所有后续节点

然后发现自己**没有任何能写文件的工具**，把最后几轮烧在自我说服上
（"write_file 不在白名单内……让我确认一下：README 是可选的，不写不拦我"），
那段独白最后成了决策包里呈给人的「本节点产出了什么」。

读端是真的（core/loop_hooks_builtin 读 <node_dir>/README.md 首行塞进交接面），
六个挂这条引导的节点里有四个写不了 —— 机制对它们永远是空的，不是因为它们
选择不写，是因为它们不能写。

这道闸**扫盘**而不是列名单：将来任何节点挂上这条引导却没配写的工具，直接红。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]

# 能把内容写进 <node_dir>/README.md 的工具。
_CAN_WRITE_README = {"write_file", "safe_write_file", "write_node_readme"}
_GUIDANCE_MARK = "交接自述"


def _nodes_advertising_the_handoff() -> list[tuple[str, list[str]]]:
    out = []
    for f in sorted((REPO / "nodes").glob("*/harness.yaml")):
        text = f.read_text(encoding="utf-8")
        if _GUIDANCE_MARK not in text:
            continue
        spec = yaml.safe_load(text) or {}
        tools = [t if isinstance(t, str) else str(t) for t in (spec.get("tools") or [])]
        out.append((f.parent.name, tools))
    return out


def test_the_handoff_guidance_is_actually_advertised_somewhere() -> None:
    """闸本身不能因为「一个都没扫到」而空转通过。"""
    found = _nodes_advertising_the_handoff()
    assert len(found) >= 4, f"只扫到 {len(found)} 个挂引导的节点，闸可能没在扫真东西"


@pytest.mark.parametrize("node,tools",
                         _nodes_advertising_the_handoff(),
                         ids=[n for n, _ in _nodes_advertising_the_handoff()])
def test_a_node_told_to_keep_a_readme_can_write_one(node: str, tools: list[str]) -> None:
    able = _CAN_WRITE_README & set(tools)
    assert able, (
        f"节点 {node} 的 harness 里写着「{_GUIDANCE_MARK}：你的目录里可以维护一个 "
        f"README.md」，但它的工具面里没有任何能写这个文件的工具"
        f"（可选：{sorted(_CAN_WRITE_README)}）。引导许诺了工具面给不出的能力，"
        f"模型只会把轮次烧在自我说服上。")

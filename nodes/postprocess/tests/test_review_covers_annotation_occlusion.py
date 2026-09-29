"""审图契约必须覆盖"文字压文字"（E2E v22 实测缺口）。

那张 4 速率图上，写着 H2/H3 判定和 δTg_ratio=6.17 的标注框被图例压住、
基本读不出来。VLM 给了 approve、零 findings —— **它没做错**：
quantitative 的检查指令写的是 "check whether the legend or annotations
cover marks"，只问有没有盖住**数据点**，没问有没有盖住**另一段标注**。

契约不对称：护住了数据，没护住标注。而对一张科研图，写着判定的标注就是
正文，丢了它比丢一个数据点更严重。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from nodes.postprocess import vlm_witness as rv


def test_quantitative_instruction_asks_about_text_over_text() -> None:
    order = rv._inspection_order("quantitative") if hasattr(rv, "_inspection_order") else None
    if order is None:                     # 函数名变了就整文件找那段指令
        import inspect

        src = inspect.getsource(rv)
        start = src.index('if kind == "quantitative":')
        order = src[start:start + 1200]
    lowered = order.lower()
    assert "cover marks" in lowered, "护住数据这条不能丢"
    assert "overlaps another" in lowered or "annotation, legend, or text box overlaps" in lowered, \
        "还必须问：标注之间有没有互相遮挡"
    assert "unreadable" in lowered


def test_quantitative_rubric_lists_annotation_occlusion() -> None:
    path = (Path(rv.__file__).resolve().parent
            / "reviewer" / "rubrics" / "quantitative.yaml")
    checks = yaml.safe_load(path.read_text(encoding="utf-8"))["checks"]
    joined = " ".join(checks).lower()
    assert "annotation-over-annotation" in joined
    assert "readable" in joined

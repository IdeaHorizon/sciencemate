"""布局交给 Graphviz —— 别再自己写一个二流布局器。

## 为什么换掉自研布局

上一轮的自研引擎（`diagram_compiler` 里的 band/rank）解决了「画得对」：盒子
由内容撑开、边遍历声明来画，所以漏边/重叠/出框在架构上消失。但 2026-09-16
真跑出来的图仍然**读不了**：

- 8 条上联全下到图底、横穿整张图宽、再挤进一个小方块，谁连谁追不了；
- `ROCE NIC 0` 该接正上方的 Switch 0，线却从 Switch 0 右下角出来、穿 GPU3/GPU4
  的缝、再钩回 NIC 0 的右上角；
- CPU 行→Switch 行、GPU 行→NIC 行各空掉框高四分之一。

在那个引擎上继续贴「重心排序 + hub 特判 + 走廊方向」是打补丁：病根不是缺
一两条启发式，而是**布局模型本身是一维堆叠**（把行摞起来），它表达不了簇、
层次、放射，贴多少启发式它还是一维堆叠。

而分层布局、交叉最小化、簇、端口、样条路由，Graphviz 的 `dot` 做了三十年。
自研引擎是在跟它竞争，而且输得很惨。所以按 [[feedback_call_it_dont_reimplement_it]]：
**调它，别再实现一遍。**

## 分工没变，只换了中间那一段

    合同（声明结构 + 机械断言 + 跨版本回归）   ← 我们独有，保留
        ↓
    DOT（groups→cluster / ranks→rank=same）    ← 本模块
        ↓
    dot -Tjson 算坐标                          ← Graphviz
        ↓
    几何（schema 不变）                         ← 两个渲染后端原样消费
        ↓
    可读性度量（交叉/边长/同构/墨水比）          ← 验收，不是替代

`dot` 只在**铸造侧**需要（布局算在 harness 进程里，几何以字面量冻进渲染
脚本）——referee 按 replay 重跑只需要 matplotlib，不需要 Graphviz。

## 缺席是可读事实

机器上没有 `dot` 时回落到自研引擎，并在 findings 里说明「布局器是回落实现、
可读性会差」——不是静默降级。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from .figure_contract import role_colors

#: dot 的坐标单位是 point，和我们几何的单位一致 —— 不需要换算。
_POINTS_PER_INCH = 72.0

#: 布局超时。一张图的布局是毫秒级的；超过这个数说明图大到不该这么画。
_DOT_TIMEOUT_S = 30.0

#: 层间距 / 同层间距 / 簇内边距（英寸，dot 的单位）。
#:
#: **这三个数不是凭感觉设的**：2026-09-17 在校准集（用户给的参考图）+ 一次真实
#: agent 产出上扫了 12 组组合，按「两边 ink 都涨且交叉不退化」选出来的。
#: 原初值（0.75 / 0.30 / 14）是我拍脑袋的，组框白占了画布 38.6%。
#: 收紧后：产出 ink 0.0599→0.0672、组框 38.6%→27.1%；参考图 ink 0.0997→0.1056、
#: 交叉 1→0。nodesep 是关键 —— 留 0.30 时 ranksep 一收紧参考图就出现 2 处交叉。
RANK_SEP_IN = 0.40
NODE_SEP_IN = 0.20
CLUSTER_MARGIN_PT = 8


def dot_available() -> bool:
    return shutil.which("dot") is not None


def _quote(text: str) -> str:
    return '"' + str(text or "").replace("\\", r"\\").replace('"', r"\"") + '""'[:1]


def _dot_id(raw: str) -> str:
    """DOT 标识符：合同 id 里的 `.`/`:`/`-` 在 DOT 里有语义，统一转义。"""

    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", str(raw))


def build_unit_dot(
    contract: dict[str, Any],
    sizes: dict[str, tuple[float, float]],
    *,
    members: list[str],
    groups: list[dict[str, Any]],
) -> str:
    """**一个单元内部**的 DOT —— 不含 band，不含跨单元的边。

    分两层的理由（2026-09-16 实测撞出来的）：「哪个单元放第几行」是**页面编排**，
    「一个子系统内部怎么连」是**图布局**，这是两个问题。把 band 用不可见边硬塞
    进 dot 的分层模型，两边会互相冲掉 —— 实测先是声明的 2×2 被压平成 1×4，
    钉死层序后 NIC 又被甩出簇外、Server 1 的两个 PCIe 域被分到画面两端。每修
    一处冲掉另一处，这个循环不收敛。

    所以：单元内部交给 dot（它擅长分层与交叉最小化），单元之间的摆放由我们
    自己 pack（那是确定性的、也是作者显式声明过的）。
    """

    keep = set(members)
    sub = {
        "nodes": [n for n in contract["nodes"] if n["id"] in keep],
        "groups": groups,
        "edges": [
            e for e in contract["edges"] if e["from"] in keep and e["to"] in keep
        ],
        "bands": [],
    }
    return _build_dot_source(sub, sizes, with_bands=False)


def build_dot(contract: dict[str, Any], sizes: dict[str, tuple[float, float]]) -> str:
    return _build_dot_source(contract, sizes, with_bands=True)


def _build_dot_source(
    contract: dict[str, Any],
    sizes: dict[str, tuple[float, float]],
    *,
    with_bands: bool = True,
) -> str:
    """合同 → DOT 源码。

    - `groups` → `subgraph cluster_<id>`：簇是 dot 的原生概念，服务器框、子系统
      框都落在这里，dot 会保证簇不重叠、边穿簇时绕行（compound=true）。
    - `groups[].ranks` → `{rank=same; …}`：作者声明的层次原样交给 dot 当约束，
      其余（层内顺序、交叉最小化）由 dot 决定 —— 那正是我们做不好的部分。
    - `bands` → 同一条 band 里的顶层条目 `rank=same`（配 newrank=true 跨簇生效），
      作者的编排意图（例如 2×2 摆四台服务器）因此被保留。
    - 盒子尺寸由我们按文字算好后 `fixedsize=true` 钉死 —— dot 不需要字体，
      渲染端量出来的宽高与布局用的完全一致（一个问题一个真相源）。
    """

    lines: list[str] = [
        "digraph figure {",
        "  graph [rankdir=TB, compound=true, newrank=true, splines=spline,",
        f"         nodesep={NODE_SEP_IN}, ranksep={RANK_SEP_IN}, pad=0.15, margin=0];",
        "  node  [shape=box, fixedsize=true, margin=0];",
        "  edge  [arrowhead=none, arrowtail=none];",
    ]

    node_ids = {node["id"] for node in contract["nodes"]}
    grouped: dict[str, list[str]] = {}
    for group in contract["groups"]:
        grouped[group["id"]] = [nid for rank in group["ranks"] for nid in rank]
    in_group = {nid for members in grouped.values() for nid in members}

    def emit_node(nid: str, indent: str) -> None:
        width, height = sizes[nid]
        lines.append(
            f"{indent}{_dot_id(nid)} [width={width / _POINTS_PER_INCH:.4f},"
            f" height={height / _POINTS_PER_INCH:.4f}];"
        )

    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for group in contract["groups"]:
        by_parent.setdefault(group.get("parent"), []).append(group)

    def emit_group(group: dict[str, Any], depth: int) -> None:
        pad = "  " * (depth + 1)
        lines.append(f"{pad}subgraph cluster_{_dot_id(group['id'])} {{")
        lines.append(
            f"{pad}  style=rounded; penwidth=1.6; margin={CLUSTER_MARGIN_PT};"
        )
        # 簇标题的高度由渲染端画，这里只用一个占位 label 让 dot 留出空间。
        if group["label"]:
            lines.append(f'{pad}  label="{"".ljust(len(group["label"]) * 2)}";')
            lines.append(
                f"{pad}  labelloc={'t' if group['label_position'] == 'top' else 'b'};"
            )
        for child in by_parent.get(group["id"], []):
            emit_group(child, depth + 1)
        for nid in grouped[group["id"]]:
            emit_node(nid, pad + "  ")
        for rank in group["ranks"]:
            if len(rank) > 1:
                same = "; ".join(_dot_id(nid) for nid in rank)
                lines.append(f"{pad}  {{rank=same; {same}}}")
            # 同一 rank 内钉住左右顺序：作者写的 GPU 0..7 是有意义的。
            # **不能带 constraint=false** —— 带了以后 dot 的交叉最小化会把顺序
            # 整个打乱（实测：Server 3 排成 GPU3 / NIC0 / GPU0 / GPU1 / GPU2 …）。
            # 同一 rank 内的边本来就不影响分层，所以直接用带约束的不可见边。
            for left, right in zip(rank, rank[1:]):
                lines.append(
                    f"{pad}  {_dot_id(left)} -> {_dot_id(right)} [style=invis, weight=50];"
                )

        # **层与层的先后**：合同里的 ranks 是有序的（rank 0 在上）。只声明
        # 「同层」而不声明「谁在谁上面」，dot 会按边的方向自己分层 —— 实测把
        # ROCE NIC 和 GPU 摊成同一层（NIC 0 夹在 GPU 3 和 GPU 4 中间），
        # 语义分层就没了。用不可见边把作者的层序钉死。
        for upper, lower in zip(group["ranks"], group["ranks"][1:]):
            lines.append(
                f"{pad}  {_dot_id(upper[0])} -> {_dot_id(lower[0])} "
                "[style=invis, weight=80];"
            )
        lines.append(f"{pad}}}")

    for group in by_parent.get(None, []):
        emit_group(group, 0)

    # **兄弟子系统的同名层对齐**：同一个父组下的若干子组是**并列的子系统**
    # （两个 PCIe 域、两条产线、两个实验臂），它们的第 i 层是同一层。
    # 不说这件事，dot 只能按边推层：`sw0 -> sw1` 是条真边，于是 Switch 1 被
    # 降到 Switch 0 下面一层，跟 GPU 行齐平（2026-09-16 实测）。
    # 这是「并列」这个结构关系的普遍性质，不是某张图的特例。
    for siblings in by_parent.values():
        if len(siblings) < 2:
            continue
        depth = max(len(g["ranks"]) for g in siblings)
        for index in range(depth):
            peers = [g["ranks"][index][0] for g in siblings if index < len(g["ranks"])]
            if len(peers) > 1:
                same = "; ".join(_dot_id(nid) for nid in peers)
                lines.append(f"  {{rank=same; {same}}}")

    for nid in sorted(node_ids - in_group):
        emit_node(nid, "  ")

    for edge in contract["edges"]:
        attrs = ["weight=4"]
        if edge["kind"] == "bus":
            attrs.append("penwidth=2.4")
        if edge["kind"] == "dashed":
            attrs.append("style=dashed")
        if edge["label"]:
            attrs.append(f'label="{" " * (len(edge["label"]) * 2)}"')
        lines.append(
            f"  {_dot_id(edge['from'])} -> {_dot_id(edge['to'])} [{', '.join(attrs)}];"
        )

    # band 是作者的编排意图（几行、每行放什么），必须两件都约束：
    #   ① 同一条 band 里的条目同层（rank=same）；
    #   ② **band 之间自上而下**——只写 ① 不写 ②，dot 会把四台结构相同、又都
    #      连向同一个 hub 的服务器全排成一行（实测：声明的 2×2 被压平成 1×4，
    #      画布 4412×590，7.5:1）。② 用不可见边表达，weight 拉高保证生效。
    if with_bands:
        band_tails: list[list[str]] = []
        for band in contract["bands"]:
            heads: list[str] = []
            tails: list[str] = []
            for entry in band:
                prefix, _, ident = entry.partition(":")
                if prefix == "node":
                    heads.append(_dot_id(ident))
                    tails.append(_dot_id(ident))
                elif grouped.get(ident):
                    # 簇不能直接 rank=same，用簇里第一个/最后一个节点代表它的上下沿。
                    heads.append(_dot_id(grouped[ident][0]))
                    tails.append(_dot_id(grouped[ident][-1]))
            if len(heads) > 1:
                lines.append(f"  {{rank=same; {'; '.join(heads)}}}")
            band_heads.append(heads)
            band_tails.append(tails)

        for upper, lower in zip(band_tails, band_heads[1:]):
            if upper and lower:
                lines.append(
                    f"  {upper[0]} -> {lower[0]} [style=invis, weight=100];"
                )

    lines.append("}")
    return "\n".join(lines)


def _parse_pos(raw: str) -> tuple[float, float]:
    x, _, y = str(raw).partition(",")
    return float(x), float(y)


def _parse_bb(raw: str) -> list[float]:
    x0, y0, x1, y1 = (float(v) for v in str(raw).split(","))
    return [x0, y0, x1 - x0, y1 - y0]


def _bezier_to_polyline(spec: str) -> list[tuple[float, float]]:
    """dot 的边 `pos` 是三次贝塞尔串，采样成折线给渲染端。

    `e,x,y` / `s,x,y` 前缀是箭头端点，去掉 —— 我们的边不带箭头（方向由
    bidirectional 显式声明），留着会在端点多出一个折点。
    """

    tokens = [item for item in str(spec).replace("\n", " ").split() if item]
    points: list[tuple[float, float]] = []
    for token in tokens:
        if token.startswith(("e,", "s,")):
            continue
        try:
            x, _, y = token.partition(",")
            points.append((float(x), float(y)))
        except ValueError:
            continue
    if len(points) < 4:
        return points
    out: list[tuple[float, float]] = [points[0]]
    for index in range(1, len(points) - 2, 3):
        p0 = out[-1]
        p1, p2, p3 = points[index], points[index + 1], points[index + 2]
        for step in range(1, 9):
            t = step / 8.0
            u = 1.0 - t
            out.append(
                (
                    u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
                )
            )
    return out


def run_dot(source: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["dot", "-Tjson"],
        input=source,
        capture_output=True,
        text=True,
        timeout=_DOT_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"dot failed: {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


def layout_with_dot(
    contract: dict[str, Any],
    sizes: dict[str, tuple[float, float]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]], list[float]]:
    """(nodes, groups, edges, canvas_bb) —— 全部是 dot 算出来的坐标。"""

    source = build_dot(contract, sizes)
    payload = run_dot(source)

    index_to_name: dict[int, str] = {}
    node_geo: dict[str, dict[str, Any]] = {}
    group_geo: dict[str, dict[str, Any]] = {}
    alias = {_dot_id(node["id"]): node["id"] for node in contract["nodes"]}
    cluster_alias = {
        f"cluster_{_dot_id(group['id'])}": group["id"] for group in contract["groups"]
    }

    for index, obj in enumerate(payload.get("objects") or []):
        name = str(obj.get("name") or "")
        index_to_name[obj.get("_gvid", index)] = name
        if name in cluster_alias and obj.get("bb"):
            group_geo[cluster_alias[name]] = {"rect": _parse_bb(obj["bb"])}
        elif name in alias and obj.get("pos"):
            cx, cy = _parse_pos(obj["pos"])
            width = float(obj.get("width", 1.0)) * _POINTS_PER_INCH
            height = float(obj.get("height", 0.5)) * _POINTS_PER_INCH
            node_geo[alias[name]] = {
                "rect": [cx - width / 2.0, cy - height / 2.0, width, height]
            }

    edges: list[dict[str, Any]] = []
    declared = list(contract["edges"])
    visible = [item for item in payload.get("edges") or [] if item.get("pos")]
    # dot 会把不可见的顺序边也带回来；按声明顺序对齐只取真实边。
    lookup: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in visible:
        tail = index_to_name.get(item.get("tail"), "")
        head = index_to_name.get(item.get("head"), "")
        lookup.setdefault((tail, head), []).append(item)
    for edge in declared:
        key = (_dot_id(edge["from"]), _dot_id(edge["to"]))
        bucket = lookup.get(key) or []
        item = bucket.pop(0) if bucket else None
        if item is None:
            edges.append({"declared": edge, "points": None})
            continue
        points = _bezier_to_polyline(item["pos"])
        label_xy = None
        if item.get("lp"):
            lx, ly = _parse_pos(item["lp"])
            label_xy = [lx, ly]
        edges.append({"declared": edge, "points": points, "label_xy": label_xy})

    return node_geo, group_geo, edges, _parse_bb(payload["bb"])


def layout_unit(
    contract: dict[str, Any],
    sizes: dict[str, tuple[float, float]],
    *,
    members: list[str],
    groups: list[dict[str, Any]],
) -> dict[str, Any]:
    """一个单元（顶层组 + 它的后代）内部的几何，坐标相对单元自身原点。

    返回 {nodes, groups, edges, size} —— 由调用方平移到页面上的位置。
    """

    payload = run_dot(build_unit_dot(contract, sizes, members=members, groups=groups))
    alias = {_dot_id(nid): nid for nid in members}
    cluster_alias = {f"cluster_{_dot_id(g['id'])}": g["id"] for g in groups}
    index_to_name: dict[int, str] = {}
    nodes: dict[str, list[float]] = {}
    group_rects: dict[str, list[float]] = {}

    for index, obj in enumerate(payload.get("objects") or []):
        name = str(obj.get("name") or "")
        index_to_name[obj.get("_gvid", index)] = name
        if name in cluster_alias and obj.get("bb"):
            group_rects[cluster_alias[name]] = _parse_bb(obj["bb"])
        elif name in alias and obj.get("pos"):
            cx, cy = _parse_pos(obj["pos"])
            width = float(obj.get("width", 1.0)) * _POINTS_PER_INCH
            height = float(obj.get("height", 0.5)) * _POINTS_PER_INCH
            nodes[alias[name]] = [cx - width / 2.0, cy - height / 2.0, width, height]

    keep = set(members)
    declared = [
        e for e in contract["edges"] if e["from"] in keep and e["to"] in keep
    ]
    lookup: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in payload.get("edges") or []:
        if not item.get("pos"):
            continue
        key = (index_to_name.get(item.get("tail"), ""), index_to_name.get(item.get("head"), ""))
        lookup.setdefault(key, []).append(item)

    edges: list[dict[str, Any]] = []
    for edge in declared:
        bucket = lookup.get((_dot_id(edge["from"]), _dot_id(edge["to"]))) or []
        item = bucket.pop(0) if bucket else None
        if item is None:
            continue
        label_xy = None
        if item.get("lp"):
            label_xy = list(_parse_pos(item["lp"]))
        edges.append(
            {
                "declared": edge,
                "points": _bezier_to_polyline(item["pos"]),
                "label_xy": label_xy,
            }
        )
    bb = _parse_bb(payload["bb"])
    return {
        "nodes": nodes,
        "groups": group_rects,
        "edges": edges,
        "origin": [bb[0], bb[1]],
        "size": [bb[2], bb[3]],
    }

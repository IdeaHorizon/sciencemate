"""可读性也是机械量 —— 不只靠模型审美。

## 为什么要有这个模块

上一轮（图合同）解决的是**画得对**：声明 8 条边就画 8 条，结构不可能与声明
分叉。但 2026-09-16 真跑出来的图仍然读不了，而记录里只有一条极粗的判据
（长宽比），于是「读不了」这件事在账上几乎不留痕：

- 8 条上联全下到图底、横穿整张图宽、再挤进一个小方块 —— 谁连谁追不了；
- `ROCE NIC 0` 该接正上方的 Switch 0，实际线从 Switch 0 右下角出来、穿
  GPU3/GPU4 的缝、再钩回 NIC 0 的右上角；
- 4 台服务器画了 4 遍**完全相同**的内部结构，信息量没增加、面积翻四倍；
- CPU 行→Switch 行、GPU 行→NIC 行各空掉框高四分之一。

这些全都是可计算的。把它们算出来，改进就能拿数字说话，而不是「我觉得好看
些了」—— 这与 [[feedback_guidance_is_not_mechanism_measured]] 是同一条：
**当能力前先量它改没改变行为**。

## 度量与判据的分工

本模块只**算**，不判「好不好」。阈值与 finding 文案在调用方
（`diagram_compiler.layout_contract`）—— 度量是证据，阈值是义务。
"""

from __future__ import annotations

import math
from typing import Any

#: 判为「线贴着无关组件走」的距离（pt）。比 NODE_GAP 的一半略小。
TOO_CLOSE_PT = 6.0


def _segments(edge: dict[str, Any]) -> list[tuple[float, float, float, float]]:
    points = edge["points"]
    return [
        (points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        for i in range(len(points) - 1)
    ]


def _orientation(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> int:
    value = (by - ay) * (cx - bx) - (bx - ax) * (cy - by)
    if abs(value) < 1e-9:
        return 0
    return 1 if value > 0 else 2


def _on_segment(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> bool:
    return (
        min(ax, bx) - 1e-9 <= cx <= max(ax, bx) + 1e-9
        and min(ay, by) - 1e-9 <= cy <= max(ay, by) + 1e-9
    )


def _segments_cross(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """两条线段是否**真正**相交。

    共享端点不算交叉 —— 同一个端口出来的多条线在起点必然重合，把那算成交叉
    会让扇出结构永远「交叉数爆表」，度量就失去了分辨力。
    """

    ax, ay, bx, by = a
    cx, cy, dx, dy = b
    ends_a = {(round(ax, 3), round(ay, 3)), (round(bx, 3), round(by, 3))}
    ends_b = {(round(cx, 3), round(cy, 3)), (round(dx, 3), round(dy, 3))}
    if ends_a & ends_b:
        return False
    o1 = _orientation(ax, ay, bx, by, cx, cy)
    o2 = _orientation(ax, ay, bx, by, dx, dy)
    o3 = _orientation(cx, cy, dx, dy, ax, ay)
    o4 = _orientation(cx, cy, dx, dy, bx, by)
    if o1 != o2 and o3 != o4:
        return True
    # **共线重叠不是交叉。** 交叉是「追不了哪条线通向哪里」；两条线叠在同一条
    # 直线上是另一回事 —— 读者看到的是一条线。总线画法（一排节点共用一条横干线）
    # 靠的正是这个，把它算成交叉会让总线永远「交叉数爆表」。
    # 叠线本身该不该报，由 collinear_overlaps 单独回答（2026-09-17 拆开）。
    if o1 == 0 and o2 == 0 and o3 == 0 and o4 == 0:
        return False
    if o1 == 0 and _on_segment(ax, ay, bx, by, cx, cy):
        return True
    if o2 == 0 and _on_segment(ax, ay, bx, by, dx, dy):
        return True
    if o3 == 0 and _on_segment(cx, cy, dx, dy, ax, ay):
        return True
    if o4 == 0 and _on_segment(cx, cy, dx, dy, bx, by):
        return True
    return False


def collinear_overlaps(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """哪两条线叠在了同一条直线上 —— 读者看到的是一条线，另一条等于没画。

    只有**共用一条总线**时这是有意为之（一排节点挂在同一条干线上），那时两条边
    带同一个 bus 标记；其余情况是缺陷，而且以前被算成「交叉」混在一起报，
    指不出真身。
    """

    out: list[dict[str, Any]] = []
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            left, right = edges[i], edges[j]
            if left.get("bus") and left.get("bus") == right.get("bus"):
                continue
            hit = False
            for sa in _segments(left):
                for sb in _segments(right):
                    ax, ay, bx, by = sa
                    cx, cy, dx, dy = sb
                    if (
                        _orientation(ax, ay, bx, by, cx, cy) == 0
                        and _orientation(ax, ay, bx, by, dx, dy) == 0
                        and (
                            _on_segment(ax, ay, bx, by, cx, cy)
                            or _on_segment(ax, ay, bx, by, dx, dy)
                            or _on_segment(cx, cy, dx, dy, ax, ay)
                        )
                    ):
                        # 只共享一个端点（扇出/扇入）不算叠线
                        ends_a = {(round(ax, 3), round(ay, 3)), (round(bx, 3), round(by, 3))}
                        ends_b = {(round(cx, 3), round(cy, 3)), (round(dx, 3), round(dy, 3))}
                        if len(ends_a & ends_b) == 1 and not (
                            _on_segment(ax, ay, bx, by, cx, cy)
                            and _on_segment(ax, ay, bx, by, dx, dy)
                        ):
                            continue
                        hit = True
                        break
                if hit:
                    break
            if hit:
                out.append(
                    {
                        "a": f"{left['from']}->{left['to']}",
                        "b": f"{right['from']}->{right['to']}",
                    }
                )
    return out


def edge_crossings(edges: list[dict[str, Any]]) -> int:
    """图里有多少处连线交叉。交叉是「追不了哪条线通向哪里」的直接成因。"""

    all_segments: list[tuple[int, tuple[float, float, float, float]]] = []
    for index, edge in enumerate(edges):
        for segment in _segments(edge):
            all_segments.append((index, segment))
    count = 0
    for i in range(len(all_segments)):
        edge_i, seg_i = all_segments[i]
        for j in range(i + 1, len(all_segments)):
            edge_j, seg_j = all_segments[j]
            if edge_i == edge_j:
                continue
            # **同一条总线上的接头不是交叉。** 一排节点垂到同一条横干线上，
            # 支线搭在干线上是这种画法的全部含义；把每个接头算成交叉，总线
            # 永远「交叉数爆表」（2026-09-17 实测 18 条总线边报出 144 个交叉）。
            bus = edges[edge_i].get("bus")
            if bus and bus == edges[edge_j].get("bus"):
                continue
            if _segments_cross(seg_i, seg_j):
                count += 1
    return count


def edge_lengths(edges: list[dict[str, Any]]) -> tuple[float, float]:
    """(总长, 最长一条)。总长是布局紧凑度最直接的代理量。"""

    total = 0.0
    longest = 0.0
    for edge in edges:
        length = sum(
            math.hypot(x1 - x0, y1 - y0) for x0, y0, x1, y1 in _segments(edge)
        )
        total += length
        longest = max(longest, length)
    return total, longest


def _distance_point_to_rect(px: float, py: float, rect: list[float]) -> float:
    x, y, w, h = rect
    dx = max(x - px, 0.0, px - (x + w))
    dy = max(y - py, 0.0, py - (y + h))
    return math.hypot(dx, dy)


def _distance_segment_to_rect(
    segment: tuple[float, float, float, float], rect: list[float]
) -> float:
    x0, y0, x1, y1 = segment
    best = min(
        _distance_point_to_rect(x0, y0, rect), _distance_point_to_rect(x1, y1, rect)
    )
    steps = 12
    for k in range(1, steps):
        t = k / steps
        best = min(
            best,
            _distance_point_to_rect(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, rect),
        )
    return best


def edges_grazing_nodes(
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    contract: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """哪些连线贴着**与它无关**的组件走。

    贴着走的线读起来像「连到了那个组件」—— 图上最常见的一类误读，而它既不是
    交叉也不是重叠，现有的任何检查都看不见。

    **扇出的同胞不算**：`sw→g7` 必然从 `g6` 旁边过，因为它们都是 sw 的下游、
    又排在同一行。读者不会把它误读成「连到了 g6」。校准集（参考图）唯一剩下的
    3 条误报全是这种形状 —— 判据要认得「几何固有」和「真误导」的区别。
    """

    siblings: dict[str, set[str]] = {}
    if contract:
        children: dict[str, set[str]] = {}
        for edge in between_nodes(contract.get("edges")):
            children.setdefault(edge["from"], set()).add(edge["to"])
            children.setdefault(edge["to"], set()).add(edge["from"])
        for parent, kids in children.items():
            for kid in kids:
                siblings.setdefault(kid, set()).update(kids - {kid})

    out: list[dict[str, Any]] = []
    for edge in edges:
        endpoints = {edge["from"], edge["to"]}
        for node_id, node in nodes.items():
            if node_id in endpoints:
                continue
            # 同一个上游的同胞：扇出几何，不是误导。
            if node_id in siblings.get(edge["to"], set()) or node_id in siblings.get(
                edge["from"], set()
            ):
                continue
            for segment in _segments(edge):
                if _distance_segment_to_rect(segment, node["rect"]) < TOO_CLOSE_PT:
                    out.append({"edge": [edge["from"], edge["to"]], "grazes": node_id})
                    break
    return out


def ink_ratio(geometry: dict[str, Any]) -> float:
    """组件盒占画布的面积比。太低 = 一张图大部分是空白和长线。"""

    canvas_w, canvas_h = geometry["canvas"]
    area = canvas_w * canvas_h
    if area <= 0:
        return 0.0
    boxes = sum(node["rect"][2] * node["rect"][3] for node in geometry["nodes"].values())
    return boxes / area


def isomorphic_groups(contract: dict[str, Any]) -> list[list[str]]:
    """哪些组的**结构完全同构**（rank 形状 + 每个位置的 role + 组内边的 role 对）。

    4 台一模一样的服务器全展开画四遍，信息量一点没增加、面积翻了四倍 ——
    这是信息设计上最贵的一种浪费，而它是**机械可判**的：把每个组归一成一个
    结构指纹，指纹相同的就是同构。

    比的是 role 不是 label：`GPU 0`/`GPU 1` 标签不同但角色相同，结构上是同一
    个东西。比 label 会让所有组都「不同构」，这道判据就永远不响。
    """

    role_of = {node["id"]: node["role"] for node in contract.get("nodes") or []}
    group_of: dict[str, str] = {}
    for group in contract.get("groups") or []:
        for rank in group["ranks"]:
            for nid in rank:
                group_of[nid] = group["id"]

    inner_edges: dict[str, list[tuple[str, str]]] = {}
    for edge in between_nodes(contract.get("edges")):
        left, right = group_of.get(edge["from"]), group_of.get(edge["to"])
        if left is not None and left == right:
            pair = tuple(sorted((role_of[edge["from"]], role_of[edge["to"]])))
            inner_edges.setdefault(left, []).append(pair)  # type: ignore[arg-type]

    fingerprints: dict[str, list[str]] = {}
    for group in contract.get("groups") or []:
        shape = [[role_of[nid] for nid in rank] for rank in group["ranks"]]
        signature = repr(
            (shape, sorted(inner_edges.get(group["id"], [])), group["label_position"])
        )
        fingerprints.setdefault(signature, []).append(group["id"])

    # **互相连着的同构组不是冗余。** 两个 PCIe 域内部结构一样，但它们之间有一
    # 条交换机互联（400G / 0.1µs）—— 折成「×2」就把这条边弄丢了，而这条边正
    # 是这张图要讲的事情之一。
    #
    # 这条判据从 iter1 到 iter12 每次都报，agent 每次都正确驳回。**一条没人
    # 采纳的判据比没有更糟**：它教会模型「findings 可以不理」。冗余的正解是
    # 「彼此可互换」，而连着对方的两个东西不可互换。
    adjacent: set[tuple[str, str]] = set()
    for edge in between_nodes(contract.get("edges")):
        left, right = group_of.get(edge["from"]), group_of.get(edge["to"])
        if left is not None and right is not None and left != right:
            adjacent.add((left, right))
            adjacent.add((right, left))

    def _linked_to_each_other(ids: list[str]) -> bool:
        return any(
            (left, right) in adjacent
            for index, left in enumerate(ids)
            for right in ids[index + 1:]
        )

    return [
        sorted(ids)
        for ids in fingerprints.values()
        if len(ids) > 1 and not _linked_to_each_other(ids)
    ]


def between_nodes(edges: Any) -> list[dict[str, Any]]:
    """只留两端都是**节点**的边。

    端点写成 `group:<id>` 的边说的是「这一整个组」—— 底下这些判据问的都是节点
    层面的问题（谁和谁挨着、哪一排其实是两个子系统、谁的度数最高），拿一个组
    去当节点索引会直接 KeyError，而那个异常会被上层吞掉、悄悄回落到另一个布局
    引擎（2026-09-17 踩到：真实症状显示成一个完全无关的 `KeyError: 'grafana'`）。
    """

    return [
        edge
        for edge in (edges or [])
        if not str(edge.get("from", "")).startswith("group:")
        and not str(edge.get("to", "")).startswith("group:")
    ]


def measure(geometry: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """一次算齐。进 figure 记录，逐版可比 —— 「越改越差」在可读性上也要看得见。"""

    canvas_w, canvas_h = geometry["canvas"]
    edges = geometry["edges"]
    total_length, longest = edge_lengths(edges)
    grazing = edges_grazing_nodes(edges, geometry["nodes"], contract)
    duplicates = isomorphic_groups(contract)
    return {
        "aspect_ratio": round(canvas_w / canvas_h, 2) if canvas_h else None,
        "canvas_pt": [round(canvas_w, 1), round(canvas_h, 1)],
        "edge_crossings": edge_crossings(edges),
        # 交叉与叠线是两件事，拆开各报各的（2026-09-17）。
        "collinear_overlaps": collinear_overlaps(edges),
        "edge_length_total_pt": round(total_length, 1),
        "edge_length_longest_pt": round(longest, 1),
        "edge_length_mean_pt": round(total_length / len(edges), 1) if edges else 0.0,
        "edges_grazing_nodes": len(grazing),
        "grazing_samples": grazing[:8],
        "ink_ratio": round(ink_ratio(geometry), 4),
        # 「空」的正确度量：内容之外的浪费，不是元件之间的必要间隔。
        **empty_bands(geometry),
        "detached_decorations": detached_decorations(geometry),
        "isomorphic_group_sets": duplicates,
        "splittable_ranks": splittable_ranks(contract),
        "rank_spanning_edges": rank_spanning_edges(contract),
        # 最长边 / 平均边长：「绕大弯」的定量症状。
        "edge_length_ratio": (
            round(longest / (total_length / len(edges)), 2)
            if edges and total_length
            else None
        ),
        "node_count": len(geometry["nodes"]),
        "edge_count": len(edges),
    }


def splittable_ranks(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """哪些「行」其实是好几个子系统挤在一起。

    2026-09-16 实测：框架报了「交叉太多，把子系统声明成嵌套子组」，agent 照做
    了 —— 但只声明了**一个**子组，把 8 张 GPU 全塞进它的一行。于是两个 switch
    仍然各自扇出到同一排，「每 4 张归一个 switch」还是没进结构，扇线照旧一团。

    机制送到了，用了一半。差的是一条更精确的判据：**这一行的节点各自只连一个
    上游，而这些上游分成了 N 组 —— 那这行就是 N 个子系统**。这是纯拓扑事实，
    数得出来，不需要看图也不需要模型判断。
    """

    out: list[dict[str, Any]] = []
    neighbours: dict[str, set[str]] = {n["id"]: set() for n in contract.get("nodes") or []}
    for edge in between_nodes(contract.get("edges")):
        neighbours[edge["from"]].add(edge["to"])
        neighbours[edge["to"]].add(edge["from"])

    for group in contract.get("groups") or []:
        for index, rank in enumerate(group["ranks"]):
            if len(rank) < 4:
                continue
            # 只看「各自恰好一个上游」的成员：那种节点的归属是没有歧义的。
            owners: dict[str, list[str]] = {}
            for nid in rank:
                peers = neighbours.get(nid) or set()
                if len(peers) != 1:
                    owners = {}
                    break
                owners.setdefault(next(iter(peers)), []).append(nid)
            if len(owners) > 1:
                # **各家的成员在这一行里是不是连成一段。**
                #
                # 连成一段时，「谁管谁」已经靠位置邻近表达清楚了（sw0 在左、
                # GPU0–3 在左），那是完全合法的编排 —— 用户点名「高级得多」的
                # 那张参考图就是这么画的。交错才是真问题：sw0/sw1/sw0/sw1 排一行，
                # 两把扇子必然互相穿。
                #
                # 这条判据过去用一个**全图**比值（最长边/平均边长 > 3.0）当开关。
                # 那是错的：它让「图里别处变好」也能把这条点着 —— 2026-09-17 实测，
                # 把栏② 的上联拉直之后短边更短、平均下降，比值 2.79 → 3.17 越过阈值，
                # 于是这条**本来就一直存在**的划分被放出来报了。
                # **判一个局部结构，开关就得是局部的。**
                order = {nid: pos for pos, nid in enumerate(rank)}
                contiguous = True
                for ids in owners.values():
                    spots = sorted(order[i] for i in ids)
                    if spots[-1] - spots[0] != len(spots) - 1:
                        contiguous = False
                        break
                out.append(
                    {
                        "group": group["id"],
                        "rank_index": index,
                        "partition": {owner: sorted(ids) for owner, ids in owners.items()},
                        "contiguous": contiguous,
                    }
                )
    return out


def rank_spanning_edges(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """组内端点相隔 >1 级的边 —— 「连线绕大弯」的**结构性**来源。

    2026-09-17 量化：agent 连续两次把一台服务器声明成 [switch] → [8×GPU] →
    [NIC]，于是 switch→NIC 必须横穿整条 GPU 级，最长边达到均值的 3.1 倍。

    这不是布线器选得差，是**声明的层次让它没有近路可走**。两张参考图都避开了
    这个形状：把 switch 的两类下游放到它的**两侧**（GPU 在上、NIC 在下），
    两条边就都只跨一级。可机械判、可给出具体改法，所以由框架说出来。
    """

    rank_of: dict[str, tuple[str, int]] = {}
    for group in contract.get("groups") or []:
        for index, rank in enumerate(group["ranks"]):
            for nid in rank:
                rank_of[nid] = (group["id"], index)
    out: list[dict[str, Any]] = []
    for edge in between_nodes(contract.get("edges")):
        left, right = rank_of.get(edge["from"]), rank_of.get(edge["to"])
        if not left or not right or left[0] != right[0]:
            continue
        span = abs(left[1] - right[1])
        if span > 1:
            out.append(
                {"edge": [edge["from"], edge["to"]], "group": left[0], "span": span}
            )
    return out


def _all_drawn_boxes(
    geometry: dict[str, Any], *, containers: bool = True
) -> list[list[float]]:
    """图上**所有**画出来的东西的包围盒 —— 节点、组框、分栏、连线，以及
    每一种装饰件（端口块、引出箭头与文字、规格条目、段落注记）。

    2026-09-17 两次踩同一族盲区：端口块跑到别的分栏（度量全绿），以及我自己
    量分栏留白时漏掉引出标注、差点去「修」一段其实站着内容的空间。
    **每加一种装饰件，几何度量就多一个盲区** —— 除非它们都从这一个口子进。
    """

    # **容器不算内容**：分栏框和组框覆盖了面积，但里面可能是空的。
    #   empty_bands 问「有没有整片没用到的区域」→ 容器不算，否则一个大而空的
    #   分栏会把自己填满，判据永远不响（改这条时实测空行 12→2，是过校正）。
    boxes: list[list[float]] = []
    for item in (geometry.get("nodes") or {}).values():
        boxes.append(list(item["rect"]))
        for port in item.get("ports") or []:
            boxes.append(list(port))
    if containers:
        for item in (geometry.get("groups") or {}).values():
            boxes.append(list(item["rect"]))
        for panel in geometry.get("panels") or []:
            boxes.append(list(panel["rect"]))
    for edge in geometry.get("edges") or []:
        xs = [p[0] for p in edge["points"]]
        ys = [p[1] for p in edge["points"]]
        boxes.append([min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)])
    for annot in geometry.get("annotations") or []:
        xs = [annot["start"][0], annot["end"][0], annot["text_xy"][0]]
        ys = [annot["start"][1], annot["end"][1], annot["text_xy"][1]]
        boxes.append([min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)])
    # 文字条目**按它真正占的地方**算，不是一个 1×1 的锚点。原先记成 1px，
    # 于是一整个「规格摘要」分栏（130px 高、半张画布宽）在 empty_bands 眼里
    # 是空的 —— 判据看不见文字，就把有内容的地方判成空的。empty_rows 十几轮
    # 稳定报 11-12 而我一直以为是排版浪费，其实是它压根没看见 spec/note
    # （2026-09-17）。
    # 大标题和图例也是内容。它们不在 nodes 里，于是几何判据看不见 —— 又是
    # 「每加一种画出来的东西就多一个盲区」。盒子由编译器交出来，判据不自己
    # 重算版面（一个问题一个真相源）。
    # 版面级图注也是画出来的文字（同族盲区第 6 次）。它常常是把画布撑宽的那一项，
    # 不算它，判据会把「被长注记撑宽的画布」误判成一大片空白。
    notes = [str(text) for text in geometry.get("notes") or []]
    if notes and geometry.get("notes_y") is not None:
        size = float((geometry.get("fonts") or {}).get("note") or 9.5)
        widest = max(_text_extent(text, size) for text in notes)
        boxes.append([
            geometry["canvas"][0] / 2.0 - widest / 2.0,
            float(geometry["notes_y"]),
            widest,
            size * 1.7 * len(notes),
        ])
    # 生命线是画出来的竖线（时序图里参与者底下那一条）—— 同族盲区第 5 次。
    for life in geometry.get("lifelines") or []:
        boxes.append([life["x"] - 1.0, life["bottom"], 2.0, life["top"] - life["bottom"]])
    for key in ("title_box", "legend_box"):
        box = geometry.get(key)
        if box:
            boxes.append(list(box))
    # 分栏标题同理 —— 它是文字，不在 nodes 里（同族盲区第 4 次）。
    for panel in geometry.get("panels") or []:
        if panel.get("title") and panel.get("title_xy"):
            x, y = panel["title_xy"]
            size = float((geometry.get("fonts") or {}).get("panel") or 13.0)
            width = _text_extent(panel["title"], size)
            if panel.get("number"):
                width += size * 2.2
            boxes.append([x, y, width, size * 1.3])
    fonts = geometry.get("fonts") or {}
    for key, font_key, anchor in (
        ("spec_items", "spec", "left"),
        ("block_notes", "note", "center"),
    ):
        size = float(fonts.get(font_key) or 10.0)
        for item in geometry.get(key) or []:
            x, y = item["xy"]
            width = _text_extent(str(item.get("text") or ""), size)
            left = x - width / 2.0 if anchor == "center" else x
            boxes.append([left, y - size * 0.25, width, size * 1.2])
    return boxes


def _text_extent(text: str, size: float) -> float:
    """够用的文字宽度估计：CJK 按一个字宽，其余按 0.55。

    这里不追求精确 —— 判据问的是「这一整条横带上有没有东西」，差几个像素
    不改变答案；记成 1px 才会改变答案。
    """

    return sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in text) * size


#: 版面边距（与 diagram_compiler.MARGIN 同一个数）。判据要把它排除在分母外。
_MARGIN_PT = 30.0


def empty_bands(geometry: dict[str, Any], slices: int = 24) -> dict[str, int]:
    """画布里有多少条整行/整列完全没有内容 —— 整片浪费，可数。

    比包围盒更细：包围盒撑满了，中间仍可能有一整条空带（例如一个分栏内容只
    占上半部分）。
    """

    # 扫的是**图真正占用的那块区域**，不是整张画布 —— 页边距是设计的一部分，
    # 把它算进来只会让判据跟画布宽窄挂钩：同样 30px 的边距，窄图占 2 格、宽图
    # 占 0 格，于是同一张好图在窄版面上被判成浪费（2026-09-17 实测，schema
    # example 正是这么被自己的判据咬了一口）。
    frame = _all_drawn_boxes(geometry, containers=True)
    content = _all_drawn_boxes(geometry, containers=False)
    if not frame:
        return {"empty_rows": 0, "empty_cols": 0, "slices": slices}
    left = min(b[0] for b in frame)
    right = max(b[0] + b[2] for b in frame)
    bottom = min(b[1] for b in frame)
    top = max(b[1] + b[3] for b in frame)
    width, height = max(right - left, 1e-6), max(top - bottom, 1e-6)
    rows = cols = 0
    for index in range(slices):
        lo_y = bottom + height * index / slices
        hi_y = bottom + height * (index + 1) / slices
        if not any(b[1] < hi_y and b[1] + b[3] > lo_y for b in content):
            rows += 1
        lo_x = left + width * index / slices
        hi_x = left + width * (index + 1) / slices
        if not any(b[0] < hi_x and b[0] + b[2] > lo_x for b in content):
            cols += 1
    return {"empty_rows": rows, "empty_cols": cols, "slices": slices}


def detached_decorations(geometry: dict[str, Any]) -> list[dict[str, Any]]:
    """装饰件有没有跑出它的宿主 —— 端口块、标注引线、分栏内容各归各位。

    2026-09-17：page 平移节点时只移了 rect 没移 ports，8 个端口小方块留在
    block 内坐标上、落到另一个分栏的文字上。**当时所有度量全绿** —— 交叉、
    贴线、包围盒都不看装饰件，于是这种错位对机械判据完全隐形，只有看图才发现。

    一条通用判据：凡是「属于某个东西」的图元，就必须落在那个东西里。写死在
    这里比每加一种装饰件都靠肉眼守可靠。
    """

    out: list[dict[str, Any]] = []
    for nid, node in (geometry.get("nodes") or {}).items():
        x, y, w, h = node["rect"]
        for px, py, pw, ph in node.get("ports") or []:
            inside_x = x - 1 <= px and px + pw <= x + w + 1
            on_edge = min(abs(py + ph / 2 - y), abs(py + ph / 2 - (y + h))) <= ph
            if not (inside_x and on_edge):
                out.append({"kind": "port", "owner": nid, "rect": [px, py, pw, ph]})

    panels = [p["rect"] for p in geometry.get("panels") or []]
    if panels:
        for item in geometry.get("spec_items") or []:
            x, y = item["xy"]
            if not any(
                px - 1 <= x <= px + pw + 1 and py - 1 <= y <= py + ph + 1
                for px, py, pw, ph in panels
            ):
                out.append({"kind": "spec_item", "owner": "panel", "xy": [x, y]})
    return out


def layout_digest(geometry: dict[str, Any]) -> dict[str, Any]:
    """图上**从上到下、从左到右都有什么** —— 机械写出来交给作者。

    为什么有它：iter15 实测 17 轮 / 79.5 万 token，其中一大块是 agent 在
    `execute_python` 里裁 PNG、手算 pt→px，只为了看看自己画的图长什么样
    （"图片高 4235px 对应 1016.45pt，比例 4.166 px/pt…"）。这正是这套合同
    要消灭的那类坐标算术，而它之所以还在，是因为工具只回了**数字**（交叉数、
    填充率），没回**版面**。

    度量回答「有没有毛病」，digest 回答「长什么样」。后者框架完全答得出，
    就不该逼模型去像素里刨 —— [[feedback_framework_makes_the_symptom]]。
    """

    nodes = geometry.get("nodes") or {}

    def _rows(subset: list[str]) -> list[str]:
        """按 y 聚成一行行，每行从左到右 —— 读者的阅读顺序。"""

        remaining = sorted(subset, key=lambda nid: -nodes[nid]["rect"][1])
        out: list[str] = []
        while remaining:
            head = remaining[0]
            hy, hh = nodes[head]["rect"][1], nodes[head]["rect"][3]
            row = [
                nid
                for nid in remaining
                if nodes[nid]["rect"][1] < hy + hh
                and nodes[nid]["rect"][1] + nodes[nid]["rect"][3] > hy
            ]
            remaining = [nid for nid in remaining if nid not in row]
            row.sort(key=lambda nid: nodes[nid]["rect"][0])
            out.append(
                "  ".join(
                    f"{nid}({nodes[nid]['label']})" if nodes[nid].get("label") else nid
                    for nid in row
                )
            )
        return out

    panels = []
    claimed: set[str] = set()
    for panel in geometry.get("panels") or []:
        x, y, w, h = panel["rect"]
        inside = [
            nid
            for nid, node in nodes.items()
            if x <= node["rect"][0] <= x + w and y <= node["rect"][1] <= y + h
        ]
        claimed.update(inside)
        # spec / note 栏里没有节点、只有文字 —— 不写出来，digest 就跟几何判据
        # 犯同一个毛病：看不见文字就说那里是空的。
        text_lines = [
            str(item.get("text") or "")
            for key in ("spec_items", "block_notes")
            for item in geometry.get(key) or []
            if x <= item["xy"][0] <= x + w and y <= item["xy"][1] <= y + h
        ]
        entry = {
            "number": panel.get("number"),
            "title": panel.get("title") or "",
            "rows_top_to_bottom": _rows(inside),
        }
        if text_lines:
            entry["text_lines"] = text_lines
        panels.append(entry)
    loose = [nid for nid in nodes if nid not in claimed]

    routes: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in between_nodes(geometry.get("edges")):
        key = (edge["from"], edge["to"])
        points = edge["points"]
        run = max(
            (abs(points[i + 1][0] - points[i][0]) for i in range(len(points) - 1)),
            default=0.0,
        )
        entry = routes.setdefault(key, {"count": 0, "straight": 0, "longest_sideways": 0.0})
        entry["count"] += 1
        entry["straight"] += 1 if run < 1.0 else 0
        entry["longest_sideways"] = round(max(entry["longest_sideways"], run), 1)

    degrees: dict[str, int] = {}
    for edge in between_nodes(geometry.get("edges")):
        degrees[edge["from"]] = degrees.get(edge["from"], 0) + 1
        degrees[edge["to"]] = degrees.get(edge["to"], 0) + 1

    return {
        "canvas_pt": [round(v, 1) for v in geometry["canvas"]],
        "panels": panels,
        "nodes_outside_any_panel": _rows(loose) if loose else [],
        "edges": [
            {"from": src, "to": dst, **info} for (src, dst), info in routes.items()
        ],
        "ports": {
            nid: {"drawn": len(node.get("ports") or []), "edges_landed": degrees.get(nid, 0)}
            for nid, node in nodes.items()
            if node.get("ports")
        },
        "callouts": [
            {
                "anchor": a.get("anchor"),
                # 几何里只留了起讫点，方向现算 —— 别报一个恒为 None 的字段
                # （[[feedback_from_never_updated_field_is_not_a_fact]]）。
                "points": (
                    "down" if a["end"][1] < a["start"][1]
                    else "up" if a["end"][1] > a["start"][1]
                    else "right" if a["end"][0] > a["start"][0] else "left"
                ),
                "text": a.get("text"),
            }
            for a in geometry.get("annotations") or []
        ],
        "collapsed_units": {
            nid: node["detail_of"]
            for nid, node in nodes.items()
            if node.get("detail_of")
        },
        "note": (
            "这是图上真正画出来的东西，按读者的阅读顺序列出 —— 不必再去裁图或"
            "换算坐标。要改哪里，改合同里对应的那一条。"
        ),
    }

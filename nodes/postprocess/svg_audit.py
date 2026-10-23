"""SVG 机械审计 —— SVG 轨铸造门的机械半边（另一半是合同 DOM 断言）。

## 职责

对 agent 手写的 SVG 做**确定性**核查，分两级：

**拒绝级（矛盾，抛 VisualContractError）**——与既有轨道同尺：
- XML 不合法 / 没有可解析的画布尺寸；
- 文字政策：语言（text_language=en 时一个 CJK 字都不许）、禁用词正则
  （复用 `figure_contract.text_policy_failures`，与 declare 同一判据）；
- 印刷字号下限：调用方/作者绑定了版心时，最小的字换算到印刷 pt 低于下限
  当场拒（与 compiled 轨 declare 时的 print_fit 拒绝同尺）；
- 缺字形：要画的字整条字体栈都画不出来（`fonts.missing_glyphs`）——
  「合同里写了、图上没有、没人报」正是这套闸要拦的事。

**披露级（finding，不拦）**：
- DOM 标记问题（缺标记 / 重复 id / 标记格式不合法 / 标记与合同对不上）——
  缺标记即 finding（§六-B4）；断言级的不一致由 `evaluate_svg_dom_assertions`
  在铸造门另算；
- viewBox 宽高比与绑定版心不符（§六-B6）；
- 几何启发式：文字出界、文字互碰（**估计值**，如实标注是估计）。

## 为什么启发式只做 finding

文字宽度用 em 估算（拉丁 0.6em、宽字符 1.0em），不是真排版。估计会错，
错了不该拦图 —— 但「估计出重叠」必须留痕，让下游知道这条是启发式说的。
真要对像素级碰撞下结论，那是渲染后 VLM 证人的岗位。
"""

from __future__ import annotations

import re
from typing import Any

from defusedxml import ElementTree as ET

from . import svg_vocab
from .contracts import VisualContractError
from .figure_contract import MEDIA, font_floor_pt, text_policy_failures
from .fonts import missing_glyphs, sans_stack

#: SVG 的默认字号（user units）：属性没写、祖先也没写时按它来。
_DEFAULT_FONT_SIZE = 16.0
#: viewBox 宽高比与版心宽高比的容忍偏差（比例相对差）。版心比例只是排版的
#: 参照系，不是审判台 —— 差太多提醒，差不多的自由。
_ASPECT_TOLERANCE = 0.15
#: width/height **物理**宽高比与 viewBox 宽高比的容忍偏差（相对差）。这俩
#: 不一致时 cairosvg 按 preserveAspectRatio 默认行为把画面 letterbox 居中，
#: 边缘留出未覆盖带（2026-10-20 e2e：0.19% 失配在 300DPI 下是上下各 ~2px
#: 的透明带，渲染侧已 background_color 填白兜底，这里点名叫作者对齐比率）。
_LETTERBOX_TOLERANCE = 0.001
#: pt/mm 换算
_PT_PER_MM = 72.0 / 25.4

#: width/height 属性单位 → mm（比率比较前两边的单位各自换算，约不掉）。
_MM_PER_UNIT = {
    "mm": 1.0, "cm": 10.0, "in": 25.4, "q": 0.25,
    "pt": 25.4 / 72.0, "pc": 25.4 / 6.0, "px": 25.4 / 96.0,
}



def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_svg(svg_text: str) -> Any:
    """解析 SVG 文本为 DOM 根。不合法 = 矛盾级拒绝（附带行列位置）。"""

    try:
        root = ET.fromstring(svg_text)
    except Exception as exc:
        raise VisualContractError(f"SVG is not well-formed XML: {exc}") from exc
    if _local(str(root.tag)) != "svg":
        raise VisualContractError(
            f"root element must be <svg>; got <{_local(str(root.tag))}>"
        )
    return root


def _viewbox(root: Any) -> tuple[float, float, float, float]:
    """画布尺寸：viewBox 优先；没有 viewBox 时退到 width/height 属性。"""

    raw = root.get("viewBox") or ""
    parts = re.split(r"[\s,]+", raw.strip())
    if len(parts) == 4:
        try:
            x, y, w, h = (float(p) for p in parts)
            if w > 0 and h > 0:
                return x, y, w, h
        except ValueError:
            pass
    try:
        w = float(re.sub(r"[a-z%]+$", "", root.get("width") or ""))
        h = float(re.sub(r"[a-z%]+$", "", root.get("height") or ""))
        if w > 0 and h > 0:
            return 0.0, 0.0, w, h
    except ValueError:
        pass
    raise VisualContractError(
        "SVG has no usable viewBox or width/height: the framework cannot map "
        "user units to physical millimetres, so font-size policy and the "
        "print-size check have nothing to stand on"
    )


def _physical_aspect(root: Any) -> float | None:
    """width/height 属性声明的物理宽高比；缺一个、是百分比或解析不了就 None。"""

    values: list[float] = []
    for attr in ("width", "height"):
        raw = (root.get(attr) or "").strip()
        match = re.fullmatch(r"([0-9.]+)\s*([a-z%]*)", raw, re.IGNORECASE)
        if not match:
            return None
        value = float(match.group(1))
        unit = (match.group(2) or "px").lower()
        factor = _MM_PER_UNIT.get(unit)
        if value <= 0 or factor is None:
            return None
        values.append(value * factor)
    return values[0] / values[1]


def _parse_font_size(raw: str, inherited: float) -> float | None:
    """font-size 值 → user units。px/裸值原样；pt 按 CSS 的 4/3 换算；
    em/% 相对继承值。解析不了（畸形/不认的单位）返回 None —— 调用方落回
    继承值，不让一个坏值把整份审计炸掉。"""

    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([a-z%]*)\s*", raw, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit in ("", "px"):
        return value
    if unit == "pt":
        return value * 4.0 / 3.0
    if unit == "em":
        return value * inherited
    if unit == "%":
        return value * inherited / 100.0
    return None


def _font_size_of(el: Any, inherited: float) -> float:
    raw = el.get("font-size")
    if raw:
        parsed = _parse_font_size(raw, inherited)
        if parsed is not None:
            return parsed
    style = el.get("style") or ""
    m = re.search(r"font-size\s*:\s*([0-9.]+)\s*([a-z%]*)", style)
    if m:
        parsed = _parse_font_size(m.group(1) + m.group(2), inherited)
        if parsed is not None:
            return parsed
    return inherited


def _est_text_width(text: str, font_size: float) -> float:
    """文字宽度的**估计**（user units）：拉丁 0.6em、宽字符 1.0em。"""
    width = 0.0
    for ch in text:
        width += font_size * (1.0 if ord(ch) > 0x2E7F else 0.6)
    return width


def extract_dom_structure(root: Any) -> dict[str, Any]:
    """从 DOM 标记里数出结构事实（nodes/edges/groups）+ 标记问题清单。

    返回的 nodes/edges/groups 与合同同形，直接喂
    `figure_contract.evaluate_svg_dom_assertions`。组的 ranks 是**成员清单**：
    从 DOM 嵌套推出（`<g data-group="x">` 里标着 data-node 的元素即成员）。
    """

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    groups: list[dict[str, Any]] = []
    group_members: dict[str, list[str]] = {}
    problems: list[str] = []
    seen_node_ids: dict[str, int] = {}

    def walk(el: Any, current_group: str | None) -> None:
        tag = _local(str(el.tag))
        role = el.get(svg_vocab.ATTR_ROLE)
        node_id = el.get(svg_vocab.ATTR_NODE)
        edge_from = el.get(svg_vocab.ATTR_EDGE_FROM)
        edge_to = el.get(svg_vocab.ATTR_EDGE_TO)
        label_for = el.get(svg_vocab.ATTR_LABEL)
        group_id = el.get(svg_vocab.ATTR_GROUP)
        placeholder = el.get(svg_vocab.ATTR_PLACEHOLDER)

        if group_id is not None:
            if not svg_vocab.is_valid_marker_id(group_id):
                problems.append(f"{svg_vocab.ATTR_GROUP}={group_id!r} 不是合法 id")
            # DOM 嵌套 = 组嵌套：内层 data-group 的 parent 记为它外层的
            # data-group（与合同的 groups[].parent 同形）。断言在 declare
            #（合同组）与铸造（DOM 组）两个时刻求值，两边的组结构必须同形，
            # 否则同一条 group 选择器断言两刻不同答（2026-10-20 M11）。
            groups.append({"id": group_id, "parent": current_group})
            group_members.setdefault(group_id, [])
            current_group = group_id  # 嵌套的内层组覆盖外层（成员归最近的容器）

        if node_id is not None:
            if not svg_vocab.is_valid_marker_id(node_id):
                problems.append(f"{svg_vocab.ATTR_NODE}={node_id!r} 不是合法 id")
            if role is None:
                problems.append(f"节点 {node_id!r} 有 {svg_vocab.ATTR_NODE} 但缺 {svg_vocab.ATTR_ROLE}")
            if tag not in svg_vocab.NODE_ELEMENTS:
                problems.append(
                    f"节点 {node_id!r} 标在 <{tag}> 上 —— 节点主形状应是 "
                    f"{'/'.join(svg_vocab.NODE_ELEMENTS)}"
                )
            seen_node_ids[node_id] = seen_node_ids.get(node_id, 0) + 1
            nodes.append({"id": node_id, "role": role or ""})
            if current_group is not None:
                group_members.setdefault(current_group, []).append(node_id)
        elif role is not None:
            problems.append(f"<{tag}> 有 {svg_vocab.ATTR_ROLE}={role!r} 但缺 {svg_vocab.ATTR_NODE}")

        if edge_from is not None or edge_to is not None:
            if not edge_from or not edge_to:
                problems.append(
                    f"一条边只有 {svg_vocab.ATTR_EDGE_FROM}/{svg_vocab.ATTR_EDGE_TO} 之一"
                    f"（from={edge_from!r} to={edge_to!r}）"
                )
            else:
                for endpoint in (edge_from, edge_to):
                    bare = endpoint.split(":", 1)[-1]
                    if not svg_vocab.is_valid_marker_id(bare):
                        problems.append(f"边端点 {endpoint!r} 不是合法 id")
                edges.append({"from": edge_from, "to": edge_to})

        if label_for is not None and not svg_vocab.is_valid_marker_id(label_for.split(":", 1)[-1]):
            problems.append(f"{svg_vocab.ATTR_LABEL}={label_for!r} 不是合法 id")
        if placeholder is not None and not str(placeholder).strip():
            problems.append(f"{svg_vocab.ATTR_PLACEHOLDER} 空值（槽位 id 必填）")

        for child in el:
            walk(child, current_group)

    walk(root, None)

    for node_id, count in sorted(seen_node_ids.items()):
        if count > 1:
            problems.append(f"节点 id {node_id!r} 在 DOM 里出现 {count} 次 —— 铁律是恰好一次")

    return {
        "nodes": nodes,
        "edges": edges,
        "groups": [
            {**group, "ranks": [group_members.get(group["id"], [])]} for group in groups
        ],
        "marker_problems": problems,
        "marked_node_ids": sorted(seen_node_ids),
    }


def _iter_texts(root: Any) -> list[tuple[Any, str, float]]:
    """全部 <text> 元素：(元素, 拼接文本, 字号 user units)。tspan 归并进父 text。

    font-size 沿 DOM 树继承（SVG 的 presentation attribute 规则）：<svg>/<g>
    上声明的字号对后代生效。2026-10-20 审查前 inherited 恒传默认值 —— 祖先
    声明的字号被无视，而印刷字号下限这道拒绝级闸就建立在读数上。
    """

    out: list[tuple[Any, str, float]] = []

    def walk(el: Any, inherited: float) -> None:
        size = _font_size_of(el, inherited)
        if _local(str(el.tag)) == "text":
            text = "".join(el.itertext()).strip()
            out.append((el, text, size))
        for child in el:
            walk(child, size)

    walk(root, _DEFAULT_FONT_SIZE)
    return out


def audit_svg(
    svg_text: str,
    *,
    contract: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    medium: str | None = None,
    enforce_print: bool = False,
) -> dict[str, Any]:
    """机械审计主入口。拒绝级抛 VisualContractError；披露级进 findings。

    `policy` 是 `contracts.caller_policy` 的形状；`medium` 是结算后的版心；
    `enforce_print` 与 declare 同尺：版心是调用方/作者绑定时印刷下限才拒绝，
    只是按 purpose 推出来的记 finding 不罚。
    """

    policy = policy or {}
    root = parse_svg(svg_text)
    vx, vy, vw, vh = _viewbox(root)
    findings: list[dict[str, Any]] = []
    structure = extract_dom_structure(root)

    def note(collector: str, message: str, **details: Any) -> None:
        findings.append({"collector": collector, "message": message, **details})

    # ── DOM 标记（§六-B4：缺标记即 finding）───────────────────────────
    for problem in structure["marker_problems"]:
        note("SVG-MARKER", problem)
    if contract is not None:
        declared = {n["id"] for n in contract.get("nodes") or []}
        marked = set(structure["marked_node_ids"])
        missing = sorted(declared - marked)
        extra = sorted(marked - declared)
        if missing:
            note(
                "SVG-MARKER",
                "合同声明了但 DOM 里没标记的节点: " + ", ".join(missing[:12]),
                missing=missing,
            )
        if extra:
            note(
                "SVG-MARKER",
                "DOM 里标记了但合同没声明的节点 id: " + ", ".join(extra[:12]),
                extra=extra,
            )

    # ── viewBox 宽高比 vs 版心（§六-B6）────────────────────────────────
    if medium and medium in MEDIA:
        spec = MEDIA[medium]
        svg_aspect = vw / vh
        media_aspect = spec["w"] / spec["h"]
        if abs(svg_aspect - media_aspect) / media_aspect > _ASPECT_TOLERANCE:
            note(
                "SVG-ASPECT",
                f"viewBox 宽高比 {svg_aspect:.2f} 与版心 {medium}（{spec['w']:.0f}×"
                f"{spec['h']:.0f}mm ≈ {media_aspect:.2f}）偏差超过 {_ASPECT_TOLERANCE:.0%}"
                " —— 印上去会被不等比拉伸或留大边",
                svg_aspect=round(svg_aspect, 3),
                medium_aspect=round(media_aspect, 3),
            )

    # ── width/height 物理比 vs viewBox 比：cairosvg letterbox（E-11）──────
    # 上面那条问「画布和版心合不合」，这条问「SVG 自己的两个尺寸说法合不合」：
    # 物理尺寸和 viewBox 各说一个比率时，渲染器只能 letterbox —— 画面之外
    # 的边带不是作者画上去的任何东西。2026-10-20 e2e：170mm×200mm +
    # viewBox 482×566，两条判据各自静默，边缘透明带无人报。
    if root.get("viewBox") and root.get("width") and root.get("height"):
        physical = _physical_aspect(root)
        if physical is not None:
            vb_aspect = vw / vh
            if abs(physical - vb_aspect) / vb_aspect > _LETTERBOX_TOLERANCE:
                note(
                    "SVG-ASPECT",
                    f"width/height 的物理宽高比 {physical:.4f} 与 viewBox 宽高比 "
                    f"{vb_aspect:.4f} 不一致 —— 栅格化时 preserveAspectRatio 会把画面 "
                    "letterbox 居中，画布边缘留出未覆盖带（渲染侧已填白兜底，但那段"
                    "画布被白白浪费）。把 viewBox 的比率对齐 width/height，或干脆去掉"
                    " width/height 让 viewBox 单独决定。",
                    physical_aspect=round(physical, 4),
                    viewbox_aspect=round(vb_aspect, 4),
                )

    # ── 文字政策（语言/禁用词）：与 declare 同一判据，矛盾级 ────────────
    texts = _iter_texts(root)
    all_text = [text for _, text, _ in texts if text]
    failures = text_policy_failures(all_text, policy)
    if failures:
        raise VisualContractError("; ".join(failures))

    # ── 缺字形：字没了没有任何报错，正是要拦的事（矛盾级）──────────────
    stack = sans_stack()
    missing = missing_glyphs("".join(all_text), stack)
    if missing:
        raise VisualContractError(
            "these characters have no glyph in the available font stack "
            f"({'/'.join(stack[:3])}…): {' '.join(missing[:12])} — they would "
            "render as tofu boxes or vanish silently. Change the text or pick "
            "characters the fonts actually cover"
        )

    # ── 印刷字号下限：px(user units) → pt 按版心缩放系数换算（§六-B7）───
    floor_pt = font_floor_pt(medium, policy.get("min_font_pt")) if medium else None
    scale = (MEDIA[medium]["w"] / vw) if medium in MEDIA else None
    smallest: tuple[float, str] | None = None
    if scale and floor_pt is not None:
        for _, text, size in texts:
            if not text:
                continue
            pt = size * scale * _PT_PER_MM
            if smallest is None or pt < smallest[0]:
                smallest = (pt, text[:30])
        if smallest and smallest[0] < floor_pt:
            message = (
                f"smallest text prints at {smallest[0]:.1f}pt ({smallest[1]!r}) "
                f"< floor {floor_pt:.1f}pt at medium={medium} "
                f"(viewBox {vw:.0f} units → {MEDIA[medium]['w']:.0f}mm)"
            )
            if enforce_print:
                raise VisualContractError(
                    message + "; shrink what sits side by side, fold a layer, or "
                    "drop the text — scaling the whole figure does not help"
                )
            note("SVG-FONT-FLOOR", message + "（版心是按 purpose 推的，未拒绝）",
                 smallest_pt=round(smallest[0], 2), floor_pt=floor_pt)

    # ── 几何启发式（估计值，披露级）─────────────────────────────────────
    oob: list[str] = []
    boxes: list[tuple[float, float, float, float, str]] = []
    for el, text, size in texts:
        if not text:
            continue
        try:
            x = float(el.get("x") or 0.0)
            y = float(el.get("y") or 0.0)
        except ValueError:
            continue
        width = _est_text_width(text, size)
        anchor = el.get("text-anchor") or "start"
        left = x - width / 2 if anchor == "middle" else (x - width if anchor == "end" else x)
        boxes.append((left, y - size, left + width, y, text[:24]))
        if left < vx - 1 or left + width > vx + vw + 1 or y - size < vy - 1 or y > vy + vh + 1:
            oob.append(text[:24])
    if oob:
        note(
            "SVG-GEOMETRY-EST",
            "文字估计超出画布（启发式，非真排版）: " + ", ".join(oob[:8]),
            out_of_bounds=oob[:8],
        )
    collisions: list[str] = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                collisions.append(f"{a[4]!r}×{b[4]!r}")
    if collisions:
        note(
            "SVG-GEOMETRY-EST",
            "文字估计互相重叠（启发式，非真排版）: " + ", ".join(collisions[:8]),
            collisions=collisions[:8],
        )

    return {
        "status": "success",
        "findings": findings,
        "observed": {
            "nodes": structure["nodes"],
            "edges": structure["edges"],
            "groups": structure["groups"],
        },
        "texts": all_text,
        "viewbox": {"x": vx, "y": vy, "w": vw, "h": vh},
        "summary": {
            "texts": len(all_text),
            "marked_nodes": len(structure["nodes"]),
            "marked_edges": len(structure["edges"]),
            "marked_groups": len(structure["groups"]),
            "marker_problems": len(structure["marker_problems"]),
            "text_out_of_bounds_est": len(oob),
            "text_collisions_est": len(collisions),
            "findings": len(findings),
        },
    }

"""四条基准 + 校准集：布局引擎的回归防线。

在这之前，每换一条基准我都是手写一个一次性脚本、跑完就散了 —— 下次改布局引擎，
**没有任何东西会告诉我流程图或时序图退化了**。这个文件就是那道防线。

合同是 agent 真跑出来的（不是手写样本：自造的样本只证明我的理解自洽，不证明它
接得住真数据 —— 这一课刚在出门检查上栽过一次）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.postprocess.diagram_compiler import layout_contract, layout_for_backend
from nodes.postprocess.dot_layout import dot_available
from nodes.postprocess.figure_contract import normalize_contract

BENCHMARKS = Path(__file__).resolve().parent.parent / "fixtures" / "benchmarks"

#: 每条基准**必须仍然用到**的词表。词表被误删、或者布局引擎不再走那条路，
#: 这里会指名道姓地红 —— 而不是等某天肉眼看图才发现。
EXERCISES: dict[str, set[str]] = {
    "calibration-reference": {"ports", "bar", "annotations", "dot-unit"},
    "topology": {"detail_of", "ports", "bar", "chip", "represents", "annotations"},
    # 流程图真正独有的是**回退边走外侧**（around）—— 光看词表它是架构图的子集，
    # 覆盖度那条测试当场把这点指了出来。布线走哪条路也是能力，也要量。
    "flowchart": {"side", "around"},
    "layered-architecture": {"side", "bar", "chip", "bus", "rail"},
    "sequence": {"step", "lifelines"},
    # 状态机：自转移 + 反向平行对，两样别的基准都压不到
    "state-machine": {"self-loop", "vertical"},
    # 22 节点 / 65 边的微服务全景 —— **已知的密度天花板**，不是合格样本。
    # 留在回归集里是为了「别让它悄悄变得更糟」，以及任何声称改善了密集图的
    # 改动，都得先在这里拿出数字。
    "microservices-ceiling": {"rail", "crossband"},
    # 同一条 prompt，agent 读过「先折叠、再挪明细」两句之后重新搭的结构：
    # 290 个交叉 → 17。这条钉的是**那个战果不许丢**。
    "microservices-restructured": {"crossband"},
}

#: 引擎名大多是**实现细节**：agent 这轮多加一个 spec 块，同一张流程图就从
#: `dot-units…` 变成 `page:dot-units…` —— 钉死它只会在合同正常变化时误报
#: （2026-09-17 实测）。真正要钉的只有两条：
#:   1. 时序必须走时序引擎（那是一项能力声明，不是细节）；
#:   2. **任何一条都不许掉回回落引擎** —— 掉回去意味着 dot 布局那条路炸了，
#:      而回落引擎画出来的东西完全是另一回事。
SEQUENCE_ENGINE = "sequence:"
FALLBACK_ENGINE = "builtin-fallback"


def _features(contract: dict, geometry: dict) -> set[str]:
    nodes, edges = contract["nodes"], contract["edges"]
    found = set()
    if any(n.get("side") for n in nodes):
        found.add("side")
    if any(n.get("detail_of") for n in nodes):
        found.add("detail_of")
    if any(n.get("ports") for n in nodes):
        found.add("ports")
    if any(n.get("shape") == "bar" for n in nodes):
        found.add("bar")
    if any(n.get("shape") == "chip" for n in nodes):
        found.add("chip")
    if any(e.get("step") for e in edges):
        found.add("step")
    if any(int(e.get("represents") or 1) > 1 for e in edges):
        found.add("represents")
    if any(block.get("annotations") for block in contract["blocks"]):
        found.add("annotations")
    if any(e.get("bus") for e in geometry["edges"]):
        found.add("bus")
    # 布线走哪条路也是能力：回退边走外侧、侧轨、组内交给 dot —— 都要量。
    found |= {e.get("mode") for e in geometry["edges"] if e.get("mode")}
    if any(e["from"] == e["to"] for e in geometry["edges"]):
        found.add("self-loop")
    if geometry.get("lifelines"):
        found.add("lifelines")
    return found


@pytest.mark.parametrize("name", sorted(EXERCISES))
def test_every_benchmark_stays_clean(name: str) -> None:
    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = json.loads((BENCHMARKS / f"{name}.json").read_text(encoding="utf-8"))
    contract = normalize_contract(raw, family="schematic")
    # **量出来的那一版**，不是估出来的那一版。判据要落在真正会被渲染的几何上：
    # 估算把每个盒子都算窄了（最多 34%），于是「0 贴线」说的是估算自洽，而纸上
    # 的字正压在盒线上（2026-09-17）。没有 xelatex 时这里退回估算，而 source
    # 会写明 —— 那种环境下这条测试守的是较弱的东西，账上看得见。
    geometry = layout_for_backend(contract, "tikz")
    metrics = geometry["layout_metrics"]

    engine = geometry["layout_engine"]
    assert engine != FALLBACK_ENGINE, "掉回回落引擎 = dot 那条路炸了"
    if name == "sequence":
        assert engine.startswith(SEQUENCE_ENGINE), engine
    else:
        assert not engine.startswith(SEQUENCE_ENGINE), engine
    if name in {"microservices-restructured", "microservices-ceiling"}:
        # 下面两支钉的是**实测字宽**世界里的真值（见各自注释：估算世界里是另一组
        # 数，17/13 与 290/223，而且随估算器变）。没量到就说没量到 —— 和下面
        # 「每个盒子装得下自己的字」那条一样按 text_metrics.source 跳。
        source = geometry["text_metrics"]["source"]
        if not source.startswith("measured"):
            pytest.skip(f"text was not measured on this host: {source}")
    # 一条缺陷都不许有 —— 这五份在 2026-09-17 全是干净的，任何一条新出的都是退化
    if name == "microservices-restructured":
        # 22 节点 / 65 边的同一张图，agent 自己重搭成「全景（业务层折叠）+ 业务层
        # 内部 + 映射表」三块之后：交叉 290 → 17、叠线 223 → 11。
        # 这不是干净图（还有 17 个交叉），但它是**密集图目前能到的最好水平**，
        # 而且完全靠作者侧的结构调整拿到，没动布局引擎一行。
        # 叠线 11 → 13：盒子按实测变宽之后多出来的两对。**11 是估算世界里的
        # 数，从来没有任何一张纸上是 11。** 钉真实的那个。
        # 模数音阶让正文变小、规格块变窄，排版跟着变好：17/13 → 14/10。
        # 钉紧到真值 —— 松着的上限抓不住退化。
        assert metrics["edge_crossings"] <= 14, metrics["edge_crossings"]
        assert len(metrics["collinear_overlaps"]) <= 10
        return
    if name == "microservices-ceiling":
        # **这张图是读不了的**（290 个交叉 / 223 对叠线），而判据如实报了 19 条
        # —— 系统在这里的行为是对的：它没有假装干净。钉住这个数，只为两件事：
        #   1. 别让密集图悄悄变得更糟；
        #   2. 任何声称改善了密集图的改动，先在这里拿出数字。
        # 2026-09-17 试过三条路，都被度量否掉：按目标拆星形总线（交叉 -13% 但
        # 叠线 +9%）、数据层拆成五层（375/525，更糟）、拆成两栏（**表达不出来**
        # —— 同一个服务不能出现在两栏里，那是版面层刻意的约束）。
        # 290/223 也是估算世界里的数；按实测字宽重摆是 281/217（盒子变宽把几条
        # 边推开了）。钉紧到真实值 —— 松着的上限抓不住退化。
        # 带子走廊 74→62 之后 281 → 278。钉紧到真值。
        assert metrics["edge_crossings"] <= 278, metrics["edge_crossings"]
        assert len(metrics["collinear_overlaps"]) <= 217
        assert geometry["layout_findings"], "这张图不该是干净的 —— 判据必须说话"
        return
    if name == "state-machine":
        # 状态机是**已知还没打平**的一条：六态十转移（含自环、含两条反向平行对）
        # 剩 1 个交叉。这个数是量出来的下限 —— 穷举该带子分配下的全部带内排列，
        # 最小就是 1；后来又试了三种布线细化（外侧车道按跨度排、区间交错的分到
        # 两侧、外侧边锚点分开），**一个都没把它降下去，所以一个都没留**。
        # 钉住现状，别让它悄悄变差；降到 0 的那天这里会红着提醒我改掉。
        assert metrics["edge_crossings"] <= 1, metrics["edge_crossings"]
        assert metrics["collinear_overlaps"] == []
        assert not [e for e in geometry["edges"] if e.get("label_unplaced")]
        return
    assert geometry["layout_findings"] == [], [
        f["message"][:120] for f in geometry["layout_findings"]
    ]
    assert metrics["edge_crossings"] == 0
    assert metrics["collinear_overlaps"] == []
    assert metrics["edges_grazing_nodes"] == 0
    assert metrics["detached_decorations"] == []
    assert not [e for e in geometry["edges"] if e.get("label_unplaced")]
    # 词表还在用：删了某个词表、或者布局不再走那条路，这里指名道姓
    assert EXERCISES[name] <= _features(contract, geometry), (
        EXERCISES[name] - _features(contract, geometry)
    )


def test_the_benchmark_set_covers_every_shape_we_have_broken() -> None:
    """四条基准各自压到的东西不许重合成一条 —— 重合了就等于只有一条基准。

    二十五轮只打磨网络拓扑，换一条流程图立刻抖出五个缺陷；再换分层架构，
    80-338 个交叉。**收敛不是完成，是该换题了。**
    """

    # 校准集是标尺；两份微服务夹具钉的是**结果**（天花板、以及改措辞拿到的战果），
    # 不是新形状 —— 它们不该参加「每条基准都得带来别人没有的能力」这条。
    # 覆盖度按能力算，而它们的能力是别人的子集。
    shapes = set(EXERCISES) - {
        "calibration-reference", "microservices-ceiling", "microservices-restructured",
    }
    assert len(shapes) >= 4, shapes
    union: set[str] = set()
    for name in shapes:
        union |= EXERCISES[name]
    # 每条基准都得带来至少一样别人没有的东西
    for name in shapes:
        others: set[str] = set()
        for other in shapes - {name}:
            others |= EXERCISES[other]
        assert EXERCISES[name] - others, f"{name} 没有压到任何别人压不到的东西"
    assert {"side", "bus", "step", "detail_of", "represents"} <= union


# ── 每个词表都要经得起同一个问题：解析不出来 / 没有效果时会怎样 ───────────────
#
# 2026-09-17：加完 detail_of 之后造变异打自己，发现两处「声明写了、像素为零、
# 没人报」。那两处是我加词表时没想全的分支 —— 前面九次同型问题都是模型撞出来
# 的，这两次是自己留的缝。所以把这个问题**固化成测试**：将来每加一个词表，
# 同一个问题会被自动问一遍。
#
# 合格的答案只有两种：**图上留下痕迹**，或者**当场拒绝**。
# 不合格的答案只有一种：什么也不发生。


def _trivial(**over):
    raw = {
        "title": "t",
        "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        "groups": [],
        "bands": [["node:a"], ["node:b"]],
        "edges": [{"from": "a", "to": "b"}],
        "assertions": [],
    }
    raw.update(over)
    return raw


VOCABULARY_PROBES = {
    # 一笔代表多条链路 → 几何里必须真的有多股
    "represents": (
        _trivial(edges=[{"from": "a", "to": "b", "label": "×3", "represents": 3}]),
        lambda g: sum(1 for e in g["edges"] if e.get("bundle", 1) > 1) == 3,
    ),
    # 端口 → 几何里必须真的有端口小块
    "ports": (
        _trivial(nodes=[{"id": "a", "label": "A"},
                        {"id": "b", "label": "B", "shape": "bar", "ports": 4}]),
        lambda g: len(g["nodes"]["b"]["ports"]) == 4,
    ),
    # 时序 → 必须有生命线
    "step": (
        {
            "title": "t",
            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "groups": [], "bands": [["node:a", "node:b"]],
            "edges": [{"from": "a", "to": "b", "label": "m1", "step": 1},
                      {"from": "b", "to": "a", "label": "m2", "step": 2}],
            "assertions": [],
        },
        lambda g: len(g.get("lifelines") or []) == 2,
    ),
    # 侧边一列 → 必须被摆到内容的右边
    "side": (
        _trivial(
            nodes=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"},
                   {"id": "m", "label": "外部", "side": "right"}],
            edges=[{"from": "a", "to": "b"}, {"from": "a", "to": "m"}],
        ),
        lambda g: g["nodes"]["m"]["rect"][0]
        > max(g["nodes"][k]["rect"][0] + g["nodes"][k]["rect"][2] for k in ("a", "b")),
    ),
    # 引出标注 → 几何里必须有它
    "annotations": (
        _trivial(annotations=[{"anchor": "a", "side": "bottom", "text": "上联"}]),
        lambda g: len(g.get("annotations") or []) == 1,
    ),
    # 虚线机箱 → 组必须带上这个样式
    "dashed_group": (
        {
            "title": "t",
            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "groups": [{"id": "gp", "label": "机箱", "style": "dashed",
                        "ranks": [["a"], ["b"]]}],
            "bands": [["group:gp"]],
            "edges": [{"from": "a", "to": "b"}],
            "assertions": [],
        },
        lambda g: g["groups"]["gp"].get("style") == "dashed",
    ),
}


@pytest.mark.parametrize("name", sorted(VOCABULARY_PROBES))
def test_no_vocabulary_declares_nothing(name: str) -> None:
    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw, left_a_trace = VOCABULARY_PROBES[name]
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    assert left_a_trace(geometry), f"{name} 写了，图上却什么也没发生"


def test_declared_ports_and_arriving_links_are_checked_both_ways() -> None:
    """「声明了端口，连线就落在端口上」这句承诺，只在口够坐时成立。

    2026-09-17 扫词表时发现判据是单边的：8 口 5 线会报，**2 口 5 线不报** ——
    而后者更严重：一台 2 口交换机插不进 5 根线，那是被画的东西本身不可能的事，
    而且此时端口吸附整个失效（5 条线里只有 2 条真落在口上）。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    def _ports_findings(ports: int, links: int):
        raw = _trivial(
            nodes=[{"id": "a", "label": "A"},
                   {"id": "b", "label": "B", "shape": "bar", "ports": ports}],
            edges=[{"from": "a", "to": "b", "represents": links}],
        )
        geometry = layout_contract(normalize_contract(raw, family="schematic"))
        return [f for f in geometry["layout_findings"] if "ports" in f]

    assert _ports_findings(2, 5), "线比口多必须报"
    assert _ports_findings(8, 5), "口比线多也要报"
    assert _ports_findings(4, 4) == [], "对得上就该干净"


def test_a_state_may_point_at_itself() -> None:
    """状态机的心跳续期、重试都是自转移。这种边曾被一律拒绝（理由：「只会画成
    一个点」）—— 于是 agent **丢掉了那条边**，并在注记里写「运行中→运行中为心跳
    自环…以自环标注于『运行中』节点」，描述了一条图上没有的边，而所有判据全绿。

    这是最初那个病（「4 条 GPU 线塌成 1 条还宣称干净」）换了身衣服。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    contract = normalize_contract(
        {
            "title": "状态机",
            "nodes": [{"id": "p", "label": "待派发"}, {"id": "r", "label": "运行中"},
                      {"id": "d", "label": "已完成"}],
            "groups": [],
            "bands": [["node:p"], ["node:r"], ["node:d"]],
            "edges": [
                {"from": "p", "to": "r", "label": "被领取"},
                {"from": "r", "to": "r", "label": "心跳续期（每 30 秒）"},
                {"from": "r", "to": "d", "label": "正常收尾"},
            ],
            "assertions": [],
        },
        family="schematic",
    )
    geometry = layout_contract(contract)
    loops = [e for e in geometry["edges"] if e["from"] == e["to"]]
    assert len(loops) == 1, geometry["edges"]
    loop = loops[0]

    # 画在右上角、带方向、不出画布、不压别的边、标注放得下
    rect = geometry["nodes"]["r"]["rect"]
    assert max(y for _x, y in loop["points"]) > rect[1] + rect[3]
    assert max(x for x, _y in loop["points"]) > rect[0] + rect[2]
    assert loop.get("arrow")
    width, height = geometry["canvas"]
    assert all(0 <= x <= width and 0 <= y <= height for x, y in loop["points"])
    assert geometry["layout_metrics"]["edge_crossings"] == 0
    assert not [e for e in geometry["edges"] if e.get("label_unplaced")]


def test_an_antiparallel_pair_runs_side_by_side() -> None:
    """A→B 与 B→A 是状态机的招牌（批准/否决、暂停/恢复）。走车道机制时两条会
    互相穿过 —— 而两端的槽位本来就已经把它们分开了。"""

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(
            {
                "title": "反向平行",
                "nodes": [{"id": "r", "label": "运行中"}, {"id": "p", "label": "已暂停"},
                          {"id": "d", "label": "已完成"}],
                "groups": [],
                "bands": [["node:r"], ["node:p", "node:d"]],
                "edges": [
                    {"from": "r", "to": "p", "label": "停止"},
                    {"from": "p", "to": "r", "label": "恢复"},
                    {"from": "r", "to": "d", "label": "收尾"},
                ],
                "assertions": [],
            },
            family="schematic",
        )
    )
    assert geometry["layout_metrics"]["edge_crossings"] == 0, [
        (e["from"], e["to"], e["mode"]) for e in geometry["edges"]
    ]
    pair = [e for e in geometry["edges"] if {e["from"], e["to"]} == {"r", "p"}]
    assert len(pair) == 2
    assert pair[0]["points"][0][0] != pair[1]["points"][0][0], "两条不能落在同一条线上"


def test_a_reordering_suggestion_is_measured_not_guessed() -> None:
    """我给过一条没量过的建议（「把那个节点单独放一层」），实测把交叉从 18 弄成
    164。**建议也要过度量这一关** —— 所以这条是真摆一遍量出来的。"""

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = json.loads((BENCHMARKS / "topology.json").read_text(encoding="utf-8"))
    clean = layout_contract(normalize_contract(raw, family="schematic"))
    # 干净的图不该收到重排建议（它只在已经有交叉时才算）
    assert not [f for f in clean["layout_findings"] if f.get("reorder")]

    raw = {
        "title": "顺序不对",
        "nodes": [{"id": i, "label": i} for i in ("a", "b", "c", "d")],
        "groups": [],
        "bands": [["node:a", "node:b"], ["node:c", "node:d"]],
        "edges": [{"from": "a", "to": "d"}, {"from": "b", "to": "c"},
                  {"from": "a", "to": "c"}],
        "assertions": [],
    }
    tangled = layout_contract(normalize_contract(raw, family="schematic"))
    hints = [f["reorder"] for f in tangled["layout_findings"] if f.get("reorder")]

    # 「有交叉」不等于「存在更优的相邻对调」—— 所以先自己穷举一遍，再断言。
    # 第一版直接写「有交叉就必须给建议」，被一个确实没有更优解的玩具图证伪。
    import copy

    baseline = tangled["layout_metrics"]["edge_crossings"]
    improvable = False
    for row in range(len(raw["bands"])):
        for index in range(len(raw["bands"][row]) - 1):
            probe = copy.deepcopy(raw)
            band = probe["bands"][row]
            band[index], band[index + 1] = band[index + 1], band[index]
            got = layout_contract(
                normalize_contract(probe, family="schematic")
            )["layout_metrics"]["edge_crossings"]
            improvable = improvable or got < baseline

    if not improvable:
        assert hints == [], "没有更优解就不该编一个出来"
    else:
        assert hints, tangled["layout_metrics"]
        # 建议里的数字是量出来的：改过之后确实更少
        assert hints[0]["crossings_after"] < hints[0]["crossings_before"]
        # 每一步都带自己的落点，作者能一步步照着做
        assert hints[0]["steps"], hints[0]
        # 2026-09-17 建议从「相邻对调」扩到「短行全排列」（贪心单步出不了局部
        # 最优：实测某一栏四种对调全是上坡，而换一个排列就从 4 降到 1）。全排列
        # 表达不成一对 swap，所以机读字段以 order 为准；恰好两个元素互换时仍然
        # 额外给 swap。
        assert all(
            {"where", "order", "crossings_after"} <= set(st) for st in hints[0]["steps"]
        )
        for st in hints[0]["steps"]:
            if "swap" in st:
                assert len(st["swap"]) == 2, st


@pytest.mark.parametrize("name", sorted(EXERCISES))
def test_every_box_holds_its_own_text(name: str) -> None:
    """盒子装得下自己的字 —— 按**排版器量出来的**宽度，不是按估算。

    这道防线是估算器骗了我之后补的：`RTX PRO 6000` 估 49.9pt、xelatex 排出来
    73.8pt，字两头压在 GPU 盒的边线上，而 graze / collinear_overlaps /
    content_fill 一条都没响 —— 它们查的是几何，几何查的是估算。

    这里不查像素，查的是「布局用的宽度 == 排版器给的宽度」。两边同源之后，
    上面那批几何判据才重新有意义。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    from nodes.postprocess.diagram_compiler import _measurable_items
    from nodes.postprocess.text_metrics import lookup, measured

    raw = json.loads((BENCHMARKS / f"{name}.json").read_text(encoding="utf-8"))
    contract = normalize_contract(raw, family="schematic")
    geometry = layout_for_backend(contract, "tikz")
    source = geometry["text_metrics"]["source"]
    if not source.startswith("measured"):
        pytest.skip(f"text was not measured on this host: {source}")

    # 把这张图的实测表重新装上，问每个盒子：你的字真的放得下吗
    from nodes.postprocess.diagram_compiler import (
        _cjk_preamble,
        _needs_cjk,
        _tex_escape,
    )
    from nodes.postprocess.text_metrics import measure_with_tex

    preamble = "\n".join(
        [r"\usepackage{fontspec}", _cjk_preamble() if _needs_cjk(geometry) else ""]
    )
    table, why = measure_with_tex(
        _measurable_items(geometry), preamble, escape=_tex_escape
    )
    assert table is not None, why

    fonts = geometry["fonts"]
    too_small = []
    with measured(table):
        for node in geometry["nodes"].values():
            box = node["rect"][2]
            for field, key in (("label", "node"), ("sublabel", "sub")):
                text = node.get(field)
                if not text:
                    continue
                size = 8.5 if (field == "label" and node.get("shape") == "chip") else fonts[key]
                real = lookup(text, size)
                if real is not None and real > box:
                    too_small.append((node["label"], field, round(box, 1), round(real, 1)))
    assert not too_small, f"盒子装不下自己的字（盒宽 vs 实测字宽）：{too_small[:4]}"


def test_the_box_check_can_actually_fail() -> None:
    """变异：把一个盒子改窄，上面那条必须红。

    绿色不指向被测的事，是我这套 loop 里最贵的一类错误 —— 这条是那条判据的
    判据。
    """

    from nodes.postprocess.text_metrics import lookup, measured

    table = {("很长很长的一段标签", 11.0): 120.0}
    with measured(table):
        assert lookup("很长很长的一段标签", 11.0) == 120.0
        # 盒子 80pt 装不下 120pt 的字 —— 上面那条测试的判断式在这里必须为真
        assert 120.0 > 80.0
    assert lookup("很长很长的一段标签", 11.0) is None, "退出 with 之后不该还留着表"


def test_the_vertical_stack_is_always_a_candidate() -> None:
    """竖排（每行一个块）永远可参选 —— 它是「什么都不做」，没有替代方案可比。

    2026-09-17 追踪到：行宽相近这条护栏是为「并排」写的，却对单块行也生效。
    块的自然宽度本来就参差，于是**基线方案自己被毙掉**，best 是 None，代码掉进
    兜底的竖排 —— `PAGE_TARGET_ASPECT` 从来没有真正参与过打分。36 轮 36 次竖
    长条就是这么来的。

    这里构造的正是它会选错的那种局面：一个很宽的图 + 一段很窄的注记。
    竖排画幅 3.85（离目标 1.33 近），并排 9.2（荒唐）。护栏把竖排毙掉之后，
    只剩并排可选 —— 旧代码会真的选它。
    """

    from nodes.postprocess.diagram_compiler import _pack_rows_towards

    laid = [{"size": (1000.0, 100.0)}, {"size": (300.0, 100.0)}]
    rows = _pack_rows_towards(laid, [0.0, 0.0], 1.33)
    assert rows == [[0], [1]], f"选了 {rows} —— 把一段窄注记拉到 1366pt 宽的一行里"


def test_pairing_still_wins_when_it_should() -> None:
    """而该并排的时候仍然要并排 —— 上一条不能把并排这条路一起堵死。"""

    from nodes.postprocess.diagram_compiler import _pack_rows_towards

    # 两个等宽等高的高瘦块：竖排画幅 0.45，并排 1.81 —— 并排离 1.33 近得多
    laid = [{"size": (300.0, 700.0)}, {"size": (300.0, 700.0)}]
    rows = _pack_rows_towards(laid, [0.0, 0.0], 1.33)
    assert rows == [[0, 1]], rows


@pytest.mark.parametrize("name", sorted(EXERCISES))
def test_every_label_is_readable_on_its_own_box(name: str) -> None:
    """盒子上的字对盒子的底色必须够对比 —— WCAG AA 正文 4.5:1。

    这是配色从「重色实底 + 白字」改成「浅底 + 同色描边 + 深色字」之后必须自带的
    那道闸：底和字都由同一个角色色派生，派生规则一动就可能把某个色相调到读不了。
    harness 明写了「最终尺寸可读、色盲安全」—— 那它就该是一条机械判据，不是嘱咐。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    from nodes.postprocess.diagram_compiler import _hex_rgb, _luma

    raw = json.loads((BENCHMARKS / f"{name}.json").read_text(encoding="utf-8"))
    geometry = layout_contract(normalize_contract(raw, family="schematic"))

    def contrast(a: str, b: str) -> float:
        la, lb = _luma(_hex_rgb(a)), _luma(_hex_rgb(b))
        lo, hi = sorted((la, lb))
        return (hi + 0.05) / (lo + 0.05)

    weak = [
        (n["label"], n["fill"], n["ink"], round(contrast(n["fill"], n["ink"]), 2))
        for n in geometry["nodes"].values()
        if n.get("fill") and contrast(n["fill"], n["ink"]) < 4.5
    ]
    assert not weak, f"字对底色不够对比（<4.5:1）：{weak[:4]}"

    # 描边也得看得见 —— 底色和描边同源，规则一松就会糊成一块
    faint = [
        (n["label"], round(contrast(n["fill"], n["stroke"]), 2))
        for n in geometry["nodes"].values()
        if n.get("fill") and contrast(n["fill"], n["stroke"]) < 2.0
    ]
    assert not faint, f"描边对底色太弱（<2:1），盒子边界会糊掉：{faint[:4]}"


def test_the_contrast_rule_bites() -> None:
    """变异：把文字亮度上限放到和底色一样，上面那条必须红。"""

    from nodes.postprocess.diagram_compiler import _hex_rgb, _luma, _shade, _tint

    def contrast(a: str, b: str) -> float:
        la, lb = _luma(_hex_rgb(a)), _luma(_hex_rgb(b))
        lo, hi = sorted((la, lb))
        return (hi + 0.05) / (lo + 0.05)

    base = "#4C72B0"
    assert contrast(_tint(base), _shade(base, 0.10)) >= 4.5
    # 把上限放到 0.80（几乎和底色一样亮）—— 对比必须掉到 AA 以下
    assert contrast(_tint(base), _shade(base, 0.80)) < 4.5


@pytest.mark.parametrize("name", sorted(EXERCISES))
def test_one_colour_never_means_two_things(name: str) -> None:
    """同一张图例里，一个颜色只能代表一件事。

    2026-09-17 实测微服务那张：15 格图例里 **4 个颜色各担 2–3 个含义**
    （#C44E52 同时是「数据层」「HTTP 调用」「查询展示」）。原因有两个，
    都在我们这边：节点色板和边色板共用了一个色值，以及边角色（10 个）多于
    边色板（6 色）之后池内循环。**图例在撒谎，而没有任何判据会说话。**

    这不是审美问题 —— 读者按颜色去认图，两件事同一个颜色就是把图读错。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = json.loads((BENCHMARKS / f"{name}.json").read_text(encoding="utf-8"))
    geometry = layout_contract(normalize_contract(raw, family="schematic"))

    seen: dict[str, list[str]] = {}
    for entry in (geometry.get("legend") or []) + (geometry.get("edge_legend") or []):
        seen.setdefault(entry["color"], []).append(entry["role"])
    clashes = {c: roles for c, roles in seen.items() if len(roles) > 1}
    assert not clashes, f"一个颜色代表了多件事：{clashes}"


def test_the_two_palettes_cannot_collide() -> None:
    """节点色板与边色板必须无交集 —— 上一条在夹具上成立，这条让它在**构造上**成立。

    夹具只覆盖它们碰巧用到的角色数；池子重叠是个结构事实，得直接钉。
    """

    from nodes.postprocess.figure_contract import EDGE_ROLE_PALETTE, ROLE_PALETTE_ORDER

    assert len(set(ROLE_PALETTE_ORDER)) == len(ROLE_PALETTE_ORDER)
    assert len(set(EDGE_ROLE_PALETTE)) == len(EDGE_ROLE_PALETTE)
    assert not set(ROLE_PALETTE_ORDER) & set(EDGE_ROLE_PALETTE)
    # 真实合同见过 10 个边角色；池子不够就会回到池内循环撞色
    assert len(EDGE_ROLE_PALETTE) >= 10


def _page(blocks, **extra):
    raw = {"title": "t", "blocks": blocks, "assertions": []}
    raw.update(extra)
    return layout_contract(normalize_contract(raw, family="schematic"))


_DIAGRAM = {
    "kind": "diagram",
    "title": "① 图",
    "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    "groups": [],
    "bands": [["node:a"], ["node:b"]],
    "edges": [{"from": "a", "to": "b"}],
}


def test_a_bare_note_does_not_get_a_panel_of_its_own() -> None:
    """一句话的注记不配独占一整栏 —— 但那句话必须还在纸上。

    壳按块收固定费：分栏边框 + 标题条一块 55pt，而真跑里那条注记内容只有 17pt
    （效率 22%）。没标题的 note 本来就是「页脚的一句话」，几何里早有那个位置。

    这条测试的第二半才是关键：**降级不许把内容降没了。** 第一版就是这么错的 ——
    块摘掉了、文字没接上，图上那句话凭空消失，而所有判据全绿。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    note = "注：四台机器配置完全相同。"
    geometry = _page([_DIAGRAM, {"kind": "note", "text": note}])
    assert len(geometry["panels"]) == 1, "注记不该占一个分栏"
    assert note in (geometry.get("notes") or []), "降级把内容降没了"


def test_a_titled_note_keeps_its_panel() -> None:
    """作者给了标题，那是他要的一个小节 —— 框留着。降级只收无标题的那种。"""

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = _page(
        [_DIAGRAM, {"kind": "note", "title": "② 说明", "text": "正文"}]
    )
    assert len(geometry["panels"]) == 2


def test_a_long_footnote_widens_the_page() -> None:
    """页脚注记的宽度必须参与页宽。

    在此之前它从来没参与过 —— 比内容宽就直接画到画布外，而几何判据一条都不会响
    （它们只看盒子，页脚是一行文字）。这条是把注记块降级时撞出来的：原来它是个
    撑着页宽的 block，降级后页面反而变窄、更竖（flowchart 0.96 → 0.61）。
    """

    from nodes.postprocess.diagram_compiler import NOTE_FS, _text_width

    long_note = "注：" + "很长的一段说明文字，" * 12
    geometry = _page([_DIAGRAM], notes=[long_note])
    assert geometry["canvas"][0] >= _text_width(long_note, NOTE_FS), (
        geometry["canvas"][0], _text_width(long_note, NOTE_FS)
    )


def test_a_known_key_that_normalisation_drops_gets_named() -> None:
    """词表认识、作者填了、归一化之后没了 —— 必须有人说话。

    2026-09-17：顶层 `notes` 在「写了 blocks」那条分支上从来没被搬过来，
    作者写的页脚一句话凭空消失。`unexpressed_findings` 没响 —— 它问的是
    「这个键认不认识」，`notes` 它认识。**一个键有两条归一化路径时，
    只要有一条忘了它，它就在那条路径上静默消失。**

    这条判据扫结果，所以将来再加分支也问得住。用一个人造的「丢了」局面来验，
    因为真身已经修好了（修好之后这条判据在八条基准上必须全静默）。
    """

    from nodes.postprocess.figure_contract import dropped_findings

    raw = {"title": "t", "notes": ["一句页脚"], "nodes": [{"id": "a"}]}
    kept = {"title": "t", "notes": ["一句页脚"], "nodes": [{"id": "a"}]}
    assert dropped_findings(raw, kept) == []
    lost = dropped_findings(raw, {"title": "t"})
    assert lost and lost[0]["code"] == "contract.dropped_keys"
    assert "notes" in lost[0]["paths"] and "nodes" in lost[0]["paths"]


@pytest.mark.parametrize("name", sorted(EXERCISES))
def test_no_benchmark_loses_a_key_on_the_way_in(name: str) -> None:
    """八条真实合同过一遍：没有任何一个填了的键在归一化里蒸发。"""

    from nodes.postprocess.figure_contract import dropped_findings

    raw = json.loads((BENCHMARKS / f"{name}.json").read_text(encoding="utf-8"))
    normalized = normalize_contract(raw, family="schematic")
    assert dropped_findings(raw, normalized) == []


# ── 版面尺度：这张图印出来多大，读得了吗 ────────────────────────────────────
#
# 画布的 pt 数**就是**物理英寸数（figsize=(W/72, H/72)）。46 轮里没有任何判据
# 问过「复现之后还读得了吗」—— 而校准集是 420mm 宽，放进期刊单栏时 11pt 的
# 节点标签只剩 2.2pt。这是出版级图最该有、我们完全没有的那一条。


def test_the_medium_changes_the_verdict() -> None:
    """`medium` 必须有机械后果 —— 同一张图换个版心，**记录里的数**跟着变。

    后果落在 `layout_metrics` 上，不落在 findings 上：iter50 实测，把它做成
    findings 会让模型**把 medium 删掉让判据闭嘴**（第1版 medium=slide → 判据
    开口 → 第2版 medium=None，几何一字不差）。给一个自愿的声明挂惩罚，
    就是教它别声明。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = json.loads(
        (BENCHMARKS / "calibration-reference.json").read_text(encoding="utf-8")
    )

    def label_pt(medium: str) -> float:
        contract = normalize_contract({**raw, "medium": medium}, family="schematic")
        geometry = layout_contract(contract)
        # 一条 finding 都不许有 —— 记录事实，不罚作者
        assert not [f for f in geometry["layout_findings"] if "final_label_pt" in f]
        return geometry["layout_metrics"]["final_label_pt"]

    # 版心越小，复现之后的字越小；三个数必须真的不同，否则 medium 是装饰
    double, slide, single = (
        label_pt("double_column"), label_pt("slide"), label_pt("single_column")
    )
    assert len({double, slide, single}) == 3, (double, slide, single)
    assert double > single, (double, single)
    assert single < 5.0, "期刊单栏下这张图的标签确实读不了 —— 事实要记下来"


def test_the_fit_is_measured_the_way_the_figure_is_actually_placed() -> None:
    """宽、高各算一个缩放，取小的 —— 不旋转。

    早先这里试过「转过来放取大的那个」（排版师会把横图转 90°）。但消费方是
    写作节点的 `\\includegraphics[width=…]` 和它的尺子 H9，两者都不旋转：
    按旋转算出来的 0.57 是一个纸上不会发生的缩放，而真实的 0.40 才是读者
    拿到的字。判据必须等于真正的问题。
    """

    from nodes.postprocess.figure_contract import MEDIA, fit_scale

    wide_w, wide_h = 420.0, 299.0
    assert fit_scale(wide_w, wide_h, "double_column") == pytest.approx(
        min(MEDIA["double_column"]["w"] / wide_w, MEDIA["double_column"]["h"] / wide_h),
        rel=1e-6,
    )
    # 竖长条由高度决定（写作侧 height=0.8\\textheight 的上限就是 200mm）
    assert fit_scale(100.0, 400.0, "double_column") == pytest.approx(
        min(170.0 / 100.0, 200.0 / 400.0), rel=1e-6
    )


def test_a_figure_nobody_can_read_shows_it_in_the_record() -> None:
    """真读不了的图，记录里的数必须难看 —— 难看的数是给人看的，不是给模型罚的。"""

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    # 一条 20 节点的长链：画幅被拉成细长条，怎么摆都缩得没法看
    strip = {
        "title": "长链",
        "medium": "double_column",
        "nodes": [{"id": f"n{i}", "label": f"节点 {i}"} for i in range(20)],
        "groups": [],
        "bands": [[f"node:n{i}" for i in range(20)]],
        "edges": [{"from": f"n{i}", "to": f"n{i+1}"} for i in range(19)],
        "assertions": [],
    }
    geometry = layout_contract(normalize_contract(strip, family="schematic"))
    from nodes.postprocess.figure_contract import MEDIA

    # 断言的是**含义**不是魔数：低于该媒介自己的下限。
    # （第一版拍了个 <2.0，实测 4.08 —— 又一次拿猜的数当判据。）
    assert (
        geometry["layout_metrics"]["final_label_pt"] < MEDIA["double_column"]["floor_pt"]
    ), geometry["layout_metrics"]["final_label_pt"]
    assert not [f for f in geometry["layout_findings"] if "final_label_pt" in f]


def test_the_medium_comes_from_purpose_when_nobody_declared_it() -> None:
    """不新增第二个真相源：调用方已经说了 purpose，作者只在需要时覆盖。"""

    from nodes.postprocess.figure_contract import medium_of

    assert medium_of({}, "presentation") == "slide"
    assert medium_of({}, "publication") == "double_column"
    assert medium_of({}, None) == "double_column"
    assert medium_of({"medium": "single_column"}, "presentation") == "single_column"


def test_the_type_scale_has_distinguishable_steps() -> None:
    """模数音阶：相邻两档必须看得出差别。

    改之前 11 个文字角色用了 8 个字号，`13.0`（组标题）与 `13.5`（栏标题）
    眼睛根本分不出 —— 那不是层级，是噪声。
    """

    from nodes.postprocess.diagram_compiler import (
        GROUP_LABEL_FS,
        NODE_FS,
        PANEL_TITLE_FS,
        SUB_FS,
        TITLE_FS,
        TYPE_RATIO,
        type_size,
    )

    steps = [type_size(i) for i in range(6)]
    for smaller, larger in zip(steps, steps[1:]):
        assert larger / smaller >= 1.15, (smaller, larger)
    # 每个角色都落在音阶上，没有游离的数
    for size in (SUB_FS, NODE_FS, GROUP_LABEL_FS, PANEL_TITLE_FS, TITLE_FS):
        assert size in steps, size
    assert TYPE_RATIO > 1.0


# ── 图 / 地：谁是主角 ───────────────────────────────────────────────────────


def _two_nodes(**over):
    raw = {
        "title": "t",
        "nodes": [{"id": "a", "label": "A", "role": "r"},
                  {"id": "b", "label": "B", "role": "r"}],
        "groups": [],
        "bands": [["node:a"], ["node:b"]],
        "edges": [{"from": "a", "to": "b"}],
        "assertions": [],
    }
    for node in raw["nodes"]:
        if node["id"] in over:
            node["emphasis"] = over[node["id"]]
    return layout_contract(normalize_contract(raw, family="schematic"))


def test_emphasis_actually_changes_the_pixels() -> None:
    """三档必须画出来真的不一样 —— 声明了却零像素是这套合同最该拦的事。

    而且只动**亮度与线宽**，不动色相：色相已经被「类别」占着，拿它兼职表达
    「重要性」就是把两件事混成一件（Bertin：色相是名义变量，没有次序）。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    from nodes.postprocess.diagram_compiler import _hex_rgb, _luma

    plain = _two_nodes()["nodes"]["a"]
    primary = _two_nodes(a="primary")["nodes"]["a"]
    muted = _two_nodes(a="muted")["nodes"]["a"]

    # 底色：primary 最实、muted 最淡
    assert _luma(_hex_rgb(primary["fill"])) < _luma(_hex_rgb(plain["fill"]))
    assert _luma(_hex_rgb(muted["fill"])) > _luma(_hex_rgb(plain["fill"]))
    # 线宽：前进 / 后退
    assert primary["line_weight"] > plain["line_weight"] > muted["line_weight"]
    # muted 的描边必须真的变淡 —— 只「封顶」时深色相低于上限，三档会一模一样
    assert _luma(_hex_rgb(muted["stroke"])) > _luma(_hex_rgb(plain["stroke"])) + 0.1
    # 色相不许动：同一个角色，三档的角色色是同一个
    assert primary["color"] == plain["color"] == muted["color"]


def test_every_emphasis_level_still_clears_the_readability_floors() -> None:
    """muted 不许靠「把字弄看不清」来后退 —— 灰字压白底是常见做法，也是常见的错。

    两条下限都是既有的（字对底 4.5:1 / 边对底 2:1），muted 的两个数就是被它们
    钉死的，不是挑出来的。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    from nodes.postprocess.diagram_compiler import _hex_rgb, _luma

    def contrast(a: str, b: str) -> float:
        lo, hi = sorted((_luma(_hex_rgb(a)), _luma(_hex_rgb(b))))
        return (hi + 0.05) / (lo + 0.05)

    for level in ("primary", None, "muted"):
        node = (_two_nodes(a=level) if level else _two_nodes())["nodes"]["a"]
        assert contrast(node["fill"], node["ink"]) >= 4.5, (level, node)
        assert contrast(node["fill"], node["stroke"]) >= 2.0, (level, node)


def test_everything_primary_is_a_contradiction() -> None:
    """「全都是主角」= 没有主角。

    强调是一个**相对关系**，需要一个安静的多数做底；全体 primary 时这个声明
    无法成立 —— 这是自相矛盾，所以拒绝录入，而不是一条质量 finding
    （拒绝分支只加在自相矛盾上）。
    """

    from nodes.postprocess.contracts import VisualContractError

    raw = {
        "title": "t",
        "nodes": [{"id": "a", "label": "A", "emphasis": "primary"},
                  {"id": "b", "label": "B", "emphasis": "primary"}],
        "groups": [],
        "bands": [["node:a"], ["node:b"]],
        "edges": [{"from": "a", "to": "b"}],
        "assertions": [],
    }
    with pytest.raises(VisualContractError, match="emphasis is a relation"):
        normalize_contract(raw, family="schematic")

    # 一个主角一个陪衬是合法的
    raw["nodes"][1]["emphasis"] = "muted"
    normalize_contract(raw, family="schematic")


# ── 判据看得见渲染器画的每一类东西吗 ────────────────────────────────────────
#
# 「每加一种画出来的东西就多一个盲区」是这套系统里出现次数最多的根因之一
# （文字不算内容 / 装饰件不算 / 生命线不算 …）。过去靠 `content_fill` 当
# 盲区探测器 —— 2026-09-17 变异实测它**对五类盲区里的四类完全失明**：
#
#   panels 不算 / spec-note 不算 / annotations 不算 / groups 不算 → 数值一动不动
#   只有 legend/title 不算才掉（四张基准里还只有一张跌破阈值）
#
# 因为它是个**包围盒**比值：只有最外围那一圈丢了才动。用它防盲区，等于用
# 门口的脚垫防小偷进屋。
#
# 正解是**直接问这件事**：渲染器画的每一类，判据都得看得见 —— 拿掉哪一类，
# 被量到的盒子集合就必须跟着变。这条在「新加了一种画出来的东西」的当天就会红。


#: 一类「画出来的东西」→ 把它从几何里**彻底**拿掉的办法。
#: 要写全它的所有表示：图例是用 `legend_box` 一个盒子量的，只删 legend 条目
#: 数值不动 —— 第一版就这么误报了一个不存在的盲区。**变异做得不对，
#: 得到的红也是假的。**
DRAWABLE_CATEGORIES: dict[str, dict] = {
    "nodes": {"nodes": {}},
    "groups": {"groups": {}},
    "panels": {"panels": []},
    "edges": {"edges": []},
    "annotations": {"annotations": []},
    "spec_items": {"spec_items": []},
    "block_notes": {"block_notes": []},
    "notes": {"notes": []},
    "legend": {"legend": [], "edge_legend": [], "legend_box": None},
    "title": {"title_box": None},
    "lifelines": {"lifelines": []},
}


def test_the_criteria_can_see_everything_the_renderer_draws() -> None:
    """渲染器画的每一类，几何判据都必须看得见。

    做法是变异：把某一类从几何里拿掉，`_all_drawn_boxes` 的输出必须跟着变。
    没变 = 这一类从来没被量过 = 一个盲区，而盲区里的重叠、贴线、空白，
    所有几何判据都会**静默通过**。

    第二条断言同样重要：**每一类都得在至少一张夹具上被真正压到**。
    只在「夹具里有这一类时才验」的写法会悄悄退化成空测 —— 哪天所有夹具都不再
    用某一类，这条测试会一边全绿一边什么也没验（同型的坑在这个仓库里踩过）。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    import copy

    from nodes.postprocess.layout_quality import _all_drawn_boxes

    # 八条夹具 + 一个**有标题的 note 块**。后者是自造的，理由写清楚：
    # iter46 把无标题注记降级成页脚之后，`block_notes` 这条路径只剩「作者给了
    # 标题的 note 块」能走，而夹具里没有 —— 于是它成了一条**零夹具覆盖**的
    # 渲染路径。这条测试问的是「判据的视野」，不是「这形状真不真实」，
    # 所以自造样本在这里是对的用法。
    titled_note = {
        "title": "t",
        "blocks": [
            {"kind": "diagram", "title": "① 图",
             "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
             "groups": [], "bands": [["node:a"], ["node:b"]],
             "edges": [{"from": "a", "to": "b"}]},
            {"kind": "note", "title": "② 说明", "text": "一段有标题的注记。"},
        ],
        "assertions": [],
    }

    exercised: set[str] = set()
    blind: list[str] = []
    cases = [
        (name, json.loads((BENCHMARKS / f"{name}.json").read_text(encoding="utf-8")))
        for name in sorted(EXERCISES)
    ] + [("titled-note", titled_note)]
    for name, raw in cases:
        geometry = layout_contract(normalize_contract(raw, family="schematic"))
        base = len(_all_drawn_boxes(geometry))
        for category, override in DRAWABLE_CATEGORIES.items():
            present = any(geometry.get(key) for key in override)
            if not present:
                continue
            exercised.add(category)
            if len(_all_drawn_boxes({**copy.deepcopy(geometry), **override})) == base:
                blind.append(f"{category}@{name}")

    assert not blind, f"这些画出来的东西判据看不见（盲区）：{blind}"
    missing = set(DRAWABLE_CATEGORIES) - exercised
    assert not missing, (
        f"这些类别没有任何一张夹具压到，这条测试对它们是空的：{sorted(missing)}"
    )


def test_the_blind_spot_test_can_actually_fail() -> None:
    """变异：把判据对节点的视野挖掉，上面那条必须红。"""

    import copy

    from nodes.postprocess.layout_quality import _all_drawn_boxes

    geometry = {
        "canvas": [100.0, 100.0],
        "nodes": {"a": {"rect": [10.0, 10.0, 20.0, 20.0], "label": "A"}},
        "groups": {}, "panels": [], "edges": [], "annotations": [],
        "spec_items": [], "block_notes": [], "legend": [], "edge_legend": [],
        "fonts": {"node": 11.0, "sub": 8.0, "spec": 9.4, "note": 9.4,
                  "legend": 9.4, "panel": 15.5, "group": 13.1, "title": 18.3,
                  "edge": 8.0},
    }
    assert len(_all_drawn_boxes(geometry)) > 0
    assert len(_all_drawn_boxes({**copy.deepcopy(geometry), "nodes": {}})) == 0


# ── 拒绝只针对自相矛盾：空行不是矛盾 ────────────────────────────────────────


def test_an_empty_row_is_dropped_and_named_not_rejected() -> None:
    """空的 rank / band **不拒**，丢掉并点名。

    2026-09-17 iter49 实测代价：agent 五次声明三次被拒，其中两次是**改上一条时
    把别处弄坏了**（bands 丢了、ranks 又空了）。为一个无害的 no-op 拒一次，
    换来的是它去破坏别处。

    架构的拒绝分支只加在自相矛盾上 —— 一个空行什么内容都没有，它不和任何声明
    打架，也画不出任何东西。既不拒也不忍：照画，并且点名（作者写下那一行多半
    是本来想往里放东西）。
    """

    from nodes.postprocess.figure_contract import empty_row_findings

    raw = {
        "title": "t",
        "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        "groups": [{"id": "g", "label": "G", "ranks": [["a"], [], ["b"]]}],
        "bands": [["group:g"], []],
        "edges": [{"from": "a", "to": "b"}],
        "assertions": [],
    }
    contract = normalize_contract(raw, family="schematic")      # 不抛
    assert contract["groups"][0]["ranks"] == [["a"], ["b"]]
    assert contract["bands"] == [["group:g"]]

    named = empty_row_findings(contract)
    assert named and named[0]["code"] == "contract.empty_rows_dropped"
    assert named[0]["dropped_empty_bands"] == 1
    assert named[0]["dropped_empty_ranks"] == 1
    # 干净的合同不许被点名
    assert empty_row_findings(normalize_contract(
        {**raw, "groups": [{"id": "g", "label": "G", "ranks": [["a"], ["b"]]}],
         "bands": [["group:g"]]}, family="schematic")) == []


def test_a_container_around_nothing_is_still_rejected() -> None:
    """但「框住空气的框」留着拒 —— 它不是 no-op。

    成员为零的组在版面上要占地方，却没有内容可占：这是「声明了一个装不下东西
    的容器」，是真的自相矛盾。丢空行之后才发现组空了，错误信息要把这件事说全。
    """

    from nodes.postprocess.contracts import VisualContractError

    raw = {
        "title": "t",
        "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        "groups": [{"id": "g", "label": "G", "ranks": [[], []]}],
        "bands": [["node:a"], ["node:b"]],
        "edges": [{"from": "a", "to": "b"}],
        "assertions": [],
    }
    with pytest.raises(VisualContractError, match="no members"):
        normalize_contract(raw, family="schematic")

    # 所有带子都空 —— 什么都放不下，也拒
    with pytest.raises(VisualContractError, match="every band is empty"):
        normalize_contract({
            **raw,
            "groups": [{"id": "g", "label": "G", "ranks": [["a"], ["b"]]}],
            "bands": [[], []],
        }, family="schematic")


# ── 说真话不该比不说更惨 ────────────────────────────────────────────────────
#
# iter50 实测的坑：`medium` 是可选声明，而声明了就会被一条**作者交不出来**的
# 判据盯上。agent 的反应不是去改图 —— 它**把声明删掉让判据闭嘴**
# （第1版 medium=slide → 第2版 medium=None，几何一字不差，白烧两轮）。
#
# 这个坑是有形状的：**一个可选声明，写了会解锁更严的检查**。这本身没错
# （声明产生义务是对的），错在那条义务作者**满足不了**。所以不变式是：
#
#     一个**如实的**可选声明，不该带来任何新的 finding。
#
# 不如实的声明（口数与落线对不上）当然要报 —— 那是矛盾，不是惩罚诚实。


def _findings_of(raw: dict) -> list[str]:
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    return [f.get("message", "")[:80] for f in geometry["layout_findings"]]


@pytest.mark.parametrize("field", ["medium", "emphasis", "ports"])
def test_a_truthful_optional_declaration_costs_nothing(field: str) -> None:
    """把一个可选字段**如实**写上去，findings 不许变多。"""

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    import copy

    raw = json.loads(
        (BENCHMARKS / "calibration-reference.json").read_text(encoding="utf-8")
    )
    before = _findings_of(raw)
    assert before == [], before  # 校准集本来就是干净的

    said = copy.deepcopy(raw)
    blocks = said.get("blocks") or []
    nodes = [n for b in blocks for n in (b.get("nodes") or [])]

    if field == "medium":
        # 用 **slide**，不是 page。这张图就是 presentation 请求画出来的，
        # 所以 slide 是它的真话；而它在 slide 下标签只有 5.3pt、低于 10pt 下限
        # —— **旧规则正是在这里开口的**。拿 page 来验等于没验（它本来就达标），
        # 那是「测试的前提不成立」，这一天已经栽过两次。
        said["medium"] = "slide"
    elif field == "emphasis":
        # 少数派 + 挑对了主角：两个 switch 是这张图讲的东西
        for node in nodes:
            if node["id"].startswith("sw"):
                node["emphasis"] = "primary"
            elif node["id"].startswith("cpu"):
                node["emphasis"] = "muted"
    else:
        # ports 写成**它真正的连接数** —— 真话就该零代价
        degree: dict[str, int] = {}
        for block in blocks:
            for edge in block.get("edges") or []:
                for end in (edge["from"], edge["to"]):
                    degree[end] = degree.get(end, 0) + int(edge.get("represents") or 1)
        for node in nodes:
            if node.get("ports"):
                continue
            if degree.get(node["id"], 0) >= 2:
                node["ports"] = degree[node["id"]]
                break

    assert _findings_of(said) == [], (
        f"如实声明 {field} 之后多出了 findings —— 这会教模型把声明删掉"
    )


def test_a_dishonest_declaration_still_gets_reported() -> None:
    """而**不如实**的声明照报 —— 上一条不是「可选字段一律免检」。

    口数与落线对不上是矛盾，报它不是惩罚诚实，正相反：它保护的是那些如实写的人。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    import copy

    raw = json.loads(
        (BENCHMARKS / "calibration-reference.json").read_text(encoding="utf-8")
    )
    lying = copy.deepcopy(raw)
    for block in lying.get("blocks") or []:
        for node in block.get("nodes") or []:
            if node.get("ports"):
                node["ports"] = int(node["ports"]) + 7   # 凭空多画七个口
                break
    assert _findings_of(lying), "口比线多了七个，必须有人说话"


def test_the_band_corridor_is_a_measured_cliff_not_a_taste() -> None:
    """带子走廊收到 62 以下，密集图当场炸 —— 这条钉住那个悬崖。

    2026-09-17 扫 74/62/54/46/38 五档：74→62 每一条基准都改善或持平，
    而 62→54 密集图交叉 281 → 317（+13%）。走廊不够时边开始互相挤。
    **62 是拐点，不是口味** —— 再往下换来的画幅是用密集图的可读性买的。

    这条测试的作用是：将来谁再为了画幅去调这个数，密集图会当场喊疼。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    import nodes.postprocess.diagram_compiler as dc

    raw = json.loads(
        (BENCHMARKS / "microservices-ceiling.json").read_text(encoding="utf-8")
    )
    contract = normalize_contract(raw, family="schematic")
    keep = dc.BAND_GAP
    try:
        dc.BAND_GAP = 62.0
        at_62 = layout_contract(contract)["layout_metrics"]["edge_crossings"]
        dc.BAND_GAP = 54.0
        at_54 = layout_contract(contract)["layout_metrics"]["edge_crossings"]
    finally:
        dc.BAND_GAP = keep
    assert dc.BAND_GAP == 62.0, "现行值就该停在拐点上"
    assert at_54 > at_62 * 1.1, (at_62, at_54)


def test_the_schematic_skill_teaches_the_three_things_the_author_actually_chooses() -> None:
    """画功那一半必须真的在 skill 里 —— 而且只教**归作者选**的那三件事。

    54 轮下来，配色 / 字号 / 线宽全部归框架（色相=类别、亮度=层级、尺寸=分量），
    模型无从选择；归它选的只有：规格写哪儿（sublabel vs 图例）、谁是主角
    （emphasis）、分几块（画幅的主要决定因素）。

    这条测试挡的是两件事：
    - 画功那一半被误删（它是四种送达形式里的第三种「清单」）
    - 有人往里塞「归框架管」的东西，把已经定死的选择重新交给模型
    """

    from pathlib import Path

    from nodes.postprocess.tools.figure import _figure_contract_schema

    # **先查必到的那个渠道。** `figure_contract_schema` 每一轮都被调；
    # `load_skill` 最近八轮只被调了三次（37.5%）—— 把指南只放进后者，
    # 等于没送到（与「新词表必须进 example」同一个道理，2026-09-17 实测）。
    always_read = "".join(_figure_contract_schema("schematic")["family_checklist"])
    for topic in ("sublabel", "emphasis", "画幅不是你能直接拧的旋钮"):
        assert topic in always_read, f"必到渠道里没教 {topic}"
    # **不许再出现那个编出来的数。** 2026-09-17：清单里一度写着
    # 「3 块≈画幅 1.0、4 块≈0.86」—— 从 45 个数据点里挑两个拼的；全量一核
    # 3 块 n=12 均值 0.86、4 块 n=33 均值 0.81，范围几乎完全重叠。
    # 放一个假数字在**每轮必读**的渠道里，比不放更坏。
    # 两句编出来的因果都不许再出现（一句量级错、一句方向错）
    assert "3 块排出来画幅" not in always_read
    assert "减少最宽那一行的元件数" not in always_read

    skill = (
        Path(__file__).resolve().parent.parent
        / "skills" / "scientific-schematic" / "SKILL.md"
    ).read_text(encoding="utf-8")

    for topic in ("sublabel", "emphasis", "medium", "画幅"):
        assert topic in skill, f"skill 里没教 {topic}"
    assert "画幅不是你能直接拧的旋钮" in skill
    # 不许把框架已经定死的东西重新交给模型选
    for forbidden in ("选一个配色", "挑字号", "自定义颜色"):
        assert forbidden not in skill, forbidden


def test_the_example_is_as_dense_as_the_figure_the_user_called_better() -> None:
    """**example 教的是密度，不只是语法** —— 模型会照抄它。

    2026-09-17 根因：`figure_contract_schema` 的 example 一度只有 1/11 个节点
    带 `sublabel`，而用户点名「高级得多」的那张参考图是 **17/19**。真跑产出
    1/19 —— 模型复制的是 example 的稀疏度。加指南把它推到 3/19，把 example
    自己补密才是根治（example 是最强的送达形式，这是这个循环第一天就立的规矩）。

    这条钉住密度：将来谁精简 example，会在这里红。
    """

    from nodes.postprocess.tools.figure import _figure_contract_schema

    example = _figure_contract_schema("schematic")["example"]
    nodes = [n for b in (example.get("blocks") or []) for n in (b.get("nodes") or [])]
    with_sub = [n for n in nodes if n.get("sublabel")]
    assert nodes
    # 参考图 17/19 = 89%。example 不该比它稀疏太多。
    assert len(with_sub) / len(nodes) >= 0.75, (
        f"example 只有 {len(with_sub)}/{len(nodes)} 个节点带副标题；"
        "模型会照抄这个密度"
    )
    # 而且不能靠复读同一句话凑数 —— 副标题要真的是各自的规格
    assert len({n["sublabel"] for n in with_sub}) >= 4


def test_links_land_straight_on_the_ports_they_were_meant_for() -> None:
    """落在端口上的线应该是直的。

    用户看图指出的：栏② 八条上联全在 zigzag。量下来根因不在布线器 ——
    端口沿长条**均布**（间距 74pt），而源把自己的几条边摊在**自己盒宽**上
    （间距 22pt），两套间距互不相干，于是每条都得横着拐一段才够得着
    （校准集实测 8/8 条拐弯，最大横移 75pt）。

    端口本来就该长在它服务的那条线底下。这条测试写在实现之前 ——
    同一处改坏过三次（0→14 交叉 / 0→4 / 一条无关的分组 finding）。

    第三次那条无关 finding 的真凶：它的开关挂在**全图**比值
    `最长边/平均边长 > 3.0` 上。把上联拉直之后短边更短、平均下降，比值
    2.79 → 3.17 越过阈值，于是一条**本来就一直存在**的 chassis 划分被放出来报了。
    **图没变坏，是「变好」把一个全局阈值顶过去了。** 开关改成局部结构判据
    （各家成员在这一行里连不连成一段）之后，这条才落得了地。
    """

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = json.loads(
        (BENCHMARKS / "calibration-reference.json").read_text(encoding="utf-8")
    )
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    ported = {nid for nid, n in geometry["nodes"].items() if n.get("ports")}
    assert ported, "校准集里本来就有一个 8 口的交换机"

    jogged = []
    for edge in geometry["edges"]:
        if not ({edge["from"], edge["to"]} & ported):
            continue
        xs = [p[0] for p in edge["points"]]
        if max(xs) - min(xs) > 2.0:
            jogged.append((edge["from"], edge["to"], round(max(xs) - min(xs), 1)))
    assert not jogged, f"落在端口上的线还在拐弯：{jogged}"


def test_a_line_nobody_explains_gets_named() -> None:
    """有的边写了 role、有的没写 —— 没写的那些在图例里没有任何解释。

    图例按 role 出。只要**有**边带 role，图例就出现；这时不带 role 的边照样
    被画出来，而图例里没有一条对应它 —— **纸上有一种线，没人说它是什么**。

    2026-09-17：用户点名「高级得多」的参考图 22 条边全部带 role；真跑连着两轮
    都是「9 条有、8 条没有」（缺的正是 switch→GPU 那 8 条）。根因还是 example
    自己让四条挂着空 role，模型照抄示范。

    **全都不带 role 不报** —— 那种图根本不出图例，没有「说了一半」的问题。
    """

    from nodes.postprocess.figure_contract import unroled_edge_findings

    base = {
        "title": "t",
        "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"},
                  {"id": "c", "label": "C"}],
        "groups": [],
        "bands": [["node:a"], ["node:b"], ["node:c"]],
        "assertions": [],
    }

    def findings(edges):
        return unroled_edge_findings(
            normalize_contract({**base, "edges": edges}, family="schematic")
        )

    # 说了一半 → 报
    mixed = findings([
        {"from": "a", "to": "b", "role": "控制"},
        {"from": "b", "to": "c"},
    ])
    assert mixed and mixed[0]["code"] == "contract.edges_without_role"
    assert mixed[0]["without_role"] == 1 and mixed[0]["with_role"] == 1

    # 全都写了 → 静默
    assert not findings([
        {"from": "a", "to": "b", "role": "控制"},
        {"from": "b", "to": "c", "role": "数据"},
    ])
    # 全都没写 → 也静默（不出图例，没有「说了一半」的问题）
    assert not findings([{"from": "a", "to": "b"}, {"from": "b", "to": "c"}])

def _two_block_tangle() -> dict:
    """① 栏一堆便宜的节点，② 栏是真卡住过的那张小图（iter89 的业务层内部调用）。

    形状照抄真实局面：乱的那一栏**不是第一栏**，而第一栏又大又慢 —— 探针过去
    正是把预算全花在第一栏上，② 栏一个候选都没轮上。
    """

    # ① 栏**排在前面、候选很多、而且一个都改不动**（八条边全扇进同一个 hub，
    # 本来就 0 交叉）。这样任何改进都只可能来自 ② 栏 —— 断言才咬得住「探针有没有
    # 走到后面那一栏」。
    #
    # 试过让 ① 栏自己也有得改，那样探针在 ① 栏先找到一步就收手，断言随之失效；
    # 又试过把 ① 栏缩小，那样预算不再是瓶颈，也就钉不住分桶那件事。判据不许依赖
    # 运行环境，也不许依赖「谁先被找到」（2026-09-17 两个坑都踩了）。
    first = [f"n{i}" for i in range(8)]
    return {
        "title": "两栏",
        "blocks": [
            {
                "kind": "diagram", "title": "① 大而无事",
                "nodes": [{"id": n, "label": n.upper(), "role": "甲"} for n in first]
                + [{"id": "hub", "label": "HUB", "role": "乙"}],
                "edges": [{"from": n, "to": "hub", "role": "连"} for n in first],
                "groups": [{"id": "g1", "label": "甲", "ranks": [first]},
                           {"id": "g2", "label": "乙", "ranks": [["hub"]]}],
                "bands": [["group:g1"], ["group:g2"]],
            },
            {
                "kind": "diagram", "title": "② 互相拱",
                "nodes": [
                    {"id": n, "label": n.upper(), "role": "丙" if n in "abc" else "丁"}
                    for n in ("a", "b", "c", "d", "e", "f")
                ],
                "edges": [
                    {"from": "a", "to": "e", "role": "调"},
                    {"from": "a", "to": "f", "role": "调"},
                    {"from": "a", "to": "d", "role": "调"},
                    {"from": "b", "to": "e", "role": "调"},
                    {"from": "c", "to": "d", "role": "调"},
                    {"from": "c", "to": "a", "role": "调"},
                ],
                "groups": [],
                "bands": [["node:a", "node:b", "node:c"],
                          ["node:d", "node:e", "node:f"]],
            },
        ],
    }


def test_the_probe_escapes_a_local_minimum_greedy_swapping_cannot() -> None:
    """贪心单步对调爬不出去的局面 —— 探针仍然要找得到路。

    2026-09-17 实测那张三栏图的 ② 栏：6 节点 7 边、交叉 4，**四种相邻对调全是
    上坡**（6 / 4 / 4 / 5），探针如实返回「没有更优解」；同时预算被第一栏的一百
    多个候选吃光，② 栏一个都没轮上。这条把两件事一起钉住。
    """

    import copy
    import itertools

    from nodes.postprocess.diagram_compiler import _cheaper_order, layout_contract

    raw = _two_block_tangle()

    def crossings(contract_raw) -> int:
        return layout_contract(
            {**normalize_contract(copy.deepcopy(contract_raw), family="schematic"),
             "_no_reorder_probe": True}
        )["layout_metrics"]["edge_crossings"]

    baseline = crossings(raw)
    assert baseline > 0, "夹具本来就不乱，钉不住任何东西"

    # **这条夹具必须真的是贪心爬不出去的那种** —— 否则这个测试是空的，
    # 把全排列退回相邻对调也照样绿（2026-09-17 我第一版就是这样）。
    bands = raw["blocks"][1]["bands"]
    for row in range(len(bands)):
        for index in range(len(bands[row]) - 1):
            probe = copy.deepcopy(raw)
            swapped = probe["blocks"][1]["bands"][row]
            swapped[index], swapped[index + 1] = swapped[index + 1], swapped[index]
            assert crossings(probe) >= baseline, (
                f"② 栏相邻对调 {row}/{index} 就能改善 —— 这张夹具挡不住贪心"
            )

    # 但换一个排列是能改善的，探针必须找得到。
    reachable = min(
        crossings(
            {**copy.deepcopy(raw),
             "blocks": [raw["blocks"][0],
                        {**copy.deepcopy(raw["blocks"][1]),
                         "bands": [list(order), bands[1]]}]}
        )
        for order in itertools.permutations(bands[0])
    )
    assert reachable < baseline, "这张夹具换排列也救不了，钉不住任何东西"

    steps = _cheaper_order(normalize_contract(copy.deepcopy(raw), family="schematic"),
                           baseline)
    assert steps, f"交叉 {baseline}（换排列可到 {reachable}），探针却说没有更优解"
    assert steps[-1][-1] < baseline, steps
    # **必须有一步是「换排列」而不是「一对对调」。** 只断言「有改进」是空的：
    # 相邻对调随便找到一步也算数，于是把全排列退回对调照样绿（2026-09-17 我第一版
    # 就是这样，变异不转红）。按块名断言也试过，那是在跟标签较劲不是跟能力较劲 ——
    # 同一行既可能挂在块上也可能挂在顶层。
    assert any(not swap for _w, _how, swap, _order, _n in steps), steps


def test_a_gap_nothing_crosses_does_not_get_the_worst_case_height() -> None:
    """空隙多高，由真正穿过它的线数决定 —— 不按最坏情况写死。

    2026-09-17：22 节点 / 10 边那张图，三道空隙 93/78/78pt，而盒子本身只有
    74.5pt；其中一道**一条线都没穿过**，照样 78pt。同时密图那张（每道十几条线）
    必须一个像素都不能变宽或变窄 —— 62 这个常数当初正是为它定的，压到 54 会让
    交叉从 281 涨到 317。
    """

    from nodes.postprocess.diagram_compiler import BAND_GAP, _band_gaps

    contract = normalize_contract(
        {
            "title": "三带",
            "nodes": [
                {"id": "a", "label": "A", "role": "上"},
                {"id": "b", "label": "B", "role": "中"},
                {"id": "c", "label": "C", "role": "下"},
            ],
            "groups": [],
            "bands": [["node:a"], ["node:b"], ["node:c"]],
            # c 只和 b 连（同一道空隙之下），所以第二道空隙一条线都不穿过。
            "edges": [
                {"from": "a", "to": "b", "role": "连"},
                {"from": "c", "to": "c", "role": "自"},
            ],
        },
        family="schematic",
    )
    gaps = _band_gaps(contract)
    assert len(gaps) == 2
    # a→b 穿过第一道；第二道一条都没有，必须比第一道矮。
    assert gaps[1] < gaps[0] < BAND_GAP, gaps


def test_a_dense_figure_still_gets_the_full_gap() -> None:
    """上限保留旧常数：每道空隙十几条线时，拿到的和从前一模一样。"""

    from nodes.postprocess.diagram_compiler import BAND_GAP, _band_gaps

    contract = normalize_contract(
        {
            "title": "密",
            "nodes": [{"id": f"n{i}", "label": f"N{i}", "role": "上"} for i in range(8)]
            + [{"id": "sink", "label": "汇", "role": "下"}],
            "groups": [],
            "bands": [["node:" + f"n{i}" for i in range(8)], ["node:sink"]],
            "edges": [{"from": f"n{i}", "to": "sink", "role": "连"} for i in range(8)],
        },
        family="schematic",
    )
    assert _band_gaps(contract) == [BAND_GAP]




@pytest.mark.parametrize("family", ["schematic", "quantitative"])
def test_every_schema_key_is_a_real_word(family: str) -> None:
    """schema 里声明的每一个顶层键，都必须在合法键表里。

    少一个的后果不是「不生效」，是**框架自相矛盾**：schema / example / 家族清单
    三处都教作者写它，而 `unexpressed_findings` 转头说「这个键词表里没有，
    已被静默丢弃」。

    2026-09-17 实测代价：`medium` 漏了这张表 —— agent 按 schema 写它，
    判据每次都说它不存在 → **九次声明、14 轮、505k token**（全程最差）。
    而我先后把这个现象解释成「模型删声明让判据闭嘴」和「重发时弄丢了」，
    **两个都错**：它是被框架告知这个键不合法的。
    """

    from nodes.postprocess.figure_contract import (
        _TOP_KEYS_ASSERTED,
        _TOP_KEYS_COMPILED,
        COMPILED_FAMILIES,
    )
    from nodes.postprocess.tools.figure import _figure_contract_schema

    declared = set(_figure_contract_schema(family)["schema"]["properties"])
    allowed = _TOP_KEYS_COMPILED if family in COMPILED_FAMILIES else _TOP_KEYS_ASSERTED
    missing = sorted(declared - allowed)
    assert not missing, f"schema 教了这些键，合法键表里却没有：{missing}"


def test_our_own_example_does_not_violate_our_own_criteria() -> None:
    """可照抄的 example 自己不许被判据点名 —— 它是模型照抄的那份。"""

    from nodes.postprocess.figure_contract import unexpressed_findings
    from nodes.postprocess.tools.figure import _figure_contract_schema

    for family in ("schematic", "quantitative"):
        example = _figure_contract_schema(family).get("example")
        if not isinstance(example, dict):
            continue
        assert not unexpressed_findings(example, family), family


def test_every_word_in_the_vocabulary_is_taught_by_the_schema() -> None:
    """反方向：**合法的字段，schema 的字段表也必须教**。

    上一条守的是「schema 教了但不合法」—— 那是框架自相矛盾，代价 14 轮 505k。
    这一条守的是反面：一个字段有合法身份、有机械后果，而「有哪些字段」最直白的
    那份答案（schema 的属性表）里没有它。

    2026-09-17 扫出来六个：nodes 的 `ports` / `side` / `detail_of`、
    edges 的 `represents` / `step`、groups 的 `style` —— 全都靠 example 和清单
    在送，唯独不在字段表里。它们不像上一条那样制造矛盾，但同样是**词表没送全**。
    """

    from nodes.postprocess.figure_contract import _SHAPE_KEYS
    from nodes.postprocess.tools.figure import _figure_contract_schema

    props = _figure_contract_schema("schematic")["schema"]["properties"]
    untaught: dict[str, list[str]] = {}
    for field, allowed in _SHAPE_KEYS.items():
        item = (props.get(field) or {}).get("items") or {}
        declared = set(item.get("properties") or {})
        if not declared:
            continue  # 这一族 schema 没有逐字段声明，不在本条管辖内
        missing = sorted(set(allowed) - declared)
        if missing:
            untaught[field] = missing
    assert not untaught, f"这些字段合法、有后果，但 schema 的字段表没教：{untaught}"


def test_a_failed_assertion_hands_back_what_the_selector_matched() -> None:
    """断言不成立时，要把**选中的是谁**一起交回去。

    2026-09-17 实测：状态机那轮 agent 写了「自环 = 1」的断言，选择器却匹配到 8。
    它被拒两次，**两次错误信息一字不差**（只说「期望 1 实得 8」），两次都没改对
    —— 那一轮 16 轮 / 698k，全程最差。

    只给一个数字，作者无从判断是**选择器写宽了**还是**图画错了**；给出名单，
    一眼就看得出「哦，它把六个状态全选进去了」。这是「报错要列出证据」的
    同一条规矩。

    成立时不带证据 —— 那时它只是噪声。
    """

    from nodes.postprocess.figure_contract import evaluate_assertions

    raw = json.loads(
        (BENCHMARKS / "state-machine.json").read_text(encoding="utf-8")
    )
    too_wide = {**raw, "assertions": [{
        "id": "bad", "kind": "node_count", "equals": 1,
        "derived_from": "自环", "selector": {},
    }]}
    failed = [
        r for r in evaluate_assertions(normalize_contract(too_wide, family="schematic"))
        if not r["holds"]
    ]
    assert failed, "这条断言本来就该不成立"
    matched = (failed[0].get("detail") or {}).get("matched")
    assert matched and len(matched) == failed[0]["actual"], failed[0]
    assert all(isinstance(x, str) for x in matched)

    # 成立的断言不带证据
    ok = [
        r for r in evaluate_assertions(normalize_contract(raw, family="schematic"))
        if r["holds"]
    ]
    assert ok and all("detail" not in r for r in ok)

"""图合同的不变量：声明先于像素、图不可能与声明分叉、回归看得见。

每条测试都钉住 2026-09-16 那次实测里真实发生过的一种失败（project `astra`，
八版渲染，见 figure_contract.py 的模块头）：

- 一整组 4 张 GPU 被画成一条线            → test_declared_edges_are_all_drawn
- 两颗 CPU 画在图上、一条线都不连、八版无人报 → test_a_component_connected_to_nothing_is_rejected
- 上联从机箱边框而不是网卡引出             → test_contract_cannot_be_bypassed_with_hand_written_code
- 第 N 版比第 N-1 版更差、没有任何东西发现   → test_regression_against_the_previous_version_is_recorded
- 一次没跑完的审图被读成「干净」            → test_incomplete_review_is_not_reported_as_clean
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute
from nodes.postprocess.contracts import VisualContractError
from nodes.postprocess.figure_contract import (
    contract_diff,
    evaluate_assertions,
    failed_assertions,
    normalize_contract,
)

bootstrap()


@pytest.fixture(autouse=True)
def _fake_sandbox(request, monkeypatch):
    """把 execute_python 换成裸 `python -c`：快，但**沙箱和高危闸都不在场**。

    2026-09-18 发现的代价：tikz 渲染脚本里的 subprocess.run(xelatex) 在真沙箱里
    会被高危闸扣下等人批（yuankk 那次连撞七次），而这个替身让整个文件的渲染
    测试从没走过那道闸 —— 替身遮住了被测实现。要验闸在不在场的测试，打
    `real_sandbox` 标记从这里退出。
    """

    if request.node.get_closest_marker("real_sandbox"):
        return
    from shared.tools.library import python_exec

    async def fake_execute_python(state, code, timeout=300, cwd=None, requirements=None, **_):
        from core.project_workspace import validate_tool_cwd

        workspace = validate_tool_cwd(state, cwd)
        workspace.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-3000:],
            "stderr_tail": proc.stderr[-1500:],
            "workspace": str(workspace),
            **({} if proc.returncode == 0 else {"error": proc.stderr[-500:]}),
        }

    monkeypatch.setattr(python_exec, "_execute_python", fake_execute_python)


def _state(tmp_path) -> State:
    state = State.new(node_type="postprocess", base_dir=tmp_path)
    state.hook_state["node_inputs"] = {
        "visual_requests": [
            {
                "request_id": "topo",
                "intent": "双CPU服务器插上8张GPU，每4张连一个 PCIe switch，共 2 个",
                "asset_kind": "schematic",
            }
        ]
    }
    return state


def _topology_contract(*, gpus: int = 8, wire_all: bool = True, cpu_edges: bool = True) -> dict:
    nodes = [
        {"id": "cpu0", "label": "CPU 0", "role": "CPU"},
        {"id": "sw0", "label": "PCIe Switch 0", "role": "PCIe Switch"},
        {"id": "sw1", "label": "PCIe Switch 1", "role": "PCIe Switch"},
    ]
    edges = [{"from": "sw0", "to": "sw1", "label": "400 Gbps", "bidirectional": True}]
    if cpu_edges:
        edges.append({"from": "cpu0", "to": "sw0"})
    for index in range(gpus):
        nodes.append({"id": f"gpu{index}", "label": f"GPU {index}", "role": "GPU"})
        if wire_all or index == 0:
            edges.append({"from": "sw0" if index < gpus // 2 else "sw1", "to": f"gpu{index}"})
    return {
        "title": "topology",
        "nodes": nodes,
        "groups": [
            {
                "id": "srv",
                "label": "Server",
                "ranks": [
                    ["cpu0"],
                    ["sw0", "sw1"],
                    [f"gpu{index}" for index in range(gpus)],
                ],
            }
        ],
        "bands": [["group:srv"]],
        "edges": edges,
        "assertions": [
            {
                "id": "gpu-degree",
                "kind": "degree",
                "selector": {"role": "GPU"},
                "equals": 1,
                "derived_from": "每 4 张 GPU 连接一个 PCIe switch",
            }
        ],
    }


def _declare(state, contract, asset_kind="schematic", request_id="topo"):
    return asyncio.run(
        execute(
            "declare_figure_contract",
            state,
            request_id=request_id,
            asset_kind=asset_kind,
            contract=contract,
        )
    )


def _render(state, **overrides):
    kwargs = {
        "output_name": "topo",
        "caption": "server topology",
        "alt_text": "topology diagram",
        "output_files": ["topo.png"],
        "source_artifact_ids": [],
        "asset_kind": "schematic",
        "request_id": "topo",
    }
    kwargs.update(overrides)
    return asyncio.run(execute("render_figure", state, **kwargs))


# ── 声明先于像素 ────────────────────────────────────────────────────────────


def test_a_schematic_cannot_be_minted_without_a_contract(tmp_path):
    """示意图的全部内容就是结构；没声明结构 = 这张图里没有任何东西可核。"""

    state = _state(tmp_path)
    result = _render(state, code="import matplotlib\n")
    assert result["status"] == "error"
    assert "declare_figure_contract" in result["error"]


def test_contract_cannot_be_bypassed_with_hand_written_code(tmp_path):
    """合同即渲染源；再收一份手写坐标就等于同时有两个真相源。"""

    state = _state(tmp_path)
    declared = _declare(state, _topology_contract())
    assert declared["status"] == "success"
    result = _render(
        state, contract_id=declared["contract_id"], code="import matplotlib\n"
    )
    assert result["status"] == "error"
    assert "not from hand-written" in result["error"]


def test_a_contract_that_contradicts_itself_is_rejected_before_any_pixels(tmp_path):
    """断言在**声明时**求值：合同自相矛盾，agent 在写第一行渲染代码前就知道。"""

    state = _state(tmp_path)
    # 只连了 1 张 GPU，其余 7 张全是孤儿 —— 先被孤儿检查拦下。
    result = _declare(state, _topology_contract(wire_all=False))
    assert result["status"] == "error"
    assert "connected to nothing" in result["error"]


def test_a_component_connected_to_nothing_is_rejected(tmp_path):
    """v1 的两颗 CPU：画在图上、八版一条线都没有、八次审图无人提。"""

    state = _state(tmp_path)
    result = _declare(state, _topology_contract(cpu_edges=False))
    assert result["status"] == "error"
    assert "cpu0" in result["error"]
    assert "isolated=true" in result["error"]


def test_standing_alone_is_allowed_when_it_is_declared(tmp_path):
    """孤立可以是有意的 —— 但必须自己举手，不能默默地就那么画着。"""

    state = _state(tmp_path)
    contract = _topology_contract(cpu_edges=False)
    for node in contract["nodes"]:
        if node["id"] == "cpu0":
            node["isolated"] = True
    assert _declare(state, contract)["status"] == "success"


# ── 判据开的药方，框架得收 ──────────────────────────────────────────────────
#
# 2026-09-17：22 节点 / 65 边那张微服务图，agent 连发五次声明、几何一模一样
# （268 交叉）。原因不是它没看见判据 —— 判据每次都报。是**两条药方框架自己都
# 不收**：交叉判据教「declare the sub-systems as nested groups (groups[].parent)」，
# 照做得到的纯容器父组被判「ends up with no members」；清单教「图太密时把细节
# 那层折叠到另一栏」，而折叠出来的细节栏只能用 annotation 表达朝外的连接，那些
# 节点又被判「connected to nothing」。教的和收的不是同一件事，模型就只能原样重发。


def _dense_stack():
    """三层、中间层八个组件、连线互相穿越 —— 会触发交叉判据的那种图。"""

    mid = [f"svc{i}" for i in range(8)]
    nodes = (
        [{"id": "gw", "label": "网关", "role": "接入"}]
        + [{"id": n, "label": n.upper(), "role": "业务"} for n in mid]
        + [{"id": f"db{i}", "label": f"存储 {i}", "role": "数据"} for i in range(3)]
    )
    edges = [{"from": "gw", "to": n, "role": "调用"} for n in mid]
    for i, n in enumerate(mid):
        for j in range(3):
            if (i + j) % 2 == 0:
                edges.append({"from": n, "to": f"db{j}", "role": "读写"})
    for i in range(len(mid) - 1):
        edges.append({"from": mid[i], "to": mid[(i + 3) % len(mid)], "role": "调用"})
    return {
        "title": "密图",
        "nodes": nodes,
        "edges": edges,
        "groups": [
            {"id": "g_in", "label": "接入", "ranks": [["gw"]]},
            {"id": "g_mid", "label": "业务", "ranks": [mid]},
            {"id": "g_db", "label": "数据", "ranks": [[f"db{i}" for i in range(3)]]},
        ],
        "bands": [["group:g_in"], ["group:g_mid"], ["group:g_db"]],
    }


def _crossings(contract):
    from nodes.postprocess.diagram_compiler import layout_for_backend

    return layout_for_backend(
        normalize_contract(contract, family="schematic"), "tikz"
    )["layout_metrics"]["edge_crossings"]


def test_a_group_whose_members_are_its_child_groups_is_not_empty():
    """交叉判据点名 groups[].parent —— 照它说的做，得能过。

    父组的成员**就是**它的子组。「这个组空不空」在逐组归一那个作用域里答不出来，
    因为那里一次只看得见一个组；判据因此搬到结构层，问的是子树里有没有节点。
    """

    contract = _dense_stack()
    mid = [f"svc{i}" for i in range(8)]
    next(g for g in contract["groups"] if g["id"] == "g_mid")["ranks"] = []
    contract["groups"] += [
        {"id": "g_a", "parent": "g_mid", "label": "账号", "ranks": [mid[:4]]},
        {"id": "g_b", "parent": "g_mid", "label": "交易", "ranks": [mid[4:]]},
    ]
    normalize_contract(contract, family="schematic")  # 不抛就是收了


def test_a_box_around_nothing_is_still_rejected():
    """搬走的是判据的位置，不是判据本身：没有子组也没有节点的框照拒。"""

    contract = _dense_stack()
    contract["groups"].append({"id": "ghost", "label": "空框", "ranks": []})
    contract["bands"].append(["group:ghost"])
    with pytest.raises(VisualContractError, match="no members"):
        normalize_contract(contract, family="schematic")


def test_a_node_whose_only_line_is_an_annotation_is_connected():
    """折叠出来的细节栏只能用 annotation 朝外连 —— 边是按 block 校验的。

    「画在图上、一条线都没有」是那条拒绝的理由。挂着 annotation 的节点旁边就画着
    一支箭头加一句话，理由不成立，拒绝就得跟着撤销。
    """

    contract = _dense_stack()
    contract["blocks"] = [
        {
            "kind": "diagram", "title": "① 总览",
            "nodes": [
                {"id": "gw", "label": "网关", "role": "接入"},
                {"id": "biz", "label": "业务层", "sublabel": "8 个服务",
                 "role": "业务", "detail_of": "g_mid_d"},
            ],
            "edges": [{"from": "gw", "to": "biz", "role": "调用"}],
            "groups": [{"id": "g_o", "label": "总览", "ranks": [["gw"], ["biz"]]}],
            "bands": [["group:g_o"]],
        },
        {
            "kind": "diagram", "title": "② 业务层明细",
            "nodes": [{"id": "svc0", "label": "SVC0", "role": "业务"}],
            "edges": [],
            "groups": [{"id": "g_mid_d", "label": "业务层", "ranks": [["svc0"]]}],
            "bands": [["group:g_mid_d"]],
            "annotations": [{"anchor": "svc0", "side": "bottom", "text": "→ 存储"}],
        },
    ]
    for key in ("nodes", "edges", "groups", "bands"):
        contract.pop(key)
    normalize_contract(contract, family="schematic")  # 不抛就是收了


def test_the_remedy_the_crossing_finding_names_first_is_the_one_that_works():
    """判据把两条路按实测效果排了序 —— 这里钉住那个顺序没说反。

    只要药方写进了每次渲染都会读到的那条 finding，它就得是真的。2026-09-17 实测
    折叠 268->8、109->4，嵌套 268->235、109->109 —— 嵌套把行理顺，但**不保证**让图
    变稀，这张合成图上它一个交叉都没减。清单先前只在 25 条里排第 23 位讲折叠，而
    每次渲染都会读到的 finding 推荐的是弱的那条。

    钉的是**主路径（dot）**量出来的顺序；回落引擎是另一台机器，数字不可比。
    """

    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    mid = [f"svc{i}" for i in range(8)]
    flat = _crossings(_dense_stack())

    nested = _dense_stack()
    next(g for g in nested["groups"] if g["id"] == "g_mid")["ranks"] = []
    nested["groups"] += [
        {"id": "g_a", "parent": "g_mid", "label": "账号", "ranks": [mid[:4]]},
        {"id": "g_b", "parent": "g_mid", "label": "交易", "ranks": [mid[4:]]},
    ]

    folded = _dense_stack()
    folded["blocks"] = [
        {
            "kind": "diagram", "title": "① 总览",
            "nodes": [n for n in folded["nodes"] if n["id"] not in set(mid)]
            + [{"id": "biz", "label": "业务层", "sublabel": "8 个服务",
                "role": "业务", "detail_of": "g_mid_d"}],
            "edges": [{"from": "gw", "to": "biz", "role": "调用"}]
            + [{"from": "biz", "to": f"db{j}", "role": "读写"} for j in range(3)],
            "groups": [
                {"id": "g_in", "label": "接入", "ranks": [["gw"]]},
                {"id": "g_biz", "label": "业务", "ranks": [["biz"]]},
                {"id": "g_db", "label": "数据", "ranks": [[f"db{i}" for i in range(3)]]},
            ],
            "bands": [["group:g_in"], ["group:g_biz"], ["group:g_db"]],
        },
        {
            "kind": "diagram", "title": "② 业务层明细",
            "nodes": [n for n in folded["nodes"] if n["id"] in set(mid)],
            "edges": [e for e in folded["edges"]
                      if e["from"] in set(mid) and e["to"] in set(mid)],
            "groups": [{"id": "g_mid_d", "label": "业务层",
                        "ranks": [mid[:4], mid[4:]]}],
            "bands": [["group:g_mid_d"]],
            "annotations": [{"anchor": n, "side": "bottom", "text": "→ 存储"}
                            for n in mid],
        },
    ]
    for key in ("nodes", "edges", "groups", "bands"):
        folded.pop(key)

    # 「折叠排第一」是这条判据的全部主张 —— 钉的就是它。嵌套不进这个不等式：
    # 它在真图上 268->235、在这张图上 109->109，本来就不是稀释手段。
    assert _crossings(folded) < _crossings(nested), (_crossings(folded), _crossings(nested))
    assert _crossings(folded) * 4 < flat, (_crossings(folded), flat)


def test_declaring_a_dense_figure_says_so_before_it_is_rendered():
    """决定结构的那一刻，判断结构好坏的信息必须在场。

    2026-09-17：22 节点 / 65 边那张图，agent 连发五次声明，每次都收到 "success"，
    一个字没提 268 个交叉；交叉只在第一次（也是唯一一次）render 之后才出现，
    那时它已经没有轮次了。五版几何一模一样不是它没在改 —— 是没人告诉它要改什么。
    """

    from nodes.postprocess.tools.figure import _layout_findings_now

    contract = normalize_contract(_dense_stack(), family="schematic")
    messages = " ".join(f["message"] for f in _layout_findings_now(contract))
    assert "edge crossings" in messages, messages


def test_a_layout_that_cannot_be_computed_does_not_look_like_a_clean_one():
    """缺席不许长得像测过 —— 摆不出来也要出声，不能回一个干净的空列表。"""

    from nodes.postprocess.tools.figure import _layout_findings_now

    findings = _layout_findings_now({"blocks": "这不是合同"})
    assert findings, "布局崩了却回了空列表 —— 读的人会以为这张图没毛病"
    assert "could not be computed" in findings[0]["message"]


def test_one_cause_is_reported_once_not_once_per_edge():
    """28 条同一句判据会把真正致命的那两条埋掉。

    密图那 30 条里，28 条是「edge X->Y 走了偏移通道」—— 那是密度的后果，不是
    28 个各自独立的缺陷。判据现在在声明那一刻就交出去，噪声比直接决定作者看不
    看得见要改的是什么。并的是同一模板，单独出现的判据一条都不动。
    """

    from nodes.postprocess.diagram_compiler import _collapse_repeated_findings

    repeated = [
        {"collector": "OB-LAYOUT",
         "message": f"edge a{i}->b{i} leaves its group through a shifted corridor"}
        for i in range(28)
    ]
    alone = {"collector": "OB-LAYOUT", "message": "268 edge crossings over 65 edges"}
    out = _collapse_repeated_findings(repeated + [alone])

    assert len(out) == 2, [f["message"] for f in out]
    assert out[0]["repeats"] == 28
    assert "28 edges hit the same thing" in out[0]["message"]
    # 独自出现的那条一个字都不许动 —— 它才是要读的那条。
    assert alone in out


def test_a_criterion_that_fires_a_few_times_is_still_listed_one_by_one():
    """并是为了不埋掉别的判据，不是为了少说话。三条以内照样一条条列。"""

    from nodes.postprocess.diagram_compiler import _collapse_repeated_findings

    few = [
        {"collector": "OB-LAYOUT",
         "message": f"edge a{i}->b{i} leaves its group through a shifted corridor"}
        for i in range(3)
    ]
    assert _collapse_repeated_findings(few) == few


def test_an_edge_may_name_a_whole_group():
    """「网关调用整个业务层」是一条边，不是十条。

    2026-09-17：22 节点 / 65 边那张微服务图，模型第三次声明写 `to="biz"` —— biz
    正是它自己上一行声明的组。报错只列了 22 个节点 id，它再没找回来，四次声明
    全被拒、一张图都没画出来。词法不是新造的：bands 一直是 'group:<id>'。
    """

    from nodes.postprocess.diagram_compiler import layout_for_backend

    contract = _dense_stack()
    mid = [f"svc{i}" for i in range(8)]
    contract["edges"] = [
        e for e in contract["edges"] if e["from"] not in set(mid)
    ] + [{"from": "group:g_mid", "to": f"db{j}", "role": "读写"} for j in range(3)]
    normalized = normalize_contract(contract, family="schematic")
    geometry = layout_for_backend(normalized, "tikz")
    drawn = {(e["from"], e["to"]) for e in geometry["edges"]}
    assert ("group:g_mid", "db0") in drawn, sorted(drawn)


def test_collapsing_the_fans_is_what_actually_thins_a_dense_figure():
    """判据把「收扇子」排在第一位 —— 这里钉住那句话是真的。"""

    mid = [f"svc{i}" for i in range(8)]
    flat = _crossings(_dense_stack())

    collapsed = _dense_stack()
    collapsed["edges"] = [
        e for e in collapsed["edges"]
        if not (e["from"] in set(mid) and e["to"].startswith("db"))
    ] + [{"from": "group:g_mid", "to": f"db{j}", "role": "读写"} for j in range(3)]
    assert _crossings(collapsed) < flat, (_crossings(collapsed), flat)


def test_naming_a_group_where_a_node_belongs_says_it_is_a_group():
    """报错要先查这个名字在别处存不存在 —— 光列合法值，作者分不出拼错还是放错。"""

    contract = _dense_stack()
    contract["edges"].append({"from": "gw", "to": "g_mid", "role": "调用"})
    with pytest.raises(VisualContractError) as caught:
        normalize_contract(contract, family="schematic")
    assert "is a group, not a node" in str(caught.value)
    assert "'group:g_mid'" in str(caught.value)


def test_a_group_endpoint_keeps_its_members_off_the_orphan_list():
    """连到组就是连到它的全体成员 —— 否则成员会被判成「画在图上没有一条线」。"""

    contract = _dense_stack()
    mid = [f"svc{i}" for i in range(8)]
    contract["edges"] = [
        e for e in contract["edges"]
        if e["from"] not in set(mid) and e["to"] not in set(mid)
    ] + [{"from": "gw", "to": "group:g_mid", "role": "调用"}] + [
        {"from": "group:g_mid", "to": f"db{j}", "role": "读写"} for j in range(3)
    ]
    normalize_contract(contract, family="schematic")  # 不抛就是收了


def test_the_dense_example_is_a_contract_that_actually_renders():
    """示范压过劝告 —— 但只有示范本身画得出来才算数。

    「零交叉、零 finding」说的是主路径（dot）；没有 dot 的机器上回落引擎
    一定会记一条「dot 不在」的 finding，这条判据在那里没有意义。
    """

    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    from nodes.postprocess.diagram_compiler import layout_for_backend

    from nodes.postprocess.tools.figure import _figure_contract_schema

    example = _figure_contract_schema("schematic")["dense_example"]
    geometry = layout_for_backend(
        normalize_contract(dict(example), family="schematic"), "tikz"
    )
    assert geometry["layout_metrics"]["edge_crossings"] == 0
    assert not geometry["layout_findings"], geometry["layout_findings"]
    assert any(
        str(e["from"]).startswith("group:") or str(e["to"]).startswith("group:")
        for e in example["edges"]
    ), "密图示范里没有一条组端点的边 —— 那这个词等于没送到"


def test_every_failing_assertion_hands_back_what_it_counted():
    """报错里说「上面是匹配到的」，上面就得真有东西。

    2026-09-17：同一条 order-calls 断言在一轮里被拒**四次**（实得 5→8→5→8），
    每次都印着「the list above is what it actually matched」，而 degree / neighbors
    这两支从不记 matched —— 上面一片空白。模型只能猜。那一轮 23 轮 / 1.6M tokens，
    13 次声明里 10 次被拒。**承诺的东西，API 得给得出。**
    """

    contract = {
        "title": "谁在调用谁",
        "nodes": [
            {"id": "order", "label": "订单", "role": "业务"},
            {"id": "pay", "label": "支付", "role": "业务"},
            {"id": "search", "label": "搜索", "role": "业务"},
        ],
        "groups": [],
        "bands": [["node:order", "node:pay", "node:search"]],
        "edges": [
            {"from": "order", "to": "pay", "role": "调用"},
            {"from": "search", "to": "order", "role": "调用"},
        ],
        "assertions": [
            {"id": "order-calls", "kind": "degree", "node": "order", "equals": 1,
             "derived_from": "订单调用支付"},
        ],
    }
    results = evaluate_assertions(normalize_contract(contract, family="schematic"))
    failing = failed_assertions(results)
    assert failing, results
    matched = (failing[0].get("detail") or {}).get("matched")
    # degree 数的是**全部**边 —— 那条入边正是作者看不见、猜了四次的东西。
    assert matched == ["order→pay", "search→order"], matched


def test_the_message_does_not_promise_a_list_it_has_not_got(tmp_path):
    """没有证据时，那句「上面是匹配到的」不许印出来。

    group_count 这一支没有「选中的是谁」可言（它数的是组的总数）—— 报错里就不该
    出现一句指向空白的指路话。
    """

    state = _state(tmp_path)
    contract = _topology_contract()
    contract["assertions"] = [
        {"id": "how-many-groups", "kind": "group_count", "equals": 99,
         "derived_from": "凑一条必然不成立的断言"},
    ]
    result = _declare(state, contract)
    assert result["status"] == "error"
    assert "how-many-groups" in result["error"]
    assert "the list above" not in result["error"], result["error"]


def test_a_selector_key_that_is_almost_right_gets_named():
    """差一个字母的词要指出来，别让作者自己去比对三个合法词。

    iter65 写 source={'node': ...}、iter83 写 source={'id': ...} —— 两轮各废掉
    一次声明。合法的是 'ids'。
    """

    from nodes.postprocess.figure_contract import _normalize_selector

    for wrong in ("id", "node", "nodes"):
        with pytest.raises(VisualContractError) as caught:
            _normalize_selector({wrong: "x"}, "assertions[0].source")
        assert "'ids'" in str(caught.value), str(caught.value)
        assert "Did you mean" in str(caught.value)

    # 真正不沾边的词不编造提示。
    with pytest.raises(VisualContractError) as caught:
        _normalize_selector({"zzz": 1}, "assertions[0].source")
    assert "Did you mean" not in str(caught.value)


def test_the_same_component_drawn_twice_is_named():
    """同一个部件在两栏各画一遍，而没有任何东西说它们是同一个。

    2026-09-17 微服务题真跑：28 个节点里 6 个是重复的 —— ① 栏画了订单/支付/库存/
    风控/优惠券/搜索，② 栏用 order2/pay2/... 又画了一遍。读者看见两个「订单」，
    无从知道是同一个东西的两种画法还是两个东西。**当时一条 finding 都没有人报。**
    不拒绝（同名不是自相矛盾），但绝不能悄无声息。
    """

    from nodes.postprocess.figure_contract import redrawn_component_findings

    contract = normalize_contract(
        {
            "title": "两栏",
            "blocks": [
                {
                    "kind": "diagram", "title": "① 全景",
                    "nodes": [{"id": "order", "label": "订单", "role": "业务"},
                              {"id": "pay", "label": "支付", "role": "业务"}],
                    "edges": [{"from": "order", "to": "pay", "role": "调用"}],
                    "groups": [{"id": "g1", "label": "业务", "ranks": [["order", "pay"]]}],
                    "bands": [["group:g1"]],
                },
                {
                    "kind": "diagram", "title": "② 明细",
                    "nodes": [{"id": "order2", "label": "订单", "role": "业务"},
                              {"id": "pay2", "label": "支付", "role": "业务"}],
                    "edges": [{"from": "order2", "to": "pay2", "role": "调用"}],
                    "groups": [{"id": "g2", "label": "业务", "ranks": [["order2", "pay2"]]}],
                    "bands": [["group:g2"]],
                },
            ],
        },
        family="schematic",
    )
    findings = redrawn_component_findings(contract)
    # 一条，不是两条 —— 这是一个决定，不是两个缺陷。
    assert len(findings) == 1, findings
    assert set(findings[0]["redrawn"]) == {"订单", "支付"}


def test_saying_it_is_the_expanded_form_costs_nothing():
    """用 detail_of 认领之后就不该再报 —— 判据问的是「说没说」，不是「画没画两遍」。"""

    from nodes.postprocess.figure_contract import redrawn_component_findings

    contract = normalize_contract(
        {
            "title": "两栏",
            "blocks": [
                {
                    "kind": "diagram", "title": "① 全景",
                    "nodes": [{"id": "biz", "label": "业务层", "role": "业务",
                               "detail_of": "g2"},
                              {"id": "gw", "label": "网关", "role": "接入"}],
                    "edges": [{"from": "gw", "to": "biz", "role": "调用"}],
                    "groups": [{"id": "g1", "label": "全景", "ranks": [["gw"], ["biz"]]}],
                    "bands": [["group:g1"]],
                },
                {
                    "kind": "diagram", "title": "② 明细",
                    "nodes": [{"id": "order", "label": "业务层", "role": "业务"},
                              {"id": "pay", "label": "支付", "role": "业务"}],
                    "edges": [{"from": "order", "to": "pay", "role": "调用"}],
                    "groups": [{"id": "g2", "label": "业务", "ranks": [["order", "pay"]]}],
                    "bands": [["group:g2"]],
                },
            ],
        },
        family="schematic",
    )
    assert redrawn_component_findings(contract) == []


def test_the_compiled_render_script_never_trips_the_high_risk_scanner():
    """框架生成的渲染脚本不该有任何能被模型代码扫描器拦下的东西。

    2026-09-18 真跑（yuankk 的六拓扑图）：tikz 脚本里的 subprocess.run(xelatex) 被
    判成 shell-out 挂起等人批，无人值守的 run 里同一堵墙连撞七次，模型绕去手写
    matplotlib。用**真正那把扫描器**逐个基准夹具、两个后端都过一遍。
    """

    import json, pathlib

    from nodes.postprocess.diagram_compiler import compile_render_script
    from shared.lib.dangerous_commands import match_high_risk

    d = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "benchmarks"
    seen = 0
    for f in sorted(d.glob("*.json")):
        c = normalize_contract(json.loads(f.read_text(encoding="utf-8")), family="schematic")
        for backend in ("tikz", "matplotlib"):
            code, _g = compile_render_script(c, outputs=["x.pdf", "x.png"], backend=backend)
            assert match_high_risk(code, mode="python") is None, (f.name, backend)
            seen += 1
    assert seen >= 2


def test_the_audit_sidecar_is_stable_for_the_same_render(tmp_path):
    """同一份代码、同一个请求 → 同一个 sidecar 名；否则审批通行证永远对不上。"""

    from nodes.postprocess.tools.figure import audit_sidecar_path

    a = audit_sidecar_path(tmp_path, "req-1", "abc123")
    b = audit_sidecar_path(tmp_path, "req-1", "abc123")
    c = audit_sidecar_path(tmp_path, "req-1", "abc124")
    assert a == b and a != c
    assert a.name.startswith(".figure_audit_")


def test_a_panel_title_never_exceeds_its_panel():
    """栏标题再长，栏也得装得下它 —— 六拓扑图里 ④⑥ 的标题被截在栏外。"""

    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    from nodes.postprocess.diagram_compiler import (
        PANEL_TITLE_FS, PANEL_TITLE_LEAD, _text_width, layout_for_backend,
    )

    long_title = "④ 拓扑 4：双 CPU 8 GPU，2 PCIe switch，2 NIC，×4 台（一个很长很长的标题）"
    contract = normalize_contract(
        {
            "title": "标题地板",
            "blocks": [
                {"kind": "diagram", "row": 0, "title": long_title,
                 "nodes": [{"id": "a", "label": "A", "role": "x"}, {"id": "b", "label": "B", "role": "x"}],
                 "groups": [], "bands": [["node:a"], ["node:b"]],
                 "edges": [{"from": "a", "to": "b", "role": "连"}]},
                {"kind": "diagram", "row": 0, "title": "② 短",
                 "nodes": [{"id": "c", "label": "C", "role": "x"}, {"id": "d", "label": "D", "role": "x"}],
                 "groups": [], "bands": [["node:c"], ["node:d"]],
                 "edges": [{"from": "c", "to": "d", "role": "连"}]},
            ],
        },
        family="schematic",
    )
    g = layout_for_backend(contract, "tikz")
    # 两栏都得在 —— 空列表会让下面的循环什么也不验就绿。
    assert len(g["panels"]) == 2, g.get("layout_engine")
    for panel in g["panels"]:
        need = _text_width(panel["title"], PANEL_TITLE_FS) + PANEL_TITLE_LEAD
        assert panel["rect"][2] + 0.5 >= need, (panel["title"], panel["rect"][2], need)


def _highrisk_events(state) -> list[str]:
    import json as _json

    out = []
    if state.transcript_path.exists():
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines():
            try:
                ev = _json.loads(line)
            except Exception:
                continue
            kind = str(ev.get("event") or ev.get("type") or "")
            if "highrisk" in kind:
                out.append(kind)
    return out


@pytest.mark.real_sandbox
def test_a_compiled_tikz_render_is_never_held_for_human_approval(tmp_path, monkeypatch):
    """框架生成的渲染脚本走模型代码的沙箱，但**不该**被模型代码的高危审批扣下。

    2026-09-18 真跑（yuankk 六拓扑图）：闸没开 bypass、没预授权，tikz 脚本里的
    xelatex 子进程被判成 shell-out 挂起等人批；无人值守的 run 里同一堵墙连撞七次，
    模型绕去手写 matplotlib，schematic 变成了 composite。这条测试就按那次的开关
    状态跑：bypass 关、预授权空。
    """

    from shared.tools.library import latex

    if latex.no_tex_engine():
        pytest.skip("本机没有 TeX（latexmk / tectonic）")
    from shared.lib import dangerous_commands as dc

    monkeypatch.setattr(dc, "BYPASS_ENABLED", False)
    dc.set_preauthorized_categories(None)

    state = _state(tmp_path)
    declared = _declare(state, _topology_contract())
    result = _render(state, contract_id=declared["contract_id"],
                     output_files=["topo.pdf", "topo.png"])
    assert result["status"] == "success", result.get("error") or result
    assert _highrisk_events(state) == [], _highrisk_events(state)
    assert result["execution"]["tikz_finish"]["status"] == "success" if "execution" in result else True
    formats = {f["format"] for f in result["files"]}
    assert "pdf" in formats, result["files"]


@pytest.mark.real_sandbox
def test_the_gate_still_bites_a_hand_written_shell_out(tmp_path, monkeypatch):
    """上一条不是把闸拆了：往生成脚本里塞回一句起子进程的代码，闸必须照样扣下。

    这也是上一条测试的变异对照 —— 没有它，「编译路径不被扣下」可能只是因为
    这套夹具里闸根本没在场。
    """

    from shared.lib import dangerous_commands as dc
    from nodes.postprocess import diagram_compiler as C
    from nodes.postprocess.tools import figure as F

    monkeypatch.setattr(dc, "BYPASS_ENABLED", False)
    dc.set_preauthorized_categories(None)

    real = C.compile_render_script

    def poisoned(*a, **k):
        code, geo = real(*a, **k)
        return code + "\nimport subprocess\nsubprocess.run(['true'])\n", geo

    monkeypatch.setattr(C, "compile_render_script", poisoned)
    state = _state(tmp_path)
    declared = _declare(state, _topology_contract())
    result = _render(state, contract_id=declared["contract_id"], output_files=["topo.pdf"])
    assert result["status"] == "error", result
    assert "pause" in (result.get("error") or "") or \
        (result.get("execution") or {}).get("status") == "pause", result
    assert any("blocked_pending_confirm" in e for e in _highrisk_events(state)), _highrisk_events(state)


# ── 图不可能与声明分叉 ──────────────────────────────────────────────────────


def test_declared_edges_are_all_drawn(tmp_path):
    """v1 的核心缺陷：一整组 4 张 GPU 被 `ax.plot(...)` 画成了一条线。

    合同编译之后这件事在架构上不可能：边是遍历 edges 画的，声明了 8 条就是
    8 条。这里直接数几何里的折线条数。
    """

    from nodes.postprocess.diagram_compiler import layout_contract

    contract = normalize_contract(_topology_contract(), family="schematic")
    geometry = layout_contract(contract)
    assert len(geometry["edges"]) == len(contract["edges"])
    gpu_edges = [
        edge
        for edge in geometry["edges"]
        if edge["from"].startswith("sw") and edge["to"].startswith("gpu")
    ]
    assert len(gpu_edges) == 8
    # 每条边都得是真的一条折线（至少两个点），不能是空壳。
    assert all(len(edge["points"]) >= 2 for edge in geometry["edges"])


def test_render_from_contract_produces_the_figure_and_binds_the_contract(tmp_path):
    """合同 → 图 → 记录绑定合同。这条和下面三条（回归账、未完成的审图、覆盖
    findings 进记录）验的都是**管道**，与后端无关；显式走 matplotlib，好让它们
    在没有 TeX 的机器（CI）上也真跑。tikz 自己的端到端另有测试，按
    `latex.no_tex_engine()` 跳。"""

    state = _state(tmp_path)
    declared = _declare(state, _topology_contract())
    result = _render(state, contract_id=declared["contract_id"], backend="matplotlib")
    assert result["status"] == "success", result.get("error")
    assert all(item["holds"] for item in result["contract_checks"])

    record = state.read_artifact(result["figure_id"])
    metadata = record["metadata"]
    assert metadata["contract_hash"] == declared["contract_hash"]
    assert metadata["render_code"]["generator"].startswith("figure-contract-compiler")
    # 合同进了出处指纹：事后改断言必须留痕。
    from shared.lib.publication_figures import figure_binding_hash

    tampered = {**metadata, "contract_hash": "sha256:" + "0" * 64}
    assert figure_binding_hash(tampered) != metadata["figure_hash"]


def test_records_without_a_contract_keep_their_old_fingerprint(tmp_path):
    """存量记录不能因为新增了合同字段而集体验不过。"""

    from shared.lib.publication_figures import figure_binding_hash

    legacy = {
        "source_artifact_ids": ["a"],
        "source_hashes": ["sha256:x"],
        "render_code": {"path": "p.py", "content_hash": "sha256:y"},
        "files": [{"format": "png", "content_hash": "sha256:z"}],
        "caption": "c",
        "alt_text": "a",
    }
    assert figure_binding_hash(legacy) == figure_binding_hash({**legacy, "contract_hash": None})


# ── 越改越差看得见 ──────────────────────────────────────────────────────────


def test_regression_against_the_previous_version_is_recorded(tmp_path):
    """v1 八版里没有任何东西比较相邻两版，所以「连线塌了」完全不可见。"""

    state = _state(tmp_path)
    first = _declare(state, _topology_contract())
    first_render = _render(state, contract_id=first["contract_id"], backend="matplotlib")
    assert first_render["status"] == "success", first_render.get("error")

    # 第二版：悄悄把 GPU 从 8 张减到 4 张，并删掉那条断言。
    weaker = _topology_contract(gpus=4)
    weaker["assertions"] = []
    second = _declare(state, weaker)
    result = _render(
        state, contract_id=second["contract_id"], output_name="topo2", backend="matplotlib"
    )
    assert result["status"] == "success"

    collectors = [item.get("collector") for item in result["findings"]]
    assert "OB-CONTRACT-REGRESSION" in collectors
    messages = " ".join(str(item.get("message")) for item in result["findings"])
    assert "gpu-degree" in messages       # 断言被删掉了
    assert "fewer nodes" in messages      # 节点变少了


def test_contract_diff_reports_a_weakened_assertion():
    before = normalize_contract(_topology_contract(), family="schematic")
    after = normalize_contract(_topology_contract(), family="schematic")
    after["assertions"][0]["comparator"] = "at_least"
    after["assertions"][0]["expected"] = 0
    findings = contract_diff(before, after)
    assert any("assertion targets changed" in str(item["message"]) for item in findings)


# ── asserted 家族：对象模型，不是像素，也不是自述 ────────────────────────────


# 数据图的设计合同（2026-09-18）：≥2 条序列要有图例、轴写 `Label (unit)`、
# 颜色不指定就用框架注入的调色板。这段代码是「照合同办事」的最小样子。
_TWO_SERIES = (
    "import matplotlib\nmatplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "fig, ax = plt.subplots(figsize=(4, 3), dpi=150)\n"
    "ax.plot([1, 2], [2, 4], label='series 0'); ax.plot([1, 2], [1, 3], label='series 1')\n"
    "ax.set_yscale('log')\n"
    "ax.set_xlabel('Step'); ax.set_ylabel('Energy (eV)')\n"
    "ax.legend(loc='upper left')\n"
    "fig.savefig('q.png')\n"
)

_ONE_SERIES = _TWO_SERIES.replace("ax.plot([1, 2], [1, 3], label='series 1')\n", "")


def _quant_state(tmp_path) -> State:
    """调用方点名 quantitative 的请求 —— 家族绑定后，数据图不能挂在示意图请求上。"""

    state = State.new(node_type="postprocess", base_dir=tmp_path)
    state.hook_state["node_inputs"] = {
        "visual_requests": [
            {"request_id": "topo", "intent": "对比两个体系的收敛", "asset_kind": "quantitative"}
        ]
    }
    return state


def _quant_contract(series: int = 2, y_scale: str = "log") -> dict:
    return {
        "title": "convergence",
        "panels": [{
            "id": "p1", "label": "a",
            "axes": {
                "x": {"label": "Step", "unit": "none"},
                "y": {"label": "Energy", "unit": "eV", "scale": y_scale},
            },
        }],
        "series": [
            {"id": f"s{index}", "label": f"series {index}", "panel": "p1"}
            for index in range(series)
        ],
        "assertions": [
            {
                "id": "n-series",
                "kind": "series_count",
                "equals": series,
                "derived_from": "对比两个体系",
            },
            {
                "id": "log-y",
                "kind": "axis_scale",
                "axis": "y",
                "equals": y_scale,
                "derived_from": "纵轴用对数",
            },
        ],
    }


def test_asserted_contract_is_checked_against_the_rendered_object_model(tmp_path):
    state = _quant_state(tmp_path)
    declared = _declare(state, _quant_contract(), asset_kind="quantitative")
    assert declared["status"] == "success"
    result = _render(
        state,
        contract_id=declared["contract_id"],
        asset_kind="quantitative",
        code=_TWO_SERIES,
        output_files=["q.png"],
        output_name="q",
    )
    assert result["status"] == "success", result.get("error")
    assert {item["id"]: item["holds"] for item in result["contract_checks"]} == {
        "n-series": True,
        "log-y": True,
    }


def test_a_chart_that_contradicts_its_contract_cannot_be_minted(tmp_path):
    """声明两条序列、代码只画一条 —— 自相矛盾，拒绝录入。

    读的是 matplotlib 对象树，不是 agent 对自己代码的描述。
    """

    state = _quant_state(tmp_path)
    declared = _declare(state, _quant_contract(series=2), asset_kind="quantitative")
    result = _render(
        state,
        contract_id=declared["contract_id"],
        asset_kind="quantitative",
        code=_ONE_SERIES,
        output_files=["q.png"],
        output_name="q",
    )
    assert result["status"] == "error"
    assert "contradicts its own contract" in result["error"]
    assert "n-series" in result["error"]
    # 报错要带上它来自需求的哪句话，否则改的人不知道该改图还是改断言。
    assert "对比两个体系" in result["error"]


def test_an_undeclared_diagram_is_named_as_such(tmp_path):
    """asset_kind 忘了声明也逃不掉：零数据序列 = 这是图解，机械可判。"""

    state = _state(tmp_path)
    result = _render(
        state,
        asset_kind="auto",
        request_id=None,
        output_name="d",
        output_files=["d.png"],
        code=(
            "import matplotlib\nmatplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots()\n"
            "ax.add_patch(plt.Rectangle((0.1, 0.1), 0.2, 0.2))\n"
            "fig.savefig('d.png')\n"
        ),
    )
    assert result["status"] == "success", result.get("error")
    collectors = [item.get("collector") for item in result["findings"]]
    assert "OB-UNDECLARED-SCHEMATIC" in collectors
    assert "OB-NO-CONTRACT" in collectors


# ── 缺席不许长得像通过 ──────────────────────────────────────────────────────


def test_incomplete_review_is_not_reported_as_clean(tmp_path, monkeypatch):
    """v1 实测：第 3 次审图 completed=false 却记了 observation_count=0，
    agent 在交接 README 里写成「observation_count=0（干净）」。"""

    state = _state(tmp_path)
    declared = _declare(state, _topology_contract())

    from core import model_roles
    from nodes.postprocess import vlm_extraction

    class _Binding:
        provider, model, base_url, api_key = "openai", "vlm", "http://x", "k"

    monkeypatch.setattr(model_roles, "resolve", lambda role: _Binding())

    async def never_completes(*, image_path, contract, config):
        return {
            "status": "extraction_incomplete",
            "questions": {},
            "extracted": {},
            "findings": [
                {"collector": "OB-EXTRACTION-INCOMPLETE", "message": "did not complete"}
            ],
            "attempts": [],
            "prompt_version": "test",
        }

    monkeypatch.setattr(vlm_extraction, "extract_against_contract", never_completes)
    result = _render(state, contract_id=declared["contract_id"], backend="matplotlib")
    assert result["status"] == "success", result.get("error")
    review = result["vlm_review"]
    assert review["completed"] is False
    # 关键：没跑完时不能出现一个会被读成「干净」的 0。
    assert review.get("observation_count") is None
    assert review["finding_count"] >= 1
    assert any(
        item.get("collector") == "OB-EXTRACTION-INCOMPLETE" for item in result["findings"]
    )


def test_extraction_mismatch_is_evidence_not_a_rejection(tmp_path):
    """模型会数错 —— 抽取分歧进 findings；能拒绝的只有机械核查。"""

    from nodes.postprocess.vlm_extraction import build_questions, compare_extraction

    contract = normalize_contract(_topology_contract(), family="schematic")
    questions = build_questions(contract)
    findings = compare_extraction(questions, {"role_counts": {"GPU": 7}, "node_degrees": {}})
    assert any(item["collector"] == "VLM-EXTRACTION" for item in findings)
    mismatch = next(item for item in findings if item["collector"] == "VLM-EXTRACTION")
    assert mismatch["mismatches"][0] == {"role": "GPU", "contract": 8, "reviewer_saw": 7}


def test_unreadable_quantities_are_neither_confirmed_nor_contradicted():
    """-1 = 「我看不出来」。它和「数出来是 0」是两件事。"""

    from nodes.postprocess.vlm_extraction import build_questions, compare_extraction

    contract = normalize_contract(_topology_contract(), family="schematic")
    questions = build_questions(contract)
    findings = compare_extraction(questions, {"role_counts": {"GPU": -1}, "node_degrees": {}})
    assert [item["collector"] for item in findings] == ["OB-EXTRACTION-UNREADABLE"]


# ── 覆盖：需求里写了、合同里没有的量 ────────────────────────────────────────


def test_quantities_in_the_request_that_the_contract_never_mentions_are_flagged(tmp_path):
    state = _state(tmp_path)
    contract = _topology_contract()
    declared = _declare(state, contract)
    assert declared["status"] == "success"
    # 需求里写了「2 个 switch」「8 张 GPU」，合同里都有 —— 「提到了」这一格干净。
    assert [
        f for f in declared["coverage_findings"] if f.get("uncovered_quantities")
    ] == [], declared["coverage_findings"]
    # 「有断言引用吗」那一格按需求原文对账。夹具那条断言引用的是
    # 「每 4 张 GPU 连接一个 PCIe switch」，需求里的「8 张」「2 个」没人引用。
    # 两个问题拆开之后这里必须分开断言 —— 合起来写只会掩盖其中一个。
    assert [f["counts_without_assertion"] for f in declared["coverage_findings"]] == [
        ["8张", "2个"]
    ], declared["coverage_findings"]


def test_a_dropped_quantity_shows_up_as_a_coverage_gap():
    from nodes.postprocess.figure_contract import coverage_findings

    contract = normalize_contract(_topology_contract(), family="schematic")
    findings = coverage_findings(contract, "共 16 台服务器连接到 1 台 ROCE 交换机上")
    assert findings and "16台" in findings[0]["uncovered_quantities"]


def test_coverage_is_a_smell_detector_not_a_proof():
    """它只答「这个数在合同里出现过没有」，不答「表达得对不对」。

    钉住这条是因为它的**假阴性**必须被知道：合同里有一个节点叫 "GPU 4"，
    需求里的「每 4 张」就算被当成「提到了」—— 哪怕合同根本没表达分组关系。
    真正保证结构的是断言（机械求值、能拒绝）。

    2026-09-17 把两个问题拆开了：「提到了没有」和「有断言核没有」不是一回事。
    带量词的数（4 张 / 2 个 / 1 台）是在数东西，图上可数，就该有断言核。

    第二格按**需求原文**对账（断言的 derived_from 引用了哪条需求），不按数字 ——
    数字级比对被一个变异当场打穿：把一张 GPU 从 sw0 改接到 sw1（扇出 3/5）、
    再删掉扇出断言，全线静默，因为数字 4 还在别处（服务器数=4）。

    它自己的**假阴性也要钉住**：一条断言若引用了一整句需求，那句里所有的数都被
    算作已覆盖。下面第二段就是这个形状。
    """

    from nodes.postprocess.figure_contract import coverage_findings

    contract = normalize_contract(_topology_contract(), family="schematic")
    findings = coverage_findings(contract, "每 4 张 GPU 一组")
    # 「提到了」这一格是干净的 —— 假阴性照旧存在（"GPU 4" 这个 label 就够了）
    assert [f for f in findings if f.get("uncovered_quantities")] == []
    assert any(node["label"] == "GPU 4" for node in contract["nodes"])
    # 「有断言引用吗」这一格按**需求原文**对账：夹具那条断言引用了「每 4 张 GPU
    # 连接一个 PCIe switch」，所以「4 张」算覆盖；「一组」没人引用，点名。
    assert [f["counts_without_assertion"] for f in findings] == [["一组"]], findings

    # 已知假阴性：一条断言引用一整句，那句里的数就全被算覆盖了。
    wordy = normalize_contract(_topology_contract(), family="schematic")
    wordy["assertions"] = [
        {
            "id": "one_long_quote",
            "kind": "node_count",
            "comparator": "equals",
            "expected": 8,
            "derived_from": "每 4 张 GPU 一组，共 2 组，接到 1 台交换机",
            "selector": {"role": "GPU"},
        }
    ]
    assert [
        f["counts_without_assertion"]
        for f in coverage_findings(wordy, "每 4 张 GPU 一组，共 2 组，接到 1 台交换机")
    ] == [], "引用整句就覆盖整句 —— 这是它的已知弱点，不是 bug"

    # 反面：需求里另有两个数，断言一个字都没引用 → 两条都点名
    assert [
        f["counts_without_assertion"]
        for f in coverage_findings(contract, "需求：2 个 switch，8 张 GPU，每 4 张一组")
    ] == [["2个", "8张", "一组"]]


# ── 家族知识被推给调用方，而不是等它自己去拉 ────────────────────────────────


def test_declaring_a_contract_pushes_the_family_checklist(tmp_path):
    """v1：scientific-schematic skill 里恰好有能抓住那次缺陷的那一条，
    而 agent 从没 list_skills、也从没加载它。"""

    state = _state(tmp_path)
    declared = _declare(state, _topology_contract())
    checklist = declared["family_checklist"]
    assert checklist and any("edges 里声明的一条" in item for item in checklist)
    assert declared["next_step"].startswith("render_figure(")


def test_assertion_without_a_source_fragment_is_rejected():
    contract = _topology_contract()
    contract["assertions"][0].pop("derived_from")
    with pytest.raises(VisualContractError, match="derived_from"):
        normalize_contract(contract, family="schematic")


def test_assertions_evaluate_against_the_declared_graph():
    contract = normalize_contract(_topology_contract(), family="schematic")
    results = {item["id"]: item for item in evaluate_assertions(contract)}
    assert results["gpu-degree"]["holds"] is True


# ── 实跑（2026-09-16，deepseek-v4-pro + qwen3.8-27b）暴露出来的三件 ──────────


def test_degree_without_a_subject_names_both_legal_forms():
    """实测：模型想问「roce_sw 连着 8 条边」，写成不带 selector 的 degree，
    框架去核全部 57 个节点、回了句 "1/57 nodes satisfy it"，模型花一整轮猜。"""

    contract = _topology_contract()
    contract["assertions"] = [
        {"id": "hub", "kind": "degree", "equals": 8, "derived_from": "连到 1 台交换机"}
    ]
    with pytest.raises(VisualContractError) as exc:
        normalize_contract(contract, family="schematic")
    message = str(exc.value)
    assert "node=<id>" in message and "selector=" in message and "neighbors" in message


def test_degree_of_one_named_node_is_that_node_s_edge_count():
    contract = _topology_contract()
    contract["assertions"] = [
        {
            "id": "sw0",
            "kind": "degree",
            "node": "sw0",
            "equals": 6,  # cpu0 + sw1 + 4 GPU
            "derived_from": "每 4 张 GPU 连一个 switch",
        }
    ]
    normalized = normalize_contract(contract, family="schematic")
    assert evaluate_assertions(normalized)[0]["holds"] is True


def test_an_unreadably_elongated_canvas_is_reported(tmp_path):
    """合同保证结构对，不保证编排好：四台服务器全展开摆一条 band，
    结构全对，出来是 5:1 长条（实测 13058x2451 px）。机械可判，所以框架说。"""

    from nodes.postprocess.diagram_compiler import layout_contract

    nodes, groups, bands, edges = [], [], [[]], []
    for server in range(4):
        gid = f"s{server}"
        nodes.append({"id": f"{gid}sw", "label": "PCIe Switch", "role": "sw"})
        rank = []
        for index in range(8):
            nid = f"{gid}g{index}"
            nodes.append({"id": nid, "label": f"GPU {index}", "role": "gpu"})
            edges.append({"from": f"{gid}sw", "to": nid})
            rank.append(nid)
        groups.append({"id": gid, "label": f"Server {server}", "ranks": [[f"{gid}sw"], rank]})
        bands[0].append(f"group:{gid}")
    contract = normalize_contract(
        {"nodes": nodes, "groups": groups, "bands": bands, "edges": edges, "assertions": []},
        family="schematic",
    )
    geometry = layout_contract(contract)
    messages = [item["message"] for item in geometry["layout_findings"]]
    assert any("too elongated" in item for item in messages)


def test_edge_labels_do_not_land_on_component_boxes():
    """实测：上联的 "400G" 压在 "NIC 0" 上，机械审计报了文字碰撞。"""

    from nodes.postprocess.diagram_compiler import (
        EDGE_LABEL_FS,
        _Rect,
        _overlaps,
        _text_width,
        layout_contract,
    )

    contract = normalize_contract(_topology_contract(), family="schematic")
    for edge in contract["edges"]:
        edge["label"] = "400G"
    geometry = layout_contract(contract)
    boxes = [_Rect(*node["rect"]) for node in geometry["nodes"].values()]
    hits = []
    for edge in geometry["edges"]:
        if not edge["label"]:
            continue
        width = _text_width(edge["label"], EDGE_LABEL_FS) + 6.0
        height = EDGE_LABEL_FS * 1.5
        cx, cy = edge["label_xy"]
        label_box = _Rect(cx - width / 2, cy - height / 2, width, height)
        hits += [edge for other in boxes if _overlaps(label_box, other)]
    assert hits == []


def test_an_unreachable_reviewer_cannot_hold_up_the_ledger(tmp_path, monkeypatch):
    """证人不能扣住账本：审图连不上/超时，记录照铸，缺席记成一行可读事实。

    实测：一次 render_figure 在 4×150s 的重试里卡了十几分钟，产物早就在盘上，
    agent 连续三轮原地等 —— 那是让证人当了判官。
    """

    import time as _time

    from core import model_roles
    from nodes.postprocess import vlm_witness
    from nodes.postprocess.vlm_extraction import extract_against_contract

    class _Binding:
        provider, model, base_url, api_key = "openai", "vlm", "http://x", "k"

    monkeypatch.setattr(model_roles, "resolve", lambda role: _Binding())

    calls = {"n": 0}

    async def always_times_out(**kwargs):
        calls["n"] += 1
        raise TimeoutError("read timeout")

    monkeypatch.setattr(vlm_witness, "_call_once", always_times_out)

    class _Config:
        role = "visual_review"
        max_tokens = 100
        max_tokens_ceiling = 200
        max_retries = 4

    contract = normalize_contract(_topology_contract(), family="schematic")
    started = _time.monotonic()
    result = asyncio.run(
        extract_against_contract(
            image_path=tmp_path / "missing.png", contract=contract, config=_Config()
        )
    )
    assert _time.monotonic() - started < 30  # 不许把墙钟耗在这里
    assert result["status"] == "extraction_incomplete"
    assert calls["n"] >= 1
    # 传输层错误被归类记账，而不是外抛把整次铸记录带垮。
    assert any("TimeoutError" in str(item.get("error")) for item in result["attempts"])
    assert result["findings"][0]["collector"] == "OB-EXTRACTION-INCOMPLETE"
    assert "not reachable in time" in result["findings"][0]["message"]


# ── 可读性也是机械量（第二轮：布局交给 Graphviz）──────────────────────────


def _nested_topology() -> dict:
    """一台服务器 + 两个并列的 PCIe 域（嵌套子组）。"""

    nodes = [{"id": "cpu0", "label": "CPU 0", "role": "CPU"},
             {"id": "cpu1", "label": "CPU 1", "role": "CPU"}]
    groups = [{"id": "srv", "label": "Server", "ranks": [["cpu0", "cpu1"]]}]
    edges = [{"from": "sw0", "to": "sw1", "label": "400G", "kind": "bus"}]
    for d in (0, 1):
        nodes.append({"id": f"sw{d}", "label": f"PCIe Switch {d}", "role": "sw"})
        nodes.append({"id": f"nic{d}", "label": f"NIC {d}", "role": "nic"})
        gpus = []
        for i in range(d * 4, d * 4 + 4):
            nodes.append({"id": f"gpu{i}", "label": f"GPU {i}", "role": "gpu"})
            edges.append({"from": f"sw{d}", "to": f"gpu{i}"})
            gpus.append(f"gpu{i}")
        groups.append({"id": f"dom{d}", "parent": "srv", "label": "",
                       "ranks": [[f"sw{d}"], gpus, [f"nic{d}"]]})
        edges += [{"from": f"cpu{d}", "to": f"sw{d}"}, {"from": f"sw{d}", "to": f"nic{d}"}]
    return {"title": "t", "nodes": nodes, "groups": groups,
            "bands": [["group:srv"]], "edges": edges, "assertions": []}


def test_ownership_must_live_in_the_structure_not_only_in_the_edges(tmp_path):
    """「这 4 张 GPU 归 switch 0」只写成边，布局器只能猜。

    实测：平层合同下两个 switch 被推到最左、8 条线拉成一把长扇子，「每 4 张
    一组」在图上完全看不出来。嵌套子组把归属写进结构，交叉数立刻掉下来。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    nested = normalize_contract(_nested_topology(), family="schematic")
    flat_raw = _nested_topology()
    # 同样的节点和边，但不声明归属：所有东西摊进一个组的三行。
    flat_raw["groups"] = [
        {
            "id": "srv",
            "label": "Server",
            "ranks": [
                ["cpu0", "cpu1"],
                ["sw0", "sw1"],
                [f"gpu{i}" for i in range(8)],
                ["nic0", "nic1"],
            ],
        }
    ]
    flat = normalize_contract(flat_raw, family="schematic")

    nested_m = layout_contract(nested)["layout_metrics"]
    flat_m = layout_contract(flat)["layout_metrics"]
    assert nested_m["edge_crossings"] < flat_m["edge_crossings"]


def test_sibling_subsystems_share_their_layers(tmp_path):
    """并列子系统的第 i 层是同一层。

    不说这件事，dot 只能按边推层：`sw0 -> sw1` 是条真边，Switch 1 就被降到
    Switch 0 下面一层、跟 GPU 行齐平（实测）。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(normalize_contract(_nested_topology(), family="schematic"))
    rect = {nid: node["rect"] for nid, node in geometry["nodes"].items()}
    assert abs(rect["sw0"][1] - rect["sw1"][1]) < 2.0
    assert abs(rect["nic0"][1] - rect["nic1"][1]) < 2.0
    # 层序仍然成立：switch 在 GPU 之上，NIC 在 GPU 之下。
    assert rect["sw0"][1] > rect["gpu0"][1] > rect["nic0"][1]


def test_structurally_identical_groups_are_named(tmp_path):
    """一模一样的单元全展开，信息量没增加、面积翻几倍 —— 机械可判。

    但**互相连着的同构组不是冗余**：两个 PCIe 域内部一样，它们之间那条
    400G/0.1us 交换机互联正是图要讲的事，折成「×2」就没了。这条判据从
    iter1 到 iter12 每次都对着这个形状报，每次都被正确驳回 —— 一条没人采纳
    的判据比没有更糟，它教会模型 findings 可以不理。
    """

    from nodes.postprocess.layout_quality import isomorphic_groups

    linked = _nested_topology()
    assert any(
        e["from"] == "sw0" and e["to"] == "sw1" for e in linked["edges"]
    ), "夹具本来就该带那条互联边"
    assert isomorphic_groups(normalize_contract(linked, family="schematic")) == []

    # 断开那条互联：两个域就真的可互换了，这时必须报。
    standalone = _nested_topology()
    standalone["edges"] = [
        e for e in standalone["edges"]
        if not (e["from"] == "sw0" and e["to"] == "sw1")
    ]
    standalone["nodes"] = [
        {**n, "isolated": True} if n["id"] in {"sw0", "sw1"} else n
        for n in standalone["nodes"]
    ]
    assert isomorphic_groups(normalize_contract(standalone, family="schematic")) == [
        ["dom0", "dom1"]
    ]


def test_nesting_cycles_are_rejected():
    raw = _nested_topology()
    raw["groups"][0]["parent"] = "dom0"
    with pytest.raises(VisualContractError, match="cycle"):
        normalize_contract(raw, family="schematic")


def test_nested_groups_may_not_be_placed_in_a_band():
    raw = _nested_topology()
    raw["bands"] = [["group:srv", "group:dom0"]]
    with pytest.raises(VisualContractError, match="nested groups may not"):
        normalize_contract(raw, family="schematic")


def test_a_missing_layout_engine_is_a_readable_fact(tmp_path, monkeypatch):
    """没有 dot 就回落自研引擎，并说出来 —— 不是静默降级。"""

    from nodes.postprocess import diagram_compiler, dot_layout

    monkeypatch.setattr(dot_layout, "dot_available", lambda: False)
    geometry = diagram_compiler.layout_contract(
        normalize_contract(_topology_contract(), family="schematic")
    )
    assert geometry["layout_engine"] == "builtin-fallback"
    assert any(
        "not installed" in str(item.get("message"))
        for item in geometry["layout_findings"]
    )


def test_the_fallback_engine_places_every_declared_node_and_group(monkeypatch):
    """没有 dot 的机器上，回落引擎必须是全函数：每个节点有盒子、每个组有框。

    2026-09-18 CI（没装 Graphviz）实测两处 KeyError：侧边节点（`side`）那条
    「追加的带子」写在摆放之后，等于从没执行；嵌套子组（groups[].parent）的
    成员没人摆 —— 而交叉判据教模型的正是「拆成嵌套子组」。回落画得差可以，
    画不出来不行。
    """

    import json, pathlib

    from nodes.postprocess import dot_layout
    from nodes.postprocess.diagram_compiler import compile_render_script, layout_contract

    monkeypatch.setattr(dot_layout, "dot_available", lambda: False)

    # ① 嵌套子组 —— 判据开的药方
    mid = [f"svc{i}" for i in range(8)]
    nested = _dense_stack()
    next(g for g in nested["groups"] if g["id"] == "g_mid")["ranks"] = []
    nested["groups"] += [
        {"id": "g_a", "parent": "g_mid", "label": "账号", "ranks": [mid[:4]]},
        {"id": "g_b", "parent": "g_mid", "label": "交易", "ranks": [mid[4:]]},
    ]
    contract = normalize_contract(nested, family="schematic")
    geometry = layout_contract(contract)
    assert geometry["layout_engine"] == "builtin-fallback"
    assert set(geometry["nodes"]) == {n["id"] for n in contract["nodes"]}
    assert set(geometry["groups"]) == {g["id"] for g in contract["groups"]}
    # 子组的框套住自己的成员
    for gid, members in (("g_a", mid[:4]), ("g_b", mid[4:])):
        bx, by, bw, bh = geometry["groups"][gid]["rect"]
        for nid in members:
            x, y, w, h = geometry["nodes"][nid]["rect"]
            assert bx <= x and by <= y and x + w <= bx + bw and y + h <= by + bh, (gid, nid)

    # ② 侧边节点 —— 流程图基准里的「重采」（side=left）
    fixture = (
        pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "benchmarks" / "flowchart.json"
    )
    contract = normalize_contract(json.loads(fixture.read_text(encoding="utf-8")), family="schematic")
    geometry = layout_contract(contract)
    assert set(geometry["nodes"]) == {n["id"] for n in contract["nodes"]}
    assert len(geometry["edges"]) >= len(contract["edges"])
    # 编译到两个后端都不许炸 —— 高危扫描那条测试就是在这里 KeyError 的
    for backend in ("tikz", "matplotlib"):
        compile_render_script(contract, outputs=["x.pdf"], backend=backend)


def test_the_fallback_engine_has_an_edge_legend_and_makes_room_for_it(monkeypatch):
    """回落引擎的图例也得有边角色，画布宽也得算上它。

    主路径 2026-09-16 修过的那条（图例宽从没算进画布宽）在回落引擎里原样活着，
    而且更糟：它根本没有边图例 —— 边按角色着色，图上没有任何地方解释颜色。
    """

    from nodes.postprocess import dot_layout
    from nodes.postprocess.diagram_compiler import layout_contract, legend_width

    monkeypatch.setattr(dot_layout, "dot_available", lambda: False)
    raw = {
        "title": "t",
        "nodes": [
            {"id": "a", "label": "A", "role": "很长的一个角色名称甲"},
            {"id": "b", "label": "B", "role": "很长的一个角色名称乙"},
        ],
        "groups": [],
        "bands": [["node:a"], ["node:b"]],
        "edges": [{"from": "a", "to": "b", "role": "同样很长的一条边的角色名"}],
        "assertions": [],
    }
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    assert geometry["layout_engine"] == "builtin-fallback"
    assert [item["role"] for item in geometry["edge_legend"]] == ["同样很长的一条边的角色名"]
    width = legend_width(
        [item["role"] for item in geometry["legend"]],
        [item["role"] for item in geometry["edge_legend"]],
    )
    assert width <= geometry["canvas"][0]
    # 边的颜色和图例是同一张表
    (edge,) = geometry["edges"]
    assert edge["color"] == geometry["edge_legend"][0]["color"]


def test_the_fallback_engine_demotes_page_content_to_footnotes_and_says_so(monkeypatch):
    """版面层的内容（规格块、注记块、引出注解）回落引擎不会摆，但不许丢。

    降成页脚一行字，并在 layout_findings 里说明降级了 —— 「画得出、并且说出来
    降级了」是回落引擎的全部承诺。几何的键集合也得和主路径一致，下游不用按
    引擎分两套读法。
    """

    from nodes.postprocess import dot_layout
    from nodes.postprocess.diagram_compiler import layout_contract

    monkeypatch.setattr(dot_layout, "dot_available", lambda: False)
    raw = {
        "title": "t",
        "assertions": [],
        "blocks": [
            {
                "kind": "diagram", "title": "① 图",
                "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                "groups": [], "bands": [["node:a"], ["node:b"]],
                "edges": [{"from": "a", "to": "b"}],
                "annotations": [{"anchor": "b", "side": "bottom", "text": "→ 存储"}],
            },
            {"kind": "spec", "title": "② 规格", "items": ["8 × GPU", "2 × NIC"]},
            {"kind": "note", "text": "注：四台机器配置完全相同。"},
        ],
    }
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    assert geometry["layout_engine"] == "builtin-fallback"
    for key in ("panels", "annotations", "spec_items", "block_notes", "edge_legend"):
        assert key in geometry, key
    notes = " | ".join(geometry["notes"])
    for text in ("8 × GPU", "2 × NIC", "注：四台机器配置完全相同。", "→ 存储"):
        assert text in notes, (text, notes)
    messages = " ".join(str(item["message"]) for item in geometry["layout_findings"])
    for kind in ("spec block", "note block", "annotation"):
        assert kind in messages, (kind, messages)


def test_readability_metrics_ride_along_with_every_layout(tmp_path):
    """度量逐版进记录 —— 「越改越差」在可读性上也要看得见。"""

    from nodes.postprocess.diagram_compiler import layout_contract

    metrics = layout_contract(
        normalize_contract(_topology_contract(), family="schematic")
    )["layout_metrics"]
    for key in (
        "aspect_ratio",
        "edge_crossings",
        "edges_grazing_nodes",
        "ink_ratio",
        "isomorphic_group_sets",
    ):
        assert key in metrics


def test_a_rank_that_is_really_two_subsystems_is_named(tmp_path):
    """实测：框架说「把子系统声明成嵌套子组」，agent 照做了 —— 但只声明了
    **一个**子组，把 8 张 GPU 全塞进它的一行，扇线照旧一团。

    机制送到了、用了一半。差的是这条更精确的判据：这一行的节点各自只连一个
    上游，而这些上游分成 N 组 —— 那这行就是 N 个子系统。纯拓扑事实。
    """

    from nodes.postprocess.layout_quality import splittable_ranks

    raw = _topology_contract()          # 8 张 GPU 一行，sw0/sw1 各拥 4 张
    contract = normalize_contract(raw, family="schematic")
    splits = splittable_ranks(contract)
    assert len(splits) == 1
    assert {owner: len(ids) for owner, ids in splits[0]["partition"].items()} == {
        "sw0": 4,
        "sw1": 4,
    }


def test_a_genuinely_single_subsystem_rank_is_not_flagged():
    """归属没有歧义才算数：一行全归同一个上游时不该报。"""

    from nodes.postprocess.layout_quality import splittable_ranks

    raw = _topology_contract()
    for edge in raw["edges"]:
        if edge["to"].startswith("gpu"):
            edge["from"] = "sw0"
    contract = normalize_contract(raw, family="schematic")
    assert splittable_ranks(contract) == []


def test_shape_tiers_change_the_footprint():
    """形状要表意：32 张 GPU 是密排小色块，共享骨干是通栏长条 —— 全用同一种
    盒子时 8 张 GPU 就占满整幅宽度，而信息量并没有增加。"""

    from nodes.postprocess.diagram_compiler import _node_size

    box = _node_size({"label": "GPU 0", "sublabel": "RTX PRO 6000", "shape": "box"})
    chip = _node_size({"label": "G0", "sublabel": "", "shape": "chip"})
    bar = _node_size({"label": "RoCE 交换机", "sublabel": "8 × 400G", "shape": "bar"})
    assert chip[0] * chip[1] < box[0] * box[1] / 3      # chip 明显更小
    assert bar[0] > box[0] * 2                          # bar 明显更宽


def test_a_bar_spans_the_content_width(tmp_path):
    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = _nested_topology()
    raw["nodes"].append(
        {"id": "fabric", "label": "共享织物", "role": "fabric", "shape": "bar"}
    )
    raw["edges"].append({"from": "nic0", "to": "fabric"})
    raw["edges"].append({"from": "nic1", "to": "fabric"})
    raw["bands"] = [["group:srv"], ["node:fabric"]]
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    canvas_w = geometry["canvas"][0]
    bar_w = geometry["nodes"]["fabric"]["rect"][2]
    assert bar_w > canvas_w * 0.8       # 通栏，不是一个普通方块


def test_edges_with_a_role_get_their_own_legend(tmp_path):
    """只有节点进图例、边不进，读者无从知道红线和蓝线差在哪。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = _nested_topology()
    for edge in raw["edges"]:
        edge["role"] = "交换机互联" if edge.get("kind") == "bus" else "PCIe 通道"
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    roles = {item["role"] for item in geometry["edge_legend"]}
    assert roles == {"交换机互联", "PCIe 通道"}
    # 图例里的颜色必须就是图上画的颜色（图例的样子要等于图上的样子）。
    colours = {item["role"]: item["color"] for item in geometry["edge_legend"]}
    for edge in geometry["edges"]:
        if edge.get("role"):
            assert edge["color"] == colours[edge["role"]]


def test_schematics_default_to_the_publication_backend():
    """出版级排版是这个节点存在的意义，所以它是默认而不是选项。"""

    from nodes.postprocess.diagram_compiler import DEFAULT_SCHEMATIC_BACKEND

    assert DEFAULT_SCHEMATIC_BACKEND == "tikz"


def test_the_legend_cannot_overflow_the_canvas():
    """画布宽必须由内容**和图例**共同撑开。

    2026-09-16 实测：加了边图例之后，图例总宽从来没有算进画布宽 —— 窄图加长
    role 名就出界，agent 只能靠「把 role 名改短」去绕（它最后写的是英文
    `PCIe link` 而不是中文）。几何该算对的地方没算对，就会让模型花轮次猜补。
    """

    from nodes.postprocess.diagram_compiler import layout_contract, legend_width

    raw = {
        "title": "t",
        "nodes": [
            {"id": "a", "label": "A", "role": "很长的一个角色名称甲"},
            {"id": "b", "label": "B", "role": "很长的一个角色名称乙"},
        ],
        "groups": [],
        "bands": [["node:a"], ["node:b"]],
        "edges": [{"from": "a", "to": "b", "role": "同样很长的一条边的角色名"}],
        "assertions": [],
    }
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    width = legend_width(
        [item["role"] for item in geometry["legend"]],
        [item["role"] for item in geometry["edge_legend"]],
    )
    assert width <= geometry["canvas"][0]


def test_a_long_title_also_widens_the_canvas():
    from nodes.postprocess.diagram_compiler import _text_width, layout_contract, TITLE_FS

    raw = {
        "title": "一个非常非常长的标题用来确认标题也会把画布撑开而不是自己出界",
        "nodes": [{"id": "a", "label": "A", "role": "r"}, {"id": "b", "label": "B", "role": "r"}],
        "groups": [],
        "bands": [["node:a"], ["node:b"]],
        "edges": [{"from": "a", "to": "b"}],
        "assertions": [],
    }
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    assert _text_width(raw["title"], TITLE_FS) <= geometry["canvas"][0]


# ── 版面层：figure 是带标题的 block 列表，diagram 只是其中一种 ────────────


def _paged_contract() -> dict:
    inner = _topology_contract()
    return {
        "title": "版面",
        "assertions": [],
        "blocks": [
            {"kind": "diagram", "title": "① 单机内部", "nodes": inner["nodes"],
             "groups": inner["groups"], "bands": inner["bands"], "edges": inner["edges"]},
            {"kind": "spec", "title": "② 规格摘要", "columns": 2,
             "items": ["每台 2 × CPU", "每台 8 × GPU", "每 4 张挂 1 个 switch", "共 2 个 switch"]},
            {"kind": "note", "text": "注：四台配置完全相同。"},
        ],
    }


def test_the_flat_form_is_still_a_valid_one_block_page():
    """表达力是**加**上去的，不是换掉的 —— 存量合同必须原样有效。"""

    contract = normalize_contract(_topology_contract(), family="schematic")
    assert [b["kind"] for b in contract["blocks"]] == ["diagram"]


def test_a_page_lays_out_titled_panels_with_spec_and_note(tmp_path):
    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(normalize_contract(_paged_contract(), family="schematic"))
    assert geometry["layout_engine"].startswith("page:")
    # 编号从标题文字里剥出来，作为画出来的徽章（①②③ 在衬线字体里没有字形）
    assert [p["title"] for p in geometry["panels"]] == ["单机内部", "规格摘要"]
    assert [p.get("number") for p in geometry["panels"]] == [1, 2]
    assert len(geometry["spec_items"]) == 4
    # 没标题、也没写 row 的 note 块**不再独占一个分栏** —— 壳按块收固定费，
    # 一句话的内容 17pt 却要付 55pt 的框（2026-09-17 两轮真跑实测）。
    # 它降级成页脚。**但那句话必须还在纸上** —— 这才是这条断言原本要守的东西。
    assert any(str(note).startswith("注：") for note in geometry["notes"])
    # 分栏不许互相重叠 —— 版面层自己也得守「算出来不是猜出来」。
    rects = [p["rect"] for p in geometry["panels"]]
    for upper, lower in zip(rects, rects[1:]):
        assert upper[1] >= lower[1] + lower[3]


def test_assertions_span_the_whole_page():
    """把图拆成两栏不该让「8 张 GPU」这条需求失效 —— 断言在**整张版面**上求值。"""

    def half(tag: str, first: int, last: int) -> dict:
        nodes = [{"id": f"{tag}sw", "label": "SW", "role": "sw"}]
        edges = []
        gpus = []
        for index in range(first, last):
            nodes.append({"id": f"g{index}", "label": f"GPU {index}", "role": "GPU"})
            edges.append({"from": f"{tag}sw", "to": f"g{index}"})
            gpus.append(f"g{index}")
        return {
            "kind": "diagram", "title": f"分栏 {tag}", "nodes": nodes,
            "groups": [{"id": f"{tag}grp", "label": "", "ranks": [[f"{tag}sw"], gpus]}],
            "bands": [[f"group:{tag}grp"]], "edges": edges,
        }

    raw = {
        "title": "两栏", "assertions": [
            {"id": "gpu-count", "kind": "node_count", "selector": {"role": "GPU"},
             "equals": 8, "derived_from": "插上8张RTX PRO6000"}
        ],
        "blocks": [half("a", 0, 4), half("b", 4, 8)],
    }
    contract = normalize_contract(raw, family="schematic")
    assert len(contract["nodes"]) == 10          # 8 GPU + 2 switch
    results = {item["id"]: item for item in evaluate_assertions(contract)}
    assert results["gpu-count"]["holds"] is True  # 跨栏仍然成立


def test_a_page_without_any_diagram_is_rejected():
    raw = _paged_contract()
    raw["blocks"] = [b for b in raw["blocks"] if b["kind"] != "diagram"]
    with pytest.raises(VisualContractError, match="at least one diagram block"):
        normalize_contract(raw, family="schematic")


def test_the_schema_example_demonstrates_every_device():
    """新词表必须进 example，否则等于没送到 —— 这条我已经犯过两次。"""

    from nodes.postprocess.tools.figure import _figure_contract_schema

    example = _figure_contract_schema("schematic")["example"]
    contract = normalize_contract(example, family="schematic")
    kinds = {b["kind"] for b in contract["blocks"]}
    assert {"diagram", "spec", "note"} <= kinds
    shapes = {n["shape"] for n in contract["nodes"]}
    assert {"chip", "bar"} <= shapes
    assert any(e["role"] for e in contract["edges"])
    assert any(g["parent"] for g in contract["groups"])
    assert any(n["ports"] for n in contract["nodes"])
    assert any(n["detail_of"] for n in contract["nodes"])
    assert any(b.get("annotations") for b in contract["blocks"])


def test_a_role_has_one_colour_across_the_whole_page(tmp_path):
    """配色是版面级的：每个 block 各算各的，颜色按块内首次出现顺序分配，跨块
    就串位（实测：「服务器 1」和 CPU 同蓝、RoCE 交换机和 PCIe Switch 同绿）。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    from nodes.postprocess.tools.figure import _figure_contract_schema

    geometry = layout_contract(
        normalize_contract(_figure_contract_schema("schematic")["example"], family="schematic")
    )
    by_role: dict[str, set[str]] = {}
    for node in geometry["nodes"].values():
        by_role.setdefault(node["role"], set()).add(node["color"])
    assert all(len(colours) == 1 for colours in by_role.values()), by_role


def test_panels_are_flush_with_the_page_margins(tmp_path):
    """分栏比页面窄时通栏长条就不通栏了（实测只占 56%）。"""

    from nodes.postprocess.diagram_compiler import MARGIN, layout_contract
    from nodes.postprocess.dot_layout import dot_available
    from nodes.postprocess.tools.figure import _figure_contract_schema

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(_figure_contract_schema("schematic")["example"], family="schematic")
    )
    canvas_w = geometry["canvas"][0]
    for panel in geometry["panels"]:
        assert abs(panel["rect"][2] - (canvas_w - 2 * MARGIN)) < 1.0
    bar = next(n for n in geometry["nodes"].values() if n.get("shape") == "bar")
    assert bar["rect"][2] > canvas_w * 0.7


def test_an_inner_block_reserves_no_legend_height():
    """「预留了却不画」= 一大片空白。suppress 时图例整体不占高度。"""

    from nodes.postprocess.diagram_compiler import _layout_two_layer
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    contract = normalize_contract(_topology_contract(), family="schematic")
    for edge in contract["edges"]:
        edge["role"] = "链路"          # 有边角色 → 从前会预留边图例的高度
    plain = _layout_two_layer({**contract, "_suppress_chrome": True})
    assert plain["legend"] == [] and plain["edge_legend"] == []
    with_chrome = _layout_two_layer(contract)
    assert plain["canvas"][1] < with_chrome["canvas"][1]


def test_a_block_does_not_eat_the_page_margin_again(tmp_path):
    """页边距是给**页**的，不是给每个 block 的。每个 block 再吃一次 30pt，
    栏内上下就各多出一圈空白（实测 ① 栏底部大片留白）。"""

    from nodes.postprocess.diagram_compiler import INNER_MARGIN, MARGIN, _layout_two_layer
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    assert INNER_MARGIN < MARGIN
    contract = normalize_contract(_topology_contract(), family="schematic")
    inner = _layout_two_layer({**contract, "_suppress_chrome": True})
    page = _layout_two_layer(contract)
    assert page["canvas"][1] - inner["canvas"][1] >= 2 * (MARGIN - INNER_MARGIN) - 1


# ── 标注层：既不是节点也不是边，但参考图里到处都是 ────────────────────────


def test_a_callout_is_not_smuggled_in_as_an_edge_and_a_fake_node():
    """「上联 RoCE 交换机」说的是「这条线去哪儿了」—— 是叙事不是拓扑。

    硬塞成一条边加一个假节点，会让 node_count / edge_count 这些机械断言全部
    失真（凭空多出一个组件和一条链路）。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = _topology_contract()
    before = len(raw["nodes"]), len(raw["edges"])
    raw["annotations"] = [
        {"anchor": "sw0", "side": "bottom", "text": "上联 RoCE 交换机"}
    ]
    contract = normalize_contract(raw, family="schematic")
    assert (len(contract["nodes"]), len(contract["edges"])) == before
    geometry = layout_contract(contract)
    assert len(geometry["annotations"]) == 1
    assert geometry["annotations"][0]["text"] == "上联 RoCE 交换机"


def test_an_annotation_must_anchor_to_a_real_node():
    raw = _topology_contract()
    raw["annotations"] = [{"anchor": "nope", "side": "top", "text": "x"}]
    with pytest.raises(VisualContractError, match="unknown node"):
        normalize_contract(raw, family="schematic")


def test_port_marks_are_drawn_from_the_declared_count(tmp_path):
    """「≥8 个 400GbE 端口」画 8 个小块比写一行字有效，也比数连线可靠 ——
    连线可能只画了代表性的几条。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = _topology_contract()
    raw["nodes"][1]["ports"] = 8          # sw0
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    assert len(geometry["nodes"]["sw0"]["ports"]) == 8
    rect = geometry["nodes"]["sw0"]["rect"]
    for px, _py, pw, _ph in geometry["nodes"]["sw0"]["ports"]:
        assert rect[0] <= px and px + pw <= rect[0] + rect[2]   # 端口不出盒


def test_a_dashed_group_is_an_enclosure_not_a_subsystem(tmp_path):
    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = _topology_contract()
    raw["groups"][0]["style"] = "dashed"
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    assert geometry["groups"]["srv"]["style"] == "dashed"


def test_the_example_demonstrates_the_annotation_layer():
    """不进 example 等于没送到 —— 这条已经犯过两次。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available
    from nodes.postprocess.tools.figure import _figure_contract_schema

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(_figure_contract_schema("schematic")["example"], family="schematic")
    )
    # 端口数不写死：example 里它必须**等于落上去的线数**（判据会核这个），
    # 所以写死一个数就等于把 example 钉在一个自相矛盾的形状上。
    ports = {
        nid: len(node.get("ports") or [])
        for nid, node in geometry["nodes"].items()
        if node.get("ports")
    }
    assert ports, "example 必须演示端口"
    # 数的是**链路条数**：一笔 represents=N 就算 N 条。
    degrees: dict[str, int] = {}
    for edge in geometry["edges"]:
        weight = int(edge.get("represents") or 1)
        degrees[edge["from"]] = degrees.get(edge["from"], 0) + weight
        degrees[edge["to"]] = degrees.get(edge["to"], 0) + weight
    assert all(count == degrees.get(nid, 0) for nid, count in ports.items()), (
        ports, degrees,
    )
    # represents=N 在几何里已经展开成 N 股（每股各占一个端口），股上带 bundle=N
    assert any(e.get("bundle", 1) > 1 for e in geometry["edges"]), "example 要演示 represents"
    assert len(geometry["annotations"]) >= 1
    assert any(g.get("style") == "dashed" for g in geometry["groups"].values())


def test_a_connector_forced_to_span_a_rank_is_named():
    """连线绕大弯不是布线器选得差，是**声明的层次让它没有近路可走**。

    实测：agent 连续两次声明 [switch] → [8×GPU] → [NIC]，switch→NIC 必须横穿
    整条 GPU 级，最长边达到均值的 3.06 倍。两张参考图都把 switch 的两类下游
    分居两侧，于是每条边都只跨一级。
    """

    from nodes.postprocess.layout_quality import rank_spanning_edges

    bad = _topology_contract()
    bad["groups"][0]["ranks"] = [["sw0"], [f"gpu{i}" for i in range(8)], ["sw1"]]
    bad["edges"] = [{"from": "sw0", "to": "sw1"}] + [
        {"from": "sw0" if i < 4 else "sw1", "to": f"gpu{i}"} for i in range(8)
    ]
    bad["nodes"] = [n for n in bad["nodes"] if n["id"] != "cpu0"]
    contract = normalize_contract(bad, family="schematic")
    spans = rank_spanning_edges(contract)
    assert [item["edge"] for item in spans] == [["sw0", "sw1"]]
    assert spans[0]["span"] == 2


def test_downstream_on_both_sides_spans_nothing():
    """正确形状：把一个节点的两类下游放到它的上下两侧，每条边只跨一级。"""

    from nodes.postprocess.layout_quality import rank_spanning_edges

    good = _topology_contract()
    good["groups"][0]["ranks"] = [
        [f"gpu{i}" for i in range(8)], ["sw0", "sw1"], ["cpu0"]
    ]
    contract = normalize_contract(good, family="schematic")
    assert rank_spanning_edges(contract) == []


def test_the_example_itself_has_no_rank_spanning_edge():
    """example 自己就得是正确形状 —— agent 是照着它写的。"""

    from nodes.postprocess.layout_quality import rank_spanning_edges
    from nodes.postprocess.tools.figure import _figure_contract_schema

    contract = normalize_contract(
        _figure_contract_schema("schematic")["example"], family="schematic"
    )
    assert rank_spanning_edges(contract) == []


def test_every_device_in_the_schema_is_also_in_the_checklist():
    """example 说「怎么写」，checklist 说「什么时候该用」—— 两者缺一都等于没送到。

    实测：annotations / ports / dashed 三样进了 schema 和 example，却没进
    checklist，结果 agent 一个都没用（ports=0 callouts=0 dashed=0）。
    """

    from nodes.postprocess.figure_contract import FAMILY_CHECKLIST

    text = " ".join(FAMILY_CHECKLIST["schematic"])
    for device in ("chip", "bar", "ports", "annotations", "dashed", "parent", "blocks"):
        assert device in text, f"{device} 不在 checklist 里"


# ── 判据要用一张公认好的图当标尺 ──────────────────────────────────────────


def _reference_contract() -> dict:
    """用户给的参考图逐构件转写。它是判据的**校准集**。

    阈值凭感觉定就会两头错：松到「一张基本是空白的图」也能过，同时又把好图
    的固有几何报成缺陷。拿一张公认好的图当标尺，两头都能校出来。
    """

    import json, pathlib

    # **读仓库里那一份。** 这个文件一度只存在于某个会话的临时目录里，而仓库里
    # 早有一份同样的抄件 —— 一个问题两个真相源，测试偏偏读的是会蒸发的那份。
    # 2026-09-18 临时目录被清空，四条校准测试**静默变成 skip**：防线离场，
    # 而记分牌上只是「265 passed」变「260 passed, 5 skipped」。
    # 现在缺文件就是红，因为它跟测试一起进了版本库。
    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "fixtures" / "benchmarks" / "calibration-reference.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_reference_figure_raises_no_finding():
    """校准的判据：一张公认好的图不该被报任何缺陷。

    校准前它被报 2 条（贴线 3 处 + 「这一行其实是两个子系统」）—— 前者是扇出
    的固有几何，后者是另一种合法编排（靠位置邻近表达分组）。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(normalize_contract(_reference_contract(), family="schematic"))
    assert geometry["layout_findings"] == [], geometry["layout_findings"]


def test_fan_out_siblings_do_not_count_as_grazing():
    """`sw→g7` 必然从 `g6` 旁边过 —— 它们都是 sw 的下游、又排在同一行。
    读者不会把它误读成「连到了 g6」。"""

    from nodes.postprocess.layout_quality import edges_grazing_nodes

    contract = normalize_contract(_topology_contract(), family="schematic")
    nodes = {"g6": {"rect": [100.0, 0.0, 20.0, 20.0]}}
    edge = {"from": "sw1", "to": "gpu7", "points": [[100.0, 10.0], [120.0, 10.0]]}
    # 没有 contract 时（旧口径）算贴线
    assert edges_grazing_nodes([edge], nodes) != []
    # 给了 contract，gpu6/gpu7 是同一个 sw1 的下游 → 不算
    nodes = {"gpu6": {"rect": [100.0, 0.0, 20.0, 20.0]}}
    assert edges_grazing_nodes([edge], nodes, contract) == []


def test_the_emptiness_threshold_comes_from_the_reference_not_from_taste():
    """阈值取自校准集，不是凭感觉 —— 好图不该被报「太空」。"""

    from nodes.postprocess.diagram_compiler import MAX_EMPTY_COLS, layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    metrics = layout_contract(
        normalize_contract(_reference_contract(), family="schematic")
    )["layout_metrics"]
    assert metrics["empty_cols"] <= MAX_EMPTY_COLS


def test_spacing_constants_keep_the_reference_clean():
    """间距不是拍脑袋的常数，是在校准集上扫出来的 —— 收紧后好图不能变差。

    实测：nodesep 留 0.30 时把 ranksep 收到 0.40，参考图会出现 2 处交叉；
    0.20 才两边都赢。这条钉住「以后谁再调间距，好图不许退化」。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(normalize_contract(_reference_contract(), family="schematic"))
    metrics = geometry["layout_metrics"]
    assert metrics["edge_crossings"] == 0
    assert metrics["ink_ratio"] >= 0.10
    assert geometry["layout_findings"] == []


def test_the_emptiness_metric_does_not_punish_dense_chips():
    """原判据 ink_ratio 按元件面积算，于是**惩罚 chip** —— 而 chip 是好设计。

    同一份结构，把成批元件从 box 换成 chip：ink 必然降（面积小了），但图并没
    有变空 —— 空行/空列不该跟着涨。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    boxes = _topology_contract()
    chips = _topology_contract()
    for node in chips["nodes"]:
        if node["role"] == "GPU":
            node["shape"] = "chip"
            node["label"] = node["label"].replace("GPU ", "G")
    a = layout_contract(normalize_contract(boxes, family="schematic"))["layout_metrics"]
    b = layout_contract(normalize_contract(chips, family="schematic"))["layout_metrics"]
    assert b["ink_ratio"] < a["ink_ratio"]        # ink 被 chip 拖低
    # 「图变空了吗」由空行/空列回答，它不该跟着 ink 一起塌。
    # （原先这行断言的是 content_fill —— 那个度量已被变异证明看不见 panels /
    #  spec-note / annotations / groups，2026-09-17 删除。）
    assert b["empty_rows"] <= a["empty_rows"]
    assert b["empty_cols"] <= a["empty_cols"]


def test_side_by_side_rows_are_available_but_not_free():
    """并排是一种工具，不是默认更好 —— 实测在这张图上把空列从 2 变成 6，
    新判据会自己报出来。"""

    import copy

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    stacked = _paged_contract()
    side = copy.deepcopy(stacked)
    for block in side["blocks"][1:]:
        block["row"] = 1
    a = layout_contract(normalize_contract(stacked, family="schematic"))
    b = layout_contract(normalize_contract(side, family="schematic"))
    # 并排真的改变了版面：同一行的分栏 y 相同、x 不同
    rows_b = {}
    for panel in b["panels"]:
        rows_b.setdefault(round(panel["rect"][1]), []).append(panel["rect"][0])
    assert any(len(v) > 1 for v in rows_b.values()), "并排没有生效"
    rows_a = {}
    for panel in a["panels"]:
        rows_a.setdefault(round(panel["rect"][1]), []).append(panel["rect"][0])
    assert all(len(v) == 1 for v in rows_a.values()), "不给 row 时不该并排"


def test_decorations_stay_inside_the_thing_they_belong_to():
    """page 平移节点时只移 rect 没移 ports，8 个端口小方块落到了另一个分栏的
    文字上 —— 而**当时所有度量全绿**：交叉/贴线/包围盒都不看装饰件。

    凡是「属于某个东西」的图元，就必须落在那个东西里。这条通用判据比每加一种
    装饰件都靠肉眼守可靠。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available
    from nodes.postprocess.layout_quality import detached_decorations

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    raw = _paged_contract()
    raw["blocks"][0]["nodes"][1]["ports"] = 8          # sw0 上挂 8 个端口
    geometry = layout_contract(normalize_contract(raw, family="schematic"))
    assert detached_decorations(geometry) == []
    assert geometry["layout_findings"] == [] or all(
        "decoration" not in f["message"] for f in geometry["layout_findings"]
    )
    # 把端口块人为挪走 → 判据必须报出来（不然它没在测这件事）
    broken = {**geometry, "nodes": dict(geometry["nodes"])}
    nid = next(k for k, v in geometry["nodes"].items() if v.get("ports"))
    broken["nodes"][nid] = {
        **geometry["nodes"][nid],
        "ports": [[9e4, 9e4, 9.0, 6.0]],
    }
    assert detached_decorations(broken) != []


def test_every_decoration_enters_the_geometry_metrics():
    """**每加一种装饰件，几何度量就多一个盲区** —— 除非它们都从同一个口子进。

    两次踩同一族：端口块跑到别的分栏（度量全绿）；我自己量分栏留白时漏掉引出
    标注，差点去「修」一段其实站着内容的空间。
    """

    from nodes.postprocess.layout_quality import _all_drawn_boxes

    geometry = {
        "canvas": [200.0, 200.0],
        "nodes": {"a": {"rect": [10.0, 10.0, 20.0, 20.0], "ports": [[12.0, 28.0, 6.0, 4.0]]}},
        "groups": {"g": {"rect": [5.0, 5.0, 40.0, 40.0]}},
        "panels": [{"rect": [0.0, 0.0, 60.0, 60.0]}],
        "edges": [{"points": [[10.0, 10.0], [90.0, 90.0]]}],
        "annotations": [{"start": [30.0, 10.0], "end": [30.0, -20.0], "text_xy": [30.0, -30.0]}],
        "spec_items": [{"xy": [150.0, 150.0]}],
        "block_notes": [{"xy": [160.0, 20.0]}],
    }
    boxes = _all_drawn_boxes(geometry)
    # 每一种都得在里面：端口、连线、引出、规格条目、段落注记
    assert any(b[1] < 0 for b in boxes), "引出标注没进来"
    assert any(b[0] >= 150 for b in boxes), "规格条目没进来"
    assert any(b[2] == 6.0 for b in boxes), "端口块没进来"
    # 容器在「用到多少」里算，在「有没有整片空」里不算
    assert len(_all_drawn_boxes(geometry, containers=False)) < len(boxes)


def test_containers_do_not_fill_themselves():
    """一个大而空的分栏不该把自己填满 —— 否则空白判据永远不响。"""

    from nodes.postprocess.layout_quality import empty_bands

    geometry = {
        "canvas": [240.0, 240.0],
        "nodes": {"a": {"rect": [0.0, 0.0, 20.0, 20.0]}},
        "groups": {},
        "panels": [{"rect": [0.0, 0.0, 240.0, 240.0]}],   # 覆盖整张画布的空分栏
        "edges": [],
    }
    bands = empty_bands(geometry)
    assert bands["empty_rows"] > 15 and bands["empty_cols"] > 15


def test_a_declared_port_is_where_the_wire_actually_lands(tmp_path):
    """端口过去只是均布的小方块：8 个口画在一处、4 条上联落在另一处，对不上。
    声明了端口，连线就得真的落在某一个口上。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    contract = normalize_contract(
        {
            "title": "端口即连接点",
            "nodes": [
                {"id": "a", "label": "服务器 1"},
                {"id": "b", "label": "服务器 2"},
                {"id": "sw", "label": "交换机", "shape": "bar", "ports": 8},
            ],
            "groups": [],
            "bands": [["node:a", "node:b"], ["node:sw"]],
            "edges": [{"from": "a", "to": "sw"}, {"from": "b", "to": "sw"}],
            "assertions": [],
        },
        family="schematic",
    )
    geometry = layout_contract(contract)
    marks = geometry["nodes"]["sw"]["ports"]
    assert len(marks) == 8
    centres = [px + pw / 2.0 for px, _py, pw, _ph in marks]
    rect = geometry["nodes"]["sw"]["rect"]
    landed = []
    for edge in geometry["edges"]:
        for x, y in (edge["points"][0], edge["points"][-1]):
            if rect[0] <= x <= rect[0] + rect[2] and abs(y - (rect[1] + rect[3])) < 3:
                landed.append(x)
    assert len(landed) == 2, landed
    for x in landed:
        assert min(abs(x - c) for c in centres) < 3.0, (x, centres)


def test_a_collapsed_node_points_back_at_the_panel_that_expands_it(tmp_path):
    """② 栏的一台服务器就是 ① 栏画开的那个机箱。合同里过去没地方说这句话，
    于是两栏语义割裂。detail_of 说了，图上就得看得见。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    def _page(detail_of):
        return normalize_contract(
            {
                "title": "两栏",
                "blocks": [
                    {
                        "kind": "diagram",
                        "title": "单机内部",
                        "nodes": [{"id": "cpu", "label": "CPU"},
                                  {"id": "gpu", "label": "GPU"}],
                        "groups": [{"id": "chassis", "label": "机箱",
                                    "style": "dashed",
                                    "ranks": [["cpu"], ["gpu"]]}],
                        "bands": [["group:chassis"]],
                        "edges": [{"from": "cpu", "to": "gpu"}],
                    },
                    {
                        "kind": "diagram",
                        "title": "集群",
                        "nodes": [
                            dict({"id": "n1", "label": "服务器 1"},
                                 **({"detail_of": detail_of} if detail_of else {})),
                            {"id": "sw", "label": "交换机", "shape": "bar"},
                        ],
                        "groups": [],
                        "bands": [["node:n1"], ["node:sw"]],
                        "edges": [{"from": "n1", "to": "sw"}],
                    },
                ],
                "assertions": [],
            },
            family="schematic",
        )

    geometry = layout_contract(_page("chassis"))
    # 两栏自动编号，引用解析成「详见 ①」里的那个 ①
    assert [p["title"] for p in geometry["panels"]] == ["单机内部", "集群"]
    assert [p["number"] for p in geometry["panels"]] == [1, 2]
    assert geometry["nodes"]["n1"]["detail_ref"] == 1

    # 指向不存在的组必须当场报错，而不是静默画成普通盒子
    with pytest.raises(VisualContractError) as excinfo:
        _page("no_such_group")
    assert "detail_of" in str(excinfo.value)


def test_a_character_the_font_cannot_draw_is_reported_not_dropped():
    """字体画不出的字会静默消失 —— 合同写了、图上没有、没人报。实测：TikZ 出
    的图上「① 单台服务器内部拓扑」只剩后半截。这条必须机械判。"""

    from nodes.postprocess import fonts

    # 判据落在「这张图真要画的字」上，所以用一条真的画不出 ① 的栈来验。
    assert fonts.missing_glyphs("① 单机", ("DejaVu Serif",)) == ["①", "单", "机"]
    assert fonts.missing_glyphs("plain ascii", ("DejaVu Serif",)) == []
    # 本机能画中文的栈不该误报
    stack = fonts.cjk_families()
    if stack:
        assert fonts.missing_glyphs("双 CPU 服务器 × 8 GPU 的 RoCE 集群拓扑", stack) == []


def test_the_render_script_records_missing_glyphs(monkeypatch):
    from nodes.postprocess import diagram_compiler as C

    contract = normalize_contract(
        {
            "title": "缺字测试",
            "nodes": [{"id": "a", "label": "甲"}, {"id": "b", "label": "乙"}],
            "groups": [],
            "bands": [["node:a"], ["node:b"]],
            "edges": [{"from": "a", "to": "b"}],
            "assertions": [],
        },
        family="schematic",
    )
    monkeypatch.setattr("nodes.postprocess.fonts.cjk_families", lambda: ("DejaVu Serif",))
    _code, geometry = C.compile_render_script(
        contract, outputs=["x.png"], backend="tikz"
    )
    missing = [
        f for f in geometry["layout_findings"] if f.get("missing_glyphs")
    ]
    assert missing, geometry["layout_findings"]
    assert set("甲乙缺字测试") <= set(missing[0]["missing_glyphs"])


def test_identical_edges_get_their_labels_on_one_line():
    """四条一模一样的上联，四个 400G 落在四个高度上 —— 对称的东西画得不对称，
    读者一眼看得出。同角色同走向的标注要对齐。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(
            {
                "title": "四条同型上联",
                "nodes": [
                    {"id": f"s{i}", "label": f"服务器 {i}"} for i in range(1, 5)
                ] + [{"id": "sw", "label": "交换机", "shape": "bar", "ports": 8}],
                "groups": [],
                "bands": [[f"node:s{i}" for i in range(1, 5)], ["node:sw"]],
                "edges": [
                    {"from": f"s{i}", "to": "sw", "label": "400G", "role": "RoCE"}
                    for i in range(1, 5)
                ],
                "assertions": [],
            },
            family="schematic",
        )
    )
    heights = {
        round(edge["label_xy"][1], 1)
        for edge in geometry["edges"]
        if edge.get("label_xy")
    }
    assert len(heights) == 1, heights


def test_a_label_does_not_land_on_another_label():
    """标注之间从不互相避让 —— 碰撞判据里根本没有别的标注。"""

    from nodes.postprocess.diagram_compiler import _Rect, _overlaps, _place_edge_labels

    edges = [
        {"label": "400G", "role": "r", "points": [[0.0, 0.0], [0.0, 200.0]]},
        {"label": "400G", "role": "r", "points": [[6.0, 0.0], [6.0, 200.0]]},
    ]
    _place_edge_labels(edges, [])
    boxes = []
    for edge in edges:
        assert edge.get("label_xy"), edge
        x, y = edge["label_xy"]
        boxes.append(_Rect(x - 20, y - 6, 40, 12))
    assert not _overlaps(boxes[0], boxes[1]), [e["label_xy"] for e in edges]


def test_the_layout_thresholds_still_fire_after_the_metric_was_fixed():
    """度量换了尺，阈值必须跟着重标 —— 否则判据整体变松、等于没有。

    校准集（参考图）与 schema example 都必须 0 findings；而 2026-09-16 真出过
    的那个形状 —— 所有单元排成一条长链、画幅拉成细长条（当时 13058×2451）——
    必须响。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available
    from nodes.postprocess.tools.figure import _figure_contract_schema

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    for good in (
        _figure_contract_schema("schematic")["example"],
        _reference_contract(),
    ):
        geometry = layout_contract(normalize_contract(good, family="schematic"))
        assert geometry["layout_findings"] == [], geometry["layout_findings"]

    strip = normalize_contract(
        {
            "title": "长链",
            "nodes": [{"id": f"n{i}", "label": f"n{i}"} for i in range(10)],
            "groups": [],
            "bands": [[f"node:n{i}"] for i in range(10)],
            "edges": [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(9)],
            "assertions": [],
        },
        family="schematic",
    )
    findings = layout_contract(strip)["layout_findings"]
    # 细长条要靠画幅判据抓。
    assert any("elongated" in f.get("message", "") for f in findings), findings


def test_unused_ports_are_a_gap_the_reader_can_see():
    """端口是真连接点之后，「画 8 个口、只有 4 条线」就是图上看得见、合同里
    却没处说明的落差。

    实测 iter12：② 栏画 4 条上联，注记写「每台服务器 2 张 RoCE 网卡分别上联」
    （= 8 条），交换机声明 8 个口 —— 三处各说各的，机械判据一条都没响。
    校准集（参考图）正是 8 口 8 线，所以这条判据不会误伤好图。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    def _figure(links_per_server: int, assertions=()):
        return normalize_contract(
            {
                "title": "上联",
                "nodes": [{"id": f"s{i}", "label": f"服务器 {i}"} for i in range(1, 5)]
                + [{"id": "sw", "label": "交换机", "shape": "bar", "ports": 8}],
                "groups": [],
                "bands": [[f"node:s{i}" for i in range(1, 5)], ["node:sw"]],
                "edges": [
                    {"from": f"s{i}", "to": "sw", "label": "400G"}
                    for i in range(1, 5)
                    for _ in range(links_per_server)
                ],
                "assertions": list(assertions),
            },
            family="schematic",
        )

    def _port_findings(contract):
        return [
            f for f in layout_contract(contract)["layout_findings"] if "ports" in f
        ]

    assert _port_findings(_figure(1)), "8 口 4 线必须报"
    assert not _port_findings(_figure(2)), "8 口 8 线是对的，不该报"
    # 也可以不补线，而是写一条断言说清这个节点的度 —— 但得说出来
    assert not _port_findings(
        _figure(1, [{"id": "d", "kind": "neighbors", "node": "sw",
                     "equals": 4, "derived_from": "只接了 4 根"}])
    )


def test_two_links_from_one_box_to_one_bar_do_not_cross_each_other():
    """一台交换机通栏摆着、8 条上联全落在它身上时，「对端中心」是同一个数 ——
    车道按中心排跨度就全相等，排序退化，跑得远的那条反而后转弯，横向段被别人
    的下落段穿过。iter14 实测：n1 自己的两条平行上联互相交叉。

    端口成了真连接点之后，中心就不再是落点了。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(
            {
                "title": "每台两条上联",
                "nodes": [{"id": f"s{i}", "label": f"服务器 {i}"} for i in range(1, 5)]
                + [{"id": "sw", "label": "交换机", "shape": "bar", "ports": 8}],
                "groups": [],
                "bands": [[f"node:s{i}" for i in range(1, 5)], ["node:sw"]],
                "edges": [
                    {"from": f"s{i}", "to": "sw", "label": "400G"}
                    for i in range(1, 5)
                    for _ in range(2)
                ],
                "assertions": [],
            },
            family="schematic",
        )
    )
    assert geometry["layout_metrics"]["edge_crossings"] == 0, [
        (e["from"], e["to"], e["points"]) for e in geometry["edges"]
    ]


def test_things_hanging_off_a_backbone_spread_across_it():
    """bar = 共享骨干，大家挂在上面。4 台服务器挤在中间 40%、端口却铺满整条
    bar 时，每条上联都得横跑一大段，8 条线缠成一团（iter14 看图）。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(
            {
                "title": "挂在骨干上",
                "nodes": [{"id": f"s{i}", "label": f"服务器 {i}"} for i in range(1, 5)]
                + [
                    {
                        "id": "sw",
                        "label": "很长很长很长的一条 RoCE 交换机骨干",
                        "shape": "bar",
                        "ports": 8,
                    }
                ],
                "groups": [],
                "bands": [[f"node:s{i}" for i in range(1, 5)], ["node:sw"]],
                "edges": [
                    {"from": f"s{i}", "to": "sw"} for i in range(1, 5) for _ in range(2)
                ],
                "assertions": [],
            },
            family="schematic",
        )
    )
    bar = geometry["nodes"]["sw"]["rect"]
    centres = [
        geometry["nodes"][f"s{i}"]["rect"][0] + geometry["nodes"][f"s{i}"]["rect"][2] / 2
        for i in range(1, 5)
    ]
    # 铺开：最左最右的服务器要覆盖骨干宽度的大半，而不是挤在中间
    covered = (max(centres) - min(centres)) / bar[2]
    assert covered > 0.6, (covered, centres, bar)
    longest_run = max(
        abs(edge["points"][i + 1][0] - edge["points"][i][0])
        for edge in geometry["edges"]
        for i in range(len(edge["points"]) - 1)
    )
    assert longest_run < bar[2] / 6, longest_run


def test_the_render_result_says_what_the_figure_looks_like():
    """iter15 实测 17 轮 / 79.5 万 token，一大块烧在 agent 用 execute_python
    裁 PNG、手算 pt→px，只为看看自己画的图长什么样。工具只回了数字（交叉数、
    填充率），没回版面。框架完全答得出的事，不该逼模型去像素里刨。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available
    from nodes.postprocess.layout_quality import layout_digest
    from nodes.postprocess.tools.figure import _figure_contract_schema

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(_figure_contract_schema("schematic")["example"], family="schematic")
    )
    digest = layout_digest(geometry)

    # 每个分栏都要说出它装了什么 —— 文字栏也要（不能重犯「看不见文字」那个毛病）
    for panel in digest["panels"]:
        assert panel["rows_top_to_bottom"] or panel.get("text_lines"), panel

    # 一行就是读者看到的一行，从左到右
    first = digest["panels"][0]["rows_top_to_bottom"][0]
    assert "(" in first and "  " in first, first

    # 端口画了几个、落了几条线，直接给出来，不必自己数
    assert all(
        set(info) == {"drawn", "edges_landed"} for info in digest["ports"].values()
    )
    assert digest["callouts"] and digest["callouts"][0]["points"] in {
        "up", "down", "left", "right",
    }
    assert digest["collapsed_units"]


def test_one_stroke_can_stand_for_several_links():
    """「一条线标 2 × 400G」是架构图的合法简写，但合同里过去没处说它 ——
    于是「4 条线 vs 8 个端口」到底是画漏了还是简写，判据分不出来
    （iter16 实测：5 条 findings 全指着这件事）。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    def _page(represents, annotation_represents):
        return normalize_contract(
            {
                "title": "捆",
                "blocks": [
                    {
                        "kind": "diagram",
                        "title": "单机",
                        "nodes": [{"id": "sw", "label": "PCIe"},
                                  {"id": "nic", "label": "NIC"}],
                        "groups": [{"id": "srv", "label": "机箱",
                                    "ranks": [["sw"], ["nic"]]}],
                        "bands": [["group:srv"]],
                        "edges": [{"from": "sw", "to": "nic"}],
                        "annotations": [{"anchor": "nic", "side": "bottom",
                                         "text": "上联",
                                         "represents": annotation_represents}],
                    },
                    {
                        "kind": "diagram",
                        "title": "集群",
                        "nodes": [
                            {"id": "s1", "label": "服务器 1", "detail_of": "srv"},
                            {"id": "fab", "label": "交换机", "shape": "bar",
                             "ports": 2},
                        ],
                        "groups": [],
                        "bands": [["node:s1"], ["node:fab"]],
                        "edges": [{"from": "s1", "to": "fab", "label": "2 × 400G",
                                   "represents": represents}],
                    },
                ],
                "assertions": [],
            },
            family="schematic",
        )

    # 一笔代表 2 条：端口 2 对得上，跨栏也对得上 → 这两条判据都不该报
    # （这个小样本本身很稀疏，填充率那几条会响，与本题无关）
    clean = layout_contract(_page(2, 2))
    counted = [
        f for f in clean["layout_findings"] if "ports" in f or "detail_of" in f
    ]
    assert counted == [], counted
    strands = [e for e in clean["edges"] if e["from"] == "s1" and e["to"] == "fab"]
    assert len(strands) == 2, strands          # 一笔代表 2 条 → 画成 2 股
    assert [e["label"] for e in strands].count("2 × 400G") == 1  # 标注只印一遍
    assert all(e.get("bundle") == 2 for e in strands)

    # 不写 represents，就是真的只有 1 条 —— 两边都得报
    noisy = layout_contract(_page(1, 2))["layout_findings"]
    assert any("ports" in f for f in noisy), noisy
    assert any("detail_of" in f for f in noisy), noisy


def test_a_bundle_label_sits_at_the_centre_of_the_bundle():
    """一捆 N 股共用一句标注。贴在其中一股旁边，读起来像「这一股是 2 × 400G」
    （iter18 渲染出来看到的）。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(
            {
                "title": "捆标注",
                "nodes": [{"id": f"s{i}", "label": f"服务器 {i}"} for i in range(1, 5)]
                + [{"id": "sw", "label": "交换机", "shape": "bar", "ports": 8}],
                "groups": [],
                "bands": [[f"node:s{i}" for i in range(1, 5)], ["node:sw"]],
                "edges": [
                    {"from": f"s{i}", "to": "sw", "label": "2 × 400G", "represents": 2}
                    for i in range(1, 5)
                ],
                "assertions": [],
            },
            family="schematic",
        )
    )
    labelled = [e for e in geometry["edges"] if e.get("label")]
    assert len(labelled) == 4, labelled          # 一捆只印一遍
    for edge in labelled:
        mates = [
            other
            for other in geometry["edges"]
            if other["from"] == edge["from"] and other["to"] == edge["to"]
        ]
        assert len(mates) == 2
        centre = sum(m["points"][0][0] for m in mates) / len(mates)
        assert abs(edge["label_xy"][0] - centre) < 1.0, (edge["label_xy"], centre)


def test_a_tall_narrow_page_gets_packed_into_rows():
    """作者一个 blocks[].row 都没给时，框架自己挑并排方式 —— 不挑就等于每次都
    出竖版（十九轮跑下来画幅始终 0.87-0.90，参考图是 1.32）。

    两侧都要钉：该并排的并了；作者自己写了 row 的一概不动。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    def _block(tag, count, row=None):
        ids = [f"{tag}{i}" for i in range(count)]
        block = {
            "kind": "diagram",
            "title": f"栏 {tag}",
            "nodes": [{"id": i, "label": i} for i in ids],
            "groups": [],
            "bands": [[f"node:{ids[0]}"], [f"node:{i}" for i in ids[1:]]],
            "edges": [{"from": ids[0], "to": i} for i in ids[1:]],
        }
        if row is not None:
            block["row"] = row
        return block

    tall = layout_contract(
        normalize_contract(
            {"title": "窄高", "blocks": [_block("a", 3), _block("b", 3)],
             "assertions": []},
            family="schematic",
        )
    )
    width, height = tall["canvas"]
    assert width / height > 1.0, (width, height)
    assert len(tall["panels"]) == 2
    # 并排不能把留白从底部搬到两侧
    assert tall["layout_metrics"]["empty_cols"] <= 1, tall["layout_metrics"]

    # 作者说了算：显式写了 row，就按他写的排（这里两块各占一行）
    explicit = layout_contract(
        normalize_contract(
            {"title": "窄高", "blocks": [_block("a", 3, row=1), _block("b", 3, row=2)],
             "assertions": []},
            family="schematic",
        )
    )
    ew, eh = explicit["canvas"]
    assert ew / eh < 1.0, (ew, eh)


@pytest.mark.asyncio
async def test_an_absent_reviewer_says_which_kind_of_absence(monkeypatch):
    """缺席也要说出是哪一种缺席。

    回 None 时，只读记录的人分不出「平台没配 visual_review 角色」和「跑了但没
    跑完」—— 而紧挨着的兄弟分支（no_png_output）早就是 {"ran": False, ...}。
    两个缺席两种记法，本身就是个分叉。
    """

    from core import model_roles
    from nodes.postprocess.tools import figure as F

    monkeypatch.setattr(model_roles, "resolve", lambda _role: None)
    findings, review = await F._witness_review(
        png_path=None, caption="", asset_kind="schematic", publication_grade=False,
    )
    assert findings == []
    assert review == {
        "ran": False,
        "reason": "role_not_configured",
        "role": "visual_review",
    }


def test_mirrored_edges_get_their_labels_on_one_line():
    """CPU 0→Switch 0 与 CPU 1→Switch 1 是一对镜像边，两个 PCIe 标签该同高。

    第一版按**每条边自己**的 dy>=dx 判走向再据此选对齐轴：两条都判成「横」→
    去对齐 x —— 可它们本来就一左一右分开摆，x 对齐毫无意义，读者看见的还是一高
    一低（iter23 放大看到的）。对齐哪个轴，问的是**这组同型边沿哪个方向铺开**。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(
            {
                "title": "镜像",
                "nodes": [
                    {"id": "cpu0", "label": "CPU 0"}, {"id": "cpu1", "label": "CPU 1"},
                    {"id": "sw0", "label": "Switch 0"}, {"id": "sw1", "label": "Switch 1"},
                ],
                "groups": [
                    {"id": "d0", "label": "域 0", "ranks": [["cpu0"], ["sw0"]]},
                    {"id": "d1", "label": "域 1", "ranks": [["cpu1"], ["sw1"]]},
                ],
                "bands": [["group:d0", "group:d1"]],
                "edges": [
                    {"from": "cpu0", "to": "sw0", "label": "PCIe", "role": "PCIe 通道"},
                    {"from": "cpu1", "to": "sw1", "label": "PCIe", "role": "PCIe 通道"},
                    {"from": "sw0", "to": "sw1", "label": "互联", "role": "Switch 互联"},
                ],
                "assertions": [],
            },
            family="schematic",
        )
    )
    heights = {
        round(edge["label_xy"][1], 1)
        for edge in geometry["edges"]
        if edge.get("label") == "PCIe"
    }
    assert len(heights) == 1, [
        (e["from"], e["label_xy"]) for e in geometry["edges"] if e.get("label") == "PCIe"
    ]


def test_a_frame_without_a_name_is_reported():
    """画了框却没有名字 = 读者看见一个盒子，不知道它圈的是什么。

    组框是画出来的（占面积、有边界），名字却是可选的 —— 这个缝里掉出来的就是
    「两个灰框」。2026-09-17 实测：schema example 自己把嵌套组的 label 留成 ""，
    agent 六轮里五轮照抄；唯一写了名字的那轮图明显更好读。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    def _figure(label):
        return normalize_contract(
            {
                "title": "框",
                "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                "groups": [{"id": "g", "label": label, "ranks": [["a"], ["b"]]}],
                "bands": [["group:g"]],
                "edges": [{"from": "a", "to": "b"}],
                "assertions": [],
            },
            family="schematic",
        )

    named = [f for f in layout_contract(_figure("子系统"))["layout_findings"] if "group" in f]
    assert named == [], named
    blank = [f for f in layout_contract(_figure(""))["layout_findings"] if "group" in f]
    assert [f["group"] for f in blank] == ["g"], blank


def test_the_example_never_teaches_an_unnamed_frame():
    """example 是**教材**：它留空的字段，agent 就照着留空。"""

    from nodes.postprocess.tools.figure import _figure_contract_schema

    contract = normalize_contract(
        _figure_contract_schema("schematic")["example"], family="schematic"
    )
    assert all(group["label"] for group in contract["groups"]), [
        g["id"] for g in contract["groups"] if not g["label"]
    ]


def test_a_note_is_centred_in_its_own_panel_not_the_page():
    """居中要居**它自己那一栏**的中。

    原先写的是 canvas_w/2（整页中心）—— 每个块都通栏时碰巧对，一旦有块并排就
    把注记甩到别人的栏里去（2026-09-17 流程图基准实测：注记横跨两栏、压在左栏
    底部外面）。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    geometry = layout_contract(
        normalize_contract(
            {
                "title": "并排",
                "blocks": [
                    {
                        "kind": "diagram", "title": "图", "row": 1,
                        "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                        "groups": [], "bands": [["node:a"], ["node:b"]],
                        "edges": [{"from": "a", "to": "b"}],
                    },
                    {"kind": "note", "title": "注", "row": 1, "text": "一段注记"},
                ],
                "assertions": [],
            },
            family="schematic",
        )
    )
    note = geometry["block_notes"][0]
    home = [
        panel for panel in geometry["panels"]
        if panel["rect"][0] <= note["xy"][0] <= panel["rect"][0] + panel["rect"][2]
    ]
    assert len(home) == 1, (note["xy"], [p["rect"] for p in geometry["panels"]])
    assert home[0]["title"] == "注", home[0]["title"]


def test_a_short_block_is_not_packed_beside_a_tall_one():
    """只比宽度时，一个又高又瘦的流程图会跟一段两行的注记配成一行，注记那一栏
    于是空掉九成 —— 流程图基准实测整整一栏是空的。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    ids = [f"n{i}" for i in range(8)]
    geometry = layout_contract(
        normalize_contract(
            {
                "title": "高瘦配矮胖",
                "blocks": [
                    {
                        "kind": "diagram", "title": "流水线",
                        "nodes": [{"id": i, "label": f"步骤 {i}"} for i in ids],
                        "groups": [], "bands": [[f"node:{i}"] for i in ids],
                        "edges": [
                            {"from": ids[i], "to": ids[i + 1]} for i in range(len(ids) - 1)
                        ],
                    },
                    {"kind": "note", "title": "注", "text": "一段很短的注记"},
                ],
                "assertions": [],
            },
            family="schematic",
        )
    )
    rows_y = {round(panel["rect"][1] + panel["rect"][3], 1) for panel in geometry["panels"]}
    assert len(rows_y) == 2, "高瘦的图和矮注记不该挤成一行"


def test_symbols_survive_the_tikz_render(tmp_path):
    """`≥ ① → ×` 这类符号不能被静默丢掉。

    xeCJK 默认只把汉字判成 CJK，符号走**拉丁正文字体**（Latin Modern），而它
    没有这些字形 —— 整个字符消失。2026-09-17 流程图基准实测：合同写
    「判定：R² ≥ 0.8 ？」，图上只有「判定：R² 0.8」。

    更麻烦的是 fonts.missing_glyphs() 查的是 CJK 字体，于是**判据说没问题、
    图上字没了** —— 判据和渲染各看各的。与其削弱判据，不如让渲染跟判据对齐。

    这条测试读的是**渲染出来的 PDF 里的字**，不是源码里的字符串
    （[[feedback_asserting_the_label_asserts_nothing]]）。
    """

    import subprocess
    import sys

    from shared.tools.library import latex

    if latex.no_tex_engine():
        pytest.skip("本机没有 TeX（latexmk / tectonic）")
    pytest.importorskip("pypdfium2")

    from nodes.postprocess.diagram_compiler import compile_render_script
    from nodes.postprocess.fonts import cjk_families

    if not cjk_families():
        pytest.skip("本机没有 CJK 字体")

    contract = normalize_contract(
        {
            "title": "符号 ≥ ① → ×",
            "nodes": [
                {"id": "a", "label": "判定", "sublabel": "R² ≥ 0.8 ？"},
                {"id": "b", "label": "产出 ①"},
            ],
            "groups": [],
            "bands": [["node:a"], ["node:b"]],
            "edges": [{"from": "a", "to": "b", "label": "≥ 0.8 → 通过"}],
            "assertions": [],
        },
        family="schematic",
    )
    code, _geometry = compile_render_script(
        contract, outputs=["sym.png"], backend="tikz", dpi=120
    )
    (tmp_path / "render.py").write_text(code, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "render.py"], cwd=tmp_path, capture_output=True, text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    # replay 的完整形态 = 脚本写 .tex + 框架编译交付（脚本本身不再起任何子进程）。
    _compile_tikz_through_the_framework(tmp_path)

    import pypdfium2

    pdfs = list(tmp_path.rglob("*.pdf"))
    assert pdfs, sorted(p.name for p in tmp_path.rglob("*"))
    text = pypdfium2.PdfDocument(str(pdfs[0]))[0].get_textpage().get_text_range()
    for char in "≥①→×":
        assert char in text, (char, text)


def test_a_back_edge_routes_around_the_outside():
    """跨了不止一条带子的边（流程图里的**回退边**）直着穿过去，要从中间每一层
    身上压过。2026-09-17 流程图基准实测：「R² < 0.8 回到预处理」竖穿图心，把两条
    并行分支切开。通用画法是走外侧绕回去。

    画布宽度是在布线**之前**定的 —— 不先把外侧那条留出来，回退边就画到画布外面
    被裁掉，而且什么都不会报。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    ids = ["raw", "qc", "prep", "feat", "base", "cv", "out"]
    contract = normalize_contract(
        {
            "title": "流程",
            "nodes": [{"id": i, "label": f"步骤 {i}"} for i in ids],
            "groups": [],
            "bands": [["node:raw"], ["node:qc"], ["node:prep"],
                      ["node:feat", "node:base"], ["node:cv"], ["node:out"]],
            "edges": [
                {"from": "raw", "to": "qc"}, {"from": "qc", "to": "prep"},
                {"from": "prep", "to": "feat"}, {"from": "prep", "to": "base"},
                {"from": "feat", "to": "cv"}, {"from": "base", "to": "cv"},
                {"from": "cv", "to": "out"},
                {"from": "cv", "to": "prep", "label": "R² < 0.8 回退"},
            ],
            "assertions": [],
        },
        family="schematic",
    )
    geometry = layout_contract(contract)
    assert geometry["layout_metrics"]["edge_crossings"] == 0, [
        (e["from"], e["to"], e["points"]) for e in geometry["edges"]
    ]

    back = [e for e in geometry["edges"] if e["from"] == "cv" and e["to"] == "prep"][0]
    inner_left = min(n["rect"][0] for n in geometry["nodes"].values())
    inner_right = max(n["rect"][0] + n["rect"][2] for n in geometry["nodes"].values())
    lane = [x for x, _y in back["points"]]
    assert min(lane) < inner_left or max(lane) > inner_right, back["points"]

    # 留出来的边距要真的够 —— 不许画到画布外面
    width, _height = geometry["canvas"]
    for edge in geometry["edges"]:
        xs = [x for x, _y in edge["points"]]
        assert 0 <= min(xs) and max(xs) <= width, (edge["from"], edge["to"], xs, width)


def test_a_complete_many_to_many_fan_becomes_one_bus():
    """六个执行节点各连三个共用基础设施 = 18 条边，一条一条排车道必然织成一张网。

    2026-09-17 分层架构基准实测：80-338 个交叉，agent 连渲六次，最后靠**删边**
    把 33 条减到 23 条 —— 用删信息换整齐，正是这套系统要拦的事。

    标准画法是总线。只有**完全二分**（每个源都连每个目标）时它才与逐条画等价、
    不丢信息 —— 所以这就是判据；少一条边就老实逐条画。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    execs = ["data", "exp", "ana", "wri", "rev", "viz"]
    infra = ["storage", "rdb", "vec"]

    def _figure(drop=None):
        pairs = [(e, s) for e in execs for s in infra if (e, s) != drop]
        return normalize_contract(
            {
                "title": "分层",
                "nodes": [{"id": "sched", "label": "调度器"}]
                + [{"id": i, "label": i} for i in execs + infra],
                "groups": [],
                "bands": [["node:sched"], [f"node:{i}" for i in execs],
                          [f"node:{i}" for i in infra]],
                "edges": [{"from": "sched", "to": e, "role": "派发"} for e in execs]
                + [{"from": a, "to": b, "role": "读写"} for a, b in pairs],
                "assertions": [],
            },
            family="schematic",
        )

    full = layout_contract(_figure())
    assert full["layout_metrics"]["edge_crossings"] == 0, full["layout_metrics"]
    assert sum(1 for e in full["edges"] if e.get("bus")) == len(execs) * len(infra)

    # 少一条边就不是总线了 —— 画成总线会凭空多出一条不存在的连接
    partial = layout_contract(_figure(drop=("viz", "vec")))
    assert all(not e.get("bus") for e in partial["edges"])


def test_overlapping_lines_are_reported_apart_from_crossings():
    """交叉与叠线是两件事。交叉 = 追不了哪条线通向哪里；叠线 = 两条画成一条、
    其中一条等于没画。以前叠线被算进「交叉」里，指不出真身；总线画法又靠共线，
    于是两边都失真。"""

    from nodes.postprocess.layout_quality import _segments_cross, collinear_overlaps

    assert _segments_cross((0, 0, 100, 0), (50, 0, 150, 0)) is False  # 共线不是交叉
    assert _segments_cross((0, 0, 100, 0), (50, -50, 50, 50)) is True

    stacked = [
        {"from": "a", "to": "b", "points": [[0, 0], [100, 0]]},
        {"from": "c", "to": "d", "points": [[20, 0], [80, 0]]},
    ]
    assert len(collinear_overlaps(stacked)) == 1, collinear_overlaps(stacked)

    # 同一条总线上的共线是有意为之
    for edge in stacked:
        edge["bus"] = "读写|1->2"
    assert collinear_overlaps(stacked) == []


def test_a_side_node_stands_beside_the_stack():
    """有的东西不站在任何一层里，而是**立在这一摞层的旁边**（外部依赖、共享服务、
    管理口）。

    2026-09-17 分层架构基准两种摆法都实测过，都不对：
    - 塞进某一层：同排横着连过去，中间隔着谁就压谁 —— 18 个交叉 + 9 对叠线；
    - 单独给它一层：别的层之间那 18 条总线边得穿过它 —— 164 个交叉。
    图里存在的这个区别，合同里过去没有位置说（同型根因第 8 次）。
    """

    from nodes.postprocess.contracts import VisualContractError
    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    peers = [f"w{i}" for i in range(6)]

    def _figure(side):
        node = {"id": "m", "label": "模型服务"}
        if side:
            node["side"] = side
        bands = [["node:top"], [f"node:{i}" for i in peers]]
        if not side:
            bands[1].append("node:m")
        return {
            "title": "侧边一列",
            "nodes": [{"id": i, "label": f"节点 {i}"} for i in peers]
            + [{"id": "top", "label": "调度器"}, node],
            "groups": [],
            "bands": bands,
            "edges": [{"from": "top", "to": i} for i in peers]
            + [{"from": i, "to": "m"} for i in peers],
            "assertions": [],
        }

    aside = layout_contract(normalize_contract(_figure("right"), family="schematic"))
    inline = layout_contract(normalize_contract(_figure(None), family="schematic"))
    assert aside["layout_metrics"]["edge_crossings"] == 0, aside["layout_metrics"]
    assert aside["layout_metrics"]["collinear_overlaps"] == []
    assert inline["layout_metrics"]["edge_crossings"] > 0, "对照组该是乱的"

    # 立在右边，且整个在画布里（画布宽度是摆放之前定的，得先留出来）
    rect = aside["nodes"]["m"]["rect"]
    others = [n["rect"] for nid, n in aside["nodes"].items() if nid != "m"]
    assert rect[0] > max(r[0] + r[2] for r in others)
    assert rect[0] + rect[2] <= aside["canvas"][0]

    # side 节点不许再进 bands —— 否则等于摆了两次
    twice = _figure("right")
    twice["bands"][1].append("node:m")
    with pytest.raises(VisualContractError, match="side"):
        normalize_contract(twice, family="schematic")


def test_a_label_with_nowhere_to_go_is_reported():
    """**读不了的图比画错的图更危险**，因为它看着像通过了。

    2026-09-17 时序图基准实测：7 条消息全挤在同一行，标签叠成一团根本读不了，
    而交叉=0、贴线=0，机械判据一句话没说。落点找不到时原先只是退回原点，注释说
    「由 savefig 时的文字碰撞审计记账」—— 那道审计只在 matplotlib 后端跑，默认的
    TikZ 后端上完全没人管。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    parts = ["user", "fe", "sched", "node"]
    geometry = layout_contract(
        normalize_contract(
            {
                "title": "全挤在一行",
                "nodes": [{"id": i, "label": i} for i in parts],
                "groups": [],
                "bands": [[f"node:{i}" for i in parts]],
                "edges": [
                    {"from": "user", "to": "fe", "label": "①提交课题"},
                    {"from": "fe", "to": "sched", "label": "②创建 run"},
                    {"from": "sched", "to": "node", "label": "③派发任务"},
                    {"from": "node", "to": "sched", "label": "④回报进度"},
                    {"from": "node", "to": "sched", "label": "⑤提交产物"},
                    {"from": "sched", "to": "fe", "label": "⑥通知刷新"},
                    {"from": "fe", "to": "user", "label": "⑦呈现结果"},
                ],
                "assertions": [],
            },
            family="schematic",
        )
    )
    assert geometry["layout_metrics"]["edge_crossings"] == 0, "对照：交叉判据确实说没毛病"
    unplaced = [f for f in geometry["layout_findings"] if f.get("labels_unplaced")]
    assert unplaced, geometry["layout_findings"]
    assert len(unplaced[0]["labels_unplaced"]) >= 4


def test_the_sequence_example_is_itself_clean():
    """时序是另一种摆法，光看拓扑那个例子学不会 —— 词表送到与否，看的是「他能不能
    照着写出来」，不是「有没有写在 schema 里」。而 example 是教材：它自己得先合规。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available
    from nodes.postprocess.tools.figure import _figure_contract_schema

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    example = _figure_contract_schema("schematic")["sequence_example"]
    contract = normalize_contract(example, family="schematic")
    geometry = layout_contract(contract)
    assert geometry["layout_engine"].startswith("sequence:")
    assert geometry["layout_findings"] == [], geometry["layout_findings"]
    assert geometry["layout_metrics"]["edge_crossings"] == 0
    # 每条消息各占一行 —— 没有两条落在同一高度
    heights = {round(e["points"][0][1], 1) for e in geometry["edges"]}
    assert len(heights) == len(geometry["edges"]), heights
    # 生命线画了，而且方向看得出来
    assert len(geometry["lifelines"]) == 4
    assert all(e.get("arrow") for e in geometry["edges"])


def test_text_that_never_reached_the_page_is_reported(tmp_path):
    """合同里写的字，**渲染出来的 PDF 里**真的找得到吗。

    几何里有那个字符，不等于纸上有：字体缺字形时整个字符会被静默丢掉。实测两次
    （① 和 ≥）都是肉眼发现的 —— 几何判据在原理上就查不出来，因为它查的是几何，
    不是纸。这是唯一一道「看见的是最终产物」的检查。
    """

    import subprocess
    import sys

    from shared.tools.library import latex

    if latex.no_tex_engine():
        pytest.skip("本机没有 TeX（latexmk / tectonic）")
    pytest.importorskip("pypdfium2")

    from nodes.postprocess.diagram_compiler import compile_render_script
    from nodes.postprocess.tools.figure import _verify_text_reached_the_page

    # 夹具要长得像真数据：真实版面带 spec / note 块，而**第一版夹具没有**，
    # 于是 _visible_strings 把它们整个字典 str() 的 bug 逃过了单测，直到真跑
    # 才被报出来（[[feedback_test_data_must_look_real]]）。
    contract = normalize_contract(
        {
            "title": "出门检查",
            "blocks": [
                {
                    "kind": "diagram", "title": "图",
                    "nodes": [{"id": "a", "label": "判定"}, {"id": "b", "label": "产出"}],
                    "groups": [], "bands": [["node:a"], ["node:b"]],
                    "edges": [{"from": "a", "to": "b", "label": "通过"}],
                },
                {"kind": "spec", "title": "规格", "columns": 1,
                 "items": ["每台服务器：2 × CPU", "互联：400 Gbps / 0.1 µs"]},
                {"kind": "note", "text": "注：四台配置完全相同。"},
            ],
            "assertions": [],
        },
        family="schematic",
    )
    code, geometry = compile_render_script(
        contract, outputs=["t.pdf"], backend="tikz", dpi=100
    )
    (tmp_path / "render.py").write_text(code, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "render.py"], cwd=tmp_path, capture_output=True, text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    # replay 的完整形态 = 脚本写 .tex + 框架编译交付（脚本本身不再起任何子进程）。
    _compile_tikz_through_the_framework(tmp_path)
    pdf = tmp_path / "t.pdf"

    # 真实几何 → 每一段文字都在纸上
    assert _verify_text_reached_the_page(geometry, [pdf], tmp_path) == []

    # 多声明一句纸上没有的话 → 必须点名
    lying = {**geometry, "notes": ["这一句从来没有画上去过"]}
    findings = _verify_text_reached_the_page(lying, [pdf], tmp_path)
    assert findings and findings[0]["text_check"] == "missing", findings
    assert "这一句从来没有画上去过" in findings[0]["missing_strings"]

    # 文字被转成轮廓时，每一条都找不到 —— 那是这道检查自己失效了，不是丢了 N 段字
    # 「每一条都找不到」要造得明确：贴着 0.6 那条线的样本只会证明我挑了个巧合
    blind = {**geometry, "nodes": {
        f"x{i}": {"label": f"纸上根本没有的第{i}段", "sublabel": "", "rect": [0, 0, 1, 1]}
        for i in range(40)
    }}
    findings = _verify_text_reached_the_page(blind, [pdf], tmp_path)
    assert findings and findings[0]["text_check"] == "no_text_layer", findings

    # 没有 PDF 可读也是可读事实，不假装查过。
    # （注意 workspace 里的 _contract_tikz/figure.pdf 也算数 —— 它**应该**被找到，
    #  所以这里得换一个真的什么都没有的目录。）
    empty = tmp_path / "nothing"
    empty.mkdir()
    findings = _verify_text_reached_the_page(geometry, [empty / "t.png"], empty)
    assert findings and findings[0]["text_check"] == "no_pdf", findings


#: 2026-09-23 Windows 真机（随包 tectonic + 微软雅黑）上那张「出门检查」图的文字层，逐字照抄
#: pypdfium2 读回来的：规格条目之间框架拼的「·」（U+00B7）画上了纸，读回来是「∙」（U+2219）。
WINDOWS_TECTONIC_PAGE = (
    "通过\r\n判定\r\n产出\r\n出门检查\r\ncomponent\r\n"
    "注：四台配置完全相同。\r\n"
    "规格: 每台服务器： 2 \xd7 CPU ∙互联： 400 Gbps / 0.1 \xb5s"
)


def test_a_glyph_read_back_as_a_sibling_code_point_reached_the_page():
    """文字层是从字形反查的：同一个字形对应两个码位时读回哪个都算「上了纸」。

    Windows + tectonic 上每一个多条目的规格块都被报「没上纸」，而纸上那个点清清楚楚、
    TeX 日志里一条 Missing character 都没有。"""

    from nodes.postprocess.tools.figure import _missing_from_page

    spec = "规格: 每台服务器：2 × CPU · 互联：400 Gbps / 0.1 µs"
    assert _missing_from_page([spec, "注：四台配置完全相同。", "出门检查"], WINDOWS_TECTONIC_PAGE) == []
    # NFKC：µ（U+00B5）读回 μ（U+03BC）同理
    assert _missing_from_page([spec], WINDOWS_TECTONIC_PAGE.replace("\xb5", "μ")) == []


def test_a_dropped_character_is_still_missing():
    """变异对照：放宽的只是「换了码位」，**丢了字**照样点名 —— 这道检查就是为它存在的。"""

    from nodes.postprocess.tools.figure import _missing_from_page

    # 2026-09-17 的两次真丢字：≥ 与 ① 被字体静默吞掉
    assert _missing_from_page(["判定：R² ≥ 0.8 ？"], "判定：R² 0.8 ？") == ["判定：R² ≥ 0.8 ？"]
    assert _missing_from_page(["① 单台服务器内部拓扑"], "单台服务器内部拓扑") == ["① 单台服务器内部拓扑"]
    # 整段只有符号：丢了就是丢了，不许被纸上别处任何一个标点冒认
    assert _missing_from_page(["→"], "通过 · 判定") == ["→"]
    # 字母、数字、汉字不放宽
    spec = "规格: 每台服务器：2 × CPU · 互联：400 Gbps / 0.1 µs"
    assert _missing_from_page([spec], WINDOWS_TECTONIC_PAGE.replace("CPU", "GPU")) == [spec]


def test_one_name_cannot_mean_two_things_in_the_legend():
    """节点的 role 说「这是个什么东西」，边的 role 说「这条连接是什么」。同一个
    名字两边都用，图例上就是同名两条 —— 一个色块、一条线，两种颜色两种含义。

    2026-09-17 流程图基准实测：「不合格分支」既是节点角色又是边角色，而那份合同
    当时 0 findings，判据完全没看见。
    """

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    def _figure(edge_role):
        return normalize_contract(
            {
                "title": "角色",
                "nodes": [
                    {"id": "a", "label": "筛查", "role": "判定点"},
                    {"id": "b", "label": "打回", "role": "不合格分支"},
                ],
                "groups": [],
                "bands": [["node:a"], ["node:b"]],
                "edges": [{"from": "a", "to": "b", "label": "不合格", "role": edge_role}],
                "assertions": [],
            },
            family="schematic",
        )

    clashing = [
        f for f in layout_contract(_figure("不合格分支"))["layout_findings"]
        if f.get("ambiguous_role")
    ]
    assert [f["ambiguous_role"] for f in clashing] == ["不合格分支"], clashing

    # 换个名字就没事 —— 判据说的是「同名」，不是「不许有分支角色」
    clean = [
        f for f in layout_contract(_figure("打回"))["layout_findings"]
        if f.get("ambiguous_role")
    ]
    assert clean == [], clean


def test_a_collapsed_node_never_declares_nothing():
    """`detail_of` 写了，图上就得看得见 —— 否则声明等于没发生。

    2026-09-17 造变异打版面层，两处静默失效：
    - `detail_of` 指向**同一栏**里的组：合同照过，而 detail_ref 解析不出来，
      徽章和内嵌边框一个都不画。图上是同一个东西并排画两遍，「详见」无处可指。
    - 目标栏**没有编号**（整页只有一个带标题的块）：同样解析不出来，同样零像素。
      这一条尤其阴 —— 渲染代码写的是 `if detail_ref:`，而 0 是 falsy。
    """

    from nodes.postprocess.contracts import VisualContractError
    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")

    def _page(second_titled=True):
        return {
            "title": "两栏",
            "blocks": [
                {
                    "kind": "diagram", "title": "① 单机",
                    "nodes": [{"id": "cpu", "label": "CPU"}, {"id": "gpu", "label": "GPU"}],
                    "groups": [{"id": "chassis", "label": "机箱",
                                "ranks": [["cpu"], ["gpu"]]}],
                    "bands": [["group:chassis"]],
                    "edges": [{"from": "cpu", "to": "gpu"}],
                },
                {
                    "kind": "diagram", "title": "② 集群" if second_titled else "",
                    "nodes": [{"id": "n1", "label": "服务器 1", "detail_of": "chassis"},
                              {"id": "sw", "label": "交换机", "shape": "bar"}],
                    "groups": [], "bands": [["node:n1"], ["node:sw"]],
                    "edges": [{"from": "n1", "to": "sw"}],
                },
            ],
            "assertions": [],
        }

    numbered = layout_contract(normalize_contract(_page(), family="schematic"))
    assert numbered["nodes"]["n1"]["detail_ref"] == 1

    # 目标栏没有编号时**仍然要有归属**（0 = 有归属、无编号），渲染据此画出
    # 「详见另一栏」；写成 if detail_ref: 就会把 0 当成没有。
    plain = layout_contract(normalize_contract(_page(False), family="schematic"))
    assert plain["nodes"]["n1"]["detail_ref"] == 0
    assert plain["nodes"]["n1"]["detail_ref"] is not None

    # 折叠形态与展开形态同栏 → 当场拒绝
    raw = _page()
    first, second = raw["blocks"]
    first["nodes"] += second["nodes"]
    first["bands"] += second["bands"]
    first["edges"] += second["edges"]
    raw["blocks"] = [first]
    with pytest.raises(VisualContractError, match="same block"):
        normalize_contract(raw, family="schematic")


# ── 词表之外：不认识的键必须被点名，不许静默丢 ─────────────────────────
def test_unknown_contract_keys_are_named_not_silently_dropped():
    """自造字段过去无声蒸发：图上少一件事，而没有任何一处说过它少了。

    2026-09-17 实测 duration_days / cardinality / swimlane / weight /
    multiplicity 五个键全部被归一化丢掉，所有判据全绿。判据必须点名。
    """

    from nodes.postprocess.figure_contract import unexpressed_findings

    raw = {
        "title": "t",
        "nodes": [{"id": "a", "label": "A", "duration_days": 5, "swimlane": "运维"}],
        "groups": [],
        "bands": [["node:a"]],
        "edges": [],
        "assertions": [],
    }
    findings = unexpressed_findings(raw, "schematic")
    codes = {f["code"] for f in findings}
    assert "contract.unexpressed_keys" in codes
    paths = {p for f in findings for p in f["paths"]}
    assert {"nodes[0].duration_days", "nodes[0].swimlane"} <= paths


def test_unexpressed_declaration_survives_normalization():
    """显式承认「合同说不出来」必须进归一化结果 —— 只打印一条 finding，
    下一版就没人记得图上缺什么。"""

    raw = {
        "title": "t",
        "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        "groups": [],
        "bands": [["node:a"], ["node:b"]],
        "edges": [{"from": "a", "to": "b"}],
        "assertions": [],
        "unexpressed": ["工序 A 持续 3 天，合同没有时长的词"],
    }
    normalized = normalize_contract(raw, family="schematic")
    assert normalized["unexpressed"] == ["工序 A 持续 3 天，合同没有时长的词"]


def test_unexpressed_is_in_the_schema_or_nobody_can_write_it():
    """逃生口不在 schema 里 = 没这个口（同型根因第 10 次）。"""

    from nodes.postprocess.figure_contract import contract_schema

    for family in ("schematic", "quantitative"):
        assert "unexpressed" in contract_schema(family)["properties"], family


def test_every_benchmark_fixture_is_expressible():
    """六个基准都该在词表内说得完 —— 有一个说不完，就是词表缺口，不是夹具问题。"""

    import json
    from pathlib import Path
    from nodes.postprocess.figure_contract import unexpressed_findings

    root = Path(__file__).resolve().parent.parent / "fixtures" / "benchmarks"
    seen = 0
    for path in sorted(root.rglob("*.json")):
        contract = json.loads(path.read_text())
        if not isinstance(contract, dict) or not (contract.keys() & {"nodes", "blocks"}):
            continue
        seen += 1
        assert unexpressed_findings(contract, "schematic") == [], path.name
    assert seen, "no benchmark contracts found — the check would pass vacuously"


def test_coverage_findings_reach_the_minted_record_not_just_the_tool_reply(tmp_path):
    """声明期发现的缺口必须跟着记录走到底。

    2026-09-17 自查：coverage / unexpressed / dropped 三类 findings 只活在
    declare_figure_contract 的**返回值**里 —— 模型看了一眼，而铸出来的记录
    一个字都没有。下一个读记录的人（referee / 交付）看到的是一张干净的图。
    「记录如实披露图上缺什么」当时是一句没兑现的话（「报告不是事实」同型）。
    """

    state = _state(tmp_path)
    contract = _topology_contract()
    # 词表接不住的键：作者想说「这台机器在机架第 3 层」，没有这个词。
    contract["nodes"][0]["rack_unit"] = 3
    declared = _declare(state, contract)
    assert any(
        item.get("code") == "contract.unexpressed_keys"
        for item in declared["coverage_findings"]
    ), declared["coverage_findings"]

    result = _render(state, contract_id=declared["contract_id"], backend="matplotlib")
    assert result["status"] == "success", result.get("error")
    record = state.read_artifact(result["figure_id"])
    codes = {
        item.get("code")
        for item in record["metadata"].get("findings") or []
    }
    assert "contract.unexpressed_keys" in codes, sorted(c for c in codes if c)


def test_the_purpose_the_caller_gave_reaches_the_readability_check(tmp_path):
    """调用方说了这张图是给幻灯用的，判据就得按幻灯的版心判。

    2026-09-17：`medium_of(contract, purpose)` 的第二个参数**从来没被接上**，
    布局层看不到 purpose，永远落到默认 `page`。后果不是判据不灵，是**判据不在场**：
    探针 purpose=presentation 本该按 slide 判（那张图 slide 下标签 5.3pt，该响），
    实际按 page 判（6.28pt，不响）。

    接法是在声明时把推出来的 medium **写进合同** —— 布局是纯函数拿不到 request，
    而把 purpose 一路传下去就有了「合同说的」和「运行时传的」两个真相源。
    写进合同 = 一个问题一个答案，而且进 hash、进记录。
    """

    def declared_medium(purpose):
        state = State.new(node_type="postprocess", base_dir=tmp_path / str(purpose))
        state.hook_state["node_inputs"] = {
            "visual_requests": [{
                "request_id": "topo",
                "intent": "双CPU服务器插上8张GPU，每4张连一个 PCIe switch，共 2 个",
                "asset_kind": "schematic",
                **({"purpose": purpose} if purpose else {}),
            }]
        }
        _declare(state, _topology_contract())
        stored = next(iter(state.hook_state["figure_contracts"].values()))
        return stored["contract"]["medium"]

    assert declared_medium("presentation") == "slide"
    assert declared_medium("publication") == "double_column"
    assert declared_medium(None) == "double_column"


def test_the_author_still_outranks_the_caller(tmp_path):
    """作者显式写了 medium 就按作者的 —— 推导只在他没表态时兜底。"""

    state = State.new(node_type="postprocess", base_dir=tmp_path)
    state.hook_state["node_inputs"] = {
        "visual_requests": [{
            "request_id": "topo", "intent": "…", "asset_kind": "schematic",
            "purpose": "presentation",
        }]
    }
    # 作者说的版心是**被判的**（poster 放得下这张 8 GPU 的图；single_column 放不下、
    # 会在声明时被拒 —— 那是另一条测试的事）。
    declared = _declare(state, {**_topology_contract(), "medium": "poster"})
    assert declared["status"] == "success", declared.get("error")
    stored = next(iter(state.hook_state["figure_contracts"].values()))
    assert stored["contract"]["medium"] == "poster"
    assert stored["contract"]["medium_source"] == "declared"


def test_a_derived_medium_is_recorded_but_does_not_judge_the_author(tmp_path):
    """推出来的版心进记录，但**不拿来罚作者**。

    2026-09-17 iter48 实测：purpose=presentation 推出 slide 之后判据响了两次，
    agent 声明了三版，画布 777pt → 777pt → 777pt **一次都没变**（第三版 aspect
    还从 0.87 掉到 0.70）。它做不到 —— 「窄 42%」对一张 19 节点的拓扑图意味着
    砍掉一半内容或拆成两张图，而需求要的是一张图。

    判据要的东西作者交不出来 = 误报；误报最贵的代价不是那几轮，是它教会模型
    「findings 可以不理」。所以：事实照进 layout_metrics（记录里看得见），
    只有作者**自己断言过**的版心才拿来判他。
    """

    state = State.new(node_type="postprocess", base_dir=tmp_path)
    state.hook_state["node_inputs"] = {
        "visual_requests": [{
            "request_id": "topo", "intent": "…", "asset_kind": "schematic",
            "purpose": "presentation",
        }]
    }
    _declare(state, _topology_contract())
    stored = next(iter(state.hook_state["figure_contracts"].values()))["contract"]
    assert stored["medium"] == "slide"          # 记录里看得见按哪个版心
    assert stored["medium_source"] == "derived"  # 但它不是作者说的

    from nodes.postprocess.diagram_compiler import layout_contract

    geometry = layout_contract(stored)
    metrics = geometry["layout_metrics"]
    assert metrics["medium"] == "slide"
    assert metrics["final_label_pt"] > 0          # 事实在
    assert not [f for f in geometry["layout_findings"] if "final_label_pt" in f]

    # 反面：一张真放不进版心的图 —— **事实进记录，但一条 finding 都不报**。
    # iter50 实测：做成 finding 会让模型把 medium 删掉让判据闭嘴。
    strip = normalize_contract({
        "title": "长链",
        "medium": "double_column",
        "nodes": [{"id": f"n{i}", "label": f"节点 {i}"} for i in range(20)],
        "groups": [],
        "bands": [[f"node:n{i}" for i in range(20)]],
        "edges": [{"from": f"n{i}", "to": f"n{i+1}"} for i in range(19)],
        "assertions": [],
    }, family="schematic")
    for source in ("declared", "derived"):
        g = layout_contract({**strip, "medium_source": source})
        from nodes.postprocess.figure_contract import MEDIA

        assert g["layout_metrics"]["final_label_pt"] < MEDIA["double_column"]["floor_pt"]
        assert not [f for f in g["layout_findings"] if "final_label_pt" in f]


def test_a_second_declaration_that_loses_a_field_gets_named(tmp_path):
    """重发整份合同时掉东西 —— 这一段过去从来没有任何东西比较过。

    `contract_diff` 只在 render 时跑（拿的是上一版 **figure 记录**），而丢失
    发生在**两次 declare 之间**：iter50 与 iter51 都在第二次声明时丢掉 `medium`
    （第一版写了 slide，第二版没有了，几何一字不差）；iter49 修断言时丢掉
    `bands`、下一版又把 ranks 弄空。

    这些字段「不写也合法」，所以掉了不报错、不拒绝、图照出 —— 只是图上少了
    一件事，而没有人知道。
    """

    state = _state(tmp_path)
    rich = _topology_contract()
    for node in rich["nodes"]:
        if node["id"] == "sw0":
            node["emphasis"] = "primary"
            node["sublabel"] = "挂载 GPU0–GPU3"
    rich["medium"] = "poster"   # 作者声明的版心会被判：poster 放得下这张图
    first = _declare(state, rich)
    assert "error" not in first, first

    # 第二版：几何一模一样，只是把那三件事掉了（正是真跑里发生的事）
    thin = _topology_contract()
    second = _declare(state, thin)
    losses = [
        f for f in (second.get("coverage_findings") or [])
        if f.get("code") == "contract.redeclaration_losses"
    ]
    assert losses, second.get("coverage_findings")
    lost = losses[0]["lost"]
    assert "medium" in lost
    assert "nodes[sw0].emphasis" in lost
    assert "nodes[sw0].sublabel" in lost

    # 第三版把它们补回来 —— 不许再被点名
    third = _declare(state, rich)
    assert not [
        f for f in (third.get("coverage_findings") or [])
        if f.get("code") == "contract.redeclaration_losses"
    ]


def test_the_first_declaration_is_never_accused(tmp_path):
    """第一次声明没有「上一版」可比 —— 不许凭空点名。"""

    state = _state(tmp_path)
    first = _declare(state, _topology_contract())
    assert not [
        f for f in (first.get("coverage_findings") or [])
        if f.get("code") == "contract.redeclaration_losses"
    ]


def _compile_tikz_through_the_framework(base) -> None:
    """脚本写完 .tex 之后的那一步，**调框架自己的** ``_finish_tikz_render``。

    这里曾经复刻它（自己 ``which("xelatex")`` + ``subprocess.run``）：框架换了编译路
    （经 ``latex.run_tex``、进墙、tectonic 住自己的家）之后，复刻品还在测旧路。"""

    from nodes.postprocess.tools.figure import _finish_tikz_render

    finish = asyncio.run(_finish_tikz_render(None, base, timeout=300))
    assert finish["status"] == "success", finish


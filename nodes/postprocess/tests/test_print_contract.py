"""图合同按印刷版心出图（2026-09-18 iter11，yuankk 论文一稿的图）。

夹具 research_runs/astra-sim-yuankk-20260918 iter11 的四个 postprocess 子 run：
61 次 declare、61 次 render 出 5 张图，其中 33 次是手写 code。三条根因，三组测试：

A. 编译渲染不按印刷尺寸出图：六拓扑总览 2×3 竖排、170mm 下副标题 5.6pt；合同里
   有 medium，编译器只在 layout.digest 里事后报告 → 模型对着报告重渲 7 次。
   → 版心决定画布预算：声明那一刻算印刷字号，不够就拒绝并给算过的改法；多面板
     按版心选并排（一行最多 3 栏）。
B. 数据图没有设计合同：8 面板、6 根柱无图例、刻度 4pt、y 轴无单位、默认橙蓝；
   合同只核「对象模型自洽」。
   → panels ≤ 4、轴写 label+unit、≥2 序列必有图例且在画布内、印刷字号、调色板；
     声明时机械核，渲染后从对象模型对账。
C. 家族/版心/语言不受调用方约束：同一 request_id 从 schematic 改标 composite 走 code。
   → asset_kind / constraints.width / text_language 绑定 request_id；同一请求最多渲染
     3 次，到上限返回「画不出合同要求的样子」与原因，让调用方决定。

夹具 fixtures/iter11_six_topologies.json 是那次子 run 里模型最后一次声明的合同原样
（只去掉了旧版心名）。写作侧的尺子（H9：pdftotext -bbox，455pt 版心，中位 ≥7 / p10 ≥6）
在 test_the_overview_reads_at_the_writing_side_s_ruler 里照抄。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute
from nodes.postprocess.figure_contract import DATA_PALETTE, MAX_PANELS, MEDIA

bootstrap()

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SIX_TOPOLOGIES = json.loads((FIXTURES / "iter11_six_topologies.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _fake_sandbox(monkeypatch):
    """subprocess 替身（与 test_figure_contract 同一份）：跑真代码、不要求容器沙箱。"""

    from shared.tools.library import python_exec

    async def fake_execute_python(state, code, timeout=300, cwd=None, requirements=None, **_):
        from core.project_workspace import validate_tool_cwd

        workspace = validate_tool_cwd(state, cwd)
        workspace.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=workspace, capture_output=True, text=True, timeout=timeout
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


def _tex_and_poppler() -> bool:
    from shared.tools.library import latex

    return not latex.no_tex_engine() and all(
        shutil.which(tool) for tool in ("pdftotext", "pdfinfo", "dot"))


# ── 写作侧发出的那 5 条请求（iter11 子 run 1789700945 的输入，政策项照 w_figures 的形状）──

POLICY = {"text_language": "en", "min_font_pt": 7, "width": "double_column", "formats": ["pdf", "png"]}
OVERVIEW_ID = "gpu-3-2-1-6-7pt-4.17pt"
PANORAMA_IDS = [
    "deepseek-v4-flash-wall-time-tp-ep-1-6-6-gpu-7pt-5.2pt",
    "kimi-k3-wall-time-tp-ep-1-6-6-7pt",
    "glm5.3-wall-time-tp-ep-1-6-6-7pt",
    "deepseek-v4-pro-wall-time-tp-ep-1-6-6-7pt",
]


def _iter11_requests() -> list[dict]:
    return [
        {
            "request_id": OVERVIEW_ID, "asset_kind": "schematic", "purpose": "publication",
            "intent": "六种 GPU 服务器拓扑总览示意图：3×2 网格，每子图极简展示拓扑1-6 的机内结构",
            "constraints": dict(POLICY),
        },
        *[
            {
                "request_id": rid, "asset_kind": "quantitative", "purpose": "publication",
                "intent": f"{rid} 全部配置组合 wall time 全景堆叠柱状图：横轴 TP/EP 策略，每组内按拓扑1-6 排 6 根堆叠柱",
                "constraints": dict(POLICY),
            }
            for rid in PANORAMA_IDS
        ],
    ]


def _state(tmp_path, requests=None) -> State:
    state = State.new(node_type="postprocess", base_dir=tmp_path)
    state.hook_state["node_inputs"] = {"visual_requests": requests if requests is not None else _iter11_requests()}
    return state


def _declare(state, contract, *, request_id=OVERVIEW_ID, asset_kind="schematic"):
    return asyncio.run(
        execute("declare_figure_contract", state, request_id=request_id, asset_kind=asset_kind, contract=contract)
    )


def _render(state, **overrides):
    kwargs = {
        "output_name": "fig", "caption": "caption", "alt_text": "alt", "output_files": ["fig.png"],
        "source_artifact_ids": [], "asset_kind": "schematic", "request_id": OVERVIEW_ID,
    }
    kwargs.update(overrides)
    return asyncio.run(execute("render_figure", state, **kwargs))


def _three_per_row(contract: dict) -> dict:
    out = copy.deepcopy(contract)
    out["bands"] = [["group:g1", "group:g2", "group:g3"], ["group:g4", "group:g5", "group:g6"]]
    return out


def _h9(pdf: Path, width_pt: float = 455.0, height_pt: float = 560.0) -> dict:
    """写作侧 w_render.py 的 H9：按 includegraphics 的宽（与 0.8\\textheight 上限）缩放，
    取 pdftotext -bbox 的文字高度分布。"""

    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30).stdout
    m = re.search(r"Page size:\s+([\d.]+) x ([\d.]+)", info)
    w, h = float(m.group(1)), float(m.group(2))
    bbox = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True, timeout=60).stdout
    hs = sorted(
        float(b) - float(a)
        for a, b in re.findall(r'yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)"', bbox)
        if float(b) - float(a) > 0.5
    )
    words = re.findall(r"<word[^>]*>([^<]*)</word>", bbox)
    scale = min(width_pt / w, height_pt / h)
    return {
        "median": hs[len(hs) // 2] * scale, "p10": hs[len(hs) // 10] * scale,
        "cjk": sum(1 for word in words if re.search(r"[一-鿿]", word)), "words": len(words),
        "canvas": (w, h),
    }


# ══ A. 编译渲染按印刷尺寸出图 ══════════════════════════════════════════════


def test_the_iter11_arrangement_is_rejected_at_declaration_with_a_measured_remedy(tmp_path):
    """模型在 iter11 声明的就是这份合同（2 组一行、3 行）：170mm 下被高度压到 0.72、
    副标题 5.75pt。过去它收到 success 然后渲了 7 次；现在声明这一刻就拒，拒绝信里是
    **算过的**改法：3 组一行 → 7.0pt。"""

    if not _tex_and_poppler():
        pytest.skip("TeX (latexmk / tectonic) or dot not on PATH")
    state = _state(tmp_path)
    result = _declare(state, SIX_TOPOLOGIES)
    assert result["status"] == "error", result
    text = result["error"]
    assert "does not print legibly at double_column" in text
    assert "bound by height" in text
    assert "3 per row" in text and "(fits)" in text, text
    assert "['group:g1', 'group:g2', 'group:g3']" in text
    # 拒绝不烧渲染预算
    assert state.hook_state.get("figure_render_attempts") in (None, {})


def test_three_per_row_is_accepted_and_the_record_carries_the_print_facts(tmp_path):
    if not _tex_and_poppler():
        pytest.skip("TeX (latexmk / tectonic) or dot not on PATH")
    state = _state(tmp_path)
    result = _declare(state, _three_per_row(SIX_TOPOLOGIES))
    assert result["status"] == "success", result.get("error")
    fit = result["print_fit"]
    assert fit["medium"] == "double_column" and fit["fits"] and fit["enforced"]
    assert fit["final_smallest_pt"] >= fit["floor_pt"] == 7.0
    assert fit["smallest_tier"] == "sub"          # 读者要读的 ×8 / ×16 / ×32
    assert result["bound"] == {
        "asset_kind": "schematic", "medium": "double_column", "medium_source": "caller",
        "text_language": "en", "font_floor_pt": 7.0,
    }
    assert result["renders_left"] == 3


def test_the_overview_reads_at_the_writing_side_s_ruler(tmp_path):
    """验收：六拓扑总览在 170mm 宽下 p10 ≥ 6pt、中位 ≥ 7pt、全英文、渲染 1 次。"""

    if not _tex_and_poppler():
        pytest.skip("TeX (latexmk / tectonic), dot or poppler not on PATH")
    state = _state(tmp_path)
    declared = _declare(state, _three_per_row(SIX_TOPOLOGIES))
    assert declared["status"] == "success", declared.get("error")
    rendered = _render(
        state, contract_id=declared["contract_id"], output_name="topology_overview",
        output_files=["topology_overview.pdf", "topology_overview.png"],
    )
    assert rendered["status"] == "success", rendered.get("error")
    assert rendered["renders_left"] == 2
    pdf = next(Path(tmp_path).glob("*/outputs/postprocess/topology_overview.pdf"))
    ruler = _h9(pdf)
    assert ruler["median"] >= 7.0, ruler
    assert ruler["p10"] >= 6.0, ruler
    assert ruler["cjk"] == 0 and ruler["words"] > 30, ruler
    # 记录里写明按哪个版心、多大缩放、最小的字几 pt
    record = state.read_artifact(rendered["figure_id"])["metadata"]
    assert record["layout"]["metrics"]["print_fit"]["fits"]
    assert record["caller_policy"]["medium"] == "double_column"
    assert record["font_floor_pt"] == 7.0


def test_blocks_without_rows_are_packed_by_the_print_medium_not_by_aspect():
    """六个分栏（版面式）没写 row 时：原来一行最多 2 栏 → 永远 2×3 竖排；现在按
    版心里印得最大的排法选 → 3×2。"""

    from nodes.postprocess.diagram_compiler import layout_contract
    from nodes.postprocess.dot_layout import dot_available
    from nodes.postprocess.figure_contract import normalize_contract

    if not dot_available():
        pytest.skip("Graphviz dot not installed on this host")
    blocks = []
    for gi in range(1, 7):
        group = next(g for g in SIX_TOPOLOGIES["groups"] if g["id"] == f"g{gi}")
        ids = {nid for rank in group["ranks"] for nid in rank}
        blocks.append({
            "kind": "diagram", "title": group["label"],
            "nodes": [n for n in SIX_TOPOLOGIES["nodes"] if n["id"] in ids],
            "groups": [{**group, "label": ""}], "bands": [[f"group:g{gi}"]],
            "edges": [e for e in SIX_TOPOLOGIES["edges"] if e["from"] in ids],
        })
    contract = normalize_contract(
        {"title": "Six GPU server topologies", "medium": "double_column", "blocks": blocks, "assertions": []},
        family="schematic",
    )
    geometry = layout_contract(contract)
    rows = {}
    for panel in geometry["panels"]:
        rows.setdefault(round(panel["rect"][1]), []).append(panel["title"])
    assert sorted(len(row) for row in rows.values()) == [3, 3], rows
    fit = geometry["layout_metrics"]["print_fit"]
    assert fit["medium"] == "double_column" and fit["scale"] > 0.7, fit


def test_a_derived_medium_reports_the_fit_but_does_not_reject(tmp_path):
    """没人说这张图印在哪儿（调用方没给 width、作者没写 medium）：版心只是按 purpose
    猜的，事实照进结果与记录，不拒 —— 拿猜的版心罚作者，报的是他交不出的东西。"""

    if not _tex_and_poppler():
        pytest.skip("TeX (latexmk / tectonic) or dot not on PATH")
    request = {"request_id": OVERVIEW_ID, "asset_kind": "schematic", "purpose": "publication", "intent": "六拓扑"}
    state = _state(tmp_path, requests=[request])
    result = _declare(state, SIX_TOPOLOGIES)      # 2 组一行：170mm 下印不下
    assert result["status"] == "success", result.get("error")
    fit = result["print_fit"]
    assert fit["medium"] == "double_column" and not fit["fits"] and not fit["enforced"]
    assert "没有拒绝" in result["next_step"]


def test_an_author_declared_medium_is_enforced(tmp_path):
    """作者自己写了 single_column，那就是他的合同：印不下就拒。"""

    if not _tex_and_poppler():
        pytest.skip("TeX (latexmk / tectonic) or dot not on PATH")
    request = {"request_id": OVERVIEW_ID, "asset_kind": "schematic", "intent": "六拓扑"}
    state = _state(tmp_path, requests=[request])
    result = _declare(state, {**_three_per_row(SIX_TOPOLOGIES), "medium": "single_column"})
    assert result["status"] == "error"
    assert "single_column" in result["error"] and "below the 7pt floor" in result["error"]


def test_chinese_figure_text_is_rejected_when_the_caller_wants_english(tmp_path):
    """写作侧发了 text_language=en；iter11 之前这边一个字没读，七张示意图画成中文。"""

    state = _state(tmp_path)
    contract = _three_per_row(SIX_TOPOLOGIES)
    contract["groups"][0]["label"] = "拓扑 1"
    contract["nodes"][0]["sublabel"] = "两颗"
    result = _declare(state, contract)
    assert result["status"] == "error"
    assert "English" in result["error"] and "'拓扑 1'" in result["error"] and "'两颗'" in result["error"]


def test_forbidden_words_from_the_caller_are_rejected_in_the_contract(tmp_path):
    requests = _iter11_requests()
    requests[0]["constraints"]["forbid_text"] = [r"case[1-6]"]
    state = _state(tmp_path, requests=requests)
    contract = _three_per_row(SIX_TOPOLOGIES)
    contract["groups"][2]["label"] = "case3"
    result = _declare(state, contract)
    assert result["status"] == "error"
    assert "/case[1-6]/" in result["error"] and "'case3'" in result["error"]


def test_the_caller_s_medium_overrides_a_conflicting_author_medium(tmp_path):
    state = _state(tmp_path)
    result = _declare(state, {**_three_per_row(SIX_TOPOLOGIES), "medium": "poster"})
    assert result["status"] == "error"
    assert "pins this figure to medium='double_column'" in result["error"]


# ══ B. 数据图的设计合同 ═══════════════════════════════════════════════════════

PANORAMA_ID = PANORAMA_IDS[0]


def _panorama_contract(panels: int = 4, *, axes: bool = True) -> dict:
    nets = ["IB CC0", "IB HPCC", "RoCE HPCC", "RoCE DCQCN", "n5", "n6", "n7", "n8"][:panels]
    return {
        "title": "DeepSeek V4 Flash wall time (ring_direct)",
        "panels": [
            {
                "id": f"p{i}", "label": net,
                **({"axes": {
                    "x": {"label": "TP/EP strategy", "unit": "none"},
                    "y": {"label": "Wall time", "unit": "ms", "scale": "linear"},
                }} if axes else {}),
            }
            for i, net in enumerate(nets, start=1)
        ],
        "series": [
            {"id": "gpu", "label": "GPU compute", "source_field": "gpu"},
            {"id": "comm", "label": "Communication", "source_field": "comm"},
        ],
        "assertions": [
            {"id": "four-panels", "kind": "panel_count", "equals": panels,
             "derived_from": "只保留 1 种 collective 算法 × 4 种网络"},
            {"id": "two-series", "kind": "series_count", "equals": 2,
             "derived_from": "堆叠柱区分 compute 与 communication"},
        ],
    }


# iter11 里模型定稿的那段代码（10×7in、刻度 8pt、写死的橙蓝、没有 label/图例/轴标题），
# 数据换成确定的合成数（真数据在夹具目录里，不在仓库）。
MODEL_CODE = """
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
NETWORKS = ['IB CC0','IB HPCC','RoCE HPCC','RoCE DCQCN']
TP = ['TP2EP16','TP4EP8','TP8EP4','TP16EP2']
rng = np.random.default_rng(0)
fig, axes = plt.subplots(2, 2, figsize=(10, 7))
for ci, net in enumerate(NETWORKS):
    ax = axes[ci//2][ci%2]
    x = np.arange(4); width = 0.12
    for ti, tp in enumerate(TP):
        for tj in range(6):
            gpu, comm = rng.uniform(100, 200), rng.uniform(20, 80)
            xpos = x[ti] + (tj-2.5)*width
            ax.bar(xpos, gpu, width=width, color='#4C72B0', edgecolor='none')
            ax.bar(xpos, comm, width=width, bottom=gpu, color='#DD8452', edgecolor='none')
    ax.set_xticks(x); ax.set_xticklabels(TP, fontsize=8, rotation=30)
    ax.set_title(net, fontsize=10); ax.tick_params(labelsize=8)
fig.suptitle('DeepSeek V4 Flash wall time (ring_direct)', fontsize=12)
fig.tight_layout(rect=[0,0,1,0.95])
fig.savefig('panorama.png', dpi=300); fig.savefig('panorama.pdf')
"""


def _compliant_code(*, legend: bool = True, ylabel: str = "Wall time (ms)", figsize=(6.6, 4.6),
                    colours: tuple[str, str] | None = None, extra_label: str | None = None,
                    legend_outside: bool = False) -> str:
    colour_kw = ["", ""] if colours is None else [f", color='{colours[0]}'", f", color='{colours[1]}'"]
    return f"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
NETWORKS = ['IB CC0','IB HPCC','RoCE HPCC','RoCE DCQCN']
TP = ['TP2EP16','TP4EP8','TP8EP4','TP16EP2']
rng = np.random.default_rng(0)
fig, axes = plt.subplots(2, 2, figsize={figsize!r}, sharey=True)
for ci, net in enumerate(NETWORKS):
    ax = axes[ci//2][ci%2]
    x = np.arange(4); width = 0.12
    for ti, tp in enumerate(TP):
        for tj in range(6):
            gpu, comm = rng.uniform(100, 200), rng.uniform(20, 80)
            xpos = x[ti] + (tj-2.5)*width
            first = (ti == 0 and tj == 0 and ci == 0)
            ax.bar(xpos, gpu, width=width, label='GPU compute' if first else None{colour_kw[0]})
            ax.bar(xpos, comm, width=width, bottom=gpu, label='Communication' if first else None{colour_kw[1]})
    ax.set_xticks(x); ax.set_xticklabels(TP, rotation=20)
    ax.set_title(net)
    if ci % 2 == 0: ax.set_ylabel({ylabel!r})
    if ci >= 2: ax.set_xlabel('TP/EP strategy')
{"axes[0][0].plot([0, 3], [150, 150], label=" + repr(extra_label) + ")" if extra_label else ""}
{"fig.legend(loc='upper left', bbox_to_anchor=(1.02, 1.0))" if legend_outside else ("axes[0][0].legend(loc='upper right')" if legend else "")}
fig.suptitle('DeepSeek V4 Flash wall time (ring_direct)')
fig.tight_layout(rect=[0,0,1,0.96])
fig.savefig('panorama.png', dpi=300); fig.savefig('panorama.pdf')
"""


def _quant_state(tmp_path) -> State:
    return _state(tmp_path)


def _declare_panorama(state, contract):
    return _declare(state, contract, request_id=PANORAMA_ID, asset_kind="quantitative")


def _render_panorama(state, contract_id, code):
    dataset = state.save_artifact(artifact_type="dataset", name="d", content="x", metadata={})["id"]
    return _render(
        state, request_id=PANORAMA_ID, asset_kind="quantitative", contract_id=contract_id, code=code,
        output_name="panorama", output_files=["panorama.png", "panorama.pdf"], source_artifact_ids=[dataset],
    )


def test_the_iter11_panorama_contract_is_rejected_for_axes_without_labels_or_units(tmp_path):
    state = _quant_state(tmp_path)
    result = _declare_panorama(state, _panorama_contract(axes=False))
    assert result["status"] == "error"
    assert "axes.x must declare a label" in result["error"]
    # 合同写了 label 没写 unit：单位缺席也要明说
    contract = _panorama_contract()
    del contract["panels"][0]["axes"]["y"]["unit"]
    result = _declare_panorama(state, contract)
    assert result["status"] == "error" and "must declare a unit" in result["error"]
    # 旧写法（裸刻度名）指路到新写法
    contract = _panorama_contract()
    contract["panels"][0]["axes"]["y"] = "linear"
    result = _declare_panorama(state, contract)
    assert result["status"] == "error" and "not the bare scale name 'linear'" in result["error"]


def test_more_than_four_panels_is_rejected_at_declaration(tmp_path):
    state = _quant_state(tmp_path)
    result = _declare_panorama(state, _panorama_contract(panels=8))
    assert result["status"] == "error"
    assert f"at most {MAX_PANELS}" in result["error"] and "Split into several figures" in result["error"]


def test_a_compliant_declaration_hands_back_the_render_guidance(tmp_path):
    state = _quant_state(tmp_path)
    result = _declare_panorama(state, _panorama_contract())
    assert result["status"] == "success", result.get("error")
    guide = result["render_guidance"]
    assert guide["figsize_max_inches"] == [round(MEDIA["double_column"]["w"] / 25.4, 2), round(MEDIA["double_column"]["h"] / 25.4, 2)]
    assert guide["font_floor_pt"] == 7.0 and guide["legend_required"]
    assert guide["axis_labels_expected"] == ["TP/EP strategy", "Wall time (ms)"]
    assert guide["palette"] == list(DATA_PALETTE)


def test_the_iter11_code_is_rejected_for_every_design_reason_at_once(tmp_path):
    """模型定稿的那段代码：没 label（→ 192 个 container 而不是 2 条序列）、没图例、
    没轴标题、10 英寸宽而刻度 8pt（170mm 下 5.3pt）、写死的 #DD8452。一次报全。"""

    state = _quant_state(tmp_path)
    declared = _declare_panorama(state, _panorama_contract())
    result = _render_panorama(state, declared["contract_id"], MODEL_CODE)
    assert result["status"] == "error", result
    text = result["error"]
    assert "two-series expects equals 2 but the rendering gives 192" in text
    assert "has no legend" in text
    assert "'Wall time (ms)'" in text and "'TP/EP strategy'" in text and "not on the page" in text
    assert "prints at" in text and "below the 7pt floor" in text and "figsize" in text
    assert "#dd8452" in text and "not in the node's palette" in text
    assert result["renders_left"] == 2   # 花了一次


def test_a_compliant_render_passes_and_series_are_counted_by_legend_entries(tmp_path):
    state = _quant_state(tmp_path)
    declared = _declare_panorama(state, _panorama_contract())
    result = _render_panorama(state, declared["contract_id"], _compliant_code())
    assert result["status"] == "success", result.get("error")
    checks = {item["id"]: item for item in result["contract_checks"]}
    assert checks["two-series"]["holds"] and checks["two-series"]["actual"] == 2
    assert checks["four-panels"]["holds"]
    record = state.read_artifact(result["figure_id"])["metadata"]
    model = record["object_model"]
    assert model["legends"][0]["labels"] == ["GPU compute", "Communication"] and model["legends"][0]["inside"]
    assert set(model["series_colors"]) <= {c.lower() for c in DATA_PALETTE}
    assert model["min_font_pt"] >= 7.0
    assert not [f for f in result["findings"] if f.get("collector") == "OB-RESOLUTION"], "savefig(dpi=300) 不该被报 100 DPI"
    # 样式前言进了冻结脚本：referee 重跑得到同一张图
    script = next(Path(tmp_path).rglob("panorama_render.py"))
    assert "axes.prop_cycle" in script.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "variant, needle",
    [
        ({"legend": False}, "has no legend"),
        ({"extra_label": "reference"}, "the legend on the page reads"),
        ({"ylabel": "Wall time"}, "not on the page: 'Wall time (ms)'"),
        ({"figsize": (10, 7)}, "below the 7pt floor"),
        ({"colours": ("#1f77b4", "#ff7f0e")}, "not in the node's palette"),
        ({"legend_outside": True}, "outside the canvas"),
    ],
)
def test_each_design_obligation_is_checked_on_the_rendered_object_model(tmp_path, variant, needle):
    state = _quant_state(tmp_path)
    declared = _declare_panorama(state, _panorama_contract())
    result = _render_panorama(state, declared["contract_id"], _compliant_code(**variant))
    assert result["status"] == "error", result
    assert needle in result["error"], result["error"]


# ══ C. 家族 / 版心 / 语言绑定调用方；渲染上限 ══════════════════════════════════


def test_a_schematic_request_cannot_be_relabelled_to_draw_with_code(tmp_path):
    """iter7/iter11：同一 request_id 从 schematic 改标 composite，用 code 画手写坐标。"""

    state = _state(tmp_path)
    declared = _declare(state, {"title": "t", "panels": [{"id": "p", "axes": {}}], "series": [], "assertions": []},
                        asset_kind="composite")
    assert declared["status"] == "error" and "is a schematic request" in declared["error"]
    rendered = _render(state, asset_kind="composite", code="print('x')")
    assert rendered["status"] == "error" and "is a schematic request" in rendered["error"]
    rendered = _render(state, asset_kind="schematic", code="print('x')")
    assert rendered["status"] == "error" and "rendered from a declared contract" in rendered["error"]


def test_an_unknown_request_id_is_rejected_at_declaration_listing_the_legal_ids(tmp_path):
    state = _state(tmp_path)
    result = _declare(state, _three_per_row(SIX_TOPOLOGIES), request_id="made-up")
    assert result["status"] == "error"
    assert OVERVIEW_ID in result["error"] and PANORAMA_IDS[0] in result["error"]


def test_the_render_budget_is_three_per_request_and_the_fourth_hands_the_decision_back(tmp_path):
    state = _quant_state(tmp_path)
    declared = _declare_panorama(state, _panorama_contract())
    assert declared["renders_left"] == 3
    first = _render_panorama(state, declared["contract_id"], MODEL_CODE)          # 失败也算一次
    assert first["status"] == "error" and first["renders_left"] == 2
    second = _render_panorama(state, declared["contract_id"], _compliant_code())
    assert second["status"] == "success" and second["renders_left"] == 1
    third = _render_panorama(state, declared["contract_id"], _compliant_code())
    assert third["status"] == "success" and third["renders_left"] == 0
    fourth = _render_panorama(state, declared["contract_id"], _compliant_code())
    assert fourth["status"] == "error" and fourth["error_code"] == "render_budget_exhausted"
    text = fourth["error"]
    assert "这张图我画不出合同要求的样子" in text
    assert "#1" in text and "192" in text                       # 每次的原因都在
    assert f"record {third['figure_id']} stands" in text        # 交付物是最后一次成功的记录
    assert "the caller decides" in text
    # 再声明也不会重置预算
    again = _declare_panorama(state, _panorama_contract())
    assert again["status"] == "success" and again["renders_left"] == 0
    # 参数写错被拒的不算一次
    bad = _render(state, request_id=PANORAMA_ID, asset_kind="quantitative", contract_id="contract__nope", code="x")
    assert bad["status"] == "error" and "never declared" in bad["error"]


def test_a_contract_belongs_to_the_request_it_was_declared_for(tmp_path):
    state = _state(tmp_path)
    declared = _declare_panorama(state, _panorama_contract())
    result = _render(state, request_id=PANORAMA_IDS[1], asset_kind="quantitative",
                     contract_id=declared["contract_id"], code="print(1)")
    assert result["status"] == "error" and "one contract belongs to one caller request" in result["error"]


def test_the_briefing_tells_the_node_what_is_bound(tmp_path):
    from types import SimpleNamespace

    from nodes.postprocess.hooks import _visual_request_briefing

    state = _state(tmp_path)
    messages = _visual_request_briefing(SimpleNamespace(state=state))
    text = messages[0].content
    assert f"`{OVERVIEW_ID}`" in text and "asset_kind=schematic" in text
    assert "width=double_column" in text and "text_language=en" in text and "min_font_pt=7" in text
    assert "最多渲染 3 次" in text


def test_the_caller_policy_vocabulary_is_in_the_request_schema():
    """契约要送到调用方：写作节点按这几个键发政策，schema 就得教这几个键。"""

    from nodes.postprocess.contracts import visual_request_schema

    props = visual_request_schema()["properties"]["constraints"]["properties"]
    assert set(props) >= {"width", "text_language", "min_font_pt", "forbid_text"}
    assert set(props["width"]["enum"]) == set(MEDIA)


# ══ 重放：iter11 写作侧发出的 5 条请求 ═════════════════════════════════════════


def test_replaying_the_five_iter11_requests_mints_five_records_within_budget(tmp_path):
    """每图 ≤ 3 次渲染；六拓扑 3×2 全英文；四张 panorama ≤ 4 面板带图例。"""

    if not _tex_and_poppler():
        pytest.skip("TeX (latexmk / tectonic), dot or poppler not on PATH")
    state = _state(tmp_path)
    renders = 0
    declared = _declare(state, SIX_TOPOLOGIES)                         # 模型会先这么声明：拒，带改法
    assert declared["status"] == "error"
    declared = _declare(state, _three_per_row(SIX_TOPOLOGIES))
    assert declared["status"] == "success", declared.get("error")
    result = _render(state, contract_id=declared["contract_id"], output_name="topology_overview",
                     output_files=["topology_overview.pdf", "topology_overview.png"])
    renders += 1
    assert result["status"] == "success", result.get("error")
    for index, rid in enumerate(PANORAMA_IDS):
        contract = _panorama_contract()
        contract["title"] = rid
        declared = _declare(state, contract, request_id=rid, asset_kind="quantitative")
        assert declared["status"] == "success", declared.get("error")
        dataset = state.save_artifact(artifact_type="dataset", name=f"d{index}", content="x", metadata={})["id"]
        result = _render(
            state, request_id=rid, asset_kind="quantitative", contract_id=declared["contract_id"],
            code=_compliant_code().replace("panorama.p", f"panorama_{index}.p"),
            output_name=f"panorama_{index}", output_files=[f"panorama_{index}.png", f"panorama_{index}.pdf"],
            source_artifact_ids=[dataset],
        )
        renders += 1
        assert result["status"] == "success", result.get("error")
    records = [state.read_artifact(e["id"])["metadata"] for e in state.list_artifacts(artifact_type="figure")]
    assert {r["request_id"] for r in records} == {OVERVIEW_ID, *PANORAMA_IDS}
    assert renders == 5 and all(len(v) <= 3 for v in state.hook_state["figure_render_attempts"].values())
    for record in records:
        if record["asset_kind"] == "quantitative":
            assert len(record["contract"]["panels"]) <= MAX_PANELS
            assert record["object_model"]["legends"] and record["object_model"]["legends"][0]["inside"]


def test_a_script_path_render_has_a_file_name_like_a_real_script(tmp_path):
    """replay11 实测：模型按文件写脚本（Path(__file__).parent / 'parsed_data.json'），
    沙箱当 -c 代码跑 → NameError，四张图各烧掉一次渲染预算；而 replay 命令跑冻结
    文件时 __file__ 在场。同一份代码两种结果，补上它。"""

    from core.project_workspace import validate_tool_cwd

    state = _quant_state(tmp_path)
    declared = _declare_panorama(state, _panorama_contract())
    workspace = validate_tool_cwd(state, None)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "parsed_data.json").write_text("[1, 2, 3]", encoding="utf-8")
    script = _compliant_code().replace(
        "rng = np.random.default_rng(0)",
        "import json, pathlib\nrng = np.random.default_rng(len(json.loads((pathlib.Path(__file__).parent / 'parsed_data.json').read_text())))",
    )
    (workspace / "render_panorama.py").write_text(script, encoding="utf-8")
    dataset = state.save_artifact(artifact_type="dataset", name="d", content="x", metadata={})["id"]
    result = _render(
        state, request_id=PANORAMA_ID, asset_kind="quantitative", contract_id=declared["contract_id"],
        script_path="render_panorama.py", output_name="panorama",
        output_files=["panorama.png", "panorama.pdf"], source_artifact_ids=[dataset],
    )
    assert result["status"] == "success", result.get("error")

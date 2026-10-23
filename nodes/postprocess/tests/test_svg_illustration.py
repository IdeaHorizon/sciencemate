"""SVG 轨（illustration 家族，AutoFigure 移植）的不变量。

钉住的事（方案 A §六 改进清单 #13 与补丁 3-7）：

- 纯文字请求路由到 SVG 轨，declare 锁轨锁 genre（agent 改不掉）
- 试渲染不占渲染预算；铸造与 render_figure 共用同一本 3 次账
- 合同断言对 SVG DOM 求值：声明了没标记 = 拒铸
- DOM 标记缺/重 = finding（不是拒绝）；文字政策（语言/字号）= 拒绝
- annotated_values 缺席落账不拦（补丁 4 纪律：文字来源的数不声明）
- request_id 同批复撞车被拒并列出合法值（含长中文 intent 的 slug 回退）
- enhance_figure 只接 SVG 轨；未配置身份 = 诚实 not_configured
- SKILL.md 与 svg_vocab 词表同源（漂移即红）
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute

bootstrap()

from nodes.postprocess import svg_vocab  # noqa: E402
from nodes.postprocess.contracts import (  # noqa: E402
    VisualContractError,
    normalize_request,
    route_track,
)


@pytest.fixture(autouse=True)
def _fake_sandbox(monkeypatch):
    """与 test_figure_contract 同一个替身：裸 `python -c`，快但沙箱不在场。

    渲染脚本需要 cairosvg —— 它由真实环境的 site-packages 提供（本测试文件
    不覆盖装包机制本身，那部分由阶段 0 spike 与真机联调负责）。
    """
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


@pytest.fixture(autouse=True)
def _no_visual_review_role(monkeypatch):
    from core import model_roles

    monkeypatch.delenv("HARNESS_MODEL_ROLES", raising=False)
    model_roles.install_from_environment(force=True)


# ── 拓扑图回归 fixture（方案 A 改进 #13：就用推演那张图）──────────────────

_ROLE_FILL = {"server": "#4C72B0", "accelerator": "#55A868", "pcie_switch": "#C44E52",
              "nic": "#8172B2", "uplink": "#CCB974"}


def _topology(*, servers: int = 2, gpus: int = 2):
    """小型双服务器拓扑：返回 (svg_text, contract)。每服务器 gpus 张卡 + 1 switch，
    两台 switch 互联，各上 1 张 NIC，全部上联 1 台 RoCE 交换机。全标记。"""
    nodes, edges, groups = [], [], []
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 480">']
    parts.append('<text x="320" y="24" text-anchor="middle" font-size="14">Network Topology</text>')
    # 上联赛交换机
    parts.append(
        f'<rect x="270" y="50" width="100" height="34" fill="{_ROLE_FILL["uplink"]}" '
        f'data-role="uplink" data-node="roce0"/>'
        f'<text x="320" y="70" text-anchor="middle" font-size="10" data-label="roce0">RoCE Switch</text>'
    )
    nodes.append({"id": "roce0", "label": "RoCE Switch", "role": "uplink"})
    for s in range(servers):
        gid = f"srv{s}"
        x0 = 40 + s * 300
        parts.append(f'<g data-group="{gid}">')
        parts.append(
            f'<rect x="{x0}" y="150" width="260" height="280" fill="none" '
            f'stroke="{_ROLE_FILL["server"]}" stroke-width="2" data-role="server" data-node="{gid}"/>'
            f'<text x="{x0 + 130}" y="170" text-anchor="middle" font-size="11" '
            f'data-label="{gid}">Server {s}</text>'
        )
        nodes.append({"id": gid, "label": f"Server {s}", "role": "server"})
        sw = f"{gid}-sw0"
        parts.append(
            f'<rect x="{x0 + 90}" y="360" width="80" height="30" fill="{_ROLE_FILL["pcie_switch"]}" '
            f'data-role="pcie_switch" data-node="{sw}"/>'
            f'<text x="{x0 + 130}" y="378" text-anchor="middle" font-size="9" '
            f'data-label="{sw}">PCIe Switch</text>'
        )
        nodes.append({"id": sw, "label": f"PCIe Switch {s}", "role": "pcie_switch"})
        edges.append({"from": sw, "to": "roce0", "role": "uplink"})
        parts.append(
            f'<path d="M {x0 + 130} 360 L 320 84" stroke="#333" fill="none" '
            f'data-edge-from="{sw}" data-edge-to="roce0"/>'
        )
        nic = f"{gid}-nic0"
        parts.append(
            f'<rect x="{x0 + 190}" y="360" width="60" height="30" fill="{_ROLE_FILL["nic"]}" '
            f'data-role="nic" data-node="{nic}"/>'
            f'<text x="{x0 + 220}" y="378" text-anchor="middle" font-size="9" '
            f'data-label="{nic}">400G NIC</text>'
        )
        nodes.append({"id": nic, "label": f"NIC {s}", "role": "nic"})
        edges.append({"from": nic, "to": sw, "role": "pcie"})
        parts.append(
            f'<path d="M {x0 + 190} 375 L {x0 + 170} 375" stroke="#333" fill="none" '
            f'data-edge-from="{nic}" data-edge-to="{sw}"/>'
        )
        for g in range(gpus):
            gpu = f"{gid}-g{g}"
            gx = x0 + 20 + g * 70
            parts.append(
                f'<rect x="{gx}" y="220" width="56" height="26" fill="{_ROLE_FILL["accelerator"]}" '
                f'data-role="accelerator" data-node="{gpu}"/>'
                f'<text x="{gx + 28}" y="236" text-anchor="middle" font-size="8" '
                f'data-label="{gpu}">GPU {g}</text>'
            )
            nodes.append({"id": gpu, "label": f"GPU {g}", "role": "accelerator"})
            edges.append({"from": gpu, "to": sw, "role": "pcie"})
            parts.append(
                f'<path d="M {gx + 28} 246 L {x0 + 130} 360" stroke="#333" fill="none" '
                f'data-edge-from="{gpu}" data-edge-to="{sw}"/>'
            )
        groups.append({"id": f"{gid}-box", "label": f"Server {s}",
                       "ranks": [[n["id"] for n in nodes if n["id"].startswith(gid)]]})
        parts.append("</g>")
    parts.append("</svg>")
    contract = {
        "title": "mini cluster topology",
        "nodes": nodes,
        "groups": groups,
        "edges": edges,
        "assertions": [
            {"id": "n-gpu", "kind": "node_count", "selector": {"role": "accelerator"},
             "equals": servers * gpus, "derived_from": "每台 8 张 GPU"},
            {"id": "n-switch", "kind": "node_count", "selector": {"role": "pcie_switch"},
             "equals": servers, "derived_from": "每台 2 个 switch"},
        ],
    }
    return "\n".join(parts), contract


def _state(tmp_path, requests=None):
    state = State.new(node_type="postprocess", base_dir=tmp_path)
    state.hook_state["node_inputs"] = {
        "visual_requests": requests
        or [{"request_id": "topo", "intent": "画一个网络拓扑图：服务器、GPU、PCIe switch、RoCE 交换机"}]
    }
    return state


def _declare(state, contract, request_id="topo", asset_kind="generative_illustration"):
    return asyncio.run(execute(
        "declare_figure_contract", state,
        request_id=request_id, asset_kind=asset_kind, contract=contract,
    ))


def _render(state, svg, *, request_id="topo", contract_id, trial=False):
    return asyncio.run(execute(
        "render_illustration", state,
        request_id=request_id, contract_id=contract_id, svg_text=svg,
        output_name="topo", caption="cluster topology", alt_text="topology",
        trial=trial,
    ))


# ── 路由与锁定（方案 A §4.1）─────────────────────────────────────────────


def test_pure_text_routes_to_svg_track_and_locks(tmp_path):
    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    assert declared["status"] == "success"
    assert declared["family"] == "illustration"
    assert declared["track"] == "svg"
    assert declared["genre"] == "paper"  # 默认文体
    # 框架发色推给作者
    assert declared["render_guidance"]["role_colors"]["accelerator"]


def test_route_track_input_form_axis():
    assert route_track({"intent": "画拓扑图"}) == "svg"
    assert route_track({"intent": "x", "source_artifact_ids": ["d1"]}) == "matplotlib"
    assert route_track({"intent": "x", "spec": {"nodes": [{"id": "n"}]}}) == "compiled"
    assert route_track({"intent": "x", "preferred_form": "data_chart"}) == "matplotlib"
    assert route_track({"intent": "x", "preferred_form": "illustration",
                        "source_artifact_ids": ["d1"]}) == "svg"
    with pytest.raises(VisualContractError):
        route_track({"intent": "x", "preferred_form": "随便"})
    assert normalize_request({"intent": "x"})["preferred_form"] is None


def test_illustration_contract_with_bands_is_rejected_loudly(tmp_path):
    """bands/blocks 是编译轨的版面词：SVG 轨布局归作者，写了必须在 declare 时
    响亮拒绝 —— 2026-10-16 e2e 之前是静默丢弃，覆盖判据再把它诬告成
    「框架漏接」。"""
    _svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, {**contract, "bands": [["group:srv0-box"]]})
    assert declared["status"] == "error"
    assert "bands" in declared["error"] and "layout" in declared["error"]


def test_illustration_contract_draws_no_coverage_libel(tmp_path):
    """illustration 家族的 nodes/groups/edges 是合法词：不得再被
    OB-CONTRACT-COVERAGE 报成「词表里没有的键」或「归一化后丢了」。"""
    _svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    assert declared["status"] == "success"
    libel = [f for f in declared.get("coverage_findings") or []
             if f.get("code") in ("contract.unexpressed_keys", "contract.dropped_keys")]
    assert libel == [], libel


# ── 铸造门 ────────────────────────────────────────────────────────────────


def test_mint_happy_path_and_consumer_validation(tmp_path):
    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["status"] == "success", minted.get("error")
    assert minted["files"][0]["format"] == "png"
    assert minted["vlm_review"]["ran"] is False  # 角色缺席：如实记，不装
    record = state.read_artifact(minted["figure_id"])
    meta = record["metadata"]
    assert meta["track"] == "svg" and meta["genre"] == "paper"
    assert meta["render_code"]["generator"] == "frozen-svg"
    assert {f["format"] for f in meta["files"]} == {"png"}  # SVG 不作交付格式
    # 补丁 4 纪律：没声明 annotated_values → 缺席落账，不拦
    assert meta["annotated_values_check"]["ran"] is False
    # 消费端重算通过（writing/_reviewer 路径零改动）
    from shared.lib.publication_figures import validate_figure_record

    validate_figure_record(record)


def test_trial_render_costs_no_budget(tmp_path):
    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    for _ in range(2):
        trial = _render(state, svg, contract_id=declared["contract_id"], trial=True)
        assert trial["status"] == "success", trial.get("error")
        assert trial["audit_summary"]["marked_nodes"] == len(contract["nodes"])
        assert trial.get("assertion_failures") is None
        # 试渲染稿同样先落盘（栅格化成败都不丢创作成果）
        assert Path(trial["svg_path"]).read_text(encoding="utf-8") == svg
    assert declared["renders_left"] == 3  # 两次试渲染后仍一次没少
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["renders_left"] == 2


def test_declared_but_unmarked_node_rejects_mint(tmp_path):
    """合同声明了、DOM 没标记 → 断言对 DOM 求值失败 → 拒铸（不是 finding）。"""
    svg, contract = _topology()
    svg = svg.replace(' data-node="srv0-g0"', "")  # 摘掉一个标记
    state = _state(tmp_path)
    declared = _declare(state, contract)
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["status"] == "error"
    assert "contradicts its own contract" in minted["error"]
    assert "n-gpu" in minted["error"]


def test_marker_problems_are_findings_not_rejection(tmp_path):
    """缺 data-role 配对是 finding；断言覆盖的事实仍由断言管。"""
    svg, contract = _topology()
    svg = svg.replace(' data-role="nic"', "")  # NIC 只剩 data-node，缺 role
    state = _state(tmp_path)
    declared = _declare(state, contract)
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["status"] == "success", minted.get("error")
    markers = [f for f in minted["findings"] if f["collector"] == "SVG-MARKER"]
    assert markers and "data-role" in markers[0]["message"]


def test_cjk_rejected_when_caller_binds_english(tmp_path):
    svg, contract = _topology()
    svg = svg.replace("RoCE Switch", "RoCE 交换机")
    state = _state(tmp_path, requests=[{
        "request_id": "topo", "intent": "network topology",
        "constraints": {"text_language": "en"},
    }])
    declared = _declare(state, contract)
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["status"] == "error"
    assert "English" in minted["error"]


def test_font_floor_enforced_when_caller_binds_medium(tmp_path):
    svg, contract = _topology()
    svg = svg.replace('font-size="8"', 'font-size="2"')  # GPU 标签缩到看不见
    state = _state(tmp_path, requests=[{
        "request_id": "topo", "intent": "network topology",
        "constraints": {"width": "single_column"},
    }])
    declared = _declare(state, contract)
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["status"] == "error"
    assert "floor" in minted["error"]


def test_rasterize_failure_still_leaves_the_svg_on_disk(tmp_path, monkeypatch):
    """栅格化失败（环境性）≠ 创作成果消失：SVG 必须先落盘，错误里带路径。

    2026-10-16 e2e 前两轮的教训：bwrap/PIL 事故后工作区一张图都没有，
    已通过审计与断言的 SVG 只存在于 transcript 的工具参数里。
    """
    from shared.tools.library import python_exec

    async def broken_execute_python(state, code, timeout=300, cwd=None, requirements=None, **_):
        return {"status": "error", "returncode": 1,
                "error": "bwrap: execvp python: No such file or directory",
                "stdout_tail": "", "stderr_tail": "bwrap: execvp ..."}

    monkeypatch.setattr(python_exec, "_execute_python", broken_execute_python)

    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["status"] == "error"
    # SVG 在盘上、逐字节等于作者稿；像素没铸成就没有记录
    assert Path(minted["svg_path"]).read_text(encoding="utf-8") == svg
    assert "figure_id" not in minted


def test_witness_questions_expand_group_edge_endpoints():
    """group:<id> 边端点 = 连到全体成员：证人问题生成的度数字典不能 KeyError
    （2026-10-16 e2e：1024 卡拓扑的组级上联在 mint 的证人环节炸掉，
    declare/trial 都发现不了）。"""
    from nodes.postprocess.vlm_extraction import build_questions

    contract = {
        "family": "illustration",
        "nodes": [{"id": "p0", "label": "P0", "role": "pod"},
                  {"id": "p1", "label": "P1", "role": "pod"},
                  {"id": "sw", "label": "SW", "role": "switch"}],
        "groups": [{"id": "pods", "label": "Pods", "ranks": [["p0", "p1"]]}],
        "edges": [{"from": "group:pods", "to": "sw", "role": "uplink"}],
    }
    questions = build_questions(contract)
    degrees = {item["label"]: item["expected"] for item in questions["degrees"]}
    # 组展开不炸，且语义与结构校验一致：一条组级边 = 每个成员各度 1、对端度 1
    assert degrees["SW"] == 1
    assert degrees["P0"] == 1 and degrees["P1"] == 1


def test_tofu_glyph_is_rejected_by_renderer_truth(tmp_path):
    """字形检查按**渲染器真相**判（cairo 只取 font-family 第一个、无逐字回退）：
    ᵀ（U+1D40）在宿主解析出的 regular 字体里没有 —— 2026-10-20 用例 3 实测
    同一字符 regular 豆腐、bold 正常，字体栈并集判据放过了它。"""
    import pytest as _pytest
    # 本测试依赖宿主的 cairo 解析行为：DejaVu Sans regular 无 U+1D40。
    # 在解析结果不同的机器上（比如真装了带 ᵀ 的 DejaVu Sans）不制造假红。
    # cairocffi 是沙箱 requirements 装的（README §SVG 轨），框架进程里没有它是常态 ——
    # 没装就跳过，别把「测试机没装 cairo」报成「字形检查坏了」（框架侧代改，wangd 授权，owner: cuikl）。
    _cairo = _pytest.importorskip("cairocffi")
    surface = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, 1, 1)
    ctx = _cairo.Context(surface)
    ctx.select_font_face("DejaVu Sans", _cairo.FONT_SLANT_NORMAL, _cairo.FONT_WEIGHT_NORMAL)
    glyphs, _, _ = ctx.get_scaled_font().text_to_glyphs(0, 0, "ᵀ", True)
    if glyphs and glyphs[0][0] != 0:
        _pytest.skip("本机 DejaVu Sans regular 覆盖 U+1D40，复现不了豆腐")

    svg, contract = _topology()
    svg = svg.replace('font-size="14">Network Topology<', 'font-size="14">Network Topology QKᵀ<')
    state = _state(tmp_path)
    declared = _declare(state, contract)
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["status"] == "error"
    assert "tofu" in minted["error"] and "ᵀ" in minted["error"]
    # 内容失败照扣预算（与环境失败相反）
    assert minted["renders_left"] == 2
    from nodes.postprocess.vlm_extraction import build_questions

    contract = {
        "family": "illustration",
        "nodes": [{"id": "p0", "label": "P0", "role": "pod"},
                  {"id": "p1", "label": "P1", "role": "pod"},
                  {"id": "sw", "label": "SW", "role": "switch"}],
        "groups": [{"id": "pods", "label": "Pods", "ranks": [["p0", "p1"]]}],
        "edges": [{"from": "group:pods", "to": "sw", "role": "uplink"}],
    }
    questions = build_questions(contract)
    degrees = {item["label"]: item["expected"] for item in questions["degrees"]}
    # 组展开不炸，且语义与结构校验一致：一条组级边 = 每个成员各度 1、对端度 1
    assert degrees["SW"] == 1
    assert degrees["P0"] == 1 and degrees["P1"] == 1


def test_environment_failure_costs_no_budget_and_breaks_after_three(tmp_path, monkeypatch):
    """环境性栅格化失败不扣铸造预算（2026-10-16 用户拍板：图不替机器背锅），
    返回里机械标注 error_class/budget_charged 供如实上报；同一请求连续
    3 次环境失败熔断 —— 确定性故障不许无限重试。"""
    from shared.tools.library import python_exec

    async def broken_execute_python(state, code, timeout=300, cwd=None, requirements=None, **_):
        return {"status": "error", "returncode": 1,
                "error": "bwrap: execvp python: No such file or directory",
                "stdout_tail": "", "stderr_tail": "bwrap: execvp ..."}

    monkeypatch.setattr(python_exec, "_execute_python", broken_execute_python)

    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    assert declared["renders_left"] == 3
    for _ in range(2):
        failed = _render(state, svg, contract_id=declared["contract_id"])
        assert failed["status"] == "error"
        assert failed["error_class"] == "environment"
        assert failed["budget_charged"] is False
        assert failed["renders_left"] == 3  # 一次都没扣
    third = _render(state, svg, contract_id=declared["contract_id"])
    assert third["status"] == "error"
    assert third["circuit_breaker"] == "env_fail_streak"
    assert "STOP" in third["error"]
    assert third["renders_left"] == 3  # 熔断也不扣


def test_framework_bug_in_mint_costs_no_budget(tmp_path):
    """铸造中途的框架 bug（非 VisualContractError 的意外异常）不扣预算：
    账本登记撤回、异常原样上抛给 registry 的 tool_exception 通道。
    （2026-10-16 e2e：证人问题生成撞组端点 KeyError 炸 mint，预算照扣。）"""
    from nodes.postprocess.tools import illustration

    async def buggy_witness(**_kwargs):
        raise KeyError("simulated framework bug")

    original = illustration._witness_review
    illustration._witness_review = buggy_witness
    try:
        svg, contract = _topology()
        state = _state(tmp_path)
        declared = _declare(state, contract)
        crashed = _render(state, svg, contract_id=declared["contract_id"])
        assert crashed["status"] == "error"
    finally:
        illustration._witness_review = original
    # bug 没扣预算：修复后的成功铸造剩 2 次（不是 1 次）
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["status"] == "success", minted.get("error")
    assert minted["renders_left"] == 2


def test_aspect_mismatch_is_a_finding(tmp_path):
    """横版 viewBox vs 竖版心：披露不拒（§六-B6）。"""
    svg, contract = _topology()
    # 最小字号抬到 10：别让它先撞上字号下限（640 宽 viewBox 在 170mm 版心下
    # 8/9 单位 ≈ 6.0/6.8pt 会被拒）—— 这条测试只问宽高比。
    svg = svg.replace('font-size="8"', 'font-size="10"').replace('font-size="9"', 'font-size="10"')
    state = _state(tmp_path, requests=[{
        "request_id": "topo", "intent": "network topology",
        "constraints": {"width": "double_column"},
    }])
    declared = _declare(state, contract)
    trial = _render(state, svg, contract_id=declared["contract_id"], trial=True)
    assert trial["status"] == "success"
    assert any(f["collector"] == "SVG-ASPECT" for f in trial["audit_findings"])


# ── 预算与撞车（改进 #2 / C10）────────────────────────────────────────────


def test_mint_budget_shared_with_render_figure(tmp_path):
    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    for i in range(3):
        result = _render(state, svg, contract_id=declared["contract_id"])
        assert result["status"] == "success", result.get("error")
    fourth = _render(state, svg, contract_id=declared["contract_id"])
    assert fourth["status"] == "error"
    assert fourth.get("error_code") == "render_budget_exhausted"


def test_duplicate_request_id_rejected_with_legible_error(tmp_path):
    """长中文 intent 的 slug 会塌成同一个回退值 —— 撞车必须当场说清。"""
    state = _state(tmp_path, requests=[
        {"intent": "画一个网络拓扑图：服务器与交换机"},       # slug → 回退
        {"intent": "画一个完全不同的话题：分子动力学模拟"},     # slug → 同一个回退
    ])
    svg, contract = _topology()
    from nodes.postprocess.tools.figure import caller_visual_request

    from nodes.postprocess.contracts import normalize_request as _norm
    rid = _norm({"intent": "画一个网络拓扑图：服务器与交换机"})["request_id"]
    with pytest.raises(VisualContractError, match="must identify exactly one"):
        caller_visual_request(state, rid)


# ── enhancement 门（补丁 6/7）────────────────────────────────────────────


def test_enhance_not_configured_is_honest(tmp_path):
    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    minted = _render(state, svg, contract_id=declared["contract_id"])
    result = asyncio.run(execute("enhance_figure", state, figure_id=minted["figure_id"]))
    assert result["status"] == "not_configured"
    assert "not_configured" == result["status"]


def test_enhance_rejects_non_svg_track(tmp_path):
    """入口闸：非 SVG 轨的记录构造上到不了美化（补丁 7）。"""
    state = _state(tmp_path)
    saved = state.save_artifact(
        artifact_type="figure",
        name="dataplot",
        content="x",
        metadata={"record": "rendered_figure", "track": "matplotlib"},
    )
    result = asyncio.run(execute("enhance_figure", state, figure_id=saved["id"]))
    assert result["status"] == "error"
    assert "svg-track" in result["error"]


# ── 词表同源（§八-10 钉住）────────────────────────────────────────────────


def test_skill_text_carries_the_marker_vocabulary():
    skill = (
        Path(__file__).parent.parent / "skills" / "svg-illustration" / "SKILL.md"
    ).read_text(encoding="utf-8")
    for attr in svg_vocab.ALL_ATTRIBUTES:
        assert attr in skill, f"SKILL.md 缺 {attr} —— 词表漂移了"
    assert "恰好出现一次" in skill  # 铁律


# ── letterbox 边带（E-11，2026-10-20 e2e 实测）─────────────────────────────
#
# 自画像那张图：SVG 声明 width="170mm" height="200mm"（比 0.85000）而
# viewBox="0 0 482 566"（比 0.85159），0.19% 的失配让 cairosvg 按
# preserveAspectRatio 把画面 letterbox 居中 —— 300DPI PNG 的上下边缘各留下
# ~2px 全透明带，而审计的两条既有判据（viewBox vs 版心、文字出界）都看不见它。
# 修复是两半：渲染侧 background_color 填白兜底（交付像素正确），审计侧
# SVG-ASPECT finding 点名叫作者对齐比率（致因不藏着）。


def test_render_and_replay_scripts_fill_letterbox_white():
    """渲染脚本与冻结重放脚本都必须填白 —— 重放要复现同一份像素。"""
    from nodes.postprocess.svg_render import _render_script
    from nodes.postprocess.tools.illustration import _frozen_svg_script

    assert 'background_color="white"' in _render_script(b"<svg/>", Path("/tmp/x.png"), 992)
    assert 'background_color="white"' in _frozen_svg_script("<svg/>", "figures/x.png", 992)


def test_audit_flags_physical_viewbox_aspect_mismatch():
    """width/height 物理比 ≠ viewBox 比 → SVG-ASPECT finding（披露级，不拒）。"""
    from nodes.postprocess.svg_audit import audit_svg

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="170mm" height="200mm" '
        'viewBox="0 0 482 566">'
        '<text x="10" y="20" font-size="10">Hello</text></svg>'
    )
    report = audit_svg(svg)
    hits = [f for f in report["findings"] if f["collector"] == "SVG-ASPECT"]
    assert hits, "物理比 0.8500 vs viewBox 比 0.8516 必须被点名"
    assert "letterbox" in hits[0]["message"]


def test_audit_clean_when_physical_matches_viewbox():
    """物理比与 viewBox 比对齐时，不该有 SVG-ASPECT finding。"""
    from nodes.postprocess.svg_audit import audit_svg

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="170mm" height="200mm" '
        'viewBox="0 0 482 567.06">'
        '<text x="10" y="20" font-size="10">Hello</text></svg>'
    )
    report = audit_svg(svg)
    assert not [f for f in report["findings"] if f["collector"] == "SVG-ASPECT"]


def test_audit_letterbox_check_ignores_viewbox_only_svg():
    """没有 width/height 的 SVG（viewBox 单独决定）不触发这条判据。"""
    from nodes.postprocess.svg_audit import audit_svg

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 480">'
        '<text x="10" y="20" font-size="10">Hello</text></svg>'
    )
    report = audit_svg(svg)
    assert not [f for f in report["findings"] if f["collector"] == "SVG-ASPECT"]


# ── 审查修复回归（2026-10-20，第一/二类）─────────────────────────────────


def test_data_family_pins_to_matplotlib_track():
    """quantitative/composite 不带 source_artifact_ids 也钉在数据轨：
    数据可以在 render 时才绑（既有用法），不能因「纯文字」翻轨。"""

    from nodes.postprocess import contracts as _contracts
    from nodes.postprocess.figure_contract import ASSERTED_FAMILIES

    assert route_track({"asset_kind": "quantitative", "intent": "画延迟对比"}) == "matplotlib"
    assert route_track({"asset_kind": "composite", "intent": "画对比"}) == "matplotlib"
    # 纯文字 schematic/auto 仍走 SVG 轨（用户拍板不变）
    assert route_track({"asset_kind": "schematic", "intent": "画一个拓扑图"}) == "svg"
    assert route_track({"intent": "画一个拓扑图"}) == "svg"
    # 词表漂移钉：钉轨集合与断言家族同一份
    assert set(_contracts._DATA_TRACK_KINDS) == set(ASSERTED_FAMILIES)


def test_render_figure_rejects_illustration_family(tmp_path):
    """家族闸（H1）：illustration 合同的断言对 SVG DOM 求值，render_figure
    没有 DOM 可读 —— 从这道门铸出来等于断言从未看过图。"""

    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    assert declared["status"] == "success"
    result = asyncio.run(execute(
        "render_figure", state,
        request_id="topo", contract_id=declared["contract_id"],
        code="import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nax.plot([1], [1])\nfig.savefig('x.png')",
        output_files=["x.png"], output_name="x", source_artifact_ids=[],
        caption="c", alt_text="a",
    ))
    assert result["status"] == "error"
    assert "render_illustration" in result["error"]


def test_enhance_finds_frozen_svg_and_redacts_api_key(tmp_path, monkeypatch):
    """H5+H6：enhance 必须读得到冻结 SVG（{name}.{tag}.svg 而不是从不存在的
    {name}_render.svg）；落账身份走 _public_identity，api_key 不进账本。"""

    from nodes.postprocess import enhancer

    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    minted = _render(state, svg, contract_id=declared["contract_id"])
    assert minted["status"] == "success"

    captured: dict = {}
    monkeypatch.setattr(enhancer, "enhancement_identity", lambda: {
        "provider": "openai_compatible", "base_url": "http://x", "model": "m",
        "api_key": "sk-secret", "source": "config_file",
    })

    async def fake_enhance(state, *, png_path, svg_text, mode, art_style, out_dir, keep_text, timeout_s):
        captured["svg_text"] = svg_text
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        variant = out / "v1.png"
        variant.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return {
            "status": "success",
            "variants": [{"path": str(variant), "bytes": 12}],
            "usage": {},
            "mode": "code2prompt",
        }

    monkeypatch.setattr(enhancer, "enhance", fake_enhance)
    result = asyncio.run(execute("enhance_figure", state, figure_id=minted["figure_id"]))
    assert result["status"] == "success", result.get("error")
    assert captured["svg_text"] and "<svg" in captured["svg_text"]
    record = state.read_artifact(result["asset_id"])
    enhancement = record["metadata"]["enhancement"]
    assert "api_key" not in enhancement["identity"]
    assert enhancement["identity"]["model"] == "m"
    assert enhancement["mode_effective"] == "code2prompt"


def test_remint_does_not_overwrite_previous_evidence(tmp_path):
    """M6：同名重铸，两版的冻结 SVG / 重放脚本 / 交付 PNG 都留在盘上，
    旧记录的 content_hash 仍与盘上字节一致。"""

    from nodes.postprocess.contracts import hash_file

    svg, contract = _topology()
    state = _state(tmp_path)
    declared = _declare(state, contract)
    first = _render(state, svg, contract_id=declared["contract_id"])
    assert first["status"] == "success"
    svg2 = svg.replace("Network Topology", "Network Topology v2")
    second = _render(state, svg2, contract_id=declared["contract_id"])
    assert second["status"] == "success"

    p1 = Path(first["files"][0]["absolute_path"])
    p2 = Path(second["files"][0]["absolute_path"])
    assert p1 != p2 and p1.is_file() and p2.is_file()
    assert hash_file(p1) == first["files"][0]["content_hash"]
    assert hash_file(p2) == second["files"][0]["content_hash"]


def test_font_size_inherits_from_ancestors():
    """M3：font-size 沿 DOM 树继承；pt 按 CSS 4/3 换算；% 相对继承值。
    继承读数是印刷字号下限（拒绝级闸）的输入 —— 读错就是闸错。"""

    from nodes.postprocess.svg_audit import _iter_texts, parse_svg

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" font-size="20">'
        '<g font-size="50%"><text x="1" y="1">half</text></g>'
        '<text x="1" y="2" font-size="12pt">points</text>'
        '<text x="1" y="3">plain</text>'
        '<g><text x="1" y="4" style="font-size: 8px">styled</text></g>'
        '</svg>'
    )
    sizes = {text: size for _el, text, size in _iter_texts(parse_svg(svg))}
    assert sizes["half"] == 10.0    # 20 的 50%
    assert sizes["points"] == 16.0  # 12pt = 16 user units
    assert sizes["plain"] == 20.0   # 继承根 svg
    assert sizes["styled"] == 8.0   # style 属性也认


def test_dom_group_nesting_captures_parent_and_two_moment_consistency():
    """M11 收口：extract_dom_structure 从 DOM 嵌套抽出 parent（与合同
    groups[].parent 同形）；同一条 group 选择器断言在 declare（合同组）
    与铸造（DOM 组）两个时刻给出同一个答案。"""

    from nodes.postprocess.figure_contract import (
        evaluate_assertions,
        evaluate_svg_dom_assertions,
        normalize_contract,
    )
    from nodes.postprocess.svg_audit import extract_dom_structure, parse_svg

    contract = normalize_contract({
        "title": "嵌套组",
        "nodes": [
            {"id": "cpu", "label": "CPU", "role": "chip"},
            {"id": "g0", "label": "GPU0", "role": "gpu"},
            {"id": "g1", "label": "GPU1", "role": "gpu"},
        ],
        "groups": [
            {"id": "srv", "label": "服务器", "ranks": [["cpu"]]},
            {"id": "dom", "label": "加速域", "ranks": [["g0", "g1"]], "parent": "srv"},
        ],
        "edges": [{"from": "cpu", "to": "g0"}, {"from": "cpu", "to": "g1"}],
        "assertions": [
            {"id": "c1", "kind": "node_count", "selector": {"group": "srv"},
             "equals": 3, "derived_from": "服务器含三个组件"},
        ],
    }, family="illustration")
    contract_side = evaluate_assertions(contract)[0]
    assert contract_side["holds"] and contract_side["actual"] == 3

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
        '<g data-group="srv">'
        '<rect data-role="chip" data-node="cpu" x="1" y="1" width="10" height="10"/>'
        '<g data-group="dom">'
        '<rect data-role="gpu" data-node="g0" x="20" y="1" width="10" height="10"/>'
        '<rect data-role="gpu" data-node="g1" x="40" y="1" width="10" height="10"/>'
        '</g></g>'
        '<path data-edge-from="cpu" data-edge-to="g0" d="M11 6 H20"/>'
        '<path data-edge-from="cpu" data-edge-to="g1" d="M11 6 H40"/>'
        '</svg>'
    )
    structure = extract_dom_structure(parse_svg(svg))
    groups = {g["id"]: g for g in structure["groups"]}
    assert groups["dom"]["parent"] == "srv"
    assert groups["srv"]["parent"] is None
    dom_side = evaluate_svg_dom_assertions(
        contract,
        dom_nodes=structure["nodes"],
        dom_edges=structure["edges"],
        dom_groups=structure["groups"],
    )[0]
    assert dom_side["actual"] == contract_side["actual"] == 3

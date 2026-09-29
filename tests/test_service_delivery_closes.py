"""服务的交付必须闭环 —— 需求方等得到货，而且拿到的地图能用。

2026-08-04 e2e8 实测：writing 缺图，正确地派了 postprocess（需求写得非常规范：
3 张图批量、intent+mode 分离、data 图带 source_artifact_id + extraction）。
服务也画对了，六个机械 flag 全绿、真 PNG 落盘。

**但交付出去的论文里是 `[Figure placeholder]`。**

两个框架缺陷叠在一起：

① writing 传了 `background=true`，框架回它"已在后台启动……**不要**轮询等待"。
   它照做：写个占位框（placeholder.tex → compile_latex → 装进 figures/），
   编译，收工。半小时后真图才画好，稿子早已定稿。
   —— 服务节点的返回值就是调用方要的东西，它天生不能异步。

② 就算它想等，也装不上：figure artifact 里写的是
   `outputs/postprocess/figures/burden_vs_coverage.png`，相对**子 run 的根**。
   回填给 writing 时字符串原样搬过去，在父 run 里指向不存在的地方。

两条都修在框架层，不改 writing 的提示词 —— 提示词那种堵法已经被绕开过一次：
writing 的 harness 白纸黑字"不得伪造图片占位符"，它写 .tex 编译成 PDF 就绕过去了。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from shared.tools.run_node import _translate_run_relative_paths


# ── ① 服务节点不许异步 ──────────────────────────────────────────────────

def _run_node(state, **kw):
    from shared.tools.run_node import _run_node_tool as impl
    # 派发前必须先跟用户说一句（见 test_orchestrator_speaks_before_dispatching）。
    # 本文件测的是交付/契约门，不测那句话本身，所以在包装里统一给个占位。
    kw.setdefault("user_note", "测试派发")
    return asyncio.run(impl(state, **kw))


@pytest.fixture()
def parent(tmp_path) -> State:
    st = State.new(node_type="writing", base_dir=tmp_path, project_id="p1")
    # callable_nodes 白名单平时由 agent_loop 启动时注入；单测里手动给上，
    # 否则调用在更早的白名单门就被拒了，测不到 background 这一层。
    st.hook_state["_callable_nodes"] = ["postprocess", "literature", "data", "experiment"]
    return st


def test_service_node_background_is_allowed_and_the_debt_is_recorded(parent, monkeypatch):
    """判决拆除第三波（run_node:1839 降格）：服务节点 background 不再被拒 —— 送达口
    （_report_background_done → _inject_to_parent）是存在的，「拿不到货」是预测不是
    物理事实。放行，但账要记：pending_service_results 登记「服务结果未到不得定稿」。
    墙加回去这条转红。"""
    import json

    async def _fake_bg(*a, **k):
        return None

    monkeypatch.setattr("shared.tools.run_node._run_child_background", _fake_bg)
    res = _run_node(parent, node_type="postprocess",
                    node_inputs={"figure_requests": []}, background=True)
    assert res["status"] == "started_background", res
    assert res["service_result_pending"]["node_type"] == "postprocess"
    assert "不得定稿" in res["note"]
    ledger = parent.hook_state["pending_service_results"]
    assert ledger and ledger[0]["node_type"] == "postprocess"
    events = [json.loads(l) for l in parent.transcript_path.read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "service_result_pending" for e in events)


def test_service_result_arrival_clears_the_debt(parent):
    """到货即消：后台回报一到，账销掉、见证 service_result_delivered。"""
    import json
    from shared.tools.run_node import _report_background_done

    parent.hook_state["pending_service_results"] = [
        {"node_type": "postprocess", "sub_run_id": "x", "requested_by_run_id": parent.run_id}]
    _report_background_done(parent, "postprocess",
                            {"run_id": "child-1", "status": "completed", "turns": 3},
                            {"imported_artifacts": []})
    assert parent.hook_state["pending_service_results"] == []
    events = [json.loads(l) for l in parent.transcript_path.read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "service_result_delivered" for e in events)


def test_non_service_node_may_still_background(parent, monkeypatch):
    """长时研究任务照旧可以后台 —— 这条限制只针对服务节点，不是把 background 废掉。"""
    started = {}

    async def _fake_bg(*a, **k):
        started["yes"] = True

    monkeypatch.setattr("shared.tools.run_node._run_child_background", _fake_bg)
    # v2.0：literature 已服务化，非服务的长时任务用 experiment 举例
    res = _run_node(parent, node_type="experiment",
                    node_inputs={"experiment_spec": "x"}, background=True)
    assert res["status"] != "error" or "服务" not in res.get("error", "")


def test_service_node_sync_is_not_blocked(parent):
    """同步调服务不受影响 —— 拒的是异步，不是这个节点本身。

    这里不 mock 执行层：只要**没被我这道门拦下**就算过。真跑会因为单测环境
    没有 LLM 凭证而报别的错 —— 那恰恰证明它越过了这道门。
    """
    try:
        res = _run_node(parent, node_type="postprocess",
                        node_inputs={"figure_requests": []}, background=False)
    except Exception:
        return   # 走到执行层才炸 = 没被这道门拦住
    assert not (res.get("status") == "error" and "服务" in res.get("error", ""))


# ── ② 跨 run 路径翻译 ───────────────────────────────────────────────────

@pytest.fixture()
def child_run(tmp_path):
    """模拟 postprocess 子 run：图落在 outputs/postprocess/figures/ 下。"""
    root = tmp_path / "child"
    figs = root / "outputs" / "postprocess" / "figures"
    figs.mkdir(parents=True)
    (figs / "burden_vs_coverage.png").write_bytes(b"\x89PNG fake")
    return root


def _fig_record() -> dict:
    rel = "outputs/postprocess/figures/burden_vs_coverage.png"
    return {
        "type": "figure",
        "name": "burden_vs_coverage",
        "content": f"![burden]({rel})\n\n**Figure:** …",
        "metadata": {"image_path": rel, "mode": "data", "chart_type": "scatter"},
    }


def test_relative_path_becomes_resolvable(child_run):
    """事故本体：父 run 拿到的路径必须自己能解析得到。"""
    out, fixes = _translate_run_relative_paths(_fig_record(), child_run)
    p = out["metadata"]["image_path"]
    assert Path(p).is_absolute()
    assert Path(p).is_file()
    assert fixes


def test_content_and_metadata_stay_consistent(child_run):
    """正文里的引用跟着一起改 —— 否则 metadata 说一套正文说另一套。"""
    out, _ = _translate_run_relative_paths(_fig_record(), child_run)
    assert out["metadata"]["image_path"] in out["content"]
    # 绝对路径本身含相对串，所以查的是"裸相对引用"没了
    assert "](outputs/" not in out["content"]


def test_only_translates_paths_that_really_exist(child_run):
    """不猜、不造：解析不到真实文件的字符串原样保留。"""
    rec = {"type": "figure", "name": "x", "content": "",
           "metadata": {"image_path": "outputs/nope/missing.png",
                        "chart_type": "scatter", "mode": "data"}}
    out, fixes = _translate_run_relative_paths(rec, child_run)
    assert out["metadata"]["image_path"] == "outputs/nope/missing.png"
    assert not fixes


def test_long_metadata_prose_does_not_crash_the_import(child_run):
    """issue #395-1：一段长 metadata 描述不是路径，更不能把导入炸掉。

    现场：Study1 实验5 子 run 已完成并产出 clean_results 等四类产物，父
    orchestrator 导入时 `OSError: [Errno 36] File name too long` —— 出错的
    "路径"是 `Synthetic settling pilot dataset: 225 terminal velocity …` 这样
    一段人类可读文字。pathlib 的 is_file() 只吞 ENOENT 那几类，ENAMETOOLONG
    直接抛穿，子 run 完成了父级却丢了全部产物。
    """
    prose = ("Synthetic settling pilot dataset: 225 terminal velocity + "
             "225 regime prediction records, " + "covering Re from 0.1 to 1000, " * 12)
    assert len(prose) > 255  # 必须真的超过单段文件名上限，否则测不到
    rec = {"type": "clean_results", "name": "settling",
           "content": prose, "metadata": {"description": prose, "n_records": 450}}
    out, fixes = _translate_run_relative_paths(rec, child_run)
    assert out["metadata"]["description"] == prose
    assert not fixes


def test_absolute_paths_untouched(child_run):
    already = str(child_run / "outputs" / "postprocess" / "figures" / "burden_vs_coverage.png")
    rec = {"type": "figure", "name": "x", "content": "",
           "metadata": {"image_path": already}}
    out, fixes = _translate_run_relative_paths(rec, child_run)
    assert out["metadata"]["image_path"] == already
    assert not fixes


def test_non_path_strings_untouched(child_run):
    """chart_type='scatter' 不是路径，不许被瞎改。"""
    out, _ = _translate_run_relative_paths(_fig_record(), child_run)
    assert out["metadata"]["chart_type"] == "scatter"
    assert out["metadata"]["mode"] == "data"


def test_escape_outside_child_root_refused(child_run, tmp_path):
    """../.. 逃逸出子 run 根的不翻译 —— 别把父 run 的文件认成子 run 的产出。"""
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    rec = {"type": "figure", "name": "x", "content": "",
           "metadata": {"image_path": "../secret.png"}}
    out, fixes = _translate_run_relative_paths(rec, child_run)
    assert out["metadata"]["image_path"] == "../secret.png"
    assert not fixes


def test_nested_lists_and_dicts_translated(child_run):
    rel = "outputs/postprocess/figures/burden_vs_coverage.png"
    rec = {"type": "figure", "name": "x", "content": "",
           "metadata": {"figures": [{"path": rel}], "paths": [rel]}}
    out, _ = _translate_run_relative_paths(rec, child_run)
    assert Path(out["metadata"]["figures"][0]["path"]).is_file()
    assert Path(out["metadata"]["paths"][0]).is_file()


def test_no_metadata_does_not_crash(child_run):
    out, fixes = _translate_run_relative_paths(
        {"type": "figure", "name": "x", "content": "hi"}, child_run)
    assert out["metadata"] == {}
    assert not fixes


def test_translation_is_recorded_for_audit(tmp_path, child_run, monkeypatch):
    """改写了什么必须留痕 —— 静默重写路径比不重写更难查。"""
    out, fixes = _translate_run_relative_paths(_fig_record(), child_run)
    assert len(fixes) == 1
    assert "→" in fixes[0]


# ── ③ 接线：翻译器必须真的在回填路径上 ──────────────────────────────────

def test_backfill_actually_calls_the_translator(tmp_path, child_run, parent):
    """变异测试逼出来的：上面那些只测了**函数**，没测它有没有被接上。

    把 `_import_required_outputs` 里那一行删掉，函数级测试全绿 —— 而事故照旧。
    今天一整天修的都是这个形状（机制存在但没接到路径），这里不能自己再犯一次：
    走完整回填路径，断言父 run 拿到的就是能解析的绝对路径。
    """
    from core.artifact_provenance import produced
    from core.harness import NodeHarness
    from core.ledger import RecordStore
    from shared.tools.run_node import _import_required_outputs

    # 子 run 的 figure 记录：原生文件 + 它的 run 本地账本一行（回填只认账本）
    rec = _fig_record()
    RecordStore(child_run / "artifacts", child_run / "records.jsonl").save(
        artifact_id="figure__burden_vs_coverage", artifact_type=rec["type"],
        name=rec["name"], content=rec["content"], metadata=rec["metadata"],
        directory=child_run / "artifacts", created_at="2026-09-12T00:00:00+00:00",
        provenance=produced("postprocess", "child-1"),
        produced_by_node_type="postprocess", produced_by_run_id="child-1",
        by_node="postprocess", by_run="child-1",
    )

    child_summary = {
        "run_id": "child-1", "node_type": "postprocess", "status": "completed",
        "state_dir": str(child_run),
        "artifacts": [{"id": "figure__burden_vs_coverage", "type": "figure",
                       "name": "burden_vs_coverage"}],
    }
    harness = NodeHarness(node_type="postprocess", version="1.0",
                          required_output_artifact_types=["figure"])

    _import_required_outputs(parent, child_summary, harness)

    got = parent.read_artifact("figure__burden_vs_coverage")
    assert got is not None, "回填本身没发生"
    p = got["metadata"]["image_path"]
    assert Path(p).is_absolute(), f"回填后仍是子 run 坐标系的相对路径：{p}"
    assert Path(p).is_file(), f"父 run 解析不到这张图：{p}"


def test_dispatch_rejects_input_keys_that_miss_the_declared_contract(parent):
    """派发处机械校验输入契约 —— 不让调用方靠背参数名。

    实测事故（2026-08-07 UI）：orchestrator 直调 postprocess 传 `figure_spec`，
    而该节点所有 visual 工具要 `visual_requests`。服务用 report_blocker 如实
    报了 missing_input，调用方没消费、原样重试 4 次，整条 run 被 cancel ——
    烧掉 4 个 12 轮子 run 才发现是参数名不对。

    根因不是 prompt 少写一句：expected_inputs 本来就声明在 harness 里、也被
    loader 读进 NodeHarness，只是 run_node 从不把它告诉调用方。契约是声明
    数据，派发处就该拿它把关。
    """
    res = _run_node(parent, node_type="postprocess",
                    node_inputs={"figure_spec": "画个负载均衡对比图"})
    assert res["status"] == "error"
    assert "visual_requests" in str(res.get("expected_inputs"))
    assert res["received_keys"] == ["figure_spec"]
    assert "不要重试同样的调用" in res["error"]


def test_dispatch_allows_declared_key_with_extra_fields(parent):
    """命中任一声明键就放行 —— 额外自定义字段不该被拦（判据保守，防误伤）。

    判据：调用**穿过**契约门真的去起子节点了（测试环境没有 LLM 凭证，于是
    在执行阶段抛错）—— 抛在执行阶段本身就证明契约门放行了。

    ⚠️ 这里锚**异常类型**，不锚报错文案。原先锚的是 "LLM_API_KEY" 这个词，
    而凭据从 env 收进模型角色之后那句话就不再出现（现在报的是"角色 reasoning
    没有可用后端"）—— 契约门一如既往地放行了，红的只是文案。测试要问的是
    "有没有走到需要模型的那一步"，`ModelRoleUnavailable` 正是那一步的机械信号。
    """
    from core.model_roles import ModelRoleUnavailable

    with pytest.raises(ModelRoleUnavailable):
        _run_node(parent, node_type="postprocess",
                  node_inputs={"visual_requests": [{"intent": "x"}], "note": "自定义"})


def test_callers_are_told_the_callee_input_contract_mechanically():
    """可调子节点的输入契约必须机械注入调用方 —— 不靠 prompt 背参数名。

    实测事故（2026-08-07 UI 真机）：orchestrator 直调 postprocess，先传
    figure_spec、再传 research_question，都不对（该节点声明 visual_requests）。
    它无处可查：契约声明在**被调节点**的 harness 里，调用方 context 里没有。
    writing 能调对，只因 owner 在 writing 的 prompt 里硬写了一句
    visual_requests —— 那是 owner 知识，换个调用方就没有。

    v2.1 把服务化推开后，调用方从"少数 owner 写死"变成"任何有 callable_nodes
    的节点"，靠 prompt 传契约不再成立。契约是声明数据，谁能调就给谁看。
    """
    import tempfile
    from pathlib import Path
    from core.state import State
    from core.loop_hooks_builtin import _callee_contracts_on_turn_start

    class _Ctx:
        def __init__(self, state):
            self.state = state
            self.turn = 1

    with tempfile.TemporaryDirectory() as td:
        state = State.new(node_type="experiment", base_dir=Path(td), project_id="p_cc")
        state.hook_state["_callable_nodes"] = ["data"]
        messages = _callee_contracts_on_turn_start(_Ctx(state))
        assert messages, "有 callable_nodes 就必须注入契约"
        body = messages[0].content
        assert "`data`" in body and "spec" in body
        # 每个 state 只注入一次，不刷屏
        assert _callee_contracts_on_turn_start(_Ctx(state)) is None

        # orchestrator 的 "*" 要展开成真实节点，且必须覆盖出事的那个契约
        orch = State.new(node_type="_orchestrator", base_dir=Path(td), project_id="p_cc2")
        orch.hook_state["_callable_nodes"] = ["*"]
        body = _callee_contracts_on_turn_start(_Ctx(orch))[0].content
        assert "visual_requests" in body, "postprocess 的输入契约必须在 orchestrator 视野里"
        assert "`postprocess`（service）" in body
        assert "_reviewer" not in body, "架构私有节点不进清单"


def test_mode_branching_covers_both_contract_and_behaviour():
    """服务的请求模式必须**同时**改交付契约和节点行为 —— 只改一半不省钱。

    实测（2026-08-07 真机两轮）：
      第一轮只声明了按 mode 分流的输出契约 → orchestrator 正确传了
      mode=targeted_lookup / max_papers=5，交付物也确实换成了 evidence
      package，但 **token 几乎没降（1.05M → 0.97M）** —— 因为节点 prompt 里
      只有一套完整综述工作流，模型照跑不误。
    契约决定'交什么'，prompt 决定'怎么干'。缺任一半，省的只是最后一步。
    """
    import yaml
    from pathlib import Path
    from core.loader import load_harness

    harness = load_harness("literature")
    # 契约侧：定向模式不得再要求完整综述两件套
    assert harness.required_outputs_for({"mode": "targeted_lookup"}) == [
        "literature_evidence_package"
    ]
    assert harness.required_outputs_for({"mode": "landscape"}) == [
        "survey_report", "literature_index"
    ]
    assert harness.required_outputs_for({}) == ["survey_report", "literature_index"]

    # 行为侧：prompt 必须真的按 mode 分流，且写明定向模式不跑完整流程
    raw = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "nodes/literature/harness.yaml")
        .read_text(encoding="utf-8")
    )
    prompt = raw["system_prompt"]
    for mode in ("targeted_lookup", "contradiction_check", "method_lookup", "landscape"):
        assert mode in prompt, f"prompt 未提及 mode={mode}"
    assert "max_papers" in prompt
    assert "别跑完整工作流" in prompt


# test_mode_branching_reaches_every_layer_that_can_fail_a_run 已随 QC 层删除（2026-08-22）：「能 fail 一个 run 的层」不再包含 QC；模式分流的其余层由同文件其它用例覆盖

"""data 交不出货时，experiment 必须收得下、并且分得清该做什么。

## 病例（2026-08-31，PR #715 合并后才发现）

data 重写成确定性流水线时把终态词表换了：

    写出:   needs_input | externally_blocked | fatal
    收货端: recoverable_blocked | blocked | incomplete      ← 交集为空

后果不是报错难看，是**闭不了环**：experiment 每次收 data 的 blocked report 都拿到
"blocked report must record a terminal/recoverable Data failure status"，记不了账；
而 experiment 那边还有硬门禁"缺正式前处理输入不得自行制造"。这正是 issue #166
修过的 experiment↔data 踢皮球死锁的同一个形状，只是这次卡在记账那一步。

CI 抓不到：两边各自的测试都绿，**没有一条测试跨这道边界**。而且它只在 data
**失败**的路径上出现 —— 恰好是最需要闭环的那条路。

## 这个文件锁两件事

1. 词表**机械对齐**（`test_every_status_data_can_emit_has_a_route`）：判据直接读
   两边的声明求子集，不抄名单。词表哪天再漂一次，这条当天红。
2. 三档**路由正确**：`needs_input` / `externally_blocked` 不是终态、不开 fallback；
   只有 `fatal`（或同一个 spec 两个 data run 都卡在可恢复档）才开。

为什么第 2 条重要：fallback = 允许 experiment 自己造输入并把整个 run 标成
`analysis_eligible=False`。为"少一个参数"付这个代价是荒唐的 —— 老词表把三种
情况糊成一坨，收货端只能一律当"data 彻底不行了"。

样本用 **data 自己的 writer** 生成（`save_preprocessing_blocked_report`），不手搓
JSON —— 手搓的样本只能证明我对格式的理解自洽。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.state import State
from nodes.experiment.tools.contract_audit import (
    DATA_RECOVERABLE_OUTCOMES, DATA_TERMINAL_OUTCOMES,
    _authorize_experiment_fallback, _record_data_delivery_outcome,
    _validate_data_request_spec,
)


def _service_spec() -> str:
    return json.dumps({
        "request_kind": "preprocessing_service_request",
        "requesting_stage": "toolchain_build",
        "purpose": "构建后需要一份网格来验证求解器能读入并跑通一步",
        "target_software": "OpenFOAM 11 (simpleFoam)",
        "scientific_parameters": "not_applicable",
        "required_assets": [{"name": "channel.msh", "format": "Gmsh", "purpose": "solver mesh"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    })


def _data_run_blocks_with(runs: Path, status: str, **payload) -> tuple[str, str]:
    """让 **data 自己**在一个真的子 run 里写一份 blocked report，返回 (run_id, artifact_id)。"""
    from nodes.data.blocked_delivery import save_preprocessing_blocked_report

    data_state = State.new("data", runs)
    record = save_preprocessing_blocked_report(
        data_state,
        name=f"preprocessing_{status}",
        payload={"status": status, "reason": f"probe:{status}", **payload},
    )
    return str(data_state.run_id), str(record["id"])


# ── 1. 词表机械对齐 ─────────────────────────────────────────────────────────

def test_every_status_data_can_emit_has_a_route() -> None:
    """data 能写出的每一个 status，收货端都得有一条路 —— 判据读声明，不抄名单。"""
    from nodes.data.blocked_delivery import REPORT_STATUSES

    routed = set(DATA_RECOVERABLE_OUTCOMES) | set(DATA_TERMINAL_OUTCOMES)
    unrouted = sorted(set(REPORT_STATUSES) - routed)
    assert not unrouted, (
        f"data 会写出这些 status，而 experiment 的收货端一条路都没有：{unrouted}。"
        f"收货端认得的：{sorted(routed)}。"
        "两边的词表分叉不会报错，只会让 data 每次交不出货时 experiment 记不了账。")


def test_the_two_buckets_do_not_overlap() -> None:
    """一个 status 不能既可恢复又是终态 —— 否则路由取决于代码里谁先判。"""
    assert not (set(DATA_RECOVERABLE_OUTCOMES) & set(DATA_TERMINAL_OUTCOMES))


#: 判决拆除 O6（ca:1269 降格，2026-08-31）：可恢复档下 fallback 授权不再被拒，
#: 但必须如实标注 corroborated=False（没有终态 blocker 佐证）。断言落在
#: corroborated 上而不是笼统的 status —— 授权语义变了，见证语义不能含糊。
def _fallback_authorization_is_uncorroborated(state, spec_id: str) -> bool:
    result = asyncio.run(_authorize_experiment_fallback(state, spec_id))
    return result.get("status") == "success" and result.get("corroborated") is False


# ── 2. 三档路由 ─────────────────────────────────────────────────────────────

def _record(state, spec_id: str, runs: Path, status: str, **payload):
    """把 data 自己写的 report 装进一份**受管派发回执**之后再记账。

    node20 的 23a0fe78 给 `record_data_delivery_outcome` 加了 provenance 门：报告
    必须来自本 spec 的 `dispatch_data_request` 回执 —— 裸 run_node 的结果或任意历史
    Data 报告都不能授权 fallback。这道门对**两档都生效**，不能只给可恢复档放行：
    两条伪造的可恢复记录会通过下面"两个不同 run"的升级规则自动变成 fatal，等于
    绕开门把 fallback 开了。

    所以这里如实搭出那份回执。报告本体仍由 **data 自己的 writer** 生成（不手搓），
    只是把它落进一个真的直属子 run —— 生产里"标成直属子 run"这一步由 run_node 做。
    """
    run_id, report_id = _data_run_blocks_with(runs, status, **payload)
    _install_dispatch_receipt(state, spec_id, runs, run_id, report_id)
    return asyncio.run(_record_data_delivery_outcome(
        state, spec_id, data_run_id=run_id, blocked_report_id=report_id)), run_id


def _install_dispatch_receipt(state, spec_id: str, runs: Path, run_id: str, report_id: str):
    """如实搭出 dispatch_data_request 会写下的那份回执（见 _record 的说明）。"""
    from nodes.experiment.tools.contract_audit import _data_dispatch_material
    from nodes.experiment.tools.input_delivery import (
        load_input_delivery_ledger, save_input_delivery_ledger,
    )

    transcript = runs / run_id / "transcript.jsonl"
    existing = transcript.read_text(encoding="utf-8") if transcript.is_file() else ""
    transcript.write_text(
        json.dumps({"event": "run_start", "node_type": "data",
                    "parent_run_id": state.run_id}) + "\n" + existing,
        encoding="utf-8",
    )

    ledger = load_input_delivery_ledger(state)
    entry = ledger["specs"][spec_id]
    material = _data_dispatch_material(spec_id, entry["request"])
    entry["delivery"]["data_dispatch_receipt"] = {
        "schema_version": material["schema_version"],
        "spec_id": spec_id,
        "request_sha256": material["request_sha256"],
        "payload_sha256": material["payload_sha256"],
        "child_run_id": run_id,
        "child_node_type": "data",
        "child_status": "incomplete",
        "child_artifact_ids": [report_id],
    }
    save_input_delivery_ledger(state, ledger)


def test_needs_input_is_not_terminal_and_keeps_the_fallback_shut(tmp_path: Path) -> None:
    """少一个参数不该换来"允许 experiment 自己造输入"。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    result, _ = _record(state, spec["spec_id"], runs, "needs_input",
                        resume_contract={"missing_fields": ["reynolds_number"]})

    assert result["status"] == "success", result
    assert result["outcome"] == "recoverable"
    assert result["data_terminally_blocked"] is False
    assert result["detail"]["missing_fields"] == ["reynolds_number"]
    assert "重新 run_node" in result["next_step"]
    assert state.hook_state["input_delivery_state"][spec["spec_id"]]["data_terminally_blocked"] is False
    # 降格后：授权照给，但「没有终态 blocker」必须如实进账（corroborated=False）
    assert _fallback_authorization_is_uncorroborated(state, spec["spec_id"])


def test_externally_blocked_is_not_terminal_either(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    result, _ = _record(state, spec["spec_id"], runs, "externally_blocked",
                        failure_category="environment_required")

    assert result["outcome"] == "recoverable"
    assert result["data_terminally_blocked"] is False
    assert "环境" in result["next_step"]
    assert _fallback_authorization_is_uncorroborated(state, spec["spec_id"])


#: 一条合格的获取尝试记录：指名来源 + 指名失败（experiment 侧验收 schema）。
_TRIED_CDS = {"acquisition_attempts": [{
    "url": "https://cds.climate.copernicus.eu/api/v2/resources/reanalysis-era5",
    "failure_type": "egress_denied",
    "evidence_path": "logs/era5_fetch.stderr",
}]}


def test_fatal_with_acquisition_evidence_is_terminal(tmp_path: Path) -> None:
    """真终态：data 对具体来源试过并失败 → 一步判终态，证据摘要交给下游。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    result, _ = _record(state, spec["spec_id"], runs, "fatal", **_TRIED_CDS)

    assert result["outcome"] == "terminal"
    assert result["data_terminally_blocked"] is True
    assert state.hook_state["input_delivery_state"][spec["spec_id"]]["data_terminally_blocked"] is True
    # 归因/问人措辞从这里取 URL 和失败类型，不再只有一句"blocked"。
    assert result["detail"]["acquisition_attempts"] == [
        {"source": "https://cds.climate.copernicus.eu/api/v2/resources/reanalysis-era5",
         "failure": "egress_denied"}]


# ── 2b. 内容质量门（东亚 E2E 回归）：终态词 + 零获取证据 ≠ 数据不可得 ────────

def test_fatal_without_acquisition_evidence_is_not_terminal(tmp_path: Path) -> None:
    """planner 空转落成的 fatal 不得一步解锁 fallback，也不得说成用户缺数据。

    东亚 E2E 的原始病灶：Data 规划循环耗尽迭代、从未尝试任何下载，落盘的
    blocker 只有 status + stop_reason。改前它被当成数据终态阻塞 → 上游转述为
    "用户缺数据"/解锁 fallback。改后它是 Data 内部失败：记账、关 fallback、
    点名重派。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    result, _ = _record(state, spec["spec_id"], runs, "fatal",
                        stop_reason="max_iterations_reached")

    assert result["outcome"] == "recoverable", result
    assert result["data_terminally_blocked"] is False
    assert result["failure_class"] == "data_internal_failure_without_acquisition_evidence"
    assert result["detail"]["stop_reason"] == "max_iterations_reached"
    assert "内部失败" in result["next_step"]
    assert "用户" in result["next_step"]          # 明说"不是用户缺数据"
    assert "dispatch_data_request" in result["next_step"]
    assert _fallback_authorization_is_uncorroborated(state, spec["spec_id"])


def test_junk_attempt_entries_do_not_count_as_evidence(tmp_path: Path) -> None:
    """凑数的尝试记录（缺来源或缺失败）不算证据 —— 否则门形同虚设。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    result, _ = _record(state, spec["spec_id"], runs, "fatal",
                        acquisition_attempts=[
                            {},                                  # 空
                            {"url": "https://example.org/x"},    # 只有来源
                            {"failure_type": "timeout"},         # 只有失败
                            "https://example.org/y",             # 不是记录
                        ])

    assert result["outcome"] == "recoverable", result
    assert result["failure_class"] == "data_internal_failure_without_acquisition_evidence"
    assert _fallback_authorization_is_uncorroborated(state, spec["spec_id"])


def test_two_evidence_less_fatal_runs_escalate_to_terminal(tmp_path: Path) -> None:
    """解除路径：两个不同的 data run 都交不出获取证据 → 升终态，fallback 可开。

    没有这条，内容门就是新的死锁（planner 一直坏 = 永远出不去）。判据仍是
    "不同的 run 出现过两次"，机械可查。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    first, first_run = _record(state, spec["spec_id"], runs, "fatal",
                               stop_reason="max_iterations_reached")
    assert first["outcome"] == "recoverable"

    second, second_run = _record(state, spec["spec_id"], runs, "fatal",
                                 stop_reason="no_effective_progress")

    assert second_run != first_run
    assert second["outcome"] == "terminal", second
    assert second["detail"]["escalated_from"] == "fatal_without_acquisition_evidence"
    assert not _fallback_authorization_is_uncorroborated(state, spec["spec_id"])


def test_legacy_empty_blocker_no_longer_terminal_on_first_hit(tmp_path: Path) -> None:
    """存量旧词表 artifact（东亚 E2E 的原样）同样过内容门，不再一步终态。

    旧词表报告今天的 data writer 已写不出来（会被矫正成 fatal），只能来自
    存量 artifact / 旧 run 的 reconcile —— 所以这里如实手搭旧格式。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    data_state = State.new("data", runs)
    record = data_state.save_artifact(
        "preprocessing_blocked_report", "planning_contract_failure",
        json.dumps({"status": "recoverable_blocked",
                    "reason": "max_iterations_reached"}))

    _install_dispatch_receipt(state, spec["spec_id"], runs,
                              str(data_state.run_id), str(record["id"]))
    result = asyncio.run(_record_data_delivery_outcome(
        state, spec["spec_id"], data_run_id=str(data_state.run_id),
        blocked_report_id=str(record["id"])))

    assert result["status"] == "success", result
    assert result["outcome"] == "recoverable"
    assert result["failure_class"] == "data_internal_failure_without_acquisition_evidence"
    assert _fallback_authorization_is_uncorroborated(state, spec["spec_id"])


# ── 3. 可恢复档必须有解除路径（否则这道闸就是新的死锁）────────────────────

def test_a_second_data_run_still_recoverable_escalates_to_terminal(tmp_path: Path) -> None:
    """补过一次还是卡在同一档 → 按终态处理。

    判据是"**两个不同的 data run** 都卡在可恢复档"，机械可查；不是"模型说它试过了"。
    没有这条，`needs_input` / `externally_blocked` 就成了一个永远出不去的状态 ——
    单边硬门禁必须配解除路径，这是 issue #166 那次死锁的教训。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    first, first_run = _record(state, spec["spec_id"], runs, "externally_blocked")
    assert first["outcome"] == "recoverable"

    second, second_run = _record(state, spec["spec_id"], runs, "externally_blocked")

    assert second_run != first_run
    assert second["outcome"] == "terminal", second
    assert second["data_status"] == "fatal"
    assert second["detail"]["escalated_from"] == "externally_blocked"
    # 升级之后，授权的见证是有佐证的（corroborated=True）。
    escalated = asyncio.run(_authorize_experiment_fallback(state, spec["spec_id"]))
    assert escalated["status"] == "success"
    assert escalated["corroborated"] is True


def test_the_same_data_run_reported_twice_does_not_escalate(tmp_path: Path) -> None:
    """重复记同一份报告不算"试过两次" —— 否则模型多调一次工具就白拿到 fallback。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    run_id, report_id = _data_run_blocks_with(runs, "needs_input")
    _install_dispatch_receipt(state, spec["spec_id"], runs, run_id, report_id)

    for _ in range(3):
        result = asyncio.run(_record_data_delivery_outcome(
            state, spec["spec_id"], data_run_id=run_id, blocked_report_id=report_id))
        assert result["outcome"] == "recoverable", result
    assert _fallback_authorization_is_uncorroborated(state, spec["spec_id"])


# ── 4. 词表外的 status 要说清合法取值 ───────────────────────────────────────

def test_an_unknown_status_names_the_legal_values(tmp_path: Path) -> None:
    """判决拆除 O6（ca:1244 降格，2026-08-31）：词表外状态照记（按终态处理、
    corroborated=False），但合法取值仍必须送到调用方手上 —— 词表再漂一次时，
    现场必须自己说得清。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    data_state = State.new("data", runs)
    record = data_state.save_artifact(
        "preprocessing_blocked_report", "weird",
        json.dumps({"status": "totally_new_word"}))

    _install_dispatch_receipt(state, spec["spec_id"], runs,
                              str(data_state.run_id), str(record["id"]))
    result = asyncio.run(_record_data_delivery_outcome(
        state, spec["spec_id"], data_run_id=str(data_state.run_id),
        blocked_report_id=str(record["id"])))

    assert result["status"] == "success", result
    # ca:1244 降格：词表外状态不再被拒，如实记账 + 合法取值送到调用方手上。
    assert result["data_status"] == "totally_new_word"
    assert result["corroborated"] is False
    for legal in ("needs_input", "externally_blocked", "fatal"):
        assert legal in result["known_statuses"]
    # 但路由仍由内容门决定（bbad5bb0，东亚 E2E 病灶）：不认识的词 + 零获取证据
    # = Data 内部失败，按可恢复档重派，不凭一个陌生词解锁 fallback。
    # 上游此测试写于内容门之前，合并后由更严的那道门接管。
    assert result["outcome"] == "recoverable"
    assert result["data_terminally_blocked"] is False

"""图上印的数，在**铸图那一刻**就要等于它声称的出处（#439 重挂）。

PR#1007 把这道对账挂在 ``validate_figure_record`` 上，而它唯一的生产调用方在旧
writing 里 —— writing 重建（6ba051b14）把它删了，此后对账和「未运行」的披露
**哪儿都不发生**。重挂到每张图都走的那一步：``State.save_artifact`` 落 figure 记录
（render_figure 在那里铸）。research_runs/ 下 195 份真 figure 记录 0 份冻结过，
冻结门是一道没人走的闸。

这里的用例全部走真入口：没声明的那条跑真 ``render_figure``；有声明的那几条用
``State.save_artifact`` 落一份 render_figure 真铸出来的 metadata，加上
``annotated_values`` —— 也就是 postprocess 一旦开始填这个字段时会落的那份记录。

数字是 E2E v26（2D Ising vs Onsager，2026-08-13）现场的真值，不是造的：

    experiment 冻结的   fss_extrapolation/T_c_est = 2.2692871655836173   1.17σ supported
    postprocess 图上印的                            2.268776           2.8σ  refuted
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute
from shared.lib.publication_figures import (
    VALUE_CHECK_NOT_RUN,
    FigureContradictsItsSource,
)

bootstrap()

_CLEAN_RESULTS = "clean_results__2D_Ising_Tc_CleanResults"

#: v26 那份冻结 clean_results 的正文（节选同形）：头条数字在正文 JSON 里，
#: metadata 里只有一个四舍五入过的 T_c_est。
_V26_CONTENT = {
    "fss_extrapolation": {
        "method": "pairwise_crossing_1_over_L_extrapolation",
        "T_c_est": 2.2692871655836173,
        "sigma_bootstrap": 8.712013995950031e-05,
    },
    "verdict": {"H1": {"verdict": "validated", "normalized_deviation": 1.1726976525178274}},
}

#: postprocess 画 FSS 图时自己用加权拟合重算出来的 —— 只以像素存在的那个数。
_PRINTED_ON_THE_FIGURE = 2.268776


@pytest.fixture(autouse=True)
def _fake_sandbox(monkeypatch):
    """subprocess 替身跑真渲染代码（同 nodes/postprocess/tests/test_render_figure.py）。
    被测的是落账那一步，不是沙箱。"""
    from shared.tools.library import python_exec

    async def fake_execute_python(state, code, timeout=300, cwd=None, requirements=None, **_):
        from core.project_workspace import validate_tool_cwd

        workspace = validate_tool_cwd(state, cwd)
        workspace.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([sys.executable, "-c", code], cwd=workspace,
                              capture_output=True, text=True, timeout=timeout)
        out = {"status": "success" if proc.returncode == 0 else "error",
               "returncode": proc.returncode, "stdout_tail": proc.stdout[-3000:],
               "stderr_tail": proc.stderr[-1500:], "workspace": str(workspace)}
        if proc.returncode != 0:
            out["error"] = proc.stderr[-1500:] or f"exit {proc.returncode}"
        return out

    monkeypatch.setattr(python_exec, "_execute_python", fake_execute_python)


_CODE = (
    "import matplotlib\nmatplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "fig, ax = plt.subplots(figsize=(4, 3), dpi=150)\n"
    "ax.plot([0.0, 0.03, 0.06], [2.2693, 2.2687, 2.2681], marker='o')\n"
    "ax.set_xlabel('1/L'); ax.set_ylabel('T_c(L)')\n"
    "fig.tight_layout()\nfig.savefig('fss.png')\n"
)


def _postprocess_with_frozen_v26_source() -> State:
    """experiment 冻结的 clean_results 与要铸图的 postprocess 在同一个 run 里可读。"""
    state = State.new(node_type="postprocess", base_dir=Path(tempfile.mkdtemp()))
    saved = state.save_artifact(
        artifact_type="clean_results", name="2D_Ising_Tc_CleanResults",
        content=json.dumps(_V26_CONTENT, indent=2),
        metadata={"T_c_est": 2.269287, "H1_verdict": "validated"},
    )
    assert saved["id"] == _CLEAN_RESULTS
    state.mark_frozen(_CLEAN_RESULTS)
    return state


def _render(state: State, name: str = "fss_extrapolation") -> dict:
    result = asyncio.run(execute(
        "render_figure", state,
        output_name=name, caption="T_c(L) extrapolated to 1/L -> 0",
        alt_text="finite-size scaling of the crossing temperature",
        code=_CODE, output_files=["fss.png"], source_artifact_ids=[_CLEAN_RESULTS],
    ))
    assert result.get("status") == "success", result
    return result


def _declaring(state: State, value: float, **overrides) -> dict:
    """render_figure 真铸出来的 metadata + 一条 annotated_values 声明。"""
    minted = state.read_artifact(_render(state, name="template")["figure_id"])
    metadata = json.loads(json.dumps(minted["metadata"]))
    metadata.pop("annotated_values_check", None)
    annotation = {
        "label": "T_c", "value": value,
        "source_artifact_id": _CLEAN_RESULTS,
        "source_field": "fss_extrapolation/T_c_est",
        "tolerance": 1e-6,
    }
    annotation.update(overrides)
    metadata["annotated_values"] = [annotation]
    return metadata


def _mint(state: State, metadata: dict, **kwargs) -> dict:
    return state.save_artifact(
        artifact_type="figure", name="fss_extrapolation",
        content="![fss](fss.png)\n\nCaption: T_c(L) extrapolated", metadata=metadata,
        **kwargs,
    )


def test_render_figure_records_that_the_value_check_did_not_run():
    """今天 postprocess 不填 annotated_values —— 不拦，但缺席落进记录本身。

    重挂之前：真 render_figure 铸出的记录里没有任何一处说过「图文没对过账」，
    而唯一会说这句话的函数没有生产调用方。"""
    state = _postprocess_with_frozen_v26_source()
    record = state.read_artifact(_render(state)["figure_id"])
    check = record["metadata"].get("annotated_values_check")
    assert check == {"ran": False, "note": VALUE_CHECK_NOT_RUN}, (
        f"铸图那一步没有把「图文对账未运行」落进记录：{check!r}")


def test_the_v26_contradiction_is_refused_at_minting():
    state = _postprocess_with_frozen_v26_source()
    metadata = _declaring(state, _PRINTED_ON_THE_FIGURE)
    with pytest.raises(FigureContradictsItsSource) as caught:
        _mint(state, metadata)
    message = str(caught.value)
    assert "2.268776" in message and "2.2692871655836173" in message, (
        f"报错没把两个数一起说出来，人还得自己去翻：{message}")
    assert f"{_CLEAN_RESULTS}/fss_extrapolation/T_c_est" in message
    assert state.read_artifact("figure__fss_extrapolation") is None, (
        "自相矛盾的图记录照样落了账")


def test_a_figure_that_agrees_with_its_source_is_minted_and_says_so():
    """反作弊：对得上的必须放行，且记录写明比过了哪几个数。"""
    state = _postprocess_with_frozen_v26_source()
    saved = _mint(state, _declaring(state, 2.2692871655836173))
    check = state.read_artifact(saved["id"])["metadata"]["annotated_values_check"]
    assert check == {"ran": True, "checked": ["T_c"]}


def test_an_unreachable_source_says_which_one():
    """够不到出处 ≠ 对上了账；说出是哪一份够不到。"""
    state = _postprocess_with_frozen_v26_source()
    metadata = _declaring(state, _PRINTED_ON_THE_FIGURE,
                          source_artifact_id="clean_results__never_produced")
    with pytest.raises(FigureContradictsItsSource, match="够不到出处 clean_results__never_produced"):
        _mint(state, metadata)


def test_a_missing_field_path_says_which_path():
    state = _postprocess_with_frozen_v26_source()
    metadata = _declaring(state, _PRINTED_ON_THE_FIGURE,
                          source_field="fss_extrapolation/T_c_weighted")
    with pytest.raises(FigureContradictsItsSource,
                       match=f"出处 {_CLEAN_RESULTS} 里没有字段 fss_extrapolation/T_c_weighted"):
        _mint(state, metadata)


def test_a_forwarded_copy_is_not_reminted():
    """转发/回填是在搬一份既有记录：接收方 run 读不到出处不是这份记录的矛盾。
    （判据要是不看 provenance，这条会被「够不到出处」拒掉。）"""
    from core import artifact_provenance as provenance

    source = _postprocess_with_frozen_v26_source()
    metadata = _declaring(source, 2.2692871655836173)
    receiver = State.new(node_type="postprocess", base_dir=Path(tempfile.mkdtemp()))
    assert receiver.read_artifact(_CLEAN_RESULTS) is None
    saved = _mint(receiver, metadata, provenance=provenance.forwarded(
        provenance.produced("postprocess", source.run_id),
        via_node_type="writing", via_run_id=receiver.run_id))
    assert "annotated_values_check" not in receiver.read_artifact(saved["id"])["metadata"]

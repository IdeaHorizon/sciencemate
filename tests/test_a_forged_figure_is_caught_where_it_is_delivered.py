"""伪造的图、铸完又被换过的图，在**交付收集点**被抓（#1200，wangd 2026-10-21 裁决）。

``validate_figure_record`` 的伪造 / 指纹检查唯一的生产调用方是旧 writing 的 stage 图
工具，09-18 重建把它删了 —— 此后这些检查哪儿都不发生（只有测试在调）。裁决：不在
铸图时拦（cuikl 的约定：伪造记录照样落账、**事后**被抓，见
nodes/postprocess/tests/test_render_figure.py 的防伪段），挂在两个收集点：

  · writing 读图与图题那一步 —— ``nodes/writing/tools/w_dossier.build_dossier``；
  · run 收尾 —— ``core.executor.finalize_run``（本 run 交出去的路径）。

两处共用 ``shared.lib.publication_figures.audit_figure_files``。这里的用例全走真入口：
真图由 ``render_figure`` 真铸，伪造记录由 postprocess 的 ``State.save_artifact`` 落账
（与 cuikl 的用例同形），读图走 ``build_dossier``，收尾走 ``finalize_run``，State 绑真
worktree 且 run 根在 worktree 的缓存目录下（平台的真实形状）。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.agent_loop import LoopResult
from core.bootstrap import bootstrap
from core.executor import FIGURE_DELIVERY_AUDIT_KEY, finalize_run
from core.harness import NodeHarness
from core.project_workspace import bind_project_workspace
from core.state import State
from core.tool_registry import execute

bootstrap()


@pytest.fixture(autouse=True)
def _fake_sandbox(monkeypatch):
    """subprocess 替身跑真渲染代码（同 nodes/postprocess/tests/test_render_figure.py）。
    被测的是收集点，不是沙箱。"""
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


def _worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir(parents=True)
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"],
                ["git", "commit", "-qm", "init", "--allow-empty"]):
        subprocess.run(cmd, cwd=wt, check=True)
    return wt


def _bound(wt: Path, node_type: str, run_id: str) -> State:
    run_root = wt / ".research" / "cache" / "runtime" / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    state = State(run_id=run_id, node_type=node_type, root=run_root)
    bind_project_workspace(state, wt)
    return state


def _code(y: float) -> str:
    return (
        "import matplotlib\nmatplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots(figsize=(4, 3), dpi=150)\n"
        f"ax.plot([1, 2], [2, {y}], marker='o')\n"
        "ax.set_xlabel('x'); ax.set_ylabel('y')\n"
        "fig.tight_layout()\nfig.savefig('demo.png')\n"
    )


def _render(state: State, *, y: float = 4.0) -> dict:
    result = asyncio.run(execute(
        "render_figure", state,
        output_name="demo", caption="y grows with x", alt_text="line chart of y vs x",
        code=_code(y), output_files=["demo.png"], source_artifact_ids=[],
    ))
    assert result.get("status") == "success", result
    return result


def _forge(state: State, wt: Path) -> dict:
    """绕过 render_figure 直写一份「rendered_figure」—— 带一张真 PNG 和一句图题。"""
    png = wt / "figures" / "laundered.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not a render" * 8)
    from shared.lib.publication_figures import sha256_file

    return state.save_artifact(
        artifact_type="figure", name="laundered",
        content="![x](figures/laundered.png)\n\nCaption: a result nobody rendered",
        metadata={"record": "rendered_figure", "figure_hash": "sha256:" + "0" * 64,
                  "source_artifact_ids": [], "source_hashes": [],
                  "render_code": {"path": "figure_src/none.py",
                                  "content_hash": "sha256:" + "1" * 64},
                  "files": [{"format": "png", "path": "figures/laundered.png",
                             "content_hash": sha256_file(png)}],
                  "caption": "a result nobody rendered"},
    )


def _dossier(wt: Path) -> dict:
    from nodes.writing.tools.w_dossier import build_dossier

    return build_dossier(_bound(wt, "writing", "w-read"), force=True)


def _finalize(state: State) -> dict:
    loop = LoopResult(final_text="done", turns=1, tool_calls=[], messages=[])
    return asyncio.run(finalize_run(state, NodeHarness(node_type=state.node_type), loop, llm=None))


def _integrity_blockers(summary: dict) -> list[dict]:
    return [b for b in summary["blockers"]
            if b.get("reported_by") == "framework:figure_record_integrity"]


# ── cuikl 的约定：保存不拦 ────────────────────────────────────────────────


def test_saving_a_forged_record_still_succeeds(tmp_path):
    wt = _worktree(tmp_path)
    saved = _forge(_bound(wt, "postprocess", "pp-1"), wt)
    assert saved["id"] == "figure__laundered"
    assert (wt / "figures" / "figure__laundered.md").is_file()


# ── 收集点一：writing 读图与图题 ───────────────────────────────────────────


def test_writing_does_not_take_a_forged_record_as_a_figure(tmp_path):
    wt = _worktree(tmp_path)
    _forge(_bound(wt, "postprocess", "pp-1"), wt)

    dossier = _dossier(wt)

    assert "laundered.png" not in {f["name"] for f in dossier["figures"]}
    assert not any("nobody rendered" in str(f.get("caption")) for f in dossier["figures"])
    withheld = {w["path"]: w for w in dossier["withheld_figures"]}
    assert set(withheld) == {"figures/figure__laundered.md", "figures/laundered.png"}
    for item in withheld.values():
        assert item["record_id"] == "figure__laundered"
        assert item["check"] == "figure_hash"
        # 给模型的话要能照着做：哪张、哪道、怎么修
        assert "figure__laundered" in item["message"] and "request_figures" in item["message"]


def test_the_model_reads_why_a_figure_is_missing_from_the_list(tmp_path):
    from nodes.writing.tools.w_dossier import _read_dossier

    wt = _worktree(tmp_path)
    _forge(_bound(wt, "postprocess", "pp-1"), wt)

    out = asyncio.run(_read_dossier(_bound(wt, "writing", "w-read"), part="figures", rebuild=True))

    assert out["status"] == "success" and out["withheld"] == 2
    assert "不能当真图用" in out["figures"] and "figure__laundered" in out["figures"]
    assert "render_figure" in out["figures"]


def test_a_genuine_figure_is_read_with_its_caption(tmp_path):
    wt = _worktree(tmp_path)
    _render(_bound(wt, "postprocess", "pp-1"))

    dossier = _dossier(wt)

    by_name = {f["name"]: f for f in dossier["figures"]}
    assert by_name["demo.png"]["caption"] == "y grows with x"
    assert dossier["withheld_figures"] == []


def test_a_figure_file_changed_after_its_record_is_withheld(tmp_path):
    wt = _worktree(tmp_path)
    minted = _render(_bound(wt, "postprocess", "pp-1"))
    png = wt / minted["files"][0]["path"]
    png.write_bytes(png.read_bytes() + b"retouched")

    dossier = _dossier(wt)

    assert "demo.png" not in {f["name"] for f in dossier["figures"]}
    withheld = {w["path"]: w for w in dossier["withheld_figures"]}
    assert withheld[minted["files"][0]["path"]]["check"] == "file_bytes"
    # 记录本身也说不清这张图了：图题不收
    assert withheld["figures/figure__demo.md"]["check"] == "file_bytes"


# ── 收集点二：run 收尾 ────────────────────────────────────────────────────


def test_the_run_that_delivers_a_forged_record_ends_blocked(tmp_path):
    wt = _worktree(tmp_path)
    state = _bound(wt, "postprocess", "pp-1")
    _forge(state, wt)

    summary = _finalize(state)

    assert summary["status"] == "blocked"
    # 一张图一条 blocker：记录正文和它认领的 PNG 是同一件事
    [blocker] = _integrity_blockers(summary)
    assert set(blocker["evidence_paths"]) == {"figures/figure__laundered.md", "figures/laundered.png"}
    assert "figure__laundered" in blocker["summary"] and "【figure_hash】" in blocker["summary"]
    assert "render_figure" in blocker["requested_action"]
    assert blocker["suggested_owner"] == "postprocess"
    findings = summary[FIGURE_DELIVERY_AUDIT_KEY]["findings"]
    assert {f["check"] for f in findings} == {"figure_hash"}


def test_a_genuine_figure_finishes_completed_and_says_it_was_checked(tmp_path):
    wt = _worktree(tmp_path)
    state = _bound(wt, "postprocess", "pp-1")
    minted = _render(state)

    summary = _finalize(state)

    assert summary["status"] == "completed", summary["blockers"]
    audit = summary[FIGURE_DELIVERY_AUDIT_KEY]
    assert audit["findings"] == []
    # 「核过了、没问题」与「根本没核」必须分得开
    assert set(audit["checked"]) == {"figures/figure__demo.md", minted["files"][0]["path"]}


def test_a_figure_file_changed_after_its_record_blocks_the_finish(tmp_path):
    wt = _worktree(tmp_path)
    state = _bound(wt, "postprocess", "pp-1")
    minted = _render(state)
    png = wt / minted["files"][0]["path"]
    png.write_bytes(png.read_bytes() + b"retouched")

    summary = _finalize(state)

    assert summary["status"] == "blocked"
    checks = {f["path"]: f["check"] for f in summary[FIGURE_DELIVERY_AUDIT_KEY]["findings"]}
    assert checks[minted["files"][0]["path"]] == "file_bytes"


def _stage_into_paper(wt: Path, minted: dict) -> Path:
    """writing 把图拷进稿件（与 w_render._stage_figures 同一个动作：copy2）。"""
    src = wt / minted["files"][0]["path"]
    dst = wt / "paper" / "manuscript" / "figures" / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def _commit(wt: Path) -> None:
    subprocess.run(["git", "add", "-A", "--", "figures"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "figures"], cwd=wt, check=True)


def test_writing_delivering_a_genuine_copy_finishes_completed(tmp_path):
    wt = _worktree(tmp_path)
    minted = _render(_bound(wt, "postprocess", "pp-1"))
    _commit(wt)
    writing = _bound(wt, "writing", "w-1")
    _stage_into_paper(wt, minted)

    summary = _finalize(writing)

    assert summary["status"] == "completed", summary["blockers"]
    assert summary[FIGURE_DELIVERY_AUDIT_KEY]["checked"] == ["paper/manuscript/figures/demo.png"]


def test_writing_delivering_a_copy_of_a_forged_figure_is_blocked_on_the_upstream_record(tmp_path):
    """读图那一步已经不给它；这是兜底 —— 早先拷进稿件的那份（_stage_figures「已在 figures/
    里的也算」）照样会被编进 PDF。"""
    wt = _worktree(tmp_path)
    _forge(_bound(wt, "postprocess", "pp-1"), wt)
    _commit(wt)
    writing = _bound(wt, "writing", "w-1")
    _stage_into_paper(wt, {"files": [{"path": "figures/laundered.png"}]})

    summary = _finalize(writing)

    assert summary["status"] == "blocked"
    [blocker] = _integrity_blockers(summary)
    assert blocker["category"] == "upstream_quality"
    assert blocker["evidence_paths"] == ["paper/manuscript/figures/laundered.png"]
    assert "figure__laundered" in blocker["summary"] and "【figure_hash】" in blocker["summary"]


def test_a_genuine_copy_stays_genuine_when_the_original_is_tampered_later(tmp_path):
    """副本是记录担保的那份字节；原位那份事后被换，是原位那份的事（postprocess 收尾、
    writing 读图时都会抓到），不把真副本说成假图。"""
    wt = _worktree(tmp_path)
    minted = _render(_bound(wt, "postprocess", "pp-1"))
    _commit(wt)
    _stage_into_paper(wt, minted)
    original = wt / minted["files"][0]["path"]
    original.write_bytes(original.read_bytes() + b"retouched")

    summary = _finalize(_bound(wt, "writing", "w-1"))

    assert summary["status"] == "completed", summary["blockers"]
    assert summary[FIGURE_DELIVERY_AUDIT_KEY]["checked"] == ["paper/manuscript/figures/demo.png"]


def test_a_same_named_file_with_other_bytes_is_not_taken_for_a_doctored_figure(tmp_path):
    """experiment 自己也会画一张 demo.png。同名不同字节是另一个文件，不是改过的图 ——
    按名字判会把别的节点的收尾无端打成 blocked。"""
    wt = _worktree(tmp_path)
    _render(_bound(wt, "postprocess", "pp-1"))
    _commit(wt)
    experiment = _bound(wt, "experiment", "e-1")
    own_plot = wt / "experiments" / "runtime" / "demo.png"
    own_plot.parent.mkdir(parents=True, exist_ok=True)
    own_plot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"experiment's own plot" * 4)

    summary = _finalize(experiment)

    assert _integrity_blockers(summary) == []
    assert summary[FIGURE_DELIVERY_AUDIT_KEY] == {"checked": [], "findings": []}


def test_a_copy_of_an_earlier_genuine_render_is_stale_not_forged(tmp_path):
    """稿件里那份是上一版真渲染的副本（_stage_figures 已存在就不再拷）：陈旧，但不是假图。"""
    wt = _worktree(tmp_path)
    postprocess = _bound(wt, "postprocess", "pp-1")
    first = _render(postprocess, y=4.0)
    copy = _stage_into_paper(wt, first)
    second = _render(postprocess, y=5.0)
    assert second["files"][0]["content_hash"] != first["files"][0]["content_hash"]
    _commit(wt)

    summary = _finalize(_bound(wt, "writing", "w-1"))

    assert copy.is_file()
    assert summary["status"] == "completed", summary["blockers"]
    assert summary[FIGURE_DELIVERY_AUDIT_KEY]["findings"] == []

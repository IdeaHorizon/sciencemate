"""B 刀新不变量：render_figure 的出处绑定 / 恒跑审计 / 证人语义 / 防伪 / 回放。

沙箱在测试机不可用（mandatory sandbox 需要容器/Landlock），执行器用
subprocess 替身 —— 被测的不是沙箱本身，而是 render_figure 对执行结果的
机械录入义务；沙箱边界由 python_exec 自己的测试与部署环境守。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute

bootstrap()


@pytest.fixture(autouse=True)
def _fake_sandbox(monkeypatch):
    """subprocess 替身：跑真代码（含审计 preamble），不要求容器沙箱。"""

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
        result = {
            "status": "success" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-3000:],
            "stderr_tail": proc.stderr[-1500:],
            "workspace": str(workspace),
        }
        if proc.returncode != 0:
            # 替身的 `error` 要和真 `_execute_python` 一样由 command_failure_note
            # 写（core/tool_errors.py）。原来这里是 `stderr[-500:] or
            # "render code failed"` —— 子进程被信号杀掉时 stderr 是空的，于是
            # 退出码（真身会写成「命令退出码 -9」）在替身这边被一句固定文案吃掉，
            # 而那正是 2026-09-17 CI 偶发红里唯一能分辨"自己崩了"和"被外力杀了"
            # 的字段。替身比真身少说话，被测的义务就有一半没在测。
            from core.tool_errors import command_failure_note

            result["error"] = command_failure_note(proc.returncode, result["stderr_tail"])
        summary = python_exec._missing_glyph_summary(proc.stdout + "\n" + proc.stderr)
        if summary:
            result["figure_glyph_warning"] = summary
        return result

    monkeypatch.setattr(python_exec, "_execute_python", fake_execute_python)


def _state(tmp_path) -> State:
    return State.new(node_type="postprocess", base_dir=tmp_path)


def _dataset(state: State) -> str:
    return state.save_artifact(
        artifact_type="dataset", name="d1", content="x,y\n1,2\n2,4", metadata={}
    )["id"]


_CLEAN_CODE = (
    "import matplotlib\nmatplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "fig, ax = plt.subplots(figsize=(4, 3), dpi=150)\n"
    "ax.plot([1, 2], [2, 4], marker='o')\n"
    "ax.set_xlabel('x'); ax.set_ylabel('y')\n"
    "fig.tight_layout()\nfig.savefig('demo.png')\n"
)


def _render(state, **overrides):
    kwargs = {
        "output_name": "demo",
        "caption": "y grows with x",
        "alt_text": "line chart of y vs x",
        "code": _CLEAN_CODE,
        "output_files": ["demo.png"],
        "source_artifact_ids": [],
    }
    kwargs.update(overrides)
    result = asyncio.run(execute("render_figure", state, **kwargs))
    if result.get("status") == "error":
        _print_render_error(kwargs, result)
    return result


def _print_render_error(kwargs: dict, result: dict) -> None:
    """render_figure 说了 error，就把它的原话带到现场。

    这批用例大多只断言 `result["status"] == "success"`，断言自己印得出来的就
    只有 `assert 'error' == 'success'`。2026-09-17 CI 上连着五轮「每轮恰好红
    一条、每轮换一条、重跑即绿」，日志里没有一个字说得出 render 为什么失败 ——
    现场全靠猜。`test_replay_command_reproduces_the_output_bytes` 更极端：它先
    崩在 `KeyError: 'figure_id'`，连一条能挂 message 的断言都没有。

    所以口径不是「给每条断言补一个 message」—— 那是一张要人维护的名单，下一条
    新写的用例照样会漏。打印挂在**唯一的调用口**上，任何一次 error 都自带现场。
    pytest 只在用例失败时展示捕获的 stdout，故意期待 error 的那些用例照常安静。

    `returncode` 是要分岔的那个字段。沙箱在本文件里被 subprocess 替身换掉了
    （见 `_fake_sandbox`），失败不可能来自沙箱 spawn；剩下的两种要靠它分：
      - 子进程自己非零退出 → returncode > 0，stderr_tail 里有 traceback
      - 子进程被外力杀掉   → returncode < 0（-9 = SIGKILL，OOM killer 的形状），
                             stderr_tail 空
    """
    execution = result.get("execution") or {}
    print("── render_figure → status=error ─────────────────────────────────")
    print(f"   error       : {result.get('error')!r}")
    print(f"   error_code  : {result.get('error_code')!r}")
    print(f"   output_files: {kwargs.get('output_files')!r}")
    if execution:
        print(f"   returncode  : {execution.get('returncode')!r}")
        print(f"   workspace   : {execution.get('workspace')!r}")
        print(f"   stderr_tail : {execution.get('stderr_tail')!r}")
        print(f"   stdout_tail : {execution.get('stdout_tail')!r}")
    else:
        print("   (没有 execution —— 失败在执行之外的机械校验里，看 error 文本)")
    print(f"   result keys : {sorted(result)}")
    print(f"   host        : {_host_pressure()}")
    print("─────────────────────────────────────────────────────────────────")


def _host_pressure() -> str:
    """失败那一刻这台机器有多挤。

    `returncode < 0` 说"被外力杀了"，但没说是谁杀的。这一行是用来把"机器被挤爆"
    和"别的东西杀了它"分开的：CI runner 16 核、capacity 4，同一时刻可能有三个
    job 各开 6 个 worker，`.gitea/workflows/test.yaml` 里那段「幽灵套件抢机器」
    的注释记的就是这台机器上同类现象。loadavg 是 1 分钟均值，等到打印时还在。

    纯标准库，拿不到就如实说拿不到 —— 这一行本身不许把用例变红。
    """
    bits = [f"cpu_count={os.cpu_count()}"]
    # Windows 的 os 模块根本没有 getloadavg（AttributeError，不是 OSError）。
    # 这个函数挂在 `_render` 的每一次 error 上，包括那些**故意**期待被拒的用例
    # —— 它一抛，九条本该绿的拒绝用例全红，还盖掉了真正的失败原因。
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        bits.append("loadavg=unavailable")
    else:
        try:
            bits.append("loadavg=" + "/".join(f"{v:.1f}" for v in getloadavg()))
        except OSError:
            bits.append("loadavg=unavailable")
    meminfo = Path("/proc/meminfo")  # Linux runner 有，本机 macOS 没有
    if meminfo.is_file():
        try:
            fields = dict(
                line.split(":", 1) for line in meminfo.read_text().splitlines() if ":" in line
            )
            for key in ("MemTotal", "MemAvailable"):
                if key in fields:
                    bits.append(f"{key}={fields[key].strip()}")
        except OSError:
            bits.append("meminfo=unreadable")
    return " ".join(bits)


# ── 出处绑定（不可压缩核 #2）────────────────────────────────────────────────


def test_record_binds_data_code_and_output_hashes(tmp_path):
    state = _state(tmp_path)
    ds = _dataset(state)
    result = _render(state, source_artifact_ids=[ds])
    assert result["status"] == "success", result
    record = state.read_artifact(result["figure_id"])
    meta = record["metadata"]

    from nodes.postprocess.contracts import artifact_payload_hash, hash_bytes, hash_file

    # 数据 hash：逐条对上真实源产物
    assert meta["source_artifact_ids"] == [ds]
    assert meta["source_hashes"] == [artifact_payload_hash(state.read_artifact(ds))]
    # 代码 hash：冻结脚本的字节
    from core import paths

    script = paths.resolve_display_relpath(state, meta["render_code"]["path"])
    assert script.is_file()
    assert meta["render_code"]["content_hash"] == hash_bytes(script.read_bytes())
    # read_text 会把 \r\n 译回 \n，比的就不是字节了 —— 要比字节就读字节。
    assert script.read_bytes() == _CLEAN_CODE.encode("utf-8"), "冻结的必须逐字节等于跑过的"
    # 输出 hash：交付文件的字节
    for item in meta["files"]:
        assert item["content_hash"] == hash_file(Path(item["absolute_path"]))
    # 三重绑定的指纹
    from shared.lib.publication_figures import figure_binding_hash

    assert meta["figure_hash"] == figure_binding_hash(meta)


def test_frozen_script_bytes_match_its_hash_where_text_mode_rewrites_newlines(
    tmp_path, monkeypatch
):
    """Windows 上文本模式写盘把 \\n 写成 \\r\\n。CI 是 Linux，那条路在这里走不到，
    所以把 pathlib 的文本写入换成 Windows 的换行语义再跑一遍：冻结脚本的字节
    必须仍然等于记录里的 content_hash。"""
    import pathlib

    real_open = pathlib.Path.open

    def windows_text_open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if "b" not in mode and any(flag in mode for flag in "wax") and newline is None:
            newline = "\r\n"
        return real_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(pathlib.Path, "open", windows_text_open)

    state = _state(tmp_path)
    result = _render(state)
    assert result["status"] == "success", result
    meta = state.read_artifact(result["figure_id"])["metadata"]

    from core import paths
    from nodes.postprocess.contracts import hash_bytes

    script = paths.resolve_display_relpath(state, meta["render_code"]["path"])
    assert meta["render_code"]["content_hash"] == hash_bytes(script.read_bytes())


def test_failure_diagnostic_survives_a_platform_without_loadavg(monkeypatch):
    """`_host_pressure` 挂在每一次 error 上，包括期待被拒的用例；它自己抛异常，
    那些用例就全红。Windows 没有 os.getloadavg —— 在这里把它拿掉重演一次。"""
    monkeypatch.delattr(os, "getloadavg", raising=False)
    assert "loadavg=unavailable" in _host_pressure()


def test_missing_source_artifact_is_rejected(tmp_path):
    state = _state(tmp_path)
    result = _render(state, source_artifact_ids=["dataset__does_not_exist"])
    assert result["status"] == "error"
    assert "does not exist" in result["error"]


def test_declared_output_partly_produced_binds_what_exists_and_records_the_gap(tmp_path):
    """判决拆除三波（figure:351 降格）：部分产出不再整次作废——绑定已存在的
    文件，缺失项记 OB-DEVIATION；零产出仍无可铸。"""
    state = _state(tmp_path)
    result = _render(state, output_files=["demo.png", "phantom.png"])
    assert result["status"] == "success", result
    assert [item["path"].split("/")[-1] for item in result["files"]] == ["demo.png"]
    deviation = [f for f in result["findings"] if f["collector"] == "OB-DEVIATION"]
    assert deviation and deviation[0]["missing_output_files"] == ["phantom.png"]
    record = state.read_artifact(result["figure_id"])
    assert any(
        f["collector"] == "OB-DEVIATION" for f in record["metadata"]["findings"]
    ), "缺失项必须落在记录自己的账里"


def test_zero_produced_outputs_cannot_be_minted(tmp_path):
    state = _state(tmp_path)
    result = _render(state, output_files=["phantom.png"])
    assert result["status"] == "error"
    assert "phantom.png" in result["error"]


def test_empty_caption_and_alt_text_are_recorded_with_an_ob_caption_finding(tmp_path):
    """判决拆除三波（figure:272 D 降格）：探索路径不必先写 alt_text；空值照录，
    findings 记 OB-CAPTION，发表路径由消费端义务应答。把墙加回去这条必转红。"""
    state = _state(tmp_path)
    result = _render(state, caption="", alt_text="   ")
    assert result["status"] == "success", result
    caption_findings = [f for f in result["findings"] if f["collector"] == "OB-CAPTION"]
    assert caption_findings and caption_findings[0]["empty_fields"] == ["caption", "alt_text"]
    record = state.read_artifact(result["figure_id"])
    assert record["metadata"]["caption"] == ""
    assert record["metadata"]["alt_text"] == ""
    assert record["metadata"]["figure_hash"] == result["figure_hash"]

    only_alt = _render(state, output_name="demo2", alt_text="")
    assert only_alt["status"] == "success", only_alt
    assert [f["empty_fields"] for f in only_alt["findings"]
            if f["collector"] == "OB-CAPTION"] == [["alt_text"]]


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"purpose": "vibes"}, "purpose"),
        ({"asset_kind": "hologram"}, "asset_kind"),
        ({"output_files": []}, "output_files"),
        ({"source_artifact_ids": [""]}, "source_artifact_ids"),
    ],
)
def test_vocabulary_and_shape_contracts_are_enforced_at_dispatch(tmp_path, overrides, needle):
    """purpose/asset_kind enum、output_files 至少一项、source id 非空：契约归
    schema，`execute()` 派发口核并列出合法值；工具体内不再手写。"""
    state = _state(tmp_path)
    result = _render(state, **overrides)
    assert result["status"] == "error", result
    assert result.get("parameter_violations"), result
    assert needle in result["error"]


def test_pre_existing_file_cannot_be_bound_as_this_render(tmp_path):
    """像素诚信：挂既有文件充数（代码没写它）会被机械拒绝。"""
    state = _state(tmp_path)
    from core.project_workspace import validate_tool_cwd

    workspace = validate_tool_cwd(state, None)
    workspace.mkdir(parents=True, exist_ok=True)
    stale = workspace / "old.png"
    stale.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    old = time.time() - 3600
    import os

    os.utime(stale, (old, old))
    result = _render(
        state,
        code=_CLEAN_CODE,  # 只写 demo.png，不碰 old.png
        output_files=["demo.png", "old.png"],
    )
    assert result["status"] == "error"
    assert "old.png" in result["error"]


def test_zero_source_figures_disclose_the_absence(tmp_path):
    state = _state(tmp_path)
    result = _render(state, source_artifact_ids=[])
    assert result["status"] == "success"
    assert any(f["collector"] == "OB-PROVENANCE" for f in result["findings"])


# ── 图像级机械审计恒跑 ──────────────────────────────────────────────────────


def test_audit_always_runs_and_is_recorded_even_when_clean(tmp_path):
    state = _state(tmp_path)
    result = _render(state, source_artifact_ids=[_dataset(state)])
    assert result["status"] == "success"
    audit = state.read_artifact(result["figure_id"])["metadata"]["audit"]
    assert audit["ran"] is True, "审计缺席与审计通过必须可区分"
    assert audit["text_geometry_attached"] is True
    assert [
        f for f in result["findings"] if f["collector"].startswith("OB-TEXT")
    ] == [], "干净的图不该有几何 findings"


def test_audit_reports_text_outside_canvas_as_a_finding(tmp_path):
    state = _state(tmp_path)
    clipped = (
        "import matplotlib\nmatplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots(figsize=(3, 2), dpi=100)\n"
        "ax.plot([1, 2], [2, 4])\n"
        "fig.text(0.9, 0.98, 'a very long overflowing annotation', fontsize=14)\n"
        "fig.savefig('bad.png')\n"
    )
    result = _render(state, code=clipped, output_files=["bad.png"])
    assert result["status"] == "success", "审计是证人不是闸：记 findings，不拒绝"
    assert any(f["collector"] == "OB-TEXT-BOUNDS" for f in result["findings"])


def test_blank_render_is_reported(tmp_path):
    state = _state(tmp_path)
    blank = (
        "from PIL import Image\n"
        "Image.new('RGB', (300, 200), 'white').save('blank.png')\n"
    )
    result = _render(state, code=blank, output_files=["blank.png"])
    assert result["status"] == "success"
    assert any(f["collector"] == "OB-FILE-VALIDITY" for f in result["findings"])


def test_non_matplotlib_output_records_geometry_absence_not_a_warning(tmp_path):
    state = _state(tmp_path)
    svg = (
        "open('plain.svg', 'w').write("
        "'<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\"/>')\n"
    )
    result = _render(state, code=svg, output_files=["plain.svg"])
    assert result["status"] == "success"
    audit = state.read_artifact(result["figure_id"])["metadata"]["audit"]
    assert audit["ran"] is True
    assert audit["text_geometry_attached"] is False
    assert [f for f in result["findings"] if "TEXT" in f["collector"]] == []


# ── VLM 证人：在场即跑、缺席即缺席、瞬态失败如实记 ─────────────────────────


def test_absent_visual_review_role_leaves_no_witness_entries(tmp_path, monkeypatch):
    from core import model_roles

    monkeypatch.delenv("HARNESS_MODEL_ROLES", raising=False)
    model_roles.install_from_environment(force=True)
    state = _state(tmp_path)
    result = _render(state, source_artifact_ids=[_dataset(state)])
    assert result["status"] == "success"
    # 缺席是可读事实 —— 而且要读得出**是哪一种**缺席（没配角色 / 没有 PNG 可审
    # / 跑了没跑完）。只回 None 时这三者在记录里长得一模一样。
    assert result["vlm_review"] == {
        "ran": False, "reason": "role_not_configured", "role": "visual_review",
    }, result["vlm_review"]
    assert [f for f in result["findings"] if f["collector"] == "VLM-WITNESS"] == []
    assert [f for f in result["findings"] if f["collector"] == "OB-REVIEW-TRANSIENT"] == []


@pytest.fixture
def visual_review_role(monkeypatch):
    from core import model_roles

    monkeypatch.setenv(
        "HARNESS_MODEL_ROLES",
        json.dumps(
            {
                "visual_review": {
                    "provider": "icompify",
                    "model": "minimax-m3",
                    "base_url": "https://reviewer.invalid",
                    "api_key": "t",
                }
            }
        ),
    )
    model_roles.install_from_environment(force=True)
    yield
    model_roles.install_from_environment(force=True)


def test_present_role_runs_the_witness_and_maps_observations(
    tmp_path, monkeypatch, visual_review_role
):
    from nodes.postprocess import vlm_witness

    async def fake_review_image(**kwargs):
        return {
            "status": "success",
            "observations": [
                {
                    "panel_id": "GLOBAL",
                    "region": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                    "observation": "legend covers the data marks",
                }
            ],
            "attempts": [{"attempt": 1, "response_valid": True}],
        }

    monkeypatch.setattr(vlm_witness, "review_image", fake_review_image)
    state = _state(tmp_path)
    result = _render(state, source_artifact_ids=[_dataset(state)])
    assert result["status"] == "success"
    assert result["vlm_review"]["ran"] is True
    assert result["vlm_review"]["completed"] is True
    assert result["vlm_review"]["model"] == "minimax-m3"
    witness = [f for f in result["findings"] if f["collector"] == "VLM-WITNESS"]
    assert len(witness) == 1
    assert witness[0]["rule_ref"] == "base.visual_occlusion"
    assert "verdict" not in witness[0], "证人只留观察，不留判决"


def test_transient_witness_failure_is_classified_not_hidden(
    tmp_path, monkeypatch, visual_review_role
):
    from nodes.postprocess import vlm_witness

    async def failing_review_image(**kwargs):
        return {"status": "review_unavailable", "error": "reviewer endpoint timed out",
                "observations": [], "attempts": []}

    monkeypatch.setattr(vlm_witness, "review_image", failing_review_image)
    state = _state(tmp_path)
    result = _render(state, source_artifact_ids=[_dataset(state)])
    assert result["status"] == "success", "瞬态审图失败只影响 findings 完整性，不挡铸记录"
    assert result["vlm_review"]["completed"] is False
    transient = [f for f in result["findings"] if f["collector"] == "OB-REVIEW-TRANSIENT"]
    assert transient and "timed out" in transient[0]["message"]


# ── 防伪：绕过 render_figure 直写的记录被机械识别 ──────────────────────────


def test_forged_record_without_binding_is_rejected(tmp_path):
    state = _state(tmp_path)
    forged = state.save_artifact(
        artifact_type="figure",
        name="forged",
        content="![x](x.png)",
        metadata={"record": "rendered_figure", "figure_hash": "sha256:" + "0" * 64},
    )
    from shared.lib.publication_figures import PublicationFigureError, validate_figure_record

    with pytest.raises(PublicationFigureError):
        validate_figure_record(state.read_artifact(forged["id"]))


def test_tampering_any_binding_field_breaks_the_fingerprint(tmp_path):
    state = _state(tmp_path)
    result = _render(state, source_artifact_ids=[_dataset(state)])
    record = state.read_artifact(result["figure_id"])
    from shared.lib.publication_figures import PublicationFigureError, validate_figure_record

    validate_figure_record(record)  # 真记录通过
    for field, value in (
        ("caption", "laundered caption"),
        ("source_hashes", ["sha256:" + "1" * 64]),
    ):
        tampered = json.loads(json.dumps(record))
        tampered["metadata"][field] = value
        with pytest.raises(PublicationFigureError):
            validate_figure_record(tampered)
    # 改渲染代码 hash 同样露馅
    tampered = json.loads(json.dumps(record))
    tampered["metadata"]["render_code"]["content_hash"] = "sha256:" + "2" * 64
    with pytest.raises(PublicationFigureError):
        validate_figure_record(tampered)


def test_record_from_a_non_visualization_owner_is_rejected(tmp_path):
    state = State.new(node_type="writing", base_dir=tmp_path)
    forged = state.save_artifact(
        artifact_type="figure",
        name="forged",
        content="![x](x.png)",
        metadata={"record": "rendered_figure"},
    )
    from shared.lib.publication_figures import PublicationFigureError, validate_figure_record

    with pytest.raises(PublicationFigureError, match="visualization owner"):
        validate_figure_record(state.read_artifact(forged["id"]))


def test_verdict_vocabulary_cannot_be_minted(tmp_path):
    """判决词表钉在**铸造侧**。消费端（publication_figures）不再因一个键名拒掉
    整张图（判决拆除第三波：publication_figures:100 删）—— 见
    tests/test_figure_verdict_vocabulary_pinned_at_minting.py。"""
    from nodes.postprocess.contracts import VisualContractError
    from nodes.postprocess.tools.figure import reject_verdict_fields

    for field in ("status", "quality_mode", "verdict"):
        with pytest.raises(VisualContractError):
            reject_verdict_fields({field: "approved"})


# ── 回放：referee 能按记录重跑 ─────────────────────────────────────────────


def test_replay_command_reproduces_the_output_bytes(tmp_path):
    state = _state(tmp_path)
    result = _render(state, source_artifact_ids=[_dataset(state)])
    meta = state.read_artifact(result["figure_id"])["metadata"]
    from core import paths

    replay_cwd = paths.resolve_display_relpath(state, meta["replay"]["cwd"])
    script = paths.resolve_display_relpath(state, meta["render_code"]["path"])
    before = {
        item["path"]: item["content_hash"] for item in meta["files"]
    }
    proc = subprocess.run(
        [sys.executable, str(script)], cwd=replay_cwd, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    from nodes.postprocess.contracts import hash_file

    for item in meta["files"]:
        assert hash_file(Path(item["absolute_path"])) == before[item["path"]], (
            "重跑冻结脚本必须逐字节复现输出 —— 否则记录不构成可复现性证据"
        )


# ── 生成式像素不得充当证据图（保留的 B 检查）───────────────────────────────


def test_generative_pixels_without_disclaimer_are_rejected(tmp_path):
    state = _state(tmp_path)
    for generative in ({"role": "hero"}, {"role": "hero", "evidence_bearing": True}):
        result = _render(state, generative=generative)
        assert result["status"] == "error"
        assert "evidence" in result["error"]
    result = _render(state, asset_kind="generative_illustration")
    assert result["status"] == "error", "generative_illustration 必须显式声明 evidence_bearing=false"


def test_declared_generative_illustration_is_recorded_and_disclosed(tmp_path):
    state = _state(tmp_path)
    result = _render(state, generative={"role": "concept_art", "evidence_bearing": False})
    assert result["status"] == "success"
    meta = state.read_artifact(result["figure_id"])["metadata"]
    assert meta["generative_content"] == {"role": "concept_art", "evidence_bearing": False}
    assert any(f["collector"] == "OB-GENERATIVE" for f in result["findings"])


# ── request_id 对账 ────────────────────────────────────────────────────────


def test_unknown_request_id_is_rejected_listing_the_legal_values(tmp_path):
    """对不上的 request_id 绑不了任何政策（家族/版心/语言），所以是拒绝 —— 但报错
    必须列出合法值：要求对方引用一个它无从枚举的标识符，本来就只能靠猜。

    2026-09-18 之前这里只记一条 OB-DEVIATION 照样铸记录；家族绑定到调用方请求
    之后，一条对不上请求的记录就是一张没有主人的图。
    """

    state = _state(tmp_path)
    state.hook_state["node_inputs"] = {
        "visual_requests": [{"request_id": "req-a", "intent": "compare"}]
    }
    result = _render(state, request_id="made-up")
    assert result["status"] == "error"
    assert "req-a" in result["error"], "报错必须列出合法值"


def test_a_request_id_with_no_caller_requests_at_all_is_only_a_finding(tmp_path):
    """run 根本没收到 visual_requests（ad-hoc 渲染）时没有东西可绑：照铸，记一条账。"""

    state = _state(tmp_path)
    result = _render(state, request_id="made-up")
    assert result["status"] == "success", result.get("error")
    deviation = [f for f in result["findings"] if f["collector"] == "OB-DEVIATION"]
    assert deviation and deviation[0]["field"] == "request_id"


def test_deprecated_quality_mode_is_witnessed_not_rejected(tmp_path):
    state = _state(tmp_path)
    state.hook_state["node_inputs"] = {
        "visual_requests": [
            {"request_id": "req-a", "intent": "compare", "quality_mode": "publication"}
        ]
    }
    result = _render(state, request_id="req-a")
    assert result["status"] == "success"
    assert any(f["collector"] == "OB-DEPRECATION" for f in result["findings"])

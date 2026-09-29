"""实验数据溯源门禁的回归。

核心是 `test_replays_e2e3_incident`：把 E2E-3 那篇论文的事故原样搭出来 ——
产物依赖 32 小时前另一个项目留下的 τ-bench 轨迹日志、正文只字未提 —— 断言这道
门禁会拦住它。当时它 12 项 QC 全绿、62 项 preflight 全过。
"""

from __future__ import annotations

import json
import os
import time
import types

import pytest

from core import data_provenance as dp
from core.bootstrap import bootstrap
from core.state import State

bootstrap()


def _file(path, content="{}", *, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _state(tmp_path, node_type="experiment", project_id="p"):
    st = State.new(node_type=node_type, base_dir=tmp_path / "runs", project_id=project_id)
    dp.mark_run_start(st)
    return st


# ── 采集口径 ────────────────────────────────────────────────────────────────


def test_records_external_data_and_flags_predating(tmp_path):
    st = _state(tmp_path)
    started = dp.run_started_at(st)
    old = _file(tmp_path / "shared" / "traj.json", mtime=started - 3600)
    new = _file(tmp_path / "shared" / "fresh.json", mtime=started + 10)

    dp.record_tool_paths(st, "run_bash", {"cmd": f"cat {old}"})
    dp.record_tool_paths(st, "execute_python", {"code": f"open('{new}')"})

    reads = {r["path"]: r for r in dp.external_reads(st)}
    assert set(reads) == {str(old), str(new)}
    assert reads[str(old)]["predates_run"] is True
    assert reads[str(new)]["predates_run"] is False
    assert [r["path"] for r in dp.stale_external_inputs(st)] == [str(old)]


def test_own_territory_is_not_external(tmp_path):
    """本 run 目录 / 本项目目录下的东西是自己的领地，不该被报成外部依赖。"""
    st = _state(tmp_path)
    started = dp.run_started_at(st)
    mine = _file(st.root / "workspace" / "out.json", mtime=started - 999)
    proj = _file(st.project_root / "workspace" / "data.json", mtime=started - 999)
    dp.record_tool_paths(st, "run_bash", {"cmd": f"cat {mine} {proj}"})
    assert dp.external_reads(st) == []


def test_code_and_system_paths_excluded(tmp_path):
    """读参考实现 / 读配置 ≠ 复用别人的实验结果。别用噪声淹没信号。"""
    st = _state(tmp_path)
    started = dp.run_started_at(st)
    src = _file(tmp_path / "shared" / "tau_bench" / "env.py", "x", mtime=started - 999)
    cfg = _file(tmp_path / "shared" / "conf.yaml", "a: 1", mtime=started - 999)
    data = _file(tmp_path / "shared" / "results.json", mtime=started - 999)
    dp.record_tool_paths(st, "run_bash", {"cmd": f"cat {src} {cfg} {data}"})
    assert [r["path"] for r in dp.external_reads(st)] == [str(data)]


def test_missing_and_directory_paths_ignored(tmp_path):
    st = _state(tmp_path)
    d = tmp_path / "shared" / "logs"
    d.mkdir(parents=True)
    dp.record_tool_paths(st, "run_bash", {"cmd": f"ls {d} && cat /nope/none.json"})
    assert dp.external_reads(st) == []


# ── 声明侧 ──────────────────────────────────────────────────────────────────


def test_declaration_in_artifact_metadata_satisfies_gate(tmp_path):
    st = _state(tmp_path)
    started = dp.run_started_at(st)
    old = _file(tmp_path / "shared" / "traj.json", mtime=started - 3600)
    dp.record_tool_paths(st, "run_bash", {"cmd": f"cat {old}"})

    undeclared = dp.undeclared_stale_inputs(st, [])
    assert [r["path"] for r in undeclared] == [str(old)]

    art = {
        "metadata": {
            "reused_inputs": [
                {
                    "path": str(old),
                    "source": "上一轮 E2E 的 τ-bench run",
                    "reason": "重跑成本过高，本阶段先做离线分析",
                }
            ]
        }
    }
    assert dp.undeclared_stale_inputs(st, [art]) == []


def test_declaration_must_match_the_actual_file(tmp_path):
    """声明了**别的**文件不算数 —— 否则随便写一句就能过。"""
    st = _state(tmp_path)
    started = dp.run_started_at(st)
    used = _file(tmp_path / "shared" / "used.json", mtime=started - 3600)
    _file(tmp_path / "shared" / "other.json", mtime=started - 3600)
    dp.record_tool_paths(st, "run_bash", {"cmd": f"cat {used}"})
    art = {"metadata": {"reused_inputs": [{"path": str(tmp_path / "shared" / "other.json")}]}}
    assert [r["path"] for r in dp.undeclared_stale_inputs(st, [art])] == [str(used)]


# ── 门禁接线 ────────────────────────────────────────────────────────────────


def _gate(st, node_type="experiment"):
    from core.executor import _data_provenance_check

    class _H:
        pass

    h = _H()
    h.node_type = node_type
    return _data_provenance_check(st, h)


def test_gate_fails_producing_node_and_passes_system_node(tmp_path):
    st = _state(tmp_path)
    started = dp.run_started_at(st)
    old = _file(tmp_path / "shared" / "traj.json", mtime=started - 3600)
    dp.record_tool_paths(st, "run_bash", {"cmd": f"cat {old}"})

    res = _gate(st, "experiment")
    assert res["passed"] is False
    assert res["mechanical"] is True and res["dimension"] == "scientific"
    assert "沉默复用等于" in res["reasoning"]
    assert str(old) in res["reasoning"]
    # 系统节点读全项目历史是职责，不由本判据管
    assert _gate(st, "_curator") is None


def test_gate_passes_when_nothing_stale(tmp_path):
    st = _state(tmp_path)
    res = _gate(st)
    assert res["passed"] is True and res["mechanical"] is True


def test_warn_mode_records_but_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_PROVENANCE_GATE", "warn")
    st = _state(tmp_path)
    started = dp.run_started_at(st)
    old = _file(tmp_path / "shared" / "traj.json", mtime=started - 3600)
    dp.record_tool_paths(st, "run_bash", {"cmd": f"cat {old}"})
    res = _gate(st)
    assert res["passed"] is True
    assert "warn 模式" in res["reasoning"]


def test_recording_never_breaks_tool_calls(tmp_path):
    """溯源是 advisory 采集 —— 参数再离谱也不能把工具调用弄挂。"""
    st = _state(tmp_path)
    dp.record_tool_paths(st, "weird", {"a": object(), "b": [{"c": None}]})
    dp.record_tool_paths(None, "weird", {"cmd": "/x"})  # state 都没有
    assert dp.external_reads(st) == []


# ── 项目工作区机械创建 ──────────────────────────────────────────────────────


def test_project_workspace_is_created_mechanically(tmp_path):
    """E2E-3：<project_root>/workspace 只是提示词里的约定，目录从没被建过。"""
    st = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p_ws")
    assert (st.project_root / "workspace").is_dir()


def test_legacy_cli_prompt_does_not_inject_mtime_gate(tmp_path):
    """The retired mtime gate is not injected into legacy CLI prompts."""
    from core.context_engine import build_messages
    from core.harness import NodeHarness

    st = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p_tell")
    h = NodeHarness(node_type="experiment", system_prompt="do experiments")
    sys_msg = build_messages(h, st, {})[0].content
    assert "data_provenance_declared" not in sys_msg
    assert "mtime" not in sys_msg

    # 系统节点不需要这段（它们不跑实验）
    h2 = NodeHarness(node_type="_curator", system_prompt="curate")
    st2 = State.new(node_type="_curator", base_dir=tmp_path / "runs", project_id="p_tell")
    assert "data_provenance_declared" not in build_messages(h2, st2, {})[0].content


# ── E2E-3 事故回放 ──────────────────────────────────────────────────────────


def test_replays_e2e3_incident(tmp_path):
    """把 E2E-3 那篇论文的事故原样搭出来，断言这道门禁会拦住。

    现场（硬时间戳）：
        轨迹文件 created  2026-07-26 07:04:55 / 07:06:41 (+0800)
        e2e3 项目起始     2026-07-27 14:56    (+0800)
    两个文件是**上一轮 E2E（另一个项目）**跑 τ-bench 时留在共享安装目录
    `<平台>/workspace/tau-bench/logs/experiment/` 里的。experiment 节点 ls 一下
    就拿来分析了，论文 Methods 写成 "trajectories were collected using
    deepseek-v4-pro"，读起来像本研究采集的。当时 12 项 QC 全绿。
    """
    st = _state(tmp_path, node_type="experiment", project_id="agent-step-routing-e2e3")
    started = dp.run_started_at(st)
    shared_logs = tmp_path / "platform" / "workspace" / "tau-bench" / "logs" / "experiments"
    traj = [
        _file(
            shared_logs / "tool-calling-deepseek-v4-pro-0.0_range_0-10_a.json",
            json.dumps({"episodes": 10}),
            mtime=started - 32 * 3600,
        ),
        _file(
            shared_logs / "tool-calling-deepseek-v4-pro-0.0_range_0-10_b.json",
            json.dumps({"episodes": 3}),
            mtime=started - 32 * 3600,
        ),
    ]
    # 节点当时就是这么干的：ls 共享目录 → 直接读两个现成的 json
    dp.record_tool_paths(st, "safe_run_bash", {"cmd": f"ls -la {shared_logs}/"})
    dp.record_tool_paths(
        st,
        "safe_execute_python",
        {"code": f"import json\nfor f in ['{traj[0]}', '{traj[1]}']:\n    d=json.load(open(f))"},
    )

    # 产物只字未提来源 —— 正是当时的情形
    st.save_artifact(
        "experiment_log",
        "Verifiability_Step_Routing_Experiment",
        "H1 validated, H2 validated, H3 refuted",
        {},
    )
    res = _gate(st, "experiment")
    assert res["passed"] is False, "这次 run 当年是 12 项 QC 全绿通过的"
    for p in traj:
        assert str(p) in res["reasoning"]
    assert "比本 run 还老" in res["reasoning"]

    # 如实声明之后放行 —— 门禁管的是"沉默复用"，不是禁止复用
    st.save_artifact(
        "experiment_log",
        "Verifiability_Step_Routing_Experiment",
        "H1 validated …（数据复用自上一轮 E2E）",
        {
            "reused_inputs": [
                {
                    "path": str(p),
                    "source": "上一轮 E2E（atomic-agents-verifiable-routing）"
                    "跑 τ-bench 留在共享安装目录的日志",
                    "reason": "本阶段做离线分析；重跑 episode 待 Phase 2",
                }
                for p in traj
            ]
        },
    )
    assert _gate(st, "experiment")["passed"] is True


@pytest.mark.parametrize("gap_seconds", [1, 60, 32 * 3600])
def test_any_pre_run_gap_counts(tmp_path, gap_seconds):
    """ "早于本 run"就是早于，不设宽限期 —— 宽限期就是绕过口。"""
    st = _state(tmp_path)
    old = _file(tmp_path / "shared" / "d.json", mtime=dp.run_started_at(st) - gap_seconds)
    dp.record_tool_paths(st, "run_bash", {"cmd": f"cat {old}"})
    assert _gate(st)["passed"] is False


def test_run_start_is_stamped_before_any_tool_runs(tmp_path):
    """基准点晚一秒就会漏掉真复用 —— 它必须在工具跑之前打上。"""
    st = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p_ts")
    before = time.time()
    dp.mark_run_start(st)
    assert before <= dp.run_started_at(st) <= time.time()


# ── 埋点确实接上了（不是测 record_tool_paths 本身）────────────────────────


@pytest.mark.asyncio
async def test_dispatch_layer_no_longer_records_mtime_provenance(tmp_path):
    """Project isolation and Git paths replace tool-argument mtime guessing."""
    from core import tool_registry

    st = _state(tmp_path)
    started = dp.run_started_at(st)
    old = _file(tmp_path / "shared" / "traj.json", '{"episodes": 3}', mtime=started - 3600)

    res = await tool_registry.execute("read_file", st, path=str(old))
    assert res["status"] == "success", res
    assert dp.stale_external_inputs(st) == []


# ── Windows 盘符路径（真机实测：旧的只认 /… 的正则在 Windows 上溯源全瞎，
#    外部数据复用一律漏报 → 平台的 rigor/审计在 Windows 上形同虚设）─────────────

def test_windows_drive_paths_are_extracted():
    cmd = r"cat C:\Users\fcbay\afs\traj.json ; open('C:/data/results.csv')"
    found = set(dp._ABS_PATH_RE.findall(cmd))
    assert r"C:\Users\fcbay\afs\traj.json" in found, "反斜杠盘符路径没抽出来"
    assert "C:/data/results.csv" in found, "正斜杠盘符路径没抽出来"


def test_posix_paths_still_extracted():
    found = set(dp._ABS_PATH_RE.findall("cat /home/u/traj.json && cat /tmp/data.csv"))
    assert found == {"/home/u/traj.json", "/tmp/data.csv"}, "POSIX 抽取不该变（逐字回归）"


def test_url_does_not_masquerade_as_a_windows_drive_path():
    found = dp._ABS_PATH_RE.findall("curl https://example.com/a/b.json")
    assert not any(len(f) > 1 and f[1] == ":" for f in found), "URL 里冒出了盘符路径"


def test_norm_is_identity_on_posix_and_case_insensitive_on_windows(monkeypatch):
    monkeypatch.setattr(dp, "_WINDOWS", False)
    assert dp._norm(r"C:\A\B") == r"C:\A\B"          # POSIX 原样，逐字无副作用
    monkeypatch.setattr(dp, "_WINDOWS", True)
    assert dp._norm(r"C:\Users\X") == "c:/users/x"   # Windows：大小写不敏感 + 分隔符归一
    assert dp._norm("C:/Users/X") == "c:/users/x"    # 两种写法归到同一个


def test_windows_system_and_venv_paths_are_code_or_system(monkeypatch):
    monkeypatch.setattr(dp, "_WINDOWS", True)
    assert dp._is_code_or_system(r"C:\Windows\System32\cmd.exe")
    assert dp._is_code_or_system(r"C:\Users\x\proj\.venv\Lib\site-packages\numpy\core.py")
    assert not dp._is_code_or_system(r"C:\Users\x\data\traj.json"), "真数据不该被当代码/系统"


def test_windows_own_territory_is_not_external(monkeypatch):
    """Windows 上本 run / 本项目领地的判定要大小写+分隔符不敏感（模型可能写 C:/ 或 C:\\）。"""
    monkeypatch.setattr(dp, "_WINDOWS", True)
    st = types.SimpleNamespace(
        hook_state={"_run_started_at": 1000.0},
        root=r"C:\Users\Fcbay\AppData\Local\afs\run",
        project_root=r"C:\Users\Fcbay\AppData\Local\afs\proj",
    )
    # 模型用小写盘符 + 正斜杠写自己领地内的路径 —— 归一后应判为「自己的」，不报外部
    mine = "c:/users/fcbay/appdata/local/afs/run/workspace/out.json"
    proj = "c:/users/fcbay/appdata/local/afs/proj/data.json"
    dp.record_tool_paths(st, "run_bash", {"cmd": f"cat {mine} {proj}"})
    assert dp.external_reads(st) == [], "自己领地被误判成外部依赖（大小写/分隔符没归一）"

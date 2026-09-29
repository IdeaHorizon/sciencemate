"""前处理边界机械对账：命中、白名单、run 级对账、以及必须无条件落事件。"""
from __future__ import annotations

import json
from pathlib import Path

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment.hooks import preprocessing_boundary_audit_on_end
from nodes.experiment.tools.preprocessing_boundary import scan_preprocessing_boundary


def _tool_call(state: State, name: str, **args) -> None:
    state.append_transcript("tool_call", turn=1, name=name, args=args)


def _events(state: State, event: str) -> list[dict]:
    if not state.transcript_path.exists():
        return []
    return [json.loads(line) for line in state.transcript_path.read_text().splitlines()
            if line.strip() and json.loads(line).get("event") == event]


def _capture_manual_writes(monkeypatch) -> list[dict]:
    from core import memory

    writes: list[dict] = []

    def capture(_state, **kwargs) -> None:
        writes.append(kwargs)

    monkeypatch.setattr(memory, "append_manual", capture)
    return writes


def test_clean_run_reports_zero_unaccounted(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="mpirun -np 8 vasp_std > run.log")

    report = scan_preprocessing_boundary(state)

    assert report["verdict"] == "PASS"
    assert report["n_unaccounted"] == 0
    assert report["n_hits"] == 0


def test_mesh_generation_without_any_data_route_is_unaccounted(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="gmsh -3 channel.geo -o channel.msh")

    report = scan_preprocessing_boundary(state)

    assert report["verdict"] == "VIOLATION"
    assert report["n_unaccounted"] == 1
    assert report["hits"][0]["label"] == "mesh_generator"


def test_writing_a_solver_input_file_is_a_hit(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_write_file", path="/work/run/POSCAR", content="Si\n1.0\n")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 1
    assert report["hits"][0]["label"] == "vasp_input"


def test_shell_redirection_into_an_input_file_is_a_hit(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="python build_kpoints.py > KPOINTS")

    report = scan_preprocessing_boundary(state)

    assert report["n_unaccounted"] == 1
    assert report["hits"][0]["label"] == "vasp_input_write"


def test_reading_an_input_file_is_not_a_hit(tmp_path: Path):
    """只读不算生成 —— 否则 `cat POSCAR` 这种检查都会被记成越界。"""
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="head -5 POSCAR && grep NSW INCAR")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 0


def test_contcar_to_poscar_restart_is_not_generation(tmp_path: Path):
    """harness 明文列为运行化轻调；复制不是生成，按语义就不该计入。"""
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="cp CONTCAR POSCAR && mpirun -np 8 vasp_std")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 0


def test_copying_a_delivered_input_into_the_workdir_is_not_generation(tmp_path: Path):
    """按 prereg 把已有输入复制进 workdir —— 同样是运行化操作，不是前处理。"""
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="cp /shared/delivered/POSCAR /work/run/POSCAR")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 0


def test_probing_a_generator_version_is_whitelisted(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="gmsh --version")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 0
    assert report["whitelisted"][0]["whitelist_reason"] == "probe_only"


def test_verbosity_flag_is_not_mistaken_for_a_probe(tmp_path: Path):
    """`-v 5` 是 gmsh 的 verbosity，不能让它把一次真实网格生成放掉。"""
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="gmsh -3 -v 5 model.geo -o model.msh")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 1
    assert report["n_unaccounted"] == 1


def test_going_through_the_data_service_accounts_for_the_hits(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    state.hook_state["input_delivery_state"] = {
        "spec": {"verified": True, "scientific_authority": True},
    }
    _tool_call(state, "safe_run_bash", command="gmsh -3 refine.geo -o refine.msh")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 1
    assert report["n_unaccounted"] == 0
    assert "verified_data_delivery" in report["accounting_reasons"]


def test_verified_authorized_experiment_fallback_accounts_for_the_hits(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    state.hook_state["input_delivery_state"] = {
        "spec": {
            "provider": "experiment_fallback",
            "verified": True,
            "fallback_authorized": True,
        },
    }
    _tool_call(state, "safe_run_bash", command="gmsh -3 channel.geo -o channel.msh")

    report = scan_preprocessing_boundary(state)

    assert report["verdict"] == "PASS"
    assert report["n_unaccounted"] == 0
    assert "verified_experiment_fallback" in report["accounting_reasons"]


def test_recorded_data_blocker_does_not_authorize_generated_inputs(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    state.hook_state["input_delivery_state"] = {
        "spec": {"verified": False, "data_terminally_blocked": True},
    }
    _tool_call(state, "safe_run_bash", command="gmsh -3 channel.geo -o channel.msh")

    report = scan_preprocessing_boundary(state)

    assert report["verdict"] == "VIOLATION"
    assert report["n_unaccounted"] == 1
    assert "data_terminal_blocker" not in report["accounting_reasons"]


def test_preprocessing_needed_section_does_not_authorize_generated_inputs(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    state.save_artifact(
        "experiment_log", "run",
        "## Execution Status\nstatus: incomplete\n\n## Preprocessing Needed\n缺 channel.msh，data 服务不可用。\n")
    _tool_call(state, "safe_run_bash", command="gmsh -3 channel.geo -o channel.msh")

    report = scan_preprocessing_boundary(state)

    assert report["verdict"] == "VIOLATION"
    assert report["n_unaccounted"] == 1
    assert report["observations"] == ["preprocessing_needed_declared"]


def test_operation_scope_accounts_for_the_hits(tmp_path: Path):
    """operation 产不出科学结论，冒烟算例不该被记成越界。"""
    state = State.new("experiment", tmp_path)
    state.hook_state["experiment_execution_scope"] = {"mode": "operational", "category": "toolchain_build"}
    _tool_call(state, "safe_execute_python", code="from ase.build import bulk\nbulk('Si').write('POSCAR')")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] >= 1
    assert report["n_unaccounted"] == 0
    assert "operation_scope" in report["accounting_reasons"]


def test_scientific_scope_does_not_account_for_the_hits(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    state.hook_state["experiment_execution_scope"] = {"mode": "scientific", "category": None}
    _tool_call(state, "safe_execute_python", code="from ase.build import bulk\nbulk('Si').write('POSCAR')")

    report = scan_preprocessing_boundary(state)

    assert report["n_unaccounted"] >= 1
    assert report["accounting_reasons"] == []


def test_hook_always_emits_the_event_even_on_a_clean_run(tmp_path: Path, monkeypatch):
    """mechanical fastpath 在事件缺席时判权威 fail —— 干净的 run 也必须有事件。"""
    state = State.new("experiment", tmp_path)
    memory_writes = _capture_manual_writes(monkeypatch)

    preprocessing_boundary_audit_on_end(HookContext(harness=None, state=state, messages=[], turn=0), None)

    events = _events(state, "preprocessing_boundary_audit")
    assert len(events) == 1
    assert events[0]["n_unaccounted"] == 0
    assert events[0]["verdict"] == "PASS"
    assert memory_writes == []


def test_hook_emits_the_event_even_when_the_scan_raises(tmp_path: Path, monkeypatch):
    """扫描炸了不能静默返回：没有事件 = QC 判权威 fail = 把 bug 变成违规判决。"""
    import nodes.experiment.hooks as hooks
    import nodes.experiment.tools.preprocessing_boundary as pb

    state = State.new("experiment", tmp_path)
    memory_writes = _capture_manual_writes(monkeypatch)
    monkeypatch.setattr(pb, "scan_preprocessing_boundary",
                        lambda _state: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(hooks, "_preprocessing_boundary", lambda: pb)

    preprocessing_boundary_audit_on_end(HookContext(harness=None, state=state, messages=[], turn=0), None)

    events = _events(state, "preprocessing_boundary_audit")
    assert len(events) == 1
    assert events[0]["n_unaccounted"] == 0
    assert events[0]["accounting_reasons"] == ["audit_scan_failed"]
    assert memory_writes == []


def test_hook_does_not_write_pitfall_when_all_hits_are_accounted(
        tmp_path: Path, monkeypatch):
    """有命中不等于有坑：全部对账时不能用 n_hits 放宽写入条件。"""
    state = State.new("experiment", tmp_path)
    state.hook_state["input_delivery_state"] = {
        "spec": {"verified": True, "scientific_authority": True},
    }
    _tool_call(state, "safe_run_bash", command="gmsh -3 refine.geo -o refine.msh")
    memory_writes = _capture_manual_writes(monkeypatch)

    preprocessing_boundary_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=0), None)

    event = _events(state, "preprocessing_boundary_audit")[-1]
    assert event["n_hits"] == 1
    assert event["n_unaccounted"] == 0
    assert memory_writes == []


def test_hook_records_unaccounted_hits_as_uncertain_mechanical_pitfall(
        tmp_path: Path, monkeypatch):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="gmsh -3 channel.geo -o channel.msh")
    memory_writes = _capture_manual_writes(monkeypatch)

    preprocessing_boundary_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=0), None)

    assert len(memory_writes) == 1
    text = memory_writes[0]["text"]
    assert "按文件名和生成器命令做的机械匹配" in text
    assert "不等于已确认的错误" in text


def test_hook_records_the_violation_count_for_the_quality_check(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="gmsh -3 channel.geo -o channel.msh")

    preprocessing_boundary_audit_on_end(HookContext(harness=None, state=state, messages=[], turn=0), None)

    event = _events(state, "preprocessing_boundary_audit")[-1]
    assert event["verdict"] == "VIOLATION"
    assert event["n_unaccounted"] == 1


def test_reading_results_with_ase_is_not_preprocessing(tmp_path: Path):
    """experiment 本职就包含解析实验输出；裸 import 不是前处理。"""
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_execute_python",
               code="from ase.io import read\natoms = read('OUTCAR', index=-1)\nprint(atoms.get_potential_energy())")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 0


def test_writing_an_analysis_csv_is_not_preprocessing(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="python collect.py > data.csv")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 0


def test_probe_in_one_segment_does_not_excuse_generation_in_another(tmp_path: Path):
    """自审实测的漏判：白名单按整条命令判时，一句 `--version` 能把同行的真生成放行。"""
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="gmsh --version && gmsh -3 a.geo -o a.msh")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 1
    assert report["n_whitelisted"] == 1
    assert report["n_unaccounted"] == 1


def test_probe_then_run_across_a_semicolon_is_still_caught(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_run_bash", command="which packmol; packmol < mix.inp")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 1
    assert report["n_unaccounted"] == 1


def test_python_reading_an_input_file_is_not_a_write(tmp_path: Path):
    """自审实测的误报：python 侧曾把所有引号字符串当写入目标，纯读也被记成生成。"""
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_execute_python",
               code="with open('POSCAR') as f:\n    print(f.read()[:100])")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 0


def test_python_writing_an_input_file_is_a_hit(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_execute_python",
               code="open('channel.msh', 'w').write(mesh_txt)")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 1
    assert report["hits"][0]["label"] == "mesh_file_write"


def test_parsing_run_output_with_ase_is_not_preprocessing(tmp_path: Path):
    """解析实验输出是本节点的职责，不能被记成越界前处理。"""
    state = State.new("experiment", tmp_path)
    _tool_call(state, "safe_execute_python",
               code="import ase.io\ntraj = ase.io.read('OUTCAR', index=':')\nprint(len(traj))")

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] == 0

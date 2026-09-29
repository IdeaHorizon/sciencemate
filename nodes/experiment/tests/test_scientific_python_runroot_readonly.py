"""scientific-primary 轻量 Python 的写边界由 OS capability 兑现。

AST 只能提前拒绝已识别的正式写入；它漏认的库方法和别名调用仍必须在
真实子进程里无法改写 run_root。唯一可写例外是本次 run 的
``.python-scratch``，供 TMPDIR 和科学库缓存使用。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from core import isolation, sandbox
from core.state import State
from nodes.experiment.tools import safe_bash
from nodes.experiment.tools.path_roles import experiment_output_dir
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    load_run_contract,
)
from nodes.experiment.tools.subprocess_policy import python_sandbox_roots


def _scientific_primary_state(tmp_path: Path) -> tuple[State, Path]:
    """Build the contract through the production frozen-prereg source."""
    state = State.new(
        "experiment",
        tmp_path / "runs",
        project_id="scientific-python-readonly",
    )
    prereg = state.save_artifact(
        "pre_registration",
        "scientific_python_readonly",
        "# Frozen preregistration\n",
        metadata={
            "execution_mode": "scientific",
            "run_role": "primary",
            "expected_params": {},
        },
    )
    state.mark_frozen(prereg["id"])
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg["id"],
        "fixture": "scientific_python_runroot_readonly",
        "requested_work": "verify the lightweight scientific Python boundary",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Execute the caller-bound primary scientific preregistration.",
    ))
    assert classified["status"] == "success", classified
    assert "run_contract" not in state.hook_state
    contract = load_run_contract(state)
    assert (contract["execution_mode"], contract["run_role"]) == (
        "scientific",
        "primary",
    )
    assert contract["prereg_artifact_id"] == prereg["id"]
    run_root = experiment_output_dir(state, "runtime", create=True).resolve()
    return state, run_root


def _evict_attempt(state: State) -> None:
    if isinstance(getattr(state, "sandbox_manifest", None), dict):
        sandbox.evict_state_attempt(state)


def _assert_os_write_denied(result: dict, label: str) -> None:
    detail = "\n".join(str(result.get(field) or "") for field in (
        "error", "stderr_tail", "stdout_tail"))
    assert any(marker in detail for marker in (
        "Read-only file system",
        "Permission denied",
        "[Errno 30]",
        "[Errno 13]",
    )), (label, result)
    assert result.get("reason") == "scientific_primary_run_root_readonly", (
        label,
        result,
    )
    assert "$TMPDIR" in str(result.get("recovery") or ""), (label, result)


def test_scientific_primary_python_roots_make_only_private_scratch_writable(
    tmp_path,
) -> None:
    state, run_root = _scientific_primary_state(tmp_path)
    scratch = run_root / ".python-scratch"
    (scratch / "tmp").mkdir(parents=True)

    writable, readonly = python_sandbox_roots(state, str(run_root))

    assert run_root in readonly
    assert run_root not in writable
    assert scratch in writable
    assert all(path == scratch or not path.is_relative_to(run_root) for path in writable)


def test_scientific_primary_scratch_symlink_fails_before_python_spawn(
    tmp_path,
    monkeypatch,
) -> None:
    state, run_root = _scientific_primary_state(tmp_path)
    scratch = run_root / ".python-scratch"
    scratch.symlink_to(".", target_is_directory=True)
    spawned = []

    async def forbidden(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("unsafe scratch must fail before Python spawn")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        "print('must not run')",
        cwd=str(run_root),
    ))

    assert result["status"] == "error"
    assert result["reason"] == "sandbox_path_contract_invalid"
    assert f"symlink at {scratch}" in result["error"]
    assert "delete that path and retry" in result["error"]
    assert spawned == []
    assert not (run_root / "tmp").exists()
    assert not (run_root / "cache").exists()


def test_operational_python_keeps_existing_run_root_write_capability(tmp_path) -> None:
    state = State.new("experiment", tmp_path / "runs")
    state.hook_state["node_inputs"] = {"fixture": "operational-python-root"}
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="Prove task 030 does not change operational Python behavior.",
    ))
    assert classified["status"] == "success", classified
    run_root = experiment_output_dir(state, "runtime", create=True).resolve()

    writable, _readonly = python_sandbox_roots(state, str(run_root))

    assert run_root in writable


def test_scientific_primary_blind_spot_writes_fail_in_real_process(
    tmp_path,
) -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")
    state, run_root = _scientific_primary_state(tmp_path)
    backend = isolation.select_backend()
    assert backend.name in {"linux", "darwin", "win32"}
    cases = {
        "to-csv": (
            "print('entered-to-csv', flush=True)\n"
            "import io\n"
            "class Frame:\n"
            "    def to_csv(self, path):\n"
            "        writer = io.open\n"
            "        with writer(path, 'w', encoding='utf-8') as stream:\n"
            "            stream.write('value\\n1\\n')\n"
            "df = Frame()\n"
            "df.to_csv('frame-out.csv')\n",
            run_root / "frame-out.csv",
        ),
        "absolute-open-alias": (
            "print('entered-absolute-open-alias', flush=True)\n"
            f"target = {str(run_root / 'absolute-out.txt')!r}\n"
            "writer = open\n"
            "with writer(target, 'w', encoding='utf-8') as stream:\n"
            "    stream.write('forbidden')\n",
            run_root / "absolute-out.txt",
        ),
    }
    try:
        for label, (code, target) in cases.items():
            result = asyncio.run(safe_bash._safe_execute_python(
                state,
                code,
                cwd=str(run_root),
                timeout=30,
            ))
            assert result["status"] == "error", (label, result)
            assert int(result.get("returncode") or 0) != 0, (label, result)
            assert f"entered-{label}" in str(result.get("stdout_tail") or ""), (
                label,
                result,
            )
            _assert_os_write_denied(result, label)
            assert not target.exists(), (label, target)
    finally:
        _evict_attempt(state)


@pytest.mark.parametrize(
    ("label", "module", "code", "target_name"),
    [
        (
            "numpy",
            "numpy",
            "print('entered-numpy', flush=True)\n"
            "import numpy as np\n"
            "np.save('numpy-out.npy', [1, 2, 3])\n",
            "numpy-out.npy",
        ),
        (
            "matplotlib",
            "matplotlib.pyplot",
            "print('entered-matplotlib', flush=True)\n"
            "import matplotlib.pyplot as plt\n"
            "plt.plot([0, 1], [0, 1])\n"
            "plt.savefig('figure-out.png')\n",
            "figure-out.png",
        ),
    ],
)
def test_scientific_primary_optional_library_writes_reach_os_wall(
    tmp_path,
    label,
    module,
    code,
    target_name,
) -> None:
    repo_venv_lib = Path(__file__).resolve().parents[3] / ".venv" / "lib"
    child_site_packages = sorted(repo_venv_lib.glob("python*/site-packages"))
    expected_abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if any(path.parent.name != expected_abi for path in child_site_packages):
        pytest.skip(
            "gate interpreter ABI differs from the product venv used for optional libraries"
        )
    pytest.importorskip(
        module,
        reason=f"the CI Python ABI does not install optional module {module}",
    )
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")
    state, run_root = _scientific_primary_state(tmp_path)
    target = run_root / target_name
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state,
            code,
            cwd=str(run_root),
            timeout=30,
        ))
        assert result["status"] == "error", (label, result)
        assert int(result.get("returncode") or 0) != 0, (label, result)
        assert f"entered-{label}" in str(result.get("stdout_tail") or ""), (
            label,
            result,
        )
        _assert_os_write_denied(result, label)
        assert not target.exists(), (label, target)
    finally:
        _evict_attempt(state)


def test_scientific_primary_sqlite_readonly_failure_explains_safe_paths(
    tmp_path,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    result = safe_bash._python_resource_envelope(
        {
            "status": "error",
            "returncode": 1,
            "stderr_tail": "sqlite3.OperationalError: disk I/O error",
        },
        scientific_primary=True,
        code="import sqlite3\nsqlite3.connect('results.db')",
        cwd=str(run_root),
        run_roots=[run_root],
        scratch_tmp=run_root / ".python-scratch" / "tmp",
    )

    assert result["reason"] == "scientific_primary_run_root_readonly"
    assert "mode=ro&immutable=1" in result["recovery"]
    assert "-journal/-wal/-shm" in result["recovery"]
    assert "禁止使用 immutable=1" in result["recovery"]
    assert "$TMPDIR" in result["recovery"]
    assert str(run_root / ".python-scratch" / "tmp") in result["recovery"]


def test_scientific_primary_readonly_reason_requires_run_root_path(tmp_path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    external = safe_bash._python_resource_envelope(
        {
            "status": "error",
            "returncode": 1,
            "stderr_tail": "PermissionError: [Errno 13] Permission denied: '/etc/shadow'",
        },
        scientific_primary=True,
        code="open('/etc/shadow').read()",
        cwd=str(run_root),
        run_roots=[run_root],
    )
    assert external.get("reason") != "scientific_primary_run_root_readonly"
    assert "recovery" not in external


def test_scientific_primary_landlock_style_run_root_error_is_attributed(
    tmp_path,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    target = run_root / "results.txt"
    result = safe_bash._python_resource_envelope(
        {
            "status": "error",
            "returncode": 1,
            "stderr_tail": (
                f"PermissionError: [Errno 13] Permission denied: {str(target)!r}"
            ),
        },
        scientific_primary=True,
        code=f"open({str(target)!r}, 'w').write('x')",
        cwd=str(run_root),
        run_roots=[run_root],
    )
    assert result["reason"] == "scientific_primary_run_root_readonly"
    assert "tempfile" in result["recovery"]
    assert "safe_run_bash cp" in result["recovery"]


def _sqlite_error_envelope(code: str, run_root: Path) -> dict:
    return safe_bash._python_resource_envelope(
        {
            "status": "error",
            "returncode": 1,
            "stderr_tail": "sqlite3.OperationalError: disk I/O error",
        },
        scientific_primary=True,
        code=code,
        cwd=str(run_root),
        run_roots=[run_root],
        scratch_tmp=run_root / ".python-scratch" / "tmp",
    )


def test_sqlite_reason_rejects_mixed_inside_and_outside_literals(tmp_path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    outside = tmp_path / "outside.db"
    result = _sqlite_error_envelope(
        "import sqlite3\n"
        "if False:\n"
        "    sqlite3.connect('never-executed-inside.db')\n"
        f"sqlite3.connect({str(outside)!r})\n",
        run_root,
    )
    assert result.get("reason") != "scientific_primary_run_root_readonly"
    assert "禁止使用 immutable=1" in str(result.get("recovery") or "")


@pytest.mark.parametrize(
    "dynamic_call",
    [
        "sqlite3.connect(database_path)",
        "sqlite3.connect(database=database_path)",
        "sq.connect(database_path)",
        "connect(database_path)",
        "assigned_connect(database_path)",
    ],
)
def test_sqlite_reason_rejects_inside_literal_plus_dynamic_path(
    tmp_path,
    dynamic_call,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    result = _sqlite_error_envelope(
        "import sqlite3\n"
        "import sqlite3 as sq\n"
        "from sqlite3 import connect\n"
        "assigned_connect = sqlite3.connect\n"
        "sqlite3.connect('inside.db')\n"
        f"{dynamic_call}\n",
        run_root,
    )
    assert result.get("reason") != "scientific_primary_run_root_readonly"
    assert "禁止使用 immutable=1" in str(result.get("recovery") or "")


@pytest.mark.parametrize(
    "code",
    [
        "import sqlite3\nsqlite3.connect('inside.db')\n",
        (
            "import sqlite3\n"
            "sqlite3.connect('inside-a.db')\n"
            "sqlite3.connect('inside-b.db')\n"
        ),
        "import sqlite3\nsqlite3.dbapi2.connect('inside.db')\n",
        "import sqlite3\ngetattr(sqlite3, 'connect')('inside.db')\n",
        (
            "from sqlite3 import connect as open_db\n"
            "open_db('inside.db')\n"
        ),
        (
            "import sqlite3\n"
            "open_db = sqlite3.connect\n"
            "open_db('inside.db')\n"
        ),
    ],
)
def test_sqlite_reason_accepts_complete_all_inside_literals(
    tmp_path,
    code,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    result = _sqlite_error_envelope(code, run_root)
    assert result["reason"] == "scientific_primary_run_root_readonly"
    assert "禁止使用 immutable=1" in result["recovery"]


@pytest.mark.parametrize(
    "escaped_call",
    [
        "sqlite3.dbapi2.connect(OUTSIDE)",
        "getattr(sqlite3, 'connect')(OUTSIDE)",
        "connectors = (sqlite3.connect,)\nconnectors[0](OUTSIDE)",
        "holder = {'open': sqlite3.connect}\nholder['open'](OUTSIDE)",
        "dispatch(sqlite3.connect, OUTSIDE)",
    ],
)
def test_sqlite_reason_rejects_unresolved_capability_references(
    tmp_path,
    escaped_call,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    outside = tmp_path / "outside.db"
    result = _sqlite_error_envelope(
        "import sqlite3\n"
        "sqlite3.connect('dormant-inside.db')\n"
        f"{escaped_call.replace('OUTSIDE', repr(str(outside)))}\n",
        run_root,
    )
    assert result.get("reason") != "scientific_primary_run_root_readonly"
    assert "禁止使用 immutable=1" in str(result.get("recovery") or "")


def test_scientific_primary_absolute_run_root_write_fails_from_build_root(
    tmp_path,
) -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")
    state, run_root = _scientific_primary_state(tmp_path)
    build_root = experiment_output_dir(state, "build", create=True).resolve()
    target = run_root / "from-build-root.txt"
    code = (
        "print('entered-build-root-write', flush=True)\n"
        f"target = {str(target)!r}\n"
        "writer = open\n"
        "with writer(target, 'w', encoding='utf-8') as stream:\n"
        "    stream.write('forbidden')\n"
    )
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state,
            code,
            cwd=str(build_root),
            timeout=30,
        ))
        assert result["status"] == "error", result
        assert int(result.get("returncode") or 0) != 0, result
        assert "entered-build-root-write" in str(
            result.get("stdout_tail") or "")
        assert not target.exists()
    finally:
        _evict_attempt(state)


def test_bound_primary_prereg_operation_redeclaration_keeps_wall(
    tmp_path,
) -> None:
    state, run_root = _scientific_primary_state(tmp_path)
    redeclared = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="Attempt to redeclare without replacing the bound preregistration.",
    ))
    assert redeclared["status"] == "success", redeclared
    contract = load_run_contract(state)
    assert (contract["execution_mode"], contract["run_role"]) == (
        "scientific",
        "primary",
    )

    scratch = run_root / ".python-scratch"
    (scratch / "tmp").mkdir(parents=True)
    writable, readonly = python_sandbox_roots(state, str(run_root))

    assert run_root in readonly
    assert run_root not in writable
    assert scratch in writable


def test_scientific_primary_read_print_and_stdlib_tmp_save_succeed(
    tmp_path,
) -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")
    state, run_root = _scientific_primary_state(tmp_path)
    source = run_root / "input.json"
    source.write_text(json.dumps({"value": 7}), encoding="utf-8")
    code = (
        "import json, os, tempfile\n"
        f"with open({str(source)!r}, 'r', encoding='utf-8') as stream:\n"
        "    print('value=' + str(json.load(stream)['value']), flush=True)\n"
        "with tempfile.NamedTemporaryFile(prefix='allowed-', suffix='.txt', delete=False) as stream:\n"
        "    stream.write(b'allowed')\n"
        "    target = stream.name\n"
        "print('tmp-saved=' + str(os.path.exists(target)), flush=True)\n"
        "print('tmp-target=' + target, flush=True)\n"
    )
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state,
            code,
            cwd=str(run_root),
            timeout=30,
        ))
        assert result["status"] == "success", result
        assert "value=7" in result["stdout_tail"]
        assert "tmp-saved=True" in result["stdout_tail"]
        scratch_tmp = run_root / ".python-scratch" / "tmp"
        assert list(scratch_tmp.glob("allowed-*.txt"))
    finally:
        _evict_attempt(state)


def test_scientific_primary_matplotlib_can_save_to_tmp_when_available(
    tmp_path,
) -> None:
    repo_venv_lib = Path(__file__).resolve().parents[3] / ".venv" / "lib"
    child_site_packages = sorted(repo_venv_lib.glob("python*/site-packages"))
    expected_abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if any(path.parent.name != expected_abi for path in child_site_packages):
        pytest.skip(
            "gate interpreter ABI differs from the product venv used for optional libraries"
        )
    pytest.importorskip(
        "matplotlib.pyplot",
        reason="the CI Python ABI does not install the optional plotting stack",
    )
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")
    state, run_root = _scientific_primary_state(tmp_path)
    code = (
        "import os\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([0, 1], [1, 0])\n"
        "target = os.path.join(os.environ['TMPDIR'], 'allowed-figure.png')\n"
        "plt.savefig(target)\n"
        "print('tmp-saved=' + str(os.path.exists(target)), flush=True)\n"
    )
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state,
            code,
            cwd=str(run_root),
            timeout=30,
        ))
        assert result["status"] == "success", result
        assert "tmp-saved=True" in result["stdout_tail"]
        assert (run_root / ".python-scratch" / "tmp" / "allowed-figure.png").is_file()
    finally:
        _evict_attempt(state)


def test_scientific_primary_literal_open_write_keeps_early_managed_hint(
    tmp_path,
    monkeypatch,
) -> None:
    state, run_root = _scientific_primary_state(tmp_path)
    spawned = []

    async def forbidden(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("literal open write must stop before executor")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        "open('r.txt', 'w').write('x')",
        cwd=str(run_root),
    ))

    assert result["status"] == "error"
    assert result["reason"] == "managed_python_workload_required"
    assert spawned == []

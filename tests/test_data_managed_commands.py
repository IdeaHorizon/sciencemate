"""Data command adapters share the real process wall and cancellation owner."""

import asyncio

import pytest

from core.state import State
from nodes.data.tools import mesh_generator


@pytest.mark.asyncio
async def test_a_stopped_data_run_never_starts_another_mesher(tmp_path):
    state = State.new("data", tmp_path)
    state.kill_event = asyncio.Event()
    state.kill_event.set()
    called = []

    def generator(**params):
        called.append(params)
        return {"status": "success"}

    with pytest.raises(asyncio.CancelledError):
        await mesh_generator._run_mesh_worker(state, generator)
    assert called == []


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["signal", "task_cancel"])
async def test_async_mesh_review_cancellation_reaps_its_real_process(tmp_path, how):
    import sys

    from nodes.data.tools.managed_commands import run_data_command
    from shared.lib.process_control import alive

    state = State.new("data", tmp_path / "runs")
    state.kill_event = asyncio.Event()
    marker = state.root / "scratch" / "payload.pid"
    calls = []

    async def generator(*, state, **params):
        calls.append("start")
        await run_data_command(
            state,
            [
                sys.executable,
                "-c",
                "import os,time;from pathlib import Path;"
                "Path('payload.pid').write_text(str(os.getpid()));time.sleep(60)",
            ],
            timeout=20,
        )
        calls.append("after")
        return {}

    async def reviewed(*, state):
        return await mesh_generator._generate_gmsh_mesh_with_review(
            generator, "test", case_dir=str(state.root / "case"), state=state
        )

    task = asyncio.create_task(mesh_generator._run_mesh_worker(state, reviewed))
    try:
        async with asyncio.timeout(8):
            while not marker.exists() and not task.done():
                await asyncio.sleep(0.01)
        assert marker.exists(), await task
        pid = int(marker.read_text())
        if how == "signal":
            state.kill_event.set()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        assert not alive(pid)
        assert calls == ["start"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_data_command_uses_the_real_write_boundary_and_keeps_complete_output(tmp_path):
    import sys

    from core import isolation
    from nodes.data.tools.managed_commands import run_data_command

    state = State.new("data", tmp_path / "runs")
    outside = tmp_path / "outside"
    result = await run_data_command(
        state, [sys.executable, "-c", "import sys;sys.stdout.write('header:'+('a'*300000)+':end')"]
    )
    assert result.returncode == 0
    assert result.stdout == "header:" + "a" * 300000 + ":end"
    if isolation.Invariant.WRITE_BOUNDARY in isolation.select_backend().capabilities():
        result = await run_data_command(
            state,
            [
                sys.executable,
                "-c",
                f"from pathlib import Path;Path({str(outside)!r}).write_text('must be denied')",
            ],
        )
        assert result.returncode != 0
        assert not outside.exists()


@pytest.mark.asyncio
async def test_shell_validation_parses_but_never_executes_the_generated_artifact(tmp_path):
    from core.project_workspace import run_scratch_directory
    from nodes.data.tools.preprocessing_capabilities import _validate_generated_artifact_format

    state = State.new("data", tmp_path)
    script = run_scratch_directory(state) / "script.sh"
    script.parent.mkdir(parents=True)
    marker = script.parent / "must-not-exist"
    script.write_text("echo wrong > must-not-exist\n")
    assert await _validate_generated_artifact_format(script, "shell", state=state) is None
    assert not marker.exists()
    script.write_text("if then\n")
    assert "shell syntax error" in await _validate_generated_artifact_format(
        script, "shell", state=state
    )


@pytest.mark.asyncio
async def test_pdf_fallback_enters_the_same_managed_command_owner(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from nodes.data.tools import atomic_structure_recovery as recovery
    from nodes.data.tools.managed_commands import run_data_command

    state = State.new("data", tmp_path)
    pdf = state.root / "reference.pdf"
    pdf.write_bytes(b"%PDF incomplete test fixture")
    monkeypatch.setattr(recovery, "_extract_pdf_with_pypdf", lambda *_: "")

    def unavailable(*_):
        raise ValueError("fixture requires the external extractor")

    monkeypatch.setitem(sys.modules, "PyPDF2", SimpleNamespace(PdfReader=unavailable))
    calls = []

    async def extracted(state, argv, **kwargs):
        calls.append(argv)
        assert argv == ["pdftotext", "-layout", str(pdf), "-"]
        return await run_data_command(
            state, [sys.executable, "-c", "print('extracted reference')"], **kwargs
        )

    monkeypatch.setattr(recovery, "run_data_command", extracted)
    text, requirement = await recovery._read_reference_document(pdf, state=state)
    assert text == "extracted reference\n" and requirement is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_gmsh_readback_preserves_the_measured_input_identity(tmp_path, monkeypatch):
    import hashlib
    import sys

    from nodes.data.tools.managed_commands import run_data_command

    state = State.new("data", tmp_path)
    mesh = state.root / "mesh.msh"
    mesh.write_text(
        "$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Nodes\n4\n"
        "1 0 0 0\n2 1 0 0\n3 0 1 0\n4 0 0 1\n$EndNodes\n"
        "$Elements\n1\n1 4 2 1 1 1 2 3 4\n$EndElements\n"
    )

    async def normalize(state, argv, **kwargs):
        destination = argv[argv.index("-o") + 1]
        return await run_data_command(
            state,
            [
                sys.executable,
                "-c",
                "import shutil,sys;shutil.copyfile(sys.argv[1],sys.argv[2])",
                str(mesh),
                destination,
            ],
            **kwargs,
        )

    monkeypatch.setattr(mesh_generator, "run_data_command", normalize)
    quality = await mesh_generator.read_gmsh_mesh_quality(str(mesh), state=state)
    assert quality["n_nodes"] == 4 and quality["n_3d_elements"] == 1
    assert quality["measurement_source"]["sha256"] == hashlib.sha256(mesh.read_bytes()).hexdigest()
    assert quality["readback"]["returncode"] == 0


@pytest.mark.asyncio
async def test_data_command_timeout_is_bounded_and_reaps_the_payload(tmp_path):
    import subprocess
    import sys

    from nodes.data.tools.managed_commands import run_data_command
    from shared.lib.process_control import alive

    state = State.new("data", tmp_path)
    with pytest.raises(subprocess.TimeoutExpired):
        await run_data_command(
            state,
            [
                sys.executable,
                "-c",
                "import os,time;from pathlib import Path;"
                "Path('timed.pid').write_text(str(os.getpid()));time.sleep(60)",
            ],
            timeout=1,
        )
    assert not alive(int((state.root / "scratch" / "timed.pid").read_text()))


@pytest.mark.asyncio
async def test_an_unavailable_wall_does_not_fall_back_to_host_execution(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from core import isolation
    from nodes.data.tools.managed_commands import run_data_command

    state = State.new("data", tmp_path)
    marker = state.root / "host-fallback"

    def reject(*_args, **_kwargs):
        raise RuntimeError("test wall unavailable")

    monkeypatch.setattr(
        isolation, "select_backend", lambda: SimpleNamespace(name="test", prepare=reject)
    )
    with pytest.raises(OSError, match="test wall unavailable"):
        await run_data_command(
            state,
            [
                sys.executable,
                "-c",
                f"from pathlib import Path;Path({str(marker)!r}).write_text('unsafe')",
            ],
        )
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [True, False])
async def test_data_command_text_mode_matches_subprocess_newlines(tmp_path, text):
    import sys

    from nodes.data.tools.managed_commands import run_data_command

    state = State.new("data", tmp_path)
    result = await run_data_command(
        state,
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'first\\r\\nsecond\\rtail')"],
        text=text,
    )
    assert result.stdout == ("first\nsecond\ntail" if text else b"first\r\nsecond\rtail")


@pytest.mark.asyncio
async def test_adapter_timeout_is_not_mistaken_for_a_progress_timer_tick(tmp_path, monkeypatch):
    state = State.new("data", tmp_path)

    async def failed_adapter(*, state):
        raise TimeoutError("adapter I/O timed out")

    def false_progress(*args, **kwargs):
        pytest.fail("a completed adapter exception is not a progress timer")

    monkeypatch.setattr(mesh_generator, "emit_progress", false_progress)
    with pytest.raises(TimeoutError, match="adapter I/O timed out"):
        await mesh_generator._run_mesh_worker(state, failed_adapter)

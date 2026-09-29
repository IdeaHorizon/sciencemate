from __future__ import annotations

from pathlib import Path

from core.state import State
from nodes.experiment.tools import resource_manager as manager


class _FakeLaunchAdapter:
    adapter_id = "fake-local-docker"

    def __init__(self, events: list[str], *, runtime_id: str = "a" * 64):
        self.events = events
        self.runtime_id = runtime_id

    def prepare(self, spec):
        self.events.append("prepare")
        return manager._PreparedSubmissionLaunch(
            intent_fields={
                "job_id": "hf-fake-launch",
                "sandbox_control_dir": str(Path(spec["cwd"]) / ".fake-control"),
                "sandbox_image": "test-image",
                "sandbox_image_id": "sha256:" + "b" * 64,
            },
            opaque={"spec": spec},
        )

    def launch(self, prepared):
        del prepared
        self.events.append("launch")
        return {"ok": True, "returncode": 0, "stdout": self.runtime_id, "stderr": ""}

    def abandon(self, prepared):
        del prepared
        self.events.append("abandon")

    def accepted_receipt(self, prepared, launch_result):
        del prepared
        return {"container_runtime_id": str(launch_result["stdout"])}


def _submit_local(state: State, attempt: str):
    runtime = manager.experiment_output_dir(state, "runtime", create=True).resolve()
    return manager._submit_sync(
        runtime_root=runtime,
        scheduler="local",
        command="echo adapter",
        job_name="adapter-contract",
        mpi_ranks=1,
        cpus_per_rank=1,
        gpus=0,
        memory_gb=1.0,
        storage_gb=1.0,
        walltime_minutes=1,
        queue=None,
        nodelist=None,
        image=None,
        workdir=str(runtime),
        dry_run=False,
        namespace=None,
        stage_in=None,
        state=state,
        submission_nonce=attempt,
        route_attempt_id=attempt,
    )


def _prepare_fake_local(monkeypatch, state: State, adapter: _FakeLaunchAdapter):
    monkeypatch.setattr(
        manager,
        "_local_job_sandbox_roots",
        lambda *_args, **_kwargs: (
            [manager.experiment_output_dir(state, "runtime", create=True)],
            [],
        ),
    )
    monkeypatch.setattr(manager, "_submission_launch_adapter_for", lambda _scheduler: adapter)


def test_launch_adapter_protocol_and_factory_are_closed_to_deployment():
    adapter = manager._NativeLocalJobLaunchAdapter()

    assert isinstance(adapter, manager._SubmissionLaunchAdapter)
    assert manager._submission_launch_adapter_for("local").adapter_id == "native-local-job"
    assert manager._submission_launch_adapter_for("slurm") is None
    assert manager._submission_launch_adapter_for("pbs") is None


def test_fake_adapter_prepare_then_durable_intent_then_launch(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path / "runs")
    events: list[str] = []
    adapter = _FakeLaunchAdapter(events)
    _prepare_fake_local(monkeypatch, state, adapter)
    original_persist = manager._persist_submission_intent

    def persist_then_observe(*args, **kwargs):
        assert events == ["prepare"]
        result = original_persist(*args, **kwargs)
        assert result["status"] == "success"
        events.append("intent")
        return result

    monkeypatch.setattr(manager, "_persist_submission_intent", persist_then_observe)
    result = _submit_local(state, "route-adapter-success")

    assert result["status"] == "success", result
    assert result["launch_adapter"] == "fake-local-docker"
    assert result["container_runtime_id"] == "a" * 64
    assert events == ["prepare", "intent", "launch"]


def test_intent_failure_abandons_prepared_adapter_without_launch(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path / "runs")
    events: list[str] = []
    adapter = _FakeLaunchAdapter(events)
    _prepare_fake_local(monkeypatch, state, adapter)
    monkeypatch.setattr(
        manager,
        "_persist_submission_intent",
        lambda *_args, **_kwargs: {"status": "error", "reason": "ledger_unavailable"},
    )

    result = _submit_local(state, "route-adapter-intent-failure")

    assert result["status"] == "error"
    assert events == ["prepare", "abandon"]


def test_unknown_runtime_identity_keeps_prepared_adapter_for_reconciliation(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path / "runs")
    events: list[str] = []
    adapter = _FakeLaunchAdapter(events, runtime_id="not-an-immutable-runtime-id")
    _prepare_fake_local(monkeypatch, state, adapter)

    result = _submit_local(state, "route-adapter-unknown")

    assert result["status"] == "submission_outcome_unknown"
    assert result["do_not_resubmit"] is True
    assert events == ["prepare", "launch"]

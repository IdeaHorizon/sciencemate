"""GPU runtime verification must not invent a build or MPI prerequisite."""

from pathlib import Path


def test_gpu_skill_allows_preinstalled_runtime_without_nvcc_or_mpi():
    text = (
        Path(__file__).parents[1] / "skills" / "gpu-hpc-porting" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "For a preinstalled runtime workload, do not require `nvcc`" in text
    assert "Probe MPI wrappers only when the workload uses MPI" in text
    assert "fail closed" in text
    assert "falling back to CPU" in text
    assert "A CPU/GPU performance comparison is optional" in text

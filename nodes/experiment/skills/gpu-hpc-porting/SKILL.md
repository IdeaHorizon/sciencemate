---
name: gpu-hpc-porting
description: |
  Build, diagnose, and verify NVIDIA CUDA-enabled HPC or AI applications.
  Use only after hardware and backend evidence establishes CUDA; for ROCm,
  Intel oneAPI, or SYCL, follow the target software's official documentation.
applies_when:
  - Building an HPC or AI application with an NVIDIA CUDA backend
  - Diagnosing GPU architecture, CUDA-host compiler, linker, or runtime failures
  - Verifying that a completed run actually used the GPU
tools_used:
  - read_file
  - safe_run_bash
  - safe_execute_python
expected_outcome: GPU binary built and acceleration verified against an equivalent CPU workload
status: validated
---

# NVIDIA CUDA HPC Build and Verification

This skill is conditional.  Do not infer CUDA from the word “GPU”.  First record
the device, compute capability, driver, toolkit, host compiler, MPI wrappers,
and the target software's documented backend.

## Required workflow

1. Establish hardware and toolchain evidence before changing configuration:

   ```bash
   nvidia-smi --query-gpu=name,compute_cap,memory.total,driver_version --format=csv
   nvcc --version
   gcc --version
   which nvcc mpicc mpicxx mpif90
   mpicc -show
   mpif90 -show
   ```

2. Read the target software's official GPU build instructions and the frozen
   run contract.  Use the software's native NVIDIA GPU package/backend first
   for a single-GPU workload.  Escalate to KOKKOS, RAJA, SYCL, or another
   abstraction only when the native route cannot satisfy the preregistered
   multi-GPU, portability, or performance requirement; record the reason.

3. Check one coherent toolchain before the first build.  CUDA, the host
   compiler, MPI wrappers, BLAS/LAPACK libraries, and the launcher must come
   from compatible installations.  Fix module/PATH selection before changing
   source or generated configuration.  Never patch vendor CUDA headers as the
   first response to a host-compiler error.

4. Match the binary to the detected GPU architecture.  For CUDA, explicitly
   pass the target through the application's official interface, such as
   `-arch=sm_XX` or `GPU_ARCH=sm_XX`; do not assume every build system accepts
   the same spelling.  Never rely on auto-detection when the interface exposes
   an architecture option.

5. Build in the declared `build_root` or `source_worktree_root`; never modify
   `source_baseline_root`.  Preserve configure/build commands, compiler and
   toolkit versions, architecture flags, and the resulting binary path.

6. Verify actual acceleration using an equivalent CPU/GPU workload permitted
   by the preregistration.  Record application backend evidence, GPU utilization,
   wall time, workload size, and comparison limits.  There is no universal
   cross-application speedup threshold.

## Architecture and compatibility reminders

| GPU family | Typical compute capability | Toolkit floor to verify |
|---|---:|---:|
| Pascal | `sm_60/61` | CUDA 8+ |
| Volta | `sm_70` | CUDA 9+ |
| Turing | `sm_75` | CUDA 10+ |
| Ampere | `sm_80/86` | CUDA 11+ |
| Ada | `sm_89` | CUDA 11.8+ |
| Hopper | `sm_90` | CUDA 11.8+ |
| Blackwell | `sm_100/120` | CUDA 13+; verify exact device support |

The table is a starting probe, not a substitute for the NVIDIA support matrix
or the target software's build documentation.  `no kernel image available` and
`cudaErrorInvalidDeviceFunction` normally require comparing the binary's target
list with the detected compute capability.  `invalid value for -arch` normally
means the toolkit is too old or the target spelling is unsupported.

Common evidence-led checks:

- CUDA compiler rejects host code: compare the CUDA-host compiler compatibility
  matrix, then select a supported compiler/toolkit pair.
- `fpclassify`, `isgreater`, or CCCL namespace errors: check CUDA/GCC/C++ mode;
  prefer the native GPU route or a supported toolchain before any source patch.
- GPU binary and launcher use different MPI families: compare `mpicc -show`,
  `mpirun --version`, and `ldd <binary>`.
- GPU utilization is zero or performance is anomalous: inspect the application's
  backend report, workload size, process binding, and device telemetry; do not
  call acceleration verified from a successful process exit alone.

## LAMMPS-specific reminder

For a single NVIDIA GPU, prefer the official native GPU package when it covers
the preregistered features.  If the official build uses `GPU_ARCH`, pass the
detected `sm_XX` target explicitly.  In the input script, place `package gpu 1`
before commands that define the simulation box (`lattice`, `region`, or
`create_box`).  Confirm the package is enabled at runtime; a binary that merely
starts is not proof that GPU kernels ran.

## Diagnostic discipline

For every failure: classify compiler/linker/runtime/configuration/environment/
resource; state one falsifiable hypothesis; run the cheapest disambiguating
probe; apply one scoped fix; then verify the expected artifact or observation.
On the second occurrence change the hypothesis and consult the target version's
official documentation or its relevant Spack recipe. On the third stop repeating
the same fix and record the
failed evidence.  On the fifth create a dead-end record, request human input,
or choose an explicitly authorized alternate route.

Do not install drivers, CUDA toolkits, or system packages without human
approval.  Do not replace a preregistered GPU run with a CPU result silently.

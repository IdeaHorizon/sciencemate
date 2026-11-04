# Windows sandbox probe

Measures, on a real Windows machine and as an ordinary non-admin user, what a payload can and
cannot do under three launch modes:

| mode | what it is |
|---|---|
| `medium` | plain child process; the control group that proves each check can succeed |
| `lowil` | today's `win32` backend shape: the user's own token lowered to Low integrity |
| `appcontainer` | zero-capability AppContainer (no `internetClient`, no other capability) |

Every launch also sits in a Job Object (kill-on-close, per-process memory cap). This is the
evidence behind `docs/RFC_WINDOWS_EXECUTION_TARGETS_20261030.md` §6; results of the 2026-11-03
run are in `docs/windows_appcontainer_probe_20261103/`.

## Layout it expects (`<ROOT>`, any folder the user owns)

```
<ROOT>\python\   a copy of the bundled CPython (python-build-standalone 3.12.x)
<ROOT>\pwsh\     PowerShell 7 portable zip, extracted (check SHA256 against the release's hashes.sha256
                 and Get-AuthenticodeSignature = Valid, Microsoft Corporation)
<ROOT>\torchlib\ optional: pip install --target <ROOT>\torchlib torch==<x>+cu128 (for run_torch.py),
                 then `icacls <ROOT>\torchlib /reset /T /C /Q`: pip moves the package in from %TEMP%
                 with TEMP's protected DACL, which the AppContainer cannot read
<ROOT>\harness\  this folder
```

The probe creates `<ROOT>\work\` and changes ACLs / integrity labels only below `<ROOT>`.
It creates the AppContainer profile `afs.probe.appcontainer.v1` and deletes it at the end.

## Scripts (run each with the copied python: `<ROOT>\python\python.exe <script> <ROOT>`)

| script | question |
|---|---|
| `ac_probe.py` | the matrix: python / pwsh / pwsh with redirected home / CUDA driver API, in all three modes |
| `ace_matrix.py` | which explicit ACE on an interpreter folder breaks Low-IL launches from it |
| `pwsh_writes.py` | which PowerShell file operations work in the AppContainer with absolute `C:\` paths |
| `subst_drive.py` | the same operations when the run root is mapped to a drive letter (no admin) |
| `lowil_column.py` | the Low-IL column, from a folder granted only to ALL APPLICATION PACKAGES |
| `run_torch.py` | torch: `cuda.is_available()`, GPU matmul vs CPU, in all three modes |

Payloads: `payload_py.py`, `payload_ps.ps1`, `payload_ps_writes.ps1`, `payload_ps_drive.ps1`,
`cuda_probe.py` (driver API + PTX vector add, no torch), `torch_probe.py`.

Launched over ssh, the probe runs in the console session of whoever is logged in on that PC; it uses
`CREATE_NO_WINDOW` and never opens UI.

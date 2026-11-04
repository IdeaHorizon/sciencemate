"""The Low-IL column: payloads launched at Low IL from a folder granted only to ALL APPLICATION
PACKAGES (a specific-AppContainer-SID ACE would make Low IL fail; see ace_matrix.py).
usage: python lowil_column.py <ROOT>"""

import json
import os
import shutil
import sys

ROOT = sys.argv[1]
sys.argv = [sys.argv[0], ROOT]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_probe as a  # noqa: E402

shared = a.WORK / "scripts_shared"
shared.mkdir(exist_ok=True)
for n in ("payload_py.py", "payload_ps.ps1", "cuda_probe.py"):
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), n), shared / n)
a._icacls(shared, "/grant", "*S-1-15-2-1:(OI)(CI)RX", "/Q")
home = a.DIRS["LOW_ONLY"] / "home"
for sub in ("Local", "Roaming", "Temp"):
    (home / sub).mkdir(parents=True, exist_ok=True)
srv = a.listener()
env = dict(os.environ)
env.update({f"PROBE_{k}": str(v) for k, v in a.DIRS.items()})
env.update(
    PROBE_PARENT=str(a.WORK),
    PROBE_DOCS=str(os.path.join(os.environ["USERPROFILE"], "Documents")),
    PROBE_LOOPBACK_PORT=str(srv.getsockname()[1]),
    PROBE_PY=str(a.PY),
    TEMP=str(home / "Temp"),
    TMP=str(home / "Temp"),
    PYTHONDONTWRITEBYTECODE="1",
    LOCALAPPDATA=str(home / "Local"),
    APPDATA=str(home / "Roaming"),
    CUDA_CACHE_PATH=str(home / "cuda_cache"),
)
ps = [
    str(a.PWSH),
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    str(shared / "payload_ps.ps1"),
]
out = {
    "python": a.launch(
        "lowil", [str(a.PY), str(shared / "payload_py.py")], env, shared, "lowcol_python"
    ),
    "pwsh": a.launch("lowil", ps, env, shared, "lowcol_pwsh"),
    "cuda": a.launch(
        "lowil",
        [str(a.PY), str(shared / "cuda_probe.py")],
        env,
        shared,
        "lowcol_cuda",
        mem_bytes=8 << 30,
    ),
}
srv.close()
(a.WORK / "lowil_column.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
)
print("wrote lowil_column.json")

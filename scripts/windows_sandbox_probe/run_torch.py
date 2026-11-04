"""Run torch_probe.py in medium / lowil / appcontainer. usage: python run_torch.py <ROOT>"""

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
for n in ("payload_py.py", "torch_probe.py"):
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), n), shared / n)
torchlib = a.ROOT / "torchlib"
sid, sid_str = a.appcontainer_sid()
a.setup(sid_str)
for folder in (shared, torchlib):
    a._icacls(folder, "/grant", "*S-1-15-2-1:(OI)(CI)RX", "/Q")
homes = {
    "medium": a.DIRS["LOW_ONLY"] / "home",
    "lowil": a.DIRS["LOW_ONLY"] / "home",
    "appcontainer": a.DIRS["RW_ACL"] / "home",
}
out = {}
for mode, home in homes.items():
    for sub in ("Local", "Roaming", "Temp"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        PROBE_TORCHLIB=str(torchlib),
        TEMP=str(home / "Temp"),
        TMP=str(home / "Temp"),
        LOCALAPPDATA=str(home / "Local"),
        APPDATA=str(home / "Roaming"),
        CUDA_CACHE_PATH=str(home / "cuda_cache"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    out[mode] = a.launch(
        mode,
        [str(a.PY), str(shared / "torch_probe.py")],
        env,
        shared,
        f"torch_{mode}",
        sid if mode == "appcontainer" else None,
        timeout=600,
        mem_bytes=16 << 30,
    )
out["profile_deleted"] = hex(a.ue.DeleteAppContainerProfile(a.NAME) & 0xFFFFFFFF)
(a.WORK / "torch_results.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
print("wrote torch_results.json")

"""Which PowerShell file operations work inside the AppContainer (absolute paths under C:\\).
usage: python pwsh_writes.py <ROOT>"""

import json
import os
import shutil
import sys

ROOT = sys.argv[1]
sys.argv = [sys.argv[0], ROOT]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_probe as a  # noqa: E402

sid, sid_str = a.appcontainer_sid()
a.setup(sid_str)
here = os.path.dirname(os.path.abspath(__file__))
shutil.copy(os.path.join(here, "payload_ps_writes.ps1"), a.DIRS["RO"] / "payload_ps_writes.ps1")
for p in a.DIRS["RW_ACL"].iterdir():
    shutil.rmtree(p) if p.is_dir() else p.unlink()
env = dict(os.environ)
env.update({f"PROBE_{k}": str(v) for k, v in a.DIRS.items()})
env.update(TEMP=str(a.DIRS["RW_ACL"]), TMP=str(a.DIRS["RW_ACL"]))
ps = [
    str(a.PWSH),
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    str(a.DIRS["RO"] / "payload_ps_writes.ps1"),
]
out = {
    m: a.launch(m, ps, env, a.DIRS["RO"], f"pswrite_{m}", sid if m == "appcontainer" else None)
    for m in ("medium", "appcontainer")
}
out["profile_deleted"] = hex(a.ue.DeleteAppContainerProfile(a.NAME) & 0xFFFFFFFF)
(a.WORK / "pswrite.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
)
print("wrote pswrite.json")

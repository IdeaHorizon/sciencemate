"""Which explicit ACEs on an interpreter folder break Low-IL (non-AppContainer) launches from it.

Measured 2026-11-03 on Windows 11 26200: only an ACE for a *specific* AppContainer SID does
(0xC0000135 STATUS_DLL_NOT_FOUND); Users / ALL APPLICATION PACKAGES / ALL RESTRICTED APPLICATION
PACKAGES do not. usage: python ace_matrix.py <ROOT>
"""

import json
import os
import subprocess
import sys

ROOT = sys.argv[1]
sys.argv = [sys.argv[0], ROOT]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_probe as a  # noqa: E402

env = dict(os.environ)
argv = [str(a.PY), "-c", "print('py-ok')"]
folder = str(a.ROOT / "python")
sid, sid_str = a.appcontainer_sid()


def icacls(*args):
    return subprocess.run(
        ["icacls", folder, *args, "/Q"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
    ).returncode


rows = []
for label, s in [
    ("Users group", "S-1-5-32-545"),
    ("ALL APPLICATION PACKAGES", "S-1-15-2-1"),
    ("specific AppContainer SID", sid_str),
    ("ALL RESTRICTED APPLICATION PACKAGES", "S-1-15-2-2"),
]:
    icacls("/remove:g", f"*{s}")
    g = icacls("/grant", f"*{s}:(OI)(CI)RX")
    r = a.launch("lowil", argv, env, a.DIRS["RO"], "ace_matrix")
    rm = icacls("/remove:g", f"*{s}")
    rows.append(
        {
            "ace": label,
            "sid": s,
            "grant_rc": g,
            "lowil_rc": r["rc"],
            "stdout": r.get("stdout_tail", ""),
            "remove_rc": rm,
        }
    )
icacls("/grant", "*S-1-15-2-1:(OI)(CI)RX")  # restore what ac_probe.setup() leaves
a.ue.DeleteAppContainerProfile(a.NAME)
(a.WORK / "ace_matrix.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
for row in rows:
    print(f"{row['ace']:38s} lowil_rc={row['lowil_rc']} out={row['stdout']!r}")

"""PowerShell provider operations inside the AppContainer when the run root is mapped to a drive
letter (DefineDosDevice, i.e. subst; no admin). usage: python subst_drive.py <ROOT>"""

import ctypes
import json
import os
import shutil
import sys

ROOT = sys.argv[1]
sys.argv = [sys.argv[0], ROOT]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_probe as a  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.DefineDosDeviceW.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p]
used = k32.GetLogicalDrives()
letter = next(c for c in "QPONMLKJ" if not used & (1 << (ord(c) - 65)))
drive = f"{letter}:"
sid, sid_str = a.appcontainer_sid()
a.setup(sid_str)
shutil.copy(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload_ps_drive.ps1"),
    a.DIRS["RO"] / "payload_ps_drive.ps1",
)
target = a.DIRS["RW_ACL"]
for p in target.iterdir():
    shutil.rmtree(p) if p.is_dir() else p.unlink()
out = {"drive": drive}
if not k32.DefineDosDeviceW(0, drive, str(target)):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    env = dict(os.environ)
    env.update(
        PROBE_DRIVE_ROOT=drive + "\\",
        PROBE_PY=str(a.PY),
        TEMP=drive + "\\",
        TMP=drive + "\\",
        PROBE_ESCAPE=str(a.DIRS["DENIED"] / "escape_attempt.txt"),
    )
    ps = [
        str(a.PWSH),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(a.DIRS["RO"] / "payload_ps_drive.ps1"),
    ]
    for m in ("medium", "appcontainer"):
        out[m] = a.launch(
            m, ps, env, drive + "\\", f"psdrive_{m}", sid if m == "appcontainer" else None
        )
finally:
    k32.DefineDosDeviceW(0x2, drive, str(target))  # DDD_REMOVE_DEFINITION
    out["subst_removed"] = not (k32.GetLogicalDrives() & (1 << (ord(letter) - 65)))
    out["profile_deleted"] = hex(a.ue.DeleteAppContainerProfile(a.NAME) & 0xFFFFFFFF)
    esc = str(a.DIRS["DENIED"] / "escape_attempt.txt")
    out["escape_file_exists"] = os.path.exists(esc)
    if os.path.exists(esc):
        os.remove(esc)
(a.WORK / "psdrive.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
)
print("wrote psdrive.json")

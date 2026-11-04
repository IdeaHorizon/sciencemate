r"""Windows sandbox capability probe for RFC_WINDOWS_EXECUTION_TARGETS_20261030 §6.

Runs as the ordinary (Medium IL, non-admin) user. Launches the same payloads in three
modes and records what each mode allows:

  medium        plain child process (control group: proves each check can succeed)
  lowil         today's win32 backend shape: own token lowered to Low IL
  appcontainer  zero-capability AppContainer (no internetClient, no anything)

Every mode runs inside a Job Object (kill-on-close, 1 GiB per-process memory cap).
Only directories under ROOT (created by this probe) get ACL / label changes. The
AppContainer profile is deleted at the end unless --keep-profile.

usage: python ac_probe.py <ROOT> [--keep-profile]
"""

import ctypes
import ctypes.wintypes as w
import json
import msvcrt
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
KEEP = "--keep-profile" in sys.argv
PY = ROOT / "python" / "python.exe"
PWSH = ROOT / "pwsh" / "pwsh.exe"
WORK = ROOT / "work"
DIRS = {k: WORK / k.lower() for k in ("RO", "RW_ACL", "RW_ACL_LOW", "LOW_ONLY", "DENIED", "OUT")}
NAME = "afs.probe.appcontainer.v1"

V = ctypes.c_void_p
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)
ue = ctypes.WinDLL("userenv", use_last_error=True)


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
        ("dwX", w.DWORD),
        ("dwY", w.DWORD),
        ("dwXSize", w.DWORD),
        ("dwYSize", w.DWORD),
        ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD),
        ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD),
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", V),
        ("hStdInput", V),
        ("hStdOutput", V),
        ("hStdError", V),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", V)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", V), ("hThread", V), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", V),
        ("Capabilities", V),
        ("CapabilityCount", w.DWORD),
        ("Reserved", w.DWORD),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", V), ("Attributes", w.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("R", "W", "O", "RB", "WB", "OB")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Basic", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("Io", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


k32.GetCurrentProcess.restype = V
k32.CreateJobObjectW.restype = V
k32.CreateJobObjectW.argtypes = [V, w.LPCWSTR]
k32.SetInformationJobObject.argtypes = [V, ctypes.c_int, V, w.DWORD]
k32.AssignProcessToJobObject.argtypes = [V, V]
k32.ResumeThread.argtypes = [V]
k32.WaitForSingleObject.argtypes = [V, w.DWORD]
k32.GetExitCodeProcess.argtypes = [V, ctypes.POINTER(w.DWORD)]
k32.TerminateJobObject.argtypes = [V, ctypes.c_uint]
k32.CloseHandle.argtypes = [V]
k32.InitializeProcThreadAttributeList.argtypes = [
    V,
    w.DWORD,
    w.DWORD,
    ctypes.POINTER(ctypes.c_size_t),
]
k32.UpdateProcThreadAttribute.argtypes = [V, w.DWORD, ctypes.c_size_t, V, ctypes.c_size_t, V, V]
k32.DeleteProcThreadAttributeList.argtypes = [V]
k32.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, V, V, w.BOOL, w.DWORD, V, w.LPCWSTR, V, V]
adv.CreateProcessAsUserW.argtypes = [
    V,
    w.LPCWSTR,
    w.LPWSTR,
    V,
    V,
    w.BOOL,
    w.DWORD,
    V,
    w.LPCWSTR,
    V,
    V,
]
adv.OpenProcessToken.argtypes = [V, w.DWORD, ctypes.POINTER(V)]
adv.DuplicateTokenEx.argtypes = [V, w.DWORD, V, ctypes.c_int, ctypes.c_int, ctypes.POINTER(V)]
adv.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(V)]
adv.ConvertSidToStringSidW.argtypes = [V, ctypes.POINTER(w.LPWSTR)]
adv.GetLengthSid.argtypes = [V]
adv.SetTokenInformation.argtypes = [V, ctypes.c_int, V, w.DWORD]
ue.CreateAppContainerProfile.argtypes = [
    w.LPCWSTR,
    w.LPCWSTR,
    w.LPCWSTR,
    V,
    w.DWORD,
    ctypes.POINTER(V),
]
ue.CreateAppContainerProfile.restype = ctypes.c_long
ue.DeriveAppContainerSidFromAppContainerName.argtypes = [w.LPCWSTR, ctypes.POINTER(V)]
ue.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
ue.DeleteAppContainerProfile.argtypes = [w.LPCWSTR]
ue.DeleteAppContainerProfile.restype = ctypes.c_long
ue.GetAppContainerFolderPath.argtypes = [w.LPCWSTR, ctypes.POINTER(w.LPWSTR)]
ue.GetAppContainerFolderPath.restype = ctypes.c_long

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_SUSPENDED = 0x4
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x400
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009


def _icacls(*args):
    r = subprocess.run(
        ["icacls", *map(str, args)],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"icacls {args} -> {r.returncode}: {r.stdout}{r.stderr}")


def appcontainer_sid():
    sid = V()
    hr = (
        ue.CreateAppContainerProfile(
            NAME, "afs probe", "afs AppContainer capability probe", None, 0, ctypes.byref(sid)
        )
        & 0xFFFFFFFF
    )
    if hr == 0x800700B7:  # already exists
        hr = ue.DeriveAppContainerSidFromAppContainerName(NAME, ctypes.byref(sid)) & 0xFFFFFFFF
    if hr != 0:
        raise OSError(f"AppContainer profile: HRESULT 0x{hr:08X}")
    s = w.LPWSTR()
    adv.ConvertSidToStringSidW(sid, ctypes.byref(s))
    return sid, s.value


def low_token():
    tok, dup, low = V(), V(), V()
    if not adv.OpenProcessToken(
        k32.GetCurrentProcess(), 0x0002 | 0x0080 | 0x0008 | 0x0001, ctypes.byref(tok)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not adv.DuplicateTokenEx(tok, 0x02000000, None, 2, 1, ctypes.byref(dup)):
        raise ctypes.WinError(ctypes.get_last_error())
    adv.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(low))
    label = SID_AND_ATTRIBUTES(low, 0x20)
    if not adv.SetTokenInformation(
        dup, 25, ctypes.byref(label), ctypes.sizeof(label) + adv.GetLengthSid(low)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return dup


def make_job(mem_bytes):
    job = k32.CreateJobObjectW(None, None)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.Basic.LimitFlags = 0x2000 | 0x100  # KILL_ON_JOB_CLOSE | PROCESS_MEMORY
    info.ProcessMemoryLimit = mem_bytes
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def launch(mode, argv, env, cwd, tag, ac_sid=None, timeout=180, mem_bytes=1 << 30):
    out_p, err_p = DIRS["OUT"] / f"{tag}.out", DIRS["OUT"] / f"{tag}.err"
    fo, fe, fi = open(out_p, "wb"), open(err_p, "wb"), open(os.devnull, "rb")
    handles = []
    for f in (fi, fo, fe):
        h = msvcrt.get_osfhandle(f.fileno())
        os.set_handle_inheritable(h, True)
        handles.append(h)
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
    si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = handles
    n_attr = 2 if mode == "appcontainer" else 1
    size = ctypes.c_size_t()
    k32.InitializeProcThreadAttributeList(None, n_attr, 0, ctypes.byref(size))
    attr = ctypes.create_string_buffer(size.value)
    if not k32.InitializeProcThreadAttributeList(attr, n_attr, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    harr = (V * 3)(*handles)
    k32.UpdateProcThreadAttribute(
        attr, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, harr, ctypes.sizeof(harr), None, None
    )
    caps = SECURITY_CAPABILITIES(ac_sid, None, 0, 0)
    if mode == "appcontainer":
        if not k32.UpdateProcThreadAttribute(
            attr,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(caps),
            ctypes.sizeof(caps),
            None,
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    si.lpAttributeList = ctypes.cast(attr, V)
    block = (
        "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
    )
    envbuf = ctypes.create_unicode_buffer(block)
    cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    flags = (
        EXTENDED_STARTUPINFO_PRESENT
        | CREATE_SUSPENDED
        | CREATE_NO_WINDOW
        | CREATE_UNICODE_ENVIRONMENT
    )
    pi = PROCESS_INFORMATION()
    job = make_job(mem_bytes)
    t0 = time.perf_counter()
    if mode == "lowil":
        ok = adv.CreateProcessAsUserW(
            low_token(),
            None,
            cmd,
            None,
            None,
            True,
            flags,
            envbuf,
            str(cwd),
            ctypes.byref(si),
            ctypes.byref(pi),
        )
    else:
        ok = k32.CreateProcessW(
            None, cmd, None, None, True, flags, envbuf, str(cwd), ctypes.byref(si), ctypes.byref(pi)
        )
    err = ctypes.get_last_error()
    k32.DeleteProcThreadAttributeList(attr)
    for f in (fi, fo, fe):
        f.close()
    if not ok:
        k32.CloseHandle(job)
        return {"spawn_error": f"WinError {err}: {ctypes.FormatError(err)}"}
    k32.AssignProcessToJobObject(job, pi.hProcess)
    k32.ResumeThread(pi.hThread)
    timed_out = k32.WaitForSingleObject(pi.hProcess, timeout * 1000) != 0
    if timed_out:
        k32.TerminateJobObject(job, 1)
        k32.WaitForSingleObject(pi.hProcess, 5000)
    code = w.DWORD()
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    for h in (pi.hThread, pi.hProcess, job):
        k32.CloseHandle(h)
    stdout = out_p.read_text(encoding="utf-8", errors="replace").strip()
    stderr = err_p.read_text(encoding="utf-8", errors="replace").strip()
    res = {
        "rc": f"0x{code.value:08X}" if code.value > 255 else code.value,
        "seconds": round(time.perf_counter() - t0, 2),
        "timed_out": timed_out,
    }
    try:
        res["result"] = json.loads(stdout.splitlines()[-1])
    except Exception:  # noqa: BLE001
        res["stdout_tail"] = stdout[-600:]
    if stderr:
        res["stderr_tail"] = stderr[-600:]
    return res


def setup(sid_str):
    for d in DIRS.values():
        d.mkdir(parents=True, exist_ok=True)
    (DIRS["RO"] / "input.txt").write_text("probe-input-ok", encoding="utf-8")
    (DIRS["DENIED"] / "secret.txt").write_text(
        "probe-secret-should-not-be-readable", encoding="utf-8"
    )
    here = Path(__file__).resolve().parent
    for name in ("payload_py.py", "payload_ps.ps1", "cuda_probe.py"):
        (DIRS["RO"] / name).write_bytes((here / name).read_bytes())
    ace = f"*{sid_str}"
    # Interpreter folders are granted to ALL APPLICATION PACKAGES (S-1-15-2-1), the way
    # Program Files is. Measured on this machine: an ACE for a *specific* AppContainer SID
    # on a folder makes non-AppContainer Low-IL processes fail to load DLLs from it
    # (0xC0000135); the generic package SID does not.
    for folder in (ROOT / "python", ROOT / "pwsh"):
        if folder.exists():
            _icacls(folder, "/remove:g", ace, "/Q")
            _icacls(folder, "/grant", "*S-1-15-2-1:(OI)(CI)RX", "/Q")
    _icacls(DIRS["RO"], "/grant", f"{ace}:(OI)(CI)RX", "/Q")
    _icacls(DIRS["RW_ACL"], "/grant", f"{ace}:(OI)(CI)M", "/Q")
    _icacls(DIRS["RW_ACL_LOW"], "/grant", f"{ace}:(OI)(CI)M", "/Q")
    _icacls(DIRS["RW_ACL_LOW"], "/setintegritylevel", "(OI)(CI)L", "/Q")
    _icacls(DIRS["LOW_ONLY"], "/setintegritylevel", "(OI)(CI)L", "/Q")


def listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
                c.close()
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()
    return srv


def main():
    sid, sid_str = appcontainer_sid()
    folder = w.LPWSTR()
    ue.GetAppContainerFolderPath(sid_str, ctypes.byref(folder))
    setup(sid_str)
    srv = listener()
    port = srv.getsockname()[1]
    home = DIRS["RW_ACL_LOW"] / "home"
    for sub in ("Local", "Roaming", "Temp"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    base = dict(os.environ)
    base.update({f"PROBE_{k}": str(v) for k, v in DIRS.items()})
    base.update(
        PROBE_PARENT=str(WORK),
        PROBE_DOCS=str(Path(os.environ["USERPROFILE"]) / "Documents"),
        PROBE_LOOPBACK_PORT=str(port),
        PROBE_PY=str(PY),
        TEMP=str(home / "Temp"),
        TMP=str(home / "Temp"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    redirected = dict(base, LOCALAPPDATA=str(home / "Local"), APPDATA=str(home / "Roaming"))
    results = {
        "appcontainer": {"name": NAME, "sid": sid_str, "folder": folder.value},
        "loopback_port": port,
        "runs": {},
    }
    sids = {"appcontainer": sid}
    for mode in ("medium", "lowil", "appcontainer"):
        ac = sids.get(mode)
        runs = results["runs"].setdefault(mode, {})
        runs["python"] = launch(
            mode,
            [str(PY), str(DIRS["RO"] / "payload_py.py")],
            base,
            DIRS["RO"],
            f"{mode}_python",
            ac,
        )
        ps = [
            str(PWSH),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(DIRS["RO"] / "payload_ps.ps1"),
        ]
        if PWSH.exists():
            runs["pwsh"] = launch(mode, ps, base, DIRS["RO"], f"{mode}_pwsh", ac)
            runs["pwsh_home_redirected"] = launch(
                mode, ps, redirected, DIRS["RO"], f"{mode}_pwsh_home", ac
            )
        else:
            runs["pwsh"] = {"skipped": f"{PWSH} not present yet"}
        cuda_env = dict(base, CUDA_CACHE_PATH=str(DIRS["RW_ACL_LOW"] / f"cuda_cache_{mode}"))
        runs["cuda"] = launch(
            mode,
            [str(PY), str(DIRS["RO"] / "cuda_probe.py")],
            cuda_env,
            DIRS["RO"],
            f"{mode}_cuda",
            ac,
            mem_bytes=8 << 30,
        )
    results["runs"]["appcontainer"]["cuda_default_cache"] = launch(
        "appcontainer",
        [str(PY), str(DIRS["RO"] / "cuda_probe.py")],
        base,
        DIRS["RO"],
        "appcontainer_cuda_default",
        sid,
        mem_bytes=8 << 30,
    )
    srv.close()
    if not KEEP:
        results["profile_deleted_hresult"] = (
            f"0x{ue.DeleteAppContainerProfile(NAME) & 0xFFFFFFFF:08X}"
        )
    out = WORK / ("results.json" if PWSH.exists() else "results_no_pwsh.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

"""Sandbox capability probe payload (python). Runs inside whatever launch mode the
harness chose and reports, one JSON line on stdout, what this process can and
cannot do. Read-only except for tiny marker files it writes and deletes."""

import ctypes
import json
import os
import socket
import subprocess
import sys

R = {}


def rec(name, fn):
    try:
        R[name] = {"ok": True, "value": fn()}
    except BaseException as exc:  # noqa: BLE001 - report everything
        R[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def token_facts():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    V = ctypes.c_void_p
    k32.GetCurrentProcess.restype = V
    adv.OpenProcessToken.argtypes = [V, ctypes.c_ulong, ctypes.POINTER(V)]
    adv.GetTokenInformation.argtypes = [
        V,
        ctypes.c_int,
        V,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    adv.ConvertSidToStringSidW.argtypes = [V, ctypes.POINTER(ctypes.c_wchar_p)]
    tok = V()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken")
    is_ac = ctypes.c_ulong(0)
    n = ctypes.c_ulong(0)
    adv.GetTokenInformation(tok, 29, ctypes.byref(is_ac), 4, ctypes.byref(n))  # TokenIsAppContainer
    buf = ctypes.create_string_buffer(256)
    if not adv.GetTokenInformation(tok, 25, buf, 256, ctypes.byref(n)):  # TokenIntegrityLevel
        raise OSError(ctypes.get_last_error(), "GetTokenInformation(IL)")
    sid_ptr = ctypes.cast(buf, ctypes.POINTER(V))[0]
    s = ctypes.c_wchar_p()
    adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(s))
    return {"is_appcontainer": bool(is_ac.value), "integrity_sid": s.value}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()[:60]


def write(directory):
    p = os.path.join(directory, f"marker_py_{os.getpid()}.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("x")
    try:
        os.remove(p)
    except OSError:
        pass
    return "written"


def connect(host, port, timeout=5):
    with socket.create_connection((host, port), timeout=timeout):
        return "connected"


def child():
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "import payload_py as p; print(json.dumps(p.token_facts()))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, os.path.dirname(os.path.abspath(__file__))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "rc": out.returncode,
        "stdout": out.stdout.strip()[:200],
        "stderr": out.stderr.strip()[-200:],
    }


def mem_cap():
    blob = bytearray(1536 * 1024 * 1024)  # job limit is 1 GiB per process
    return f"allocated {len(blob) >> 20} MiB (cap NOT enforced)"


if __name__ == "__main__":
    e = os.environ
    rec("token", token_facts)
    rec("python", lambda: sys.version.split()[0])
    rec("read_ro", lambda: read(os.path.join(e["PROBE_RO"], "input.txt")))
    rec("read_denied", lambda: read(os.path.join(e["PROBE_DENIED"], "secret.txt")))
    rec("list_user_documents", lambda: len(os.listdir(e["PROBE_DOCS"])))
    for key in ("RW_ACL", "RW_ACL_LOW", "LOW_ONLY", "RO", "DENIED", "PARENT"):
        rec(f"write_{key.lower()}", lambda k=key: write(e[f"PROBE_{k}"]))
    rec("net_dns", lambda: socket.getaddrinfo("api.github.com", 443)[0][4][0])
    rec("net_tcp_ip", lambda: connect("223.5.5.5", 53))
    rec("net_tcp_host", lambda: connect("api.github.com", 443))
    rec("net_loopback", lambda: connect("127.0.0.1", int(e["PROBE_LOOPBACK_PORT"]), 3))
    rec("child_process", child)
    rec("mem_cap", mem_cap)
    sys.stdout.write(json.dumps(R, ensure_ascii=False) + "\n")

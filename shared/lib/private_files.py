"""Private application data on POSIX permissions and Windows DACLs.

On Windows chmod only controls the read-only attribute. Use a protected DACL
with the current user and SYSTEM; inheritance protects subsequently created data.
Failures are explicit: an application must not silently claim a secret is private.
"""
from __future__ import annotations

import os
from pathlib import Path

from shared.lib.filesystem import io_path


def make_private(path: Path) -> None:
    path = io_path(path)
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    _set_windows_private_dacl(path)


def write_private_bytes(path: Path, data: bytes, *, overwrite: bool = True) -> None:
    parent = io_path(path.parent)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    make_private(parent)
    flags = os.O_WRONLY | os.O_CREAT | (0 if overwrite else os.O_EXCL)
    # Do not truncate existing secret data before its access policy is established.
    descriptor = os.open(io_path(path), flags, 0o600)
    try:
        make_private(path)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.truncate(0)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _set_windows_private_dacl(path: Path) -> None:
    import ctypes
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.GetTokenInformation.argtypes = [w.HANDLE, w.DWORD, pointer, w.DWORD, ctypes.POINTER(w.DWORD)]
    advapi.GetTokenInformation.restype = w.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(w.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(w.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [pointer, ctypes.POINTER(w.BOOL), ctypes.POINTER(pointer), ctypes.POINTER(w.BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = w.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [w.LPWSTR, w.DWORD, w.DWORD, pointer, pointer, pointer, pointer]
    advapi.SetNamedSecurityInfoW.restype = w.DWORD
    advapi.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, w.DWORD, w.DWORD, pointer, pointer, ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer)]
    advapi.GetNamedSecurityInfoW.restype = w.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [pointer, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)]
    advapi.GetSecurityDescriptorControl.restype = w.BOOL
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer

    token = w.HANDLE()
    sid_text = w.LPWSTR()
    security = pointer()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        needed = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(pointer))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        inherit = "OICI" if path.is_dir() else ""
        sddl = f"D:P(A;{inherit};FA;;;{sid_text.value})(A;{inherit};FA;;;SY)"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(security), None):
            raise ctypes.WinError(ctypes.get_last_error())
        present, defaulted, dacl = w.BOOL(), w.BOOL(), pointer()
        if not advapi.GetSecurityDescriptorDacl(security, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Reapplying a directory DACL propagates inheritance through its tree.
        # Read the actual policy first; a repeated key lookup must not rewrite it.
        existing_dacl, existing_security = pointer(), pointer()
        result = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None,
                                              ctypes.byref(existing_dacl), None,
                                              ctypes.byref(existing_security))
        if result:
            raise ctypes.WinError(result)
        try:
            control, revision = w.WORD(), w.DWORD()
            if not advapi.GetSecurityDescriptorControl(existing_security, ctypes.byref(control), ctypes.byref(revision)):
                raise ctypes.WinError(ctypes.get_last_error())
            def acl_bytes(address):
                if not address:
                    return None
                size = w.WORD.from_address(address.value + 2).value
                return ctypes.string_at(address, size)
            if control.value & 0x1000 and acl_bytes(existing_dacl) == acl_bytes(dacl):
                return
        finally:
            kernel.LocalFree(existing_security)
        result = advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
        if result:
            raise ctypes.WinError(result)
    finally:
        if security:
            kernel.LocalFree(security)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, pointer))
        if token:
            kernel.CloseHandle(token)

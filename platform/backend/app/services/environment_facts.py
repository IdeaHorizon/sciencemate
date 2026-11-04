"""环境事实与修复提案 —— 全部机械，一个字不问模型（RFC_WORKER_START_IS_NOT_A_GATE §3.2，窄单 B）。

## 为什么要有这一层

lujy 10-15 现场：worker 报「读不到 marker」，后端日志里是 `[WinError 5] 拒绝访问`。分得出病因的
三样东西（这条路径归谁、ACL 是什么、是不是我们自己的孤儿进程握着）没有任何一层读过，于是只能
靠人手敲 `icacls` / `dir /q` / `whoami`，而且猜了三天。看不见就修不了 —— 这是第一道门槛，跟自主性无关。

## 分工（§G4）

- **事实由探针读**：所有者、权限（POSIX mode / Windows DACL 原文）、reparse、持有者（只在**自己派出**
  的 worker 里找，不扫全机）、本进程的令牌（用户、Windows 完整性级别）。读不出来写 `null` 带原因，
  **不省略字段**（两种词表两个问题：缺席≠正常）。
- **修复提案查表**：`code → (kind, argv, 改什么, 可撤销否, 要不要人点头)`。表里没有的 code 只有事实
  没有提案；提案永远是服务端按 code + 路径**重算**的，前端只回传 code 与路径，不回传命令。
- 执行不在这里（`session_environment.run_repair`）：以用户自己的身份、不进沙箱、经 HITL。
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 这些 code 的「读不到 / 写不进」在权限层面有同一种修法：把数据根的权限恢复成继承。
_PERMISSION_SHAPED_CODES = frozenset({
    "home_marker_unreadable", "state_marker_unreadable", "state_dir_unavailable",
    "lock_unavailable", "activity_file_unavailable", "durable_sink_unavailable",
    "state_unreadable", "conversation_unreadable", "transcript_unwritable",
})

#: 这些 code 是真冲突：没有能代执行的修复，只有给管理员的话。
_HARD_CODES = frozenset({"tenant_home_conflict", "runtime_identity_conflict"})

_PERMISSION_ERROR_MARKERS = ("PermissionError", "WinError 5", "Errno 13", "EACCES", "拒绝访问", "Permission denied")


def _looks_like_permission_denial(error: str | None) -> bool:
    text = error or ""
    return any(marker in text for marker in _PERMISSION_ERROR_MARKERS)


# ── 事实 ─────────────────────────────────────────────────────────────────────


def probe_path(path: Path | str | None) -> dict[str, Any]:
    """一条路径的事实。每个字段都在：读不出来是 `None` + `error`，不是没有这个键。"""
    facts: dict[str, Any] = {
        "path": str(path) if path is not None else None,
        "exists": None, "kind": None, "owner": None, "mode": None, "acl": None,
        "reparse_tag": None, "error": None,
    }
    if path is None:
        facts["error"] = "no path"
        return facts
    target = Path(path)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        facts["exists"] = False
        facts["kind"] = "missing"
        return facts
    except OSError as exc:
        # stat 都拒绝访问也是事实（lujy 现场的 run 目录就是这样）。
        facts["error"] = f"{type(exc).__name__}: {exc}"
        return facts
    facts["exists"] = True
    if stat.S_ISLNK(st.st_mode):
        facts["kind"] = "symlink"
    elif stat.S_ISDIR(st.st_mode):
        facts["kind"] = "dir"
    elif stat.S_ISREG(st.st_mode):
        facts["kind"] = "file"
    else:
        facts["kind"] = "other"
    if sys.platform == "win32":
        tag = getattr(st, "st_reparse_tag", 0)
        facts["reparse_tag"] = hex(tag) if tag else None
        facts["owner"] = _windows_owner(target)
        facts["acl"] = _windows_acl_text(target)
    else:
        facts["mode"] = oct(stat.S_IMODE(st.st_mode))
        facts["owner"] = _posix_owner(st)
        facts["acl"] = _posix_acl_text(target)
    return facts


def _posix_owner(st: os.stat_result) -> str:
    try:
        import pwd

        name = pwd.getpwuid(st.st_uid).pw_name
    except (ImportError, KeyError):
        name = None
    return f"{name or '?'} (uid {st.st_uid})"


def _posix_acl_text(target: Path) -> str | None:
    """`getfacl` 有就读，没有就 None（mode 已经在了）。"""
    try:
        done = subprocess.run(["getfacl", "-p", str(target)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or "").strip()[:4000] or None


def _windows_owner(target: Path) -> str | None:  # pragma: no cover - 只在 Windows 上跑
    try:
        import ctypes
        from ctypes import wintypes as w

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.c_void_p
        advapi.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, w.DWORD, w.DWORD, ctypes.POINTER(pointer), pointer,
                                                 pointer, pointer, ctypes.POINTER(pointer)]
        advapi.GetNamedSecurityInfoW.restype = w.DWORD
        advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(w.LPWSTR)]
        advapi.ConvertSidToStringSidW.restype = w.BOOL
        advapi.LookupAccountSidW.argtypes = [w.LPCWSTR, pointer, w.LPWSTR, ctypes.POINTER(w.DWORD), w.LPWSTR,
                                             ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD)]
        advapi.LookupAccountSidW.restype = w.BOOL
        kernel.LocalFree.argtypes = [pointer]
        owner, descriptor = pointer(), pointer()
        OWNER_SECURITY_INFORMATION = 0x1
        rc = advapi.GetNamedSecurityInfoW(str(target), 1, OWNER_SECURITY_INFORMATION, ctypes.byref(owner),
                                          None, None, None, ctypes.byref(descriptor))
        if rc:
            return f"<GetNamedSecurityInfo failed: {rc}>"
        try:
            sid_text = w.LPWSTR()
            text = "?"
            if advapi.ConvertSidToStringSidW(owner, ctypes.byref(sid_text)):
                text = sid_text.value or "?"
                kernel.LocalFree(ctypes.cast(sid_text, pointer))
            name = ctypes.create_unicode_buffer(256)
            domain = ctypes.create_unicode_buffer(256)
            name_len, domain_len, use = w.DWORD(256), w.DWORD(256), w.DWORD()
            if advapi.LookupAccountSidW(None, owner, name, ctypes.byref(name_len), domain,
                                        ctypes.byref(domain_len), ctypes.byref(use)):
                return f"{domain.value}\\{name.value} ({text})"
            return text
        finally:
            kernel.LocalFree(descriptor)
    except Exception as exc:  # noqa: BLE001 — 探针自己坏了也要留下原因
        return f"<owner probe failed: {type(exc).__name__}: {exc}>"


def _windows_acl_text(target: Path) -> str | None:  # pragma: no cover - 只在 Windows 上跑
    """`icacls <path>` 的原文：人读得懂、能直接贴进 issue 的那一种。"""
    try:
        done = subprocess.run(["icacls", str(target)], capture_output=True, text=True, timeout=15,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<icacls failed: {type(exc).__name__}: {exc}>"
    return ((done.stdout or "") + (done.stderr or "")).strip()[:4000] or None


def probe_token() -> dict[str, Any]:
    """本进程是谁、在什么完整性级别上跑。"""
    token: dict[str, Any] = {"user": None, "uid": None, "integrity": None, "elevated": None, "error": None}
    try:
        token["user"] = getpass.getuser()
    except Exception as exc:  # noqa: BLE001
        token["error"] = f"{type(exc).__name__}: {exc}"
    if hasattr(os, "getuid"):
        token["uid"] = os.getuid()
    if sys.platform == "win32":  # pragma: no cover
        token["integrity"], token["elevated"] = _windows_token_level()
    return token


def _windows_token_level() -> tuple[str | None, bool | None]:  # pragma: no cover - 只在 Windows 上跑
    """(完整性级别, 是否提权)。提权是本应用目录权限出问题最常见的前因（以管理员身份跑过一次，
    之后 owner 是 Administrators 的东西正常运行读不了），所以和完整性级别一起报。

    每个 Win32 函数都声明 argtypes/restype：没声明时 64 位指针按 32 位 int 传，SID 地址被截断，
    这里曾经一直静默返回 None（lujy 10-29 诊断包里 ``integrity: null``）。
    """
    try:
        import ctypes
        from ctypes import wintypes as w

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        advapi.OpenProcessToken.restype = w.BOOL
        advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                               ctypes.POINTER(w.DWORD)]
        advapi.GetTokenInformation.restype = w.BOOL
        advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, w.DWORD]
        advapi.GetSidSubAuthority.restype = ctypes.POINTER(w.DWORD)
        token = w.HANDLE()
        TOKEN_QUERY, TokenElevation, TokenIntegrityLevel = 0x0008, 20, 25
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            return None, None
        try:
            level = None
            needed = w.DWORD()
            advapi.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            if advapi.GetTokenInformation(token, TokenIntegrityLevel, buffer, needed, ctypes.byref(needed)):
                sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
                count = advapi.GetSidSubAuthorityCount(sid)[0]
                rid = advapi.GetSidSubAuthority(sid, count - 1)[0]
                level = {0x1000: "Low", 0x2000: "Medium", 0x2100: "MediumPlus", 0x3000: "High",
                         0x4000: "System"}.get(rid, hex(rid))
            elevated = None
            flag = w.DWORD()
            if advapi.GetTokenInformation(token, TokenElevation, ctypes.byref(flag), ctypes.sizeof(flag),
                                          ctypes.byref(needed)):
                elevated = bool(flag.value)
            return level, elevated
        finally:
            kernel.CloseHandle(token)
    except Exception:  # noqa: BLE001
        return None, None


def own_holders(project_id: str, session_id: str) -> list[dict[str, Any]]:
    """还握着这个会话运行目录的**我们自己派出的**进程。只认锁文件 / 活动文件里记的 pid 且
    命令行真是 platform_runtime --serve 的；别的进程不扫、不认、不杀（§G5）。"""
    from app.services import harness_sessions as hs

    holders: list[dict[str, Any]] = []
    for reader in (hs._session_lock_path, hs._session_activity_path):
        try:
            path = reader(project_id, session_id)
            if path is None or not path.is_file():
                continue
            record = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            holders.append({"pid": None, "spawn_token": None, "alive": None,
                            "error": f"{type(exc).__name__}: {exc}", "record": reader.__name__})
            continue
        pid = record.get("pid") if isinstance(record, dict) else None
        if not isinstance(pid, int) or pid <= 1:
            continue
        alive = False
        try:
            alive = hs._process_is_a_runtime_worker(pid)
        except Exception:  # noqa: BLE001
            alive = None
        try:
            from app.services.spawn_ledger import recognise

            ours, why = recognise(project_id, session_id, record)
        except Exception as exc:  # noqa: BLE001 —— 认不了亲也要把事实给出去
            ours, why = "unknown", f"{type(exc).__name__}: {exc}"
        holders.append({
            "pid": pid, "spawn_token": str(record.get("spawn_token") or "")[:8] or None,
            "alive": alive, "record": reader.__name__, "error": None,
            "ours": ours, "ours_reason": why,
        })
    return holders


# ── 修复提案 ──────────────────────────────────────────────────────────────────


def repair_proposal(code: str, *, error: str | None, data_root: Path | str) -> dict[str, Any] | None:
    """按 code 与错误形状查表。表里没有 → None（只有事实，没有提案）。

    `changes` 两种语言都写：提案跟着事件存下来，之后谁用哪种语言看，写的这一刻不知道 ——
    前端按那一屏挑（`environment.ts`；老事件里是一句字符串，照原样给）。
    """
    if code in _HARD_CODES:
        return {
            "kind": "needs_administrator", "argv": None, "command": None,
            "changes": {
                "zh": "这是部署问题，需要管理员处理，详细信息在后端日志中。",
                "en": "This is a deployment problem that an administrator needs to handle; the details are "
                      "in the backend log.",
            },
            "reversible": None, "needs_consent": True, "automatic": False,
        }
    if code in _PERMISSION_SHAPED_CODES and _looks_like_permission_denial(error):
        root = str(data_root)
        if sys.platform == "win32":
            argv = ["icacls", root, "/reset", "/T", "/C"]
            # 以管理员身份跑：读不了的那些文件多半是本应用**以管理员身份运行时**建的，属主是
            # Administrators，DACL 只认属主（Python 的 mkdir(0o700)，见 shared.lib.private_files.
            # mkdir_private）—— 用户本人未提权连改权限的资格都没有，照原样跑只会「处理 0 个文件」
            # （lujy 10-29 诊断包里的 repair_attempt 就是这一句）。
            changes = {"zh": "将本应用数据目录中所有文件的权限恢复为继承上级目录，不影响其他文件。"
                             "会弹出一次 Windows 管理员确认：读不了的文件是以管理员身份运行本应用时建的，"
                             "只有管理员能改回来。",
                       "en": "Resets permissions on everything in this app's data folder to inherit from its "
                             "parent folder. Nothing else is affected. Windows will ask for administrator "
                             "approval once: the unreadable files were created while the app ran as "
                             "administrator, and only an administrator can change them back."}
        else:
            argv = ["chmod", "-R", "u+rwX", root]
            changes = {"zh": "为当前用户恢复本应用数据目录中所有文件的读写权限。不影响其他文件。",
                       "en": "Restores your read and write access to everything in this app's data folder. "
                             "Nothing else is affected."}
        return {
            "kind": "reset_data_root_permissions", "argv": argv, "command": _shell_text(argv),
            "changes": changes, "reversible": False, "needs_consent": True, "automatic": False,
            "elevated": sys.platform == "win32",
        }
    return None


def _shell_text(argv: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


# ── 把 worker 的 warning 补成完整的事件 ───────────────────────────────────────


def enrich_warning(warning: dict[str, Any], *, project_id: str, session_id: str,
                   data_root: Path | str) -> dict[str, Any]:
    """worker 只说 code / 路径 / 错误原文；这里补上事实、持有者、令牌、提案。幂等：已经补过的不再探。"""
    if warning.get("facts") is not None:
        return warning
    enriched = dict(warning)
    enriched["facts"] = probe_path(warning.get("path"))
    enriched["holders"] = own_holders(project_id, session_id)
    enriched["token"] = probe_token()
    enriched["repair"] = repair_proposal(str(warning.get("code") or ""), error=warning.get("error"),
                                         data_root=data_root)
    enriched["data_root"] = str(data_root)
    return enriched


def enrich_report(report: dict[str, Any] | None, *, project_id: str, session_id: str,
                  data_root: Path | str) -> dict[str, Any] | None:
    if not report:
        return report
    warnings = report.get("warnings")
    if not isinstance(warnings, list):
        return report
    out = dict(report)
    out["warnings"] = [
        enrich_warning(w, project_id=project_id, session_id=session_id, data_root=data_root)
        if isinstance(w, dict) else w
        for w in warnings
    ]
    return out


def hard_failure_warning(exc: BaseException, *, project_id: str, session_id: str,
                         data_root: Path | str) -> dict[str, Any] | None:
    """三种硬失败里带路径的那两种：把它们也做成同一形状的事件（severity=fatal），事实照探。"""
    code = getattr(exc, "code", None)
    if code not in _HARD_CODES:
        return None
    details = getattr(exc, "details", None)
    path = details.get("path") if isinstance(details, dict) else None
    warning = {
        "type": "environment_warning", "code": code, "capability": "identity", "severity": "fatal",
        "path": str(path) if path else None, "error": str(exc)[:2000],
        "consequences": [{"zh": "这次执行没有开始", "en": "This run did not start"}],
        "details": {k: v for k, v in (details or {}).items() if k != "path"} if isinstance(details, dict) else None,
    }
    return enrich_warning(warning, project_id=project_id, session_id=session_id, data_root=data_root)

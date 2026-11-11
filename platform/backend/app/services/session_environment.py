"""会话的环境状态：活 worker 报的、上一条 run 记下的、连续失败时自动跑的 doctor，以及经人点头的修复
（RFC_WORKER_START_IS_NOT_A_GATE §3.3 / §3.4 / §3.6，窄单 B / C）。

三个读者共用这一份：界面上的条幅（`api/v1/session_environment`）、run 摘要（`local_execution` 在收尾时
把它写进 `run.summary["environment"]`，后端重启后条幅还能读到）、诊断包（runs 目录里就是 run 摘要）。

**重复即升级**（§3.6）：同一会话同一 code **连续**第二次，不再只是「再发一条」—— 后端自动对失败里的路径
跑一遍探针，结果作为条幅摆出来。「连续」按最后一次成功之后算：中间夹一次成功就从头数。
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import the_data_root
from app.models.execution import Run
from app.services import environment_facts as facts
from app.services.say import SaidToAPerson

logger = logging.getLogger(__name__)


def live_report(project_id: str, session_id: str) -> dict[str, Any] | None:
    """活着的 worker 起来时报的能力位与降级事实（已补事实）。没有活 worker → None。"""
    from app.services.harness_sessions import harness_session_manager

    return harness_session_manager.environment_report(project_id, session_id)


def _warnings_of(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not report:
        return []
    warnings = report.get("warnings")
    return [w for w in warnings if isinstance(w, dict)] if isinstance(warnings, list) else []


async def _previous_run(db: AsyncSession, *, session_id: str, run_id: str) -> Run | None:
    rows = (await db.execute(
        select(Run).where(Run.session_id == session_id, Run.id != run_id)
        .order_by(Run.created_at.desc()).limit(1)
    )).scalars().all()
    return rows[0] if rows else None


async def environment_for_run(
    db: AsyncSession, *, project_id: str, session_id: str, run_id: str,
    failure: dict[str, Any] | None = None, exc: BaseException | None = None,
) -> dict[str, Any] | None:
    """这条 run 收尾时写进 summary 的那一节。成功的 run 也写：降级是会话级的事实，不只属于失败的轮。"""
    data_root = the_data_root()
    warnings = list(_warnings_of(live_report(project_id, session_id)))
    source = "live" if warnings else None
    if exc is not None:
        hard = facts.hard_failure_warning(exc, project_id=project_id, session_id=session_id, data_root=data_root)
        if hard is not None:
            warnings.append(hard)
            source = "hard_failure"

    repeated = 0
    escalated = False
    doctor: dict[str, Any] | None = None
    code = str((failure or {}).get("code") or "") if failure else ""
    if code:
        repeated = 1
        previous = await _previous_run(db, session_id=session_id, run_id=run_id)
        previous_summary = previous.summary if previous is not None and isinstance(previous.summary, dict) else {}
        previous_failure = previous_summary.get("failure") if isinstance(previous_summary.get("failure"), dict) else None
        if previous_failure and str(previous_failure.get("code") or "") == code:
            previous_env = previous_summary.get("environment") if isinstance(previous_summary.get("environment"), dict) else {}
            repeated = int(previous_env.get("repeated") or 1) + 1
        if repeated >= 2:
            # 第二次就不再把人当重试循环：自动跑 doctor，把事实摆出来。
            escalated = True
            doctor = await asyncio.to_thread(_doctor, project_id, session_id, data_root, warnings)
    if not warnings and not escalated:
        return None
    return {
        "warnings": warnings, "source": source, "repeated": repeated,
        "escalated": escalated, "doctor": doctor, "data_root": str(data_root),
    }


def _doctor(project_id: str, session_id: str, data_root: Path, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    """对失败里的路径跑一遍探针；没有路径就探数据根。全部机械。"""
    paths = [w.get("path") for w in warnings if w.get("path")]
    return {
        "paths": [facts.probe_path(p) for p in paths] or [facts.probe_path(data_root)],
        "data_root": facts.probe_path(data_root),
        "token": facts.probe_token(),
        "holders": facts.own_holders(project_id, session_id),
    }


async def current_environment(db: AsyncSession, *, project_id: str, session_id: str) -> dict[str, Any]:
    """界面读的那一份：活 worker 的报告优先；没有活 worker 就读这个会话最近一条 run 记下的。"""
    from app.services.permissions_snapshot import latest_change

    machine = [latest_change()] if latest_change() else []
    report = live_report(project_id, session_id)
    if report is not None:
        return {
            "warnings": machine + _warnings_of(report), "capabilities": report.get("capabilities"),
            "source": "live", "repeated": 0, "escalated": False, "doctor": None,
            "runId": None, "data_root": str(the_data_root()),
        }
    rows = (await db.execute(
        select(Run).where(Run.session_id == session_id).order_by(Run.created_at.desc()).limit(1)
    )).scalars().all()
    latest = rows[0] if rows else None
    summary = latest.summary if latest is not None and isinstance(latest.summary, dict) else {}
    env = summary.get("environment") if isinstance(summary.get("environment"), dict) else None
    if env is None:
        return {"warnings": machine, "capabilities": None, "source": "machine" if machine else None, "repeated": 0,
                "escalated": False, "doctor": None, "runId": None, "data_root": str(the_data_root())}
    # 读的是上一条 run 记下的：source 说 last_run（记录里原来的 source 另存），界面据此知道这是「上次」不是「现在」——
    # 修好之后要等下一个 worker 起来（下一轮）ready 全绿，条幅才收起。
    return {**env, "warnings": machine + _warnings_of(env), "capabilities": None,
            "source": "last_run", "recorded_source": env.get("source"),
            "runId": latest.id if latest is not None else None}


class RepairRefused(SaidToAPerson, ValueError):
    """没有这样的提案、或提案不能代执行。环境条幅上原话显示（409），两种语言都写。"""


async def run_repair(db: AsyncSession, *, project_id: str, session_id: str, code: str,
                     path: str | None) -> dict[str, Any]:
    """经人点头之后，以后端（= 用户自己）的身份执行**服务端重算**的提案，回填输出，再探一次。

    前端只回传 code 与路径，命令永远是这里按表算出来的 —— 界面上显示的命令和真正执行的命令出自
    同一个函数。无人值守时没有人点这个接口，所以不执行就是自然的（§3.4）。
    """
    current = await current_environment(db, project_id=project_id, session_id=session_id)
    matching = [w for w in current["warnings"]
                if str(w.get("code")) == code and (path is None or str(w.get("path")) == str(path))]
    if not matching:
        raise RepairRefused("这个问题已不存在，请刷新页面。",
                            en="This problem no longer applies to this Session. Refresh the page.")
    warning = matching[0]
    proposal = facts.repair_proposal(code, error=warning.get("error"), data_root=the_data_root(),
                                    path=warning.get("path"))
    if proposal is None or not proposal.get("argv"):
        # 唯一不带 argv 的提案是 needs_administrator（`environment_facts.repair_proposal`）。
        raise RepairRefused("这个问题无法自动修复，需要管理员处理。",
                            en="This problem cannot be fixed automatically; an administrator needs to handle it.")
    argv = list(proposal["argv"])
    logger.warning("running environment repair for %s/%s: %s", project_id, session_id, argv)
    runner = _run_argv_elevated if proposal.get("elevated") else _run_argv
    result = await asyncio.to_thread(runner, argv)
    after = facts.probe_path(warning.get("path"))
    outcome = {
        "code": code, "path": warning.get("path"), "command": proposal["command"],
        "returncode": result["returncode"], "stdout": result["stdout"], "stderr": result["stderr"],
        "status": result.get("status") or (
            "executed" if result["returncode"] is not None else "unknown"),
        "facts_after": after,
    }
    # 活 worker 的报告上记一笔：下一次 ready 之前，条幅能说「已执行，结果如下」。
    report = live_report(project_id, session_id)
    for w in _warnings_of(report):
        if str(w.get("code")) == code and str(w.get("path")) == str(warning.get("path")):
            w["repair_result"] = outcome
    return outcome


def _run_argv_elevated(argv: list[str], *, verb: str = "runas", timeout_s: int = 300) -> dict[str, Any]:
    """Windows：经 UAC 以管理员身份跑（人在这一刻已经点了「修复」，UAC 是 Windows 再问一次）。

    提权的子进程拿不到管道，所以包一层 ``cmd /c … > log 2>&1``，等它退出、读回日志与退出码。人在
    UAC 上点了「否」→ ``ERROR_CANCELLED``，如实说是拒了，不是失败。``verb`` 只给测试换成 ``open``
    （同一条路，不弹 UAC）。
    """
    import ctypes
    import tempfile
    from ctypes import wintypes as w

    from app.services.harness_imports import ensure_harness_importable

    fd, log_name = tempfile.mkstemp(prefix="sciencemate-repair-", suffix=".log")
    os.close(fd)
    log = Path(log_name)
    command = subprocess.list2cmdline(argv)
    parameters = f'/d /s /c "{command} > "{log}" 2>&1"'

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("fMask", w.ULONG), ("hwnd", w.HWND), ("lpVerb", w.LPCWSTR),
                    ("lpFile", w.LPCWSTR), ("lpParameters", w.LPCWSTR), ("lpDirectory", w.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", w.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", w.LPCWSTR), ("hkeyClass", w.HKEY), ("dwHotKey", w.DWORD),
                    ("hIconOrMonitor", w.HANDLE), ("hProcess", w.HANDLE)]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = w.BOOL
    kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel32.WaitForSingleObject.restype = w.DWORD
    kernel32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel32.GetExitCodeProcess.restype = w.BOOL
    kernel32.CloseHandle.argtypes = [w.HANDLE]
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = 0x00000040 | 0x00000400          # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    info.lpVerb = verb
    info.lpFile = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    info.lpParameters = parameters
    info.nShow = 0                                  # SW_HIDE
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            code = ctypes.get_last_error()
            if code == 1223:                        # ERROR_CANCELLED：人在 UAC 上点了「否」
                return {"status": "cancelled", "returncode": None, "stdout": "",
                        "stderr": "administrator approval was declined; nothing was changed"}
            return {"status": "not_started", "returncode": None, "stdout": "",
                    "stderr": f"ShellExecuteEx failed: WinError {code}"}
        try:
            if kernel32.WaitForSingleObject(info.hProcess, timeout_s * 1000) != 0:
                return {"status": "unknown", "returncode": None, "stdout": "",
                        "stderr": f"timeout after {timeout_s}s"}
            exit_code = w.DWORD()
            kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code))
        finally:
            kernel32.CloseHandle(info.hProcess)
        ensure_harness_importable()
        from shared.lib.native_output import to_utf8

        text = to_utf8(log.read_bytes()).decode("utf-8", errors="replace")
        return {"returncode": int(exit_code.value), "stdout": text[:4000], "stderr": ""}
    finally:
        log.unlink(missing_ok=True)


def _run_argv(argv: list[str]) -> dict[str, Any]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        return {"returncode": None, "stdout": (exc.stdout or "")[:4000] if isinstance(exc.stdout, str) else "",
                "stderr": f"timeout after {exc.timeout}s"}
    except OSError as exc:
        return {"returncode": None, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}
    return {"returncode": done.returncode, "stdout": (done.stdout or "")[:4000], "stderr": (done.stderr or "")[:4000]}

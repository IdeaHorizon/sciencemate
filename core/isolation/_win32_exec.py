#!/usr/bin/env python3
r"""Windows 写边界启动器：用**完整性级别（Low IL）**把命令关进只许写这几个根的墙，
再用 **Job Object** 给它套上资源上限与整组可杀，然后起命令、等它结束。

独立脚本，不 import core（它是隔离层，越少依赖越好）。用法：

    python -I _win32_exec.py probe                          # 自检，打印 JSON 能力集
    python -I _win32_exec.py run '<spec json>' -- cmd …     # 施加隔离后起 cmd，等它退出

机制（RFC #848 §11 真机验证过）：
  * **write_boundary** = 把载荷令牌降到 Low IL（降自己令牌不需要管理员），可写根打 Low
    强制标签（`icacls /setintegritylevel (OI)(CI)L`）让 Low-IL 可写；其余 Medium 对象
    Low-IL 写不了。**不是**合成受限 SID（那要真实本地主体 = 管理员）。
  * **git_unwritable** = 可写根下的 `.git` 打 **Medium** 标签（不是 High —— 把标签升到
    比自己令牌还高需要 SeRelabelPrivilege=管理员；Medium 已经 ≥ Low，Low-IL 写不进，
    而 Medium 进程给对象打 Medium 标签不需要特权）。
  * **mem_cap / pids_cap** = Job Object 的 JOB_MEMORY / PROCESS_MEMORY / ACTIVE_PROCESS。
  * **group_kill** = Job 的 KILL_ON_JOB_CLOSE：本启动器持着 job 句柄，被咽喉杀掉时句柄
    关闭 → 整棵进程树随之消亡。

与 `_landlock_exec.py` 的不同：Landlock 是 restrict_self 后 exec；Windows 不能给自己
降 IL 再 exec（要用**新令牌**起进程），所以本启动器不 exec，而是 `CreateProcessAsUser`
起载荷、继承本进程的 stdout/stderr（咽喉已把它们接到落盘文件）、等它退出并透传退出码。

⚠️ 64 位下每个 HANDLE 参数/返回都要显式 `argtypes=c_void_p`，否则被截成 32 位 int
（报 ERROR_INVALID_HANDLE）—— 这是 §11 真机踩到并修好的坑。
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import tempfile
from ctypes import wintypes

V = ctypes.c_void_p

# ── 常量 ──────────────────────────────────────────────────────────────────────
TOKEN_ALL = 0xF01FF
SecurityImpersonation = 2
TokenPrimary = 1
TokenIntegrityLevel = 25
SE_GROUP_INTEGRITY = 0x20
LOW_INTEGRITY_SID = "S-1-16-4096"
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12
HANDLE_FLAG_INHERIT = 0x00000001
INFINITE = 0xFFFFFFFF
JOB_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_LIMIT_JOB_MEMORY = 0x00000200
JOB_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
ERROR_NOT_ENOUGH_QUOTA = 1816


# ── ctypes 结构体 ─────────────────────────────────────────────────────────────
class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", V), ("Attributes", wintypes.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", V), ("hStdInput", V), ("hStdOutput", V), ("hStdError", V),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", V), ("hThread", V), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in "abcdef"]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _win32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    k.GetCurrentProcess.restype = V
    k.GetStdHandle.restype = V
    k.GetStdHandle.argtypes = [wintypes.DWORD]
    k.SetHandleInformation.argtypes = [V, wintypes.DWORD, wintypes.DWORD]
    k.CreateJobObjectW.restype = V
    k.CreateJobObjectW.argtypes = [V, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [V, ctypes.c_int, V, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [V, V]
    k.ResumeThread.argtypes = [V]
    k.WaitForSingleObject.argtypes = [V, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.GetExitCodeProcess.argtypes = [V, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [V]
    a.OpenProcessToken.argtypes = [V, wintypes.DWORD, ctypes.POINTER(V)]
    a.DuplicateTokenEx.argtypes = [V, wintypes.DWORD, V, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(V)]
    a.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(V)]
    a.SetTokenInformation.argtypes = [V, ctypes.c_int, V, wintypes.DWORD]
    a.CreateProcessAsUserW.argtypes = [
        V, wintypes.LPCWSTR, wintypes.LPWSTR, V, V, wintypes.BOOL, wintypes.DWORD,
        V, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    return k, a


def _low_il_primary_token(a) -> V:
    """复制自有令牌 → 降到 Low IL。降自己不需要管理员（§11 验证）。"""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = V
    tok = V()
    if not a.OpenProcessToken(k.GetCurrentProcess(), TOKEN_ALL, ctypes.byref(tok)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken")
    dup = V()
    if not a.DuplicateTokenEx(tok, TOKEN_ALL, None, SecurityImpersonation, TokenPrimary, ctypes.byref(dup)):
        raise OSError(ctypes.get_last_error(), "DuplicateTokenEx")
    sid = V()
    a.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid))
    label = TOKEN_MANDATORY_LABEL()
    label.Label.Sid = sid
    label.Label.Attributes = SE_GROUP_INTEGRITY
    if not a.SetTokenInformation(dup, TokenIntegrityLevel, ctypes.byref(label), ctypes.sizeof(label)):
        raise OSError(ctypes.get_last_error(), "SetTokenInformation(Low)")
    return dup


def _make_job(k, *, memory_bytes: int, pids: int) -> V:
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObject")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = JOB_LIMIT_KILL_ON_JOB_CLOSE
    if memory_bytes > 0:
        flags |= JOB_LIMIT_JOB_MEMORY | JOB_LIMIT_PROCESS_MEMORY
        info.JobMemoryLimit = memory_bytes
        info.ProcessMemoryLimit = memory_bytes
    if pids > 0:
        flags |= JOB_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = pids
    info.BasicLimitInformation.LimitFlags = flags
    if not k.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                     ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    return job


def _icacls(path: str, level: str) -> None:
    """给 path 打强制完整性标签。level 'L'=Low(放行 Low-IL 写)、'M'=Medium(拒 Low-IL 写)。
    只降/平级：给对象打不高于自己令牌的标签不需要管理员（打 High 才要 SeRelabelPrivilege）。
    对自己拥有的对象失败不抛（探针会把真结果反映出来）。"""
    subprocess.run(["icacls", path, "/setintegritylevel", f"(OI)(CI){level}"],
                   capture_output=True, timeout=15, check=False)


def _spawn_low_and_wait(k, a, argv: list[str], job: V, *, timeout_ms: int = INFINITE,
                        inherit: bool = True, use_std_handles: bool = False) -> int:
    """用 Low IL 令牌起 argv、放进 job、等它退出，返回退出码。

    `use_std_handles`（真实载荷路径 `run()` 用）：设 STARTF_USESTDHANDLES，把**本启动器
    自己的** stdin/stdout/stderr 透传给载荷。咽喉起本启动器时已把 stdout/stderr 接到落盘
    文件、stdin 接到 NUL —— 都是**文件/设备句柄**（不是控制台），Low-IL 继承它们写得进去
    （Chrome 的 Low-IL 渲染器写 broker 给的管道同理）。不透传就等于 `CREATE_NO_WINDOW` +
    无有效 stdout → 载荷的 print 全丢（真机 3 条测试 stdout 为空就栽在这）。先把三个句柄
    标成可继承，免得继承链上某一环没带上 HANDLE_FLAG_INHERIT。

    `use_std_handles=False`（探针用）：**不设 STARTF_USESTDHANDLES**。探针在终端里直接跑，
    GetStdHandle 拿到的是**控制台句柄**，Low-IL 去连 Medium 控制台会挂死（§11 踩过）；探针
    的命令又只往文件里重定向、不靠 stdout，所以不透传最稳。`CREATE_NO_WINDOW` 避免连默认桌面。
    """
    token = _low_il_primary_token(a)
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    if use_std_handles:
        std = [k.GetStdHandle(h) for h in (STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE)]
        for h in std:
            if h:
                k.SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdInput, si.hStdOutput, si.hStdError = std
    pi = PROCESS_INFORMATION()
    # lpApplicationName=NULL：CreateProcess 从命令行取 argv[0]。list2cmdline 会把带空格的
    # 路径（如 `C:\Program Files\Git\usr\bin\bash.exe`）加引号，所以第一 token 无歧义；
    # 裸 `cmd.exe` 走正常搜索（System32）。传全路径当 app 反而要求 argv[0] 必须是绝对路径。
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    ok = a.CreateProcessAsUserW(
        token, None, cmdline, None, None, bool(inherit),
        CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, None, None,
        ctypes.byref(si), ctypes.byref(pi),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), f"CreateProcessAsUser({argv[0]})")
    k.AssignProcessToJobObject(job, pi.hProcess)  # 起时挂起 → 先进 job 再放行，无逃逸窗口
    k.ResumeThread(pi.hThread)
    k.WaitForSingleObject(pi.hProcess, timeout_ms)
    code = wintypes.DWORD()
    k.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    return int(code.value)


def run(spec: dict, argv: list[str]) -> int:
    k, a = _win32()
    for root in spec.get("writable", []):
        _icacls(str(root), "L")            # 可写根：Low-IL 可写
    # 可写根**内部**声明的只读洞：Windows 上 deny 原语已经存在 —— 就是 git 那条用的
    # "重打 Medium 标签"。`(OI)(CI)` 会往下传播，所以在可写根之后打、按深度升序打，
    # 于是最具体的那条赢（#900-B2：不必等 write_layers 改成补集覆盖）。
    for root in spec.get("readonly", []):
        _icacls(str(root), "M")
    for marker in spec.get("git", []):
        _icacls(str(marker), "M")          # 其下 .git：打 Medium，Low-IL 写不进（Medium 无需特权）
    job = _make_job(k, memory_bytes=int(spec.get("memory_bytes") or 0),
                    pids=int(spec.get("pids") or 0))
    return _spawn_low_and_wait(k, a, argv, job, use_std_handles=True)


def probe() -> dict:
    """自检：在临时目录上真跑一遍，报告哪些不变量在这台机器上确实成立。"""
    import os
    import shutil

    caps = {"write_boundary": False, "git_unwritable": False,
            "readonly_carveout": False,
            "pids_cap": False, "mem_cap": False, "group_kill": False,
            "payload_home": False}
    base = tempfile.mkdtemp(prefix="hf-win32-probe-")
    try:
        k, a = _win32()
        allowed = os.path.join(base, "allowed")
        denied = os.path.join(base, "denied")
        gitdir = os.path.join(allowed, ".git")
        # 可写根**内部**的只读洞（#899/#900 的形状：writable=[allowed]、
        # readonly=[allowed/deps]）—— 探针必须真写一次才知道标签传播的次序对不对。
        carve = os.path.join(allowed, "deps")
        os.makedirs(allowed)
        os.makedirs(denied)
        os.makedirs(gitdir)
        os.makedirs(carve)
        _icacls(allowed, "L")
        _icacls(carve, "M")
        _icacls(gitdir, "M")
        job = _make_job(k, memory_bytes=0, pids=0)
        af, df, gf = (os.path.join(allowed, "a"), os.path.join(denied, "b"),
                      os.path.join(gitdir, "HACK"))
        cf = os.path.join(carve, "c")
        # 不给路径加引号：tempfile 的 tmp 路径没有空格，而 list2cmdline 会把内层引号转成
        # `\"`（CommandLineToArgvW 规则），cmd.exe 不认那种转义、重定向目标会变成带反斜杠
        # 的坏路径。真实载荷是 `bash -c`（bash 认这套转义），不受影响；只有本探针用 cmd。
        cmd = ["cmd.exe", "/c",
               f"echo A>{af} & echo B>{df} & echo G>{gf} & echo C>{cf}"]
        _spawn_low_and_wait(k, a, cmd, job, timeout_ms=20000, inherit=False)
        caps["write_boundary"] = os.path.exists(af) and not os.path.exists(df)
        caps["git_unwritable"] = os.path.exists(af) and not os.path.exists(gf)
        caps["readonly_carveout"] = os.path.exists(af) and not os.path.exists(cf)

        # pids_cap：装满一个 ActiveProcessLimit=1 的 job，第二个进程应被 1816 拒
        pjob = _make_job(k, memory_bytes=0, pids=1)
        assigned = _try_fill_job(k, a, pjob)
        caps["pids_cap"] = assigned <= 1
        # mem_cap：Job 接受内存上限即视为可用（内核保证；§11 单测过 OOM）
        try:
            _make_job(k, memory_bytes=128 * 1024 * 1024, pids=0)
            caps["mem_cap"] = True
        except OSError:
            caps["mem_cap"] = False
        # group_kill：起一个长命令、TerminateJobObject、确认它死
        caps["group_kill"] = _probe_group_kill(k, a)
        caps["payload_home"] = _probe_payload_home(k, a, base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return caps


# 载荷自己问系统「我的 LocalAppData 在哪」，然后往里写一个文件。GUID 按内存布局
# （bytes_le）当指针传；HRESULT 0 才写。
_HOME_PROBE = (
    "import ctypes,uuid\n"
    "f=ctypes.windll.shell32.SHGetKnownFolderPath\n"
    "f.argtypes=[ctypes.c_char_p,ctypes.c_uint32,ctypes.c_void_p,"
    "ctypes.POINTER(ctypes.c_wchar_p)]\n"
    "p=ctypes.c_wchar_p()\n"
    "g=uuid.UUID('F1B32785-6FBA-4FCF-9D55-7B8E7F157091').bytes_le\n"
    "f(g,0,None,ctypes.byref(p)) or open(p.value+'\\\\hf-home-probe','w').write('x')\n"
)


def _probe_payload_home(k, a, base: str) -> bool:
    """墙给的家在这台机器上接不接得住（core.isolation._native.payload_home）。

    Windows 程序经 Known Folder 找 ``%LOCALAPPDATA%``，不看同名环境变量；它的路径是
    注册表里的 ``%USERPROFILE%\\AppData\\Local`` 按**进程自己的环境**展开的。所以把载荷的
    ``USERPROFILE`` 换成一个带 AppData 的目录，系统给出的 LocalAppData 就跟着换 ——
    这里真起一个 Low-IL 载荷去问、去写，不靠这句话本身。组策略把它重定向成绝对路径的
    机器上它不跟着走，这里就是 False，账上看得见。
    """
    import os

    home = os.path.join(base, "home")
    local = os.path.join(home, "AppData", "Local")
    os.makedirs(os.path.join(home, "AppData", "Roaming"))
    os.makedirs(local)
    _icacls(home, "L")
    saved = os.environ.get("USERPROFILE")
    os.environ["USERPROFILE"] = home          # lpEnvironment=NULL：子进程继承本进程的环境
    try:
        job = _make_job(k, memory_bytes=0, pids=0)
        _spawn_low_and_wait(k, a, [sys.executable, "-I", "-c", _HOME_PROBE], job,
                            timeout_ms=20000, inherit=False)
    except OSError:
        return False
    finally:
        if saved is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = saved
    return os.path.exists(os.path.join(local, "hf-home-probe"))


def _try_fill_job(k, a, job: V, want: int = 4) -> int:
    """往 job 里塞 want 个挂起进程，返回成功塞进去的个数（上限时 Assign 报 1816）。"""
    token = _low_il_primary_token(a)
    assigned = 0
    for _ in range(want):
        si = STARTUPINFOW(); si.cb = ctypes.sizeof(STARTUPINFOW)
        pi = PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer('cmd.exe /c ping -n 4 127.0.0.1 >nul')
        if not a.CreateProcessAsUserW(token, None, cmd, None, None, False,
                                      CREATE_SUSPENDED | CREATE_NO_WINDOW, None, None,
                                      ctypes.byref(si), ctypes.byref(pi)):
            break
        if not k.AssignProcessToJobObject(job, pi.hProcess):
            k.CloseHandle(pi.hProcess)
            break
        k.ResumeThread(pi.hThread)
        assigned += 1
    return assigned


def _probe_group_kill(k, a) -> bool:
    """起一个长跑命令、放进 job、TerminateJobObject，确认它当场死。"""
    k.TerminateJobObject.argtypes = [V, wintypes.UINT]
    token = _low_il_primary_token(a)
    job = _make_job(k, memory_bytes=0, pids=0)
    si = STARTUPINFOW(); si.cb = ctypes.sizeof(STARTUPINFOW)
    pi = PROCESS_INFORMATION()
    cmd = ctypes.create_unicode_buffer('cmd.exe /c ping -n 30 127.0.0.1 >nul')
    if not a.CreateProcessAsUserW(token, None, cmd, None, None, False,
                                  CREATE_SUSPENDED | CREATE_NO_WINDOW, None, None,
                                  ctypes.byref(si), ctypes.byref(pi)):
        return False
    k.AssignProcessToJobObject(job, pi.hProcess)
    k.ResumeThread(pi.hThread)
    k.TerminateJobObject(job, 1)
    # 0 = WAIT_OBJECT_0（退出了）；非 0（含 WAIT_TIMEOUT 0x102）= 还活着
    return k.WaitForSingleObject(pi.hProcess, 5000) == 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "probe":
        print(json.dumps(probe()), flush=True)
        return 0
    try:
        assert argv[1] == "run"
        spec = json.loads(argv[2])
        assert argv[3] == "--" and len(argv) > 4
        command = argv[4:]
    except (IndexError, ValueError, AssertionError):
        print("usage: _win32_exec.py run '<spec json>' -- cmd ...", file=sys.stderr)
        return 64
    try:
        return run(spec, command)
    except OSError as exc:
        print(f"HARNESS_ISOLATION_ERROR win32_setup_failed:{exc}", file=sys.stderr, flush=True)
        return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

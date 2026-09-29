"""扫盘闸：主机 CPU / 内存怎么读，运行时只许有一个答案 —— `shared.lib.hostinfo`。

两种"读主机内存"的写法在非 Linux 上会静默失效或读不到：``os.sysconf``（Windows 没有）和
直接 ``psutil.virtual_memory``（散着写就会有第二个答案）。收口之后这两样只能出现在
hostinfo 里。用 AST 扫调用，不扫文本。

注意：``/proc/meminfo`` 不在这道闸里 —— ``build_resource_guard`` 用它读的是"cgroup 分到多少
内存 + Swap + PSI"，是 Linux 沙箱内的另一个问题，不是主机事实。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN = [
    ROOT / "core",
    ROOT / "shared",
    ROOT / "nodes",
    ROOT / "platform_runtime.py",
    ROOT / "chat.py",
    ROOT / "platform" / "backend" / "app",
]
THE_ONLY_ANSWER = ROOT / "shared" / "lib" / "hostinfo.py"


def _runtime_files():
    for entry in SCAN:
        if entry.is_file():
            yield entry
            continue
        for path in entry.rglob("*.py"):
            if "tests" in path.parts:
                continue
            yield path


def _uses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(ROOT)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue
        # os.sysconf(...)
        if isinstance(fn.value, ast.Name) and fn.value.id == "os" and fn.attr == "sysconf":
            hits.append(f"{rel}:{node.lineno} os.sysconf()")
        # psutil.virtual_memory(...)
        if isinstance(fn.value, ast.Name) and fn.value.id == "psutil" and fn.attr == "virtual_memory":
            hits.append(f"{rel}:{node.lineno} psutil.virtual_memory()")
    return hits


def test_the_scanner_sees_the_one_place_that_reads_host_memory():
    assert _uses(THE_ONLY_ANSWER), "扫描器认不出 hostinfo.py 里的 psutil.virtual_memory，守卫会静默归零"


def test_no_runtime_code_reads_host_memory_outside_hostinfo():
    offenders = [hit for path in _runtime_files() if path != THE_ONLY_ANSWER for hit in _uses(path)]
    assert not offenders, (
        "主机 CPU / 内存只能经 shared.lib.hostinfo（memory / logical_cpus / disk）"
        "—— 别把 os.sysconf（Windows 没有）或第二处 psutil.virtual_memory 加回来：\n  "
        + "\n  ".join(offenders)
    )

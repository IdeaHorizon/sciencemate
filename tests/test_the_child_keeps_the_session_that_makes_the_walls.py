"""能力在机器上有、在子进程里没有 —— 这类回归只能靠机检发现（#849）。

## 病例

2026-09-07 实测：UI 路径下一个吃资源的脚本**不受任何 cgroup 约束**地跑完 —— 188 个
进程（超 ``pids=128`` 限额 47%）、内存 +2GB、全程零干预，最后是脚本自己的 60 秒
deadline 到点退出。同一载荷在 CLI 下 **1.4–2.4 秒**内就被 ``pids=128`` 拦住。

排查链的落点不在墙上，在**环境**上：

1. 机器本身完全有能力 —— 手跑
   ``systemd-run --user --scope -p MemoryMax=64M -p TasksMax=16 /bin/true`` → rc=0；
2. 后端进程 environ 里 ``XDG_RUNTIME_DIR`` / ``DBUS_SESSION_BUS_ADDRESS`` 都在；
3. worker 进程里这两个**数量 = 0** —— 父进程构造子环境时用的是白名单，白名单里没有它们；
4. 于是 ``core/isolation/linux.py::_probe_systemd()`` 的行为探针必然失败，后端**如实**
   报告 ``mem_cap`` / ``pids_cap`` / ``cpu_cap`` 缺失。记账是诚实的，缺的是能力本身。

## 为什么必须是机检

agent 跑完之后的原话：「180 个进程全部 spawn 成功，没有任何一个因资源不足中途停掉」
「吃得消，而且很轻松」，并据此建议把规模放大到 1500 分片。**它把「防线不存在」读成了
「资源有余量」** —— 守卫是否生效根本不在它的可观测面内，它只看得到"进程起成功了"，
分不出"因为机器强"和"因为守卫没装上"。指望模型自查没有出路。

## 判据

两条，都不依赖跑在什么机器上：

* **结构**：决定某条能力存不存在的变量，必须在"一处回答"的放行名单里，并且真的活着
  穿过 ``harness_subprocess_env``。
* **扫盘**：不许再有第二份手抄的 env 白名单 —— #849 的直接原因就是同一份名单被抄了
  两遍、两遍都漏了同样的东西，而症状出现在第三个地方，报错指不回这里。

第三条**行为**判据只在这台机器真有用户级 systemd 会话时才跑（有就必须不丢）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shared.lib.platform_env import (
    LINUX_SESSION_ENV,
    passthrough_names,
    system_env_passthrough,
)

ROOT = Path(__file__).resolve().parents[1]


def test_the_session_variables_are_in_the_one_answer():
    """连用户级 systemd 会话要的两个变量，必须在放行名单里。"""
    assert {"XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"} <= passthrough_names()
    assert LINUX_SESSION_ENV <= passthrough_names()


def test_they_survive_the_filter(monkeypatch):
    """父进程有，子进程就必须还有 —— 白名单式过滤最容易吞掉的正是它们。"""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    picked = system_env_passthrough()
    assert picked["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert picked["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"


def _hand_rolled_env_allowlists() -> list[str]:
    """扫盘：形如 ``{k: v for k, v in os.environ.items() if k in {...字面量...}}`` 的地方。

    这是 #849 的形状 —— 一份**自己抄的**系统变量白名单。扫的是"这件事"，不是某个写法
    的名字，所以新抄的一份也会被抓到（[[feedback_guardrails_must_scan_not_list]]）。
    合法出路只有一条：同一个函数里走 ``system_env_passthrough`` / ``harness_subprocess_env``
    把系统变量补回来。
    """
    hits: list[str] = []
    for path in sorted((ROOT / "platform").rglob("*.py")) + sorted((ROOT / "core").rglob("*.py")):
        if "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(tree):
            if not isinstance(node, ast.DictComp):
                continue
            comp = node.generators[0] if node.generators else None
            if comp is None or not isinstance(comp.iter, ast.Call):
                continue
            if not (isinstance(comp.iter.func, ast.Attribute) and comp.iter.func.attr == "items"):
                continue
            if "environ" not in ast.dump(comp.iter.func.value):
                continue
            if not any(isinstance(t, ast.Set) for c in comp.ifs for t in ast.walk(c)):
                continue
            # 同一个函数体里把系统变量补回来了 → 合法
            window = "\n".join(source.splitlines()[max(0, node.lineno - 30):node.lineno + 30])
            if "system_env_passthrough" in window or "harness_subprocess_env" in window:
                continue
            hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return hits


def test_nobody_hand_rolls_a_second_env_allowlist():
    hits = _hand_rolled_env_allowlists()
    assert not hits, (
        "这些地方按字面量白名单过滤 os.environ，却没有把系统变量从一处回答补回来：\n  "
        + "\n  ".join(hits)
        + "\n漏掉的变量不会在这里报错 —— 它会在别的地方变成"
        "「能力在机器上有、在进程里没有」（#849：UI 路径下 188 进程无约束）。"
    )


def _host_has_a_user_systemd_session() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        return subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet",
             "-p", "MemoryMax=64M", "-p", "TasksMax=16", "/bin/true"],
            capture_output=True, timeout=20, check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(
    not _host_has_a_user_systemd_session(),
    reason="this host has no usable per-user systemd session; nothing to lose",
)
def test_a_child_does_not_lose_the_resource_walls_the_host_has():
    """机器有的资源墙，harness 子进程里必须还在。

    判据不依赖运行环境：**这台机器没有就跳过**，有就不许丢。#849 的实测形状正是
    "父有、子无"，而两边各自的记账都诚实 —— 只有把两边并排看才发现能力在中间掉了。
    """
    sys.path.insert(0, str(ROOT / "platform" / "backend"))
    from app.services.harness_runtime import harness_subprocess_env

    probe = (
        "import json;from core import isolation;"
        "print(json.dumps(isolation.enforcement_snapshot()))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True, text=True,
        env=harness_subprocess_env(ROOT, passthrough=("HARNESS_EXECUTOR",)), timeout=180,
    )
    import json

    record = json.loads((out.stdout or "{}").strip().splitlines()[-1])
    enforced = set(record.get("enforced") or ())
    assert {"mem_cap", "pids_cap"} <= enforced, (
        f"宿主有用户级 systemd 会话，子进程却报 {sorted(enforced)} —— "
        f"资源墙在传环境这一步掉了（#849）。missing="
        f"{record.get('missing_for_unattended')}"
    )

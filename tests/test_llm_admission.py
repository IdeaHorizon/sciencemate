"""issue #426：账户级 LLM 并发准入（core.llm_admission）。

nidy4 实测：四路 orchestrator + curator dreaming 同时起跑，每个进程自己的
Semaphore 都合规，账户级并发照样打爆 —— 上限是账户级的，守卫必须也是。

flock 的排他性对**同一进程的两个不同 fd** 同样成立（flock 按 open file
description 记账），所以跨进程语义可以在单进程内用两个句柄如实测出来。
"""
from __future__ import annotations

import asyncio
import os
import stat

import pytest

from core import llm_admission

#: 真实实现 —— 下面的 autouse fixture 会把 _slots_dir 换掉，
#: 但目录权限那条用例测的正是真实实现。
_REAL_SLOTS_DIR = llm_admission._slots_dir


@pytest.fixture(autouse=True)
def _isolated_slots(tmp_path, monkeypatch):
    """槽位目录隔离到 tmp_path —— 测试之间、测试与真实进程之间互不干扰。"""
    monkeypatch.setattr(
        llm_admission, "_slots_dir", lambda endpoint: tmp_path
    )
    # 轮询加速：测试里等待者要真的等，但不必等 0.5s
    monkeypatch.setattr(llm_admission, "_POLL_BASE_S", 0.02)


def test_capacity_from_env(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "3")
    assert llm_admission.capacity() == 3
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "0")
    assert llm_admission.capacity() == 0
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "garbage")
    assert llm_admission.capacity() == 8  # 解析失败退默认，不炸
    monkeypatch.delenv("HARNESS_LLM_MAX_CONCURRENT")
    assert llm_admission.capacity() == 8


def test_cap_zero_disables_admission(monkeypatch):
    """cap=0 = 准入关闭：立即放行，不碰文件系统。"""
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "0")

    async def go():
        async with llm_admission.llm_slot("http://x") as slot:
            assert slot is None

    asyncio.run(go())


def test_cap_one_serializes_two_callers(monkeypatch):
    """cap=1：第二个调用必须等第一个放槽 —— 顺序可观测。"""
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "1")
    order: list[str] = []

    async def caller(name: str, hold_s: float):
        async with llm_admission.llm_slot("http://x"):
            order.append(f"{name}:in")
            await asyncio.sleep(hold_s)
            order.append(f"{name}:out")

    async def go():
        t1 = asyncio.create_task(caller("a", 0.1))
        await asyncio.sleep(0.03)  # 确保 a 先进
        t2 = asyncio.create_task(caller("b", 0.0))
        await asyncio.gather(t1, t2)

    asyncio.run(go())
    assert order == ["a:in", "a:out", "b:in", "b:out"]


def test_low_priority_leaves_reserve_for_foreground(monkeypatch):
    """cap=2、reserve=1：low 只能用 1 个槽位；第二个 low 等着，normal 直接进。"""
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "2")
    events: list[str] = []

    async def hold(name: str, priority: str, release: asyncio.Event):
        async with llm_admission.llm_slot("http://x", priority=priority):
            events.append(f"{name}:in")
            await release.wait()
        events.append(f"{name}:out")

    async def go():
        gate = asyncio.Event()
        low1 = asyncio.create_task(hold("low1", "low", gate))
        await asyncio.sleep(0.05)
        assert "low1:in" in events  # 第一个 low 拿到唯一允许的槽位
        low2 = asyncio.create_task(hold("low2", "low", gate))
        await asyncio.sleep(0.08)
        assert "low2:in" not in events  # 第二个 low 被挡（保留位不给它）
        # normal 用保留位直接进
        norm = asyncio.create_task(hold("norm", "normal", gate))
        await asyncio.sleep(0.08)
        assert "norm:in" in events
        gate.set()
        await asyncio.gather(low1, low2, norm)

    asyncio.run(go())


def test_cap_one_low_priority_passes_through(monkeypatch):
    """cap=1 时 low 没有可竞争槽位 → 直接放行（退化成纯退避），绝不永等。"""
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "1")

    async def go():
        async with llm_admission.llm_slot("http://x", priority="low") as slot:
            assert slot is None

    asyncio.run(go())


def test_crash_releases_slot(monkeypatch):
    """持槽方异常退出 → 槽位随句柄关闭自动释放（flock 语义），下一位能进。"""
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "1")

    async def go():
        with pytest.raises(RuntimeError):
            async with llm_admission.llm_slot("http://x"):
                raise RuntimeError("boom")
        # 槽位必须已经空出来
        async with llm_admission.llm_slot("http://x") as slot:
            assert slot is not None

    asyncio.run(go())


def test_spawn_silent_inherits_priority(monkeypatch):
    """dreaming 的 twin 不能悄悄升回 normal。"""
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_MODEL", "m")
    from core.llm import LLMClient

    c = LLMClient()
    assert c.priority == "normal"
    c.priority = "low"
    assert c.spawn_silent().priority == "low"


# ---------------------------------------------------------------------------
# 跨用户（2026-08-14 node20：jichaoq 撞上 wangd 建的槽位文件 → PermissionError
# 一路冒到 run_loop，节点 run 当场炸）
# ---------------------------------------------------------------------------


def test_slots_dir_survives_umask(tmp_path, monkeypatch):
    """槽位目录必须 world-writable + sticky，且不被 umask 削掉。

    同机第二个用户能不能起 run，全看这一位。
    """
    monkeypatch.setattr(llm_admission.tempfile, "gettempdir", lambda: str(tmp_path))
    old = os.umask(0o077)  # 最刻薄的 umask：不修就是 0700
    try:
        d = _REAL_SLOTS_DIR("http://x")
    finally:
        os.umask(old)
    assert stat.S_IMODE(d.stat().st_mode) == 0o1777


def test_slot_file_is_world_writable(tmp_path, monkeypatch):
    """槽位文件同理 —— 目录可写但文件 0644，第二个用户照样开不了。"""
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "1")
    old = os.umask(0o077)
    try:
        async def go():
            async with llm_admission.llm_slot("http://x") as slot:
                assert slot is not None
        asyncio.run(go())
    finally:
        os.umask(old)
    f = tmp_path / "slot-0.lock"
    assert stat.S_IMODE(f.stat().st_mode) == 0o666


def test_readonly_slot_still_competes(tmp_path, monkeypatch):
    """别人用旧权限建下的只读槽位：退回只读参与竞争，别当它不存在。

    "打不开就跳过"会让第二个用户各自跑满 cap —— 那正是 #426 的病。
    """
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "1")
    f = tmp_path / "slot-0.lock"
    f.touch()
    f.chmod(0o444)

    async def go():
        async with llm_admission.llm_slot("http://x") as held:
            assert held is not None
            # 唯一的槽位被占着 → 第二个必须等，不能放行
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(_enter("http://x"), timeout=0.3)

    asyncio.run(go())


async def _enter(endpoint: str):
    async with llm_admission.llm_slot(endpoint):
        return True


def test_unopenable_slots_pass_through(monkeypatch):
    """一个槽位都打不开 → 放行（退回纯退避），不抛也不永等。

    准入是保护机制，不是新的单点故障。
    """
    monkeypatch.setenv("HARNESS_LLM_MAX_CONCURRENT", "4")
    monkeypatch.setattr(llm_admission, "_open_slot", lambda p: None)
    llm_admission._degraded.clear()

    async def go():
        async with llm_admission.llm_slot("http://x") as slot:
            assert slot is None  # 没拿到槽，但请求照发

    asyncio.run(asyncio.wait_for(go(), timeout=2))


def test_slot_open_never_follows_symlinks(tmp_path, monkeypatch):
    """目录是 world-writable 的：别顺着别人种的软链去写别处。"""
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    link = tmp_path / "slot-0.lock"
    link.symlink_to(victim)

    assert llm_admission._open_slot(link) is None
    assert victim.read_text() == "keep me"


# ── Windows：os 没有 O_NOFOLLOW（真机 09-08：每次 LLM 调用都 AttributeError 打挂 orchestrator）──

def test_o_nofollow_is_the_posix_flag_or_zero():
    import os as _os
    from core import llm_admission
    # POSIX：真旗标（逐字不变）；Windows：0（无操作）
    assert llm_admission._O_NOFOLLOW == getattr(_os, "O_NOFOLLOW", 0)


def test_open_slot_works_when_o_nofollow_is_absent(tmp_path, monkeypatch):
    # 模拟 Windows（_O_NOFOLLOW=0）：_open_slot 仍开得出槽位，不抛 AttributeError
    from core import llm_admission
    monkeypatch.setattr(llm_admission, "_O_NOFOLLOW", 0)
    fh = llm_admission._open_slot(tmp_path / "slot-0.lock")
    assert fh is not None, "没有 O_NOFOLLOW 时开不出槽位了"
    fh.close()


def test_no_direct_posix_only_os_calls_in_module():
    # 机械闸：模块里不许再直接写 POSIX-only 的 os.O_NOFOLLOW / os.fchmod（Windows 上 AttributeError，
    # 每次 LLM 调用都打挂 orchestrator）——一律走 _O_NOFOLLOW / _FCHMOD。
    # getattr(os, "...", ...)（字符串取属性）不算，天然豁免。
    import ast
    from pathlib import Path
    posix_only = {"O_NOFOLLOW", "fchmod", "fchown", "O_PATH"}
    tree = ast.parse((Path(__file__).resolve().parents[1] / "core" / "llm_admission.py")
                     .read_text(encoding="utf-8"))
    bad = [f"os.{n.attr}@{n.lineno}" for n in ast.walk(tree)
           if isinstance(n, ast.Attribute) and n.attr in posix_only
           and isinstance(n.value, ast.Name) and n.value.id == "os"]
    assert not bad, f"直接用了 POSIX-only 的 os 调用（Windows AttributeError），改走 getattr 常量：{bad}"

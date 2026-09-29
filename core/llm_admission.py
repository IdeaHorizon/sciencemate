"""跨进程 LLM 并发准入（issue #426）。

退避回答的是"撞墙之后等多久"；准入回答的是"别撞墙"。这两个不是一回事：

nidy4 实测（材料 E2E，2026-08-13）：四路 orchestrator 同时起跑 + curator
dreaming 抢并发，把账户级并发位打满 —— 每个进程自己的 asyncio.Semaphore
都守得好好的，但**上限是账户级的，守卫是进程级的**，四个各自合规的进程
加起来照样打爆。llm.py 那套 5/10/20/40/80s 的 429 退避只能把撞墙摊薄，
需求超过容量时怎么退都还是撞：literature/hypothesis/curator 直接被 429
打空，一整条管线没走出第一步。

机制：按 endpoint 分组的**文件锁槽位**（flock）。同一台机器上的所有
harness 进程共享同一组槽位文件 —— 包括属于**不同 OS 用户**的进程（一台
推理机上几个研究者各跑各的，配额是那台机器的，不是某个用户的）；
acquire = 对某个槽位文件拿到排他锁，release = 释放锁。进程崩溃时 OS
自动释放 flock —— 不会留下死槽位。

只在**真正发请求**时占槽：退避睡眠在槽外进行，不占用并发位。流式调用
从发起到流耗尽全程占槽 —— 服务端的并发位就是被整个生成过程占着的，
提前放槽等于自欺。

优先级两档：
  normal —— 前台（用户在等的 run）：可用全部槽位。
  low    —— 后台代谢（curator dreaming）：只能用部分槽位，永远给前台
            留出余量。dreaming 晚几分钟跑毫无代价；前台被 429 打死一个
            节点 run 的代价是实测过的。

配置：env `HARNESS_LLM_MAX_CONCURRENT`（默认 8；0 = 关闭准入，恢复
纯退避行为）。部署侧应把它设成 provider 实际并发配额（GPUStack 的
per-user concurrency、云厂商的 RPM 换算）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO

from shared.lib import filelock

log = logging.getLogger(__name__)

#: 等槽超过这个秒数打一条 warning（只打一次）——让"变慢"可诊断，
#: 而不是让用户以为卡死。
_SLOW_WAIT_WARN_S = 30.0

_ENV_CAP = "HARNESS_LLM_MAX_CONCURRENT"
_DEFAULT_CAP = 8

#: 拿不到槽位时的轮询间隔（秒）。带抖动，避免多个等待者同步唤醒。
_POLL_BASE_S = 0.5

#: low 优先级给前台保留的槽位比例的分母：reserve = max(1, cap // 4)。
_RESERVE_DIVISOR = 4


def capacity() -> int:
    """当前配置的并发上限。0 = 准入关闭。"""
    try:
        v = int(os.getenv(_ENV_CAP, str(_DEFAULT_CAP)) or _DEFAULT_CAP)
    except ValueError:
        v = _DEFAULT_CAP
    return max(0, v)


#: 槽位目录名的版本号。**改变槽位文件的权限约定时必须 +1**：老目录和老
#: 文件的属主可能是别的用户，本进程 chmod 不动它们，只能换个名字重来 ——
#: 比要求每台部署机上有人拿 root 去清 /tmp 可靠。
_DIR_VERSION = 2

#: 槽位目录/文件的权限：目录 world-writable + sticky（和 /tmp 本身同一个
#: 约定：谁都能在里面建，只能删自己的），文件 world-writable。
_DIR_MODE = 0o1777
_FILE_MODE = 0o666

#: O_NOFOLLOW 是 POSIX-only 的加固（world-writable 目录下别顺着别人种的符号链接走）。
#: Windows 上 `os` 没有这个常量（真机 AttributeError 直接把每次 LLM 调用打挂），而槽位
#: 目录在 Windows 落在用户私有的 %LOCALAPPDATA%\afs 下、不是 world-writable，本就不需要它。
#: 用 getattr 兜底：POSIX 上是真旗标（逐字不变），Windows 上是 0（无操作）。
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: 同理 `os.fchmod`（按 fd 改权限）也是 POSIX-only，Windows 上没有（真机 AttributeError）。
#: 它本就是"能修则修"的尽力而为（umask 削过 / 别人用旧权限建的槽位），Windows 上槽位用户私有、
#: 不共享，省掉即可。POSIX 上是真函数（逐字不变），Windows 上是 None（跳过）。
_FCHMOD = getattr(os, "fchmod", None)


def _slots_dir(endpoint: str) -> Path:
    """endpoint → 槽位目录。按 endpoint 分组：不同 provider 各有各的配额。

    目录必须**跨 OS 用户可写**：同一台机器上的 harness 进程属于不同研究者
    （node20 上 wangd / jichaoq / nidy4 各跑各的），而配额是这台机器的推理
    服务，不是某个用户的 —— 按用户分目录等于把 #426 的病（各自合规、加起来
    打爆）从"跨进程"平移到"跨用户"。

    mkdir 的 mode 会被 umask 削掉（umask=002 → 0775 → 第二个用户 open() 直接
    EACCES，2026-08-14 node20 现场），所以创建后显式 chmod 一次。
    """
    h = hashlib.sha1((endpoint or "default").encode()).hexdigest()[:12]
    d = Path(tempfile.gettempdir()) / f"harness-llm-slots-v{_DIR_VERSION}-{h}"
    try:
        d.mkdir(exist_ok=True)
        os.chmod(d, _DIR_MODE)
    except OSError:
        # 目录已存在且属主是别人 → chmod EPERM。能不能用交给 _open_slot 判，
        # 这里不替它下结论。
        pass
    return d


def _open_slot(path: Path) -> IO | None:
    """打开（必要时创建）一个槽位文件，拿不到就返回 None，不抛。

    - O_NOFOLLOW（`_O_NOFOLLOW`）：目录是 world-writable 的，别顺着别人种下的符号链接走。
      Windows 上没有这个旗标、也不需要（槽位目录用户私有），退化成 0。
    - 不截断：槽位文件永远是空的，"w" 的 O_TRUNC 只是多一份写权限要求。
    - 只读兜底：flock 不挑打开模式（flock(2)：与打开模式无关）。别的用户
      用旧 umask 建下的 0644 槽位，只读也能参与竞争 —— 退回只读比放弃这个
      槽位好，后者会让两个用户各自跑满 cap。
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW, _FILE_MODE)
    except OSError:
        try:
            fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
        except OSError:
            return None
    if _FCHMOD is not None:
        try:
            _FCHMOD(fd, _FILE_MODE)  # umask 削过的、或别人建的旧权限，能修则修（Windows 无此调用、跳过）
        except OSError:
            pass
    try:
        return os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return None


def _allowed_slots(cap: int, priority: str) -> range:
    """该优先级允许竞争的槽位下标。

    low 只能用前 `cap - reserve` 个；normal 可用全部。reserve 至少 1 ——
    哪怕 cap=1，low 也拿不到那个唯一槽位（前台永远有路走）。
    """
    if priority == "low":
        reserve = max(1, cap // _RESERVE_DIVISOR)
        return range(max(0, cap - reserve))
    return range(cap)


def _try_acquire(endpoint: str, cap: int, priority: str) -> tuple[IO | None, int]:
    """非阻塞扫一遍允许的槽位。

    返回 `(持锁句柄 or None, 本轮真正打开成功的槽位数)`。第二个值是给调用方
    区分两件事的 —— **"槽位都被占着，该等"** 和 **"一个槽位都打不开，该放行"**。
    旧版把它们都表达成 None（而且打不开时直接让 OSError 冒出去），于是一个
    /tmp 权限问题当场炸掉整个 run。
    """
    d = _slots_dir(endpoint)
    slots = list(_allowed_slots(cap, priority))
    random.shuffle(slots)  # 别让所有进程都从 slot-0 开始挤
    usable = 0
    for i in slots:
        fh = _open_slot(d / f"slot-{i}.lock")
        if fh is None:
            continue
        usable += 1
        try:
            filelock.acquire(fh, blocking=False)
            return fh, usable
        except OSError:
            fh.close()
    return None, usable


#: 已经就"准入失效"喊过的 endpoint。喊一次就够：每次 LLM 调用都喊会把
#: 日志淹掉，而这是个装不上就一直装不上的状态。
_degraded: set[str] = set()


def _warn_degraded_once(endpoint: str) -> None:
    if endpoint in _degraded:
        return
    _degraded.add(endpoint)
    log.warning(
        "LLM 并发准入不可用：%s 下一个槽位文件都打不开（多半是同机另一个用户"
        "先建了目录/文件且权限不共享）。本进程退化成纯退避 —— 不再有账户级"
        "并发上限，429 会变多。修法：删掉该目录让它按 %04o 重建，或 chmod 到"
        "跨用户可写。",
        _slots_dir(endpoint), _DIR_MODE & 0o7777,
    )


@asynccontextmanager
async def llm_slot(endpoint: str, *, priority: str = "normal"):
    """在 with 体内持有一个账户级并发槽位。

    cap=0（准入关闭）或 low 优先级无可用槽位区间时直接放行 —— 准入是
    保护机制，不是新的单点故障：配置异常时退化成纯退避，绝不能把所有
    LLM 调用锁死。
    """
    cap = capacity()
    if cap <= 0:
        yield None
        return
    if not _allowed_slots(cap, priority):
        # cap=1 且 low：没有可竞争的槽位。放行而不是永等 —— dreaming
        # 被 429 打回去有退避兜底，被锁死在这里则永远不会醒。
        yield None
        return
    fh = None
    waited_s = 0.0
    warned = False
    while fh is None:
        fh, usable = _try_acquire(endpoint, cap, priority)
        if fh is None and usable == 0:
            # 一个槽位都打不开：目录/文件属主是别的用户、/tmp 只读、fd 耗尽…
            # 放行，退化成纯退避。准入是保护机制，不是新的单点故障 ——
            # 2026-08-14 node20：第二个用户撞上第一个用户建的 0644 槽位文件，
            # PermissionError 一路冒到 run_loop，节点 run 当场炸。
            _warn_degraded_once(endpoint)
            yield None
            return
        if fh is None:
            delay = _POLL_BASE_S + random.uniform(0, 0.3)
            waited_s += delay
            if waited_s >= _SLOW_WAIT_WARN_S and not warned:
                warned = True
                log.warning(
                    "LLM 并发槽位已满（cap=%d, priority=%s），本调用已排队 %.0fs "
                    "—— 这是准入在防 429 雪崩，不是卡死。并发需求持续超配额时"
                    "应调大 %s 或减少并行 run。",
                    cap, priority, waited_s, _ENV_CAP,
                )
            await asyncio.sleep(delay)
    try:
        yield fh
    finally:
        try:
            filelock.release(fh)
        finally:
            fh.close()


def waiters_snapshot(endpoint: str) -> dict:
    """诊断用：当前占用中的槽位数 / 上限。不加锁，只探测。"""
    cap = capacity()
    if cap <= 0:
        return {"capacity": 0, "held": 0}
    held = 0
    d = _slots_dir(endpoint)
    for i in range(cap):
        p = d / f"slot-{i}.lock"
        if not p.exists():
            continue
        fh = _open_slot(p)
        if fh is None:
            continue  # 打不开的槽位诊断不了，但诊断工具不该自己抛
        try:
            filelock.acquire(fh, blocking=False)
            filelock.release(fh)
        except OSError:
            held += 1
        finally:
            fh.close()
    return {"capacity": cap, "held": held}

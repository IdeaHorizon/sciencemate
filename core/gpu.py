"""本机 GPU 的分配与记账（#893 阶段 1，wangd 10-21 裁决）。

**做的是分配与记账，不是隔离。** 没有 MIG / vGPU 时，同机其它进程（包括别的用户、
不经本平台的程序）照样看得见、用得上这张卡 —— ``gpu_capability().isolated`` 恒为 False，
理由写在那里。做到的是三件事：

1. **本平台的 run 之间不撞卡**：一个 run 经人批准拿到 N 张卡（``submit_job(gpus=N)`` 时
   框架替模型弹的那张卡），这 N 张卡的锁在它手里，别的 run 分不到；
2. **有账可查**：每张卡一个锁文件 ``<jobs_root>/gpu-locks/<index>.json``，写着谁占着
   （哪个 run、哪个后台作业、它们的进程出生身份）；
3. **占卡的进程没了，锁自己回收**：判活按进程出生身份（``process_control.identity_matches``），
   号被复用也认得出来。worker 崩了、作业被杀了，下一个来分卡的人顺手收回。

持有者有两种：``run``（拿到授权的那个 worker 进程）与 ``job``（这个 run 起的后台作业，
按它的控制目录判活）。run 结束时放掉自己那一份（``core.executor.finalize_run``）；作业
还在跑，卡就继续算它的，作业结束才空出来。

卡的编号用 ``nvidia-smi`` 的 index（PCI 总线顺序），所以交给命令的环境同时设
``CUDA_DEVICE_ORDER=PCI_BUS_ID`` —— CUDA 默认按「最快的在前」排，两套编号会对不上。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOCK_DIR = "gpu-locks"
#: 作业刚登记、还没起来（record 里没有 pid）时，给它多久算活着。
_JOB_STARTUP_GRACE_S = 300.0


@dataclass(frozen=True)
class GpuLease:
    """一个 run 拿到的卡。"""

    indices: tuple[int, ...]
    run_id: str

    def env(self) -> dict[str, str]:
        return {"CUDA_VISIBLE_DEVICES": ",".join(str(i) for i in self.indices),
                "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}

    def as_dict(self) -> dict[str, Any]:
        return {"indices": list(self.indices), "run_id": self.run_id}


#: 没拿到授权的 run：一张卡都不给看。值是 ``-1`` 不是空串：CUDA 遇到第一个不存在的编号
#: 就停，``-1`` 在 Linux 与 Windows 上都等于「没有卡」；空串在 Windows 上等于没设
#: （11-04 9800x3d 实测：Low IL 下 ``CUDA_VISIBLE_DEVICES=""`` 照样 cuInit=0、看得见 5080）。
#: 只是环境变量 —— 命令自己改掉它照样用得上（Windows 上没有墙管这件事；Linux 上没授权
#: 时墙本来就拦着 CUDA 初始化，cuInit=304）。
HIDDEN = {"CUDA_VISIBLE_DEVICES": "-1"}


def visible_indices() -> list[int] | None:
    """``nvidia-smi`` 看得见的卡的 index。查不出来（没有 nvidia-smi / 跑不通）→ None，不是空。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=index", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    found: list[int] = []
    for line in out.stdout.splitlines():
        text = line.strip()
        if text.isdigit():
            found.append(int(text))
    return found


def _locks_dir() -> Path:
    from core.sandbox import _jobs_root

    path = _jobs_root() / LOCK_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _holder_alive(holder: dict[str, Any]) -> bool:
    from shared.lib import process_control

    role = holder.get("role")
    if role == "run":
        pid = int(holder.get("pid") or 0)
        if pid <= 0:
            return False
        match = process_control.identity_matches(pid, holder.get("birth"))
        return process_control.alive(pid) if match is None else bool(match)
    if role == "job":
        record = _read(Path(str(holder.get("control_dir") or "")) / "record.json")
        if record is None:
            return False
        status = str(record.get("status") or "created")
        if status in {"exited", "dead", "failed", "cancelled", "completed", "stopped"}:
            return False
        pid = int(record.get("pid") or 0)
        if not pid:
            created = float(record.get("created_at") or 0)
            return (time.time() - created) < _JOB_STARTUP_GRACE_S
        match = process_control.identity_matches(pid, record.get("birth_identity"))
        return process_control.alive(pid) if match is None else bool(match)
    return False


def _live_holders(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not data:
        return []
    return [h for h in data.get("holders") or [] if isinstance(h, dict) and _holder_alive(h)]


def _mutex():
    from shared.lib import filelock

    return filelock.exclusive(_locks_dir() / ".mutex")


def ledger(visible: list[int] | None = None) -> list[dict[str, Any]]:
    """每张卡现在是谁的（只列有活持有者的）。给开局说明、诊断与测试用。"""
    out = []
    for path in sorted(_locks_dir().glob("*.json")):
        data = _read(path)
        live = _live_holders(data)
        if live and data is not None:
            out.append({"index": data.get("index"), "run_id": data.get("run_id"),
                        "holders": live})
    if visible is not None:
        out = [row for row in out if row.get("index") in visible]
    return out


def free_indices(visible: list[int] | None = None) -> list[int]:
    seen = visible_indices() if visible is None else visible
    if not seen:
        return []
    taken = {row["index"] for row in ledger()}
    return [i for i in seen if i not in taken]


def acquire(run_id: str, count: int, *, visible: list[int] | None = None) -> GpuLease | None:
    """给这个 run 分 ``count`` 张空闲的卡；不够就一张都不分（返回 None）。"""
    from shared.lib import process_control

    if count <= 0:
        return None
    with _mutex():
        seen = visible_indices() if visible is None else visible
        free = free_indices(seen)
        if len(free) < count:
            return None
        chosen = tuple(free[:count])
        me = os.getpid()
        holder = {"role": "run", "pid": me, "birth": process_control.birth_identity(me),
                  "run_id": run_id}
        for index in chosen:
            _write(_locks_dir() / f"{index}.json",
                   {"index": index, "run_id": run_id, "acquired_at": time.time(),
                    "holders": [holder]})
    return GpuLease(chosen, run_id)


def attach_job(lease: GpuLease, control_dir: Path | str) -> None:
    """这个 run 起的后台作业也算这几张卡的持有者：run 先结束，卡等作业结束才空出来。"""
    with _mutex():
        for index in lease.indices:
            path = _locks_dir() / f"{index}.json"
            data = _read(path) or {"index": index, "run_id": lease.run_id, "holders": []}
            holders = [h for h in data.get("holders") or [] if isinstance(h, dict)]
            holders.append({"role": "job", "control_dir": str(control_dir),
                            "run_id": lease.run_id})
            data["holders"] = holders
            _write(path, data)


def release_run(lease: GpuLease) -> list[int]:
    """run 放掉自己那一份。返回这次真正空出来的卡（没有作业还占着的）。"""
    freed: list[int] = []
    me = os.getpid()
    with _mutex():
        for index in lease.indices:
            path = _locks_dir() / f"{index}.json"
            data = _read(path)
            if data is None or data.get("run_id") != lease.run_id:
                continue                    # 早已被回收、或已经是别人的了
            holders = [h for h in data.get("holders") or [] if isinstance(h, dict)
                       and not (h.get("role") == "run" and int(h.get("pid") or 0) == me
                                and h.get("run_id") == lease.run_id)]
            live = [h for h in holders if _holder_alive(h)]
            if live:
                data["holders"] = live
                _write(path, data)
            else:
                try:
                    path.unlink()
                except OSError:
                    pass
                freed.append(index)
    return freed


__all__ = ["GpuLease", "HIDDEN", "LOCK_DIR", "acquire", "attach_job", "free_indices", "ledger",
           "release_run", "visible_indices"]

"""派活账本：这个后端派出过哪个 worker（窄单 D #1219，RFC_WORKER_START_IS_NOT_A_GATE §3.5）。

回收孤儿 worker 要先认亲。从前的判据只有「锁记录里的 pid 还活着、命令行像 platform_runtime --serve」——
pid 会被复用、命令行会撞车（开发者手起一个 worker、另一个数据根的后端派的那个），而认错的代价是
杀掉一个不归我们管的进程。

spawn token 是认亲的凭据：后端派活时生成，经环境变量交给 worker，worker 写进它的锁记录。
但后端只把 token 留在内存里，重启就没了 —— 而回收孤儿恰恰发生在重启之后。这里把每次派活落一行：
**只存 token 的哈希**（token 本身是控制通道握手的凭证，不落盘第二份）。

认亲四种结论：
- ``ours``：锁记录里的 token 与账本上这个会话最近一次派活对得上。可以收。
- ``not_ours``：账本在用，却对不上（或这个会话没有记录）。不杀，记一句为什么。
- ``legacy``：锁是在账本启用之前拿的（上一版派的 worker）—— 按从前的判据处理，升级不倒退。
- ``unknown``：账本读不出来（权限问题正是这份 RFC 要防的那一类）—— 按从前的判据处理并记 warning，
  不让一个读不到的文件造出新的硬失败。

写账本失败从不拦派活：派活是这一轮要的，账本是下一次重启要的。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

LEDGER_DIRNAME = "spawn-ledger"
#: 账本从什么时候开始记：在它之前拿的锁，账本上本来就不会有。
SINCE_FILENAME = ".since"

Verdict = Literal["ours", "not_ours", "legacy", "unknown"]


def ledger_dir() -> Path:
    from app.config import the_data_root

    return the_data_root() / LEDGER_DIRNAME


def _entry_path(directory: Path, project_id: str, session_id: str) -> Path:
    key = hashlib.sha256(f"{project_id}\0{session_id}".encode()).hexdigest()[:24]
    return directory / f"{key}.json"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def record_spawn(project_id: str, session_id: str, spawn_token: str, pid: int | None) -> None:
    """派出一个 worker 之后落一行。失败只记 warning。"""
    if not spawn_token:
        return
    try:
        directory = ledger_dir()
        directory.mkdir(parents=True, exist_ok=True)
        since = directory / SINCE_FILENAME
        if not since.exists():
            since.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        entry = _entry_path(directory, project_id, session_id)
        tmp = entry.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "token_sha256": _token_hash(spawn_token), "pid": pid,
            "spawned_at": datetime.now(UTC).isoformat(),
            "project_id": project_id, "session_id": session_id,
        }), encoding="utf-8")
        tmp.replace(entry)
    except OSError as exc:
        logger.warning("cannot record the spawn of %s/%s in the spawn ledger: %s", project_id, session_id, exc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def recognise(project_id: str, session_id: str, lock_row: dict[str, Any]) -> tuple[Verdict, str]:
    """锁记录里那个 worker 是不是我们派的。返回（结论, 一句给人看的原因）。"""
    directory = ledger_dir()
    try:
        since_text = (directory / SINCE_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return "legacy", "派活账本还没启用过：按命令行判据处理"
    except OSError as exc:
        return "unknown", f"派活账本读不出来（{type(exc).__name__}: {exc}）：按命令行判据处理"
    acquired = _parse_time(lock_row.get("acquired_at"))
    since = _parse_time(since_text.strip())
    token = str(lock_row.get("spawn_token") or "")
    try:
        entry = json.loads(_entry_path(directory, project_id, session_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        entry = None
    except (OSError, ValueError) as exc:
        return "unknown", f"这个会话的派活记录读不出来（{type(exc).__name__}: {exc}）：按命令行判据处理"
    if isinstance(entry, dict) and token and entry.get("token_sha256") == _token_hash(token):
        return "ours", "锁记录里的 spawn token 与派活账本对得上"
    if entry is None and since is not None and acquired is not None and acquired < since:
        return "legacy", "这把锁是派活账本启用之前拿的（上一版派的 worker）：按命令行判据处理"
    if not token:
        return "not_ours", "持有者的锁记录里没有 spawn token —— 不是本应用派出的"
    return "not_ours", "持有者的 spawn token 不在派活账本上 —— 不是本应用派出的"

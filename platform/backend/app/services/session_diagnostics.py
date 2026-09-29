"""一个会话的诊断包：「这里出了什么事」需要的记录，打成一个 zip 交到人手里。

## 为什么要有它

会话出了问题，能说清原因的记录都在磁盘上，但用户够不着：

- 会话自己的记录在 `<数据根>/project-worktrees/<项目>/<会话>/.research/runtime/runs/`
  下面。Windows 上数据根在默认隐藏的 AppData 里，再往下还有两层按 id 起名的目录。
  科研用户找不到，也不该要他们去找。
- **一个会话不止一个 run 目录。** 调度器派出去的节点子 run 和调度器自己的
  `orchestrator__…` 目录平级，按时间戳起名。只拷调度器那一个，节点里真正出错的
  那部分就丢了。所以这里收的是整个 runs 根。
- 后端日志（壳把后端的 stdout/stderr 接到 `<数据根>/logs/backend.log`）是整台
  后端的，按会话 id 搜只能搜到 HTTP 访问行，worker 的报错不带会话 id。所以这里
  按**时间窗**截：从会话创建前一点到现在。

## 包里有什么

    README.txt          给人看：里面有什么、有没有密钥
    manifest.json       版本、平台、会话、每个文件收了多少、哪些没收以及为什么
    runs/<run>/…        会话的全部 run 目录（调度器 + 子 run），原样
    logs/backend.log    后端日志按时间窗截出的一段
    logs/<其它>.log     日志目录里别的日志（Windows 壳的 shell.log 等）的尾部

## 两条边界

- **密钥出门前抹掉。** 这个包是要发给别人的。库里每一个模型后端的 key 都按原值
  替换；再过一遍后端统一的「什么算密钥」（`app.services.redaction`：名字像密钥的
  JSON 字段、私钥、连接串、AWS key、Bearer、sk-/pk_ token）—— 与界面入库脱敏是同一个
  判断，不另抄一份。不做界面那套截断和路径缩写：诊断包要的是原样的证据。
- **只有能操作这个会话的人能下载**（`drive_session`）。包里是整棵原始运行目录，
  比界面上任何一处给得都多。
- **组织服务器上的后端日志是很多人共用的。** 那里只收点名了这个会话或这个项目
  的那几条记录，别的日志文件一概不收；个人档整台机器只有一个人，时间窗内全收。
"""
from __future__ import annotations

import json
import os
import platform
import re
import stat
import sys
import tempfile
import zipfile
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: 日志目录在数据根下的名字。壳（`platform/desktop/*`）把后端输出接到这里。
LOGS_DIRNAME = "logs"
BACKEND_LOG_NAME = "backend.log"

#: 单个文件进包的上限。超过的文本日志留尾巴（出事的地方通常在最后），别的跳过。
FILE_CAP_BYTES = 64 * 1024 * 1024
#: 整包（未压缩）的上限。到顶就停，剩下的记进 manifest 的 skipped。
TOTAL_CAP_BYTES = 512 * 1024 * 1024
#: 后端日志截出来的那一段最多多大（留最新的）。
BACKEND_LOG_CAP_BYTES = 32 * 1024 * 1024
#: 后端日志最多往回扫多少字节。一台开了几个月的机器，日志可以很大。
BACKEND_LOG_SCAN_BYTES = 256 * 1024 * 1024
#: 日志目录里别的日志各收多少尾巴。
OTHER_LOG_TAIL_BYTES = 4 * 1024 * 1024
#: 时间窗往会话创建时刻之前多留一点：建会话那一刻前后的启动日志也有用。
LOG_WINDOW_LEAD = timedelta(minutes=10)

#: 超过上限时可以只留尾巴的文件：按行写的日志，截掉开头仍然每一行都读得懂。
_TAIL_READABLE_SUFFIXES = frozenset({".jsonl", ".log", ".txt"})

REDACTED = b"[REDACTED]"
#: 结构化日志每条记录的开头（`app.core.logging.StructuredFormatter`，本地时间）。
_LOG_TIMESTAMP = re.compile(rb"^timestamp=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


class Redactor:
    """把密钥从字节里抹掉：先按库里的原值，再按后端统一的「什么算密钥」。

    按原值抹是必须的：不长成 `sk-…` 的 key（各家形状不一）只有原值认得出。统一判断
    （``app.services.redaction.scrub_secrets_in_text``）管其余的 —— 口令、令牌、连接串、
    私钥，以及名字像密钥的 JSON 字段（运行目录里的 ``spawn_token`` 就是这么抹掉的）。
    二进制也过：按 ``surrogateescape`` 解码再编码，不是 UTF-8 的字节原样往返。
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        # 太短的不算密钥（`none`、`test` 这类会把正文抹花）。长的先替：一个值是
        # 另一个的前缀时，先替短的会留下一截尾巴。
        values = {
            secret.strip().encode()
            for secret in secrets
            if secret and len(secret.strip()) >= 8
        }
        self._values = sorted(values, key=len, reverse=True)

    def __call__(self, data: bytes) -> tuple[bytes, int]:
        from app.services.redaction import scrub_secrets_in_text

        count = 0
        for value in self._values:
            hits = data.count(value)
            if hits:
                data = data.replace(value, REDACTED)
                count += hits
        text, hits = scrub_secrets_in_text(data.decode("utf-8", errors="surrogateescape"))
        if hits:
            data = text.encode("utf-8", errors="surrogateescape")
            count += hits
        return data, count


async def known_secret_values(db: AsyncSession) -> list[str]:
    """这台后端握着的每一个模型凭据的原值 —— 抹的时候按值抹，新加的后端自动在列。"""
    from app.models.model_backend import ModelBackendConfig
    from app.services.model_backends import resolved_api_key

    values: list[str] = []
    for config in (await db.execute(select(ModelBackendConfig))).scalars().all():
        try:
            value = resolved_api_key(config)
        except Exception:  # 解不开的 key（换过加密钥匙）不影响打包
            continue
        if value:
            values.append(value)
    return values


@dataclass
class _Ledger:
    """manifest 里「收了什么、没收什么」的那两张表，和整包的体积账。"""

    included: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    total_bytes: int = 0
    redactions: int = 0


@dataclass(frozen=True)
class DiagnosticsBundle:
    path: Path
    filename: str
    manifest: dict


def _zip_time(moment: datetime) -> tuple[int, int, int, int, int, int]:
    local = moment.astimezone() if moment.tzinfo else moment
    if local.year < 1980:  # zip 的时间从 1980 年算起
        local = datetime(1980, 1, 1)
    return (local.year, local.month, local.day, local.hour, local.minute, local.second)


def _add(
    archive: zipfile.ZipFile,
    ledger: _Ledger,
    redact: Redactor,
    arcname: str,
    data: bytes,
    *,
    original_bytes: int,
    modified: datetime,
    note: str | None = None,
) -> bool:
    """放进包里并记账。整包到了上限就不放、记进 skipped，返回 False。

    上限只在这里判：运行记录和日志都经这一处，谁都不会绕过它（日志曾经不计入）。
    """
    if ledger.total_bytes + len(data) > TOTAL_CAP_BYTES:
        ledger.skipped.append(
            {"path": arcname, "reason": "整包到了上限", "bytes": original_bytes})
        return False
    data, hits = redact(data)
    info = zipfile.ZipInfo(arcname, date_time=_zip_time(modified))
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)
    ledger.total_bytes += len(data)
    ledger.redactions += hits
    entry: dict = {"path": arcname, "bytes": len(data), "original_bytes": original_bytes}
    if note:
        entry["note"] = note
    if hits:
        entry["redactions"] = hits
    ledger.included.append(entry)
    return True


def _read_tail(path: Path, size: int, cap: int) -> bytes:
    """最后 `cap` 字节，从下一个完整行开始。"""
    with path.open("rb") as handle:
        handle.seek(max(0, size - cap))
        data = handle.read()
    if size > cap:
        newline = data.find(b"\n")
        if 0 <= newline < len(data) - 1:
            data = data[newline + 1:]
    return data


def _run_files(runs_root: Path) -> list[Path]:
    """runs 根下的每个普通文件。调度器目录与每个 run 的顶层记录排在前面。

    到整包上限时先丢的是 `event_blobs/`、`scratch/` 这些深处的大件，而不是
    `events.jsonl` / `transcript.jsonl`。
    """
    found: list[Path] = []
    for directory, subdirectories, names in os.walk(runs_root, followlinks=False):
        subdirectories.sort()
        found.extend(Path(directory) / name for name in sorted(names))

    def priority(path: Path) -> tuple[int, int, str]:
        relative = path.relative_to(runs_root)
        nested = len(relative.parts) > 2
        orchestrator = relative.parts[0].startswith("orchestrator__")
        return (int(nested), int(not orchestrator), relative.as_posix())

    return sorted(found, key=priority)


def _collect_runs(
    archive: zipfile.ZipFile, ledger: _Ledger, redact: Redactor, runs_root: Path | None
) -> None:
    if runs_root is None or not runs_root.is_dir():
        ledger.skipped.append({"path": "runs/", "reason": "这个会话在这台机器上没有运行记录目录"})
        return
    for path in _run_files(runs_root):
        arcname = "runs/" + path.relative_to(runs_root).as_posix()
        try:
            info = os.lstat(path)
        except OSError as exc:
            ledger.skipped.append({"path": arcname, "reason": f"读不到：{exc}"})
            continue
        if not stat.S_ISREG(info.st_mode):
            # 套接字（worker.sock）、符号链接这类不是记录，打不进包，也不该跟过去。
            ledger.skipped.append({"path": arcname, "reason": "不是普通文件"})
            continue
        size = info.st_size
        note = None
        try:
            if size > FILE_CAP_BYTES:
                if path.suffix not in _TAIL_READABLE_SUFFIXES:
                    ledger.skipped.append(
                        {"path": arcname, "reason": "超过单文件上限", "bytes": size}
                    )
                    continue
                data = _read_tail(path, size, FILE_CAP_BYTES)
                note = f"只留了最后 {len(data)} 字节"
            else:
                data = path.read_bytes()
        except OSError as exc:
            ledger.skipped.append({"path": arcname, "reason": f"读不到：{exc}"})
            continue
        _add(
            archive, ledger, redact, arcname, data,
            original_bytes=size,
            modified=datetime.fromtimestamp(info.st_mtime),
            note=note,
        )


def slice_backend_log(
    path: Path,
    *,
    since_local: datetime,
    naming: tuple[str, ...] | None,
    cap_bytes: int = BACKEND_LOG_CAP_BYTES,
    scan_bytes: int = BACKEND_LOG_SCAN_BYTES,
) -> tuple[bytes, dict]:
    """从后端日志里截出 `since_local` 之后的记录。

    一条记录 = 带 `timestamp=` 的那一行 + 它后面不带时间戳的续行（traceback、
    uvicorn 自己打的行）。续行跟着它前面那条走，时间窗和点名都按整条判。
    `naming` 给了就只留提到其中任何一个的记录（组织服务器上用）。
    """
    size = path.stat().st_size
    start = max(0, size - scan_bytes)
    kept: deque[bytes] = deque()
    kept_bytes = 0
    dropped_for_cap = 0
    records_in_window = 0
    needles = tuple(name.encode() for name in naming) if naming else None

    def settle(record: list[bytes] | None, in_window: bool) -> None:
        nonlocal kept_bytes, dropped_for_cap, records_in_window
        if not record or not in_window:
            return
        text = b"".join(record)
        if needles is not None and not any(needle in text for needle in needles):
            return
        records_in_window += 1
        if len(text) > cap_bytes:
            # 一条记录自己就比上限大（巨大的 traceback / 一整段请求体）：留它的尾巴。
            text = text[-cap_bytes:]
        kept.append(text)
        kept_bytes += len(text)
        while kept_bytes > cap_bytes and len(kept) > 1:
            kept_bytes -= len(kept.popleft())
            dropped_for_cap += 1

    record: list[bytes] | None = None
    in_window = False
    with path.open("rb") as handle:
        handle.seek(start)
        if start:
            handle.readline()  # 从中间开始读，第一行多半是半截
        for line in handle:
            match = _LOG_TIMESTAMP.match(line)
            if match:
                settle(record, in_window)
                record = [line]
                try:
                    moment = datetime.strptime(match.group(1).decode(), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    moment = since_local
                in_window = moment >= since_local
            elif record is not None:
                record.append(line)
    settle(record, in_window)
    facts = {
        "window_start_local": since_local.isoformat(sep=" ", timespec="seconds"),
        "records": records_in_window,
        "dropped_oldest_for_size": dropped_for_cap,
        "scanned_from_byte": start,
        "file_bytes": size,
        "filter": "records naming this session or project" if naming else "all records in window",
    }
    return b"".join(kept), facts


def _collect_logs(
    archive: zipfile.ZipFile,
    ledger: _Ledger,
    redact: Redactor,
    logs_dir: Path,
    *,
    since_local: datetime,
    naming: tuple[str, ...] | None,
) -> dict:
    facts: dict = {"dir": str(logs_dir)}
    backend_log = logs_dir / BACKEND_LOG_NAME
    if backend_log.is_file():
        try:
            data, slice_facts = slice_backend_log(
                backend_log, since_local=since_local, naming=naming
            )
        except OSError as exc:
            ledger.skipped.append({"path": f"logs/{BACKEND_LOG_NAME}", "reason": f"读不到：{exc}"})
        else:
            facts[BACKEND_LOG_NAME] = slice_facts
            _add(
                archive, ledger, redact, f"logs/{BACKEND_LOG_NAME}", data,
                original_bytes=slice_facts["file_bytes"],
                modified=datetime.fromtimestamp(backend_log.stat().st_mtime),
                note="按时间窗截出的一段",
            )
    else:
        ledger.skipped.append({
            "path": f"logs/{BACKEND_LOG_NAME}",
            "reason": "这台机器的数据根里没有后端日志（组织服务器的日志通常在系统日志里）",
        })
    if naming is not None:
        # 组织服务器上别的日志是整台机器的，谁的都有。不收。
        return facts
    if not logs_dir.is_dir():
        return facts
    for path in sorted(logs_dir.iterdir()):
        if path.name == BACKEND_LOG_NAME:
            continue
        arcname = f"logs/{path.name}"
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                continue
            data = _read_tail(path, info.st_size, OTHER_LOG_TAIL_BYTES)
        except OSError as exc:
            ledger.skipped.append({"path": arcname, "reason": f"读不到：{exc}"})
            continue
        _add(
            archive, ledger, redact, arcname, data,
            original_bytes=info.st_size,
            modified=datetime.fromtimestamp(info.st_mtime),
            note=None if len(data) == info.st_size else f"只留了最后 {len(data)} 字节",
        )
    return facts


def _as_utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _readme(session_id: str, generated: datetime) -> str:
    return (
        "ScienceMate 会话诊断包\n"
        "======================\n\n"
        f"会话：{session_id}\n"
        f"生成时间：{generated.astimezone().isoformat(sep=' ', timespec='seconds')}\n\n"
        "里面有什么：\n"
        "  runs/          这个会话的全部运行记录（调度器和它派出的每个节点），\n"
        "                 包括完整的对话、工具调用和输出。\n"
        "  logs/          这台机器的后端日志里和这个会话同一时段的部分。\n"
        "  manifest.json  版本、平台，以及每个文件收了多少、哪些没收和原因。\n\n"
        "密钥（模型服务的 key、口令、令牌、连接串、私钥等）在打包时已经抹掉（显示为 [REDACTED]）。\n"
        "对话内容和研究过程没有抹。发给别人之前，请确认你愿意分享这些内容。\n\n"
        "---\n\n"
        "ScienceMate session diagnostics\n\n"
        "runs/ holds every run record of this session (the orchestrator and each node it\n"
        "dispatched), including the full conversation and tool output. logs/ holds the\n"
        "part of this machine's backend log from the same period. Secrets (model API keys,\n"
        "passwords, tokens, connection strings, private keys) were replaced with [REDACTED];\n"
        "the research content was not. Check that you are happy\n"
        "to share it before sending it to anyone.\n"
    )


def write_diagnostics_bundle(
    *,
    project_id: str,
    session_id: str,
    session_facts: dict,
    session_created_at: datetime | None,
    runs_root: Path | None,
    logs_dir: Path,
    secrets: Iterable[str],
    shared_log: bool,
    profile_label: str,
    app_version: str | None,
    destination_dir: Path | None = None,
    now: datetime | None = None,
) -> DiagnosticsBundle:
    """把诊断包写进一个临时文件。同步、会读大量文件 —— 调用方放到线程里跑。"""
    generated = now or datetime.now(UTC)
    created = _as_utc(session_created_at) or generated
    since_local = created.astimezone().replace(tzinfo=None) - LOG_WINDOW_LEAD
    # 日志是很多人共用的（组织服务器）：只收点名了这个会话或项目的记录。
    naming = (session_id, project_id) if shared_log else None
    redact = Redactor(secrets)
    ledger = _Ledger()

    stamp = generated.astimezone().strftime("%Y%m%d-%H%M%S")
    filename = f"session-diagnostics-{session_id[:8]}-{stamp}.zip"
    handle = tempfile.NamedTemporaryFile(
        prefix="session-diagnostics-", suffix=".zip", delete=False,
        dir=str(destination_dir) if destination_dir else None,
    )
    path = Path(handle.name)
    try:
        with handle, zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            _collect_runs(archive, ledger, redact, runs_root)
            log_facts = _collect_logs(
                archive, ledger, redact, logs_dir, since_local=since_local, naming=naming
            )
            manifest = {
                "format": 1,
                "generated_at": generated.isoformat(),
                "app": {
                    "version": app_version,
                    "profile": profile_label,
                    "platform": sys.platform,
                    "os": platform.platform(),
                    "python": platform.python_version(),
                },
                "session": {"id": session_id, "project_id": project_id, **session_facts},
                "sources": {
                    "runs_root": str(runs_root) if runs_root is not None else None,
                    "logs": log_facts,
                },
                "limits": {
                    "file_cap_bytes": FILE_CAP_BYTES,
                    "total_cap_bytes": TOTAL_CAP_BYTES,
                    "backend_log_cap_bytes": BACKEND_LOG_CAP_BYTES,
                },
                "redactions": ledger.redactions,
                "included": ledger.included,
                "skipped": ledger.skipped,
            }
            # manifest 自己也过一遍抹除：会话标题、路径里理论上也可能带着 key。
            manifest_bytes, _ = redact(
                json.dumps(manifest, ensure_ascii=False, indent=2, default=str).encode()
            )
            archive.writestr(
                zipfile.ZipInfo("manifest.json", date_time=_zip_time(generated)), manifest_bytes,
                compress_type=zipfile.ZIP_DEFLATED,
            )
            archive.writestr(
                zipfile.ZipInfo("README.txt", date_time=_zip_time(generated)),
                _readme(session_id, generated).encode(),
                compress_type=zipfile.ZIP_DEFLATED,
            )
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return DiagnosticsBundle(path=path, filename=filename, manifest=manifest)


def session_runs_root(project_id: str, session_id: str) -> Path | None:
    """这个会话的 runs 根（调度器目录和每个子 run 的父目录）。没有工作树就是 None。

    与 `harness_sessions._session_runtime_dir` 同一条路：工作树位置归仓库答，
    现行/存量两个位置归 `session_runtime_paths.resolve_existing` 答。
    """
    from app.services.project_repository import get_project_repository
    from app.services.session_runtime_paths import resolve_existing

    try:
        worktree = get_project_repository().session_path(project_id, session_id)
    except Exception:  # 仓库还没初始化：这个会话在这里没有记录
        return None
    if not worktree.is_dir():
        return None
    return resolve_existing(worktree, "runs")


def build_for_session(
    *,
    project_id: str,
    session_id: str,
    session_facts: dict,
    session_created_at: datetime | None,
    secrets: Iterable[str],
) -> DiagnosticsBundle:
    """按这台后端的配置找齐来源，写出诊断包。同步 —— 放到线程里调。"""
    from app.assembly import profile_name, the_backend_log_is_shared
    from app.config import the_data_root
    from app.version import installed_version

    try:
        version = installed_version()
    except Exception:  # 版本号读不到不该让人拿不到诊断包
        version = None
    return write_diagnostics_bundle(
        project_id=project_id,
        session_id=session_id,
        session_facts=session_facts,
        session_created_at=session_created_at,
        runs_root=session_runs_root(project_id, session_id),
        logs_dir=the_data_root() / LOGS_DIRNAME,
        secrets=secrets,
        shared_log=the_backend_log_is_shared(),
        profile_label=profile_name(),
        app_version=version,
    )

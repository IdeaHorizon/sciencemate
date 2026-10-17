"""升级彩排：拿**上一版真跑过课题的数据根**装新版、起来、发一轮，证明旧数据读得回、权限没变
（RFC_WORKER_START_IS_NOT_A_GATE §3.10，窄单 H）。

三个子命令，一条路：

    capture  --data-root <上一版的数据根> --out <夹具目录> --version 0.5.6
             把那份数据根抄成夹具：脱敏（模型 key、凭据主密钥）、不带载荷与缓存、大文件不带，
             写 fixture-manifest.json（版本、项目 / 会话 / run 的 id、KB 条数、有没有权限快照）。
    restore  --fixture <夹具目录或 .tgz> --to <新版要用的数据根>
             把夹具抄到新版要起的位置（调用方再以 PLATFORM_DATA_ROOT 指过去起壳）。
    check    --url http://127.0.0.1:<端口> --fixture <夹具目录> --data-root <它正跑在哪个数据根上>
             --receipt windows-upgrade-rehearsal.json --for-installer <Setup.exe>
             对**跑着的新版**：配一个假模型、往夹具里那个旧会话发一轮、等回复；然后核四件：
             环境条幅没有降级 / 致命 / 「升级前后权限变了」；上一版的 run 在 runs 接口里；
             上一版的 KB 记录读得回；权限快照与夹具里那份零差异。全过才写收据 ——
             发布那头（publish_release.refuse_a_windows_build_that_never_rehearsed_an_upgrade）认它。

为什么是夹具不是「拿打包机的数据根直接跑」：夹具入库之后每次发布都在同一份旧数据上彩排，红了能
复现；打包机的数据根每天在变，红了说不清是哪一天的什么。为什么只核这四件：它们正是 lujy 10-15
现场坏掉的四样 —— 读不到旧 marker、旧 run 目录拒绝访问、旧 .git 读不了、权限莫名其妙变了。

发消息那半借 `personal_smoke.py` 的函数：同一条路、同一份判据，不另抄一份。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MANIFEST = "fixture-manifest.json"
RECEIPT_NAME = "windows-upgrade-rehearsal.json"
#: 夹具里带哪些顶层项。载荷 / 更新缓存 / WebView2 用户数据 / socket 不是数据；凭据主密钥是秘密。
CARRIED = ("db.sqlite", "harness-state", "project-repositories", "project-worktrees", "projects", "org", "user", "shell.json")
CARRIED_LOGS = ("permissions-snapshot.json", "permissions.jsonl")
#: 单个文件超过这个就不带（记进 manifest.skipped_large）：夹具是结构，不是产物仓库。
FILE_CAP = 4 * 1024 * 1024
TOTAL_CAP = 96 * 1024 * 1024


def _smoke():
    spec = importlib.util.spec_from_file_location("afs_personal_smoke", HERE / "personal_smoke.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ── capture ──────────────────────────────────────────────────────────────────


def _count_jsonl(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
    except OSError:
        return 0


def _inventory(db: Path) -> dict[str, Any]:
    """从上一版的 SQLite 里读出项目 / 会话 / run 的 id —— 这些是 check 要核的「旧东西」。"""
    out: dict[str, Any] = {"projects": [], "sessions": [], "runs": []}
    if not db.is_file():
        return out
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        if "projects" in tables:
            out["projects"] = [{"id": str(r[0]), "name": r[1]} for r in con.execute("select id, name from projects")]
        if "sessions" in tables:
            out["sessions"] = [{"id": str(r[0]), "project_id": str(r[1]), "title": r[2]}
                               for r in con.execute("select session_id, project_id, title from sessions")]
        if "runs" in tables:
            out["runs"] = [{"id": str(r[0]), "session_id": str(r[1]), "project_id": str(r[2]), "status": r[3]}
                           for r in con.execute("select id, session_id, project_id, status from runs")]
    finally:
        con.close()
    return out


def _kb_counts(fixture: Path, project_ids: list[str]) -> dict[str, int]:
    """每个项目的 KB claims 条数（项目层 `harness-state/projects/<id>/kb_claims.jsonl`；
    0.5.3 之前按人分存在 users/<uid>/projects/<id>/，两处都数、取大）。"""
    counts: dict[str, int] = {}
    state = fixture / "harness-state"
    for pid in project_ids:
        best = 0
        for candidate in [state / "projects" / pid / "kb_claims.jsonl",
                          *state.glob(f"users/*/projects/{pid}/kb_claims.jsonl")]:
            best = max(best, _count_jsonl(candidate))
        counts[pid] = best
    return counts


def _scrub_sqlite(db: Path) -> list[str]:
    """抹掉上一版库里的秘密：模型 key（加密的也抹 —— 夹具会入库）。表不在就跳过。"""
    done: list[str] = []
    con = sqlite3.connect(db)
    try:
        tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        if "model_backend_configs" in tables:
            columns = {row[1] for row in con.execute("pragma table_info(model_backend_configs)")}
            if "encrypted_api_key" in columns:
                con.execute("update model_backend_configs set encrypted_api_key = NULL")
                done.append("model_backend_configs.encrypted_api_key")
        # 名字像秘密的列一律抹：password / secret / api_key / *_token / token_hash / secret_ref。
        # `*_tokens`（prompt_tokens 这种计数）和 context_window_tokens 不是秘密，不碰。NOT NULL 的列抹不了就
        # 跳过并记下来（夹具里留着它比抄坏一张表好；入库前的 _grep_secrets 还会再扫一遍正文）。
        for table in sorted(tables):
            # (cid, name, type, notnull, dflt, pk)。只抹文本 / 二进制列：布尔与整数列（must_change_password、
            # *_tokens）不是秘密，写占位串还会把类型写坏。
            typed = {row[1]: (row[2] or "").upper() for row in con.execute(f"pragma table_info({table})")}
            for column in sorted(typed):
                if f"{table}.{column}" in done or not _looks_like_a_secret_column(column):
                    continue
                if typed[column] and not any(kind in typed[column] for kind in ("CHAR", "TEXT", "CLOB", "BLOB")):
                    continue
                try:
                    con.execute(f"update {table} set {column} = NULL")
                except sqlite3.DatabaseError:
                    # NOT NULL 的列（users.hashed_password 这种）：写一个一眼看得出的占位串，原值不留。
                    try:
                        con.execute(f"update {table} set {column} = 'scrubbed-for-fixture'")
                    except sqlite3.DatabaseError as exc:
                        done.append(f"{table}.{column} (kept: {exc})")
                        continue
                    done.append(f"{table}.{column} (placeholder)")
                    continue
                done.append(f"{table}.{column}")
        con.commit()
        con.execute("vacuum")
    finally:
        con.close()
    return done


def _looks_like_a_secret_column(column: str) -> bool:
    lowered = column.lower()
    if lowered.endswith("_tokens") or lowered.startswith("context_window"):
        return False
    if any(word in lowered for word in ("password", "secret", "api_key", "token_hash", "private_key")):
        return True
    return lowered == "token" or lowered.endswith("_token")


def capture(data_root: Path, out: Path, *, version: str) -> dict[str, Any]:
    if out.exists():
        raise SystemExit(f"夹具目录已存在：{out}（换个名字，夹具不覆盖）")
    out.mkdir(parents=True)
    skipped_large: list[str] = []
    total = 0

    def copy_tree(src: Path, dst: Path) -> None:
        nonlocal total
        for directory, names, files in os.walk(src, followlinks=False):
            rel = Path(directory).relative_to(src)
            (dst / rel).mkdir(parents=True, exist_ok=True)
            for name in files:
                source = Path(directory) / name
                try:
                    info = source.lstat()
                except OSError:
                    continue
                if source.is_symlink():
                    continue
                if info.st_size > FILE_CAP:
                    skipped_large.append(str(source.relative_to(data_root)))
                    continue
                if total + info.st_size > TOTAL_CAP:
                    raise SystemExit(f"夹具超过 {TOTAL_CAP // (1 << 20)} MB 上限：先清掉大产物再抄")
                shutil.copy2(source, dst / rel / name)
                total += info.st_size

    for name in CARRIED:
        source = data_root / name
        if source.is_dir():
            copy_tree(source, out / name)
        elif source.is_file():
            shutil.copy2(source, out / name)
            total += source.stat().st_size
    (out / "logs").mkdir(exist_ok=True)
    for name in CARRIED_LOGS:
        source = data_root / "logs" / name
        if source.is_file():
            shutil.copy2(source, out / "logs" / name)
    scrubbed = _scrub_sqlite(out / "db.sqlite") if (out / "db.sqlite").is_file() else []
    inventory = _inventory(out / "db.sqlite")
    manifest = {
        "schema_version": 1,
        "previous_version": version,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": sys.platform,
        "host": socket.gethostname(),
        "source_data_root": str(data_root),
        "bytes": total,
        "skipped_large": skipped_large,
        "scrubbed": scrubbed,
        "has_permissions_snapshot": (out / "logs" / "permissions-snapshot.json").is_file(),
        **inventory,
        "kb_claims": _kb_counts(out, [p["id"] for p in inventory["projects"]]),
    }
    (out / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    leaked = _grep_secrets(out)
    if leaked:
        raise SystemExit("夹具里还有像密钥的东西，不能入库：\n  " + "\n  ".join(leaked))
    return manifest


def _grep_secrets(root: Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix in {".sqlite", ".db", ".npy"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for needle in ("sk-", "Bearer ", "BEGIN RSA PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY"):
            if needle in text:
                hits.append(f"{path.relative_to(root)}: {needle!r}")
                break
    return hits


# ── restore ──────────────────────────────────────────────────────────────────


def long_path(path: Path | str) -> str:
    r"""Windows 上给绝对路径加 `\\?\` 前缀：夹具里 `project-worktrees/<12>/<12>/.research/runtime/runs/orchestrator__<36>__session__<36>/…`
    一层层下来轻松过 260 字符（PC 上第一次彩排就是 WinError 206 死在这）。os / tarfile / shutil 都认这个前缀。"""
    text = str(Path(path).resolve()) if not str(path).startswith("\\\\?\\") else str(path)
    if sys.platform == "win32" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def unpack_if_tarball(fixture: Path) -> Path:
    """夹具入库是一个 .tgz（里面有嵌套的 .git，目录形态进不了仓库）；给了压缩包就解到临时目录，
    返回里面那个夹具目录（含 fixture-manifest.json 的那一层）。给了目录原样返回。

    临时目录放在 `%LOCALAPPDATA%`（Windows）下、名字尽量短：里面的路径本来就深。"""
    if fixture.is_dir():
        return fixture
    if not fixture.is_file():
        raise SystemExit(f"没有这个夹具：{fixture}")
    import tarfile
    import tempfile

    scratch = Path(tempfile.mkdtemp(prefix="ufx", dir=os.environ.get("LOCALAPPDATA") or None))
    with tarfile.open(fixture, "r:*") as archive:
        members = archive.getmembers()
        if sys.platform == "win32":
            # `\\?\` 前缀关掉了 Win32 的路径规范化：正斜杠不再被当成分隔符（WinError 123）。
            # tar 成员名是 POSIX 的，解之前换成反斜杠。
            for member in members:
                member.name = member.name.replace("/", "\\")
        archive.extractall(long_path(scratch), members=members, filter="data")
    candidates = [p.parent for p in scratch.rglob(MANIFEST)]
    if len(candidates) != 1:
        raise SystemExit(f"压缩包里应恰有一份 {MANIFEST}，找到 {len(candidates)} 份：{fixture}")
    return candidates[0]


def restore(fixture: Path, to: Path, *, python: str | None = None, tree: Path | None = None) -> dict[str, Any]:
    """把夹具抄到 `to`，然后**搬根**：库里记的 `sessions.git_worktree_path` 和 git worktree 的双向绝对路径
    都还指着上一版当时的数据根，不改写新版会拒绝启动（`main._refuse_to_serve_where_the_data_is_not`，
    拦得对）。换根的唯一合法出口是 `platform/backend/scripts/relocate_project_data.py`，这里调它
    （`--copied --apply`：数据已经在 `to`，只修指针）—— 不另抄一份搬根逻辑。

    `python` = 装好的那个应用的解释器（它装着 `app`）；没给就只抄不搬根，并大声说一句。"""
    fixture = unpack_if_tarball(fixture)
    manifest = json.loads((fixture / MANIFEST).read_text(encoding="utf-8"))
    if to.exists() and any(to.iterdir()):
        raise SystemExit(f"目标不是空的：{to}（彩排要在干净位置上装旧数据）")
    to.mkdir(parents=True, exist_ok=True)
    for item in fixture.iterdir():
        if item.name == MANIFEST:
            continue
        if item.is_dir():
            shutil.copytree(long_path(item), long_path(to / item.name), symlinks=False)
        else:
            shutil.copy2(long_path(item), long_path(to / item.name))
    if python is None:
        print("⚠️ 没给 --python：只抄了数据，没有搬根。新版会因为库里记的旧根拒绝启动 —— 彩排要给装好的解释器。")
        return manifest
    relocate = (tree or HERE.parents[1]) / "platform" / "backend" / "scripts" / "relocate_project_data.py"
    env = {**os.environ, "PLATFORM_DATA_ROOT": str(to), "HARNESS_FRAMEWORK_HOME": str(to), "PYTHONUTF8": "1"}
    import subprocess

    done = subprocess.run([python, str(relocate), "--to", str(to), "--apply", "--copied"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=600)
    print((done.stdout or "").strip())
    if done.returncode != 0:
        raise SystemExit(f"搬根没成（rc={done.returncode}）：{(done.stderr or '').strip()[-2000:]}")
    return manifest


# ── check ────────────────────────────────────────────────────────────────────


def _items(payload: Any) -> list[Any]:
    """接口回的要么是列表，要么是 {"items": [...]}（runs 接口是后者）—— 两种都认，别猜。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "runs", "claims", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _compare_snapshots(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """与 app.services.permissions_snapshot.compare 同一条规则（这里不能 import 后端）。"""
    diffs: list[dict[str, Any]] = []
    before, after = previous.get("entries") or {}, current.get("entries") or {}
    for key in sorted(set(before) & set(after)):
        for field in ("owner", "mode", "acl_sha256", "label", "error"):
            if before[key].get(field) != after[key].get(field):
                diffs.append({"path": key, "field": field, "before": before[key].get(field), "after": after[key].get(field)})
    return diffs


def check(url: str, fixture: Path, data_root: Path, *, receipt: Path | None, installer: Path | None,
          timeout: float = 600.0) -> int:
    smoke = _smoke()
    fixture = unpack_if_tarball(fixture)
    manifest = json.loads((fixture / MANIFEST).read_text(encoding="utf-8"))
    runs_by_session: dict[str, list[str]] = {}
    for run in manifest.get("runs", []):
        runs_by_session.setdefault(run["session_id"], []).append(run["id"])
    sessions = [s for s in manifest.get("sessions", []) if runs_by_session.get(s["id"])]
    if not sessions:
        print("REHEARSAL FAILED: 夹具里没有跑过 run 的会话 —— 这不是一份「真跑过课题」的数据根", file=sys.stderr)
        return 1
    session = sessions[0]
    project_id, session_id = session["project_id"], session["id"]
    started = time.monotonic()
    checks: dict[str, Any] = {}
    base = url.rstrip("/")
    api = f"{base}/api/v1"
    model_server, model_url = smoke.start_fake_model()
    try:
        smoke.wait_until_ready(base, None, None, started + timeout)
        status, text = smoke.request("POST", f"{api}/settings/model-backends", body={
            "provider": "openai_compatible", "display_name": "rehearsal", "model": "rehearsal",
            "base_url": model_url, "api_key": "rehearsal-key",
        })
        backend_id = smoke.expect(status, text, (201,), "配模型")["id"]
        status, text = smoke.request("POST", f"{api}/settings/model-backends/{backend_id}/default")
        smoke.expect(status, text, (200, 201, 204), "设成默认模型")
        # 旧会话绑着上一版验收时的那个假模型端口（早就没了）：把它切到这次的假模型 —— 用户换模型走的也是这条路。
        status, text = smoke.request("PATCH", f"{api}/projects/{project_id}/sessions/{session_id}",
                                     body={"model_backend_id": backend_id})
        smoke.expect(status, text, (200,), "把上一版的会话切到这次的模型")

        # 1. 往**旧会话**发一轮：worker 要在旧的家、旧的 state 目录上起来
        status, text = smoke.request(
            "POST", f"{api}/chat/projects/{project_id}/stream",
            body={"answer": {"kind": "text", "text": "Say hello."}, "conversation_id": session_id},
            timeout=max(30.0, started + timeout - time.monotonic()), stream=True,
        )
        smoke.expect(status, text, (200,), "往上一版的会话发一条消息")
        reply = smoke.wait_for_reply(api, project_id, session_id, started + timeout)
        checks["model_called"] = bool(smoke._FakeModel.calls)
        if not checks["model_called"]:
            raise smoke.Failed("模型一次都没被调到")

        # 2. 环境条幅：没有降级、没有致命、没有「升级前后权限变了」
        status, text = smoke.request("GET", f"{api}/projects/{project_id}/sessions/{session_id}/environment")
        environment = smoke.expect(status, text, (200,), "读会话的环境状态")
        bad = [w for w in environment.get("warnings", []) if w.get("severity") in ("degraded", "fatal")]
        checks["environment_clean"] = not bad
        capabilities = environment.get("capabilities") or {}
        checks["capabilities_all_true"] = all(v is True for v in capabilities.values()) if capabilities else None
        checks["environment"] = {"warnings": [(w.get("code"), w.get("severity"), w.get("path")) for w in environment.get("warnings", [])],
                                 "capabilities": capabilities}
        if bad:
            raise smoke.Failed("新版在旧数据根上起来是降级的：" + "、".join(f"{w.get('code')}@{w.get('path')}" for w in bad))

        # 3. 上一版的 run 在界面读的那个接口里
        status, text = smoke.request("GET", f"{api}/runs?sessionId={session_id}&limit=200")
        if status != 200:
            raise smoke.Failed(f"runs 接口 {status}：{text[:200]}")
        listed = {str(r.get("id")) for r in _items(json.loads(text)) if isinstance(r, dict)}
        missing_runs = sorted(set(runs_by_session[session_id]) - listed)
        checks["previous_runs_visible"] = not missing_runs
        if missing_runs:
            raise smoke.Failed(f"上一版的 run 不见了：{missing_runs}")

        # 4. 上一版的 KB 读得回
        claims_by_project = manifest.get("kb_claims") or {}
        expected_claims = int(claims_by_project.get(project_id)
                              or claims_by_project.get(project_id.replace("-", "")) or 0)
        status, text = smoke.request("GET", f"{api}/kb/claims?project_id={project_id}&limit=200")
        got_claims = len(_items(json.loads(text))) if status == 200 else -1
        checks["kb_readable"] = status == 200 and got_claims >= min(expected_claims, 200)
        checks["kb_claims"] = {"expected_at_least": expected_claims, "got": got_claims}
        if not checks["kb_readable"]:
            raise smoke.Failed(f"上一版的 KB 读不回：期望 ≥{expected_claims}，读到 {got_claims}（HTTP {status}）")

        # 5. 权限快照零差异（夹具里有上一版的快照才比；新版启动时写了自己的那份）
        previous = fixture / "logs" / "permissions-snapshot.json"
        current = data_root / "logs" / "permissions-snapshot.json"
        if previous.is_file() and current.is_file():
            diffs = _compare_snapshots(json.loads(previous.read_text(encoding="utf-8")),
                                       json.loads(current.read_text(encoding="utf-8")))
            checks["permissions_unchanged"] = not diffs
            checks["permission_diffs"] = diffs[:20]
            if diffs:
                raise smoke.Failed("升级前后数据根的权限变了：" + "；".join(f"{d['path']}.{d['field']} {d['before']}→{d['after']}" for d in diffs[:5]))
        else:
            checks["permissions_unchanged"] = None   # 上一版还没有快照（0.5.6 及之前）：如实说没比
            checks["permission_diffs"] = None
        print(f"REHEARSAL OK: {manifest.get('previous_version')} → 新版，旧会话一轮回复 {reply[:60]!r}；"
              f"run {len(runs_by_session[session_id])} 条都在；KB {got_claims} 条；权限 "
              f"{'零差异' if checks['permissions_unchanged'] else '没比（上一版没有快照）'}")
        if receipt is not None:
            _write_receipt(receipt, url, installer, manifest, checks, time.monotonic() - started)
        return 0
    except smoke.Failed as exc:
        print(f"REHEARSAL FAILED: {exc}", file=sys.stderr)
        print(json.dumps(checks, ensure_ascii=False, indent=1), file=sys.stderr)
        return 1
    finally:
        model_server.shutdown()


def _write_receipt(path: Path, against: str, installer: Path | None, manifest: dict[str, Any],
                   checks: dict[str, Any], seconds: float) -> None:
    body: dict[str, Any] = {
        "result": "REHEARSAL OK",
        "against": against,
        "previous_version": manifest.get("previous_version"),
        "fixture_captured_at": manifest.get("captured_at"),
        "checks": {k: v for k, v in checks.items() if k in (
            "model_called", "environment_clean", "capabilities_all_true", "previous_runs_visible",
            "kb_readable", "permissions_unchanged")},
        "platform": sys.platform,
        "host": socket.gethostname(),
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seconds": round(seconds, 1),
    }
    if installer is not None:
        digest = hashlib.sha256()
        with installer.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        body["installer"] = installer.name
        body["sha256"] = digest.hexdigest()
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  彩排收据写在 {path}")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Windows 控制台默认 cp1252：这里的输出有中文，别让一句打印把整步弄成 UnicodeEncodeError。
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="把上一版的数据根抄成夹具")
    cap.add_argument("--data-root", required=True, type=Path)
    cap.add_argument("--out", required=True, type=Path)
    cap.add_argument("--version", required=True, help="上一版的版本号，如 0.5.6")
    res = sub.add_parser("restore", help="把夹具抄到新版要起的数据根位置")
    res.add_argument("--fixture", required=True, type=Path)
    res.add_argument("--to", required=True, type=Path)
    res.add_argument("--python", default=None, help="装好的应用的解释器（Resources/python/python.exe）：用它跑 relocate_project_data.py 搬根")
    res.add_argument("--tree", default=None, type=Path, help="源码树（找 platform/backend/scripts/relocate_project_data.py）；默认本脚本所在的树")
    chk = sub.add_parser("check", help="对跑着的新版核四件事，全过写收据")
    chk.add_argument("--url", required=True)
    chk.add_argument("--fixture", required=True, type=Path)
    chk.add_argument("--data-root", required=True, type=Path, help="那个实例正跑在哪个数据根上（restore --to 的位置）")
    chk.add_argument("--receipt", type=Path, default=None)
    chk.add_argument("--for-installer", type=Path, default=None)
    chk.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    if args.command == "capture":
        manifest = capture(args.data_root, args.out, version=args.version)
        print(json.dumps({k: manifest[k] for k in ("previous_version", "bytes", "projects", "sessions", "kb_claims", "scrubbed", "has_permissions_snapshot")},
                         ensure_ascii=False, indent=1))
        return 0
    if args.command == "restore":
        manifest = restore(args.fixture, args.to, python=args.python, tree=args.tree)
        print(f"夹具 {manifest.get('previous_version')} 已抄到 {args.to}")
        return 0
    return check(args.url, args.fixture, args.data_root, receipt=args.receipt,
                 installer=args.for_installer, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())

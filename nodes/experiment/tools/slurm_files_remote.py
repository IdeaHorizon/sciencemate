"""Fixed stdlib file operations sent over SSH; no daemon or installed service.

All paths are POSIX coordinates on the selected host. Descendants are opened
relative to directory descriptors with O_NOFOLLOW, never by archive extraction.
The caller owns authorization, job identity and the local destination.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone


def clean(path):
    if (not isinstance(path, str) or not path.startswith("/")
            or path.startswith("//") or "\\" in path
            or any(ord(c) < 32 for c in path)):
        raise ValueError("expected an absolute POSIX path")
    return os.path.normpath(path)


@contextmanager
def parent_fd(path):
    """Pin every ancestor. A changed link must never redirect a file read/write."""
    path = clean(path)
    parts = path.split("/")[1:]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1] or "."
    finally:
        os.close(fd)


def kind(mode):
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def identity(path, hash_cap=256 * 1024 * 1024):
    path = clean(path)
    row = {"path": path, "kind": "missing"}
    try:
        with parent_fd(path) as (parent, name):
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            row.update(kind=kind(st.st_mode), st_dev=st.st_dev, st_ino=st.st_ino,
                       st_nlink=st.st_nlink, size_bytes=st.st_size,
                       mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns)
            if row["kind"] == "file":
                if st.st_size > hash_cap:
                    row["sha256_skipped"] = "size_over_cap"
                else:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    with os.fdopen(fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                            raise OSError("file changed during observation")
                        digest = hashlib.sha256()
                        remaining = st.st_size
                        while remaining:
                            block = stream.read(min(1024 * 1024, remaining))
                            if not block:
                                raise OSError("file truncated during hashing")
                            digest.update(block)
                            remaining -= len(block)
                        after = os.fstat(stream.fileno())
                        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (st.st_size, st.st_mtime_ns, st.st_ctime_ns):
                            raise OSError("file changed during hashing")
                    row["sha256"] = digest.hexdigest()
            elif row["kind"] == "symlink":
                row["link_target"] = os.readlink(name, dir_fd=parent)
    except FileNotFoundError:
        row = {"path": path, "kind": "missing"}
    except OSError as exc:
        row.update(kind="error", error=f"{type(exc).__name__}: {exc}")
    return row


def health(path):
    ident = identity(path, hash_cap=0)
    if ident["kind"] in {"missing", "error"}:
        return {**ident, "exists": False}
    result = {**ident, "exists": True, "is_file": ident["kind"] == "file",
              "mtime_epoch_s": ident["mtime_ns"] / 1e9,
              "mtime": datetime.fromtimestamp(ident["mtime_ns"] / 1e9, timezone.utc).isoformat()}
    if ident["kind"] == "directory":
        with parent_fd(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                with os.scandir(fd) as it:
                    names = []
                    for entry in it:
                        names.append(entry.name)
                        if len(names) > 500:
                            break
                result["entries"] = [identity(path.rstrip("/") + "/" + n, hash_cap=0) for n in sorted(names[:500])]
                result["entries_truncated"] = len(names) > 500
            finally:
                os.close(fd)
    return result


def snapshot(roots):
    started = time.monotonic()
    entries, coverage = {}, []
    truncated = False
    for root in roots:
        root = clean(root)
        covered = {"root": root, "status": "complete"}
        coverage.append(covered)
        pending = [root]
        while pending:
            if len(entries) >= 500 or time.monotonic() - started > 8:
                covered["status"] = "truncated"
                truncated = True
                break
            path = pending.pop()
            ident = identity(path, hash_cap=8 * 1024 * 1024)
            if ident["kind"] == "directory":
                try:
                    with parent_fd(path) as (parent, name):
                        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            with os.scandir(fd) as it:
                                for entry in it:
                                    if len(pending) + len(entries) >= 500:
                                        covered["status"] = "truncated"
                                        truncated = True
                                        break
                                    if entry.name in {".git", ".ssh", ".research"}:
                                        coverage.append({"root": path.rstrip("/") + "/" + entry.name, "status": "excluded"})
                                        continue
                                    pending.append(path.rstrip("/") + "/" + entry.name)
                        finally:
                            os.close(fd)
                except OSError as exc:
                    covered.update(status="error", error=str(exc))
                    truncated = True
            else:
                mapped = dict(ident)
                mapped.update(size=ident.get("size_bytes"), inode=ident.get("st_ino"))
                if ident["kind"] == "symlink":
                    mapped["target"] = ident.get("link_target")
                entries[path] = mapped
                if ident["kind"] == "error":
                    covered["status"] = "error"
                    truncated = True
                elif ident["kind"] == "symlink":
                    coverage.append({"root": path, "status": "excluded"})
                elif path == root and ident["kind"] == "missing":
                    covered["status"] = "missing"
    return {"entries": entries, "root_coverage": coverage, "truncated": truncated,
            "scanned": len(entries), "taken_at": time.time(), "seconds": time.monotonic() - started}


def operate(action, data):
    expected = data.get("expected_identity")
    if expected is not None:
        if (not expected.get("user") or not expected.get("cluster")
                or pwd.getpwuid(os.geteuid()).pw_name != expected["user"]):
            raise ValueError("remote login identity differs from the recorded job")
        config = subprocess.run(["scontrol", "show", "config"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        found = re.search(r"(?m)^\s*ClusterName\s*=\s*([^\s#]+)", config.stdout)
        if config.returncode or found is None or found.group(1) != expected["cluster"]:
            raise ValueError("remote Slurm cluster differs from the recorded job")
    if action == "check":
        result = {}
        for field in ("workdir", "output_dir"):
            path = clean(data[field])
            # The selected root may itself be a legitimate shared-storage alias.
            # Return its actual coordinate so submission binds that same target.
            resolved = os.path.realpath(path)
            with parent_fd(resolved) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(fd)
            result[field] = resolved
        return {"ok": True, **result, "observed_at": time.time()}
    if action == "prepare":
        boot = clean(data["bootstrap"])
        with parent_fd(boot) as (parent, name):
            os.mkdir(name, mode=0o700, dir_fd=parent)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                os.mkdir("inputs", mode=0o700, dir_fd=fd)
            finally:
                os.close(fd)
        return {"ok": True, "observed_at": time.time()}
    if action == "upload":
        path = clean(data["path"])
        size, digest = 0, hashlib.sha256()
        with parent_fd(path) as (parent, name):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    while True:
                        block = sys.stdin.buffer.read(min(1024 * 1024, data["size"] - size + 1))
                        if not block:
                            break
                        size += len(block)
                        if size > data["size"]:
                            raise ValueError("upload exceeds approved file size")
                        digest.update(block)
                        handle.write(block)
                    if size != data["size"] or digest.hexdigest() != data["sha256"]:
                        raise ValueError("upload content identity mismatch")
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.fchmod(handle.fileno(), int(data.get("mode", "0644"), 8) & 0o755)
            except BaseException:
                os.unlink(name, dir_fd=parent)
                raise
        return {"ok": True, "size": size, "sha256": digest.hexdigest()}
    if action == "snapshot":
        return snapshot(data["roots"])
    if action == "health":
        return health(clean(data["path"]))
    if action == "identity":
        return identity(clean(data["path"]), hash_cap=data.get("hash_cap", 256 * 1024 * 1024))
    if action in {"tail", "fetch"}:
        path = clean(data["path"])
        with parent_fd(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                st = os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise ValueError("only regular files can be transferred")
                if action == "tail":
                    size = max(0, min(int(data.get("limit", 8192)), 65536))
                    stream.seek(max(0, st.st_size - size))
                    return {"text": stream.read(size).decode("utf-8", "replace")}
                expected = data["identity"]
                for key, actual in (("st_dev", st.st_dev), ("st_ino", st.st_ino),
                                    ("size_bytes", st.st_size), ("mtime_ns", st.st_mtime_ns),
                                    ("ctime_ns", st.st_ctime_ns)):
                    if expected.get(key) != actual:
                        raise ValueError("remote file changed since frozen observation")
                digest = hashlib.sha256()
                remaining = st.st_size
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("remote file truncated during transfer")
                    digest.update(block)
                    sys.stdout.buffer.write(block)
                    remaining -= len(block)
                after = os.fstat(stream.fileno())
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (st.st_size, st.st_mtime_ns, st.st_ctime_ns):
                    raise ValueError("remote file changed during transfer")
                if expected.get("sha256") and digest.hexdigest() != expected["sha256"]:
                    raise ValueError("remote file content changed since observation")
        return None
    raise ValueError("unknown file operation")


if __name__ == "__main__":
    try:
        result = operate(sys.argv[1], json.loads(sys.argv[2]))
        if result is not None:
            print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

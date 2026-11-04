"""Bounded OpenSSH transport for Experiment's existing Slurm lifecycle.

This module grants no access and owns no job state. Callers bind the target,
paths and inputs to existing approval and durable submission intent. Personal
OpenSSH configuration/agent supplies authentication; no organization keys or
caller-supplied SSH options are accepted.
"""
from __future__ import annotations

import io
import ipaddress
import math
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any, BinaryIO

MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
_TARGET_FIELDS = {"host", "user", "port", "login_shell"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Git OpenSSH on Windows truncates long remote command arguments at ~8 KiB.
# Send a length-prefixed script before the caller's binary input. os.read reads
# exactly the prefix, leaving the same descriptor positioned at payload input;
# buffered stdin would risk consuming bytes that belong to the scientific job.
_STDIN_BOOTSTRAP = """import os, sys
def exact(size):
    chunks = bytearray()
    while len(chunks) < size:
        block = os.read(0, min(size - len(chunks), 65536))
        if not block:
            raise EOFError('incomplete SSH command frame')
        chunks.extend(block)
    return bytes(chunks)
sys.stdout.buffer.write(exact(int.from_bytes(exact(8), 'big')))
"""


def normalize_target(raw: Any) -> dict[str, Any]:
    """Normalize connection coordinates, never authorization or credentials."""
    if not isinstance(raw, dict):
        raise ValueError("slurm_target must be an object with host")
    if set(raw) - _TARGET_FIELDS:
        # Unknown values may be accidentally supplied secrets: do not echo them.
        raise ValueError("unsupported slurm_target fields; use personal SSH configuration/agent for authentication")
    host = raw.get("host")
    valid_host = isinstance(host, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host))
    if isinstance(host, str) and ":" in host:
        try:
            valid_host = ipaddress.ip_address(host).version == 6
        except ValueError:
            valid_host = False
    if not valid_host or len(host) > 255:
        raise ValueError("slurm_target.host must be an SSH alias, hostname, or IP; specify user and port separately")
    result: dict[str, Any] = {"host": host}
    user = raw.get("user")
    if user is not None:
        if (not isinstance(user, str) or not user or len(user) > 128
                or user.startswith("-") or _CONTROL.search(user)
                or any(c.isspace() or c in "@/\\" for c in user)):
            raise ValueError("slurm_target.user must be a single SSH login name")
        result["user"] = user
    port = raw.get("port")
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("slurm_target.port must be an integer from 1 to 65535")
        result["port"] = port
    login = raw.get("login_shell", True)
    if not isinstance(login, bool):
        raise ValueError("slurm_target.login_shell must be a boolean")
    result["login_shell"] = login
    return result


def normalize_path(value: Any, base: str | None = None) -> str:
    """Resolve Linux coordinates lexically without consulting the local host.

    Authorization, containment and remote symlink checks belong to the caller.
    Dot segments are normalized before those checks; home/variables require
    actual remote resolution and never use the Windows environment.
    """
    if (not isinstance(value, str) or not value or _CONTROL.search(value)
            or "\\" in value or re.match(r"^[A-Za-z]:", value)
            or value.startswith(("~", "//"))
            or re.search(r"\$(?:[A-Za-z_]|\{|\()", value)):
        raise ValueError("remote path must be a Linux path without Windows drives, unexpanded home/variables, or control characters")
    if not value.startswith("/"):
        if base is None:
            raise ValueError("relative remote path requires a remote base directory")
        value = posixpath.join(normalize_path(base), value)
    return posixpath.normpath(value)


def _ssh_argv(target: dict[str, Any], remote_command: str) -> list[str]:
    executable = shutil.which("ssh")
    if executable is None:
        raise FileNotFoundError("OpenSSH client is unavailable")
    args = [executable, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
            "-o", "ForwardX11=no", "-o", "ForwardX11Trusted=no", "-o", "Tunnel=no",
            "-o", "PermitLocalCommand=no", "-o", "RequestTTY=no",
            "-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "ControlPath=none",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2"]
    if "port" in target:
        args += ["-p", str(target["port"])]
    if "user" in target:
        args += ["-l", target["user"]]
    return args + ["--", target["host"], remote_command]


def _native_argv(args: list[str]) -> list[str] | str:
    """Reuse Core's Windows/MSYS argv encoding; never introduce a shell."""
    if os.name == "nt":
        from shared.lib.shell import msys_runtime_bin
        if msys_runtime_bin(args[0]):
            try:
                from core.isolation._win32_exec import _command_line
            except ImportError as exc:
                raise RuntimeError("MSYS SSH requires the existing Core Windows argv encoder; no fallback was executed") from exc
            return _command_line(args, msys=True)
    return args


def _failure(result: dict[str, Any], code: str, error: str) -> dict[str, Any]:
    result.update(ok=False, transport_error=code, error=error, remote_effects="unknown")
    return result


def _execute(target: dict[str, Any], argv: list[str], *, timeout: float,
             stdin_file: BinaryIO | None, stdout_file: BinaryIO | None,
             max_output_bytes: int, expected_identity: dict[str, str] | None) -> dict[str, Any]:
    target = normalize_target(target)
    if (not isinstance(argv, (list, tuple)) or not argv
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
            or not argv[0]):
        raise ValueError("remote argv must contain a nonempty executable and NUL-free strings")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("SSH timeout must be a positive finite number")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be a positive integer")
    if expected_identity is not None:
        if (not isinstance(expected_identity, dict) or set(expected_identity) != {"user", "cluster"}
                or any(not isinstance(value, str) or not value or _CONTROL.search(value)
                       or any(char.isspace() for char in value) for value in expected_identity.values())):
            raise ValueError("expected_identity requires the recorded remote user and cluster")
    token = uuid.uuid4().hex
    start = ("\x00HARNESS_SSH_OUTPUT_" + token + "\x00").encode("ascii")
    done = "HARNESS_SSH_DONE_" + token
    identity_refused = "HARNESS_SSH_IDENTITY_REFUSED_" + token
    # Login profiles may print banners or exit 0 before this body. A stdout
    # delimiter strips banners even for binary transfer; a stderr footer
    # attests that the requested argv returned. Neither is a job identity.
    payload = shlex.join(argv) + "\n_harness_ssh_rc=$?\n"
    if expected_identity is not None:
        # Probe and payload share one SSH connection and inherited environment.
        # A separate preflight connection cannot bind later file/scheduler I/O.
        payload = ("_harness_identity_rc=0\n"
                   "_harness_actual_user=$(id -un) || _harness_identity_rc=125\n"
                   "_harness_config=$(scontrol show config) || _harness_identity_rc=125\n"
                   "_harness_actual_cluster=$(printf '%s\\n' \"$_harness_config\" | awk '$1 == \"ClusterName\" && $2 == \"=\" {print $3}')\n"
                   "if [ \"$_harness_identity_rc\" != 0 ] || [ \"$_harness_actual_user\" != "
                   + shlex.quote(expected_identity["user"]) + " ] || [ \"$_harness_actual_cluster\" != "
                   + shlex.quote(expected_identity["cluster"]) + " ]; then\n"
                   + "  printf '\\n" + identity_refused + "\\n' >&2\n  _harness_ssh_rc=125\nelse\n"
                   + payload + "fi\n")
    body = ("set +e\nprintf '\\000HARNESS_SSH_OUTPUT_" + token + "\\000'\n"
            + payload + "printf '\\n" + done
            + ":%s\\n' \"$_harness_ssh_rc\" >&2\nexit \"$_harness_ssh_rc\"\n")
    loader = shlex.join(["python3", "-c", _STDIN_BOOTSTRAP])
    remote = shlex.join(["bash", "-lc" if target["login_shell"] else "-c",
                         'eval "$(' + loader + ')"'])
    result: dict[str, Any] = {"ok": False, "returncode": None, "stdout": "", "stderr": ""}
    try:
        args = _ssh_argv(target, remote)
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as framed:
            encoded = body.encode("utf-8")
            framed.write(len(encoded).to_bytes(8, "big"))
            framed.write(encoded)
            if stdin_file is not None:
                shutil.copyfileobj(stdin_file, framed, length=1024 * 1024)
            framed.seek(0)
            proc = subprocess.Popen(_native_argv(args), stdin=framed,
                                    stdout=out, stderr=err, shell=False,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + timeout
            stopped: str | None = None
            try:
                while proc.poll() is None:
                    if time.monotonic() >= deadline:
                        stopped = "timeout"
                        break
                    if (os.fstat(out.fileno()).st_size > max_output_bytes + MAX_OUTPUT_BYTES
                            or os.fstat(err.fileno()).st_size > MAX_OUTPUT_BYTES):
                        stopped = "output_limit"
                        break
                    time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            result["returncode"] = None if stopped else proc.returncode
            err_size = os.fstat(err.fileno()).st_size
            err.seek(0)
            result["stderr"] = err.read(MAX_OUTPUT_BYTES).decode("utf-8", "replace")
            if stopped:
                return _failure(result, stopped, "SSH transport stopped; remote effects remain unknown, reconcile before retrying a side effect")
            if err_size > MAX_OUTPUT_BYTES:
                return _failure(result, "output_limit", "SSH stderr exceeded 2 MiB")
            match = re.search(r"\n" + done + r":([0-9]+)\r?\n$", result["stderr"])
            if match is None:
                return _failure(result, "completion_unobserved", "SSH/login shell ended without observing the requested command's completion")
            command_rc = int(match.group(1))
            result["stderr"] = result["stderr"][:match.start()]
            if command_rc != result["returncode"]:
                return _failure(result, "completion_mismatch", "SSH exit status disagrees with the remote command witness")
            if identity_refused in result["stderr"].splitlines():
                result["stderr"] = result["stderr"].replace(identity_refused, "Remote user/cluster could not be matched to the recorded job")
                _failure(result, "target_identity_mismatch", "Remote identity differed or was unavailable; the requested command was not sent")
                result.update(remote_effects="not_sent", command_completed=False, payload_spawned=False)
                return result
            out.seek(0)
            prefix = out.read(MAX_OUTPUT_BYTES + len(start))
            offset = prefix.find(start)
            if offset < 0:
                return _failure(result, "output_start_unobserved", "remote command output start was not observed within the bounded login output")
            offset += len(start)
            size = os.fstat(out.fileno()).st_size - offset
            if size > max_output_bytes:
                return _failure(result, "output_limit", "remote command output exceeded the requested byte limit")
            result.update(command_completed=True, stdout_bytes=size,
                          login_output_bytes=offset - len(start))
            out.seek(offset)
            if stdout_file is None:
                result["stdout"] = out.read(max_output_bytes).decode("utf-8", "replace")
            else:
                shutil.copyfileobj(out, stdout_file, length=1024 * 1024)
            result["ok"] = command_rc == 0
            return result
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return _failure(result, "ssh_unavailable" if isinstance(exc, FileNotFoundError) else "ssh_error",
                        f"{type(exc).__name__}: {exc}")


def run(target: dict[str, Any], argv: list[str], timeout: float = 20, *,
        input_bytes: bytes | None = None,
        expected_identity: dict[str, str] | None = None) -> dict[str, Any]:
    """One bounded text command; failures never retry or fall back locally."""
    if input_bytes is not None and (not isinstance(input_bytes, bytes) or len(input_bytes) > MAX_INPUT_BYTES):
        raise ValueError("SSH input must be bytes of at most 16 MiB; use run_stream for files")
    if input_bytes is None:
        return _execute(target, argv, timeout=timeout, stdin_file=None, stdout_file=None,
                        max_output_bytes=MAX_OUTPUT_BYTES, expected_identity=expected_identity)
    with tempfile.TemporaryFile() as source:
        source.write(input_bytes)
        source.seek(0)
        return _execute(target, argv, timeout=timeout, stdin_file=source, stdout_file=None,
                        max_output_bytes=MAX_OUTPUT_BYTES, expected_identity=expected_identity)


def run_stream(target: dict[str, Any], argv: list[str], *, stdin_file: BinaryIO | None = None,
               stdout_file: BinaryIO | None = None, timeout: float = 20,
               max_output_bytes: int | None = None,
               expected_identity: dict[str, str] | None = None) -> dict[str, Any]:
    """Transfer through caller-owned files without loading them in RAM.

    Input requires a file descriptor. Output is written only after transport
    completion and the byte bound are observed. Nonzero commands may still
    produce bytes useful for diagnosis. None selects the usual 2 MiB bound;
    file callers should pass their known expected size plus one explicitly.
    Limits are monitored and checked before copying, not kernel disk quotas.
    """
    if stdin_file is not None:
        try:
            stdin_file.fileno()
        except (AttributeError, OSError, io.UnsupportedOperation) as exc:
            raise ValueError("stdin_file must be an open binary file with a file descriptor") from exc
    if stdout_file is not None and not callable(getattr(stdout_file, "write", None)):
        raise ValueError("stdout_file must be an open writable binary file")
    bound = MAX_OUTPUT_BYTES if max_output_bytes is None else max_output_bytes
    if isinstance(bound, bool) or not isinstance(bound, int) or bound <= 0:
        raise ValueError("max_output_bytes must be a positive integer")
    if stdout_file is None:
        bound = min(bound, MAX_OUTPUT_BYTES)
    return _execute(target, argv, timeout=timeout, stdin_file=stdin_file, stdout_file=stdout_file,
                    max_output_bytes=bound, expected_identity=expected_identity)


def probe(target: dict[str, Any]) -> dict[str, Any]:
    """Fixed read-only probes of this login environment; they authorize nothing."""
    target = normalize_target(target)
    marker = "HARNESS_SLURM_PROBE_" + uuid.uuid4().hex
    commands = ("sbatch", "squeue", "sacct", "scancel", "scontrol", "sinfo", "python3")
    script = (f"printf '{marker}USER\\t'; id -un || exit $?\n"
              + "\n".join(f"printf '{marker}TOOL\\t{cmd}\\t'; command -v {cmd} || printf '\\n'" for cmd in commands)
              + "\nif command -v scontrol >/dev/null 2>&1; then\n"
              + f"  scontrol show config 2>/dev/null | sed -n 's/^[[:space:]]*ClusterName[[:space:]]*=[[:space:]]*/{marker}CLUSTER\\t/p'\nfi\n")
    raw = run(target, ["bash", "-c", script], timeout=20)
    result: dict[str, Any] = {"ok": False, "target": target, "user": None, "cluster": None,
                              "tools": {cmd: None for cmd in commands}, "python3": None, "raw": raw,
                              "identity_sources": {"user": "id -un", "cluster": "scontrol show config: ClusterName"}}
    if not raw["ok"]:
        result["error"] = raw.get("error") or "remote Slurm environment probe failed"
        return result
    users: list[str] = []
    clusters: list[str] = []
    for line in raw["stdout"].splitlines():
        if not line.startswith(marker):
            continue
        fields = line[len(marker):].split("\t", 2)
        if fields[0] == "USER" and len(fields) == 2 and fields[1].strip():
            users.append(fields[1].strip())
        elif fields[0] == "CLUSTER" and len(fields) == 2 and fields[1].strip():
            clusters.append(fields[1].strip())
        elif fields[0] == "TOOL" and len(fields) == 3 and fields[1] in result["tools"]:
            result["tools"][fields[1]] = fields[2].strip() or None
    if len(users) != 1 or len(set(clusters)) > 1:
        result["error"] = "remote identity/cluster observation is missing or ambiguous"
        return result
    result.update(ok=True, user=users[0], cluster=clusters[0] if clusters else None,
                  python3=result["tools"]["python3"])
    return result

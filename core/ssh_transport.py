"""OpenSSH transport for registered resources, outside the model's local shell.

The remote account is the execution boundary. No local sandbox guarantees are
claimed for that account. Host keys use OpenSSH's persistent TOFU checking.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from shared.lib import process_control


class SSHError(RuntimeError):
    pass


class SSHInputManifestConflict(SSHError):
    def __init__(self, existing_input_manifest_sha256: str | None):
        super().__init__("Submission key already belongs to a different input manifest")
        self.existing_input_manifest_sha256 = existing_input_manifest_sha256


@dataclass(frozen=True)
class SSHConnection:
    host: str
    user: str | None = None
    port: int | None = None
    jump: str | None = None

    def __post_init__(self):
        for value in (self.host, self.user, self.jump):
            if value is not None and (
                not value
                or value.startswith("-")
                or not re.fullmatch(r"[A-Za-z0-9_.:@,\[\]%-]+", value)
            ):
                raise ValueError("Invalid SSH host, user, or jump host")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")

    def public(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


class SSHTransport:
    def __init__(self, connection: SSHConnection, password: str | None = None):
        self.connection, self.password = connection, password

    def call(self, request: dict, *, timeout: float = 40) -> dict:
        import shlex

        connection = self.connection
        argv = [
            "ssh",
            "-T",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "NumberOfPasswordPrompts=1",
        ]
        if connection.port:
            argv += ["-p", str(connection.port)]
        if connection.user:
            argv += ["-l", connection.user]
        if connection.jump:
            argv += ["-J", connection.jump]
        source = Path(__file__).with_name("_ssh_agent.py.in").read_text()
        argv += ["--", connection.host, "python3 -I -c " + shlex.quote(source)]
        payload = dict(request)
        if payload.get("action") == "submit":
            payload["agent_source"] = source
        with tempfile.TemporaryDirectory(prefix="hf-ssh-") as temporary:
            env = os.environ.copy()
            if self.password is not None:
                argv[1:1] = ["-o", "PreferredAuthentications=password,keyboard-interactive"]
                # A separate environment is passed only to OpenSSH/askpass. The
                # password is never a command-line argument or a project file.
                helper = Path(temporary) / "askpass"
                helper.write_text('#!/bin/sh\nprintf "%s" "$HF_SSH_PASSWORD"\n')
                helper.chmod(0o700)
                env.update(
                    SSH_ASKPASS=str(helper),
                    SSH_ASKPASS_REQUIRE="force",
                    DISPLAY=env.get("DISPLAY") or ":0",
                    HF_SSH_PASSWORD=self.password,
                )
            else:
                argv[1:1] = ["-o", "BatchMode=yes"]
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=out,
                    stderr=err,
                    env=env,
                    **process_control.group_spawn_kwargs(),
                )
                group = process_control.Group.of(process.pid)
                try:
                    process.communicate(json.dumps(payload).encode(), timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    group.kill()
                    process.wait()
                    raise SSHError(
                        "SSH response timed out; query the same job ID before retrying submission"
                    ) from exc
                out.seek(0)
                raw = out.read(12 * 1024 * 1024 + 1)
                err.seek(0)
                diagnostic = err.read(4096).decode(errors="replace")
                if self.password:
                    from shared.lib.secret_values import redact_values, secret_values

                    diagnostic = redact_values(diagnostic, secret_values([self.password]))
                if process.returncode:
                    raise SSHError(f"SSH failed ({process.returncode}): {diagnostic.strip()}")
                if len(raw) > 12 * 1024 * 1024:
                    raise SSHError("SSH response exceeded the transfer limit")
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise SSHError("Remote Python did not return a valid response") from exc
                if not isinstance(result, dict):
                    raise SSHError("Remote Python returned an invalid response shape")
                if result.get("error") and "state" not in result:
                    if payload.get("job_id") and result.get("error_code") == "input_manifest_conflict":
                        raise SSHInputManifestConflict(result.get("existing_input_manifest_sha256"))
                    raise SSHError(str(result["error"]))
                return result

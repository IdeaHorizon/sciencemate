"""Pinned SSH connections and loopback-only API tunnels for personal runtimes."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import asyncssh
except ModuleNotFoundError:
    # Existing desktop installations receive pure-Python dependencies with app/ updates.
    sys.path.append(str(Path(__file__).resolve().parents[1] / "_vendor"))
    import asyncssh

from app.services.other_homes import ThereIsNoSuchHomeError


class HostKeyRequiredError(ThereIsNoSuchHomeError):
    def __init__(self, fingerprint: str):
        super().__init__(
            "请核对服务器主机指纹后确认连接。",
            en="Verify the server host fingerprint before connecting.",
            status=409,
        )
        self.fingerprint = fingerprint


async def connect(record: dict, credentials: dict):
    expected = record.get("fingerprint")
    observed = ""

    class Client(asyncssh.SSHClient):
        def validate_host_public_key(self, host, addr, port, key):
            nonlocal observed
            observed = key.get_fingerprint("sha256")
            return bool(expected and observed == expected)

    options = dict(
        client_factory=Client,
        connect_timeout=15,
        login_timeout=20,
        keepalive_interval=20,
        keepalive_count_max=3,
    )
    # Imported SSH aliases retain IdentityFile, ProxyJump and other OpenSSH options.
    if record.get("username"):
        options["username"] = record["username"]
    if record.get("port"):
        options["port"] = record["port"]
    if expected:
        options["known_hosts"] = b""
    if record.get("identity_file"):
        identity = Path(record["identity_file"]).expanduser()
        if not identity.is_file():
            raise ThereIsNoSuchHomeError(
                "找不到 SSH 私钥文件，请重新选择。",
                en="SSH private key file not found. Choose it again.",
                status=422,
            )
        options["client_keys"] = [str(identity)]
    for key in ("password", "passphrase"):
        if credentials.get(key):
            options[key] = credentials[key]
    if record.get("auth_method") == "password":
        if not credentials.get("password"):
            raise ThereIsNoSuchHomeError(
                "请重新输入 SSH 密码以连接服务器。",
                en="Enter the SSH password to reconnect.",
                status=401,
            )
        options.update(client_keys=[], agent_path=None)
    try:
        connection = await asyncssh.connect(record["host"], **options)
    except asyncssh.HostKeyNotVerifiable as exc:
        if observed and not expected:
            raise HostKeyRequiredError(observed) from exc
        raise ThereIsNoSuchHomeError(
            "服务器主机指纹与记录不一致，请核对服务器身份。",
            en="The server host fingerprint has changed. Verify the server's identity.",
            status=409,
        ) from exc
    except (asyncssh.Error, OSError, ValueError, TimeoutError) as exc:
        # Never reflect library error strings: they may include credentials or paths.
        raise ThereIsNoSuchHomeError(
            "SSH 连接失败，请检查地址、用户名、密钥或密码，以及本机 SSH 配置。",
            en="SSH connection failed. Check the host, username, credentials and local SSH config.",
            status=503,
        ) from exc
    actual = connection.get_server_host_key().get_fingerprint("sha256")
    if expected and actual != expected:
        connection.close()
        await connection.wait_closed()
        raise ThereIsNoSuchHomeError(
            "服务器主机指纹发生变化。", en="Server fingerprint changed.", status=409
        )
    return connection, actual


@dataclass
class Tunnel:
    connection: object
    listener: object
    url: str


class TunnelManager:
    def __init__(self):
        self._active: dict[str, Tunnel] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def connected(self, server_id: str) -> bool:
        tunnel = self._active.get(server_id)
        return bool(tunnel and not tunnel.connection.is_closed())

    async def endpoint(self, record: dict, credentials: dict | None = None) -> str:
        from app.services.remote_servers import credentials_for

        if record.get("enabled") is False:
            raise ThereIsNoSuchHomeError(
                "连接已断开，请在运行环境设置中点击「连接检查」以重新连接。",
                en="Disconnected. Use Check connection in Runtime environments to reconnect.",
                status=503,
            )
        server_id = record["id"]
        async with self._locks.setdefault(server_id, asyncio.Lock()):
            if self.connected(server_id):
                return self._active[server_id].url
            connection, _ = await connect(
                record, credentials if credentials is not None else credentials_for(server_id)
            )
            try:
                listener = await connection.forward_local_port(
                    "127.0.0.1", 0, "127.0.0.1", record["runtime_port"]
                )
            except BaseException as exc:
                connection.close()
                await connection.wait_closed()
                if not isinstance(exc, (asyncssh.Error, OSError)):
                    raise
                raise ThereIsNoSuchHomeError(
                    "无法建立 SSH 隧道，请检查服务器的端口转发权限。",
                    en="Cannot open the SSH tunnel. Check server port-forwarding permissions.",
                    status=503,
                ) from exc
            url = f"http://127.0.0.1:{listener.get_port()}"
            self._active[server_id] = Tunnel(connection, listener, url)
            return url

    async def disconnect(self, server_id: str) -> None:
        async with self._locks.setdefault(server_id, asyncio.Lock()):
            tunnel = self._active.pop(server_id, None)
            if tunnel:
                tunnel.listener.close()
                tunnel.connection.close()
                await tunnel.connection.wait_closed()

    async def close(self) -> None:
        await asyncio.gather(*(self.disconnect(key) for key in list(self._active)))


tunnels = TunnelManager()


def ssh_aliases() -> list[str]:
    """Read concrete Host entries, including Include files; never execute SSH commands."""
    import glob

    seen: set[Path] = set()
    names: set[str] = set()

    def read(path: Path):
        path = path.expanduser()
        if path in seen or len(seen) >= 64:
            return
        seen.add(path)
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return
        for line in lines:
            try:
                parts = shlex.split(line, comments=True)
            except ValueError:
                continue
            if not parts:
                continue
            if parts[0].lower() == "host":
                names.update(name for name in parts[1:] if not any(c in name for c in "*?!"))
            elif parts[0].lower() == "include":
                for pattern in parts[1:]:
                    expanded = Path(pattern).expanduser()
                    if not expanded.is_absolute():
                        expanded = Path.home() / ".ssh" / expanded
                    for match in glob.glob(str(expanded)):
                        read(Path(match))

    read(Path.home() / ".ssh/config")
    return sorted(names)


def runtime_bundle() -> Path:
    app_root = Path(__file__).resolve().parents[1]
    candidates = sorted((app_root / "runtime_bundle").glob("sciencemate-runtime-*.tar.gz"))
    if candidates:
        return candidates[-1]
    # Source-checkout mode uses the same builder as desktop packaging.
    repo = Path(__file__).resolve().parents[4]
    builder = repo / "scripts/package/build_remote_runtime_bundle.py"
    if builder.is_file():
        import importlib.util

        spec = importlib.util.spec_from_file_location("remote_runtime_bundle", builder)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.build()
    raise ThereIsNoSuchHomeError(
        "当前安装缺少远程运行时安装包，请更新桌面应用后重试。",
        en="This installation is missing the remote runtime bundle. Update the desktop app.",
        status=409,
    )


async def provision(record: dict, credentials: dict) -> tuple[dict, dict]:
    connection, fingerprint = await connect(record, credentials)
    directory = None
    try:
        bundle = await asyncio.to_thread(runtime_bundle)
        # A private, unique staging directory prevents two desktop installs racing.
        result = await connection.run("umask 077; mktemp -d", check=True, timeout=20)
        directory = result.stdout.strip()
        if not directory.startswith("/") or "\n" in directory:
            raise ValueError("Invalid staging directory")
        async with connection.start_sftp_client() as sftp:
            await sftp.put(str(bundle), directory + "/runtime.tar.gz")
        command = (
            f"set -eu; cd {shlex.quote(directory)}; "
            "tar -xzf runtime.tar.gz; bash install-runtime.sh"
        )
        # The installer reads/writes the API token on the server. No secrets in argv.
        result = await connection.run(command, check=False, timeout=900)
        if result.exit_status:
            codes = {
                "linux_required": (
                    "远程运行目前支持 Linux 服务器。",
                    "Remote runtimes currently require a Linux server.",
                ),
                "install_in_progress": (
                    "服务器上已有安装正在进行，请稍后重试。",
                    "Another installation is already running on the server. Try again later.",
                ),
                "user_service_unavailable": (
                    "服务器无法启动 systemd 用户服务，请联系管理员启用后重试。",
                    "Systemd user services are unavailable. Ask the administrator to enable them.",
                ),
                "linger_required": (
                    "请管理员为此用户启用 loginctl enable-linger，确保 SSH 退出后任务继续运行。",
                    "Ask the administrator to enable loginctl enable-linger for this user "
                    "so tasks continue after SSH logout.",
                ),
                "git_required": (
                    "服务器缺少 Git，请安装后重试。",
                    "Git is missing on the server. Install it and retry.",
                ),
            }
            for code, (zh, en) in codes.items():
                if f"SCIENCEMATE_RUNTIME_ERROR={code}" in result.stderr.splitlines():
                    raise ThereIsNoSuchHomeError(zh, en=en, status=502)
            raise ThereIsNoSuchHomeError(
                "远程运行时安装失败。请检查服务器网络、Python 环境和后台服务权限后重试。",
                en="Remote runtime installation failed. Check server networking, Python and "
                "background-service permissions, then retry.",
                status=502,
            )
        marker = next(
            (
                line.removeprefix("SCIENCEMATE_RUNTIME=")
                for line in result.stdout.splitlines()
                if line.startswith("SCIENCEMATE_RUNTIME=")
            ),
            None,
        )
        installed = json.loads(marker or "null")
        if not installed or installed.get("protocol") != 1:
            raise ValueError("Installer did not return a supported runtime")
        record = {
            **record,
            "fingerprint": fingerprint,
            "runtime_port": int(installed["port"]),
            "version": installed["version"],
            "service_manager": installed["service_manager"],
        }
        credentials = {**credentials, "api_token": installed["token"]}
        return record, credentials
    except ThereIsNoSuchHomeError:
        raise
    except (asyncssh.Error, OSError, ValueError, TypeError, KeyError, TimeoutError) as exc:
        raise ThereIsNoSuchHomeError(
            "无法完成远程安装，请检查服务器连接后重试。",
            en="Remote installation could not finish. Check the server connection and retry.",
            status=502,
        ) from exc
    finally:
        if directory and len(Path(directory).parts) > 2 and not connection.is_closed():
            try:
                await connection.run(f"rm -rf -- {shlex.quote(directory)}", timeout=10)
            except (asyncssh.Error, OSError, TimeoutError):
                pass
        # Closing SSH is deliberately independent from the installed service.
        connection.close()
        await connection.wait_closed()

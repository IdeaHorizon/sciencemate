"""Controlled network acquisition for Experiment source and data inputs.

This tool is the controlled path for pulling an external HTTPS file or public Git
repository into authorized storage:

1. validate a fixed HTTPS file download or public Git clone, and check the target
   host -- plus every redirect hop and every Git submodule URL -- against the
   runtime-effective egress allowlist **before** that request is sent;
2. create an empty staging directory inside an already-authorized write root;
3. run the downloader as a one-shot networked process whose only writable root is
   that staging directory;
4. verify the resulting file/tree on the host control plane; and
5. atomically import it into the requested authorized destination.

Stated so nobody relies on more (#770): on the native backends the acquisition
step gets a plain network switch, not per-domain filtering, and reads are not
isolated -- the downloader can read what the host user can read.  Domain
filtering is this tool's own pre-request check; the real egress wall is Core's.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from asyncio import sleep as async_sleep
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlsplit

from core.state import State
from core.tool_registry import ToolDefinition, register_tool
from shared.lib.cancellable_subprocess import spawn_and_wait

from .path_roles import (
    IMMUTABLE_ROLES,
    experiment_output_dir,
    matching_path_roles,
)

_DEFAULT_MAX_BYTES = 512 * 1024**2
_MAX_MAX_BYTES = 8 * 1024**3
_DEFAULT_TIMEOUT_SECONDS = 900
_MAX_TIMEOUT_SECONDS = 3600
_MAX_TREE_ENTRIES = 100_000
#: 文件下载逐跳核对重定向的上限跳数；git 子模块逐层核对的上限层数（#770）。
_MAX_REDIRECT_HOPS = 5
_MAX_SUBMODULE_DEPTH = 8
_MAX_CURL_ATTEMPTS = 3
_CURL_RETRYABLE_RETURN_CODES = frozenset(
    {5, 6, 7, 18, 22, 28, 35, 47, 52, 55, 56, 92}
)
_CURL_RETRY_DELAYS_SECONDS = (1.0, 2.0)
_ALLOWED_DESTINATION_ROLES = frozenset(
    {
        "managed_source_root",
        "run_root",
        "build_root",
        "approved_write_root",
        "dependency_root",
    }
)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._+-]+")
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class AcquisitionError(RuntimeError):
    """A requested acquisition violates the fixed adapter contract."""


class NetworkAccessRequired(AcquisitionError):
    """The exact next host needs an explicit per-run egress grant."""

    def __init__(
        self,
        decision: dict[str, Any],
        *,
        reason: str,
        what_for: str,
        redirect_of: str = "",
        already_requested: bool = False,
    ) -> None:
        host = str(decision.get("host") or "")
        policy = decision.get("policy") or {}
        arguments = {
            "host": host,
            "reason": str(reason or "").strip(),
            "what_for": str(what_for or "").strip(),
        }
        if redirect_of:
            arguments["redirect_of"] = str(redirect_of).strip()
        rendered = ", ".join(
            f"{key}={value!r}" for key, value in arguments.items()
        )
        via = f"（由 {redirect_of} 重定向而来）" if redirect_of else ""
        allowlist = _deployment_allowlist_with(policy, host)
        if already_requested:
            message = (
                f"本 run 已经为精确主机 {host} 调用过 request_network_access，"
                "但授权当前仍未生效（可能被拒绝、未答复，或答复没有形成授权）。"
                "不要再次申请同一主机，也不要重复 fetch_resource。"
                f"请调用 report_blocker，说明需要访问 {host} 以及用途：{reason}。"
                "需要长期放行时，由部署方使用下面这行配置并重启服务：\n"
                f"HARNESS_SANDBOX_EGRESS_ALLOWLIST={allowlist}"
            )
        else:
            message = (
                f"目标域名 {host}{via} 不在生效 egress 白名单内，且本 run 没有该精确主机的授权"
                f"（来源={policy.get('source') or '未知'}）={policy.get('raw') or ''}。"
                "本次在联网之前拒绝，没有发出这个请求。"
                f"先调用 request_network_access({rendered})；用户允许后原样重试 fetch_resource。"
                "若需长期放行，由部署方使用下面这行配置并重启服务：\n"
                f"HARNESS_SANDBOX_EGRESS_ALLOWLIST={allowlist}\n"
                "不要改用其它镜像域名绕过声明的来源。"
            )
        super().__init__(message)
        self.host = host
        self.arguments = arguments
        self.call = f"request_network_access({rendered})"
        self.decision = decision
        self.requestable = not already_requested

    def as_result(self, *, kind: str) -> dict[str, Any]:
        result = {
            "status": "error",
            "error_code": "network_access_required",
            "error": str(self),
            "kind": kind,
            "request_sent": False,
            "retryable_after_change": True,
            "request_network_access": {
                "tool": "request_network_access",
                "arguments": dict(self.arguments),
                "call": self.call,
            },
            "egress_decision": dict(self.decision),
        }
        if not self.requestable:
            result["error_code"] = "network_access_request_unresolved"
            result.pop("request_network_access", None)
            result["next_action"] = {
                "tool": "report_blocker",
                "arguments": {
                    "summary": (
                        f"本 run 对 {self.host} 的出网授权申请未生效，"
                        "无法继续取得声明的输入资源"
                    ),
                    "requested_action": (
                        f"确认是否允许 {self.host}，或由部署方长期加入 egress allowlist"
                    ),
                    "suggested_owner": "部署方或本 run 的授权审批人",
                    "retryable_after_change": True,
                },
            }
        return result


class MaxBytesExceeded(AcquisitionError):
    """The acquisition crossed the caller's effective byte ceiling."""

    def __init__(
        self,
        requested_max_bytes: int,
        max_bytes_source: str,
        *,
        actual_size_bytes: int | None = None,
        detail: str = "",
    ) -> None:
        message = (
            f"下载结果超过 max_bytes={requested_max_bytes} bytes；已丢弃，未导入 destination。"
        )
        if actual_size_bytes is not None:
            message += f"实际大小={actual_size_bytes} bytes。"
        if detail:
            message += f"detail={detail}"
        super().__init__(message)
        self.requested_max_bytes = int(requested_max_bytes)
        self.max_bytes_source = str(max_bytes_source)
        self.actual_size_bytes = actual_size_bytes

    def as_result(self, *, kind: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "error",
            "error_code": "max_bytes_exceeded",
            "error": str(self),
            "kind": kind,
            "requested_max_bytes": self.requested_max_bytes,
            "max_bytes_source": self.max_bytes_source,
            "retryable": False,
            "retryable_after_change": True,
            "recovery": (
                "不要原样重试。省略 max_bytes 会回到默认 512 MiB "
                f"({_DEFAULT_MAX_BYTES} bytes)；需要更大上限时显式设置 max_bytes，"
                f"最高 8 GiB ({_MAX_MAX_BYTES} bytes)。"
            ),
        }
        if self.actual_size_bytes is not None:
            result["actual_size_bytes"] = self.actual_size_bytes
        return result


def _raise_if_fetch_cancelled(state: State) -> None:
    """Do not let a retry delay hide Core's run cancellation signals."""
    from core.cancellation import RunCancelled, signal_for

    signal = signal_for(state)
    if not signal:
        event = getattr(state, "kill_event", None)
        try:
            if event is not None and event.is_set():
                signal = {"reason": "fetch_resource retry cancelled"}
        except Exception:
            pass
    if signal:
        raise RunCancelled("fetch_resource", signal)


async def _sleep_for_curl_retry(
    state: State,
    delay_seconds: float,
    deadline: float,
) -> None:
    """Wait within one curl-hop deadline while polling the authoritative cancel flag."""
    wake_at = min(deadline, monotonic() + max(0.0, delay_seconds))
    while True:
        _raise_if_fetch_cancelled(state)
        remaining = wake_at - monotonic()
        if remaining <= 0:
            return
        # The legacy curl retry schedule is 1s then 2s.  Polling at most once a
        # second preserves that pacing without making a sticky /stop wait for
        # the whole second delay to finish.
        await async_sleep(min(1.0, remaining))


def _validated_https_url(raw: str) -> str:
    url = str(raw or "").strip()
    # 判决拆除·第三波（rf:69 → schema / rf:76 删，2026-09-02）：非空、≤4096、无 NUL
    # 由 fetch_resource schema（minLength/maxLength/pattern）在派发口核；fragment 本就
    # 不发给服务器，拒绝不保护任何东西——机械剥掉。
    parsed = urlsplit(url)
    if parsed.fragment:
        parsed = parsed._replace(fragment="")
        url = parsed.geturl()
    if parsed.scheme.lower() != "https":
        raise AcquisitionError("受控下载只接受 HTTPS；HTTP、SSH、git:// 和本地路径均被拒绝")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise AcquisitionError("URL 必须包含公共主机名，且不能携带用户名或密码")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or not all(
        part and part.replace("-", "").isalnum() for part in host.split(".")
    ):
        raise AcquisitionError("URL 主机名格式不合法；IP literal 和 localhost 均被拒绝")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise AcquisitionError("URL 只能使用公共域名，不能使用 IP literal")
    try:
        host.encode("idna")
    except UnicodeError as exc:
        raise AcquisitionError("URL 主机名无法按 IDNA 规范化") from exc
    return url


def _default_name(url: str, kind: str) -> str:
    raw = unquote(Path(urlsplit(url).path).name)
    if kind == "git" and raw.endswith(".git"):
        raw = raw[:-4]
    cleaned = _SAFE_NAME_RE.sub("-", raw).strip(".-")
    if not cleaned:
        cleaned = "repository" if kind == "git" else "download.bin"
    return cleaned[:160]


def _resolve_destination(
    state: State,
    destination: str | None,
    *,
    url: str,
    kind: str,
) -> tuple[Path, Path]:
    default_root = experiment_output_dir(state, "runtime", create=True) / "acquired"
    default_root.mkdir(parents=True, exist_ok=True)
    raw = str(destination or "").strip()
    if not raw:
        candidate = default_root / _default_name(url, kind)
    else:
        supplied = Path(raw).expanduser()
        if supplied.is_absolute():
            candidate = supplied
        else:
            if any(part in {"", ".", ".."} for part in supplied.parts):
                raise AcquisitionError("相对 destination 不能包含 .、.. 或空路径段")
            # 归一化：相对 destination 只相对唯一权威基点 runtime/acquired 解析。
            # 调用方若把路径写成相对节点根（自带 runtime/acquired 前缀），剥掉
            # 前缀再拼，避免产出 runtime/acquired/runtime/acquired/... 嵌套目录。
            parts = supplied.parts
            while parts[:2] == ("runtime", "acquired"):
                parts = parts[2:]
            if parts:
                candidate = default_root.joinpath(*parts)
            else:
                candidate = default_root / _default_name(url, kind)

    candidate = candidate.resolve(strict=False)
    roles = matching_path_roles(candidate, state)
    allowed = [
        role
        for role in roles
        if role.writable
        and not role.container_only
        and role.role not in IMMUTABLE_ROLES
        and role.role in _ALLOWED_DESTINATION_ROLES
    ]
    if not allowed or len(allowed) != len(roles):
        details = [
            {
                "role": role.role,
                "path": role.path,
                "writable": role.writable,
                "container_only": role.container_only,
            }
            for role in roles
        ]
        raise AcquisitionError(
            "destination 必须位于本 run 的 run_root/build_root，或显式声明的 "
            "managed_source_root、可写 dependency_root、approved_write_root 内；"
            f"当前匹配={details or 'none'}"
        )
    root = max(
        (Path(role.path).resolve(strict=False) for role in allowed),
        key=lambda path: len(path.parts),
    )
    if candidate == root:
        raise AcquisitionError("destination 必须是获准写入根下面的新文件或目录，不能替换根本身")
    return candidate, root


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _tree_inventory(root: Path) -> tuple[str, int, int]:
    """Hash a downloaded tree without following any downloaded symlink."""
    digest = hashlib.sha256()
    size = 0
    entries = 0
    resolved_root = root.resolve(strict=True)

    def visit(directory: Path) -> None:
        nonlocal size, entries
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            # Git's commit is the reproducibility identity.  Administrative
            # files contain clone-local reflog timestamps and would make a
            # checkout content hash nondeterministic.
            if entry.name == ".git":
                continue
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            info = entry.stat(follow_symlinks=False)
            entries += 1
            if entries > _MAX_TREE_ENTRIES:
                raise AcquisitionError(
                    f"下载树超过 {_MAX_TREE_ENTRIES} 个条目；已丢弃，未导入 destination"
                )
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                target_path = Path(target)
                if target_path.is_absolute():
                    raise AcquisitionError(f"下载树含绝对 symlink：{relative} -> {target}")
                resolved_target = (path.parent / target_path).resolve(strict=False)
                if not resolved_target.is_relative_to(resolved_root):
                    raise AcquisitionError(f"下载树含越界 symlink：{relative} -> {target}")
                digest.update(f"L\0{relative}\0{mode:o}\0{target}\0".encode())
            elif stat.S_ISDIR(info.st_mode):
                digest.update(f"D\0{relative}\0{mode:o}\0".encode())
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                file_hash, file_size = _sha256_file(path)
                size += file_size
                digest.update(f"F\0{relative}\0{mode:o}\0{file_size}\0{file_hash}\0".encode())
            else:
                raise AcquisitionError(f"下载树含不支持的特殊文件：{relative}")

    visit(root)
    return digest.hexdigest(), size, entries


def _git_head(repository: Path) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env={**os.environ, **_GIT_SANDBOX_ENVIRONMENT, "GIT_TERMINAL_PROMPT": "0"},
    )
    commit = result.stdout.strip().lower()
    if result.returncode != 0 or not _HEX_DIGEST_RE.fullmatch(commit):
        raise AcquisitionError(
            "Git 下载完成但无法解析 HEAD：" + (result.stderr or result.stdout).strip()[-300:]
        )
    return commit


def _copy_into_destination(source: Path, destination: Path, *, is_tree: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise AcquisitionError(
            f"destination 已存在，受控下载不会覆盖：{destination}。请换一个新路径。"
        )
    temporary = destination.parent / f".{destination.name}.hf-import-{uuid.uuid4().hex}"
    try:
        if is_tree:
            shutil.copytree(source, temporary, symlinks=True)
        else:
            shutil.copyfile(source, temporary, follow_symlinks=False)
            os.chmod(temporary, 0o644)
        if destination.exists() or destination.is_symlink():
            raise AcquisitionError(f"destination 在导入期间被创建，未覆盖：{destination}")
        temporary.rename(destination)
    finally:
        if temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary, ignore_errors=True)
        else:
            temporary.unlink(missing_ok=True)


def _normalized_egress_host(host_or_url: str) -> str:
    """Parse URLs at the Experiment boundary, then normalize only the real host."""
    from core.capability_grants import normalize_host

    raw = str(host_or_url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw or raw.startswith("//") else f"//{raw}")
    return normalize_host(str(parsed.hostname or ""))


def _url_for_human(url: str) -> str:
    """Return scheme/host/path only, keeping query credentials out of cards/transcript."""
    parsed = urlsplit(str(url or ""))
    host = _normalized_egress_host(url)
    if parsed.scheme and host:
        return f"{parsed.scheme.lower()}://{host}{parsed.path or ''}"
    return host


def _redact_current_url(text: str, url: str) -> str:
    """Redact the current signed request URL before subprocess text reaches node output."""
    raw_url = str(url or "")
    return str(text or "").replace(raw_url, _url_for_human(raw_url))


def _deployment_allowlist_with(policy: dict[str, Any], host: str) -> str:
    entries = [str(item) for item in (policy.get("deployment_entries") or []) if item]
    if host and host not in entries:
        entries.append(host)
    return ",".join(entries)


def _network_access_was_requested(state: State, host: str) -> bool:
    """Whether this run already invoked the grant tool for this exact host."""
    path = getattr(state, "transcript_path", None)
    if not isinstance(path, Path) or not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if '"request_network_access"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if event.get("event") != "tool_call" or event.get("name") != "request_network_access":
                    continue
                args = event.get("args")
                if isinstance(args, dict) and _normalized_egress_host(args.get("host", "")) == host:
                    return True
    except OSError:
        return False
    return False


def _egress_access_decision(state: State, host_or_url: str = "") -> dict[str, Any]:
    """Return the one Experiment-side decision for an egress host.

    Core owns both inputs.  Its deployment policy keeps the historical suffix
    semantics; a human grant is deliberately compared as one exact host.  The
    two sets never get merged before suffix matching.
    """
    from core.capability_grants import granted_hosts, is_granted, normalize_host
    from core.sandbox import effective_egress_policy

    core_policy = effective_egress_policy()
    deployment_entries = [
        normalize_host(item)
        for item in (core_policy.get("from_environment") or [])
        if normalize_host(item)
    ]
    host = _normalized_egress_host(host_or_url)
    deployment_allowed = bool(host) and any(
        host == domain or host.endswith("." + domain)
        for domain in deployment_entries
    )
    exact_granted = bool(host) and is_granted(state, host)
    source = str(core_policy.get("source") or "")
    if " + " in source:
        source = source.split(" + ", 1)[0]
    policy = {
        "env_var": core_policy.get("env_var"),
        "raw": ",".join(deployment_entries),
        "entries": list(deployment_entries),
        "deployment_entries": list(deployment_entries),
        "per_run_granted_hosts": list(granted_hosts(state)),
        "source": source,
        "matching": {
            "deployment": "exact_or_subdomain_suffix",
            "per_run_grant": "exact_host_only",
        },
    }
    return {
        "host": host,
        "allowed": bool(deployment_allowed or exact_granted),
        "authorized_by": (
            "deployment_suffix_allowlist" if deployment_allowed else
            "per_run_exact_grant" if exact_granted else None
        ),
        "deployment_allowed": deployment_allowed,
        "per_run_exact_grant": exact_granted,
        "policy": policy,
    }


def _curl_metadata(stdout: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _require_egress_access(
    state: State,
    url: str,
    *,
    reason: str,
    what_for: str,
    redirect_of: str = "",
) -> dict[str, Any]:
    """联网之前核对目标域名；源地址、每一跳重定向、每个子模块都过这一道（#770）。

    Core 给取物段的是布尔网络开关（core/sandbox.py 写明这份白名单"是策略不是墙"），
    按域名放行只能由本工具在发请求前做。真正的出网墙归 Core。
    """
    decision = _egress_access_decision(state, url)
    if not decision["allowed"]:
        if _network_access_was_requested(state, decision["host"]):
            raise NetworkAccessRequired(
                decision,
                reason=reason,
                what_for=_url_for_human(what_for),
                redirect_of=_normalized_egress_host(redirect_of) if redirect_of else "",
                already_requested=True,
            )
        raise NetworkAccessRequired(
            decision,
            reason=reason,
            what_for=_url_for_human(what_for),
            redirect_of=_normalized_egress_host(redirect_of) if redirect_of else "",
        )
    return decision


def _network_failure_guidance(
    returncode: int | None, detail: str, url: str, state: State,
) -> str:
    """请求发出之后的失败：域名已在联网前核过，这里按错误形状说真实原因。

    旧文案对名单外域名一律说"不在白名单"，DNS 解析失败（curl rc=6）也被这样归因。
    """
    host = str(urlsplit(url).hostname or "").rstrip(".").lower()
    text = detail.lower()
    if returncode == 6 or "could not resolve host" in text:
        cause = f"域名 {host} 解析失败（DNS）：检查 URL 拼写，或本机/部署的 DNS 与代理配置；"
    elif returncode == 7 or "failed to connect" in text or "connection refused" in text:
        cause = "连不上目标主机（网络、代理或防火墙）：先原样重试一次，持续失败再按 detail 找部署方；"
    elif returncode == 28 or "timed out" in text:
        cause = "请求超时：先原样重试一次；源文件很大时调大 timeout_seconds；"
    elif returncode in (35, 51, 58, 60) or "ssl" in text or "certificate" in text:
        cause = "TLS/证书错误：检查 URL 与部署的 CA 配置，不要关闭证书校验；"
    elif returncode == 22 or "returned error" in text:
        cause = "源站返回 HTTP 错误（状态码见 detail），多为 URL、权限或限流问题：先原样重试一次，再按状态码处置；"
    else:
        cause = "具体原因见 detail：先原样重试一次，再失败时按 detail 处置；"
    decision = _egress_access_decision(state, url)
    policy = decision["policy"]
    return (
        f"目标域名 {host} 已在当前出网授权范围内（方式={decision['authorized_by']}；"
        f"部署来源={policy['source']}）={policy['raw']}，"
        f"联网前已核对，这次失败不是白名单问题。{cause}不要申请修改白名单。"
    )


def _curl_argv(url: str, payload: Path, timeout_seconds: int, max_bytes: int) -> list[str]:
    # 不带 --location：重定向由调用方逐跳核对目标域名后再发下一次请求（#770）。
    # -q 必须紧跟 curl、放在第一个：不读 ~/.curlrc，那里面可以写代理或凭据选项
    # （第三会话复审 0915 沙箱 git 配置 P3-1，与 git 不读宿主全局配置同理）。
    return [
        "curl", "-q", "--fail", "--silent", "--show-error",
        # Python 侧按 rc 选择性重试；curl --retry-all-errors 会把 rc=63 的确定性超限
        # 也重放三次，既浪费带宽又把“改参数后再试”伪装成瞬时失败。
        "--connect-timeout", "20", "--max-time", str(timeout_seconds),
        "--max-filesize", str(max_bytes), "--proto", "=https",
        "--output", str(payload), "--write-out", "%{json}", "--", url,
    ]


_GIT_SAFE_ARGS = (
    "git",
    "-c", "protocol.file.allow=never",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.ssh.allow=never",
    "-c", "protocol.git.allow=never",
    # 不跟 HTTP 重定向：跳去哪个域名 git 不会先告诉我们，没法逐跳核对白名单（#770）。
    "-c", "http.followRedirects=false",
)
# 沙箱里的 git 不读宿主的全局 / 系统配置。沙箱把宿主 HOME 原样带进去，git 于是读到
# ~/.gitconfig：credential.helper=store 让取物进程用上宿主保存的凭据（与 credential_policy
# 「只支持公开 HTTPS 源」相悖），url.<x>.insteadOf 还会在节点核对 URL 之后把地址改写成别的
# 主机（第三会话复审 0914c 第九节，本机 git 2.43 实测）。只清掉 credential.helper 挡不住
# insteadOf，所以整份不读。出网代理走 HTTPS_PROXY 等环境变量，不受影响。
_GIT_SANDBOX_ENVIRONMENT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


async def _submodule_urls_as_git_resolves(checkout: Path, spawn: Any) -> list[tuple[str, str]]:
    """让 git 自己登记这一层子模块，读回它解析出的 URL（名字、URL）。不联网。

    相对 URL（../x）怎么解析以 git 为准。自己用 urljoin 算会和 git 分叉：
    `../../../evil.example.com/x.git` 按 urljoin 仍落在父仓库域名下，git 2.43 却解析成
    evil.example.com（d0541a1e 审查实测）。所以先 `git submodule init`（只写本地配置），
    再读 git 登记下的 submodule.<name>.url——核对的就是 git 接下来要去拉的地址。
    两步都经受管沙箱、不给网，不在宿主上新增子进程调用点。
    """
    gitmodules = checkout / ".gitmodules"
    if not gitmodules.is_file() or gitmodules.is_symlink():
        return []
    await spawn([*_GIT_SAFE_ARGS, "-C", str(checkout), "submodule", "init"], network=False)
    raw = await spawn(
        [*_GIT_SAFE_ARGS, "-C", str(checkout), "config", "--local", "--null",
         "--get-regexp", r"^submodule\..*\.url$"],
        network=False, ok_codes=(0, 1),  # 1 = 没有任何已登记的子模块
    )
    urls: list[tuple[str, str]] = []
    for item in raw.decode("utf-8", errors="replace").split("\0"):
        if not item:
            continue
        key, _, value = item.partition("\n")
        urls.append((key[len("submodule."):].rsplit(".", 1)[0], value))
    return sorted(urls)


async def _gitlink_paths(checkout: Path, spawn: Any) -> list[str]:
    """这一层检出里 git 索引记录的子模块路径（gitlink，mode 160000）。不联网。"""
    raw = await spawn(
        [*_GIT_SAFE_ARGS, "-C", str(checkout), "ls-files", "--stage", "-z"],
        network=False,
    )
    paths: list[str] = []
    for record in raw.decode("utf-8", errors="replace").split("\0"):
        meta, _, path = record.partition("\t")
        if meta.startswith("160000 ") and path:
            paths.append(path)
    return paths


async def _fetch_submodules_level_by_level(
    repository: Path, state: State, spawn: Any,
) -> None:
    """逐层取子模块：这一层每个检出的每个子模块 URL 都核对通过之后，才联网拉这一层。

    `git clone --recurse-submodules` 会一口气跟完所有层，子模块指向哪个域名事先
    核对不了（#770）。这里一层一层来：先让 git 登记并读回它解析出的 URL，整层核完
    再逐个 `submodule update`，然后按 gitlink 往下一层走。
    """
    level = [repository]
    for _depth in range(_MAX_SUBMODULE_DEPTH):
        planned: list[tuple[Path, list[tuple[str, str]]]] = []
        for checkout in level:
            registered = await _submodule_urls_as_git_resolves(checkout, spawn)
            for name, url in registered:
                validated_url = _validated_https_url(url)
                _require_egress_access(
                    state,
                    validated_url,
                    reason=f"获取 Git 子模块 {name}",
                    what_for=validated_url,
                )
            if registered:
                planned.append((checkout, registered))
        if not planned:
            return
        next_level: list[Path] = []
        for checkout, registered in planned:
            await spawn(
                [*_GIT_SAFE_ARGS, "-C", str(checkout),
                 "submodule", "update", "--depth", "1"],
                registered[0][1],
            )
            next_level.extend(
                checkout / path for path in await _gitlink_paths(checkout, spawn))
        level = next_level
    raise AcquisitionError(
        f"子模块嵌套超过 {_MAX_SUBMODULE_DEPTH} 层，已放弃、未导入 destination"
    )


async def _fetch_resource(
    state: State,
    url: str,
    kind: str = "file",
    destination: str | None = None,
    ref: str | None = None,
    recursive: bool = False,
    expected_sha256: str | None = None,
    expected_commit: str | None = None,
    max_bytes: int | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    **_: Any,
) -> dict[str, Any]:
    """Fetch one file or public Git repository through the controlled adapter."""
    try:
        try:
            from .execution_action_census import pending_operation_action_block
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.execution_action_census import pending_operation_action_block
        pending_block = pending_operation_action_block(
            state,
            {
                "tool": "fetch_resource",
                "program": "controlled_fetch",
                "read_only": False,
                "dry_run": False,
                "observed_effects": ["network_access", "workspace_write"],
            },
            {
                "decision": "route_not_required",
                "policy": "controlled_resource_fetch",
                "effective_effects": ["network_access", "workspace_write"],
            },
        )
        if pending_block is not None:
            return pending_block
        max_bytes_source = "default" if max_bytes is None else "explicit"
        max_bytes = _DEFAULT_MAX_BYTES if max_bytes is None else int(max_bytes)
        # 判决拆除·第三波（2026-09-02）：rf:286 node_type 路由断言删——注册表
        # allowed_node_types=["experiment"] 是唯一真相源；rf:290/292/298/304/311/314
        # 的 kind 枚举、max_bytes/timeout_seconds 区间、sha256/commit pattern 全部由
        # fetch_resource schema 在派发口核，这里不再手写。
        source_url = _validated_https_url(url)
        try:
            from .run_contract import audit_execution_intent_binding
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.run_contract import audit_execution_intent_binding
        try:
            intent_binding = audit_execution_intent_binding(state, require=True)
        except Exception as exc:
            return {
                "status": "error",
                "error_code": "execution_intent_binding_required",
                "error": "受控下载无法核验本 run 的上游目标绑定；未创建目录、未联网。",
                "kind": kind,
                "execution_intent_binding": {
                    "passed": False,
                    "status": "binding_audit_error",
                    "reason": f"{type(exc).__name__}: {exc}",
                },
            }
        if not intent_binding.get("passed", False):
            return {
                "status": "error",
                "error_code": "execution_intent_binding_required",
                "error": "上游任务输入或冻结 prereg 已漂移/不可核验；未创建目录、未联网。",
                "kind": kind,
                "execution_intent_binding": intent_binding,
            }
        # 联网之前核对目标域名（#770）。此前白名单只在失败之后读来写指引，名单外的域名
        # 照样发出请求；原生后端的取物段直接给网，没有别处替它按域名放行。
        _require_egress_access(
            state,
            source_url,
            reason="获取当前 Experiment 声明的输入资源",
            what_for=source_url,
        )
        target, authorized_root = _resolve_destination(
            state, destination, url=source_url, kind=kind
        )
        expected_hash = str(expected_sha256 or "").strip().lower()
        expected_git = str(expected_commit or "").strip().lower()
        if kind == "file" and (ref or recursive or expected_git):
            raise AcquisitionError("ref、recursive、expected_commit 只适用于 kind=git")
        if kind == "git" and expected_hash:
            raise AcquisitionError("expected_sha256 只适用于 kind=file；Git 请用 expected_commit")
        branch = str(ref or "").strip()
        if branch and (len(branch) > 255 or branch.startswith("-") or "\x00" in branch):
            raise AcquisitionError("ref 不是安全的 Git branch/tag 名")

        # 判决拆除·第三波（rf:325 删，2026-09-02）：destination 已存在的判决只有
        # 一处——原子导入 `_copy_into_destination`（永不覆盖）；这里的预检是同条件
        # 三写之一，删。
        target.parent.mkdir(parents=True, exist_ok=True)
        public_source_url = _url_for_human(source_url)
        state.append_transcript(
            "resource_acquisition_started",
            kind=kind,
            url=public_source_url,
            destination=str(target),
            max_bytes=max_bytes,
            max_bytes_source=max_bytes_source,
            timeout_seconds=timeout_seconds,
        )

        from core.sandbox import SandboxLimits

        limits = SandboxLimits(
            memory_bytes=512 * 1024**2,
            cpus=1,
            pids=64,
            walltime_seconds=timeout_seconds,
            storage_bytes=max(64 * 1024**2, max_bytes),
            storage_entries=_MAX_TREE_ENTRIES,
            output_bytes=4 * 1024**2,
            tmpfs_bytes=64 * 1024**2,
        )
        with tempfile.TemporaryDirectory(
            prefix=".harness-acquire-", dir=str(authorized_root)
        ) as temporary:
            staging = Path(temporary).resolve(strict=True)
            payload = staging / ("repository" if kind == "git" else "download")
            redirects: list[dict[str, Any]] = []

            async def _spawn(
                argv: list[str], url: str = source_url, *,
                network: bool = True, ok_codes: tuple[int, ...] = (0,),
            ) -> bytes:
                is_curl = network and argv[:1] == ["curl"]
                attempts = _MAX_CURL_ATTEMPTS if is_curl else 1
                deadline = monotonic() + timeout_seconds
                for attempt in range(attempts):
                    if attempt:
                        payload.unlink(missing_ok=True)
                    _raise_if_fetch_cancelled(state)
                    remaining_seconds = deadline - monotonic()
                    # ceil keeps the caller's complete first-attempt budget:
                    # int(timeout_seconds - a few microseconds) silently made
                    # 10 seconds become 9.  The outer timeout remains the
                    # authority even though curl's argv still names the
                    # original hop budget.
                    remaining = max(1, math.ceil(remaining_seconds))
                    status, returncode, out, err = await spawn_and_wait(
                        *argv,
                        state=state,
                        timeout=remaining,
                        cwd=str(staging),
                        writable_roots=[staging],
                        readonly_roots=[],
                        sandbox_limits=limits,
                        network_access=network,
                        sandbox_environment=(
                            dict(_GIT_SANDBOX_ENVIRONMENT)
                            if argv[:1] == ["git"] else None
                        ),
                    )
                    if status == "done" and returncode in ok_codes:
                        return out
                    raw_detail = err.decode("utf-8", errors="replace")
                    detail = _redact_current_url(raw_detail, url)[-1600:]
                    if is_curl and returncode == 63:
                        raise MaxBytesExceeded(
                            max_bytes,
                            max_bytes_source,
                            detail=detail,
                        )
                    should_retry = (
                        is_curl
                        and status == "done"
                        and returncode in _CURL_RETRYABLE_RETURN_CODES
                        and attempt + 1 < attempts
                    )
                    if should_retry:
                        delay = _CURL_RETRY_DELAYS_SECONDS[attempt]
                        # Match curl's pre-existing 1s/2s backoff, but do not
                        # wait when that would consume the entire shared
                        # deadline and leave no time for another request.
                        if deadline - monotonic() > delay:
                            await _sleep_for_curl_retry(state, delay, deadline)
                            if deadline - monotonic() > 0:
                                continue
                    raise AcquisitionError(
                        (f"受控 {kind} 下载失败（status={status}, rc={returncode}）：{detail}\n"
                         + _network_failure_guidance(returncode, raw_detail, url, state))
                        if network else
                        (f"受控 {kind} 取回之后的本地 git 步骤失败（未联网，status={status}, "
                         f"rc={returncode}）：{detail}")
                    )
                raise AssertionError("curl retry loop exhausted without a result")

            if kind == "file":
                # curl 不跟重定向：每一跳先核对目标域名，再发下一次请求（#770）。
                hop_url = source_url
                for _hop in range(_MAX_REDIRECT_HOPS + 1):
                    payload.unlink(missing_ok=True)
                    stdout = await _spawn(
                        _curl_argv(hop_url, payload, timeout_seconds, max_bytes), hop_url)
                    hop = _curl_metadata(stdout)
                    code = _int_or_zero(hop.get("http_code") or hop.get("response_code"))
                    redirect_url = str(hop.get("redirect_url") or "").strip()
                    if not (300 <= code < 400 and redirect_url):
                        break
                    next_url = _validated_https_url(redirect_url)
                    origin = _egress_access_decision(state, hop_url)
                    redirect_reason = "继续获取源地址返回的 HTTPS 重定向资源"
                    redirect_of = hop_url
                    if origin["deployment_allowed"]:
                        redirect_reason += (
                            f"（来路 {origin['host']} 已由部署白名单放行）"
                        )
                        # Core 的卡片只把本 run grant 视为 redirect_of 已授权；部署名单
                        # 已放行的来路不传该字段，避免卡片错误声称“本次并没有被授权”。
                        redirect_of = ""
                    _require_egress_access(
                        state,
                        next_url,
                        reason=redirect_reason,
                        what_for=next_url,
                        redirect_of=redirect_of,
                    )
                    redirects.append({
                        "from": _url_for_human(hop_url),
                        "to": _url_for_human(next_url),
                        "http_code": code,
                    })
                    hop_url = next_url
                else:
                    raise AcquisitionError(
                        f"重定向超过 {_MAX_REDIRECT_HOPS} 跳，已放弃、未导入 destination；"
                        f"最后一跳指向 {_url_for_human(hop_url)}"
                    )
            else:
                argv = [*_GIT_SAFE_ARGS, "clone", "--no-tags", "--depth", "1"]
                if branch:
                    argv.extend(["--branch", branch, "--single-branch"])
                argv.extend(["--", source_url, str(payload)])
                stdout = await _spawn(argv, source_url)
                if recursive and payload.is_dir() and not payload.is_symlink():
                    await _fetch_submodules_level_by_level(payload, state, _spawn)
            if not payload.exists():
                raise AcquisitionError("下载命令返回成功但 staging 中没有目标文件/目录")

            metadata: dict[str, Any] = {}
            if kind == "file":
                if not payload.is_file() or payload.is_symlink():
                    raise AcquisitionError("下载结果不是普通文件")
                digest, size = _sha256_file(payload)
                if size > max_bytes:
                    raise MaxBytesExceeded(
                        max_bytes,
                        max_bytes_source,
                        actual_size_bytes=size,
                    )
                if expected_hash and digest != expected_hash:
                    raise AcquisitionError(
                        f"SHA-256 不匹配：expected={expected_hash}, actual={digest}；已丢弃"
                    )
                metadata = _curl_metadata(stdout)
                _copy_into_destination(payload, target, is_tree=False)
                result = {
                    "status": "success",
                    "kind": kind,
                    "url": public_source_url,
                    "final_url": _url_for_human(
                        str(metadata.get("url_effective") or hop_url)
                    ),
                    "redirects": redirects,
                    "destination": str(target),
                    "sha256": digest,
                    "size_bytes": size,
                    "content_type": metadata.get("content_type"),
                }
            else:
                if not payload.is_dir() or payload.is_symlink():
                    raise AcquisitionError("Git 下载结果不是普通目录")
                commit = _git_head(payload)
                if expected_git and commit != expected_git:
                    raise AcquisitionError(
                        f"Git commit 不匹配：expected={expected_git}, actual={commit}；已丢弃"
                    )
                tree_hash, size, entries = _tree_inventory(payload)
                if size > max_bytes:
                    raise MaxBytesExceeded(
                        max_bytes,
                        max_bytes_source,
                        actual_size_bytes=size,
                    )
                _copy_into_destination(payload, target, is_tree=True)
                result = {
                    "status": "success",
                    "kind": kind,
                    "url": public_source_url,
                    "destination": str(target),
                    "commit": commit,
                    "tree_sha256": tree_hash,
                    "size_bytes": size,
                    "entries": entries,
                    "recursive": recursive,
                }

        state.append_transcript("resource_acquisition_completed", **result)
        return result
    except MaxBytesExceeded as exc:
        result = exc.as_result(kind=kind)
        try:
            state.append_transcript(
                "resource_acquisition_failed",
                kind=kind,
                url=_url_for_human(str(url))[:4096],
                destination=str(destination or ""),
                error=str(exc),
                error_code=result["error_code"],
                requested_max_bytes=result["requested_max_bytes"],
                max_bytes_source=result["max_bytes_source"],
                retryable=False,
            )
        except Exception:
            pass
        return result
    except NetworkAccessRequired as exc:
        result = exc.as_result(kind=kind)
        try:
            event = {
                "kind": kind,
                "url": _url_for_human(str(url))[:4096],
                "destination": str(destination or ""),
                "error": str(exc),
                "error_code": result["error_code"],
                "blocked_host": exc.host,
            }
            if "request_network_access" in result:
                event["request_network_access"] = result["request_network_access"]
            if "next_action" in result:
                event["next_action"] = result["next_action"]
            state.append_transcript("resource_acquisition_failed", **event)
        except Exception:
            pass
        return result
    except AcquisitionError as exc:
        try:
            state.append_transcript(
                "resource_acquisition_failed",
                kind=kind,
                url=_url_for_human(str(url))[:4096],
                destination=str(destination or ""),
                error=str(exc),
            )
        except Exception:
            pass
        return {"status": "error", "error": str(exc), "kind": kind}
    except Exception as exc:
        try:
            state.append_transcript(
                "resource_acquisition_failed",
                kind=kind,
                url=_url_for_human(str(url))[:4096],
                destination=str(destination or ""),
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        return {
            "status": "error",
            "error": f"受控下载内部失败：{type(exc).__name__}: {exc}",
            "kind": kind,
        }


register_tool(
    ToolDefinition(
        name="fetch_resource",
        description=(
            "把外部 HTTPS 文件或公开 Git 仓库安全落盘到 Experiment 获准的写入根。"
            "联网之前逐一核对源地址、每一跳重定向和每个 Git 子模块的域名是否在生效 egress "
            "部署白名单或本 run 精确主机授权内；缺授权时返回可直接调用的 "
            "request_network_access，下载进程唯一可写的是空 staging，"
            "下载完成后返回 SHA-256 或 Git commit/tree hash 供复现审计。\n\n"
            "**Use when**：需要下载源码 tarball、公开数据文件，或 clone 公开 HTTPS "
            "Git 仓库用于 toolchain_build/diagnostic。Git 子模块用 recursive=true。"
            "安装 Python wheel/sdist 时，把主包、全部依赖和构建后端都分别用 "
            "fetch_resource(kind='file') 取到同一目录；先 declare_execution_route 并按提示完成 "
            "preflight，再用 safe_run_bash 执行 pip install --no-index --find-links <取回目录> "
            "--target <获准可写目录> <包名>（也可按契约改用 --prefix）；耗时长则用 submit_job "
            "执行同一离线命令。\n\n"
            "**Do NOT use when**：读网页正文用 web_fetch；私有仓库/带凭据 URL 不支持；不要再用 "
            "safe_run_bash 的 curl/wget/git clone，它所在的 Attempt 刻意无网。\n\n"
            "destination 省略时落到本 run 的 runtime/acquired；相对路径以该目录为根；"
            "绝对路径必须位于 run_root/build_root 或显式授权的 managed_source_root、"
            "可写 dependency_root、approved_write_root。永不覆盖已有路径。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "pattern": "^[^\\x00]*$",
                    "description": "公开 HTTPS 文件或 Git repository URL；不能含凭据；#fragment 会被剥掉。",
                },
                "kind": {
                    "type": "string",
                    "enum": ["file", "git"],
                    "default": "file",
                    "description": "file=固定 GET 下载；git=受控 shallow clone。",
                },
                "destination": {
                    "type": "string",
                    "description": "可选新路径；已有路径绝不覆盖。相对路径落在 runtime/acquired。",
                },
                "ref": {
                    "type": "string",
                    "description": "kind=git 时可选 branch/tag；省略取远端默认分支。",
                },
                "recursive": {
                    "type": "boolean",
                    "default": False,
                    "description": "kind=git 时同时 shallow clone HTTPS submodules。",
                },
                "expected_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-fA-F]{64}$",
                    "description": "kind=file 的可选完整 SHA-256；不匹配则不导入。",
                },
                "expected_commit": {
                    "type": "string",
                    "pattern": "^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$",
                    "description": "kind=git 的可选完整 commit id；不匹配则不导入。",
                },
                "max_bytes": {
                    "type": "integer",
                    "default": _DEFAULT_MAX_BYTES,
                    "minimum": 1024,
                    "maximum": _MAX_MAX_BYTES,
                    "description": "下载文件或 Git 普通文件总字节上限，默认 512 MiB，最高 8 GiB。",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "default": _DEFAULT_TIMEOUT_SECONDS,
                    "minimum": 10,
                    "maximum": _MAX_TIMEOUT_SECONDS,
                    "description": "受控下载总时限，默认 900 秒，最高 3600 秒。",
                },
            },
            "required": ["url"],
        },
        allowed_node_types=["experiment"],
        risk_level="medium",
    ),
    _fetch_resource,
)


async def _describe_acquisition_capabilities(state: State, **_: Any) -> dict[str, Any]:
    """Report the runtime-effective acquisition policy without side effects."""
    policy = _egress_access_decision(state)["policy"]
    return {
        "status": "success",
        "egress": policy,
        "supported_kinds": ["file", "git"],
        "size_limits": {
            "default_max_bytes": _DEFAULT_MAX_BYTES,
            "max_max_bytes": _MAX_MAX_BYTES,
        },
        "timeout_limits": {
            "default_seconds": _DEFAULT_TIMEOUT_SECONDS,
            "max_seconds": _MAX_TIMEOUT_SECONDS,
        },
        "credential_policy": (
            "只支持公开 HTTPS 源：URL 不能携带用户名/密码，私有仓库、SSH、"
            "token 等任何凭据形式均不支持；下载进程只能写空 staging 目录"
            "（原生后端不隔离读取，按域名放行是本工具联网前的自查）。"
        ),
    }


register_tool(
    ToolDefinition(
        name="describe_acquisition_capabilities",
        description=(
            "只读查询 fetch_resource 当前的运行时生效获取能力：部署 egress 域名白名单、"
            "本 run 已获精确主机授权（两者匹配规则分开披露）、单文件/工作树大小上限、超时上限、支持的获取类型"
            "（file/git）和无凭证限制。规划数据源前先查我，据此选择白名单内的"
            "镜像域名与合规大小，避免用失败下载试探策略。无任何副作用。"
        ),
        parameters_schema={"type": "object", "properties": {}},
        allowed_node_types=["experiment"],
        risk_level="low",
        replayable_read=True,
    ),
    _describe_acquisition_capabilities,
)

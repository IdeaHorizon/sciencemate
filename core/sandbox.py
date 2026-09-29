"""模型命令的资源限额、attempt 能力记录与可写根 —— 执行器分档后留下来的那部分。

2026-09-04 之前这里是 2,800 行 Docker 控制面（常驻容器、supervisor 协议、准入账本、
4 Hz 资源探针、镜像身份钉死）。wangd 拍板拆除（docs/RFC_EXECUTOR_TIERS_20260904.md，
PR C）：墙由 ``core.isolation`` 的原生后端守（darwin seatbelt / linux Landlock+bwrap+
cgroup），Docker 一行不留。本模块只剩三样与后端无关的东西：

* **限额**（:class:`SandboxLimits` / :class:`SandboxCeiling` / :func:`limits_for_profile`）：
  模型选 ``resource_profile``，linux 后端把它翻成 cgroup 的 MemoryMax / TasksMax / CPUQuota。
* **attempt 能力记录**（:class:`SandboxManifest`）：一个 RunAttempt 出生时冻结"谁来守、
  哪些根可写、天花板多高、授权了哪些高危类"。平台冻一份进 DB，worker 校验 hash。
  v1–v3 是 Docker 年代的 payload（含 image_id 等），解析仍认、hash 不变，新铸的是 v4。
* **可写根**（:func:`write_roots_for` 等）：自己的节点目录 + run-local（+ manifest 里
  显式 rw 的项目资源）；整个 worktree 是拒写层。

文件末尾是给 experiment 节点留的**兼容垫片**：那些以 Docker 容器为前提的调用
（``inspect_container`` / ``stop_container`` / ``image_name`` …）不再有真身，返回
"不存在"或抛 :class:`SandboxUnavailable`，迁移登记在 issue #793。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from shared.lib.json_file import read_object, write_object

DEFAULT_EGRESS_ALLOWLIST = (
    "pypi.org,files.pythonhosted.org,github.com,githubusercontent.com,zenodo.org"
)
"""取物工具（resource_fetch）自己核对目标域名用的默认白名单。原生后端的取物段直接给网，
这份清单是策略不是墙。

⚠️ 这五个域**全部是装软件用的，没有一个科学数据源**。所以它不是"够用的默认"，它是
"每个新数据源都要走一遍人肉流程"的起点 —— 见 :func:`effective_egress_policy`。"""

EGRESS_ALLOWLIST_ENV = "HARNESS_SANDBOX_EGRESS_ALLOWLIST"
"""部署期配置的白名单环境变量。运行期的增补走授权通道，不改这个变量。"""


def effective_egress_policy(state: Any = None) -> dict[str, Any]:
    """取物工具核对目标域名用的**生效**白名单 —— 部署 env（或内置默认）∪ 已授权 grant。

    **`state` 不是可选装饰**：授权按 run 计，不落盘、不进环境（`capability_grants`
    的第一条），所以"这一刻准连哪些主机"这个问题只有拿着那个 run 的 state 才答得出。
    不传 state 的调用问的是另一个问题（部署基线是什么），答案里 `granted` 恒空。
    两个问题分开，是因为把它们压成一个的后果正是这条 issue：函数号称是唯一真相源，
    而它连"谁的 run"都不知道。

    **这是这个问题的唯一权威答案。** 此前它有两份：core 一份、节点里
    ``resource_fetch._effective_egress_policy`` 一份"照着 core 再算一遍"的抄件（其
    docstring 还写着"Resolve the egress allowlist exactly as core.sandbox does" ——
    而 core 那边的 egress 代理已经随 Docker 一起删干净了，抄件在镜像一个不存在的东西）。
    两份抄件就有两个各自演化的答案，且分叉时不报错
    （[[feedback_one_truth_source_per_question]]）。

    grant 那一半的存在理由（#770）：agent 撞上白名单时，唯一的出路是"人肉找部署方改
    env、重启、回来重新派发 run"。授权通道把它变成"在 chat 里点一次头，机械生效"。
    grant 只能**追加主机**，改不了沙箱其它任何参数；主机形状由
    ``core.capability_grants.normalize_host`` 收口（按 ``urlsplit`` 解析，去端口、
    去 userinfo、小写；逐个主机比，不做父域匹配）。
    """
    from core import capability_grants

    raw_env = os.environ.get(EGRESS_ALLOWLIST_ENV)
    if raw_env is None:
        base = DEFAULT_EGRESS_ALLOWLIST.strip()
        source = f"builtin_default（{EGRESS_ALLOWLIST_ENV} 未设置，运行时回退内置默认）"
    else:
        base = raw_env.strip()
        source = f"environment:{EGRESS_ALLOWLIST_ENV}"
    from_environment = [item.strip() for item in base.split(",") if item.strip()]
    # 已授权的主机只能从**本 run 的 state** 上取 —— 授权是 run 级的
    # （不落盘、不进环境），没有 state 就没有"已授权"这回事。
    #
    # 2026-09-21（#1068）：这里原来调的是 `capability_grants.granted_values(
    # capability_grants.CAPABILITY_EGRESS_DOMAIN)` —— **那两个符号在这个模块里
    # 不存在**（第一版接口，09-15 合并时留下的），异常被一个宽 except 吞掉，于是
    # 生效授权集合恒为空：人点了"允许"，白名单一动不动。
    #
    # 所以现在**不吞**：拿到 state 却读不出授权，那是接口对不上，得当场炸出来。
    # 没有 state 是另一回事（部署基线查询、启动自检），那种调用合法，授权为空。
    granted: list[str] = []
    if state is not None:
        granted = [h for h in capability_grants.granted_hosts(state) if h]
    entries = list(dict.fromkeys([*from_environment, *granted]))
    if granted:
        source += f" + {len(granted)} 条已授权 grant"
    return {
        "env_var": EGRESS_ALLOWLIST_ENV,
        "raw": ",".join(entries),
        "entries": entries,
        "from_environment": from_environment,
        "granted": granted,
        "source": source,
    }


class SandboxUnavailable(RuntimeError):
    pass


class SandboxContractError(ValueError):
    pass


# ── 限额 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SandboxLimits:
    """Finite limits applied to every model-controlled process."""

    memory_bytes: int = 4 * 1024**3
    cpus: float = 2.0
    pids: int = 128
    walltime_seconds: int = 600
    storage_bytes: int = 8 * 1024**3
    storage_entries: int = 100_000
    output_bytes: int = 16 * 1024**2
    tmpfs_bytes: int = 256 * 1024**2

    def validate(self) -> None:
        integer_limits = {
            "memory_bytes": self.memory_bytes,
            "pids": self.pids,
            "walltime_seconds": self.walltime_seconds,
            "storage_bytes": self.storage_bytes,
            "storage_entries": self.storage_entries,
            "output_bytes": self.output_bytes,
            "tmpfs_bytes": self.tmpfs_bytes,
        }
        for name, value in integer_limits.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SandboxContractError(f"{name} must be a positive integer")
        if not isinstance(self.cpus, (int, float)) or isinstance(self.cpus, bool) or self.cpus <= 0:
            raise SandboxContractError("cpus must be a positive number")
        if self.memory_bytes < 64 * 1024**2:
            raise SandboxContractError("memory_bytes must be at least 64 MiB")
        if self.pids > 4096:
            raise SandboxContractError("pids must not exceed 4096")
        if self.storage_entries > 1_000_000:
            raise SandboxContractError("storage_entries must not exceed 1000000")
        if self.output_bytes > self.storage_bytes:
            raise SandboxContractError("output_bytes must not exceed storage_bytes")


@dataclass(frozen=True)
class SandboxCeiling:
    """Maximum allocation an attempt may reach without changing capability."""

    memory_bytes: int = 32 * 1024**3
    cpus: float = 8.0
    pids: int = 1024
    walltime_seconds: int = 4 * 60 * 60
    storage_bytes: int = 64 * 1024**3
    storage_entries: int = 500_000
    output_bytes: int = 64 * 1024**2
    tmpfs_bytes: int = 1024 * 1024**2

    @classmethod
    def from_environment(cls) -> SandboxCeiling:
        """Read one operator-owned ceiling object; models cannot mutate it."""
        raw = os.environ.get("HARNESS_SANDBOX_CEILING", "").strip()
        if not raw:
            ceiling = cls()
        else:
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise SandboxContractError("HARNESS_SANDBOX_CEILING must be a JSON object") from exc
            defaults = asdict(cls())
            if not isinstance(payload, dict) or not set(payload).issubset(defaults):
                raise SandboxContractError("HARNESS_SANDBOX_CEILING contains unknown fields")
            try:
                ceiling = cls(**{**defaults, **payload})
            except TypeError as exc:
                raise SandboxContractError("HARNESS_SANDBOX_CEILING has invalid values") from exc
        ceiling.validate()
        if not ceiling.admits(SandboxLimits()):
            raise SandboxContractError(
                "HARNESS_SANDBOX_CEILING must admit the default 4GiB/2CPU/"
                "128-pid/600-second/8GiB profile"
            )
        return ceiling

    def validate(self) -> None:
        SandboxLimits(**asdict(self)).validate()

    def admits(self, limits: SandboxLimits) -> bool:
        return (
            limits.memory_bytes <= self.memory_bytes
            and limits.cpus <= self.cpus
            and limits.pids <= self.pids
            and limits.walltime_seconds <= self.walltime_seconds
            and limits.storage_bytes <= self.storage_bytes
            and limits.storage_entries <= self.storage_entries
            and limits.output_bytes <= self.output_bytes
            and limits.tmpfs_bytes <= self.tmpfs_bytes
        )


def limits_for_profile(profile: str | None, *, walltime_seconds: int) -> SandboxLimits:
    """Translate the model-visible initial-size choice into finite limits."""
    name = str(profile or "standard").strip().lower()
    values = {
        "small": (1024**3, 1.0, 64, 4 * 1024**3, 8 * 1024**2),
        "standard": (4 * 1024**3, 2.0, 128, 8 * 1024**3, 16 * 1024**2),
        "large": (16 * 1024**3, 4.0, 512, 32 * 1024**3, 32 * 1024**2),
        "xlarge": (32 * 1024**3, 8.0, 1024, 64 * 1024**3, 64 * 1024**2),
    }
    if name not in values:
        raise SandboxContractError("resource_profile must be small, standard, large, or xlarge")
    memory, cpus, pids, storage, output = values[name]
    limits = SandboxLimits(
        memory_bytes=memory,
        cpus=cpus,
        pids=pids,
        walltime_seconds=max(1, int(walltime_seconds)),
        storage_bytes=storage,
        output_bytes=output,
        tmpfs_bytes=min(1024**3, max(256 * 1024**2, memory // 16)),
    )
    limits.validate()
    return limits


# ── 路径工具 ──────────────────────────────────────────────────────────────────


def _canonical_existing(path: Path | str | None) -> Path | None:
    if path is None or str(path).strip() == "":
        return None
    try:
        candidate = Path(path).expanduser().resolve(strict=True)
    except (OSError, ValueError):
        return None
    return candidate


def _dedupe_paths(paths: Sequence[Path | str]) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        path = _canonical_existing(raw)
        if path is None:
            raise SandboxContractError(f"sandbox mount does not exist: {raw}")
        if path not in result:
            result.append(path)
    return result


def _contains(root: Path, candidate: Path) -> bool:
    return candidate == root or candidate.is_relative_to(root)


def _minimal_roots(paths: Sequence[Path], *, carve_outs: Sequence[Path] = ()) -> list[Path]:
    """折叠冗余的嵌套根 —— 但**不折叠**被另一种根隔开的那些。

    ``A ⊃ B`` 且两者同一种模式时，B 通常是冗余的。可一旦有一条**另一种**模式的根 C
    满足 ``A ⊃ C ⊃ B``，B 就不再冗余：它是一处 carve-out，删掉这条就再没人表达得了
    这条边界。#899-C 实测：``writable=[<wt>/experiment/runtime]``、
    ``readonly=[<wt>, <state.root>, <wt>/experiment/runtime/deps]`` 时，
    ``_minimal_roots(readonly)`` 返回 ``['<wt>', '<state.root>']`` —— ``deps`` 那条**整个
    消失**，后端根本收不到它，于是即便绑定次序修好了，``submit_job(local)`` 这条路上
    声明的只读洞也永远兑现不了。

    ``carve_outs`` 传另一种模式的全部根（给 readonly 折叠时传 writable，反之亦然）。
    """
    result: list[Path] = []
    for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
        redundant = False
        for parent in result:
            if not _contains(parent, path):
                continue
            if any(
                divider != parent and divider != path
                and _contains(parent, divider) and _contains(divider, path)
                for divider in carve_outs
            ):
                continue  # 中间隔着另一种模式的根 → 这条不是冗余，是一处 carve-out
            redundant = True
            break
        if not redundant:
            result.append(path)
    return result


_FORBIDDEN_MOUNTS = {
    Path(value).resolve(strict=False)
    for value in (
        "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/proc",
        "/root", "/run", "/sbin", "/sys", "/usr", "/var",
    )
}
_SENSITIVE_HOST_ROOTS = tuple(
    Path(value).resolve(strict=False)
    for value in (
        "/boot", "/dev", "/etc", "/proc", "/root", "/run", "/sys", "/usr", "/bin", "/sbin",
        "/lib", "/lib64",
    )
)


def _validate_mount(path: Path) -> None:
    """一个可写根能不能是这个路径：机器级目录一律不行。"""
    if path in _FORBIDDEN_MOUNTS:
        raise SandboxContractError(f"host-wide mount is forbidden: {path}")
    if any(_contains(root, path) for root in _SENSITIVE_HOST_ROOTS):
        raise SandboxContractError(f"sensitive host mount is forbidden: {path}")
    if any(ch in str(path) for ch in (",", "\n", "\r")):
        raise SandboxContractError(f"unsupported character in mount path: {path}")


# ── attempt 能力记录 ──────────────────────────────────────────────────────────

_LEGACY_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class SandboxManifest:
    """Immutable capability contract owned by one RunAttempt.

    v4（现行）：attempt_id / run_id / mounts / ceiling / authorized_risk_classes / backend。
    v1–v3 是 Docker 年代的 payload，多出 image_id / network_mode / security_profile /
    queue_limit；解析仍接受、canonical payload 按版本原样重放，所以老 attempt 的 hash 不变。
    """

    attempt_id: str
    run_id: str
    mounts: tuple[tuple[str, str], ...]
    ceiling: SandboxCeiling = SandboxCeiling()
    authorized_risk_classes: tuple[str, ...] = ()
    backend: str = "auto"
    version: int = 4
    # ── v1–v3 遗留字段，只为重放老 payload 的 hash ──
    image_id: str = ""
    network_mode: str = "none"
    security_profile: str = "hardened"
    """v4 不进 payload。含义沿用 Docker 年代：hardened = 每条命令按自己的根收窄写面 ——
    原生后端（seatbelt / Landlock / bwrap）本来就是逐条命令构造墙，所以恒为 hardened；
    experiment 的预检认的就是这个词。"""
    queue_limit: int = 8

    def canonical_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self.version,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "mounts": [{"path": path, "mode": mode} for path, mode in self.mounts],
            "ceiling": asdict(self.ceiling),
            "authorized_risk_classes": list(self.authorized_risk_classes),
        }
        if self.version <= 3:
            payload["image_id"] = self.image_id
            payload["network_mode"] = self.network_mode
            payload["queue_limit"] = self.queue_limit
            if self.version >= 2:
                payload["security_profile"] = self.security_profile
            if self.version >= 3:
                payload["backend"] = self.backend
        else:
            payload["backend"] = self.backend
        return payload

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def validate(self) -> None:
        if self.version not in {1, 2, 3, 4}:
            raise SandboxContractError("unsupported sandbox manifest version")
        if not self.backend or "\x00" in self.backend:
            raise SandboxContractError("invalid sandbox backend")
        if self.version <= 3:
            # Docker 年代的 payload：只校验它自己的形状，不再要求镜像真的存在。
            if self.backend == "image" and not _LEGACY_IMAGE_ID.fullmatch(self.image_id):
                raise SandboxContractError("sandbox manifest has no immutable image id")
            if self.backend != "image" and self.version < 3:
                raise SandboxContractError("non-image backends need a v3 sandbox manifest")
        elif self.backend == "image":
            raise SandboxContractError(
                "the image backend was removed; a v4 manifest names a native backend"
            )
        if not self.attempt_id or len(self.attempt_id) > 128 or "\x00" in self.attempt_id:
            raise SandboxContractError("invalid sandbox attempt id")
        if not self.run_id or len(self.run_id) > 128 or "\x00" in self.run_id:
            raise SandboxContractError("invalid sandbox run id")
        if tuple(sorted(set(self.authorized_risk_classes))) != self.authorized_risk_classes:
            raise SandboxContractError("sandbox authorized_risk_classes must be sorted and unique")
        if not all(isinstance(item, str) and item for item in self.authorized_risk_classes):
            raise SandboxContractError("invalid sandbox authorized_risk_classes")
        self.ceiling.validate()
        if not self.mounts:
            raise SandboxContractError("sandbox manifest has no mounts")
        ordered = tuple(sorted(self.mounts, key=lambda item: (len(Path(item[0]).parts), item[0])))
        mount_paths = [path for path, _mode in self.mounts]
        if self.mounts != ordered or len(set(mount_paths)) != len(mount_paths):
            raise SandboxContractError("sandbox manifest mounts must be unique and parent-first")
        for raw, mode in self.mounts:
            path = _canonical_existing(raw)
            if path is None:
                raise SandboxContractError(f"sandbox manifest mount does not exist: {raw}")
            _validate_mount(path)
            if mode not in {"ro", "rw"}:
                raise SandboxContractError(f"invalid sandbox manifest mount mode: {mode}")


def _coerce_ceiling(raw: Mapping[str, Any] | None) -> SandboxCeiling:
    if raw is None:
        return SandboxCeiling.from_environment()
    defaults = asdict(SandboxCeiling())
    if not set(raw).issubset(defaults):
        raise SandboxContractError("sandbox manifest ceiling contains unknown fields")
    try:
        ceiling = SandboxCeiling(**{**defaults, **dict(raw)})
    except TypeError as exc:
        raise SandboxContractError("sandbox manifest ceiling has invalid values") from exc
    ceiling.validate()
    return ceiling


def parse_manifest(payload: Mapping[str, Any]) -> SandboxManifest:
    mounts: list[tuple[str, str]] = []
    for item in payload.get("mounts") or []:
        if not isinstance(item, Mapping):
            raise SandboxContractError("invalid sandbox manifest mount")
        raw_path = str(item.get("path") or "")
        path = _canonical_existing(raw_path)
        mounts.append((str(path) if path is not None else raw_path, str(item.get("mode") or "")))
    version = int(payload.get("version") or 0)
    legacy_profile = str(payload.get("security_profile") or ("hardened" if version == 1 else ""))
    if version <= 3 and version >= 2 and legacy_profile not in {"portable", "hardened", "native"}:
        raise SandboxContractError("invalid sandbox security profile")
    if version == 1 and legacy_profile != "hardened":
        raise SandboxContractError("legacy sandbox manifests require hardened security")
    manifest = SandboxManifest(
        attempt_id=str(payload.get("attempt_id") or ""),
        run_id=str(payload.get("run_id") or ""),
        mounts=tuple(mounts),
        ceiling=_coerce_ceiling(
            payload.get("ceiling") if isinstance(payload.get("ceiling"), Mapping) else None
        ),
        authorized_risk_classes=tuple(
            str(item) for item in (payload.get("authorized_risk_classes") or [])
        ),
        backend=str(payload.get("backend") or ("image" if version <= 3 else "")),
        version=version,
        image_id=str(payload.get("image_id") or ""),
        network_mode=str(payload.get("network_mode") or "none"),
        security_profile=legacy_profile or "hardened",
        queue_limit=int(payload.get("queue_limit") or 8),
    )
    manifest.validate()
    return manifest


# ── 可写根 ────────────────────────────────────────────────────────────────────


def _manifest_rw_mounts(state: Any) -> list[Path]:
    """平台冻进 manifest 的显式 rw 项目资源（dataset / storage）。Docker 年代靠挂载给容器，
    原生后端靠把它们加进可写根。"""
    raw = getattr(state, "sandbox_manifest", None)
    if not isinstance(raw, Mapping):
        return []
    out: list[Path] = []
    for item in raw.get("mounts") or []:
        if isinstance(item, Mapping) and item.get("mode") == "rw":
            path = _canonical_existing(str(item.get("path") or ""))
            if path is not None:
                out.append(path)
    return out


def write_roots_for(state: Any) -> list[Path]:
    """Return the node-owned workspace and run-owned state directory (+ explicit rw resources)."""
    roots: list[Path] = []
    worktree = getattr(state, "project_worktree", None)
    if worktree is not None:
        relative = str(getattr(state, "workspace_relative_path", "") or "")
        if relative and Path(relative).suffix:
            scoped = _canonical_existing(Path(worktree) / relative)
        else:
            scoped = _canonical_existing(getattr(state, "workspace_root", None))
        if scoped is not None:
            roots.append(scoped)
    run_root = _canonical_existing(getattr(state, "root", None))
    if run_root is not None:
        roots.append(run_root)
    if not roots:
        raise SandboxContractError("run has no writable sandbox root")
    worktree_root = _canonical_existing(worktree) if worktree is not None else None
    for path in _manifest_rw_mounts(state):
        # 整个 worktree 被平台冻成 rw 是常态，但节点只能写自己的目录 —— worktree 内
        # 的 rw 挂载不放宽；worktree 外的显式资源（数据集落盘目录）才加进来。
        if worktree_root is not None and _contains(worktree_root, path):
            continue
        roots.append(path)
    return _minimal_roots(roots)


def readonly_overrides_for(state: Any) -> list[Path]:
    worktree = _canonical_existing(getattr(state, "project_worktree", None))
    return [worktree] if worktree is not None else []


def model_tool_roots(state: Any) -> tuple[list[Path], list[Path]]:
    """The model-tool boundary is mandatory, including unattended runs."""
    return write_roots_for(state), readonly_overrides_for(state)


# ── 本地铸 manifest（CLI 路径）与继承判据 ─────────────────────────────────────


def _local_manifest(
    state: Any,
    writable_roots: Sequence[Path],
    readonly_roots: Sequence[Path],
) -> SandboxManifest:
    identity = ":".join(
        str(value or "local")
        for value in (
            getattr(state, "tenant_id", None),
            getattr(state, "project_id", None),
            getattr(state, "session_id", None),
            getattr(state, "run_id", None),
        )
    )
    base_writable = write_roots_for(state)
    base_readonly = readonly_overrides_for(state)
    raw_writable = [*base_writable, *writable_roots]
    raw_readonly = [*base_readonly, *readonly_roots]
    writable = _minimal_roots(raw_writable, carve_outs=raw_readonly)
    readonly = _minimal_roots(raw_readonly, carve_outs=raw_writable)
    # 此前这里还有一句"把被某个 writable 根包含的 readonly 根剔除"。那句话把
    # carve-out 当成了冗余：冻结的 attempt manifest 因此对这条边界**保持沉默**，
    # `_effective_mode` 随后把该路径报成 "rw"（#899-C）。谁都不该拿那份 manifest
    # 当"这条边界不存在"的旁证 —— 现在它如实带着这条 ro。
    mounts: list[tuple[str, str]] = []
    for path in readonly:
        mounts.append((str(path), "ro"))
    for path in writable:
        mounts.append((str(path), "rw"))
    mounts.sort(key=lambda item: (len(Path(item[0]).parts), item[0]))
    from core import isolation

    return SandboxManifest(
        attempt_id=f"local:{identity}",
        run_id=str(getattr(state, "run_id", "") or identity),
        mounts=tuple(mounts),
        ceiling=SandboxCeiling.from_environment(),
        backend=isolation.attempt_capability()["backend"],
    )


def _effective_mode(manifest_roots: Sequence[tuple[Path, str]], path: Path) -> str | None:
    """最具体的那条挂载说了算（rw 父目录不能把 ro 子目录放宽）。"""
    matches = [
        (len(root.parts), mode) for root, mode in manifest_roots if _contains(root, path)
    ]
    return max(matches)[1] if matches else None


def uncovered_write_roots(raw: Mapping[str, Any], state: Any) -> list[Path]:
    """`state` 自己的可写根里，`raw` 这份冻结 manifest **没有**以 rw 覆盖的那些。"""
    try:
        manifest = parse_manifest(raw)
        roots = write_roots_for(state)
    except SandboxContractError:
        return []
    manifest_roots = [(Path(path), mode) for path, mode in manifest.mounts]
    return [path for path in roots if _effective_mode(manifest_roots, path) != "rw"]


def manifest_for(
    state: Any,
    *,
    writable_roots: Sequence[Path | str],
    readonly_roots: Sequence[Path | str] = (),
) -> SandboxManifest:
    """Return the attempt-born contract; per-command roots may only narrow it."""
    raw_writable = _dedupe_paths(writable_roots)
    raw_readonly = _dedupe_paths(readonly_roots)
    writable = _minimal_roots(raw_writable, carve_outs=raw_readonly)
    readonly = _minimal_roots(raw_readonly, carve_outs=raw_writable)
    raw = getattr(state, "sandbox_manifest", None)
    manifest = (
        parse_manifest(raw)
        if isinstance(raw, Mapping)
        else _local_manifest(state, writable, readonly)
    )
    if not isinstance(raw, Mapping):
        setattr(state, "sandbox_manifest", manifest.canonical_payload())
        setattr(state, "sandbox_manifest_hash", manifest.sha256)
    declared_hash = str(getattr(state, "sandbox_manifest_hash", "") or "")
    if declared_hash and declared_hash != manifest.sha256:
        raise SandboxContractError("sandbox manifest identity mismatch")
    manifest_roots = [(Path(path), mode) for path, mode in manifest.mounts]
    for path in writable:
        if _effective_mode(manifest_roots, path) != "rw":
            raise SandboxContractError(f"writable root was not frozen into this RunAttempt: {path}")
    for path in readonly:
        if _effective_mode(manifest_roots, path) is None:
            raise SandboxContractError(
                f"read-only root was not frozen into this RunAttempt: {path}"
            )
    return manifest


# ── 兼容垫片（Docker 年代的调用面）──────────────────────────────────────────────
#
# experiment 节点（owner lujy）仍按容器模型调这些名字：本地作业 = 一个 Docker 容器，
# 用 inspect / stop / image 身份对账。容器没有了，这些名字留下来只为两件事：
#   1. import 不炸（节点整个模块级 import 它们）；
#   2. 语义如实：没有容器 → "不存在"；要起容器 → SandboxUnavailable 指向 #793。
# 前台命令的两个入口（prepare_attempt_command / prepare_launch）改成走原生后端，
# 返回的 Launch.argv 自带 cd，所以 `subprocess.run(launch.argv)` 这种老用法照样在墙内。

_REMOVED = "the Docker sandbox was removed (RFC_EXECUTOR_TIERS PR C); see issue #793"


def availability(*, refresh: bool = False) -> tuple[bool, str]:
    """这台机器上有没有后端能守写边界。老调用方问的是"Docker 在不在"，现在答的是
    "原生后端在不在"。"""
    del refresh
    from core import isolation

    try:
        backend = isolation.select_backend()
    except isolation.IsolationContractError as exc:
        return False, str(exc)
    if isolation.Invariant.WRITE_BOUNDARY not in backend.capabilities():
        return False, getattr(backend, "unavailable_reason", "") or "unavailable"
    return True, ""


def require_available() -> None:
    ok, reason = availability()
    if not ok:
        raise SandboxUnavailable(f"execution boundary unavailable: {reason}")


def effective_security_profile() -> str:
    """Docker 年代的词：hardened = 每条命令按自己的根收窄写面。原生后端逐条命令构造墙，
    所以只要它守得住写边界就是 hardened；守不住时报 portable（experiment 预检会拒）。"""
    ok, _reason = availability()
    return "hardened" if ok else "portable"


def trusted_image_id() -> str:
    return ""


def image_name() -> str:
    return ""


def _native_launch(
    payload_argv: Sequence[str],
    *,
    state: Any,
    cwd: Path | str,
    writable_roots: Sequence[Path | str],
    readonly_roots: Sequence[Path | str],
    limits: SandboxLimits | None,
    network_access: bool,
    environment: Mapping[str, str] | None,
):
    from core import isolation

    if not payload_argv or any(
        not isinstance(part, str) or "\x00" in part for part in payload_argv
    ):
        raise SandboxContractError("payload argv must be a non-empty string sequence")
    applied = limits or SandboxLimits()
    applied.validate()
    raw_writable = _dedupe_paths(writable_roots)
    raw_readonly = _dedupe_paths(readonly_roots)
    writable = _minimal_roots(raw_writable, carve_outs=raw_readonly)
    readonly = _minimal_roots(raw_readonly, carve_outs=raw_writable)
    for path in [*writable, *readonly]:
        _validate_mount(path)
    workdir = _canonical_existing(cwd)
    if workdir is None or not workdir.is_dir():
        raise SandboxContractError(f"sandbox cwd does not exist or is not a directory: {cwd}")
    backend = isolation.select_backend()
    spec = isolation.CommandSpec(
        argv=tuple(payload_argv),
        cwd=str(workdir),
        writable_roots=tuple(writable),
        readonly_roots=tuple(readonly),
        limits=applied,
        environment=environment,
        network_access=bool(network_access),
    )
    launch = backend.prepare(spec, state=state)
    # 老调用方拿 argv 自己 subprocess.run，不会应用 launch.cwd —— 把 cd 编进 argv。
    from shared.lib.shell import posix_shell

    launch.argv = [posix_shell(), "-c", 'cd -- "$0" && exec "$@"', str(workdir), *launch.argv]
    return launch


def prepare_attempt_command(
    payload_argv: Sequence[str],
    *,
    state: Any,
    cwd: Path | str,
    writable_roots: Sequence[Path | str],
    readonly_roots: Sequence[Path | str] = (),
    limits: SandboxLimits | None = None,
    environment: Mapping[str, str] | None = None,
):
    """兼容入口：前台模型命令，现在直接由原生后端包一层。新代码请走 spawn_and_wait。"""
    return _native_launch(
        payload_argv, state=state, cwd=cwd, writable_roots=writable_roots,
        readonly_roots=readonly_roots, limits=limits, network_access=False,
        environment=environment,
    )


# ── GPU：可见 / 可调度 / 可受管执行是三件不同的事 ─────────────────────────────


GPU_ISOLATION_NONE = "none"
"""整卡都没有独占契约 —— 不是 MIG，也不是 vGPU，更不是"显存分区"。"""


def _visible_gpus() -> tuple[list[dict[str, str]], str]:
    """宿主上**看得见**几张卡。看得见只证明 ``visible``，不证明任何别的。

    用稳定的 device UUID 而不是可变的 index：index 会随 ``CUDA_VISIBLE_DEVICES`` 与
    驱动枚举顺序变，拿它做身份对账迟早对错人。
    """
    import shutil
    import subprocess

    exe = shutil.which("nvidia-smi")
    if not exe:
        return [], "nvidia-smi not installed"
    try:
        out = subprocess.run(
            [exe, "--query-gpu=uuid,name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"nvidia-smi failed: {exc}"
    if out.returncode != 0:
        return [], (out.stderr or "nvidia-smi returned non-zero").strip()[:200]
    devices: list[dict[str, str]] = []
    for line in (out.stdout or "").splitlines():
        parts = [piece.strip() for piece in line.split(",", 1)]
        if len(parts) == 2 and parts[0]:
            devices.append({"uuid": parts[0], "name": parts[1]})
    return devices, ""


def gpu_capability() -> dict[str, Any]:
    """受管 GPU 执行能力的**结构化事实** —— 给规划者读，不用它去猜。

    #893 实测：``discover_resources`` 列出 4×A100 且 local scheduler
    ``available=true``，Slurm 分区 ``GRES=(null)``；agent 花了多轮探测 Python /
    Torch / CUDA，进入高风险人工审批，人点了"批准执行"，然后
    ``submit_job(scheduler=local, gpus=1)`` 在 Core admission 被稳定拒绝：
    ``local GPU execution is not provided by the native backends``。

    没有 job id、没有 runtime id、没有 workload exit code —— 一个**在启动前就能确定**
    的结论，被推到了不可逆审批之后。这不是模型选错参数，是能力面与执行面互相矛盾：
    API 收 ``gpus``、资源发现显示 4 卡、执行层永久拒绝。

    所以这里把三条轴分开答，谁都不必从设备枚举推executability：

    * ``visible_gpus`` —— 宿主探测到了什么（``nvidia-smi``）；
    * ``schedulable_gpus`` —— 当前调度域能分配几张；
    * ``managed_local_gpu_execution`` —— 受管执行契约能否安全兑现；
    * ``gpu_isolation`` —— ``none`` / ``whole_device`` / ``mig`` / ``vgpu`` / ``gres``。

    当前原生后端的答案是 **False**，而且原因是结构性的：``core.isolation.CommandSpec``
    只有 argv / cwd / roots / limits / environment / network，**没有** GPU 数量、device
    identity、租约、``CUDA_VISIBLE_DEVICES``、MIG identity 或释放/对账字段。native data
    plane 确实没有办法兑现 ``gpus=1``（PR C 删 Docker 时只改了错误文案，没有补 contract）。

    这个函数是 #893 阶段 0 的全部：**先让能力声明诚实**，与开不开放 local GPU 无关。
    阶段 1（strict/shared 还是 attended 整卡租约）需要拍板，落地后这里的
    ``managed_local_gpu_execution`` 变 True、``gpu_isolation`` 说出到底是哪种隔离，
    调用方逻辑一行不用改 —— 这正是把它做成结构化事实而不是一句错误文案的理由。
    """
    devices, why = _visible_gpus()
    return {
        "visible_gpus": len(devices),
        "visible_devices": devices,
        "schedulable_gpus": 0,
        "managed_local_gpu_execution": False,
        "gpu_isolation": GPU_ISOLATION_NONE,
        "reason": (
            "the native executor has no GPU allocation contract: core.isolation."
            "CommandSpec carries no device identity, lease, or release/reconcile "
            "fields, so a managed local GPU job cannot be granted or accounted for"
        ),
        "visibility_note": why or f"nvidia-smi reports {len(devices)} device(s)",
        "tracking_issue": "#893",
    }


def sandbox_namespace() -> str:
    """作业归属的部署命名空间。Docker 年代用它隔离容器名；原生作业只当标签记着。"""
    value = os.environ.get("HARNESS_SANDBOX_NAMESPACE", "harness").strip().lower()
    return value or "harness"


# ── 原生 detached 作业（替代 Docker Job 容器）──────────────────────────────────
#
# experiment 的 submit_job scheduler=local 按容器契约工作：prepare_launch(detached=True)
# 给一个 launch（container_name / control_dir / image_id / argv），跑 argv 拿回不可变
# runtime id，之后按名字或 id inspect / stop。这里用 core/isolation/_native_job.py 的
# 脱离终端进程组给这份契约一个原生真身；record.json 是作业事实账。

_JOB_NAME_RE = re.compile(r"hf-job-[0-9a-f]{16}")
_NATIVE_JOB = Path(__file__).resolve().parent / "isolation" / "_native_job.py"


def _jobs_root() -> Path:
    import tempfile

    root = Path(os.environ.get("HARNESS_JOBS_ROOT") or (Path(tempfile.gettempdir()) / "hf-jobs"))
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass
class NativeJobLaunch:
    argv: list[str]
    container_name: str
    control_dir: Path
    image_id: str = ""
    detached: bool = True

    def terminate(self, *, remove: bool = True) -> bool:
        return stop_container(self.container_name, remove=remove)

    def cleanup(self) -> None:
        # 作业已经起来的话 record 在，控制目录是它的账本，留着；没起来的才清。
        record = self.control_dir / "record.json"
        if not record.exists() or not read_object(record).get("pid"):
            cleanup_control_dir(self.control_dir)


def _job_record(ref: str) -> tuple[Path, dict[str, Any]] | None:
    """按作业名或 runtime id 找账本。"""
    root = _jobs_root()
    candidates = [root / ref] if _JOB_NAME_RE.fullmatch(ref) else list(root.glob("hf-job-*"))
    for control_dir in candidates:
        record_path = control_dir / "record.json"
        if not record_path.exists():
            continue
        try:
            record = read_object(record_path)
        except ValueError:
            continue
        if record.get("name") == ref or record.get("runtime_id") == ref:
            return control_dir, record
    return None


def _pid_alive(pid: int) -> bool:
    from shared.lib import process_control

    return pid > 0 and process_control.alive(pid)



# ── GPU：看得见 ≠ 跑得了（#893）────────────────────────────────────────────────


@dataclass(frozen=True)
class GpuCapability:
    """这台机器的 GPU 到底处在什么状态 —— **一个问题一个答案**。

    起因（#893，node20 真机 E2E）：资源画像报告 local ``available=true`` 且可见
    4×A100，而 ``submit_job(gpus=1)`` 在创建 job identity **之前**稳定拒绝。
    模型据此做了错误决定：它读到"有 4 张卡"，于是一路规划到提交才撞墙，
    而那时人已经批过一次不可逆的审批了。

    根子不是缺 GPU，是**一个 ``available`` 字段同时回答了两个不同的问题** ——
    「我看得见卡吗」和「我能通过受管入口用上它吗」。这两件事在拆掉 Docker 之后
    就分家了：卡还在，受管执行的那条路没了。

    所以这里把它拆开，每条各自可判：

    * ``visible``      —— ``nvidia-smi`` 数得出来几张。``None`` = 查不出来
      （没有 nvidia-smi / 容器里看不见），**不是 0**。查不出来不许当成没有。
    * ``schedulable``  —— 受管入口今天能不能给你分一张。
    * ``isolated``     —— 分了之后，别的 run 能不能看见/抢走你这张。
    * ``reason``       —— 不能用时，一句给人看的话。

    **这个答案在提交之前、在问人之前就能确定** —— 这正是 #893 的代价所在：
    它被推到了不可逆审批之后才说出口。
    """

    visible: int | None
    schedulable: bool
    isolated: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "visible": self.visible,
            "schedulable": self.schedulable,
            "isolated": self.isolated,
            "reason": self.reason,
        }

    def sentence(self) -> str:
        """给模型和人读的一句话。看得见几张、能不能用、为什么。"""
        seen = ("看不出来（没有 nvidia-smi）" if self.visible is None
                else f"{self.visible} 张")
        if self.schedulable:
            return f"GPU：可见 {seen}，受管入口可分配。"
        return f"GPU：可见 {seen}，但受管入口不可分配 —— {self.reason}"


def gpu_capability() -> GpuCapability:
    """这台机器的 GPU 现状。**可见性与可用性分开回答。**

    今天所有原生后端（darwin / linux / win32）都还没有 GPU 分配与隔离的实现：
    Docker 年代那条路随执行器分档一起拆掉了，原生这边没接上（#793 的尾巴）。
    所以 ``schedulable`` 恒为 False —— 但 ``visible`` 说真话，
    机器上有几张就是几张。

    **不要把这两个字段合成一个 ``available``**。合起来就只能二选一地撒谎：
    说 True 则提交时才撞墙（#893 的现场），说 False 则人看着 4 张空卡
    被告知"没有 GPU"。
    """
    import shutil
    import subprocess

    visible: int | None = None
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=15)
            if out.returncode == 0:
                visible = len([l for l in out.stdout.splitlines() if l.strip()])
        except (OSError, subprocess.SubprocessError):
            visible = None
    return GpuCapability(
        visible=visible,
        schedulable=False,
        isolated=False,
        reason=(
            "原生后端（darwin / linux / win32）还没有 GPU 分配与隔离的实现 —— "
            "Docker 年代那条路随执行器分档拆掉了，原生这边尚未接上（#893 / #793）。"
            "这个答案在提交之前就能确定，不必等到审批之后。"
        ),
    )


def prepare_launch(
    payload_argv: Sequence[str],
    *,
    cwd: Path | str,
    writable_roots: Sequence[Path | str],
    readonly_roots: Sequence[Path | str] = (),
    limits: SandboxLimits | None = None,
    network_access: bool = False,
    environment: Mapping[str, str] | None = None,
    detached: bool = False,
    stdout_path: Path | str | None = None,
    stderr_path: Path | str | None = None,
    gpus: int = 0,
):
    """兼容入口：前台取物命令走原生后端；detached 作业走原生作业进程组。"""
    import secrets
    import sys

    if gpus:
        # 拒绝要带上事实，而且是**和资源画像同一份事实**（#893）。
        # 旧文案只说"原生后端不提供"，于是读到"可见 4×A100"的模型无从对账，
        # 只能一路规划到提交才撞墙 —— 而那时人已经批过一次不可逆的审批。
        # 实测现场：模型在撞上这里之前做了多轮 Python/Torch/CUDA 探测，
        # 因为它无法从一句文案判断"是缺卡、缺驱动、还是缺契约"。四条事实都摆出来。
        cap = gpu_capability()
        raise SandboxContractError(
            "local GPU execution is not provided by the native backends. "
            + cap.sentence()
            + f"（visible={cap.visible}, schedulable={cap.schedulable}, "
            + f"isolated={cap.isolated}）"
            + " 这个答案在提交之前、在问人之前就能确定：core.sandbox.gpu_capability()。"
        )
    if not detached:
        return _native_launch(
            payload_argv, state=None, cwd=cwd, writable_roots=writable_roots,
            readonly_roots=readonly_roots, limits=limits, network_access=network_access,
            environment=environment,
        )
    if stdout_path is None or stderr_path is None:
        raise SandboxContractError("detached jobs need stdout_path and stderr_path")
    inner = _native_launch(
        payload_argv, state=None, cwd=cwd, writable_roots=writable_roots,
        readonly_roots=readonly_roots, limits=limits, network_access=network_access,
        environment=environment,
    )
    name = f"hf-job-{secrets.token_hex(8)}"
    runtime_id = secrets.token_hex(32)
    control_dir = _jobs_root() / name
    control_dir.mkdir(mode=0o700)
    (control_dir / "launch.json").write_text(json.dumps({
        "argv": list(inner.argv),
        "cwd": str(getattr(inner, "cwd", None) or cwd),
        "env": dict(getattr(inner, "env", None) or os.environ),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }), encoding="utf-8")
    write_object(control_dir / "record.json", {
        "name": name,
        "runtime_id": runtime_id,
        "kind": "job",
        "namespace": sandbox_namespace(),
        "status": "created",
        "created_at": __import__("time").time(),
    })
    return NativeJobLaunch(
        argv=[sys.executable, "-I", "-B", str(_NATIVE_JOB), "start", str(control_dir)],
        container_name=name,
        control_dir=control_dir,
    )


def inspect_container(container_name: str) -> dict[str, Any]:
    """按作业名或 runtime id 查原生作业。形状与 Docker 年代一致：exists / managed / id /
    name / kind / namespace / running / status。"""
    found = _job_record(str(container_name))
    if found is None:
        return {"exists": False}
    _control_dir, record = found
    status = str(record.get("status") or "created")
    pid = int(record.get("pid") or 0)
    identity_state = "not_applicable"
    if status in {"starting", "running"} and pid:
        # 先问身份，再问活性（#1085 A）。
        #
        # 裸 pid 判活在号被复用之后会把**占号的无关进程**当成这个作业还在跑 ——
        # supervisor 被 SIGKILL / OOM / 主机重启打断、没来得及改状态时，记录就
        # 停在 running，而重启后 PID 计数从头开始，旧号很快又被分配出去。
        from shared.lib import process_control

        match = process_control.identity_matches(pid, record.get("birth_identity"))
        if match is False:
            status, identity_state = "dead", "pid_reused"
        elif match is None:
            # 读不到身份（升级前起的作业、或权限不足）：退回裸 pid 判活 ——
            # 这是升级前的行为，**如实标注**，不假装查过。
            identity_state = "unknown"
            if not _pid_alive(pid):
                status = "dead"
        else:
            identity_state = "confirmed"
            if not _pid_alive(pid):
                status = "dead"
    return {
        "exists": True,
        "managed": True,
        "id": record.get("runtime_id"),
        "name": record.get("name"),
        "kind": "job",
        "namespace": record.get("namespace") or sandbox_namespace(),
        "running": status in {"starting", "running"},
        "status": status,
        "exit_code": record.get("exit_code"),
        "pid": pid or None,
        # 这个答案是**怎么得出来的**（#1085 A）：confirmed = 出生身份对上了；
        # pid_reused = 号被别人占了，所以判死；unknown = 读不到身份，退回裸 pid
        # 判活。缺席与"查过了"必须分得开。
        "identity": identity_state,
    }


def attempt_status(container_name: str, control_dir: Path | None = None) -> dict[str, Any]:
    del container_name, control_dir
    return {"error": "docker_backend_removed"}


def stop_container(
    container_name: str, *, remove: bool = True, expected_container_id: str | None = None
) -> bool:
    import shutil
    import signal
    import time

    found = _job_record(str(container_name))
    if found is None:
        return False
    control_dir, record = found
    if expected_container_id and record.get("runtime_id") != expected_container_id:
        raise SandboxContractError("job runtime id does not match the expected identity")
    # 作业的组在事实账里：新记录写 `group`（"pgid:<n>" / 将来的 "job:<name>"），
    # 升级前起的作业只有 `pgid` —— 读侧两种都认。
    identity = str(record.get("group") or "")
    if not identity and record.get("pgid"):
        identity = f"pgid:{int(record['pgid'])}"
    # ── 发信号之前先问两个问题（#1085 A）────────────────────────────────
    #
    # 一、账上已经是终态了吗。Experiment 在**每个本地作业正常收尾时**都会调一次
    #     `stop_container`，而此前它不看状态，直接对记录里的组号 killpg —— 实测：
    #     收尾一个 `status=exited` 的作业，SIGTERM 发给了占号的无关进程组。
    # 二、现在占着这个号的，还是当初那个进程吗。号被复用之后发信号打的是别人；
    #     而平台自己的 worker 和命令正好是以组长身份起的，同样在射程内。
    #
    # 读不到身份时 fail closed：既不判死，也不发信号 —— 那正是"不知道"该有的行为。
    supervisor_pid = int(record.get("pid") or 0)
    if str(record.get("status") or "") in {"exited", "dead"}:
        identity = ""           # 终态：只做清理，一个信号都不发
    elif identity and supervisor_pid:
        from shared.lib import process_control as _pc

        _match = _pc.identity_matches(supervisor_pid, record.get("birth_identity"))
        if _match is False:
            # 号被复用：不发信号、**不删账本**（删了就把"这条记录没收尾"这个事实
            # 也一起抹掉），结构化地说出来。
            write_object(control_dir / "record.json",
                         {"status": "dead", "stop_refused": "pid_reused",
                          "ended_at": time.time()}, merge=True)
            return False
    if identity:
        from shared.lib import process_control

        supervisor = int(record.get("pid") or 0)
        try:
            group = process_control.Group.from_identity(identity)
        except ValueError:
            group = None
        if group is None and _pid_alive(supervisor):
            return False
        if group is not None:
            try:
                group.terminate(grace_s=3, still_alive=lambda: _pid_alive(supervisor))
                deadline = time.monotonic() + 5
                while _pid_alive(supervisor) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if _pid_alive(supervisor):
                    return False
            finally:
                group.close()
    elif _pid_alive(int(record.get("pid") or 0)):
        return False
    if remove:
        shutil.rmtree(control_dir, ignore_errors=True)
    else:
        write_object(control_dir / "record.json", {"status": "exited", "ended_at": time.time()}, merge=True)
    return True


def release_reservation(container_name: str) -> None:
    del container_name


def cleanup_control_dir(path: Path | str) -> None:
    import shutil

    target = Path(path)
    root = _jobs_root()
    if target.resolve(strict=False).is_relative_to(root):
        shutil.rmtree(target, ignore_errors=True)


def list_attempt_instances() -> list[dict[str, Any]]:
    return []


def evict_attempt(attempt_id: str, *, blocking: bool = True) -> bool:
    del attempt_id, blocking
    return False


def evict_state_attempt(state: Any) -> bool:
    del state
    return False


def _reset_probe_cache_for_tests() -> None:
    from core import isolation

    isolation._reset_for_tests()


__all__ = [
    "DEFAULT_EGRESS_ALLOWLIST",
    "EGRESS_ALLOWLIST_ENV",
    "GPU_ISOLATION_NONE",
    "effective_egress_policy",
    "gpu_capability",
    "SandboxCeiling",
    "SandboxContractError",
    "SandboxLimits",
    "SandboxManifest",
    "SandboxUnavailable",
    "availability",
    "limits_for_profile",
    "manifest_for",
    "model_tool_roots",
    "parse_manifest",
    "prepare_attempt_command",
    "prepare_launch",
    "readonly_overrides_for",
    "require_available",
    "uncovered_write_roots",
    "write_roots_for",
]

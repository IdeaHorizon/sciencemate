"""Deterministic path-role contract for the experiment node.

Directory names are not permissions.  This module normalizes canonical roles
from structured inputs and legacy aliases, then resolves a path by the most
specific declared root.  Conflicting roles on the same root are reported as
ambiguous instead of being guessed from suffixes such as ``.F90`` or path
components such as ``build``/``tmp``.

Canonical roles are application-agnostic:

* source_baseline_root: immutable original source
* managed_source_root: transient source-acquisition area (never a normal build root)
* source_worktree_root: editable source derived from the baseline
* source_patch_root: patch/diff/overlay storage
* workspace_root: this node's bound Git workspace; writable but never a job root
* experiment_root: logical container; not writable by itself
* build_root: build products and build cache
* run_root: runtime inputs, logs, outputs and checkpoints
* approved_write_root: a path a human explicitly approved for writing
* dependency_root: external dependency/toolchain root, read-only by default

Every experiment receives isolated run-local ``run_root``, ``build_root`` and
``managed_source_root`` defaults; explicit declarations still override them.
Source/build defaults are latent allocations: they are advertised before
planning but only materialized on demand.  This prevents the first legitimate
acquire/compile from deadlocking on a caller-supplied task classification. Framework-owned
run state (artifacts, transcript, checkpoints) is deliberately not a writable
application root: commands default to ``outputs/experiment/runtime`` and
``outputs/experiment/build`` instead.

Authority (2026-08-04): a path role is a permission, so only a principal that
already holds permission may declare one.  Roles are collected from fixture /
orchestration ``node_inputs``, from ``hook_state['path_roles']`` (node hooks
and human approvals), and from the run-local defaults.  **Artifacts never
grant or lock a role**, regardless of type or apparent provenance.  The agent
authors ``declared_route`` freely via ``save_artifact``; before this rule a
routine in-source build contract (``source_path`` + a nested ``build_dir``)
mapped ``source_path`` onto an immutable baseline, made the contract overlap,
and turned every subsequent write in the run into an unappealable
``invalid_path_role_contract`` — a self-inflicted, unrecoverable deadlock.
The mirror case was worse: ``{"build_dir": "/anywhere"}`` silently granted
write access to that path.  ``metadata['_forwarded_input']`` cannot rescue the
distinction because ``save_artifact`` passes caller metadata through verbatim,
so the agent can forge it.  ``declared_route`` remains readable as build
*evidence* (see ``tools.build_contract``); it simply carries no authority.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


CANONICAL_ROLES = {
    "source_baseline_root",
    "managed_source_root",
    "source_worktree_root",
    "source_patch_root",
    "workspace_root",
    "experiment_root",
    "build_root",
    "run_root",
    "approved_write_root",
    "dependency_root",
}

# ``cleanup``: how much of a root a contained destructive command may remove
# without asking a human.  "none" = no automatic removal at all; "contents" =
# entries inside the root, but never the root itself; "root" = the root may be
# removed and recreated.  This is the only input the high-risk gate consults
# when deciding whether a *contained* deletion still needs confirmation, and it
# is deliberately a property of the declared role rather than of the directory
# name (see module docstring).
_ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "source_baseline_root": {
        "writable": False, "container_only": False, "patch_tracked": False,
        "cleanup": "none",
    },
    # Transient acquisition area: downloading or extracting a source package
    # needs a writable destination before it is promoted to a baseline.  It is
    # intentionally not inferred from ``source_path`` and never a normal build
    # root; compilation must use a distinct build_root.  Removing the tree
    # still asks.
    "managed_source_root": {
        "writable": True, "container_only": False, "patch_tracked": False,
        "cleanup": "none",
    },
    "source_worktree_root": {
        "writable": True, "container_only": False, "patch_tracked": True,
        "cleanup": "none",
    },
    "source_patch_root": {
        "writable": True, "container_only": False, "patch_tracked": True,
        "cleanup": "contents",
    },
    # This node's bound Git workspace is writable but never disposable job state.
    # It is injected only from State, never accepted from node_inputs.
    "workspace_root": {
        "writable": True, "container_only": False, "patch_tracked": False,
        "cleanup": "none",
    },
    "experiment_root": {
        "writable": False, "container_only": True, "patch_tracked": False,
        "cleanup": "none",
    },
    "build_root": {
        "writable": True, "container_only": False, "patch_tracked": False,
        "cleanup": "root",
    },
    "run_root": {
        "writable": True, "container_only": False, "patch_tracked": False,
        "cleanup": "root",
    },
    # Only a human answering a scope-guard pause creates this role, and the
    # pause text states exactly these semantics: writable, contents may be
    # cleared, removing the directory itself asks again.
    "approved_write_root": {
        "writable": True, "container_only": False, "patch_tracked": False,
        "cleanup": "contents",
    },
    "dependency_root": {
        "writable": False, "container_only": False, "patch_tracked": False,
        "cleanup": "none",
    },
}

_CLEANUP_MODES = ("none", "contents", "root")

class PathRoleContractError(ValueError):
    """路径角色契约无效 —— 据此铸出的挂载面不可信，调用方必须停在这里。"""


# Roles that participate in baseline integrity auditing / immutability.  Only
# an explicit canonical declaration can produce one; no alias maps here.
IMMUTABLE_ROLES = frozenset({"source_baseline_root"})

# Compatibility is accepted only at ingestion.  Runtime decisions and emitted
# manifests always use the canonical role on the left.
#
# ``source_path``/``repo_dir``/... name the source consumed by a build.  The
# Experiment contract is strictly out-of-source, so a trusted orchestration
# input using one of these aliases always means an immutable baseline.  An
# agent artifact still cannot create this role at all (see the authority rule
# above); a nested build_dir is rejected by build_contract before compilation.
# ``managed_source_root`` is canonical-only and reserved for the short source
# acquisition phase.  A persistent baseline must instead arrive explicitly
# from trusted orchestration input; no agent artifact can promote a path.
LEGACY_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "source_baseline_root": (
        "source_root", "source_dir", "source_path", "src_dir", "srcroot",
        "src_root", "repo_dir", "repo_root", "code_dir", "code_root",
        "upstream_src", "upstream_src_dir",
    ),
    "source_worktree_root": (
        "build_src", "build_src_dir", "work_src", "work_src_dir", "target_path",
    ),
    "source_patch_root": (
        "patch_root", "patch_dir", "patches_dir", "overlay_root", "overlay_dir",
    ),
    "experiment_root": (
        "workdir", "work_dir", "workspace_dir", "workspace_root", "case_dir",
        "casedir", "case_root",
    ),
    "build_root": ("build_dir", "builddir"),
    "run_root": (
        "run_dir", "rundir", "output_dir", "outputs_dir", "scratch_dir",
        "scratchdir",
    ),
    "dependency_root": (
        "deps_root", "deps_dir", "dependency_dir", "env_root",
    ),
}


@dataclass(frozen=True)
class PathRole:
    role: str
    path: str
    writable: bool
    container_only: bool
    patch_tracked: bool
    source: str
    legacy_alias: str | None = None
    cleanup: str = "none"

    def allows_cleanup(self, *, delete_root: bool) -> bool:
        """May a contained destructive command run here without confirmation?"""
        if not self.writable or self.container_only:
            return False
        if self.role in IMMUTABLE_ROLES:
            return False
        return self.cleanup == "root" if delete_root else self.cleanup in {
            "contents", "root"}


def experiment_output_dir(
    state: Any,
    kind: str = "",
    *,
    create: bool = False,
) -> Path:
    """Return this node's application-output directory.

    这里**不再自己算路径**。"某节点这一轮的副产物放哪"只有一个真相源
    `core.paths.node_output_dir`：绑了 Project worktree 就是
    ``<worktree>/experiment/outputs/<kind>/``，没绑就退回 run 目录。

    此前这里抄了一份自己的锚点逻辑（先是 `state.project_root`，后是
    `<worktree>/experiment/runtime`）。两份逻辑并存的代价是实测过的：产物落在
    worktree 外 → 通用文件工具够不着 → postprocess 拿不到数据；进不了
    checkpoint → 发布不到 project main → 下一个 session 看不见上一轮跑了什么；
    而 submit_job 的路径角色门又按**另一套**判定，把 agent 选的正确路径拒了。
    抄一份就多一处会分叉的判断，所以删掉抄件、只留转发。

    超大二进制（轨迹 dump）由平台 checkpoint 的 blob 上限自然挡在 commit 之外
    （withheld 并如实报告），文件仍在磁盘上供本 session 内的下游节点读取。
    """
    from core.paths import node_output_dir

    return node_output_dir(state, "experiment", kind, create=create)


def default_stage_workdir(state: Any, stage: str, *, create: bool = False) -> Path:
    """Return the existing experiment output anchor selected by execution stage.

    This is intentionally a stage-only mapping: it never inspects command text
    and does not replace an explicit cwd/workdir supplied by a caller.
    """
    kinds = {
        "diagnostic": "",
        "toolchain_build": "build",
        "simulation": "runtime",
    }
    try:
        kind = kinds[str(stage).strip().lower()]
    except KeyError as exc:
        raise ValueError("stage must be diagnostic, toolchain_build, or simulation") from exc
    return experiment_output_dir(state, kind, create=create)


def project_workspace_dir(state: Any) -> Path | None:
    """Return the write-containment boundary for a project-backed run."""
    # 写入边界直接用绑定时算好的 `state.workspace_root`（= 本节点在 worktree 里
    # 自己的目录），不再在这里重算一遍。"允许写的地方"和"实际写的地方"必须出自
    # 同一个来源，否则两边各自演化就会对不上 —— v20 实测：submit_job 拒绝了
    # agent 选的、其实完全正确的 worktree 内路径。
    bound = getattr(state, "workspace_root", None)
    if bound is not None:
        return Path(bound)
    # ``run_node --sandbox`` assigns a project_root for project-scoped memory,
    # but does not bind a project *worktree*.  In that mode application output
    # is deliberately run-local (``core.paths.node_output_dir``), so inventing
    # ``<project_root>/workspace`` here conflicts with the run-local path roles
    # and makes safe_write_file impossible to satisfy.
    if getattr(state, "project_worktree", None) is None:
        return None
    project_root = getattr(state, "project_root", None)
    if project_root is None:
        return None
    return Path(project_root) / "workspace"


def sandbox_mount_roots(state: Any) -> tuple[list[Path], list[Path]]:
    """Map the validated role contract to the sandbox's read/write mounts.

    ``validated`` 不是形容词而是前置条件：契约无效时角色之间的包含关系本身就
    不可信，据此铸出的挂载面同样不可信，所以这里先校验再映射，而不是让调用方
    在拿到根之后才发现契约有问题。

    可写根一旦**包住**某个只读根，交给后端就等于没有那道墙：
    ``core.isolation._native.write_layers`` 把落在只读根内部的可写根归入
    priority，而 bwrap 的绑定次序是 ``broad(rw) → readonly(ro) → priority(rw)``，
    priority 在只读之后绑，反过来把里层的 ``--ro-bind`` 盖掉（Landlock 与 win32
    更彻底：纯放行清单没有 deny 层）。``subprocess_policy.bash_sandbox_roots``
    已按同一判据收窄，本函数是最后一个没跟上的产出面。
    """
    from core.sandbox import model_tool_roots

    contract = validate_path_roles(state)
    if not contract["valid"]:
        raise PathRoleContractError("; ".join(contract["errors"]))

    writable, readonly = model_tool_roots(state)
    for role in collect_path_roles(state):
        if role.container_only:
            continue
        path = Path(role.path).expanduser()
        if not path.exists():
            continue
        resolved = path.resolve()
        target = writable if role.writable else readonly
        if resolved not in target:
            target.append(resolved)

    # 只在真的存在嵌套只读根时才丢弃宽祖先根；没有嵌套时宽根无害，保持原行为。
    narrowed = [
        root for root in writable
        if not any(_covers(str(root), str(ro)) and root != ro for ro in readonly)
    ]
    return narrowed, readonly


# A role's value must be absolute (or home-anchored): a relative string would
# silently inherit authority from the CWD, and a bare role-name is not a path.
# "Absolute" is recognised on *any* host — POSIX ``/`` / ``~`` plus a Windows
# drive (``C:\`` or ``C:/``) or UNC (``\\host\share``) prefix — so the contract
# normalises identically whether a path was produced on Windows or replayed on
# another OS.  ``os.path.isabs`` is host-specific and would silently reject every
# ``C:\…`` root on a POSIX host, which is exactly the POSIX-only anchor bug that
# left Windows runs with *zero* declared writable roots (the default run_root /
# build_root / workspace_root all vanished); PR#864 hit the same class in
# ``core.data_provenance``.  A drive letter without a following separator
# (``C:foo``) is drive-relative, not absolute, and stays rejected.
_WINDOWS_ABS_RE = re.compile(r"[A-Za-z]:[\\/]|\\\\")


def _is_abs_or_home(text: str) -> bool:
    return text.startswith(("/", "~")) or bool(_WINDOWS_ABS_RE.match(text))


def _normalize_path(value: Any) -> str | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    text = str(value).strip()
    if not text or not _is_abs_or_home(text):
        return None
    return str(Path(os.path.expandvars(os.path.expanduser(text))).resolve(strict=False))


def _iter_specs(role: str, spec: Any, source: str, alias: str | None):
    """Yield one entry per declared path.

    A role legitimately covers several roots (two build variants, a build root
    plus an approved one).  A list used to fall through ``_coerce_role``'s
    ``isinstance(spec, dict)`` check into ``_normalize_path``, which rejects
    non-``str`` input, so every element vanished *and* the contract still
    reported ``valid=True``.  Expanding here keeps the loss impossible.
    """
    if isinstance(spec, (list, tuple)):
        for item in spec:
            yield role, item, source, alias
    else:
        yield role, spec, source, alias


def _iter_role_values(mapping: dict[str, Any], source: str, *,
                      include_aliases: bool = True):
    explicit = mapping.get("path_roles")
    if isinstance(explicit, dict):
        for role, spec in explicit.items():
            if role in CANONICAL_ROLES:
                yield from _iter_specs(role, spec, source, None)

    for role in CANONICAL_ROLES:
        if role in mapping:
            yield from _iter_specs(role, mapping[role], source, None)

    if include_aliases:
        for role, aliases in LEGACY_ROLE_ALIASES.items():
            for alias in aliases:
                if alias in mapping:
                    yield from _iter_specs(role, mapping[alias], source, alias)

    # Build contracts are commonly wrapped as ``build_contract: {...}`` or
    # ``route: {...}``.  The contract validator accepts these documents, so
    # path-role discovery must inspect the same nested payload; otherwise a
    # valid declared_route is saved but its build_root is invisible to the
    # scope guard (observed in the bench dry-run E2E).
    for wrapper in ("build_contract", "route", "declared_route"):
        nested = mapping.get(wrapper)
        if isinstance(nested, dict):
            yield from _iter_role_values(
                nested, f"{source}:{wrapper}", include_aliases=include_aliases)


# Roots that must never be handed out as a writable dependency_root.  Pointing
# dependency_root at these read-only is legitimate (that is what the role is
# for); making them writable is not, because it would neutralize the guard for
# the entire machine.
_SYSTEM_ROOTS = frozenset({
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64",
    "/libx32", "/media", "/mnt", "/opt", "/proc", "/root", "/run", "/sbin",
    "/srv", "/sys", "/usr", "/var",
})

# Shared-filesystem mount points.  On a cluster these hold every project's
# data, so their *root* is exactly as dangerous to declare writable as "/" —
# a subtree below them stays perfectly legitimate.
_SHARED_MOUNT_ROOTS = frozenset({
    "/beegfs", "/gpfs", "/lustre", "/scratch", "/work", "/data", "/nfs",
    "/shared", "/share", "/project", "/projects",
})


def _is_system_root(path: str) -> bool:
    """Is ``path`` a machine-wide root (or $HOME itself) rather than a subtree?

    ``~/.local`` is a legitimate writable dependency root; ``~`` is not — the
    latter would cover the whole user profile including other projects' data.
    The same reasoning extends to ``/home`` and to shared cluster mounts:
    before 2026-08-04 all of ``/home``, ``/mnt`` and ``/mnt/beegfs`` could be
    declared a writable dependency_root, which covered every other user and
    every other project on the machine.
    """
    if not path:
        return False
    normalized = str(Path(path)).rstrip("/") or "/"
    if normalized in _SYSTEM_ROOTS or normalized in _SHARED_MOUNT_ROOTS:
        return True
    try:
        return normalized == str(Path.home()).rstrip("/")
    except (RuntimeError, OSError):
        return False


def _coerce_role(role: str, spec: Any, source: str,
                 legacy_alias: str | None = None,
                 issues: list[str] | None = None) -> PathRole | None:
    def _note(message: str) -> None:
        if issues is not None:
            issues.append(message)

    defaults = _ROLE_DEFAULTS[role]
    if role == "workspace_root" and source.startswith("node_inputs"):
        _note("workspace_root is framework-owned and may not be declared by "
              + source)
        return None
    if isinstance(spec, dict):
        raw_path = spec.get("path")
        path = _normalize_path(raw_path)
        writable = bool(spec.get("writable", defaults["writable"]))
        container_only = bool(
            spec.get("container_only", defaults["container_only"]))
        patch_tracked = bool(
            spec.get("patch_tracked", defaults["patch_tracked"]))
        cleanup = str(spec.get("cleanup", defaults["cleanup"]))
        if cleanup not in _CLEANUP_MODES:
            _note(f"{role}: unknown cleanup mode {cleanup!r}; "
                  f"using {defaults['cleanup']!r}")
            cleanup = defaults["cleanup"]
    else:
        raw_path = spec
        path = _normalize_path(spec)
        writable = defaults["writable"]
        container_only = defaults["container_only"]
        patch_tracked = defaults["patch_tracked"]
        cleanup = defaults["cleanup"]
    if path is None:
        # Silently dropping a declaration produced the worst possible failure
        # mode: zero roles, zero diagnostics, and every write then classified
        # as unknown.  Say so instead.
        _note(f"{role} declaration ignored (not an absolute path): "
              f"{raw_path!r} from {source}")
        return None

    # Baseline and logical container semantics are invariants, not caller hints.
    if role == "source_baseline_root":
        writable = False
        cleanup = "none"
    if role == "experiment_root":
        writable = False
        container_only = True
        cleanup = "none"
    # Only a human approval creates this role, and the approval text promises
    # exactly "writable, contents clearable, removing the root asks again".
    if role == "approved_write_root":
        writable = True
        container_only = False
        cleanup = "contents"
    # A writable dependency_root is the sanctioned way to install user-level
    # dependencies, but it must never become a blanket escape from the guard.
    # Declaring "/" or "/usr" (or $HOME itself) writable would hand the node
    # write access to the whole machine through a one-line contract edit.
    if role == "dependency_root" and writable and _is_system_root(path):
        _note(f"dependency_root {path} is a machine-wide root; "
              "forced read-only")
        writable = False
    # The same reasoning applies to every other role: no declaration may make
    # a machine-wide root or a shared-mount root writable.
    if writable and _is_system_root(path):
        _note(f"{role} {path} is a machine-wide root; forced read-only")
        writable = False
        cleanup = "none"
    if not writable:
        cleanup = "none"
    return PathRole(
        role=role, path=path, writable=writable,
        container_only=container_only, patch_tracked=patch_tracked,
        source=source, legacy_alias=legacy_alias, cleanup=cleanup,
    )


def _collect_with_issues(
    state: Any, *, include_runtime: bool = True,
) -> tuple[list[PathRole], list[str]]:
    """Collect roles and the diagnostics produced while normalizing them.

    Authority is the whole point of the source list below: ``node_inputs`` is
    the fixture/orchestration channel and ``hook_state['path_roles']`` is
    written by node hooks and by human scope approvals.  Neither is reachable
    from the agent's tool surface.  Artifacts are *not* consulted — see the
    module docstring for the deadlock and the self-grant that ingesting them
    produced.
    """
    candidates: list[PathRole] = []
    issues: list[str] = []

    hook_roles = getattr(state, "hook_state", {}).get("path_roles")
    if isinstance(hook_roles, dict):
        for item in _iter_role_values({"path_roles": hook_roles}, "hook_state"):
            role = _coerce_role(*item, issues=issues)
            if role:
                candidates.append(role)

    inputs = getattr(state, "hook_state", {}).get("node_inputs") or {}
    if isinstance(inputs, dict):
        for item in _iter_role_values(inputs, "node_inputs"):
            role = _coerce_role(*item, issues=issues)
            if role:
                candidates.append(role)

    if include_runtime:
        workspace = _normalize_path(getattr(state, "workspace_root", None))
        if workspace:
            # State is the only authority for this role.  It gives the scope
            # guard the same owned-directory fact that Core uses for cwd and
            # subprocess sandboxing, without borrowing run_root semantics.
            candidates = [r for r in candidates if not (
                r.role == "experiment_root" and r.path == workspace)]
            candidates.append(PathRole(
                role="workspace_root", path=workspace, writable=True,
                container_only=False, patch_tracked=False,
                source="framework:workspace_root",
                cleanup=_ROLE_DEFAULTS["workspace_root"]["cleanup"],
            ))
        # Every experiment has isolated run-local allocations.  Supplying the
        # latent build role removes an accidental bootstrap deadlock where the
        # first legitimate compile is rejected merely because the agent had
        # not repeated a path-role declaration already implied by this run.
        #
        # The default is suppressed only when an explicit root of the same role
        # already covers the same tree.  Suppressing it whenever *any* root of
        # that role exists (the behaviour before 2026-08-04, which contradicted
        # this comment) silently removed the run-local build area as soon as a
        # workspace build directory was declared.
        defaults = {
            "run_root": ("runtime", "framework:experiment_runtime"),
            "managed_source_root": (
                "runtime/source", "framework:experiment_managed_source"),
            "build_root": ("build", "framework:experiment_build"),
        }
        for role_name, (kind, source) in defaults.items():
            try:
                default_root = _normalize_path(experiment_output_dir(state, kind))
            except ValueError:
                # No framework run root (unit-test states, pre-run contexts):
                # there is no run-local area to allocate, so declared roles are
                # the whole contract.  Previously unreachable because the probe
                # only ran when no role of this kind existed at all.
                break
            if not default_root:
                continue
            if any(r.role == role_name and _covers(r.path, default_root)
                   for r in candidates):
                continue
            candidates.append(PathRole(
                role=role_name, path=default_root, writable=True,
                container_only=False, patch_tracked=False,
                source=source, cleanup=_ROLE_DEFAULTS[role_name]["cleanup"],
            ))

    # Deduplicate identical declarations while retaining conflicting roles.
    unique: dict[tuple[str, str, bool, bool], PathRole] = {}
    for role in candidates:
        key = (
            role.role, role.path, role.writable, role.container_only)
        current = unique.get(key)
        if current is None or (
            current.legacy_alias is not None and role.legacy_alias is None
        ):
            unique[key] = role
    ordered = sorted(unique.values(), key=lambda r: (r.path, r.role, r.source))
    return ordered, issues


def collect_path_roles(state: Any, *, include_runtime: bool = True) -> list[PathRole]:
    """Collect and normalize the path-role contract for one run."""
    roles, _ = _collect_with_issues(state, include_runtime=include_runtime)
    return roles


def _covers(root: str, path: str) -> bool:
    """Is ``path`` equal to, or contained by, ``root``?"""
    try:
        target = Path(path).expanduser().resolve(strict=False)
        base = Path(root).expanduser().resolve(strict=False)
    except (RuntimeError, OSError, ValueError):
        return False
    return target == base or base in target.parents


def serialize_path_roles(state: Any) -> list[dict[str, Any]]:
    return [asdict(role) for role in collect_path_roles(state)]


def matching_path_roles(path: str | Path, state: Any) -> list[PathRole]:
    target = Path(path).expanduser().resolve(strict=False)
    matches = []
    for role in collect_path_roles(state):
        root = Path(role.path).expanduser().resolve(strict=False)
        try:
            target.relative_to(root)
            matches.append(role)
        except ValueError:
            continue
    if not matches:
        return []
    longest = max(len(Path(r.path).parts) for r in matches)
    return [r for r in matches if len(Path(r.path).parts) == longest]


def validate_path_roles(state: Any) -> dict[str, Any]:
    """Validate the contract, reporting *which trees* a conflict poisons.

    A contract error used to abort every command in the run, including writes
    to an unrelated run-local log.  Errors now carry the exact paths involved
    (``conflict_paths``) so a caller can refuse only the operations that touch
    those trees and let the rest of the run proceed.
    """
    roles, issues = _collect_with_issues(state)
    errors: list[str] = []
    warnings: list[str] = list(issues)
    conflict_paths: set[str] = set()

    by_path: dict[str, list[PathRole]] = {}
    for role in roles:
        by_path.setdefault(role.path, []).append(role)
        if role.legacy_alias:
            warnings.append(
                f"{role.legacy_alias} is a legacy alias; use {role.role}")

    for path, entries in by_path.items():
        semantic = {
            (e.role, e.writable, e.container_only)
            for e in entries
        }
        if len(semantic) > 1:
            errors.append(
                f"conflicting roles for {path}: "
                + ", ".join(sorted(e.role for e in entries)))
            conflict_paths.add(path)

    # Only an explicit immutable baseline constrains nesting, and only against
    # roots that actually grant writes.  ``container_only`` roles (experiment_root)
    # grant nothing, so a baseline sitting inside one is harmless — treating
    # them as writable made the ordinary "workspace/ container + source tree
    # inside it" layout an unfixable contract error.
    #
    # A build root nested in an immutable baseline stays an error: an in-source
    # build writes into the tree, which Experiment never permits.  Source
    # acquisition and compilation are separate phases; compilation always uses
    # a distinct build_root.
    baselines = [r for r in roles if r.role in IMMUTABLE_ROLES]
    # State-owned workspace_root records node ownership. It is a broad framework
    # boundary, not an application role that grants a source/build/run write;
    # its trusted identity is the State-owned path, not a provenance string that
    # can differ after hook injection or resume.
    state_workspace = _normalize_path(getattr(state, "workspace_root", None))
    writable_roots = [
        r for r in roles
        if r.writable and not r.container_only
        and not (r.role == "workspace_root" and r.path == state_workspace)
    ]
    for baseline in baselines:
        bp = Path(baseline.path)
        for writable in writable_roots:
            wp = Path(writable.path)
            try:
                wp.relative_to(bp)
            except ValueError:
                try:
                    bp.relative_to(wp)
                except ValueError:
                    continue
                errors.append(
                    f"immutable source_baseline_root {baseline.path} is inside "
                    f"writable {writable.role} {writable.path}; baseline and "
                    "writable roles must not overlap")
            else:
                errors.append(
                    f"{writable.role} {writable.path} is inside immutable "
                    f"source_baseline_root {baseline.path}; choose a distinct "
                    "build_root outside the source tree")
            conflict_paths.update({baseline.path, writable.path})

    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "conflict_paths": sorted(conflict_paths),
        "roles": [asdict(r) for r in roles],
    }


def conflicting_role_paths(state: Any) -> list[str]:
    """Paths a caller must refuse to touch because their contract is broken."""
    return validate_path_roles(state).get("conflict_paths") or []

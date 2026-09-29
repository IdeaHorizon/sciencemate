"""Experiment-owned subprocess policy layered over the framework sandbox API."""
from __future__ import annotations

import stat
from pathlib import Path
from typing import Any


class PathRoleSandboxContractError(RuntimeError):
    """路径角色无法形成可信且可兑现的子进程写能力。"""


class PythonSandboxContractError(PathRoleSandboxContractError):
    """路径角色无法形成不自相矛盾的 Python 写能力。"""


class BashSandboxContractError(PathRoleSandboxContractError):
    """路径角色无法形成不自相矛盾的 Bash 写能力。"""


_PYTHON_EXECUTION_ROLES = frozenset({
    "run_root", "build_root", "managed_source_root",
    "source_patch_root", "approved_write_root",
})
_PYTHON_ALWAYS_WRITABLE_ROLES = frozenset({"run_root"})
_PYTHON_ALWAYS_READONLY_ROLES = frozenset({
    "source_baseline_root", "source_worktree_root", "workspace_root",
    "experiment_root", "dependency_root",
})


def _canonical_path(value: Any) -> Path | None:
    if value is None or not str(value).strip():
        return None
    try:
        return Path(str(value)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _inside(path: Path | None, root: Path | None) -> bool:
    return bool(path is not None and root is not None
                and (path == root or path.is_relative_to(root)))


def _nearest_existing_overlay(path: Path | None) -> Path | None:
    """不存在的 protected role 由最近存在祖先承接只读覆盖。"""
    current = path
    while current is not None and not current.exists():
        parent = current.parent
        if parent == current:
            return current if current.exists() else None
        current = parent
    return current


_APPROVED_WRITE_CAPABILITIES_KEY = "_approved_subprocess_write_roots"


def register_approved_subprocess_write_root(state: Any, value: str) -> Path:
    """记录由本进程消费人工确认后签发的 run-local 路径能力。"""
    path = _canonical_path(value)
    if path is None or not path.is_absolute():
        raise PathRoleSandboxContractError("人工批准路径不是有效绝对路径")
    hook_state = getattr(state, "hook_state", None)
    if not isinstance(hook_state, dict):
        raise PathRoleSandboxContractError("run 缺少可持久化 capability 状态")
    entries = hook_state.setdefault(_APPROVED_WRITE_CAPABILITIES_KEY, [])
    if not isinstance(entries, list):
        raise PathRoleSandboxContractError("路径 capability 状态损坏")
    rendered = str(path)
    if rendered not in entries:
        entries.append(rendered)
    return path


def _local_write_capability_roots(state: Any) -> list[Path]:
    """仅从 Core 已授予写根和本进程消费的人工批准构造能力。"""
    from core import sandbox

    writable, _readonly = sandbox.model_tool_roots(state)
    if writable is None:
        writable = sandbox.write_roots_for(state)
    values = list(writable or [])
    hook_state = getattr(state, "hook_state", None)
    if isinstance(hook_state, dict):
        approved = hook_state.get(_APPROVED_WRITE_CAPABILITIES_KEY, [])
        if isinstance(approved, list):
            values.extend(approved)
    roots: list[Path] = []
    for value in values:
        path = _canonical_path(value)
        if path is not None and path not in roots:
            roots.append(path)
    return roots


def path_has_local_write_capability(state: Any, value: str | Path) -> bool:
    path = _canonical_path(value)
    return any(_inside(path, root) for root in _local_write_capability_roots(state))


def _role_has_local_write_capability(state: Any, role_path: Path) -> bool:
    # 必须覆盖整个 role root；只因 role 内某个子路径已可写，不能反向扩成整树 bind。
    return any(
        _inside(role_path, root)
        for root in _local_write_capability_roots(state)
    )


def _is_scientific_primary(state: Any) -> bool:
    """Whether this run's accepted contract requires the scientific-primary wall.

    This decision comes from the run contract, never from Python source
    inference.  AST findings may still reject a known formal write earlier,
    but an unrecognized call cannot widen the filesystem capability installed
    for the child process.
    """
    try:
        from .run_contract import load_run_contract
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.run_contract import load_run_contract
    contract = load_run_contract(state)
    return (
        str(contract.get("execution_mode") or "") == "scientific"
        and str(contract.get("run_role") or "") == "primary"
    )


def _validate_existing_scratch_tree(scratch: Path) -> None:
    """Reject aliases that could make the scratch bind write outside itself.

    This runs both before the caller materializes the standard subdirectories
    and immediately before spawn.  ``lstat`` is intentional: resolving first
    would erase the evidence that a directory entry is a symlink.
    """
    if not scratch.exists():
        return
    pending = [scratch]
    while pending:
        current = pending.pop()
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise PythonSandboxContractError(
                "scientific_primary_python_scratch_unsafe: cannot inspect "
                f"{current}: {type(exc).__name__}: {exc}; delete the unsafe "
                "entry and retry"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PythonSandboxContractError(
                "scientific_primary_python_scratch_unsafe: symlink at "
                f"{current}; delete that path and retry"
            )
        if stat.S_ISDIR(metadata.st_mode):
            try:
                pending.extend(current.iterdir())
            except OSError as exc:
                raise PythonSandboxContractError(
                    "scientific_primary_python_scratch_unsafe: cannot list "
                    f"{current}: {type(exc).__name__}: {exc}; delete the "
                    "unsafe entry and retry"
                ) from exc
        elif metadata.st_nlink > 1:
            raise PythonSandboxContractError(
                "scientific_primary_python_scratch_unsafe: hard-linked "
                f"non-directory at {current} (st_nlink={metadata.st_nlink}); "
                "delete that path and retry"
            )


def scientific_python_scratch_root(
    state: Any,
    *,
    require_exists: bool,
) -> Path | None:
    """Return the capability-safe scratch root for scientific-primary Python.

    The writable bind must name the dedicated directory itself.  Resolving a
    symlink here and then handing its target to Core would turn a run-local
    scratch exception into write access to the target (including run_root).
    Validate both before materialization and again before spawn.
    """
    if not _is_scientific_primary(state):
        return None
    try:
        from .path_roles import experiment_output_dir
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.path_roles import experiment_output_dir

    runtime_entry = Path(experiment_output_dir(
        state, "runtime", create=False))
    scratch_entry = runtime_entry / ".python-scratch"
    try:
        if scratch_entry.is_symlink():
            raise PythonSandboxContractError(
                "scientific_primary_python_scratch_unsafe: symlink at "
                f"{scratch_entry}; delete that path and retry"
            )
    except OSError as exc:
        raise PythonSandboxContractError(
            "scientific_primary_python_scratch_unsafe: "
            f"cannot inspect .python-scratch: {type(exc).__name__}: {exc}"
        ) from exc
    if scratch_entry.exists() and not scratch_entry.is_dir():
        raise PythonSandboxContractError(
            "scientific_primary_python_scratch_unsafe: .python-scratch must "
            f"be a directory ({scratch_entry}); delete that path and retry"
        )
    _validate_existing_scratch_tree(scratch_entry)

    runtime = _canonical_path(runtime_entry)
    scratch = _canonical_path(scratch_entry)
    if (
        runtime is None
        or scratch is None
        or not _inside(scratch, runtime)
        or _inside(runtime, scratch)
    ):
        raise PythonSandboxContractError(
            "scientific_primary_python_scratch_unsafe: resolved scratch "
            "must be a strict descendant of run_root and must not contain it"
        )
    if require_exists and (
        not scratch_entry.exists() or not scratch_entry.is_dir()
    ):
        raise PythonSandboxContractError(
            "scientific_primary_python_scratch_unavailable"
        )
    return scratch


def python_sandbox_roots(
    state: Any, cwd: str | None, *,
    authorized_targets: list[str] | None = None,
) -> tuple[list[Path], list[Path]]:
    """为单次轻量 Python 构造最小 OS 写能力。

    公共 sandbox 为通用工具保留 state、共享临时目录和缓存写面；轻量
    Python 不继承这些宽能力。这里复用 ``core.sandbox.confine`` 的全盘
    默认只读模型，只精确回放当前执行角色和本次已通过 scope 确认的字面
    目标角色。operational run 保留 run_root 写能力；scientific-primary
    则把 run_root 保持只读，只回放其 ``.python-scratch`` 子目录。
    """
    from core import sandbox
    try:
        from .path_roles import (
            collect_path_roles,
            validate_path_roles,
        )
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.path_roles import (
            collect_path_roles,
            validate_path_roles,
        )

    contract = validate_path_roles(state)
    if not contract["valid"]:
        raise PythonSandboxContractError("; ".join(contract["errors"]))

    _base_writable, readonly = sandbox.model_tool_roots(state)
    if _base_writable is None:
        # Python 完整性边界不服从 bypass；这里只恢复公共只读覆盖，
        # 不恢复公共 /tmp、用户 cache 或整个 state.root 的宽写能力。
        readonly = sandbox.readonly_overrides_for(state)

    roles = collect_path_roles(state)
    scientific_primary = _is_scientific_primary(state)
    run_roots = [
        path
        for role in roles
        if role.role == "run_root"
        and (path := _canonical_path(role.path)) is not None
    ]
    cwd_path = _canonical_path(cwd)
    target_paths = [
        path for value in (authorized_targets or [])
        if (path := _canonical_path(value)) is not None
    ]
    protected: list[Path] = []
    protected_values = list(readonly or []) + [
        getattr(state, "root", None),
        getattr(state, "project_worktree", None),
        getattr(state, "workspace_root", None),
    ]
    for value in protected_values:
        path = _nearest_existing_overlay(_canonical_path(value))
        if path is not None and path not in protected:
            protected.append(path)
    for role in roles:
        path = _canonical_path(role.path)
        if (not role.writable or role.container_only
                or role.role in _PYTHON_ALWAYS_READONLY_ROLES):
            overlay = _nearest_existing_overlay(path)
            if overlay is not None and overlay not in protected:
                protected.append(overlay)
        if scientific_primary and any(
                _inside(path, root) or _inside(root, path)
                for root in run_roots):
            overlay = _nearest_existing_overlay(path)
            if overlay is not None and overlay not in protected:
                protected.append(overlay)

    # 根文件系统默认只读；仅按 path-role 回放本次执行所需的精确
    # 可写根。run-local TMP/cache 已由调用方设置，因此不继承公共 scratch。
    writable: list[Path] = []
    for role in roles:
        path = _canonical_path(role.path)
        if path is None or not path.exists() or not role.writable or role.container_only:
            continue
        # scientific-primary 的轻量 Python 只能读整棵 run_root。角色名不能
        # 把墙重新打开：cwd/AST 命中的 managed_source_root、approved root，
        # 或包住 run_root 的祖先写根都不能被回放。唯一例外在循环后精确加入
        # 已验证的 .python-scratch。
        if scientific_primary and any(
                _inside(path, root) or _inside(root, path)
                for root in run_roots):
            continue
        allowed = (
            role.role in _PYTHON_ALWAYS_WRITABLE_ROLES
            and str(role.source).startswith("framework:")
        )
        if role.role in _PYTHON_EXECUTION_ROLES and _inside(cwd_path, path):
            allowed = True
        if role.role in _PYTHON_EXECUTION_ROLES and any(
                _inside(target, path) for target in target_paths):
            allowed = True
        # source_worktree 默认只读；只有 AST 已识别写动作且 scope 确认已
        # 通过时，调用方才把该目标放进 authorized_targets。
        if role.role == "source_worktree_root" and any(
                _inside(target, path) for target in target_paths):
            allowed = True
        if allowed:
            if not _role_has_local_write_capability(state, path):
                raise PythonSandboxContractError(
                    f"path_capability_required: {role.role}={path}")
            if path not in writable:
                writable.append(path)

    if scientific_primary:
        scratch = scientific_python_scratch_root(
            state, require_exists=True)
        if (
            scratch is None
            or not scratch.is_dir()
            or not any(_inside(scratch, root) for root in run_roots)
            or any(_inside(root, scratch) for root in run_roots)
        ):
            raise PythonSandboxContractError(
                "scientific_primary_python_scratch_unavailable"
            )
        if not _role_has_local_write_capability(state, scratch):
            raise PythonSandboxContractError(
                f"path_capability_required: python_scratch={scratch}"
            )
        if scratch not in writable:
            writable.append(scratch)

    return writable, protected


_BASH_CWD_WRITABLE_ROLES = frozenset({
    "run_root", "build_root", "managed_source_root",
    "approved_write_root", "dependency_root",
})
_BASH_TARGET_WRITABLE_ROLES = frozenset({
    *_BASH_CWD_WRITABLE_ROLES, "source_worktree_root", "source_patch_root",
})


def bash_sandbox_roots(
    state: Any, cwd: str | None, *,
    authorized_targets: list[str] | None = None,
) -> tuple[list[Path], list[Path]]:
    """把已通过 scope guard 的路径角色精确回放到 Bash OS 沙箱。

    路径角色只描述语义，不能自行扩张 OS 能力。只有 Core 已授予写根，或
    本进程消费人工确认后登记的精确路径，才能成为新增 bind。cwd 只选择正常
    执行根；源码 worktree 还必须有已解析写目标。
    """
    from core import sandbox
    try:
        from .path_roles import collect_path_roles, validate_path_roles
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.path_roles import collect_path_roles, validate_path_roles

    contract = validate_path_roles(state)
    if not contract["valid"]:
        raise BashSandboxContractError("; ".join(contract["errors"]))

    base_writable, readonly = sandbox.model_tool_roots(state)
    if base_writable is None:
        base_writable = sandbox.write_roots_for(state)
        readonly = sandbox.readonly_overrides_for(state)
    roles = collect_path_roles(state)
    state_root = _canonical_path(getattr(state, "root", None))

    # Core 的通用工具能力包含整个 state.root；对模型 payload 来说，这会把
    # transcript、artifact 与恢复状态一并暴露为可写。这里先把框架状态和受保护
    # 角色压成只读，再只回绑本次 cwd 或已确认目标对应的精确 role root。
    integrity_overlays: list[Path] = []
    integrity_values = [getattr(state, "root", None)]
    integrity_values.extend(
        role.path for role in roles
        if (not role.writable or role.container_only
            or role.role in {
                "source_baseline_root", "source_worktree_root",
                "source_patch_root", "dependency_root",
            })
    )
    for value in integrity_values:
        overlay = _nearest_existing_overlay(_canonical_path(value))
        if overlay is not None and overlay not in integrity_overlays:
            integrity_overlays.append(overlay)

    protected: list[Path] = []
    for value in [*(readonly or []), *integrity_overlays]:
        overlay = _nearest_existing_overlay(_canonical_path(value))
        if overlay is not None and overlay not in protected:
            protected.append(overlay)

    # 移除等于/位于只读覆盖内部的 Core 写根 —— 以及**包含**只读覆盖的宽祖先根。
    #
    # 后者曾经被认为无害（原注释：「祖先宽根仍保留，后续 readonly overlay 会遮住
    # 其中的 state/source 子树」）。2026-09-08 实测证伪：可写根一旦自身落在某个只读
    # 根内部（Project 绑定的 run 里 workspace_root 就住在只读的 project worktree
    # 内），core.isolation._native.write_layers 会把它归入 priority 层，而 bwrap 的
    # 绑定次序是 broad(rw) → readonly(ro) → priority(rw)：priority 在只读之后，于是
    # 这个宽根的 --bind 反过来盖掉了嵌套在它内部的 source_baseline_root 的 --ro-bind，
    # 不可变基线被成功改写。Landlock 更彻底 —— 纯放行清单没有 deny 层，放行祖先就
    # 等于放行全部子树。
    #
    # 只在真的存在嵌套只读覆盖时才丢弃：没有嵌套时宽根无害，保持既有行为。
    writable: list[Path] = []
    for value in base_writable or []:
        path = _canonical_path(value)
        if path is None or any(_inside(path, root) for root in integrity_overlays):
            continue
        if any(_inside(root, path) and root != path for root in integrity_overlays):
            continue
        if path not in writable:
            writable.append(path)

    cwd_path = _canonical_path(cwd)
    target_paths = [
        path for value in (authorized_targets or [])
        if (path := _canonical_path(value)) is not None
    ]

    for role in roles:
        path = _canonical_path(role.path)
        if path is None:
            continue
        cwd_allowed = (
            role.writable and not role.container_only
            and role.role in _BASH_CWD_WRITABLE_ROLES
            and _inside(cwd_path, path)
        )
        target_allowed = (
            role.writable and not role.container_only
            and role.role in _BASH_TARGET_WRITABLE_ROLES
            and any(_inside(target, path) for target in target_paths)
        )
        allowed = cwd_allowed or target_allowed
        if allowed:
            if path == state_root:
                raise BashSandboxContractError(
                    f"framework_state_root_write_forbidden: {role.role}={path}")
            if not _role_has_local_write_capability(state, path):
                raise BashSandboxContractError(
                    f"path_capability_required: {role.role}={path}")
            if not path.exists():
                raise BashSandboxContractError(
                    f"受权路径角色尚未物化：{role.role}={path}")
            if path not in writable:
                writable.append(path)

    return writable, protected

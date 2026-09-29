"""Provisioned build environment support.

The experiment node runs every shell command in a fresh non-login bash.  A
standalone ``export FC=...`` or ``module load ...`` therefore does not persist to
the next ``run_bash`` call.  This module makes the build environment explicit:

* the agent may edit one env script (toolchain intent);
* framework probes source that same script (derived truth);
* major build/run commands source it too (execution consistency);
* coherence checks are deterministic and generic.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "build_env"
ENV_REL_PATH = "env/build_env.sh"


def _state_root(state: Any | None) -> Path:
    root = getattr(state, "root", None)
    if root:
        return Path(root)
    return Path.cwd()


def env_path_for_state(state: Any | None) -> Path:
    return (_state_root(state) / ENV_REL_PATH).expanduser().resolve()


def _quote(path: str | Path) -> str:
    return shlex.quote(str(path))


def source_prefix(env_path: str | Path | None) -> str:
    """Shell prefix that sources the provisioned environment if present."""
    if not env_path:
        return ""
    return (
        "if [ -f /etc/profile.d/modules.sh ]; then . /etc/profile.d/modules.sh >/dev/null 2>&1 || true; fi; "
        f"if [ -f {_quote(env_path)} ]; then . {_quote(env_path)}; fi"
    )


def wrap_command(cmd: str, env_path: str | Path | None) -> str:
    """Run ``cmd`` under the provisioned environment script."""
    prefix = source_prefix(env_path)
    if not prefix:
        return cmd
    stripped = (cmd or "").strip()
    if not stripped:
        return cmd
    if str(env_path) in stripped and re.search(r"(^|[;&|]\s*)\.?\s*source?\s+", stripped):
        return cmd
    return f"{prefix}; {cmd}"


def ensure_env_script(state: Any | None, create_artifact: bool = True) -> dict[str, Any]:
    """Ensure the run has an editable build env script and return its metadata."""
    path = env_path_for_state(state)
    created = False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/usr/bin/env bash\n"
            "# Provisioned build environment for this experiment run.\n"
            "# Agent edits here are allowed; framework probes and major builds source this file.\n"
            "# Keep setup deterministic: module load / conda activate / export CC/CXX/FC/PATH/etc.\n",
            encoding="utf-8",
        )
        created = True
    try:
        path.chmod(path.stat().st_mode | 0o100)
    except Exception:
        pass
    info = env_info(path)
    info["created"] = created
    if create_artifact and state is not None:
        try:
            state.save_artifact(
                ARTIFACT_TYPE,
                f"build_env_{getattr(state, 'run_id', 'run')}",
                path.read_text(encoding="utf-8", errors="replace"),
                metadata={
                    "schema_version": SCHEMA_VERSION,
                    "env_path": str(path),
                    "sha256": info["sha256"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "role": "agent_editable_framework_validated_env",
                },
            )
        except Exception:
            pass
    return info


def env_info(path: str | Path) -> dict[str, Any]:
    p = Path(path).expanduser()
    exists = p.exists()
    text = p.read_text(encoding="utf-8", errors="replace") if exists else ""
    return {
        "schema_version": SCHEMA_VERSION,
        "env_path": str(p.resolve()) if exists else str(p),
        "exists": exists,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "size_bytes": len(text.encode("utf-8")),
        "non_comment_lines": [
            line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ][:80],
    }


def _compiler_family(value: str | None) -> str | None:
    v = (value or "").lower()
    base = os.path.basename(v)
    if not v:
        return None
    if re.search(r"-(?:fc|cc|cxx)=(?:ifx|ifort|icx|icc|icpx|icpc)\b", v):
        return "intel"
    if any(x in base for x in ("nvfortran", "nvc++", "nvc")) or "nvidia" in v or "nvhpc" in v:
        return "nvidia"
    if base in {"gfortran", "gcc", "g++"} or "gcc" in v:
        return "gnu"
    if base in {"ifort", "ifx", "icc", "icx", "icpc", "icpx"} or "oneapi" in v or "intel" in v:
        return "intel"
    if "clang" in base:
        return "llvm"
    return None


def _first_word(value: str | None) -> str:
    try:
        return shlex.split(value or "")[0]
    except Exception:
        return (value or "").split()[0] if value else ""


def _selected_fc(profile: dict[str, Any]) -> str | None:
    env = profile.get("env_vars") or {}
    fc = env.get("FC") or env.get("F90")
    if fc:
        return str(fc)
    tools = ((profile.get("domains") or {}).get("compiler") or {}).get("tools") or {}
    for name in ("nvfortran", "ifx", "ifort", "gfortran"):
        if name in tools:
            return str((tools[name] or {}).get("path") or name)
    return None


def coherence_check(profile: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic high-value toolchain coherence diagnostics.

    The check is intentionally conservative: ambiguous or missing data becomes
    warnings. Hard errors are reserved for objective facts: a cross-family wrapper
    mismatch, or a wrapper whose default backend compiler is not installed
    (underlying named by -show but unresolvable on PATH).
    """
    selected = _selected_fc(profile)
    selected_family = _compiler_family(selected)
    errors: list[str] = []
    warnings: list[str] = []
    facts: dict[str, Any] = {
        "selected_fc": selected,
        "selected_fc_family": selected_family,
    }

    mpi = profile.get("mpi_wrappers") or {}
    wrapper_families: dict[str, str] = {}
    for wrapper in ("mpif90", "mpifort", "mpiifort", "mpiifx"):
        info = mpi.get(wrapper) or {}
        underlying = info.get("underlying") or _first_word(info.get("show"))
        fam = _compiler_family(underlying or info.get("underlying_path"))
        if fam:
            wrapper_families[wrapper] = fam
        if selected_family and fam and selected_family != fam:
            errors.append(
                f"{wrapper} wrapper targets {fam} compiler ({underlying}), "
                f"but selected FC is {selected_family} ({selected})"
            )
    facts["mpi_wrapper_families"] = wrapper_families

    # Backend existence — family coherence is necessary but NOT sufficient.
    # A wrapper may default to a backend compiler that is not installed: Intel
    # oneAPI >=2024 ships only the LLVM compilers (ifx/icx/icpx), so a wrapper whose
    # default backend is a classic compiler (ifort/icc/icpc) fails with
    # "<backend>: command not found" *inside* the wrapper. ifort and ifx share the
    # same "intel" family, so the family check above cannot catch this. The signal is
    # objective: the wrapper's -show names a backend, but `command -v` for it is empty
    # (underlying_path == ""). Remedy is to retarget the wrapper backend
    # (mpiifort -fc=ifx / mpiicpc -cxx=icpx / mpiicc -cc=icx), NOT to edit PATH or
    # suspect the package source. Applies to all wrappers (Fortran and C/C++).
    missing_backends: dict[str, str] = {}
    backend_alternatives: dict[str, list[dict[str, str]]] = {}
    for wrapper, info in mpi.items():
        if not isinstance(info, dict):
            continue
        underlying = info.get("underlying")
        if not underlying:
            continue
        if not str(info.get("underlying_path") or "").strip():
            missing_backends[wrapper] = str(underlying)
            valid_alts: list[dict[str, str]] = []
            for flag, alt in (info.get("backend_alternatives") or {}).items():
                if not isinstance(alt, dict):
                    continue
                alt_path = str(alt.get("underlying_path") or "").strip()
                if not alt_path:
                    continue
                valid_alts.append({
                    "flag": str(flag),
                    "underlying": str(alt.get("underlying") or ""),
                    "underlying_path": alt_path,
                })
            if valid_alts:
                backend_alternatives[wrapper] = valid_alts
            selected_uses_valid_alt = bool(
                selected and wrapper in str(selected)
                and any(alt["flag"] in str(selected) for alt in valid_alts)
            )
            msg = (
                f"{wrapper} default backend compiler '{underlying}' is not installed "
                f"(wrapper -show names it, but `command -v {underlying}` is empty). "
                f"This is a wrapper backend selection error, not a missing PATH or "
                f"source-compatibility issue: retarget the backend to an installed "
                f"compiler (e.g. mpiifort -fc=ifx / mpiicpc -cxx=icpx / mpiicc -cc=icx)."
            )
            if valid_alts:
                msg += " Validated alternatives: " + ", ".join(
                    f"{wrapper} {alt['flag']} -> {alt['underlying']}" for alt in valid_alts
                )
            if selected_uses_valid_alt:
                warnings.append(msg)
            else:
                errors.append(msg)
    if missing_backends:
        facts["mpi_wrapper_missing_backends"] = missing_backends
    if backend_alternatives:
        facts["mpi_wrapper_backend_alternatives"] = backend_alternatives

    netcdf = profile.get("netcdf") or {}
    nf_fc = ((netcdf.get("nf-config") or {}).get("fc")
             or ((profile.get("domains") or {}).get("io_libraries") or {})
             .get("netcdf", {}).get("nf-config", {}).get("fc"))
    nf_family = _compiler_family(nf_fc)
    if nf_fc:
        facts["netcdf_fortran_fc"] = nf_fc
        facts["netcdf_fortran_fc_family"] = nf_family
    if selected_family and nf_family and selected_family != nf_family:
        errors.append(
            f"NetCDF-Fortran reports FC family {nf_family} ({nf_fc}), "
            f"but selected FC is {selected_family} ({selected})"
        )

    env = profile.get("env_vars") or {}
    has_nvhpc_env = any(env.get(k) for k in ("HPC_SDK", "NVHPC_ROOT"))
    if has_nvhpc_env and selected_family and selected_family != "nvidia":
        warnings.append(
            f"NVHPC environment variables are set, but selected FC family is {selected_family}"
        )
    if not selected:
        warnings.append("No selected Fortran compiler found from FC/F90 or common compiler tools")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error" if errors else ("warning" if warnings else "ok"),
        "facts": facts,
        "errors": errors,
        "warnings": warnings,
    }


def env_fingerprint(env_path: str | Path | None, profile: dict[str, Any] | None = None) -> str:
    env_hash = env_info(env_path)["sha256"] if env_path else "no-env"
    facts = {}
    if profile:
        coherence = profile.get("coherence") or coherence_check(profile)
        facts = coherence.get("facts") or {}
    payload = json.dumps({"env_sha256": env_hash, "facts": facts}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def profile_metadata(env_path: str | Path | None, profile: dict[str, Any]) -> dict[str, Any]:
    coherence = profile.get("coherence") or coherence_check(profile)
    return {
        "schema_version": SCHEMA_VERSION,
        "env": env_info(env_path) if env_path else {"exists": False},
        "coherence": coherence,
        "env_fingerprint": env_fingerprint(env_path, {**profile, "coherence": coherence}),
        "generated_by": "framework_probe_under_env",
    }

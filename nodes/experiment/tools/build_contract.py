"""Generic HPC build contract validation.

The contract is a machine-checkable version of ``declared_route``.  It is
intentionally application-agnostic: it validates route structure, path roles,
toolchain/dependency domains, and evidence presence, but it does not encode how
any specific package should be built.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from core.tool_registry import contract_requirement

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is expected in this project.
    yaml = None


ROUTE_TYPES = {


    "official_build_system",
    "package_manager",
    "container",
    "project_native_manual",
    "manual_component_build",
    "unknown",
}

# ── declared_route 的内容契约 ───────────────────────────────────────────────
#
# 模型是通过 `save_artifact(artifact_type='declared_route', content=<JSON/YAML>)`
# 写这份契约的 —— 而 `save_artifact` 是通用工具，挂不住某一种 artifact 的形状。
# 于是这几个必填字段此前**只活在下面的校验器里**：模型得先写错一版、被
# validate_contract 拒掉，才知道要传什么。
#
# 声明在这里，两个消费者共用：校验器用 `contract_requirement()` 取措辞，
# `describe_contract_requirements()` 把同一份声明讲给模型听。
DECLARED_ROUTE_CONTRACT = {
    "route_type": f"one of {sorted(ROUTE_TYPES)}",
    "activities": "a mapping of compile/run/modify_source booleans",
    "env_domains": "a non-empty mapping; each domain needs at least one objective probe",
    "expected_artifacts": "a non-empty list of build/run products; each item needs a path",
}


def describe_contract_requirements() -> str:
    """把 declared_route 的契约讲给模型 —— 与校验器同源，不是另写一份说明。"""
    from core.tool_registry import _render_content_contract

    return _render_content_contract(DECLARED_ROUTE_CONTRACT).strip()


BASE_REQUIRED_DOMAINS = {"compiler", "build_discovery"}
KNOWN_DOMAINS = {
    "compiler",
    "mpi",
    "gpu",
    "io_libraries",
    "math_libraries",
    "python_runtime",
    "build_discovery",
}

_PATH_ALIASES = {
    "source_baseline_root": ("source_root", "source_dir", "source_path"),
    "source_worktree_root": ("build_src", "work_src"),
    "source_patch_root": ("patch_root", "patch_dir"),
    "experiment_root": ("workdir", "work_dir", "case_root", "case_dir"),
    "build_root": ("build_dir",),
    "run_root": ("run_dir", "output_dir"),
    "dependency_root": ("deps_root", "dependency_dir", "env_root"),
}


def _contract_path(contract: dict[str, Any], role: str) -> tuple[str, str | None]:
    roles = contract.get("path_roles")
    value = roles.get(role) if isinstance(roles, dict) else None
    if isinstance(value, dict):
        value = value.get("path")
    if str(value or "").strip():
        return str(value).strip(), None
    if str(contract.get(role) or "").strip():
        return str(contract[role]).strip(), None
    for alias in _PATH_ALIASES[role]:
        if str(contract.get(alias) or "").strip():
            return str(contract[alias]).strip(), alias
    return "", None


def parse_contract(content: Any) -> dict[str, Any]:
    """Parse declared_route content as JSON/YAML/dict.

    Free-form text is deliberately not accepted as a valid contract; returning an
    empty dict makes the caller report a clear schema error.
    """
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        return {}
    text = content.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    if yaml is not None:
        try:
            parsed = yaml.safe_load(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    return {}


def _blob(*objs: Any) -> str:
    return "\n".join(json.dumps(o, ensure_ascii=False, sort_keys=True)
                     for o in objs if o is not None).lower()


def _risk_signals(platform_profile: dict[str, Any]) -> list[str]:
    risks = platform_profile.get("risk_signals")
    if risks is None:
        risks = platform_profile.get("unresolved_risks")
    return [str(x) for x in (risks or [])]


def infer_required_domains(platform_profile: dict[str, Any] | None = None,
                           source_recon: dict[str, Any] | None = None,
                           prereg_text: str = "") -> list[str]:
    """Infer route-critical domains from generic evidence.

    The result is conservative and generic.  It is a prompt/gate aid, not a
    domain expert: projects can always mark extra domains as required in the
    contract.
    """
    pf = platform_profile or {}
    sr = source_recon or {}
    text = _blob(sr, prereg_text)
    domains = set(BASE_REQUIRED_DOMAINS)

    if re.search(r"\b(mpi|mpirun|mpiexec|mpicc|mpif90|mpifort|srun)\b", text):
        domains.add("mpi")
    if re.search(r"\b(cuda|gpu|openacc|openmp\s+offload|hip|rocm|nvcc|nvidia)\b", text):
        domains.add("gpu")
    if re.search(r"\b(netcdf|hdf5|pnetcdf|adios|parallel\s+io|scorpio|pio)\b", text):
        domains.add("io_libraries")
    if re.search(r"\b(blas|lapack|mkl|openblas|fftw|petsc|scalapack|kokkos)\b", text):
        domains.add("math_libraries")
    if re.search(r"\b(pyproject|setup\.py|requirements|conda|venv|python|pip)\b", text):
        domains.add("python_runtime")

    tools = pf.get("toolchain") or {}
    if isinstance(tools, dict):
        if any(k in tools for k in ("mpicc", "mpif90", "mpifort", "mpirun", "mpiexec")):
            if "mpi" in text:
                domains.add("mpi")
        if any(k in tools for k in ("nvcc", "nvidia-smi")):
            if re.search(r"\b(cuda|gpu|openacc|offload)\b", text):
                domains.add("gpu")

    for risk in _risk_signals(pf):
        rl = risk.lower()
        if re.search(r"\bmpi|mpif|mpicc|mpirun\b", rl):
            domains.add("mpi")
        if re.search(r"cuda|gpu|hip|rocm|nvidia", rl):
            domains.add("gpu")
        if re.search(r"netcdf|hdf5|pnetcdf|adios|io\b", rl):
            domains.add("io_libraries")
        if re.search(r"blas|lapack|mkl|openblas|fftw|petsc", rl):
            domains.add("math_libraries")
        if re.search(r"conda|venv|python|pip", rl):
            domains.add("python_runtime")

    return sorted(domains)


def _has_probe(domain: dict[str, Any]) -> bool:
    probes = domain.get("probes")
    if isinstance(probes, list):
        return any(str(p).strip() for p in probes)
    return bool(str(probes or "").strip())


def _has_selected(domain: dict[str, Any]) -> bool:
    selected = domain.get("selected")
    if isinstance(selected, list):
        return any(str(x).strip() for x in selected)
    if isinstance(selected, dict):
        return any(str(v).strip() for v in selected.values())
    return bool(str(selected or "").strip())


def _risk_has_evidence(item: Any) -> bool:
    if isinstance(item, dict):
        return bool(str(item.get("evidence") or item.get("probe") or "").strip())
    return bool(str(item or "").strip())


def validate_contract(contract_or_content: Any,
                      platform_profile: dict[str, Any] | None = None,
                      source_recon: dict[str, Any] | None = None,
                      prereg_text: str = "") -> dict[str, Any]:
    """Validate a declared_route build contract.

    Returns a deterministic report:
    ``{"valid": bool, "errors": [...], "warnings": [...], "required_domains": [...]}``.
    """
    contract = parse_contract(contract_or_content)
    errors: list[str] = []
    warnings: list[str] = []
    required_domains = infer_required_domains(platform_profile, source_recon, prereg_text)

    if not contract:
        return {
            "valid": False,
            "errors": ["declared_route content is not a structured build contract (JSON/YAML object required)"],
            "warnings": [],
            "required_domains": required_domains,
            "contract": {},
        }

    route_type = str(contract.get("route_type") or "").strip()
    if route_type not in ROUTE_TYPES:
        errors.append(contract_requirement(DECLARED_ROUTE_CONTRACT, "route_type"))

    activities = contract.get("activities")
    if activities is None:
        # Legacy build contracts represented a compile route.
        activities = {"compile": True, "run": bool(_contract_path(contract, "run_root")[0])}
        warnings.append(
            "activities is missing; inferred legacy compile contract")
    elif not isinstance(activities, dict):
        errors.append(contract_requirement(DECLARED_ROUTE_CONTRACT, "activities"))
        activities = {}

    compile_requested = bool(activities.get("compile"))
    run_requested = bool(activities.get("run"))
    modify_source = bool(activities.get("modify_source"))
    uses_source = bool(activities.get(
        "uses_source",
        compile_requested and route_type not in {"package_manager", "container"}))

    paths = {
        role: _contract_path(contract, role)
        for role in _PATH_ALIASES
    }
    for role, (_, alias) in paths.items():
        if alias:
            warnings.append(f"{alias} is a legacy alias; use {role}")

    if uses_source and not paths["source_baseline_root"][0]:
        errors.append("source_baseline_root is required for a source experiment")
    if compile_requested and not paths["build_root"][0]:
        errors.append("build_root is required when activities.compile=true")
    if run_requested and not paths["run_root"][0]:
        errors.append("run_root is required when activities.run=true")
    if modify_source and not paths["source_worktree_root"][0]:
        errors.append(
            "source_worktree_root is required when activities.modify_source=true")
    if modify_source and not contract.get("source_change_record"):
        errors.append(
            "source_change_record (patch/diff/commit evidence) is required "
            "when activities.modify_source=true")
    if (paths["source_baseline_root"][0]
            and paths["source_baseline_root"][0]
            == paths["source_worktree_root"][0]):
        errors.append(
            "source_baseline_root and source_worktree_root must be different")
    baseline = paths["source_baseline_root"][0]
    if baseline:
        baseline_path = Path(baseline).expanduser().resolve(strict=False)
        for role in ("build_root", "run_root"):
            other = paths[role][0]
            if not other:
                continue
            other_path = Path(other).expanduser().resolve(strict=False)
            if (baseline_path == other_path
                    or baseline_path in other_path.parents
                    or other_path in baseline_path.parents):
                errors.append(
                    f"source_baseline_root and {role} must not overlap")

    env_domains = contract.get("env_domains")
    if not isinstance(env_domains, dict) or not env_domains:
        errors.append(contract_requirement(DECLARED_ROUTE_CONTRACT, "env_domains"))
        env_domains = {}

    unknown = sorted(set(env_domains) - KNOWN_DOMAINS)
    if unknown:
        warnings.append(f"unknown env_domains are allowed but not interpreted: {unknown}")

    for domain_name in required_domains:
        domain = env_domains.get(domain_name)
        if not isinstance(domain, dict):
            errors.append(f"env_domains.{domain_name} is required")
            continue
        if domain.get("required", True) is False:
            errors.append(f"env_domains.{domain_name}.required cannot be false for an inferred required domain")
        if not _has_selected(domain):
            errors.append(f"env_domains.{domain_name}.selected is required")
        if not _has_probe(domain):
            errors.append(f"env_domains.{domain_name}.probes must include at least one objective probe")

    for domain_name, domain in env_domains.items():
        if isinstance(domain, dict) and domain.get("required") is True:
            if not _has_selected(domain):
                errors.append(f"env_domains.{domain_name}.selected is required")
            if not _has_probe(domain):
                errors.append(f"env_domains.{domain_name}.probes must include at least one objective probe")

    expected = contract.get("expected_artifacts")
    if not isinstance(expected, list) or not expected:
        errors.append(contract_requirement(DECLARED_ROUTE_CONTRACT, "expected_artifacts"))
    else:
        for i, item in enumerate(expected):
            if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                errors.append(f"expected_artifacts[{i}].path is required")

    cache = contract.get("cache_invalidation")
    if not isinstance(cache, dict):
        errors.append("cache_invalidation mapping is required")
    elif "clean_on_toolchain_change" not in cache:
        warnings.append("cache_invalidation.clean_on_toolchain_change is recommended")

    risks = _risk_signals(platform_profile or {})
    accepted = contract.get("accepted_risks") or []
    if risks:
        has_risk_evidence = isinstance(accepted, list) and any(_risk_has_evidence(x) for x in accepted)
        if not has_risk_evidence:
            errors.append("platform_profile has risk signals; accepted_risks must include evidence or authorization")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "required_domains": required_domains,
        "contract": contract,
    }


def render_contract_template(platform_profile: dict[str, Any] | None = None,
                             source_recon: dict[str, Any] | None = None,
                             prereg_text: str = "") -> str:
    """Render a YAML template for a valid structured declared_route."""
    domains = infer_required_domains(platform_profile, source_recon, prereg_text)
    env_domains = {
        d: {
            "required": True,
            "selected": "<prefix/toolchain/module>",
            "probes": ["<objective command and relevant output>"],
        }
        for d in domains
    }
    template = {
        "route_type": "<official_build_system|package_manager|container|project_native_manual|unknown>",
        "activities": {
            "uses_source": True,
            "compile": True,
            "run": True,
            "modify_source": False,
            "manage_dependencies": False,
        },
        "path_roles": {
            "source_baseline_root": "<absolute immutable source root>",
            "build_root": "<absolute build root>",
            "run_root": "<absolute run root>",
        },
        "env_domains": env_domains,
        "expected_artifacts": [{"path": "<absolute or case-relative path>", "type": "<executable|static_lib|shared_lib|output>"}],
        "cache_invalidation": {"clean_on_toolchain_change": True, "generated_cache_paths": ["<build cache dir>"]},
        "accepted_risks": [{"risk": "<risk signal or none>", "evidence": "<probe or human authorization>"}],
    }
    if yaml is not None:
        return yaml.safe_dump(template, sort_keys=False, allow_unicode=True)
    return json.dumps(template, ensure_ascii=False, indent=2)

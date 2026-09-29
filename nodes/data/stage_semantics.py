"""Shared, discipline-neutral semantics for preprocessing workflow stages."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_SUPERSCRIPT_NUMBER_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
_SCIENTIFIC_VALUE = (
    r"[-+]?\d+(?:\.\d+)?(?:\s*(?:[x×*]\s*10\s*(?:\^\s*[-+]?\d+|"
    r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)|[eE]\s*[-+]?\d+))?"
)


def dependency_artifact_ready(value: Any) -> bool:
    """Validate a file- or directory-backed predecessor artifact contract."""
    if isinstance(value, str):
        return bool(value.strip() and Path(value).expanduser().exists())
    if not isinstance(value, dict):
        return False
    if str(value.get("status") or "").lower() not in {
        "ready", "success", "available", "completed", "complete",
    }:
        return False
    quality_status = str(
        value.get("quality_status")
        or (value.get("quality_gate") or {}).get("status")
        or "pass"
    ).lower()
    if quality_status not in {"pass", "passed", "ok", "success"}:
        return False
    raw_path = str(
        value.get("path") or value.get("file") or value.get("directory")
        or value.get("artifact_path") or ""
    ).strip()
    if not raw_path:
        return False
    root = Path(raw_path).expanduser()
    if not root.exists():
        return False
    required_files = value.get("required_files") or []
    if not isinstance(required_files, list):
        return False
    for required in required_files:
        required_path = Path(str(required)).expanduser()
        candidate = required_path if required_path.is_absolute() else root / required_path
        if not candidate.exists():
            return False
    return True


_FAMILY_PATTERNS = (
    (
        "trajectory_derivation",
        r"(?:snapshot|frame|trajectory).*(?:select|extract|sample)|"
        r"(?:select|extract|sample).*(?:snapshot|frame|trajectory)|"
        r"快照选择|轨迹采样|提取.*快照",
    ),
    ("relaxation", r"relax|optimi[sz]|弛豫|结构优化"),
    ("neb", r"\bneb\b|migration path|扩散势垒|迁移路径"),
    ("aimd", r"\baimd\b|molecular dynamics|分子动力学"),
    ("dfpt", r"\bdfpt\b|electron.phonon|电子.?声子"),
    ("bader", r"\bbader\b"),
    ("band_structure", r"band structure|能带"),
    ("dos", r"\bdos\b|态密度"),
    ("adsorption", r"adsorption|吸附"),
    ("intercalation", r"intercalat|insert|嵌入|插层"),
    ("strain", r"strain|应变"),
    ("control_structure", r"control structure|comparison structure|控制结构|对照结构"),
    ("phonon", r"\bphonon\b|声子"),
    ("static", r"\bstatic\b|single.point|静态|单点"),
)


_STAGE_KIND_PATTERNS = (
    (
        "gate_evaluator",
        r"gate[ _-]*(?:evaluator|decision|check)|decision[ _-]*gate|"
        r"branch[ _-]*(?:condition|decision)|门控|分支判定|判据检查",
    ),
    (
        "mesh_convergence",
        r"mesh[ _-]*(?:convergence|independence|refinement)|"
        r"grid[ _-]*(?:convergence|independence|refinement)|"
        r"(?:coarse|medium|fine)[ _-]*(?:cell|cells|element|elements)|"
        r"grid[ _-]*levels?|(?:cell|element)[ _-]*count.*(?:convergence|criterion)|"
        r"网格(?:收敛|无关性|细化)",
    ),
    (
        "mesh_generation",
        r"(?:mesh|grid)[ _-]*(?:generation|build|creation)|"
        r"(?:generate|build|create)[ _-]*(?:mesh|grid)|网格(?:生成|构建|创建)|离散化|"
        r"\b(?:blockMesh|snappyHexMesh|gmshToFoam)\b|(?:结构化|非结构化|C[型形]|O[型形])网格|"
        r"target[ _-]*(?:cell|element)[ _-]*count|wall[ _-]*y[ _-]*plus|"
        r"streamwise[ _-]*(?:delta|spacing)|(?:mesh|grid)[ _-]*type",
    ),
    (
        "data_derivation",
        r"post[ _-]*process(?:ing)?|data[ _-]*(?:transfer|derivation|analysis)|"
        r"result[ _-]*(?:analysis|extraction)|(?:extract|map|bridge)[ _-]*(?:results?|fields?|data)|"
        r"后处理|数据(?:传递|转换|分析)|结果(?:分析|提取)|流场(?:提取|映射)",
    ),
    (
        "parameter_sweep",
        r"parameter[ _-]*(?:sweep|scan)|(?:design|condition)[ _-]*sweep|"
        r"参数(?:扫描|遍历)|工况扫描",
    ),
)


def stage_text(stage: dict[str, Any]) -> str:
    return " ".join([
        str(stage.get("id") or ""),
        str(stage.get("name") or ""),
        str(stage.get("calculation_type") or ""),
        str(stage.get("solver") or stage.get("application") or ""),
        str(stage.get("model_scale") or ""),
        str(stage.get("constraint_text") or ""),
        json.dumps(stage.get("expected_outputs") or [], ensure_ascii=False, default=str),
        json.dumps(stage.get("parameters") or {}, ensure_ascii=False, default=str),
        json.dumps(stage.get("evidence") or [], ensure_ascii=False, default=str),
    ])


def _semantic_text(stage: dict[str, Any]) -> str:
    """Normalize machine-friendly stage labels before applying generic semantics."""
    return re.sub(r"[_/]+", " ", stage_text(stage))


def family_from_text(value: str) -> str:
    for family, pattern in _FAMILY_PATTERNS:
        if re.search(pattern, str(value or ""), flags=re.I):
            return family
    return ""


def stage_family(stage: dict[str, Any]) -> str:
    return family_from_text(stage_text(stage)) or str(stage.get("id") or "").strip()


def stage_kind(stage: dict[str, Any]) -> str:
    """Return a discipline-neutral execution role for a workflow stage."""
    explicit = str(
        stage.get("stage_kind")
        or (stage.get("parameters") or {}).get("stage_kind")
        or ""
    ).strip()
    if explicit:
        return explicit
    text = _semantic_text(stage)
    # A calculation's acceptance criterion may mention a gate (for example a
    # phonon stability check).  That does not turn the calculation itself into
    # a gate evaluator, so classify that role only from the stage label.
    role_text = re.sub(r"[_/]+", " ", " ".join([
        str(stage.get("name") or ""),
        str(stage.get("calculation_type") or ""),
    ]))
    for kind, pattern in _STAGE_KIND_PATTERNS:
        candidate_text = role_text if kind == "gate_evaluator" else text
        if re.search(pattern, candidate_text, flags=re.I):
            return kind
    return "solver_run"


def execution_kind(stage: dict[str, Any]) -> str:
    explicit = str(
        stage.get("execution_kind")
        or (stage.get("parameters") or {}).get("execution_kind")
        or ""
    ).strip()
    if explicit in {"solver_input", "data_derivation", "asset_generation", "guidance"}:
        return explicit
    kind = stage_kind(stage)
    if kind == "gate_evaluator":
        return "guidance"
    if kind in {"mesh_generation", "mesh_convergence"}:
        return "asset_generation"
    if kind == "data_derivation" or stage_family(stage) == "trajectory_derivation":
        return "data_derivation"
    return "solver_input"


def produces_runtime_result(stage: dict[str, Any]) -> bool:
    """Return whether a dependency needs a completed runtime result.

    Import stages expose an already available input asset, so their consumers
    can start from that asset without waiting for a simulation result.
    """
    kind = stage_kind(stage)
    if kind in {"mesh_generation", "mesh_convergence"}:
        return False
    if re.search(
        r"\b(?:structure|geometry|mesh|data|asset)[ _-]*(?:import|load|read)\b|"
        r"\b(?:import|load|read)[ _-]*(?:structure|geometry|mesh|data|asset)\b|"
        r"导入(?:结构|几何|网格|数据)|(?:结构|几何|网格|数据)导入",
        _semantic_text(stage),
        flags=re.I,
    ):
        return False
    return execution_kind(stage) in {"solver_input", "data_derivation"} or kind == "gate_evaluator"


def is_xfoil_stage(stage: dict[str, Any]) -> bool:
    """Identify an XFOIL stage from its declared fields, not its evidence text."""
    parameters = stage.get("effective_parameters") or stage.get("parameters") or {}
    values = [
        stage.get(key) for key in ("solver", "application", "tool", "software", "name", "calculation_type")
    ]
    values.extend(parameters.get(key) for key in ("solver", "application", "tool", "software"))
    return any(re.search(r"\bXFOIL\b", str(value or ""), re.I) for value in values)


def structure_source_role(stage: dict[str, Any]) -> str:
    explicit = str(
        stage.get("source_artifact_role")
        or (stage.get("parameters") or {}).get("source_artifact_role")
        or ""
    ).strip()
    if explicit:
        return explicit
    # AIMD produces a trajectory; a mention of that output must not make the
    # AIMD input itself depend on an already extracted snapshot collection.
    if stage_family(stage) == "aimd":
        return ""
    return "trajectory" if re.search(r"snapshot|frame|trajectory|快照|轨迹", stage_text(stage), re.I) else ""


def structure_variant(stage: dict[str, Any]) -> str:
    explicit = str((stage.get("parameters") or {}).get("structure_variant") or "").strip()
    if explicit:
        return explicit
    return (
        "modified_composition"
        if re.search(r"intercalat|insert|guest|dop|嵌入|插层|掺杂", stage_text(stage), re.I)
        else "base_structure"
    )


def _scientific_number(value: str) -> float | int | None:
    normalized = str(value or "").strip().translate(_SUPERSCRIPT_NUMBER_MAP)
    multiplication = re.fullmatch(
        r"([-+]?\d+(?:\.\d+)?)\s*[x×*]\s*10\s*(?:\^\s*)?([-+]?\d+)",
        normalized,
        flags=re.I,
    )
    try:
        if multiplication:
            result = float(multiplication.group(1)) * 10 ** int(multiplication.group(2))
        else:
            result = float(re.sub(r"\s+", "", normalized))
    except ValueError:
        return None
    return int(result) if result.is_integer() else result


_PARAMETER_KEY_ALIASES = {
    "alpha": "angle_of_attack",
    "alpha_deg": "angle_of_attack",
    "aoa": "angle_of_attack",
    "angle_of_attack_deg": "angle_of_attack",
    "cfl": "max_co",
    "cfl_max": "max_co",
    "dt": "delta_t",
    "timestep": "delta_t",
    "time_step": "delta_t",
    "tuinf_c": "total_simulation_time_tUinf_over_c",
    "re": "reynolds_number",
    "reynolds": "reynolds_number",
    "ma": "mach_number",
    "mach": "mach_number",
    "encut": "encut",
    "energy_cutoff": "encut",
    "ecut": "encut",
    "k_points": "kmesh",
    "kpoint_mesh": "kmesh",
    "kpoints": "kmesh",
    "young_modulus": "youngs_modulus",
    "youngs_modulus": "youngs_modulus",
    "fsti": "freestream_turbulence_intensity_percent",
}


def canonical_stage_parameter_key(value: str) -> str:
    """Return one stable key for an explicit research-plan parameter."""
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return _PARAMETER_KEY_ALIASES.get(normalized, normalized)


def _explicit_assignment_value(value: str) -> Any:
    raw = str(value or "").strip().strip("`* ")
    raw = re.sub(r"\s*(?:°|deg|K|Pa|bar|eV|Hz|s|seconds?)\s*$", "", raw, flags=re.I)
    if re.fullmatch(_SCIENTIFIC_VALUE, raw):
        parsed = _scientific_number(raw)
        return parsed if parsed is not None else raw
    if re.fullmatch(r"(?i)true|false|yes|no", raw):
        return raw.lower() in {"true", "yes"}
    sequence = [part.strip() for part in re.split(r"[x×,]", raw)]
    if len(sequence) > 1 and all(re.fullmatch(_SCIENTIFIC_VALUE, part) for part in sequence):
        parsed_sequence = [_scientific_number(part) for part in sequence]
        if all(item is not None for item in parsed_sequence):
            return parsed_sequence
    return raw


def _generic_explicit_assignments(value: str) -> dict[str, Any]:
    """Preserve explicit ASCII key/value controls even for an unseen discipline."""
    extracted: dict[str, Any] = {}
    assignment = re.compile(
        r"(?<![A-Za-z0-9_./∞])([A-Za-z][A-Za-z0-9_.+/-]{0,63})\s*"
        r"(?:=|:|：)\s*([^,，;；|\n\r()（）]+)"
    )
    for match in assignment.finditer(str(value or "")):
        key = canonical_stage_parameter_key(match.group(1))
        raw_value = match.group(2).strip()
        if not key or not raw_value:
            continue
        extracted[key] = _explicit_assignment_value(raw_value)
    return extracted


def extract_explicit_stage_parameters(value: str) -> dict[str, Any]:
    """Extract reusable scientific controls from one stage's own evidence text.

    The result is intentionally limited to explicit values. It does not infer
    defaults or borrow values from unrelated stages.
    """
    text = str(value or "")
    patterns = {
        "reynolds_number": (
            rf"(?<![A-Za-z0-9])Re(?:ynolds)?(?![A-Za-z])\s*[=:：]?\s*({_SCIENTIFIC_VALUE})"
        ),
        "mach_number": (
            rf"(?<![A-Za-z0-9])Ma(?:ch)?(?![A-Za-z])\s*[=:：]?\s*({_SCIENTIFIC_VALUE})"
        ),
        "angle_of_attack": (
            rf"(?:(?<![A-Za-z0-9])(?:alpha|AoA)(?![A-Za-z0-9])|α|攻角|迎角)"
            rf"\s*[=:：]?\s*({_SCIENTIFIC_VALUE})\s*(?:°|deg|度)?"
        ),
        "max_co": (
            rf"(?<![A-Za-z0-9])(?:CFL|maxCo)(?![A-Za-z0-9])"
            rf"\s*(?:<=|≤|<|=|:|：)?\s*({_SCIENTIFIC_VALUE})"
        ),
        "freestream_turbulence_intensity_percent": (
            rf"(?:(?<![A-Za-z0-9])FSTI(?![A-Za-z0-9])|"
            rf"free[- ]?stream turbulence intensity|来流湍流度)"
            rf"\s*(?:<=|≤|<|=|:|：)?\s*({_SCIENTIFIC_VALUE})\s*%"
        ),
        "total_simulation_time_tUinf_over_c": (
            rf"(?:t\s*U\s*(?:∞|inf)\s*/\s*c|tU(?:∞|inf)/c)"
            rf"\s*(?:<=|≤|<|=|:|：)?\s*({_SCIENTIFIC_VALUE})"
        ),
        "temperature": rf"\bT\s*[=:：]\s*({_SCIENTIFIC_VALUE})\s*K\b",
        "pressure": rf"\bp\s*[=:：]\s*({_SCIENTIFIC_VALUE})\s*(?:Pa|bar)?\b",
        "delta_t": rf"(?:\bdt\b|\bdeltaT\b|时间步长)\s*[=:：]\s*({_SCIENTIFIC_VALUE})",
    }
    extracted: dict[str, Any] = _generic_explicit_assignments(text)
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        parsed = _scientific_number(match.group(1))
        if parsed is not None:
            extracted[key] = parsed
    dimension = re.search(r"(?<![A-Za-z0-9])([123])\s*D(?![A-Za-z0-9])|([一二三])维", text, re.I)
    if dimension:
        token = dimension.group(1) or dimension.group(2)
        extracted["spatial_dimension"] = (
            {"一": 1, "二": 2, "三": 3}[token]
            if token in {"一", "二", "三"}
            else int(token)
        )
    fidelity = re.search(r"(?<![A-Za-z0-9])(DNS|LES|RANS)(?![A-Za-z0-9])", text, re.I)
    if fidelity:
        extracted["model_fidelity"] = fidelity.group(1).upper()
    if re.search(
        r"\b(?:extend(?:ed)?|continue|continuation|restart|resume)\b|延长|续算|重启",
        text,
        flags=re.I,
    ):
        extracted["restart_from_latest_time"] = True
    return extracted


def extract_explicit_temporal_constraints(value: str) -> list[str]:
    """Return full calendar dates explicitly declared in a planning document.

    A complete date (rather than a bare year from a citation) is a portable
    workflow constraint: it can be checked in a simulation deck, an input
    request, or a data-acquisition description without knowing the discipline.
    """
    text = str(value or "")
    matches = [
        *re.findall(r"\b(19\d{2}|20\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\b", text),
        *re.findall(r"\b(19\d{2}|20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text),
    ]
    return list(dict.fromkeys(
        f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        for year, month, day in matches
    ))


def extract_explicit_parameter_space(value: str) -> dict[str, Any]:
    """Extract explicit discrete dimensions and unresolved continuous ranges.

    This parser is deliberately discipline-neutral.  It expands only values
    written by the plan and records ranges without inventing sample points.
    """
    text = str(value or "")
    dimensions: dict[str, list[Any]] = {}
    ranges: dict[str, dict[str, Any]] = {}
    number = _SCIENTIFIC_VALUE
    unit = r"(?:\s*(?:%|°|deg|K|Pa|bar|eV|Hz|s))?"
    key_pattern = r"(?:α|[A-Za-z][A-Za-z0-9_.+/-]{0,63})"

    for match in re.finditer(
        rf"(?<![A-Za-z0-9_./∞])({key_pattern})\s*=\s*"
        rf"(({number}){unit}(?:\s*[/,，]\s*({number}){unit})+)",
        text,
        flags=re.I,
    ):
        raw_key = "alpha" if match.group(1) == "α" else match.group(1)
        key = canonical_stage_parameter_key(raw_key)
        values = [
            parsed
            for token in re.findall(number, match.group(2), flags=re.I)
            for parsed in [_scientific_number(token)]
            if parsed is not None
        ]
        if key and len(values) >= 2:
            dimensions[key] = list(dict.fromkeys(values))

    for match in re.finditer(
        rf"(?<![A-Za-z0-9_./∞])({key_pattern})\s*=\s*"
        rf"({number}){unit}\s*[-–—]\s*({number}){unit}",
        text,
        flags=re.I,
    ):
        raw_key = "alpha" if match.group(1) == "α" else match.group(1)
        key = canonical_stage_parameter_key(raw_key)
        low = _scientific_number(match.group(2))
        high = _scientific_number(match.group(3))
        if key and low is not None and high is not None:
            ranges[key] = {
                "parameter": key,
                "minimum": low,
                "maximum": high,
                "sampling_status": "not_declared",
                "source_text": match.group(0),
            }

    # Preserve explicit categorical alternatives such as two geometry/model
    # identifiers. Requiring a digit avoids treating prose like "mesh + solver"
    # as a parameter dimension.
    categorical = re.search(
        r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z_-]*\d[A-Za-z0-9_-]*)"
        r"(?:\s*\+\s*([A-Za-z][A-Za-z_-]*\d[A-Za-z0-9_-]*))+",
        text,
    )
    if categorical:
        values = re.findall(
            r"[A-Za-z][A-Za-z_-]*\d[A-Za-z0-9_-]*",
            categorical.group(0),
        )
        if len(values) >= 2:
            dimensions["geometry_variant"] = list(dict.fromkeys(values))

    return {
        "dimensions": [
            {"parameter": key, "values": values}
            for key, values in dimensions.items()
        ],
        "unresolved_ranges": list(ranges.values()),
    }


def requests_parameter_inheritance(value: str) -> bool:
    """Return whether a stage explicitly reuses its predecessor's settings."""
    return bool(re.search(
        r"\b(?:same as|reuse|inherit)(?:\s+(?:settings|parameters|case))?\b|"
        r"同(?:前述|前一|上游|基准|基线|S\d+)|其余设置同|沿用.*(?:设置|参数|工况)",
        str(value or ""),
        flags=re.I,
    ))

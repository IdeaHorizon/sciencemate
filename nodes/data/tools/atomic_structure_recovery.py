"""Evidence-driven recovery of missing atomistic structures."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.state import State
from nodes.data.planning.store import witness_plan_approval
from nodes.data.progress import emit_progress


_STRUCTURE_SUFFIXES = {".cif", ".vasp", ".poscar", ".contcar", ".xyz"}
_REFERENCE_TEXT_SUFFIXES = {".txt", ".md", ".html", ".htm", ".xml", ".json", ".csv", ".tsv"}
_LOCAL_REFERENCE_SUFFIXES = _STRUCTURE_SUFFIXES | _REFERENCE_TEXT_SUFFIXES | {".pdf"}
_FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
_PYMATGEN_PACKAGE = "pymatgen==2026.5.4"
_ELEMENT_NAME_TO_SYMBOL = {
    "hydrogen": "H", "helium": "He", "lithium": "Li", "beryllium": "Be", "boron": "B",
    "carbon": "C", "nitrogen": "N", "oxygen": "O", "fluorine": "F", "neon": "Ne",
    "sodium": "Na", "magnesium": "Mg", "aluminum": "Al", "aluminium": "Al", "silicon": "Si",
    "phosphorus": "P", "sulfur": "S", "sulphur": "S", "chlorine": "Cl", "argon": "Ar",
    "potassium": "K", "calcium": "Ca", "scandium": "Sc", "titanium": "Ti", "vanadium": "V",
    "chromium": "Cr", "manganese": "Mn", "iron": "Fe", "cobalt": "Co", "nickel": "Ni",
    "copper": "Cu", "zinc": "Zn", "gallium": "Ga", "germanium": "Ge", "arsenic": "As",
    "selenium": "Se", "bromine": "Br", "krypton": "Kr", "rubidium": "Rb", "strontium": "Sr",
    "yttrium": "Y", "zirconium": "Zr", "niobium": "Nb", "molybdenum": "Mo", "technetium": "Tc",
    "ruthenium": "Ru", "rhodium": "Rh", "palladium": "Pd", "silver": "Ag", "cadmium": "Cd",
    "indium": "In", "tin": "Sn", "antimony": "Sb", "tellurium": "Te", "iodine": "I",
    "xenon": "Xe", "cesium": "Cs", "caesium": "Cs", "barium": "Ba", "lanthanum": "La",
    "cerium": "Ce", "praseodymium": "Pr", "neodymium": "Nd", "promethium": "Pm", "samarium": "Sm",
    "europium": "Eu", "gadolinium": "Gd", "terbium": "Tb", "dysprosium": "Dy", "holmium": "Ho",
    "erbium": "Er", "thulium": "Tm", "ytterbium": "Yb", "lutetium": "Lu", "hafnium": "Hf",
    "tantalum": "Ta", "tungsten": "W", "rhenium": "Re", "osmium": "Os", "iridium": "Ir",
    "platinum": "Pt", "gold": "Au", "mercury": "Hg", "thallium": "Tl", "lead": "Pb",
    "bismuth": "Bi", "polonium": "Po", "astatine": "At", "radon": "Rn",
}
_ELEMENT_SYMBOLS = set(_ELEMENT_NAME_TO_SYMBOL.values())
_SUBSCRIPT_TRANSLATION = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "structure"


def _frac(value: float) -> float:
    normalized = value % 1.0
    if abs(normalized - 1.0) < 1e-10 or abs(normalized) < 1e-10:
        return 0.0
    return normalized


def _space_group_object(space_group: str):
    """Resolve a space group through an optional general crystallography library."""
    try:
        from pymatgen.symmetry.groups import SpaceGroup
    except ImportError:
        return None
    raw = str(space_group or "").strip()
    number_match = re.search(r"(?:No\.?\s*|#\s*)(\d{1,3})", raw, flags=re.I)
    if number_match is None and re.fullmatch(r"\d{1,3}", raw):
        number_match = re.fullmatch(r"(\d{1,3})", raw)
    if number_match:
        try:
            return SpaceGroup.from_int_number(int(number_match.group(1)))
        except (AttributeError, ValueError):
            pass
    symbol = re.sub(r"\(\s*(?:No\.?\s*)?\d{1,3}\s*\)", "", raw, flags=re.I)
    symbol = " ".join(symbol.split()).strip()
    if not symbol:
        return None
    try:
        return SpaceGroup(symbol)
    except ValueError:
        compact_symbol = re.sub(r"\s+", "", re.sub(r"\s*-\s*", "-", symbol))
        if compact_symbol and compact_symbol != symbol:
            try:
                return SpaceGroup(compact_symbol)
            except ValueError:
                pass
        return None


def _valid_element_symbol(value: str) -> str:
    symbol = str(value or "").strip()
    return symbol if symbol in _ELEMENT_SYMBOLS else ""


def _parse_formula(value: str) -> dict[str, float]:
    compact = str(value or "").translate(_SUBSCRIPT_TRANSLATION).strip()
    compact = re.sub(r"[\s,;]", "", compact)
    compact = re.sub(r"(?:\^?\d*[+-])$", "", compact)
    if not compact or len(compact) > 80:
        return {}

    def read_number(source: str, index: int) -> tuple[float, int]:
        match = re.match(r"\d+(?:\.\d+)?", source[index:])
        if not match:
            return 1.0, index
        return float(match.group(0)), index + len(match.group(0))

    def parse_group(source: str, index: int, stop: str = "") -> tuple[dict[str, float], int] | None:
        parsed: dict[str, float] = {}
        while index < len(source):
            if stop and source[index] == stop:
                return parsed, index + 1
            if source[index] == "(":
                nested = parse_group(source, index + 1, ")")
                if nested is None:
                    return None
                nested_composition, index = nested
                multiplier, index = read_number(source, index)
                for symbol, count in nested_composition.items():
                    parsed[symbol] = parsed.get(symbol, 0.0) + count * multiplier
                continue
            match = re.match(r"[A-Z][a-z]?", source[index:])
            if not match:
                return None
            symbol = _valid_element_symbol(match.group(0))
            if not symbol:
                return None
            index += len(match.group(0))
            count, index = read_number(source, index)
            if count <= 0:
                return None
            parsed[symbol] = parsed.get(symbol, 0.0) + count
        return (parsed, index) if not stop else None

    composition: dict[str, float] = {}
    for part in compact.split("·"):
        if not part:
            return {}
        coefficient, offset = read_number(part, 0)
        if offset == 0:
            coefficient = 1.0
        parsed = parse_group(part, offset)
        if parsed is None or parsed[1] != len(part):
            return {}
        for symbol, count in parsed[0].items():
            composition[symbol] = composition.get(symbol, 0.0) + coefficient * count
    return composition


def _formula_text(composition: dict[str, float]) -> str:
    parts: list[str] = []
    for symbol, count in composition.items():
        if abs(count - 1.0) < 1e-12:
            parts.append(symbol)
        elif float(count).is_integer():
            parts.append(f"{symbol}{int(count)}")
        else:
            parts.append(f"{symbol}{count:g}")
    return "".join(parts)


def _element_from_site_prefix(value: str) -> str:
    match = re.match(r"([A-Z][a-z]?)", str(value or "").strip())
    return _valid_element_symbol(match.group(1)) if match else ""


def _extract_wyckoff_sites(text: str) -> list[dict[str, Any]]:
    sites: list[dict[str, Any]] = []
    pattern = re.compile(
        rf"(?<![A-Za-z0-9])(?:(?P<element>[A-Z][a-z]?\d*)\s*(?:[:=@]\s*)?)?"
        rf"(?P<multiplicity>\d{{1,3}})\s*(?P<letter>[A-Za-z])\s*(?P<suffix>\d*)\s*"
        rf"\(\s*(?P<x>{_FLOAT})\s*,\s*(?P<y>{_FLOAT})\s*,\s*(?P<z>{_FLOAT})\s*\)",
    )
    for match in pattern.finditer(text):
        multiplicity = int(match.group("multiplicity"))
        letter = match.group("letter").lower()
        suffix = match.group("suffix") or ""
        label = f"{multiplicity}{letter}{suffix}"
        element = _element_from_site_prefix(match.group("element") or "")
        sites.append({
            "label": label,
            "multiplicity": multiplicity,
            "letter": letter,
            "representative": [
                float(match.group(name)) for name in ("x", "y", "z")
            ],
            "element": element or "X",
            "element_source": "explicit_wyckoff_site_label" if element else "unresolved",
        })
    return sites


def _expand_supported_wyckoff_positions(space_group: str, sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    group = _space_group_object(space_group)
    if not sites or group is None:
        return []
    coordinates: list[dict[str, Any]] = []
    seen: set[tuple[str, float, float, float]] = set()
    for site in sites:
        try:
            # Published Wyckoff representatives are commonly rounded to four
            # decimals; use a tolerance that preserves those special positions.
            orbit = group.get_orbit(site["representative"], tol=5e-4)
        except (AttributeError, TypeError, ValueError):
            orbit = []
        expected = int(site.get("multiplicity") or 0)
        if expected and len(orbit) != expected:
            continue
        for xyz in orbit:
            normalized = [_frac(float(axis)) for axis in xyz]
            key = (str(site.get("element") or "X"), *(round(value, 10) for value in normalized))
            if key in seen:
                continue
            seen.add(key)
            coordinates.append({
                "element": site.get("element") or "X",
                "fractional": normalized,
                "source": "crystallography_library_symmetry_expansion",
                "wyckoff_label": site.get("label"),
            })
    return coordinates


def _resolve_composition_from_text(text: str) -> dict[str, Any]:
    """Resolve composition without guessing site identities for compounds."""
    evidence: list[dict[str, Any]] = []
    explicit_candidates: list[dict[str, float]] = []
    lowered = text.lower()
    structural_context = (
        r"allotrope|phase|crystal|crystalline|structure|lattice|polymorph|monolayer|"
        r"nanosheet|nanotube|framework|unit\s+cell|material"
    )
    for name, symbol in _ELEMENT_NAME_TO_SYMBOL.items():
        if re.search(rf"\b{name}\b\s+(?:{structural_context})\b", lowered, flags=re.I) or re.search(
            rf"\b(?:{structural_context})\s+(?:of\s+)?\b{name}\b",
            lowered,
            flags=re.I,
        ):
            evidence.append({
                "composition": {symbol: 1.0},
                "source": "element_name_context",
                "matched": name,
                "confidence": "contextual",
            })

    formula_patterns = [
        rf"\b(?:chemical[ \t]+formula|composition|stoichiometry)[ \t]*(?:is|of|:|=)?[ \t]*"
        rf"([A-Z][A-Za-z0-9().·+\-^]{{0,79}})\b(?![A-Za-z])",
        rf"\b(?:elemental|pure|single[- \t]?element)[ \t]+([A-Z][a-z]?)\b(?![A-Za-z])",
    ]
    for pattern in formula_patterns:
        for match in re.finditer(pattern, text):
            parsed = _parse_formula(match.group(1))
            if parsed:
                explicit_candidates.append(parsed)
                evidence.append({
                    "composition": parsed,
                    "source": "explicit_formula_context",
                    "matched": match.group(0)[:80],
                    "confidence": "explicit",
                })

    distinct_explicit = {
        json.dumps(candidate, sort_keys=True)
        for candidate in explicit_candidates
    }
    if len(distinct_explicit) == 1:
        composition = explicit_candidates[0]
        return {
            "status": "resolved",
            "formula": _formula_text(composition),
            "elements": list(composition),
            "stoichiometry": composition,
            "source": "explicit_formula_context",
            "evidence": evidence[:20],
            "conflicts": [],
        }
    if len(distinct_explicit) > 1:
        return {
            "status": "conflict",
            "formula": "",
            "elements": [],
            "stoichiometry": {},
            "source": "",
            "evidence": evidence[:20],
            "conflicts": sorted(distinct_explicit),
        }

    contextual_symbols = {
        next(iter(item["composition"]))
        for item in evidence
        if item.get("source") == "element_name_context"
    }
    if len(contextual_symbols) == 1:
        symbol = next(iter(contextual_symbols))
        return {
            "status": "resolved",
            "formula": symbol,
            "elements": [symbol],
            "stoichiometry": {symbol: 1.0},
            "source": "element_name_context",
            "evidence": evidence[:20],
            "conflicts": [],
        }
    return {
        "status": "unresolved" if not contextual_symbols else "conflict",
        "formula": "",
        "elements": [],
        "stoichiometry": {},
        "source": "",
        "evidence": evidence[:20],
        "conflicts": sorted(contextual_symbols),
    }


def _apply_composition_to_unlabeled_coordinates(evidence: dict[str, Any], composition: str) -> None:
    parsed = _parse_formula(composition)
    resolution = evidence.get("composition_resolution") or {}
    elements = list(parsed) or list(resolution.get("elements") or [])
    element = elements[0] if len(elements) == 1 else ""
    if not element:
        return
    for key in ("expanded_wyckoff_coordinates", "wyckoff_positions"):
        for item in evidence.get(key) or []:
            if item.get("element") == "X":
                item["element"] = element


def _apply_declared_composition(evidence: dict[str, Any], composition: str) -> None:
    declared = _parse_formula(composition)
    if not declared:
        if str(composition or "").strip():
            evidence["composition_resolution"] = {
                **(evidence.get("composition_resolution") or {}),
                "status": "conflict",
                "conflicts": [
                    *(evidence.get("composition_resolution") or {}).get("conflicts", []),
                    {
                        "kind": "invalid_declared_composition",
                        "value": str(composition),
                    },
                ],
            }
        return

    structural_elements = {
        _valid_element_symbol(item.get("element"))
        for item in [
            *(evidence.get("fractional_coordinates") or []),
            *(evidence.get("wyckoff_positions") or []),
        ]
    } - {""}
    conflicts: list[Any] = []
    if structural_elements and structural_elements != set(declared):
        conflicts.append({
            "kind": "declared_composition_site_element_mismatch",
            "declared_elements": sorted(declared),
            "structural_elements": sorted(structural_elements),
        })
    evidence["composition_resolution"] = {
        "status": "conflict" if conflicts else "resolved",
        "formula": _formula_text(declared),
        "elements": list(declared),
        "stoichiometry": declared,
        "source": "declared_composition_parameter",
        "evidence": [
            {
                "composition": declared,
                "source": "declared_composition_parameter",
                "matched": str(composition),
                "confidence": "explicit",
            }
        ],
        "conflicts": conflicts,
        "structural_elements": sorted(structural_elements),
    }
    evidence["inferred_composition"] = _formula_text(declared) if not conflicts else ""
    evidence["composition_evidence"] = list(evidence["composition_resolution"]["evidence"])
    if not conflicts:
        _apply_composition_to_unlabeled_coordinates(evidence, composition)


def _coordinate_evidence(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = evidence.get("fractional_coordinates") or []
    if explicit:
        return explicit
    return evidence.get("expanded_wyckoff_coordinates") or []


def _unresolved_coordinate_species(evidence: dict[str, Any]) -> list[str]:
    unresolved: list[str] = []
    for item in _coordinate_evidence(evidence):
        symbol = str(item.get("element") or "").strip()
        if symbol == "X" or symbol not in _ELEMENT_SYMBOLS:
            unresolved.append(symbol or "<missing>")
    return sorted(set(unresolved))


def _composition_is_resolved(evidence: dict[str, Any]) -> bool:
    resolution = evidence.get("composition_resolution") or {}
    return bool(
        resolution.get("status") == "resolved"
        and resolution.get("elements")
        and not resolution.get("conflicts")
    )


def _merge_structural_composition(
    resolution: dict[str, Any],
    coordinates: list[dict[str, Any]],
    wyckoff_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    coordinate_elements = {
        _valid_element_symbol(item.get("element"))
        for item in coordinates
    }
    site_elements = {
        _valid_element_symbol(item.get("element"))
        for item in wyckoff_positions
    }
    structural_elements = sorted((coordinate_elements | site_elements) - {""})
    if not structural_elements:
        return resolution

    formula_elements = set(resolution.get("elements") or [])
    conflicts = list(resolution.get("conflicts") or [])
    if formula_elements and formula_elements != set(structural_elements):
        conflicts.append({
            "kind": "formula_site_element_mismatch",
            "formula_elements": sorted(formula_elements),
            "structural_elements": structural_elements,
        })
        return {
            **resolution,
            "status": "conflict",
            "conflicts": conflicts,
            "structural_elements": structural_elements,
        }

    counts: dict[str, float] = {}
    source = "explicit_coordinate_rows" if coordinate_elements - {""} else "explicit_wyckoff_site_labels"
    source_items = coordinates if coordinate_elements - {""} else wyckoff_positions
    for item in source_items:
        symbol = _valid_element_symbol(item.get("element"))
        if symbol:
            counts[symbol] = counts.get(symbol, 0.0) + float(item.get("multiplicity") or 1.0)
    return {
        **resolution,
        "status": "resolved",
        "formula": resolution.get("formula") or _formula_text(counts),
        "elements": structural_elements,
        "stoichiometry": resolution.get("stoichiometry") or counts,
        "source": source,
        "structural_elements": structural_elements,
        "conflicts": conflicts,
    }


def _extract_evidence(texts: list[dict[str, Any]]) -> dict[str, Any]:
    combined = "\n".join(
        str(item.get("text") or item.get("abstract") or item.get("snippet") or "")
        for item in texts
    )
    lattice: dict[str, float] = {}
    equal_axes = re.search(
        rf"\ba\s*=\s*b\s*=\s*({_FLOAT})\s*(?:Å|A|angstrom)?",
        combined,
        flags=re.I,
    )
    if equal_axes:
        lattice["a"] = float(equal_axes.group(1))
        lattice["b"] = float(equal_axes.group(1))
    for axis in "abc":
        match = re.search(
            rf"\b{axis}\s*[=:]\s*({_FLOAT})\s*(?:Å|A|angstrom)?",
            combined,
            flags=re.I,
        )
        if match:
            lattice[axis] = float(match.group(1))
    space_group = ""
    numbered_match = re.search(
        r"(?:space\s*group|空间群)\s*(?:is|of|为|[:=])?\s*"
        r"([A-Za-z][A-Za-z0-9_\- /]{0,50}?)\s*"
        r"\(\s*(?:No\.?\s*)?(\d{1,3})\s*\)",
        combined,
        flags=re.I,
    )
    if numbered_match:
        space_group = " ".join(numbered_match.group(1).split()) + f" (No. {numbered_match.group(2)})"
    match = re.search(
        r"(?:space\s*group|空间群)\s*(?:is|of|为|[:=])?\s*"
        r"([A-Za-z][A-Za-z0-9_\- /]{0,30}?)(?=\s*\(\s*(?:No\.?\s*)?\d{1,3}\s*\)|"
        r"[,;.，；。\n]|\s+with\b)",
        combined,
        flags=re.I,
    )
    if match and not space_group:
        space_group = " ".join(match.group(1).split())
    if not space_group:
        match = re.search(
            r"(?:space\s*group|空间群)\s*(?:is|of|为|[:=])?\s*"
            r"([A-Za-z][A-Za-z0-9_\- /]{0,30}?)\s*"
            r"\(\s*(?:No\.?\s*)?(\d{1,3})\s*\)",
            combined,
            flags=re.I,
        )
        if match:
            space_group = " ".join(match.group(1).split()) + f" (No. {match.group(2)})"
    lattice_angles: dict[str, float] = {}
    for name, aliases in {
        "alpha": r"(?:alpha|α)",
        "beta": r"(?:beta|β)",
        "gamma": r"(?:gamma|γ)",
    }.items():
        angle_match = re.search(
            rf"\b{aliases}\s*[=:]\s*({_FLOAT})\s*(?:deg|degree|°)?",
            combined,
            flags=re.I,
        )
        if angle_match:
            lattice_angles[name] = float(angle_match.group(1))

    coordinates: list[dict[str, Any]] = []
    row = re.compile(
        rf"(?m)^\s*([A-Z][a-z]?)(?:\d+)?\s+(?:[A-Za-z0-9()_\-]+\s+)?"
        rf"({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s*$"
    )
    for match in row.finditer(combined):
        xyz = [float(match.group(index)) for index in (2, 3, 4)]
        element = _valid_element_symbol(match.group(1))
        if element and all(-0.001 <= value <= 1.001 for value in xyz):
            coordinates.append({
                "element": element,
                "fractional": xyz,
                "element_source": "explicit_coordinate_row",
            })
    wyckoff_positions = _extract_wyckoff_sites(combined)
    expanded_wyckoff = _expand_supported_wyckoff_positions(space_group, wyckoff_positions)
    composition_resolution = _merge_structural_composition(
        _resolve_composition_from_text(combined),
        coordinates,
        wyckoff_positions,
    )
    inferred_composition = (
        str(composition_resolution.get("formula") or "")
        if composition_resolution.get("status") == "resolved"
        else ""
    )
    return {
        "lattice": lattice,
        "lattice_angles": lattice_angles,
        "space_group": space_group,
        "inferred_composition": inferred_composition,
        "composition_resolution": composition_resolution,
        "composition_evidence": list(composition_resolution.get("evidence") or [])[:20],
        "wyckoff_sites_mentioned": bool(
            wyckoff_positions
            or re.search(r"\bwyckoff\b|\bwickoff\b|Wyckoff\s*位点", combined, flags=re.I)
        ),
        "wyckoff_positions": wyckoff_positions,
        "expanded_wyckoff_coordinates": expanded_wyckoff,
        "fractional_coordinates": coordinates,
        "has_complete_orthogonal_lattice": all(axis in lattice for axis in "abc"),
    }


def _inspect_cif_atom_sites(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if line.lower() != "loop_":
            continue
        headers: list[str] = []
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].startswith("_"):
            headers.append(lines[cursor].split()[0].lower())
            cursor += 1
        required = {
            "_atom_site_fract_x",
            "_atom_site_fract_y",
            "_atom_site_fract_z",
        }
        if not required <= set(headers):
            continue
        symbol_key = (
            "_atom_site_type_symbol"
            if "_atom_site_type_symbol" in headers
            else "_atom_site_label"
            if "_atom_site_label" in headers
            else ""
        )
        if not symbol_key:
            continue
        symbol_index = headers.index(symbol_key)
        coordinate_indices = [headers.index(key) for key in sorted(required)]
        species: list[str] = []
        rows = 0
        while cursor < len(lines):
            row = lines[cursor]
            if not row or row.startswith("#"):
                cursor += 1
                continue
            if row.startswith("_") or row.lower() == "loop_" or row.lower().startswith("data_"):
                break
            try:
                values = shlex.split(row, comments=True)
            except ValueError:
                break
            if len(values) < len(headers):
                break
            symbol = _element_from_site_prefix(values[symbol_index])
            if not symbol:
                return {"valid": False, "error": f"Unresolved CIF atom-site species: {values[symbol_index]}"}
            try:
                for coordinate_index in coordinate_indices:
                    float(re.sub(r"\([^)]*\)$", "", values[coordinate_index]))
            except ValueError:
                return {"valid": False, "error": "Invalid CIF fractional coordinate value."}
            species.append(symbol)
            rows += 1
            cursor += 1
        if rows:
            return {
                "valid": True,
                "species": list(dict.fromkeys(species)),
                "atom_site_rows": rows,
            }
    return {"valid": False, "error": "No complete CIF fractional atom-site loop was found."}


def _inspect_structure_file(path: Path, expected_atom_count: int | None) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "suffix": path.suffix.lower(),
        "valid": False,
    }
    if path.suffix.lower() == ".cif":
        result["format"] = "cif"
        cif_sites = _inspect_cif_atom_sites(text)
        result.update(cif_sites)
        result["valid"] = bool(
            cif_sites.get("valid")
            and "_cell_length_a" in text.lower()
        )
    else:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        result["format"] = "poscar_or_vasp"
        if len(lines) >= 8:
            species = lines[5].split()
            has_species_line = bool(species) and all(not token.isdigit() for token in species)
            if has_species_line:
                result["species"] = species
                unknown_species = [
                    token for token in species
                    if token == "X" or token not in _ELEMENT_SYMBOLS
                ]
                result["unknown_species"] = unknown_species
            else:
                unknown_species = []
            try:
                counts = [int(value) for value in lines[6].split()]
            except ValueError:
                counts = []
            result["atom_count"] = sum(counts)
            result["valid"] = bool(
                counts
                and len(lines) >= 8 + sum(counts)
                and has_species_line
                and len(species) == len(counts)
                and not unknown_species
            )
            if result["valid"]:
                try:
                    scale = float(lines[1])
                    vectors = [[float(value) * scale for value in lines[index].split()[:3]] for index in (2, 3, 4)]
                    coordinate_start = 8 if lines[7].lower().startswith(("d", "c", "k")) else 9
                    coordinates = [
                        [float(value) for value in lines[coordinate_start + index].split()[:3]]
                        for index in range(sum(counts))
                    ]
                    volume = abs(
                        vectors[0][0] * (vectors[1][1] * vectors[2][2] - vectors[1][2] * vectors[2][1])
                        - vectors[0][1] * (vectors[1][0] * vectors[2][2] - vectors[1][2] * vectors[2][0])
                        + vectors[0][2] * (vectors[1][0] * vectors[2][1] - vectors[1][1] * vectors[2][0])
                    )
                    minimum = math.inf
                    for left in range(len(coordinates)):
                        for right in range(left + 1, len(coordinates)):
                            delta = [coordinates[left][axis] - coordinates[right][axis] for axis in range(3)]
                            delta = [value - round(value) for value in delta]
                            cart = [sum(delta[row] * vectors[row][column] for row in range(3)) for column in range(3)]
                            minimum = min(minimum, math.sqrt(sum(value * value for value in cart)))
                    result["cell_volume"] = volume
                    result["minimum_periodic_distance"] = None if math.isinf(minimum) else minimum
                    result["valid"] = bool(volume > 1e-6 and (math.isinf(minimum) or minimum >= 0.4))
                except (IndexError, ValueError):
                    result["valid"] = False
                    result["geometry_validation_error"] = "Unable to parse lattice vectors or coordinates."
    atom_count = result.get("atom_count") or result.get("atom_site_rows")
    if expected_atom_count:
        result["expected_atom_count_match"] = atom_count == expected_atom_count
        result["valid"] = bool(result["valid"] and result["expected_atom_count_match"])
    return result


def _extract_pdf_with_pypdf(path: Path, errors: list[str]) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
        try:
            reader = PdfReader(str(path))
            return "\n".join((page.extract_text() or "") for page in reader.pages)[:2_000_000]
        except Exception as exc:
            errors.append(f"pypdf: {type(exc).__name__}: {exc}")
    except ImportError as exc:
        errors.append(f"pypdf: {type(exc).__name__}: {exc}")
    return ""


def _read_reference_document(path: Path, state: State | None = None) -> tuple[str, dict[str, Any] | None]:
    if path.suffix.lower() in _REFERENCE_TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")[:2_000_000], None
    if path.suffix.lower() == ".pdf":
        errors: list[str] = []
        text = _extract_pdf_with_pypdf(path, errors)
        if text:
            return text, None
        try:
            from PyPDF2 import PdfReader as PyPDF2Reader  # type: ignore[import-not-found]
            try:
                reader = PyPDF2Reader(str(path))
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
                return text[:2_000_000], None
            except Exception as exc:
                errors.append(f"PyPDF2: {type(exc).__name__}: {exc}")
        except ImportError as exc:
            errors.append(f"PyPDF2: {type(exc).__name__}: {exc}")
        try:
            completed = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return completed.stdout[:2_000_000], None
            errors.append(f"pdftotext: returncode={completed.returncode}: {completed.stderr[:500]}")
        except Exception as exc:
            errors.append(f"pdftotext: {type(exc).__name__}: {exc}")
        return "", {
            "path": str(path),
            "capability": "pdf_text_extraction",
            "package_type": "python",
            "package": "pypdf",
            "environment_owner": "experiment",
            "error": "; ".join(errors),
        }
    return "", None


def _reference_paths(reference_evidence: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for item in reference_evidence:
        if not isinstance(item, dict):
            continue
        for key in ("path", "file_path", "local_path"):
            value = str(item.get(key) or "").strip()
            if value and value not in paths:
                paths.append(value)
    return paths


def _auto_reference_paths_from_inspected_inputs(state: State) -> list[str]:
    """Reuse local files discovered by inspect_input_path before asking for public search."""
    hook_state = getattr(state, "hook_state", None)
    if not isinstance(hook_state, dict):
        return []
    inspections = hook_state.get("data_input_inspections")
    if not isinstance(inspections, list):
        return []

    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for inspection in reversed(inspections):
        if not isinstance(inspection, dict):
            continue
        for item in inspection.get("files") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            suffix = str(item.get("suffix") or Path(path).suffix).lower()
            if not path or suffix not in _LOCAL_REFERENCE_SUFFIXES:
                continue
            try:
                candidate = Path(path).expanduser()
                if not candidate.is_file():
                    continue
                resolved = str(candidate.resolve())
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            name = candidate.name.lower()
            priority = 30
            if suffix in _STRUCTURE_SUFFIXES or candidate.name.upper() in {"POSCAR", "CONTCAR"}:
                priority = 0
            elif suffix == ".pdf":
                priority = 10
            elif re.search(r"(paper|article|supp|support|reference|si|论文|补充)", name, flags=re.I):
                priority = 15
            candidates.append((priority, resolved))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [path for _priority, path in candidates[:20]]


def _write_candidate_poscar(path: Path, structure_name: str, evidence: dict[str, Any]) -> None:
    _apply_composition_to_unlabeled_coordinates(evidence, str(evidence.get("inferred_composition") or ""))
    if not _composition_is_resolved(evidence):
        raise ValueError("Cannot write POSCAR without a conflict-free resolved composition.")
    unresolved_species = _unresolved_coordinate_species(evidence)
    if unresolved_species:
        raise ValueError(
            "Cannot write POSCAR with unresolved coordinate species: "
            + ", ".join(unresolved_species)
        )
    lattice = evidence["lattice"]
    grouped: dict[str, list[list[float]]] = {}
    species: list[str] = []
    for item in _coordinate_evidence(evidence):
        element = item["element"]
        if element not in grouped:
            species.append(element)
            grouped[element] = []
        grouped[element].append(item["fractional"])
    angles = dict(evidence.get("lattice_angles") or {})
    if len(angles) < 3:
        group = _space_group_object(str(evidence.get("space_group") or ""))
        crystal_system = str(getattr(group, "crystal_system", "") or "").lower()
        if crystal_system in {"cubic", "tetragonal", "orthorhombic"}:
            angles = {"alpha": 90.0, "beta": 90.0, "gamma": 90.0}
        elif crystal_system in {"hexagonal", "trigonal"}:
            angles = {"alpha": 90.0, "beta": 90.0, "gamma": 120.0}
        else:
            raise ValueError(
                "Cannot construct lattice vectors without alpha, beta, gamma or a resolvable "
                "space group with standard conventional-cell angles."
            )
    a, b, c = (float(lattice[axis]) for axis in "abc")
    alpha, beta, gamma = (
        math.radians(float(angles[name])) for name in ("alpha", "beta", "gamma")
    )
    sin_gamma = math.sin(gamma)
    if abs(sin_gamma) < 1e-12:
        raise ValueError("Invalid lattice gamma angle.")
    cx = c * math.cos(beta)
    cy = c * (math.cos(alpha) - math.cos(beta) * math.cos(gamma)) / sin_gamma
    cz_squared = c * c - cx * cx - cy * cy
    if cz_squared <= 1e-12:
        raise ValueError("Lattice lengths and angles do not define a positive-volume cell.")
    lattice_vectors = [
        [a, 0.0, 0.0],
        [b * math.cos(gamma), b * sin_gamma, 0.0],
        [cx, cy, math.sqrt(cz_squared)],
    ]
    lines = [
        f"{structure_name} reconstructed from published crystallographic evidence",
        "1.0",
        *((" ".join(f"{value:.12f}" for value in vector)) for vector in lattice_vectors),
        " ".join(species),
        " ".join(str(len(grouped[item])) for item in species),
        "Direct",
    ]
    for element in species:
        lines.extend(
            " ".join(f"{value:.12f}" for value in xyz)
            for xyz in grouped[element]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _manifest(
    structure_name: str,
    status: str,
    evidence: dict[str, Any],
    search_history: list[dict[str, Any]],
    candidate_path: str = "",
    reason: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    assumptions: list[str] = []
    if not evidence.get("has_complete_orthogonal_lattice"):
        assumptions.append(
            "The complete lattice parameters/vectors are unresolved; no simulation-ready unit cell is assumed."
        )
    if not str(evidence.get("space_group") or "").strip():
        assumptions.append(
            "The authoritative space group is unresolved; symmetry expansion is not assumed."
        )
    if not _coordinate_evidence(evidence):
        assumptions.append(
            "Fractional atomic coordinates are unresolved; POSCAR/CIF generation remains blocked."
        )
    if _unresolved_coordinate_species(evidence):
        assumptions.append(
            "One or more coordinate species are unresolved; placeholder element symbols are not simulation-ready."
        )
    if not _composition_is_resolved(evidence):
        assumptions.append(
            "The chemical composition is unresolved or conflicts with coordinate-site element assignments."
        )
    if not evidence.get("wyckoff_sites_mentioned"):
        assumptions.append(
            "Wyckoff sites or equivalent symmetry-position evidence were not recovered from available sources."
        )
    if status != "success":
        assumptions.append(
            "The structure is not simulation-ready until lattice, coordinates, composition, atom count, and minimum-distance checks pass."
        )
    return {
        "objective": f"Recover an authoritative atomistic structure for {structure_name}",
        "status": status,
        "data_model": {"kind": "atomistic_structure", "format": "POSCAR/CIF"},
        "semantics": {"coordinates": "fractional", "lattice_units": "angstrom"},
        "measurement_context": {"origin": "published or repository crystallographic evidence"},
        "quality_gates": [
            {"name": "complete_lattice", "status": "pass" if evidence.get("has_complete_orthogonal_lattice") else "fail"},
            {"name": "fractional_coordinates", "status": "pass" if _coordinate_evidence(evidence) else "fail"},
            {
                "name": "resolved_coordinate_species",
                "status": "fail" if _unresolved_coordinate_species(evidence) else "pass",
            },
            {
                "name": "resolved_composition",
                "status": "pass" if _composition_is_resolved(evidence) else "fail",
            },
            {
                "name": "wyckoff_symmetry_expansion",
                "status": "pass" if evidence.get("expanded_wyckoff_coordinates") else "not_applicable",
            },
            {"name": "simulation_ready", "status": "pass" if status == "success" else "fail"},
        ],
        "lineage": [{
            "input": "public literature/repositories",
            "op": "crystallographic_evidence_recovery",
            "params": {"structure_name": structure_name},
            "output": candidate_path or "blocked structure contract",
            "timestamp": now,
            "stable_id": hashlib.sha256(f"{structure_name}:{status}:{now}".encode()).hexdigest(),
        }],
        "assumptions": assumptions,
        "reproducibility": {"candidate_path": candidate_path, "evidence_recorded": True},
        "downstream_contract": {
            "simulation_ready": status == "success",
            "blocked_on_missing_structure": status != "success",
            "reason": reason,
            "resume_when": "A verified structure file or complete lattice and fractional coordinates become available.",
        },
        # Keep verbose provenance last. The framework quality summary previews
        # artifact content from the front; putting long search logs first hides
        # quality_gates/lineage/assumptions from the judge and causes false
        # incomplete runs.
        "source": {"search_history": search_history, "crystallographic_evidence": evidence},
    }


async def recover_atomic_structure(
    state: State,
    structure_name: str,
    composition: str = "",
    expected_atom_count: int | None = None,
    source_paths: list[str] | None = None,
    reference_evidence: list[dict[str, Any]] | None = None,
    search_history: list[dict[str, Any]] | None = None,
    operation: str = "assess",
    output_name: str = "POSCAR",
    **_: Any,
) -> dict[str, Any]:
    """Assess evidence, reconstruct from complete coordinates, or emit a blocked dataset."""
    operation = str(operation or "assess").strip().lower()
    emit_progress(
        state,
        "atomic_structure",
        operation,
        structure=structure_name,
        expected_atom_count=expected_atom_count,
    )
    # 判决拆除三波（asr:991 → schema，2026-09-02）：operation 的合法值只在注册
    # schema 的 enum 里声明一次，派发口核一次；内部调用方只传字面量 "assess"。
    expected = int(expected_atom_count) if expected_atom_count else None
    inspected = []
    document_evidence = list(reference_evidence or [])
    document_requirements: list[dict[str, Any]] = []
    all_source_paths = list(source_paths or [])
    for path in _reference_paths(document_evidence):
        if path not in all_source_paths:
            all_source_paths.append(path)
    for path in _auto_reference_paths_from_inspected_inputs(state):
        if path not in all_source_paths:
            all_source_paths.append(path)
    for raw_path in all_source_paths:
        path = Path(raw_path).expanduser()
        recognized = path.suffix.lower() in _STRUCTURE_SUFFIXES or path.name.upper() in {"POSCAR", "CONTCAR"}
        if path.is_file() and recognized:
            inspected.append(_inspect_structure_file(path, expected))
        elif path.is_file():
            text, requirement = _read_reference_document(path, state=state)
            if text:
                document_evidence.append({"source_type": "local_reference_document", "path": str(path), "text": text})
            if requirement:
                document_requirements.append(requirement)
    valid = next((item for item in inspected if item.get("valid")), None)
    evidence = _extract_evidence(document_evidence)
    if composition:
        _apply_declared_composition(evidence, composition)
    else:
        composition = str(evidence.get("inferred_composition") or "")
        _apply_composition_to_unlabeled_coordinates(evidence, composition)
    history = list(search_history or [])
    if valid:
        emit_progress(state, "atomic_structure_done", "existing structure file is valid", structure=structure_name)
        return {"status": "success", "structure_source": valid, "simulation_ready": True, "evidence": evidence}

    coordinate_count = len(_coordinate_evidence(evidence))
    complete_coordinates = bool(coordinate_count and (not expected or coordinate_count == expected))
    unresolved_species = _unresolved_coordinate_species(evidence)
    symmetry_tool_required = bool(
        evidence.get("wyckoff_positions")
        and evidence.get("space_group")
        and not evidence.get("expanded_wyckoff_coordinates")
        and _space_group_object(str(evidence.get("space_group") or "")) is None
    )
    can_reconstruct = bool(
        evidence["has_complete_orthogonal_lattice"]
        and complete_coordinates
        and not unresolved_species
        and _composition_is_resolved(evidence)
    )
    if operation == "assess":
        state.hook_state["pending_atomic_structure_recovery"] = {
            "structure_name": structure_name,
            "composition": composition,
            "expected_atom_count": expected,
            "reference_evidence": document_evidence,
            "source_paths": all_source_paths,
            "search_history": history,
        }
        emit_progress(
            state,
            "atomic_structure_assessed",
            (
                "ready_to_reconstruct"
                if can_reconstruct
                else "externally_blocked"
                if symmetry_tool_required
                else "needs_evidence"
            ),
            coordinate_count=coordinate_count,
        )
        return {
            "status": (
                "ready_to_reconstruct"
                if can_reconstruct else "externally_blocked"
                if symmetry_tool_required else "needs_reference_extraction"
                if document_requirements else "needs_reference_search"
            ),
            "simulation_ready": False,
            "inspected_sources": inspected,
            "crystallographic_evidence": evidence,
            "unresolved_coordinate_species": unresolved_species,
            "document_extraction_requirements": document_requirements,
            "tool_requirements": (
                [{
                    "capability": "general_space_group_symmetry_expansion",
                    "package_type": "python",
                    "package": "pymatgen",
                    "version": _PYMATGEN_PACKAGE.split("==", 1)[1],
                    "import_name": "pymatgen",
                    "environment_owner": "experiment",
                    "reason": (
                        "The evidence contains a space group and representative Wyckoff sites, "
                        "but no general crystallography library is available to expand them."
                    ),
                }]
                if symmetry_tool_required else []
            ),
            "search_plan": {
                "sequence": [
                    "record the missing crystallographic fields in targeted_search_requests",
                    "after plan approval, search publisher article text and supporting information",
                    "after plan approval, search author/research-group repositories and DOI/title leads",
                    "after plan approval, search NOMAD, Materials Cloud, COD, OPTIMADE, Zenodo and Figshare",
                    "check authorized local databases or credentials before requesting user input",
                ],
                "queries": [
                    f'"{structure_name}" {composition} CIF POSCAR fractional coordinates',
                    f'"{structure_name}" lattice parameters space group Wyckoff atomic coordinates paper',
                    f'"{structure_name}" supporting information supplementary structure file',
                ],
                "accept_papers_as_evidence": True,
                "required_for_reconstruction": [
                    "complete lattice vectors or lengths and angles",
                    "all fractional coordinates, or Wyckoff sites plus a symmetry expansion tool",
                    "composition, per-site element assignment, and expected atom count",
                ],
            },
            "next_action": (
                "Do not run public searches before planning approval. Pass this evidence gap into the "
                "preprocessing planning loop as targeted_search_requests; after approval, pass retrieved "
                "article excerpts, tables, and downloaded supplements back as reference_evidence/source_paths."
            ),
        }

    approval_stamp: dict[str, Any] = {}
    if operation == "reconstruct":
        # 判决拆除 O9（随根 store:468 降格，2026-08-31）：未评审执行照跑，
        # 产物打 plan_approval_status:unapproved。
        approval_stamp = witness_plan_approval(state, "recover_atomic_structure")
    output_dir = state.root / ".data_node_work" / "generated" / "structures" / _slug(structure_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    if operation == "reconstruct":
        if not can_reconstruct:
            emit_progress(state, "atomic_structure_blocked", "insufficient evidence", coordinate_count=coordinate_count)
            return {
                "status": "externally_blocked",
                "outcome": "externally_blocked",
                "error": "Published evidence is insufficient for an unambiguous simulation-ready structure.",
                "crystallographic_evidence": evidence,
                "candidate_generation_allowed": False,
                "next_action": "Continue literature recovery or finalize a blocked dataset contract.",
            }
        candidate = output_dir / _slug(output_name)
        _write_candidate_poscar(candidate, structure_name, evidence)
        validation = _inspect_structure_file(candidate, expected)
        status = "success" if validation.get("valid") else "candidate_only"
        manifest = _manifest(structure_name, status, evidence, history, str(candidate))
        if approval_stamp:
            manifest.update(approval_stamp)
        manifest_path = output_dir / "structure_recovery_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "status": "success" if validation.get("valid") else "needs_revision",
            "outcome": "completed" if validation.get("valid") else "revise_asset",
            "simulation_ready": bool(validation.get("valid")),
            "candidate_path": str(candidate),
            "validation": validation,
            "manifest_path": str(manifest_path),
            **approval_stamp,
        }

    reason = "No authoritative structure file or complete crystallographic coordinate set was recovered."
    emit_progress(state, "atomic_structure_blocked", reason, coordinate_count=coordinate_count)
    manifest = _manifest(structure_name, "externally_blocked", evidence, history, reason=reason)
    manifest_path = output_dir / "structure_recovery_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    state.hook_state.pop("pending_atomic_structure_recovery", None)
    return {
        "status": "externally_blocked",
        "outcome": "externally_blocked",
        "simulation_ready": False,
        "manifest_path": str(manifest_path),
        "reason": reason,
        "human_input_required": False,
        "human_input_useful_only_for": [
            "authorized database credentials",
            "a local paper supplement or structure file unknown to the node",
        ],
    }

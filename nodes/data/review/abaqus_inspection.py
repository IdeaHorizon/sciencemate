"""Read-back observations for single-part planar Abaqus decks, not a solver.

Unsupported constructs remain explicit evidence gaps. Keyword equality, separate
node/element set namespaces and instance-qualified references are intentional.
"""
from __future__ import annotations

import math
import re
from collections import Counter


def keyword_block_signatures(text: str, keywords: list[str]) -> dict[str, str]:
    """Hash complete ordered blocks, ignoring comments and line-ending style."""
    import hashlib

    selected = {str(key).strip().lstrip("*").upper(): [] for key in keywords}
    current = ""
    for line in text.splitlines():
        if line.lstrip().startswith("**"):
            continue
        if line.lstrip().startswith("*"):
            current = line.lstrip().split(",", 1)[0][1:].strip().upper()
        if current in selected:
            selected[current].append(line)
    return {key: hashlib.sha256("\n".join(lines).encode()).hexdigest() for key, lines in selected.items()}


def inspect_planar_inp(text: str, geometry: dict | None = None, *, solver: str = "") -> dict:
    try:
        measured = _inspect(text, geometry)
        if "calculix" in solver.casefold():
            unsupported = [name for name in measured["element_types"]
                           if not re.fullmatch(r"(?:CPS|CPE)(?:3|4|4R|6|8|8R)", name)]
            measured["element_format"] = {
                "solver": "CalculiX", "status": "fail" if unsupported else "pass",
                "unsupported_types": unsupported,
                "scope": "Planar continuum element names and their temperature DOF activation under HEAT TRANSFER.",
                "reference": "https://raw.githubusercontent.com/Dhondtguido/CalculiX/master/src/elements.f",
                "thermal_rule": "HEAT TRANSFER selects temperature DOF 11 on these elements; it does not require Abaqus DC2D names.",
                "thermal_reference": "https://web.mit.edu/calculix_v2.7/CalculiX/ccx_2.7/doc/ccx/node132.html",
            }
        return measured
    except (ValueError, KeyError, IndexError, TypeError, ZeroDivisionError) as exc:
        return {"status": "unsupported_or_invalid", "reason": str(exc),
                "scope": "Single-part planar INP read-back; unresolved constructs need another validator."}


def _inspect(text, geometry):
    blocks = []
    keyword_lines = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            fields = [v.strip().upper() for v in line.split(",")]
            keyword_lines.setdefault(fields[0], []).append(number)
            blocks.append((fields[0], dict(v.split("=", 1) for v in fields[1:] if "=" in v),
                           {v for v in fields[1:] if "=" not in v}, []))
        elif blocks:
            blocks[-1][3].append([v.strip() for v in line.split(",") if v.strip()])
    counts = Counter(block[0] for block in blocks)
    allowed = {"*HEADING", "*PREPRINT", "*PART", "*END PART", "*NODE", "*ELEMENT", "*NSET", "*ELSET",
               "*SOLID SECTION", "*MATERIAL", "*ELASTIC", "*DENSITY", "*ASSEMBLY", "*END ASSEMBLY",
               "*INSTANCE", "*END INSTANCE", "*BOUNDARY", "*STEP", "*STATIC", "*CLOAD", "*OUTPUT",
               "*NODE OUTPUT", "*ELEMENT OUTPUT", "*END STEP", "*CONDUCTIVITY", "*HEAT TRANSFER"}
    unverified = set(counts) - allowed
    # Surface definitions/loads do not change the mesh or material read-back.
    # Retain those measurements while explicitly leaving surface/load semantics
    # to the reviewer; unrelated unsupported constructs still stop this reader.
    if unverified - {"*SURFACE", "*DSLOAD"} or any(counts[key] > 1 for key in ("*PART", "*INSTANCE", "*STEP")):
        raise ValueError("Unsupported keywords or multiple parts/instances/steps: " + str(sorted(set(counts)-allowed)))
    nodes, elements, types, nsets, elsets = {}, {}, {}, {}, {}
    sections, materials, boundaries, loads = [], {}, [], []
    instance = material = ""
    parts = {attrs["NAME"] for keyword, attrs, _, _ in blocks if keyword == "*PART"}
    duplicate_ids = 0
    levels = []
    material_levels = {}
    for keyword, attrs, flags, rows in blocks:
        if keyword in {"*PART", "*ASSEMBLY", "*INSTANCE", "*STEP"}:
            expected = ["*ASSEMBLY"] if keyword == "*INSTANCE" else []
            if levels != expected:
                raise ValueError(f"Illegal {keyword} nesting in {levels}")
            levels.append(keyword)
        elif keyword in {"*END PART", "*END ASSEMBLY", "*END INSTANCE", "*END STEP"}:
            if not levels or levels.pop() != keyword.replace("END ", ""):
                raise ValueError(f"Unmatched {keyword}")
        if keyword == "*MATERIAL":
            if levels:
                raise ValueError("*MATERIAL must be defined at model level, outside PART/ASSEMBLY/INSTANCE/STEP")
            material_levels[attrs["NAME"]] = "model"
        if keyword == "*INSTANCE":
            if rows:
                raise ValueError("Instance transformations require full assembly validation")
            instance = attrs["NAME"]
            if attrs.get("PART") not in parts:
                raise ValueError("Instance references an undefined part")
        elif keyword == "*NODE":
            for row in rows:
                n = int(row[0]); duplicate_ids += n in nodes
                nodes[n] = tuple(float(v.replace("D", "E").replace("d", "e")) for v in row[1:])
                if len(nodes[n]) not in (2, 3) or not all(math.isfinite(v) for v in nodes[n]):
                    raise ValueError("Invalid node coordinates")
        elif keyword == "*ELEMENT":
            family = attrs["TYPE"]
            if not re.fullmatch(r"(?:(?:CPS|CPE)(?:3|4|6|8)R?|DC2D(?:3|4|6|8))", family):
                raise ValueError("Unsupported element formulation: " + family)
            for row in rows:
                n = int(row[0]); duplicate_ids += n in elements
                elements[n] = tuple(map(int, row[1:])); types[n] = family
                expected = int(re.search(r"(3|4|6|8)R?$", family)[1])
                if len(elements[n]) != expected:
                    raise ValueError("Wrapped or invalid element connectivity")
                if attrs.get("ELSET"):
                    elsets.setdefault(attrs["ELSET"], set()).add(n)
        elif keyword in {"*NSET", "*ELSET"}:
            sets = nsets if keyword == "*NSET" else elsets
            values = sets.setdefault(attrs[keyword[1:]], set())
            for row in rows:
                if "GENERATE" in flags:
                    start, stop, stride = map(int, row)
                    if stride <= 0 or stop < start:
                        raise ValueError("Invalid generated set range")
                    if (stop-start)//stride+1 > len(nodes)+len(elements):
                        raise ValueError("Generated set range exceeds the defined mesh")
                    values.update(range(start, stop+1, stride))
                else:
                    values.update(int(v) for v in row)
        elif keyword == "*SOLID SECTION":
            sections.append({**attrs, "thickness": float(rows[0][0]) if rows else None})
        elif keyword == "*MATERIAL":
            material = attrs["NAME"]; materials[material] = {}
        elif keyword == "*ELASTIC":
            if attrs or len(rows) != 1 or len(rows[0]) != 2:
                raise ValueError("Only constant isotropic elasticity is measured")
            materials[material]["elastic"] = list(map(float, rows[0]))
        elif keyword == "*CONDUCTIVITY":
            if attrs or len(rows) != 1 or len(rows[0]) != 1:
                raise ValueError("Only constant isotropic conductivity is measured")
            materials[material]["conductivity"] = float(rows[0][0])
        elif keyword == "*BOUNDARY":
            if attrs:
                raise ValueError("Boundary modifiers require full solver validation")
            boundaries.extend(rows)
        elif keyword == "*CLOAD":
            if attrs:
                raise ValueError("Load modifiers require full solver validation")
            loads.extend(rows)
    if levels:
        raise ValueError(f"Unclosed keyword scopes: {levels}")
    # DSLOAD's first field is a surface, not a node/element set. With no
    # SURFACE at all this is a demonstrated defect, including qualified names.
    # INCLUDE and unsupported/multi-part constructs have already exited above.
    missing_surfaces = sorted({row[0] for keyword, _, _, rows in blocks
                               if keyword == "*DSLOAD" and not counts["*SURFACE"] for row in rows})
    if not nodes or not elements or any(abs(p[2]) > 1e-12 for p in nodes.values() if len(p) == 3):
        raise ValueError("Expected a nonempty XY planar mesh")
    def bounds(ids):
        return [[min(nodes[n][axis] for n in ids), max(nodes[n][axis] for n in ids)] for axis in range(2)] if ids else []
    def resolve(reference):
        value = reference.upper()
        if "." in value:
            prefix, value = value.rsplit(".", 1)
            if prefix != instance:
                raise ValueError("Unknown instance: " + prefix)
        return {int(value)} if value.isdigit() else nsets[value]
    used = {n for cell in elements.values() for n in cell}
    if not used <= nodes.keys():
        raise ValueError("Undefined node references")
    covered = set().union(*(elsets[s["ELSET"]] for s in sections))
    boundary_summary = [{"reference": row[0], "controls": row[1:], "node_count": len(resolve(row[0])),
                         "bounds": bounds(resolve(row[0]))} for row in boundaries]
    force_groups = {}
    for reference, dof, value in loads:
        group = force_groups.setdefault(int(dof), {})
        for n in resolve(reference):
            group[n] = group.get(n, 0.0)+float(value)
    load_summary = []
    linear = all(len(cell) in (3, 4) for cell in elements.values())
    for dof, forces in force_groups.items():
        box = bounds(forces)
        summary = {"dof": dof, "node_count": len(forces), "bounds": box, "sum": sum(forces.values()),
                   "force_min": min(forces.values()), "force_max": max(forces.values()),
                   "matching_node_sets": [name for name, ids in nsets.items() if ids == set(forces)]}
        axes = [i for i, (lo, hi) in enumerate(box) if hi > lo]
        if linear and len(axes) == 1 and len(forces) >= 2:
            ordered = sorted(forces, key=lambda n: nodes[n][axes[0]])
            weights = dict.fromkeys(ordered, 0.0)
            for a, b in zip(ordered, ordered[1:]):
                length = math.dist(nodes[a][:2], nodes[b][:2])
                weights[a] += length/2; weights[b] += length/2
            density = [forces[n]/weights[n] for n in ordered]
            summary["linear_edge_force_per_length_range"] = [min(density), max(density)]
        load_summary.append(summary)
    cells = [list(cell[:3 if len(cell) in (3, 6) else 4]) for cell in elements.values()]
    from nodes.data.tools.planar_geometry import check_planar_geometry

    geometry_check = None
    if geometry:
        try:
            geometry_check = check_planar_geometry(nodes, cells, {"geometry": geometry})
        except ValueError as exc:
            geometry_check = {"status": "unresolved", "reason": str(exc)}
    return {"status": "partial" if unverified else "measured", "scope": "Exact keyword single-part XY planar mesh, separate NSET/ELSET, "
            "instance-qualified references, constant isotropic elastic/thermal material and static CLOAD observations; not a solver run.",
            "unverified_keywords": sorted(unverified),
            "unverified_scope": "Surface references and DSLOAD magnitudes/directions are not validated or included in loads." if unverified else "",
            "keyword_lines": keyword_lines,
            "keyword_counts": dict(counts),
            "undefined_surface_load_references": missing_surfaces,
            "model_structure": {"scopes_balanced": True, "material_levels": material_levels,
                "section_material_references_resolved_after_model_read": all(s["MATERIAL"] in materials for s in sections),
                "rule": "Sections in a part may reference model-level materials defined after END PART; material definitions may precede or follow ASSEMBLY.",
                "reference": "https://docs.software.vt.edu/abaqusv2025/English/SIMACAEMODRefMap/simamod-c-partassy.htm"},
            "nodes": len(nodes), "elements": len(elements), "element_types": dict(Counter(types.values())),
            "bounds": bounds(nodes), "duplicate_ids": duplicate_ids,
            "duplicate_nodes": len(nodes)-len(set(nodes.values())),
            "duplicate_elements": len(elements)-len({(types[n], tuple(sorted(cell))) for n, cell in elements.items()}),
            "isolated_nodes": len(nodes.keys()-used),
            "node_sets": {name: {"count": len(ids), "bounds": bounds(ids)} for name, ids in nsets.items()},
            "element_sets": {name: {"count": len(ids), "undefined_ids": len(ids-elements.keys())} for name, ids in elsets.items()},
            "sections": sections, "elements_without_section": len(elements.keys()-covered),
            "undefined_section_elements": len(covered-elements.keys()),
            "undefined_materials": [s["MATERIAL"] for s in sections if s["MATERIAL"] not in materials],
            "materials": materials, "boundaries": boundary_summary, "loads": load_summary,
            "expected_geometry": geometry,
            "geometry_check": geometry_check}

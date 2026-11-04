"""Internal scientific-discipline and data-shape inference helpers."""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from .scientific_assets import inspect_scientific_asset_path


# Solver identity belongs to the lightweight scientific adapter, not the
# universal preprocessing planner.  Adapters can extend this list without
# changing planning or asset-acquisition rules.
SOLVER_ALIASES = (
    "VASP", "CP2K", "Quantum ESPRESSO", "QE", "LAMMPS", "OpenFOAM",
    "GROMACS", "AMBER", "NAMD", "Abaqus", "ANSYS", "COMSOL", "Elmer",
    "SU2", "Code_Aster", "CalculiX", "FEniCS", "MOOSE", "WRF-ARW", "WRF", "WPS",
    "pimpleFoam", "simpleFoam", "icoFoam", "blockMesh", "snappyHexMesh", "Gmsh", "XFOIL",
)


def extract_solver_name(text: str) -> str:
    value = str(text or "")
    for solver in SOLVER_ALIASES:
        if re.search(rf"(?<![A-Za-z0-9_.+-]){re.escape(solver)}(?![A-Za-z0-9_.+-])", value, flags=re.I):
            return solver
    return ""


def discipline_from_solver(text: str) -> str:
    solver = extract_solver_name(text).casefold()
    if solver in {"vasp", "cp2k", "quantum espresso", "qe"}:
        return "electronic_structure"
    if solver in {"lammps", "gromacs", "amber", "namd"}:
        return "md"
    if solver in {"abaqus", "calculix", "code_aster"}:
        return "csm"
    if solver in {"openfoam", "su2"}:
        return "cfd"
    if solver in {"wrf-arw", "wrf", "wps"}:
        return "atmospheric_science"
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# 第一部分：学科领域知识库
# ═══════════════════════════════════════════════════════════════════════════════

DISCIPLINE_REGISTRY: dict[str, dict[str, Any]] = {
    "cfd": {
        "label": "计算流体力学 (CFD)",
        "keywords": [
            "velocity", "pressure", "turbulence", "Reynolds", "Mach",
            "drag", "lift", "airfoil", "boundary_layer", "viscosity",
            "density", "temperature", "enthalpy", "compressible",
            "incompressible", "Navier-Stokes", "RANS", "LES", "DNS",
            "k-epsilon", "k-omega", "SST", "streamline", "vortex",
            "cylinder", "pipe", "nozzle", "diffuser", "channel",
            "流速", "压力", "湍流", "雷诺", "马赫", "阻力", "升力", "翼型",
            "边界层", "粘性", "不可压", "可压", "涡", "圆柱", "管道", "喷管",
        ],
        "file_extensions": [".foam", ".vtk", ".stl", ".obj", ".msh", ".cgns", ".plt"],
        "file_signatures": {
            "OpenFOAM": ["constant/polyMesh", "0/U", "system/controlDict"],
            "VTK": ["# vtk DataFile"],
            "CGNS": ["CGNS"],
            "SU2": ["NDIME=", "NELEM=", "NPOIN="],
            "Fluent": ["(0 \"", "FLUENT"],
        },
        "variable_patterns": [
            r"\b[Uu]\b", r"\b[Pp]\b", r"\bk\b", r"\bomega\b", r"\bepsilon\b",
            r"\bnut\b", r"\balphat\b", r"\bphi\b", r"\brho\b",
        ],
        "typical_dimensions": [2, 3],
        "recommended_solvers": ["OpenFOAM", "SU2", "Fluent", "COMSOL"],
        "mesh_types": [
            "airfoil_ogrid", "airfoil_gmsh", "cylinder_flow", "pipe_flow",
            "converging_diverging_nozzle", "sphere",
            "structured_rect", "unstructured_tet", "unstructured_poly",
            "cartesian_with_amr", "hybrid",
        ],
    },
    "csm": {
        "label": "计算结构力学 (CSM / FEA)",
        "keywords": [
            "stress", "strain", "displacement", "deformation", "Young",
            "Poisson", "modulus", "elasticity", "plasticity", "fracture",
            "fatigue", "buckling", "modal", "frequency", "vibration",
            "finite_element", "FEM", "FEA", "von_Mises", "shear",
            "beam", "cantilever", "plate", "shell", "contact",
            "应力", "应变", "位移", "变形", "弹性", "塑性", "断裂", "模态",
            "梁", "悬臂", "板", "壳",
        ],
        "file_extensions": [".inp", ".rst", ".vtk", ".h5", ".xdmf", ".frd"],
        "file_signatures": {
            "Abaqus": ["*HEADING", "*PART", "*STEP", "*NODE"],
            "ANSYS": ["/PREP7", "/SOLU", "MPDATA"],
            "CalculiX": ["*HEADING", "*NODE", "*ELEMENT"],
            "Code_Aster": ["DEBUT()", "FIN()"],
        },
        "variable_patterns": [
            r"\bdisp[xyz]\b", r"\bstress\b", r"\bstrain\b",
            r"\bUX\b", r"\bUY\b", r"\bUZ\b", r"\bSXYZ\b",
            r"\bsigma\b", r"\bepsilon\b",
        ],
        "typical_dimensions": [2, 3],
        "recommended_solvers": ["Abaqus", "ANSYS", "CalculiX", "Code_Aster", "FEniCS"],
        "mesh_types": [
            "cantilever_beam", "structured_hex", "unstructured_tet",
            "unstructured_hex_dominant", "shell_quad", "beam_line",
        ],
    },
    "cem": {
        "label": "计算电磁学 (CEM)",
        "keywords": [
            "electric", "magnetic", "electromagnetic", "Maxwell", "wave",
            "antenna", "radar", "scattering", "S-parameter", "impedance",
            "permeability", "permittivity", "dielectric", "conductivity",
            "EMC", "EMI", "RF", "microwave", "frequency_domain",
            "waveguide", "resonator", "cavity", "FDTD", "FEM",
            "电场", "磁场", "电磁", "麦克斯韦", "天线", "雷达", "散射",
            "波导", "谐振",
        ],
        "file_extensions": [".csv", ".snp", ".cst", ".aedt", ".out"],
        "file_signatures": {
            "CST": ["CST STUDIO SUITE"],
            "HFSS": ["Ansoft HFSS", "Ansys Electronics Desktop"],
            "NEC": ["CE - Coordinate", "GW - Wire"],
        },
        "variable_patterns": [
            r"\b[Ee][-_]?(field|radiated)\b", r"\b[Hh][-_]?(field|radiated)\b",
            r"\bS\d+\d+\b", r"\bZ\b", r"\bdB\b",
            r"\bE_theta\b", r"\bE_phi\b", r"\bfarfield\b",
        ],
        "typical_dimensions": [2, 3],
        "recommended_solvers": ["CST", "HFSS", "FEKO", "openEMS"],
        "mesh_types": [
            "rectangular_waveguide", "structured_hex", "unstructured_tet",
            "fitted_mesh", "conformal_hex",
        ],
    },
    "heat_transfer": {
        "label": "热传导 / 传热学",
        "keywords": [
            "temperature", "heat_flux", "thermal_conductivity", "convection",
            "radiation", "conduction", "specific_heat", "enthalpy", "Fourier",
            "Newton_cooling", "heat_transfer_coefficient", "thermal_resistance",
            "heat_sink", "fin", "radiator", "cooling", "heating",
            "温度", "热流", "导热", "对流", "辐射", "比热", "散热", "翅片",
        ],
        "file_extensions": [".vtk", ".plt", ".csv", ".h5"],
        "file_signatures": {},
        "variable_patterns": [
            r"\b[Tt]\b", r"\bT_\w+\b", r"\bhtc\b", r"\bq\b", r"\bflux\b",
            r"\balpha\b", r"\bc_p\b", r"\bkappa\b",
        ],
        "typical_dimensions": [1, 2, 3],
        "recommended_solvers": ["OpenFOAM", "COMSOL", "ANSYS", "FEniCS"],
        "mesh_types": [
            "heat_sink", "structured_hex", "unstructured_tet",
            "boundary_layer", "structured_o_grid",
        ],
    },
    "md": {
        "label": "分子动力学 (MD)",
        "keywords": [
            "atom", "molecule", "Lennard-Jones", "LJ", "potential",
            "trajectory", "rdf", "msd", "diffusion", "pair_correlation",
            "NVT", "NPT", "NVE", "thermostat", "barostat", "force_field",
            "EAM", "Tersoff", "Morse", "bond", "angle", "dihedral",
            "原子", "分子", "势函数", "轨迹", "扩散",
        ],
        "file_extensions": [".lammpstrj", ".xyz", ".dcd", ".trr", ".xtc", ".gro", ".top"],
        "file_signatures": {
            "LAMMPS": ["ITEM: TIMESTEP", "ITEM: NUMBER OF ATOMS"],
            "GROMACS": ["# mdrun", "GROMACS"],
            "VMD": ["ITEM: TIMESTEP"],
        },
        "variable_patterns": [
            r"\batom\b", r"\bmol\b", r"\btype\b", r"\bmass\b",
            r"\bx\b.*\by\b.*\bz\b", r"\bfx\b.*\bfy\b.*\bfz\b",
            r"\bpair_coeff\b", r"\bbond_coeff\b",
        ],
        "typical_dimensions": [3],
        "recommended_solvers": ["LAMMPS", "GROMACS", "VASP", "ASE"],
        "mesh_types": ["fcc_lattice", "bcc_lattice", "sc_lattice", "diamond_lattice", "atomistic", "none"],
    },
    "multiphysics": {
        "label": "多物理场耦合",
        "keywords": [
            "coupled", "multiphysics", "FSI", "fluid_structure",
            "conjugate_heat", "CHT", "electromechanical", "thermomechanical",
            "piezoelectric", "magnetohydrodynamic", "MHD",
            "耦合", "流固耦合", "共轭传热", "热力耦合",
        ],
        "file_extensions": [".vtk", ".h5", ".xdmf", ".plt"],
        "file_signatures": {},
        "variable_patterns": [
            r"\bcoupled\b", r"\binterface\b",
        ],
        "typical_dimensions": [2, 3],
        "recommended_solvers": ["COMSOL", "OpenFOAM+CalculiX", "ANSYS", "SU2"],
        "mesh_types": ["hybrid", "conformal_interface", "nonconformal_interface"],
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# 第二部分：学科识别引擎（保留原有 + 增强）
# ═══════════════════════════════════════════════════════════════════════════════

def _score_by_keywords(text: str, discipline_key: str) -> float:
    """根据文本中出现的学科关键词计算得分（指数平滑）。"""
    info = DISCIPLINE_REGISTRY[discipline_key]
    keywords = info["keywords"]
    if not text or not keywords:
        return 0.0
    text_lower = text.lower()
    hits = sum(1 for kw in keywords if re.search(
        rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])"
        if kw.isascii() else re.escape(kw), text_lower,
    ))
    if hits == 0:
        return 0.0
    return 1.0 - math.exp(-0.35 * hits)


def _score_by_file_extension(file_path: str, discipline_key: str) -> float:
    """根据文件扩展名匹配学科得分。"""
    info = DISCIPLINE_REGISTRY[discipline_key]
    exts = info.get("file_extensions", [])
    if not file_path or not exts:
        return 0.0
    suffix = Path(file_path).suffix.lower()
    if suffix in exts:
        return 0.3
    return 0.0


def _score_by_file_signature(file_path: str, discipline_key: str) -> float:
    """读取文件头部，匹配文件签名。"""
    info = DISCIPLINE_REGISTRY[discipline_key]
    sigs = info.get("file_signatures", {})
    if not file_path or not sigs:
        return 0.0
    try:
        p = Path(file_path)
        if not p.exists():
            return 0.0
        if p.is_dir():
            for solver_name, markers in sigs.items():
                if all((p / m).exists() for m in markers):
                    return 0.5
            return 0.0
        head = ""
        try:
            with open(p, errors="ignore") as f:
                head = f.read(4096)
        except (OSError, PermissionError):
            return 0.0
        for solver_name, markers in sigs.items():
            if any(m in head for m in markers):
                return 0.5
    except Exception:
        pass
    return 0.0


def _score_by_variable_names(text: str, discipline_key: str) -> float:
    """根据变量名模式匹配学科（指数平滑）。"""
    info = DISCIPLINE_REGISTRY[discipline_key]
    patterns = info.get("variable_patterns", [])
    if not text or not patterns:
        return 0.0
    hits = sum(1 for pat in patterns if re.search(pat, text))
    if hits == 0:
        return 0.0
    return 1.0 - math.exp(-0.5 * hits)


def _read_text_sample(file_path: str, max_bytes: int = 16384) -> str:
    """读取文件的前 N 字节文本用于分析。目录则列出文件名。"""
    p = Path(file_path)
    if not p.exists():
        return ""
    if p.is_dir():
        names = []
        for root, dirs, files in os.walk(p):
            for f in files:
                names.append(str(Path(root) / f))
            if len(names) > 200:
                break
        return "\n".join(names)
    try:
        with open(p, errors="ignore") as f:
            return f.read(max_bytes)
    except (OSError, PermissionError):
        return ""


def identify_discipline(
    file_path: str | None = None,
    text_sample: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """核心识别逻辑：综合文件签名、关键词、扩展名和变量名模式给出学科判断。"""
    scores: dict[str, float] = {}
    warnings: list[str] = []
    detected_format: str | None = None

    combined_text = ""
    if text_sample:
        combined_text += text_sample + "\n"
    if metadata:
        combined_text += json.dumps(metadata, ensure_ascii=False) + "\n"

    if file_path:
        file_text = _read_text_sample(file_path)
        if file_text:
            combined_text += file_text + "\n"

    for key in DISCIPLINE_REGISTRY:
        s = 0.0
        s += _score_by_keywords(combined_text, key) * 0.35
        if file_path:
            s += _score_by_file_extension(file_path, key) * 0.20
            s += _score_by_file_signature(file_path, key) * 0.30
        s += _score_by_variable_names(combined_text, key) * 0.15
        scores[key] = s

    rankings = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    primary_key, primary_score = rankings[0]
    asset_profile: dict[str, Any] = {}

    # 检测具体文件格式
    if file_path:
        p = Path(file_path)
        asset_profile = inspect_scientific_asset_path(p)
        for key, info in DISCIPLINE_REGISTRY.items():
            sigs = info.get("file_signatures", {})
            if p.is_dir():
                for solver_name, markers in sigs.items():
                    if all((p / m).exists() for m in markers):
                        detected_format = solver_name
                        break
            elif p.is_file():
                try:
                    with open(p, errors="ignore") as f:
                        head = f.read(4096)
                    for solver_name, markers in sigs.items():
                        if any(m in head for m in markers):
                            detected_format = solver_name
                            break
                except (OSError, PermissionError):
                    pass
            if detected_format:
                break

    if primary_score < 0.1:
        solver_discipline = discipline_from_solver(combined_text)
        if solver_discipline:
            # An explicit solver is stronger evidence than a sparse keyword
            # sample (for example, a mesh request that only names OpenFOAM).
            # Reuse the existing solver mapping instead of routing a valid
            # generation request into the unknown-discipline reference path.
            primary_key = solver_discipline
            warnings.append(
                "关键词置信度过低，已依据请求中明确的求解器身份选择适配领域。"
            )
        else:
            primary_key = "unknown"
            warnings.append(
                "置信度过低（<0.1），未能有效识别学科领域。"
                "将继续按数据形态和下游契约规划，不会自动降级到其他学科。"
            )

    discipline_info = DISCIPLINE_REGISTRY.get(primary_key, {
        "label": "未知或尚未注册的科学领域",
        "recommended_solvers": [],
        "mesh_types": [],
        "typical_dimensions": [],
    })

    return {
        "primary_discipline": primary_key,
        "confidence": round(primary_score, 4),
        "rankings": [(k, round(v, 4)) for k, v in rankings],
        "discipline_info": {
            "label": discipline_info["label"],
            "recommended_solvers": discipline_info["recommended_solvers"],
            "mesh_types": discipline_info["mesh_types"],
            "typical_dimensions": discipline_info["typical_dimensions"],
        },
        "detected_format": detected_format,
        "data_model_hint": asset_profile.get("data_model_kind") or "unknown",
        "asset_profile": asset_profile,
        "warnings": warnings,
    }

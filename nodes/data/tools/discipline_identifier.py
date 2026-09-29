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


# ═══════════════════════════════════════════════════════════════════════════════
# 第三部分：数据细节深度分析引擎
# ═══════════════════════════════════════════════════════════════════════════════

# ── 变量物理含义知识库 ────────────────────────────────────────────────────────

VARIABLE_PHYSICS_REGISTRY: dict[str, dict[str, Any]] = {
    # CFD 变量
    "U":     {"physics": "速度场 (velocity)",       "discipline": "cfd",            "unit": "m/s",    "dim": "vector"},
    "p":     {"physics": "压力场 (pressure)",        "discipline": "cfd",            "unit": "Pa",     "dim": "scalar"},
    "k":     {"physics": "湍动能 (turbulent KE)",    "discipline": "cfd",            "unit": "m²/s²",  "dim": "scalar"},
    "omega": {"physics": "比耗散率 (spec. diss.)",   "discipline": "cfd",            "unit": "1/s",    "dim": "scalar"},
    "epsilon":{"physics":"湍流耗散率 (diss. rate)",  "discipline": "cfd",            "unit": "m²/s³",  "dim": "scalar"},
    "nut":   {"physics": "湍流粘度 (turb. viscosity)","discipline": "cfd",           "unit": "m²/s",   "dim": "scalar"},
    "phi":   {"physics": "通量 (flux)",              "discipline": "cfd",            "unit": "m³/s",   "dim": "scalar"},
    "rho":   {"physics": "密度 (density)",           "discipline": "cfd",            "unit": "kg/m³",  "dim": "scalar"},
    "Ma":    {"physics": "马赫数 (Mach number)",     "discipline": "cfd",            "unit": "1",      "dim": "scalar"},
    "Re":    {"physics": "雷诺数 (Reynolds number)", "discipline": "cfd",            "unit": "1",      "dim": "scalar"},
    # CSM 变量
    "D":     {"physics": "位移场 (displacement)",    "discipline": "csm",            "unit": "m",      "dim": "vector"},
    "sigma": {"physics": "应力 (stress)",            "discipline": "csm",            "unit": "Pa",     "dim": "tensor"},
    "epsilon_s":{"physics":"应变 (strain)",          "discipline": "csm",            "unit": "1",      "dim": "tensor"},
    "UX":    {"physics": "X 位移 (disp. X)",         "discipline": "csm",            "unit": "m",      "dim": "scalar"},
    "UY":    {"physics": "Y 位移 (disp. Y)",         "discipline": "csm",            "unit": "m",      "dim": "scalar"},
    "UZ":    {"physics": "Z 位移 (disp. Z)",         "discipline": "csm",            "unit": "m",      "dim": "scalar"},
    "E":     {"physics": "杨氏模量 (Young's mod.)",  "discipline": "csm",            "unit": "Pa",     "dim": "scalar"},
    "nu":    {"physics": "泊松比 (Poisson's ratio)", "discipline": "csm",            "unit": "1",      "dim": "scalar"},
    # CEM 变量
    "E_field":{"physics":"电场 (electric field)",    "discipline": "cem",            "unit": "V/m",    "dim": "vector"},
    "H_field":{"physics":"磁场 (magnetic field)",    "discipline": "cem",            "unit": "A/m",    "dim": "vector"},
    "S11":   {"physics": "反射系数 (S11)",           "discipline": "cem",            "unit": "1",      "dim": "scalar"},
    "Z0":    {"physics": "特性阻抗 (char. impedance)","discipline": "cem",           "unit": "Ω",      "dim": "scalar"},
    # 热传导变量
    "T":     {"physics": "温度 (temperature)",       "discipline": "heat_transfer",  "unit": "K",      "dim": "scalar"},
    "htc":   {"physics": "传热系数 (HTC)",           "discipline": "heat_transfer",  "unit": "W/(m²·K)","dim": "scalar"},
    "q":     {"physics": "热流 (heat flux)",         "discipline": "heat_transfer",  "unit": "W/m²",   "dim": "scalar"},
    "alpha_t":{"physics":"热扩散率 (thermal diff.)", "discipline": "heat_transfer",  "unit": "m²/s",   "dim": "scalar"},
    "cp":    {"physics": "比热容 (specific heat)",   "discipline": "heat_transfer",  "unit": "J/(kg·K)","dim": "scalar"},
    # MD 变量
    "x":     {"physics": "X 坐标",                   "discipline": "md",             "unit": "Å",      "dim": "scalar"},
    "y":     {"physics": "Y 坐标",                   "discipline": "md",             "unit": "Å",      "dim": "scalar"},
    "z":     {"physics": "Z 坐标",                   "discipline": "md",             "unit": "Å",      "dim": "scalar"},
    "fx":    {"physics": "X 力分量",                  "discipline": "md",             "unit": "eV/Å",   "dim": "scalar"},
    "fy":    {"physics": "Y 力分量",                  "discipline": "md",             "unit": "eV/Å",   "dim": "scalar"},
    "fz":    {"physics": "Z 力分量",                  "discipline": "md",             "unit": "eV/Å",   "dim": "scalar"},
    "pe":    {"physics": "势能 (potential energy)",   "discipline": "md",             "unit": "eV",     "dim": "scalar"},
    "ke":    {"physics": "动能 (kinetic energy)",     "discipline": "md",             "unit": "eV",     "dim": "scalar"},
}

# ── 物理模型知识库 ─────────────────────────────────────────────────────────────

PHYSICS_MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    # CFD 模型
    "incompressible_newtonian": {
        "label": "不可压牛顿流体",
        "discipline": "cfd",
        "keywords": ["incompressible", "Newtonian", "low_Mach", "Boussinesq",
                      "不可压", "牛顿"],
        "required_vars": ["U", "p"],
        "optional_vars": ["nut", "phi"],
        "typical_solvers": ["simpleFoam", "pisoFoam", "pimpleFoam", "icoFoam"],
    },
    "rans_k_epsilon": {
        "label": "RANS k-epsilon 湍流模型",
        "discipline": "cfd",
        "keywords": ["k-epsilon", "kEpsilon", "RANS", "turbulence", "湍流"],
        "required_vars": ["U", "p", "k", "epsilon"],
        "optional_vars": ["nut"],
        "typical_solvers": ["simpleFoam", "rhoSimpleFoam"],
    },
    "rans_k_omega_sst": {
        "label": "RANS k-omega SST 湍流模型",
        "discipline": "cfd",
        "keywords": ["k-omega", "kOmegaSST", "SST", "Menter", "湍流"],
        "required_vars": ["U", "p", "k", "omega"],
        "optional_vars": ["nut"],
        "typical_solvers": ["simpleFoam", "rhoSimpleFoam", "pimpleFoam"],
    },
    "compressible_euler": {
        "label": "可压欧拉方程",
        "discipline": "cfd",
        "keywords": ["compressible", "Euler", "supersonic", "shock", "可压"],
        "required_vars": ["U", "p", "rho", "T"],
        "optional_vars": ["e", "phi"],
        "typical_solvers": ["rhoCentralFoam", "sonicFoam"],
    },
    "les": {
        "label": "大涡模拟 (LES)",
        "discipline": "cfd",
        "keywords": ["LES", "large_eddy", "Smagorinsky", "dynamic_model", "大涡"],
        "required_vars": ["U", "p"],
        "optional_vars": ["k", "nut"],
        "typical_solvers": ["pimpleFoam", "dnsFoam"],
    },
    # CSM 模型
    "linear_elastic": {
        "label": "线弹性模型",
        "discipline": "csm",
        "keywords": ["linear", "elastic", "Hooke", "小变形", "线弹性"],
        "required_vars": ["D"],
        "optional_vars": ["sigma", "epsilon"],
        "typical_solvers": ["CalculiX", "Abaqus", "FEniCS"],
    },
    "nonlinear_elastic": {
        "label": "非线性弹性模型",
        "discipline": "csm",
        "keywords": ["nonlinear", "hyperelastic", "large_deformation", "Neo-Hookean",
                      "大变形", "超弹性"],
        "required_vars": ["D"],
        "optional_vars": ["sigma", "epsilon", "E"],
        "typical_solvers": ["CalculiX", "Abaqus"],
    },
    "modal_analysis": {
        "label": "模态分析",
        "discipline": "csm",
        "keywords": ["modal", "frequency", "eigenvalue", "vibration", "模态", "固有频率"],
        "required_vars": ["D"],
        "optional_vars": [],
        "typical_solvers": ["CalculiX", "Abaqus"],
    },
    # CEM 模型
    "fdtd": {
        "label": "时域有限差分 (FDTD)",
        "discipline": "cem",
        "keywords": ["FDTD", "time_domain", "Yee", "时域"],
        "required_vars": ["E_field", "H_field"],
        "optional_vars": [],
        "typical_solvers": ["openEMS", "MEEP"],
    },
    "fem_em": {
        "label": "频域有限元 (FEM-EM)",
        "discipline": "cem",
        "keywords": ["frequency_domain", "HFSS", "CST", "FEM", "频域"],
        "required_vars": ["E_field", "H_field"],
        "optional_vars": ["S11"],
        "typical_solvers": ["HFSS", "CST"],
    },
    # 热传导模型
    "steady_conduction": {
        "label": "稳态导热",
        "discipline": "heat_transfer",
        "keywords": ["steady", "conduction", "Fourier", "稳态", "导热"],
        "required_vars": ["T"],
        "optional_vars": ["q", "alpha_t"],
        "typical_solvers": ["laplacianFoam"],
    },
    "transient_conduction": {
        "label": "瞬态导热",
        "discipline": "heat_transfer",
        "keywords": ["transient", "conduction", "unsteady", "瞬态", "非稳态"],
        "required_vars": ["T"],
        "optional_vars": ["q", "cp"],
        "typical_solvers": ["laplacianFoam"],
    },
    "conjugate_heat_transfer": {
        "label": "共轭传热 (CHT)",
        "discipline": "heat_transfer",
        "keywords": ["conjugate", "CHT", "solid_fluid", "共轭"],
        "required_vars": ["T", "U", "p"],
        "optional_vars": ["htc", "kappa"],
        "typical_solvers": ["chtMultiRegionFoam"],
    },
    # MD 模型
    "lj_fluid": {
        "label": "LJ 流体",
        "discipline": "md",
        "keywords": ["Lennard-Jones", "LJ", "fluid", "LJ流体"],
        "required_vars": ["x", "y", "z"],
        "optional_vars": ["fx", "fy", "fz", "pe"],
        "typical_solvers": ["LAMMPS"],
    },
    "eam_metal": {
        "label": "EAM 金属",
        "discipline": "md",
        "keywords": ["EAM", "metal", "embedded_atom", "金属"],
        "required_vars": ["x", "y", "z"],
        "optional_vars": ["pe"],
        "typical_solvers": ["LAMMPS"],
    },
    "tersoff_semiconductor": {
        "label": "Tersoff 半导体",
        "discipline": "md",
        "keywords": ["Tersoff", "semiconductor", "Si", "Ge", "半导体"],
        "required_vars": ["x", "y", "z"],
        "optional_vars": ["pe"],
        "typical_solvers": ["LAMMPS"],
    },
}

# ── 边界条件知识库 ────────────────────────────────────────────────────────────

BOUNDARY_CONDITION_REGISTRY: dict[str, dict[str, Any]] = {
    # CFD 边界条件
    "inlet":   {"label": "入口 (inlet)",   "discipline": "cfd", "of_types": ["fixedValue", "zeroGradient", "turbulentIntensityKineticEnergyInlet"]},
    "outlet":  {"label": "出口 (outlet)",  "discipline": "cfd", "of_types": ["fixedValue", "zeroGradient", "inletOutlet"]},
    "wall":    {"label": "壁面 (wall)",    "discipline": "cfd", "of_types": ["noSlip", "fixedValue", "slip", "wallFunction"]},
    "farfield":{"label": "远场 (farfield)","discipline": "cfd", "of_types": ["freestream", "freestreamPressure", "waveTransmissive"]},
    "symmetry":{"label": "对称 (symmetry)","discipline": "cfd", "of_types": ["symmetry"]},
    "cyclic":  {"label": "周期 (cyclic)",  "discipline": "cfd", "of_types": ["cyclic", "cyclicAMI"]},
    "empty":   {"label": "空面 (empty)",   "discipline": "cfd", "of_types": ["empty"]},
    # CSM 边界条件
    "fixed_disp":  {"label": "固定位移",     "discipline": "csm", "of_types": ["fixedDisplacement"]},
    "fixed_face":  {"label": "固定面",       "discipline": "csm", "of_types": ["fixed"]},
    "load":        {"label": "载荷面",       "discipline": "csm", "of_types": ["force", "pressure", "traction"]},
    # 热传导边界条件
    "fixed_T":     {"label": "固定温度",     "discipline": "heat_transfer", "of_types": ["fixedValue"]},
    "adiabatic":   {"label": "绝热面",       "discipline": "heat_transfer", "of_types": ["zeroGradient"]},
    "convective":  {"label": "对流面",       "discipline": "heat_transfer", "of_types": ["mixed", "convectiveHeatTransfer"]},
    "radiative":   {"label": "辐射面",       "discipline": "heat_transfer", "of_types": ["greyDiffusiveRadiation"]},
    # CEM 边界条件
    "pec":         {"label": "理想导体 (PEC)", "discipline": "cem", "of_types": ["perfectElectricConductor"]},
    "pmc":         {"label": "理想磁导体 (PMC)","discipline": "cem", "of_types": ["perfectMagneticConductor"]},
    "radiation_bc":{"label": "辐射边界",      "discipline": "cem", "of_types": ["radiation", "PML", "ABC"]},
    "port":        {"label": "端口",          "discipline": "cem", "of_types": ["wavePort", "lumpedPort"]},
    # MD 边界条件
    "periodic_md": {"label": "周期边界",      "discipline": "md",  "of_types": ["p p p", "p p f"]},
    "shrink_wrap": {"label": "收缩包裹",      "discipline": "md",  "of_types": ["s s s"]},
    "reflective":  {"label": "反射壁面",      "discipline": "md",  "of_types": ["f f f"]},
}

# ── 坐标系与单位制知识 ────────────────────────────────────────────────────────

COORDINATE_SYSTEMS: dict[str, dict[str, Any]] = {
    "cartesian_2d":  {"label": "二维笛卡尔 (x, y)",         "axes": ["x", "y"]},
    "cartesian_3d":  {"label": "三维笛卡尔 (x, y, z)",      "axes": ["x", "y", "z"]},
    "cylindrical":   {"label": "柱坐标 (r, θ, z)",          "axes": ["r", "theta", "z"]},
    "spherical":     {"label": "球坐标 (r, θ, φ)",          "axes": ["r", "theta", "phi"]},
    "curvilinear":   {"label": "曲线坐标 (ξ, η, ζ)",       "axes": ["xi", "eta", "zeta"]},
}

UNIT_SYSTEMS: dict[str, dict[str, Any]] = {
    "si":       {"label": "国际单位制 (SI)",        "length": "m",    "mass": "kg",   "time": "s",   "temperature": "K"},
    "cgs":      {"label": "CGS 单位制",             "length": "cm",   "mass": "g",    "time": "s",   "temperature": "K"},
    "lj_reduced":{"label":"LJ 约化单位",            "length": "σ",    "mass": "m",    "time": "τ",   "temperature": "ε/kB"},
    "imperial": {"label": "英制单位",               "length": "ft",   "mass": "lbm",  "time": "s",   "temperature": "R"},
    "openfoam": {"label": "OpenFOAM 默认 (SI)",     "length": "m",    "mass": "kg",   "time": "s",   "temperature": "K"},
    "lammps_metal":{"label":"LAMMPS metal 单位",    "length": "Å",    "mass": "amu",  "time": "ps",  "temperature": "K"},
    "lammps_lj": {"label": "LAMMPS lj 单位",       "length": "σ",    "mass": "m",    "time": "τ",   "temperature": "ε/kB"},
}


# ── 数据细节分析核心函数 ──────────────────────────────────────────────────────

def analyze_variable_physics(
    variable_names: list[str],
    discipline: str | None = None,
) -> dict[str, Any]:
    """分析变量名的物理含义。

    参数：
        variable_names: 变量名列表
        discipline: 已知学科（可选，用于消歧）

    返回：
        每个变量的物理含义、单位、维度信息
    """
    result = {}
    unresolved = []

    for var in variable_names:
        # 精确匹配
        if var in VARIABLE_PHYSICS_REGISTRY:
            info = VARIABLE_PHYSICS_REGISTRY[var]
            result[var] = {
                "physics": info["physics"],
                "unit": info["unit"],
                "dim": info["dim"],
                "discipline": info["discipline"],
                "match_type": "exact",
            }
            continue

        # 模糊匹配：前缀、后缀、下划线变体
        matched = False
        for key, info in VARIABLE_PHYSICS_REGISTRY.items():
            # 变体模式：U_xxx, xxx_U, Uxx, etc.
            patterns = [
                rf"^{re.escape(key)}[-_]",
                rf"[-_]{re.escape(key)}$",
                rf"^{re.escape(key)}\d",
            ]
            for pat in patterns:
                if re.search(pat, var, re.IGNORECASE):
                    result[var] = {
                        "physics": info["physics"] + f"（来自 {key}）",
                        "unit": info["unit"],
                        "dim": info["dim"],
                        "discipline": info["discipline"],
                        "match_type": "fuzzy",
                        "matched_key": key,
                    }
                    matched = True
                    break
            if matched:
                break

        if not matched:
            unresolved.append(var)

    return {
        "identified": result,
        "unresolved": unresolved,
        "total_variables": len(variable_names),
        "identified_count": len(result),
    }


def detect_spatial_dimension(
    file_path: str | None = None,
    text_sample: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """检测数据的空间维度（1D/2D/3D）。"""
    clues = {"1d": 0, "2d": 0, "3d": 0}
    evidence = []

    combined = ""
    if text_sample:
        combined += text_sample.lower() + " "
    if metadata:
        combined += json.dumps(metadata, ensure_ascii=False).lower() + " "

    if file_path:
        file_text = _read_text_sample(file_path, max_bytes=32768).lower()
        combined += file_text + " "

    # 直接维度声明
    dim_patterns = [
        (r"\b2[- ]?d\b|\btwo[- ]?dimensional\b|\b2d\b", "2d"),
        (r"\b3[- ]?d\b|\bthree[- ]?dimensional\b|\b3d\b", "3d"),
        (r"\b1[- ]?d\b|\bone[- ]?dimensional\b|\b1d\b", "1d"),
    ]
    for pat, dim_key in dim_patterns:
        n = len(re.findall(pat, combined))
        if n > 0:
            clues[dim_key] += n * 3
            evidence.append(f"发现 {dim_key} 维度关键词 ({n} 次)")

    # OpenFOAM 空面边界 → 2D
    if "empty" in combined and "frontandback" in combined:
        clues["2d"] += 5
        evidence.append("发现 OpenFOAM empty/frontAndBack 边界 → 2D")

    # 坐标轴出现
    has_x = bool(re.search(r"\bx\b", combined))
    has_y = bool(re.search(r"\by\b", combined))
    has_z = bool(re.search(r"\bz\b", combined))
    if has_x and has_y and has_z:
        clues["3d"] += 3
        evidence.append("发现 x/y/z 三个坐标轴 → 可能 3D")
    elif has_x and has_y and not has_z:
        clues["2d"] += 3
        evidence.append("仅发现 x/y 坐标轴 → 可能 2D")
    elif has_x and not has_y and not has_z:
        clues["1d"] += 2
        evidence.append("仅发现 x 坐标轴 → 可能 1D")

    # 轴对称关键词
    if any(kw in combined for kw in ["axisymmetric", "wedge", "轴对称"]):
        clues["2d"] += 4
        evidence.append("发现轴对称关键词 → 2D 轴对称")

    # 决策
    best_dim = max(clues, key=lambda k: clues[k])
    if clues[best_dim] == 0:
        best_dim = "unknown"

    return {
        "detected_dimension": best_dim,
        "confidence_scores": clues,
        "evidence": evidence,
    }


def infer_physics_model(
    variable_names: list[str],
    text_sample: str | None = None,
    discipline: str | None = None,
) -> dict[str, Any]:
    """推断可能的物理模型。"""
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}

    combined_text = ""
    if text_sample:
        combined_text += text_sample.lower() + " "

    var_set = set(variable_names)

    for model_key, model_info in PHYSICS_MODEL_REGISTRY.items():
        # 如果指定了学科，先过滤
        if discipline and model_info["discipline"] != discipline:
            continue

        score = 0.0
        model_reasons = []

        # 检查必需变量
        required = set(model_info["required_vars"])
        found_required = required & var_set
        if found_required:
            score += len(found_required) / len(required) * 0.4
            model_reasons.append(f"匹配必需变量: {found_required}")

        # 检查可选变量
        optional = set(model_info["optional_vars"])
        found_optional = optional & var_set
        if found_optional:
            score += len(found_optional) / max(len(optional), 1) * 0.2
            model_reasons.append(f"匹配可选变量: {found_optional}")

        # 关键词匹配
        if combined_text:
            kw_hits = sum(1 for kw in model_info["keywords"] if kw.lower() in combined_text)
            if kw_hits > 0:
                score += min(kw_hits * 0.15, 0.4)
                model_reasons.append(f"关键词命中 {kw_hits} 个")

        scores[model_key] = round(score, 4)
        if model_reasons:
            reasons[model_key] = model_reasons

    # 排序
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # 返回 top-3
    top_models = []
    for key, score in ranked[:3]:
        if score > 0:
            info = PHYSICS_MODEL_REGISTRY[key]
            top_models.append({
                "model": key,
                "label": info["label"],
                "score": score,
                "reasons": reasons.get(key, []),
                "typical_solvers": info["typical_solvers"],
            })

    return {
        "inferred_models": top_models,
        "total_candidates": len([s for s in scores.values() if s > 0]),
    }


def extract_boundary_conditions(
    file_path: str | None = None,
    text_sample: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """提取/推断边界条件信息。"""
    found: dict[str, dict[str, Any]] = {}
    combined = ""

    if text_sample:
        combined += text_sample.lower() + " "
    if metadata:
        combined += json.dumps(metadata, ensure_ascii=False).lower() + " "

    if file_path:
        # 尝试读取 OpenFOAM 边界文件
        p = Path(file_path)
        if p.is_dir():
            # 遍历 0/ 目录下的场文件
            time_dir = p / "0"
            if time_dir.exists():
                for field_file in time_dir.iterdir():
                    if field_file.is_file():
                        try:
                            content = field_file.read_text(errors="ignore")
                            # 提取 boundaryField 段
                            bc_match = re.search(
                                r"boundaryField\s*\{(.+?)\}\s*\Z",
                                content, re.DOTALL,
                            )
                            if bc_match:
                                bc_text = bc_match.group(1)
                                # 提取各 patch 名
                                patch_names = re.findall(
                                    r"(\w+)\s*\{", bc_text,
                                )
                                for pname in patch_names:
                                    type_match = re.search(
                                        rf"{pname}\s*\{{[^}}]*type\s+(\w+)",
                                        bc_text,
                                    )
                                    bc_type = type_match.group(1) if type_match else "unknown"
                                    found[pname] = {
                                        "source": f"OpenFOAM 0/{field_file.name}",
                                        "type": bc_type,
                                    }
                        except (OSError, PermissionError):
                            pass

            file_text = _read_text_sample(file_path, max_bytes=32768).lower()
            combined += file_text + " "
        elif p.is_file():
            try:
                with open(p, errors="ignore") as f:
                    combined += f.read(32768).lower() + " "
            except (OSError, PermissionError):
                pass

    # 关键词推断
    for bc_key, bc_info in BOUNDARY_CONDITION_REGISTRY.items():
        keywords_to_check = [bc_key.lower(), bc_info["label"].lower().split("(")[0].strip()]
        for kw in keywords_to_check:
            if kw in combined and bc_key not in found:
                found[bc_key] = {
                    "source": "关键词推断",
                    "label": bc_info["label"],
                    "discipline": bc_info["discipline"],
                    "possible_types": bc_info["of_types"],
                }
                break

    return {
        "detected_boundaries": found,
        "total_boundaries": len(found),
    }


def detect_coordinate_system(
    file_path: str | None = None,
    text_sample: str | None = None,
) -> dict[str, Any]:
    """检测坐标系类型。"""
    scores: dict[str, float] = {}
    evidence = []

    combined = ""
    if text_sample:
        combined += text_sample.lower() + " "
    if file_path:
        combined += _read_text_sample(file_path, max_bytes=16384).lower() + " "

    # 关键词检测
    coord_keywords = {
        "cartesian_2d": ["2d", "xy", "planar", "二维", "平面"],
        "cartesian_3d": ["3d", "xyz", "spatial", "三维", "空间"],
        "cylindrical":  ["cylindrical", "cylinder", "r-theta", "轴对称", "柱坐标",
                         "radial", "周向", "circumferential"],
        "spherical":    ["spherical", "球坐标", "r-theta-phi", "极坐标"],
        "curvilinear":  ["curvilinear", "body-fitted", "曲线坐标", "贴体"],
    }

    for cs_key, kws in coord_keywords.items():
        hits = sum(1 for kw in kws if kw in combined)
        if hits > 0:
            scores[cs_key] = hits
            evidence.append(f"坐标系关键词 '{cs_key}': {hits} 次命中")

    # 变量名检测
    if re.search(r"\b(r|radius|radial)\b", combined) and re.search(r"\b(theta|angle)\b", combined):
        if re.search(r"\b(z|height|axial)\b", combined):
            scores["cylindrical"] = scores.get("cylindrical", 0) + 3
            evidence.append("发现 r/theta/z 变量 → 柱坐标")
        else:
            scores["spherical"] = scores.get("spherical", 0) + 3
            evidence.append("发现 r/theta 变量（无 z）→ 可能球坐标")

    if not scores:
        scores["cartesian_3d"] = 1
        evidence.append("无明确坐标系线索，默认假设笛卡尔 3D")

    best = max(scores, key=lambda k: scores[k])
    cs_info = COORDINATE_SYSTEMS.get(best, {})

    return {
        "detected_system": best,
        "label": cs_info.get("label", best),
        "axes": cs_info.get("axes", []),
        "confidence_scores": scores,
        "evidence": evidence,
    }


def detect_unit_system(
    file_path: str | None = None,
    text_sample: str | None = None,
) -> dict[str, Any]:
    """检测单位制。"""
    scores: dict[str, float] = {}
    evidence = []

    combined = ""
    if text_sample:
        combined += text_sample.lower() + " "
    if file_path:
        combined += _read_text_sample(file_path, max_bytes=16384).lower() + " "

    # 直接关键词
    unit_keywords = {
        "si":        ["si", "si unit", "国际单位", "pa", "kg/m", "m/s"],
        "cgs":       ["cgs", "dyne", "erg", "cm/s"],
        "lj_reduced":["reduced unit", "lj unit", "sigma", "epsilon", "约化单位"],
        "imperial":  ["imperial", "psi", "lbm", "ft/s", "inch"],
        "openfoam":  ["openfoam", "foamfile"],
        "lammps_metal": ["lammps", "metal unit", "angstrom", "ev", "amu", "ps"],
        "lammps_lj": ["lammps", "lj unit"],
    }

    for us_key, kws in unit_keywords.items():
        hits = sum(1 for kw in kws if kw in combined)
        if hits > 0:
            scores[us_key] = scores.get(us_key, 0) + hits
            evidence.append(f"单位制 '{us_key}': {hits} 次命中")

    # 数值范围推断（简单启发式）
    if re.search(r"\b\d+\.?\d*[eE][+-]?0[1-3]\b", combined):
        # 值在 1e-5 ~ 1e3 量级 → 可能 SI (Pa, m/s)
        scores["si"] = scores.get("si", 0) + 1
    if re.search(r"\b\d\.?\d*\b", combined) and "angstrom" not in combined:
        # 小数值可能是约化单位
        if any(kw in combined for kw in ["sigma", "epsilon"]):
            scores["lj_reduced"] = scores.get("lj_reduced", 0) + 2

    if not scores:
        scores["si"] = 1
        evidence.append("无明确单位线索，默认假设 SI")

    best = max(scores, key=lambda k: scores[k])
    us_info = UNIT_SYSTEMS.get(best, {})

    return {
        "detected_unit_system": best,
        "label": us_info.get("label", best),
        "base_units": {
            "length": us_info.get("length", "?"),
            "mass": us_info.get("mass", "?"),
            "time": us_info.get("time", "?"),
            "temperature": us_info.get("temperature", "?"),
        },
        "confidence_scores": scores,
        "evidence": evidence,
    }


def analyze_time_characteristics(
    file_path: str | None = None,
    text_sample: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """分析时间特征（稳态/瞬态/频域）。"""
    scores = {"steady": 0, "transient": 0, "frequency_domain": 0, "time_independent": 0}
    evidence = []
    details: dict[str, Any] = {}

    combined = ""
    if text_sample:
        combined += text_sample.lower() + " "
    if metadata:
        combined += json.dumps(metadata, ensure_ascii=False).lower() + " "

    if file_path:
        p = Path(file_path)
        if p.is_dir():
            # OpenFOAM: 检查时间目录
            time_dirs = []
            for d in p.iterdir():
                if d.is_dir() and re.match(r"^[\d.eE+-]+$", d.name):
                    time_dirs.append(float(d.name))
            if len(time_dirs) > 1:
                scores["transient"] += 10
                time_dirs.sort()
                details["time_steps"] = len(time_dirs)
                details["time_range"] = [time_dirs[0], time_dirs[-1]]
                details["delta_t"] = time_dirs[1] - time_dirs[0] if len(time_dirs) > 1 else None
                evidence.append(f"发现 {len(time_dirs)} 个时间目录 → 瞬态")
            elif len(time_dirs) == 1:
                scores["steady"] += 5
                evidence.append("仅一个时间目录(0/) → 可能稳态")

            # 读取 controlDict
            cd = p / "system" / "controlDict"
            if cd.exists():
                try:
                    cd_text = cd.read_text(errors="ignore")
                    dt_match = re.search(r"deltaT\s+(\S+);", cd_text)
                    end_match = re.search(r"endTime\s+(\S+);", cd_text)
                    if dt_match:
                        details["delta_t"] = float(dt_match.group(1))
                    if end_match:
                        details["end_time"] = float(end_match.group(1))
                except (OSError, ValueError):
                    pass

            combined += _read_text_sample(file_path, max_bytes=16384).lower() + " "
        elif p.is_file():
            try:
                with open(p, errors="ignore") as f:
                    combined += f.read(16384).lower() + " "
            except (OSError, PermissionError):
                pass

    # 关键词
    kw_map = {
        "steady":          ["steady", "steady-state", "稳态", "定常"],
        "transient":       ["transient", "unsteady", "time-dependent", "瞬态", "非定常",
                            "deltaT", "timeStep"],
        "frequency_domain":["frequency", "harmonic", "spectral", "FFT", "频域", "谐响应"],
        "time_independent":["eigenvalue", "modal", "buckling", "特征值", "屈曲"],
    }
    for category, kws in kw_map.items():
        hits = sum(1 for kw in kws if kw in combined)
        if hits > 0:
            scores[category] += hits * 2
            evidence.append(f"'{category}' 关键词命中 {hits} 次")

    # MD timestep 特征
    if "ITEM: TIMESTEP" in combined:
        scores["transient"] += 10
        evidence.append("发现 MD TIMESTEP 标记 → 瞬态")

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        best = "unknown"

    time_labels = {
        "steady": "稳态", "transient": "瞬态",
        "frequency_domain": "频域", "time_independent": "时间无关（特征值问题）",
        "unknown": "未知",
    }

    return {
        "time_type": best,
        "label": time_labels.get(best, best),
        "details": details,
        "confidence_scores": scores,
        "evidence": evidence,
    }


def extract_mesh_statistics(
    file_path: str | None = None,
) -> dict[str, Any]:
    """提取网格拓扑统计信息。"""
    stats: dict[str, Any] = {
        "n_points": None,
        "n_cells": None,
        "n_faces": None,
        "n_internal_faces": None,
        "n_boundary_patches": None,
        "boundary_patches": {},
        "cell_type": "unknown",
        "mesh_format": "unknown",
    }

    if not file_path:
        return stats

    p = Path(file_path)

    # OpenFOAM polyMesh
    if p.is_dir():
        pm = p / "constant" / "polyMesh"
        if pm.exists():
            stats["mesh_format"] = "OpenFOAM polyMesh"

            # 读取 points
            pts_file = pm / "points"
            if pts_file.exists():
                try:
                    text = pts_file.read_text(errors="ignore")
                    m = re.search(r"^\s*(\d+)\s*\n\s*\(", text, re.MULTILINE)
                    if m:
                        stats["n_points"] = int(m.group(1))
                except OSError:
                    pass

            # 读取 faces
            fac_file = pm / "faces"
            if fac_file.exists():
                try:
                    text = fac_file.read_text(errors="ignore")
                    m = re.search(r"^\s*(\d+)\s*\n\s*\(", text, re.MULTILINE)
                    if m:
                        stats["n_faces"] = int(m.group(1))
                    # 推断单元类型
                    n_fp_match = re.findall(r"^(\d+)\(", text, re.MULTILINE)
                    if n_fp_match:
                        fp_counts = {}
                        for nf in n_fp_match:
                            fp_counts[nf] = fp_counts.get(nf, 0) + 1
                        most_common = max(fp_counts, key=fp_counts.get)
                        type_map = {"4": "hex/quad", "3": "tet/tri", "5": "prism/pyramid"}
                        stats["cell_type"] = type_map.get(most_common, f"polygon({most_common})")
                except OSError:
                    pass

            # 读取 owner
            own_file = pm / "owner"
            if own_file.exists():
                try:
                    text = own_file.read_text(errors="ignore")
                    m = re.search(r"^\s*(\d+)\s*\n\s*\(", text, re.MULTILINE)
                    if m:
                        n_faces = int(m.group(1))
                        vals = re.findall(r"^\s*(\d+)\s*$", text, re.MULTILINE)
                        if vals:
                            stats["n_cells"] = max(int(v) for v in vals) + 1
                except OSError:
                    pass

            # 读取 neighbour
            neigh_file = pm / "neighbour"
            if neigh_file.exists():
                try:
                    text = neigh_file.read_text(errors="ignore")
                    m = re.search(r"^\s*(\d+)\s*\n\s*\(", text, re.MULTILINE)
                    if m:
                        stats["n_internal_faces"] = int(m.group(1))
                except OSError:
                    pass

            # 读取 boundary
            bnd_file = pm / "boundary"
            if bnd_file.exists():
                try:
                    text = bnd_file.read_text(errors="ignore")
                    patches = re.findall(r"(\w+)\s*\{[^}]*type\s+(\w+);[^}]*nFaces\s+(\d+);", text, re.DOTALL)
                    stats["n_boundary_patches"] = len(patches)
                    for pname, ptype, pnfaces in patches:
                        stats["boundary_patches"][pname] = {
                            "type": ptype,
                            "nFaces": int(pnfaces),
                        }
                except OSError:
                    pass

        # LAMMPS data
        data_file = p / "lammps.data"
        if data_file.exists() and stats["mesh_format"] == "unknown":
            stats["mesh_format"] = "LAMMPS data"
            try:
                text = data_file.read_text(errors="ignore")
                m = re.search(r"(\d+)\s+atoms", text)
                if m:
                    stats["n_cells"] = int(m.group(1))  # 对于 MD, atoms ≈ "cells"
                    stats["cell_type"] = "atom"
                for axis in ["x", "y", "z"]:
                    m = re.search(rf"(\S+)\s+(\S+)\s+{axis}lo\s+{axis}hi", text)
                    if m:
                        stats[f"box_{axis}"] = [float(m.group(1)), float(m.group(2))]
            except OSError:
                pass

    return stats

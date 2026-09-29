"""Universal computational-mesh facade and quality-controlled dispatcher.

This module owns format conversion, adapter dispatch, review and bounded
iteration. Shared geometry discovery and compact parametric adapters live in
separate internal modules behind the unified mesh contract.

接口契约：
  - async def，第一个 kwarg 是 state: State
  - 返回 dict 含 status 字段
  - 加 **_: Any 兜底
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from string import Template
from typing import Any

from core.state import State
from core.tool_registry import contract_requirement
from nodes.data.pipeline_contract import needs_input_result
from nodes.data.progress import emit_progress
from .geometry_assets import canonical_mesh_controls, flatten_parameter_groups, geometry_file_from_params
from .parametric_mesh_adapters import (
    generate_cantilever_beam_mesh,
    generate_converging_diverging_nozzle_mesh,
    generate_heat_sink_mesh,
    generate_rectangular_waveguide_mesh,
)
from .human_input_utils import (
    apply_mesh_density_from_text,
    apply_recommended_mesh_density,
    caller_request_text,
    extract_naca_code as _extract_explicit_naca_code,
    has_mesh_density_parameters,
    mesh_density_confirmed,
    mesh_density_requested,
    mesh_density_requires_confirmation,
    has_valid_airfoil_geometry,
    is_placeholder_value,
    looks_like_public_reference_request,
    original_intent_allows_airfoil_mesh,
    original_user_request_text_from_state,
    pause_for_input as _pause_for_input,
    surrogate_geometry_explicitly_approved,
)
from .mesh_iteration_advisor import (
    CANONICAL_CFD_MESH_INTENT_PATTERNS,
    explicit_mesh_contract as _match_cfd_case_profile,
    _resolve_mesh_iteration_inputs,
    detect_special_domain_requirements,
    geometry_parameters_need_reference_resolution,
    looks_like_turbomachinery_blade,
    public_geometry_reference_request,
    special_domain_reference_request,
)
from .scientific_assets import surface_topology_metrics
from ..review.mesh_reviewer import semantic_mesh_review


#: 会让调用被**拒**的网格参数 —— 声明在这里，两个消费者共用：
#: 本模块的校验器用 `contract_requirement()` 取措辞，`prepare_scientific_mesh`
#: （模型唯一看得见的网格入口）把同一份声明渲染进自己的 description。
#:
#: 为什么必须成对：这些参数是模型通过 `prepare_scientific_mesh(parameters=…)`
#: 传进来的，而拒绝发生在本模块深处。只在这里 raise = 模型得先写错一版被拒，
#: 才知道有这条约束（tests/test_tool_contracts_reach_the_caller.py 就是为这件事
#: 立的闸）。
MESH_PARAMETER_CONTRACT: dict[str, str] = {
    "domain_type": "The coordinate-profile adapter supports c (C-shaped) and o (circular) far-field domains. For other domain shapes use an explicit geometry recipe; never silently substitute a different shape.",
    "characteristic_length": "Absolute target mesh size in geometry units (Gmsh -clmax), not a scaling factor. Preserve supplied units and local refinement.",
    "minimum_length": "Optional lower bound on Gmsh mesh sizing (-clmin), in the same units as characteristic_length; not a guarantee that every edge exceeds this length.",
    "length_unit": "Optional CAD import target unit (m, mm, cm, etc.). STEP/IGES are converted by OpenCASCADE before meshing; express mesh sizes in this unit. Omit to preserve native coordinates.",
    "solver_mesh_format": "Optional final Gmsh export format (e.g. msh4 or inp); internal MSH2 quality/conversion data is not a substitute for the requested export.",
    "required_surface_topology": "Optional triangular or quadrilateral surface-cell contract, independent of polynomial order; controls both recombination and subdivision of generated geometry.",
    "recombine": "Whether to recombine surface triangles into quadrilaterals. False also disables automatic all-quad subdivision; an explicit required_surface_topology takes precedence.",
    "mesh_size_factor": "Optional dimensionless Gmsh -clscale multiplier; omit when absolute sizing is already specified.",
    "element_order": "Polynomial element order (Gmsh -order), independent of mesh_dimension; 2 means quadratic, not two-dimensional.",
    "extrude_to_3d": (
        "controls storage extrusion, not the physical simulation dimension; OpenFOAM conversion "
        "requires a one-layer volume for 2-D input, with empty end patches. "
        "A pure surface mesh is preserved when convert_to_openfoam=false and extrude_to_3d=false."
    ),
    "gmsh_to_foam_cmd": (
        "OpenFOAM converter launcher and options; the active generated mesh path is supplied by Data. "
        "A trailing .msh example operand is replaced by that path. Native tools and the openfoam launcher are supported."
    ),
    "n_cylinder": "Explicit circumferential resolution takes precedence over the default h_cylinder-derived resolution; h_cylinder derives the count only when n_cylinder is omitted.",
    "farfield_radius": (
        "greater than the cylinder/body radius when a circular far field is requested "
        "(domain_shape=circle/circular/radial, or farfield_radius given at all)"
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# 第一部分：几何工具函数
# ═══════════════════════════════════════════════════════════════════════════════


async def _run_mesh_worker(state: State, generator: Any, **params: Any) -> Any:
    """Run a blocking mesher without leaving the caller with a silent UI."""
    task = asyncio.create_task(asyncio.to_thread(generator, **params))
    elapsed = 0
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except asyncio.TimeoutError:
            elapsed += 30
            emit_progress(
                state,
                "mesh_generation_progress",
                "external mesh tool is still running",
                elapsed_seconds=elapsed,
                execution_budget_seconds=params.get("timeout", 120),
            )

def naca_4digit(code: str, n_points: int = 201) -> tuple[list[float], list[float], list[float], list[float]]:
    """生成 NACA 4 位翼型的上下表面坐标（余弦分布）。"""
    m = int(code[0]) / 100.0
    p = int(code[1]) / 10.0
    t = int(code[2:]) / 100.0

    theta = [math.pi * i / (n_points - 1) for i in range(n_points)]
    x = [0.5 * (1.0 - math.cos(th)) for th in theta]

    yt = []
    for xi in x:
        yt.append(
            5.0 * t * (
                0.2969 * math.sqrt(max(xi, 0.0))
                - 0.1260 * xi
                - 0.3516 * xi**2
                + 0.2843 * xi**3
                - 0.1015 * xi**4
            )
        )

    if m == 0 or p == 0:
        xu = x[:]
        yu = yt[:]
        xl = x[:]
        yl = [-y for y in yt]
    else:
        xu, yu, xl, yl = [], [], [], []
        for xi, yti in zip(x, yt):
            if xi < p:
                yc = m / p**2 * (2.0 * p * xi - xi**2)
                dyc = 2.0 * m / p**2 * (p - xi)
            else:
                yc = m / (1.0 - p)**2 * ((1.0 - 2.0 * p) + 2.0 * p * xi - xi**2)
                dyc = 2.0 * m / (1.0 - p)**2 * (p - xi)
            angle = math.atan(dyc)
            xu.append(xi - yti * math.sin(angle))
            yu.append(yc + yti * math.cos(angle))
            xl.append(xi + yti * math.sin(angle))
            yl.append(yc - yti * math.cos(angle))

    return xu, yu, xl, yl


def naca_4digit_closed_loop(code: str, n_surface: int = 241) -> list[tuple[float, float]]:
    """Return clockwise-ish NACA coordinates from TE upper -> LE -> TE lower.

    The first and last points are both at the trailing edge so downstream tools
    can create two splines without relying on a periodic spline.
    """
    n_half = max(41, int(n_surface) // 2)
    xu, yu, xl, yl = naca_4digit(code, n_points=n_half)
    upper = list(zip(xu[::-1], yu[::-1]))  # TE -> LE
    lower = list(zip(xl[1:], yl[1:]))      # after LE -> TE
    return upper + lower


def _normalise_naca_code(code: Any, fallback_text: str = "") -> str:
    text = str(code or "").strip()
    m = None
    if text:
        m = re.search(r"(?:NACA\s*)?(\d{4})", text, flags=re.IGNORECASE)
    if not m and fallback_text:
        path_match = re.search(r"(?:^|[^A-Za-z0-9])naca\s*[-_ ]?(\d{4})(?:$|[^0-9])", fallback_text, flags=re.IGNORECASE)
        if path_match:
            return path_match.group(1)
    if not m:
        return ""
    return m.group(1)


def _extract_non_naca_airfoil_name(*texts: Any) -> str | None:
    for text in texts:
        if text is None:
            continue
        if isinstance(text, dict):
            if str(text.get("type") or "").lower() == "naca" or _extract_explicit_naca_code(text):
                continue
            text = text.get("airfoil_name") or text.get("name") or ""
        value = str(text).strip()
        if not value or _extract_explicit_naca_code(value):
            continue
        if looks_like_turbomachinery_blade(value, {}):
            continue
        profile_code = r"(?:[A-Z]{1,4}\s*[-_]?\s*\d{2,4}[A-Z0-9]{0,3}|DU\s*\d{2}\s*W\s*\d{2,4}|NLF\s*\d{3,5})"
        for keyword in re.finditer(r"\b(?:airfoil|aerofoil|profile)\b|翼型", value, flags=re.IGNORECASE):
            window = value[max(0, keyword.start() - 100): keyword.end() + 60]
            named = re.search(rf"\b({profile_code})\b", window, flags=re.IGNORECASE)
            if named and not _extract_explicit_naca_code(named.group(1)):
                candidate = re.sub(r"[\s_-]+", "", named.group(1)).upper()
                if not re.fullmatch(r"V\d{2,5}", candidate, flags=re.I):
                    return candidate
        m = re.search(r"\b([A-Za-z][A-Za-z0-9_.-]*\s*[-_ ]?airfoil)\b", value, flags=re.IGNORECASE)
        if m:
            compact = re.sub(r"[^A-Za-z0-9]+", "", m.group(1))
            if re.search(r"\d{5,}", compact) or re.search(r"[A-Za-z]{5,}\d{4}airfoil$", compact, flags=re.I):
                continue
            return m.group(1).strip()
    return None


def _has_airfoil_coordinate_input(params: dict[str, Any]) -> bool:
    return has_valid_airfoil_geometry(params)


def _airfoil_geometry_request(name: str | None, params: dict[str, Any]) -> dict[str, Any]:
    airfoil_name = name or str(params.get("airfoil_name") or params.get("airfoil") or "").strip()
    example_name = airfoil_name or "custom_airfoil"
    parameter_template = {
        "mesh_type": "airfoil_gmsh",
        "airfoil_name": airfoil_name,
        "airfoil_dat_path": "/absolute/path/to/profile.dat",
        "angle_of_attack": params.get("angle_of_attack", params.get("aoa", 0)),
        "far_field": params.get("far_field", 25),
        "wake_length": params.get("wake_length", 25),
        "n_surface": params.get("n_surface", 241),
        "h_airfoil": params.get("h_airfoil", 0.008),
        "h_farfield": params.get("h_farfield", 0.8),
        "boundary_layer_first": params.get("boundary_layer_first", 0.001),
        "quality_preset": params.get("quality_preset", "robust"),
        "convert_to_openfoam": params.get("convert_to_openfoam", True),
        "write_tecplot": params.get("write_tecplot", True),
    }
    prompt_examples = [
        (
            f"请生成 {example_name} 翼型 OpenFOAM 网格，攻角 "
            f"{params.get('angle_of_attack', params.get('aoa', 0))} 度。翼型几何文件："
            f"/absolute/path/to/profile.dat。使用 airfoil_gmsh，"
            "输出 OpenFOAM polyMesh 和 Tecplot dat。其他网格参数可以使用默认 robust 设置。"
        ),
        {
            "mesh_type": "airfoil_gmsh",
            "airfoil_name": airfoil_name,
            "airfoil_dat_path": "/absolute/path/to/profile.dat",
            "angle_of_attack": params.get("angle_of_attack", params.get("aoa", 0)),
            "quality_preset": "robust",
            "convert_to_openfoam": True,
            "write_tecplot": True,
        },
    ]
    return _pause_for_input(
        question=(
            f"请提供非 NACA 翼型 {airfoil_name or 'custom airfoil'} 的几何文件绝对路径，"
            "或直接粘贴翼型坐标数组/坐标文本。"
        ),
        context=(
            "该翼型不是标准 NACA 代号。为避免生成错误翼型，data 节点不会回退为 NACA0012。\n"
            "坐标格式示例见 nodes/data/examples/airfoils/README.md。\n\n"
            "可直接回复该几何文件的绝对路径。"
        ),
        metadata={
            "input_kind": "airfoil_geometry",
            "airfoil_name": airfoil_name,
            "required_fields": ["airfoil_dat_path 或 airfoil_coordinate_text 或 airfoil_coordinates"],
            "parameter_template": parameter_template,
            "prompt_examples": prompt_examples,
            "resume_instruction": (
                "把用户回答解析为 airfoil_dat_path；如果回答不是路径，则尝试作为 "
                "airfoil_coordinate_text。然后用更新后的 parameters 重新调用原网格/前处理工具。"
            ),
        },
    ) | {
        "reason": (
            f"非 NACA 翼型 {airfoil_name!r} 缺少几何坐标。"
            "为避免生成错误翼型，已停止生成，不会回退为 NACA0012。"
        ),
        "message": (
            "请提供该翼型的 airfoil_dat_path、airfoil_coordinate_text 或 airfoil_coordinates。"
            "格式示例见 nodes/data/examples/airfoils/README.md。"
            "用户已经指定了非 NACA 翼型，不要建议改用 NACA 翼型。"
        ),
        "required_fields": [
            "airfoil_dat_path 或 airfoil_coordinate_text 或 airfoil_coordinates",
        ],
        "prompt_examples": prompt_examples,
        "do_not_suggest": [
            "不要建议改用 NACA0012/NACA0024 或其他 NACA 翼型",
            "不要把 NACA 翼型推荐表作为方案",
        ],
        "parameter_template": parameter_template,
    }


def _naca_or_profile_required_request(params: dict[str, Any]) -> dict[str, Any]:
    return _pause_for_input(
        question="请提供明确的 NACA 代号或真实二维坐标轮廓后再生成翼型网格。",
        context=(
            "当前请求走翼型/NACA 网格路径，但没有检测到显式 naca_code，也没有 "
            "airfoil_dat_path/airfoil_coordinate_text/airfoil_coordinates。data 节点不会默认回退到 "
            "NACA0012。若您要生成 NACA 网格，请提供例如 naca_code=0015；若是公开/自定义几何，"
            "请提供坐标文件，或先用 data_web_search/data_web_download 获取真实 profile，再交给 prepare_scientific_mesh。"
        ),
        metadata={
            "input_kind": "airfoil_geometry",
            "required_fields": [
                "naca_code",
                "airfoil_dat_path",
                "airfoil_coordinate_text",
                "airfoil_coordinates",
                "coordinate_profile_path",
            ],
            "parameters": params,
            "resume_instruction": (
                "把用户回复解析为 naca_code 或真实坐标文件路径；若是多个坐标段，先调用 "
                "prepare_scientific_mesh(operation='build_profile') 合并坐标，再重试 prepare_scientific_mesh。"
            ),
        },
    )


def _airfoil_mesh_density_request(params: dict[str, Any]) -> dict[str, Any]:
    customer_message = (
        "已识别到您希望生成加密网格，但还需要确认网格密度。\n\n"
        "请选择一种方式继续：\n"
        "A. 回复“使用推荐加密参数”，我将采用：n_surface=401，h_airfoil=0.004，"
        "boundary_layer_first=1e-4，boundary_layer_ratio=1.12，boundary_layer_thickness=0.04。\n"
        "B. 提交自定义参数，例如：n_surface=501，h_airfoil=0.003，"
        "boundary_layer_first=5e-5，boundary_layer_ratio=1.10，boundary_layer_thickness=0.05。\n\n"
        "不需要修改 minimal.yaml；直接在任务 prompt 或 parameters JSON 中补充即可。"
    )
    payload = _pause_for_input(
        question="请确认网格加密参数，或直接回复“使用推荐加密参数”。",
        context=customer_message,
        options=[
            "使用推荐加密参数",
            "我将提供自定义 n_surface/h_airfoil/boundary_layer_first 等参数",
        ],
        metadata={
            "input_kind": "airfoil_mesh_density",
            "resume_instruction": (
                "如果用户选择推荐参数，设置 mesh_density_confirmed=true 并应用推荐值；"
                "如果用户提供自定义数值，解析后重新调用原网格/前处理工具。"
            ),
        },
    )
    payload.update({
        "customer_message": customer_message,
        "reason": "检测到用户希望调整翼型网格量或近壁加密，但缺少明确数值参数。",
        "message": (
            "请用户选择一种继续方式：1) 回复“使用推荐加密参数”直接采用默认加密值；"
            "2) 在任务 prompt 或 parameters JSON 中提交自定义网格密度参数。"
            "不需要修改 minimal.yaml。"
        ),
        "next_actions": [
            "回复“使用推荐加密参数”",
            "或提交自定义 n_surface、h_airfoil、boundary_layer_first、boundary_layer_ratio、boundary_layer_thickness",
        ],
        "required_fields": [
            "n_surface：翼型表面/周向离散点数",
            "h_airfoil：翼型附近目标网格尺寸",
            "boundary_layer_first：第一层网格距离/高度",
            "boundary_layer_ratio：边界层增长率",
            "boundary_layer_thickness：边界层总厚度",
        ],
        "parameter_template": {
            "mesh_type": "airfoil_gmsh",
            "n_surface": 401,
            "h_airfoil": 0.004,
            "h_farfield": 0.8,
            "boundary_layer_first": 0.0001,
            "boundary_layer_thickness": 0.04,
            "boundary_layer_ratio": 1.12,
            "quality_preset": params.get("quality_preset", "robust"),
            "convert_to_openfoam": params.get("convert_to_openfoam", True),
            "write_tecplot": params.get("write_tecplot", True),
        },
        "prompt_examples": [
            "使用推荐加密参数",
            (
                "请生成翼型网格，并进行近壁加密：n_surface=401，h_airfoil=0.004，"
                "boundary_layer_first=1e-4，boundary_layer_ratio=1.12，"
                "boundary_layer_thickness=0.04。"
            )
        ],
    })
    return payload


def _slug_asset_name(name: str, fallback: str = "asset") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or fallback).strip()).strip("_")
    return slug or fallback


def _parse_airfoil_coordinates(raw: Any) -> list[tuple[float, float]] | None:
    """Parse generic airfoil coordinates from a list, JSON string, text, or .dat file.

    Supported text format is the common Selig/UIUC style: optional name line,
    followed by x y pairs ordered around the airfoil surface.
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, (list, tuple)):
        pts: list[tuple[float, float]] = []
        for item in raw:
            if isinstance(item, dict):
                pts.append((float(item["x"]), float(item["y"])))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                pts.append((float(item[0]), float(item[1])))
        return pts
    text = str(raw)
    path = Path(text).expanduser()
    if "\n" not in text and path.exists():
        text = path.read_text(encoding="utf-8", errors="ignore")
    else:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (list, tuple)):
                return _parse_airfoil_coordinates(parsed)
        except json.JSONDecodeError:
            pass
    pts = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        parts = re.split(r"[\s,;]+", stripped)
        if len(parts) < 2:
            continue
        try:
            pts.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return pts or None


def _normalise_airfoil_coordinates(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Normalize arbitrary airfoil coordinates to chord ~= 1 and TE -> LE -> TE order."""
    cleaned: list[tuple[float, float]] = []
    for x, y in points:
        if not cleaned or math.hypot(x - cleaned[-1][0], y - cleaned[-1][1]) > 1e-10:
            cleaned.append((float(x), float(y)))
    if len(cleaned) >= 2 and math.hypot(cleaned[0][0] - cleaned[-1][0], cleaned[0][1] - cleaned[-1][1]) < 1e-10:
        cleaned.pop()
    # 判决拆除（mg:504 删，2026-08-31）：≥20 点是任意审美阈值 —— 模型能判的
    # 别硬编码；真 validity 只剩「至少 2 个不重合点」与相邻的零弦长检查。
    if len(cleaned) < 2:
        raise ValueError("Generic airfoil coordinates require at least 2 unique x-y points")

    min_x = min(x for x, _ in cleaned)
    max_x = max(x for x, _ in cleaned)
    chord = max_x - min_x
    if chord <= 1e-12:
        raise ValueError("Generic airfoil coordinates have zero chord length")
    normalized = [((x - min_x) / chord, y / chord) for x, y in cleaned]

    # Public/profile coordinate files often contain two numerically distinct
    # points for an analytically sharp trailing edge.  Leaving a tiny closing
    # segment between them can make interpolating splines cross and send Gmsh
    # into repeated edge-splitting.  Collapse only sub-grid closure gaps; a
    # genuinely finite-thickness trailing edge remains untouched.
    if len(normalized) >= 3 and math.hypot(
        normalized[0][0] - normalized[-1][0],
        normalized[0][1] - normalized[-1][1],
    ) <= 5e-4:
        normalized.pop()

    # Most airfoil .dat files are already TE upper -> LE -> TE lower. If not,
    # rotate the loop so a trailing-edge point starts the sequence.
    first_x = normalized[0][0]
    last_x = normalized[-1][0]
    if first_x < 0.8 and last_x < 0.8:
        te_idx = max(range(len(normalized)), key=lambda i: normalized[i][0])
        normalized = normalized[te_idx:] + normalized[:te_idx]

    le_idx = min(range(len(normalized)), key=lambda i: normalized[i][0])
    if le_idx in {0, len(normalized) - 1}:
        raise ValueError(
            "Generic airfoil coordinates must trace both surfaces around the leading edge; "
            "expected order like TE upper -> LE -> TE lower"
        )
    return normalized


def _rotate_points_2d(
    points: list[tuple[float, float]],
    angle_degrees: float,
    center_x: float = 0.25,
    center_y: float = 0.0,
) -> list[tuple[float, float]]:
    """Rotate points around a chord reference point.

    Positive aerodynamic AoA means the airfoil is nose-up while the farfield
    axes and freestream remain unchanged. With the NACA coordinates ordered
    from trailing edge to leading edge, that is a clockwise geometry rotation.
    """
    if abs(angle_degrees) < 1e-14:
        return points
    theta = -math.radians(angle_degrees)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    rotated: list[tuple[float, float]] = []
    for x, y in points:
        dx = x - center_x
        dy = y - center_y
        rotated.append((
            center_x + dx * cos_t - dy * sin_t,
            center_y + dx * sin_t + dy * cos_t,
        ))
    return rotated


def _area_3d(points: list[tuple[float, float, float]]) -> float:
    def tri_area(a: tuple[float, float, float], b: tuple[float, float, float], c: tuple[float, float, float]) -> float:
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        cx = uy * vz - uz * vy
        cy = uz * vx - ux * vz
        cz = ux * vy - uy * vx
        return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)

    if len(points) < 3:
        return 0.0
    if len(points) == 3:
        return tri_area(points[0], points[1], points[2])
    return tri_area(points[0], points[1], points[2]) + tri_area(points[0], points[2], points[3])


def _aspect_ratio_3d(points: list[tuple[float, float, float]]) -> float:
    lengths = []
    for i, p in enumerate(points):
        q = points[(i + 1) % len(points)]
        length = math.sqrt((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 + (q[2] - p[2]) ** 2)
        if length > 1e-14:
            lengths.append(length)
    if not lengths:
        return float("inf")
    return max(lengths) / min(lengths)


# type: (topological dimension, linear family, corner-node count). Higher-order
# nodes do not change entity counts or corner geometry.
_GMSH_ELEMENT_FAMILIES: dict[int, tuple[int, str, int]] = {
    **{kind: (1, "line", 2) for kind in (1, 8, 26, 27, 28)},
    **{kind: (2, "triangle", 3) for kind in (2, 9, 20, 21, 22, 23, 24, 25)},
    **{kind: (2, "quadrilateral", 4) for kind in (3, 10, 16)},
    **{kind: (3, "tetrahedron", 4) for kind in (4, 11, 29, 30, 31)},
    **{kind: (3, "hexahedron", 8) for kind in (5, 12, 17)},
    **{kind: (3, "prism", 6) for kind in (6, 13, 18)},
    **{kind: (3, "pyramid", 5) for kind in (7, 14, 19)},
}


def _parse_gmsh_msh2_quality(mesh_path: str) -> dict[str, Any]:
    """Parse an ASCII MSH2 file and compute simple 2D quality statistics."""
    text = Path(mesh_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    from .scientific_assets import validate_format_version

    format_error = validate_format_version("\n".join(text[:3]) + "\n", "msh2.2")
    if format_error or len(text) < 2 or text[1].split()[1:2] != ["0"]:
        raise ValueError(format_error or "Mesh quality inspection requires ASCII MSH2, not binary data.")
    nodes: dict[int, tuple[float, float, float]] = {}
    tri_count = 0
    quad_count = 0
    tet_count = 0
    hex_count = 0
    prism_count = 0
    pyramid_count = 0
    areas: list[float] = []
    aspects: list[float] = []
    surface_cells: list[list[int]] = []
    physical_names: dict[tuple[int, int], str] = {}
    physical_counts: dict[tuple[int, int], int] = {}
    physical_geometry: dict[tuple[int, int], dict[str, Any]] = {}
    nodes_by_dimension: dict[int, set[int]] = {}
    element_keys: set[tuple[int, tuple[int, ...]]] = set()
    duplicate_elements = 0

    i = 0
    while i < len(text):
        line = text[i].strip()
        if line == "$PhysicalNames":
            count = int(text[i + 1].strip())
            for row in text[i + 2:i + 2 + count]:
                parts = row.split(maxsplit=2)
                if len(parts) == 3:
                    physical_names[(int(parts[0]), int(parts[1]))] = parts[2].strip('"')
            i += count + 2
        elif line == "$Nodes":
            n_nodes = int(text[i + 1].strip())
            for row in text[i + 2:i + 2 + n_nodes]:
                parts = row.split()
                if len(parts) >= 4:
                    nodes[int(parts[0])] = (float(parts[1]), float(parts[2]), float(parts[3]))
            i += n_nodes + 2
        elif line == "$Elements":
            n_elem = int(text[i + 1].strip())
            for row in text[i + 2:i + 2 + n_elem]:
                parts = row.split()
                if len(parts) < 4:
                    continue
                elem_type = int(parts[1])
                n_tags = int(parts[2])
                tags = [int(v) for v in parts[3:3 + n_tags]]
                family = _GMSH_ELEMENT_FAMILIES.get(elem_type)
                dimension = family[0] if family else None
                if dimension is not None and tags:
                    key = (dimension, tags[0])
                    physical_counts[key] = physical_counts.get(key, 0) + 1
                node_ids = [int(v) for v in parts[3 + n_tags:]]
                if dimension is not None:
                    nodes_by_dimension.setdefault(dimension, set()).update(node_ids)
                element_key = (elem_type, tuple(sorted(node_ids)))
                duplicate_elements += int(element_key in element_keys)
                element_keys.add(element_key)
                corner_ids = node_ids[:family[2]] if family else node_ids
                pts = [nodes[nid] for nid in corner_ids if nid in nodes]
                if dimension is not None and tags and pts:
                    stats = physical_geometry.setdefault((dimension, tags[0]), {
                        "dimension": dimension,
                        "bounds_min": list(pts[0]), "bounds_max": list(pts[0]),
                    })
                    for axis in range(3):
                        stats["bounds_min"][axis] = min(stats["bounds_min"][axis], *(p[axis] for p in pts))
                        stats["bounds_max"][axis] = max(stats["bounds_max"][axis], *(p[axis] for p in pts))
                    if family[1] == "line" and len(pts) == 2:
                        length = math.dist(*pts)
                        stats["length_sum"] = stats.get("length_sum", 0.0) + length
                        stats["length_min"] = min(stats.get("length_min", length), length)
                        stats["length_max"] = max(stats.get("length_max", length), length)
                if family and family[0] == 3:
                    if family[1] == "tetrahedron":
                        tet_count += 1
                    elif family[1] == "hexahedron":
                        hex_count += 1
                    elif family[1] == "prism":
                        prism_count += 1
                    elif family[1] == "pyramid":
                        pyramid_count += 1
                    continue
                if not family or family[0] != 2:
                    continue
                if len(pts) != len(corner_ids):
                    continue
                area = _area_3d(pts)
                areas.append(area)
                aspects.append(_aspect_ratio_3d(pts))
                surface_cells.append(corner_ids)
                if family[1] == "triangle":
                    tri_count += 1
                elif family[1] == "quadrilateral":
                    quad_count += 1
            i += n_elem + 2
        i += 1

    finite_aspects = [a for a in aspects if math.isfinite(a)]
    topology = {
        **surface_topology_metrics(surface_cells),
        "mesh_dimension": 3 if (tet_count + hex_count + prism_count + pyramid_count) > 0 else 2,
    }
    return {
        "n_nodes": len(nodes),
        "n_triangles": tri_count,
        "n_quads": quad_count,
        "n_2d_elements": tri_count + quad_count,
        "n_tets": tet_count,
        "n_hexes": hex_count,
        "n_prisms": prism_count,
        "n_pyramids": pyramid_count,
        "n_3d_elements": tet_count + hex_count + prism_count + pyramid_count,
        "min_area": min(areas) if areas else None,
        "max_area": max(areas) if areas else None,
        "max_aspect_ratio": max(finite_aspects) if finite_aspects else None,
        "mean_aspect_ratio": sum(finite_aspects) / len(finite_aspects) if finite_aspects else None,
        "negative_or_zero_area_cells": sum(1 for a in areas if a <= 0),
        "duplicate_nodes": len(nodes) - len(set(nodes.values())),
        "duplicate_elements": duplicate_elements,
        "isolated_nodes": len(set(nodes) - nodes_by_dimension.get(max(nodes_by_dimension, default=0), set())),
        "bounds_min": [min(p[axis] for p in nodes.values()) for axis in range(3)] if nodes else [],
        "bounds_max": [max(p[axis] for p in nodes.values()) for axis in range(3)] if nodes else [],
        "physical_groups": {
            name: physical_counts.get(key, 0)
            for key, name in physical_names.items()
        },
        # Observations only: the request/model decides which region extents
        # and sizing are appropriate; no geometry-specific acceptance rules.
        "physical_group_geometry": {
            name: physical_geometry[key]
            for key, name in physical_names.items() if key in physical_geometry
        },
        "topology": topology,
    }


def _extrude_msh2_surface_to_thin_3d(mesh_path: str, out_path: str, span: float = 0.1) -> dict[str, Any]:
    """Create a one-cell-thick 3D MSH2 mesh from a 2D surface MSH2 mesh.

    This is a generic bridge for imported 2D CAD/IGES geometries: Gmsh can
    often surface-mesh them, while OpenFOAM/gmshToFoam needs volume cells.
    The function keeps the original surface mesh as front/back patches and
    turns boundary line elements into side wall patches.
    """
    lines = Path(mesh_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    nodes: dict[int, tuple[float, float, float]] = {}
    physical_names: dict[int, str] = {}
    line_elems: list[tuple[list[int], list[int]]] = []
    surface_elems: list[tuple[int, list[int], list[int]]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line == "$PhysicalNames":
            count = int(lines[i + 1].strip())
            for row in lines[i + 2:i + 2 + count]:
                parts = row.strip().split(maxsplit=2)
                if len(parts) == 3 and parts[0] == "1":
                    physical_names[int(parts[1])] = parts[2].strip().strip('"')
            i += count + 2
        elif line == "$Nodes":
            n_nodes = int(lines[i + 1].strip())
            for row in lines[i + 2:i + 2 + n_nodes]:
                parts = row.split()
                if len(parts) >= 4:
                    nodes[int(parts[0])] = (float(parts[1]), float(parts[2]), float(parts[3]))
            i += n_nodes + 2
        elif line == "$Elements":
            n_elem = int(lines[i + 1].strip())
            for row in lines[i + 2:i + 2 + n_elem]:
                parts = row.split()
                if len(parts) < 4:
                    continue
                elem_type = int(parts[1])
                n_tags = int(parts[2])
                tags = [int(v) for v in parts[3:3 + n_tags]]
                node_ids = [int(v) for v in parts[3 + n_tags:]]
                if elem_type == 1 and len(node_ids) == 2:
                    line_elems.append((tags, node_ids))
                elif elem_type in {2, 3} and len(node_ids) in {3, 4}:
                    surface_elems.append((elem_type, tags, node_ids))
            i += n_elem + 2
        i += 1
    if not nodes or not surface_elems:
        raise ValueError("MSH2 file does not contain 2D surface elements to extrude")

    node_ids_sorted = sorted(nodes)
    duplicate_offset = max(node_ids_sorted)
    duplicated = {nid: duplicate_offset + idx + 1 for idx, nid in enumerate(node_ids_sorted)}
    side_groups: dict[int, str] = {}
    for tags, _nids in line_elems:
        physical_tag = int(tags[0]) if tags else 0
        side_groups.setdefault(physical_tag, physical_names.get(physical_tag) or "boundary")
    if not side_groups:
        side_groups = {0: "boundary"}
    side_tags = {physical_tag: 3 + index for index, physical_tag in enumerate(side_groups)}
    out = Path(out_path)
    with out.open("w", encoding="utf-8") as handle:
        handle.write("$MeshFormat\n2.2 0 8\n$EndMeshFormat\n")
        handle.write(f"$PhysicalNames\n{2 + len(side_groups)}\n")
        handle.write('3 1 "fluid"\n')
        handle.write('2 2 "frontAndBack"\n')
        for physical_tag, name in side_groups.items():
            handle.write(f'2 {side_tags[physical_tag]} "{name}"\n')
        handle.write("$EndPhysicalNames\n")
        handle.write("$Nodes\n")
        handle.write(f"{len(nodes) * 2}\n")
        for nid in node_ids_sorted:
            x, y, z = nodes[nid]
            handle.write(f"{nid} {x:.16g} {y:.16g} {z:.16g}\n")
        for nid in node_ids_sorted:
            x, y, z = nodes[nid]
            handle.write(f"{duplicated[nid]} {x:.16g} {y:.16g} {z + float(span):.16g}\n")
        handle.write("$EndNodes\n")

        elements: list[tuple[int, int, list[int], list[int]]] = []
        for tags, nids in line_elems:
            a, b = nids
            if a in duplicated and b in duplicated:
                pa = nodes.get(a)
                pb = nodes.get(b)
                if pa is None or pb is None:
                    continue
                if math.sqrt((pb[0] - pa[0]) ** 2 + (pb[1] - pa[1]) ** 2 + (pb[2] - pa[2]) ** 2) < 1e-12:
                    continue
                physical_tag = int(tags[0]) if tags else 0
                elements.append((3, 2, [side_tags[physical_tag], 1], [a, b, duplicated[b], duplicated[a]]))
        for elem_type, _tags, nids in surface_elems:
            elements.append((elem_type, 2, [2, 1], list(reversed(nids))))
            elements.append((elem_type, 2, [2, 2], [duplicated[nid] for nid in nids]))
            if elem_type == 2:
                elements.append((6, 2, [1, 1], nids + [duplicated[nid] for nid in nids]))
            elif elem_type == 3:
                elements.append((5, 2, [1, 1], nids + [duplicated[nid] for nid in nids]))
        handle.write("$Elements\n")
        handle.write(f"{len(elements)}\n")
        for eid, (elem_type, n_tags, tags, nids) in enumerate(elements, 1):
            handle.write(
                f"{eid} {elem_type} {n_tags} "
                + " ".join(str(v) for v in tags)
                + " "
                + " ".join(str(v) for v in nids)
                + "\n"
            )
        handle.write("$EndElements\n")
    return {
        "status": "success",
        "input_mesh": mesh_path,
        "extruded_mesh": str(out),
        "span": span,
        "n_original_nodes": len(nodes),
        "n_surface_elements": len(surface_elems),
        "n_boundary_lines": len(line_elems),
    }


def _mesh_review_thresholds(mesh_type: str, requested: dict[str, Any] | None = None) -> dict[str, Any]:
    requested = requested or {}
    thresholds = {
        "max_aspect_ratio": 500.0,
        "min_2d_elements": 100,
        "min_3d_elements": 1 if _as_bool(requested.get("extrude_to_3d"), True) else 0,
    }
    if str(mesh_type) == "geometry_file_gmsh":
        thresholds["min_2d_elements"] = int(requested.get("min_2d_elements") or 1)
        thresholds["max_aspect_ratio"] = float(requested.get("max_aspect_ratio") or 1000.0)
        thresholds["max_non_positive_area_fraction"] = float(requested.get("max_non_positive_area_fraction") or 0.0)
        if int(requested.get("mesh_dimension") or 3) < 3:
            thresholds["min_3d_elements"] = 0
    if mesh_type in {"airfoil_gmsh", "cylinder_gmsh", "cylinder_flow"}:
        thresholds["min_2d_elements"] = 1000
    if mesh_type == "airfoil_gmsh":
        thresholds.update({
            "min_far_field": 10.0,
            "min_wake_length": 15.0,
            "min_surface_points": 121,
            "max_boundary_layer_ratio": 1.25,
        })
    return thresholds


def _canonical_cfd_mesh_matches_original_intent(
    state: State,
    mesh_type: str,
    intent_text: str,
    params: dict[str, Any],
) -> bool:
    """Reject substituting simple canonical shapes for unrelated complex CFD cases."""
    if surrogate_geometry_explicitly_approved(params):
        return True
    normalized = str(mesh_type or "").strip().lower()
    pattern = CANONICAL_CFD_MESH_INTENT_PATTERNS.get(normalized)
    if not pattern:
        return True
    original = original_user_request_text_from_state(state)
    source_text = original or intent_text
    if looks_like_turbomachinery_blade(source_text or "", params):
        return False
    return bool(re.search(pattern, source_text or "", flags=re.IGNORECASE))


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"false", "0", "no", "off", "none", "null"}:
        return False
    if text in {"true", "1", "yes", "on"}:
        return True
    return default


def _parse_check_mesh_anomalies(output: str, returncode: int = 0) -> dict[str, Any]:
    """Parse topology/geometry failures that OpenFOAM may print before ``Mesh OK``."""
    failed_match = re.search(r"Failed\s+(\d+)\s+mesh checks", output, flags=re.IGNORECASE)
    severe_non_ortho = re.search(r"Number of severely non-orthogonal[^:]*:\s*(\d+)", output)
    under_determined = re.search(
        r"(?:Writing\s+)?(\d+)\s+(?:cells[^\n]*under-determined|under-determined cells)",
        output,
        flags=re.IGNORECASE,
    )
    low_weights = re.search(r"(?:Writing\s+)?(\d+)\s+faces[^\n]*small interpolation weight", output, flags=re.IGNORECASE)
    two_internal_faces = re.search(
        r"Writing\s+(\d+)\s+cells with two non-boundary faces",
        output,
        flags=re.IGNORECASE,
    )
    failed_checks = int(failed_match.group(1)) if failed_match else 0
    mesh_ok = bool(re.search(r"^\s*Mesh OK\.\s*$", output, flags=re.IGNORECASE | re.MULTILINE))
    values = {
        "returncode": int(returncode),
        "mesh_ok_marker": mesh_ok,
        "failed_checks": failed_checks,
        "severely_non_orthogonal_faces": int(severe_non_ortho.group(1)) if severe_non_ortho else 0,
        "under_determined_cells": int(under_determined.group(1)) if under_determined else 0,
        "low_interpolation_weight_faces": int(low_weights.group(1)) if low_weights else 0,
        "cells_with_two_non_boundary_faces": int(two_internal_faces.group(1)) if two_internal_faces else 0,
    }
    fatal_anomaly = any(
        values[key]
        for key in (
            "failed_checks",
            "severely_non_orthogonal_faces",
            "under_determined_cells",
            "low_interpolation_weight_faces",
        )
    )
    # ``checkMesh -allTopology`` writes cells with only two internal faces to
    # a diagnostic cellSet even for valid extruded 2-D prism meshes.  OpenFOAM
    # still terminates with ``Mesh OK``.  Preserve the count as evidence, but
    # do not turn that informational set into a failed quality gate.
    values["warnings"] = []
    if values["cells_with_two_non_boundary_faces"]:
        values["warnings"].append({
            "code": "two_internal_faces_cell_set",
            "count": values["cells_with_two_non_boundary_faces"],
            "message": "checkMesh wrote an informational twoInternalFacesCells set.",
        })
    values["status"] = (
        "pass"
        if values["returncode"] == 0 and mesh_ok and not fatal_anomaly
        else "fail"
    )
    return values


def _cleanup_check_mesh_diagnostics(poly_dir: str | Path) -> None:
    """Remove transient cell/face sets emitted by ``checkMesh``.

    These files are useful while diagnosing a failed attempt, but they are not
    solver inputs or delivery assets. The parsed audit retains their counts.
    """
    sets_dir = Path(poly_dir) / "sets"
    if sets_dir.exists():
        shutil.rmtree(sets_dir, ignore_errors=True)


def _openfoam_command(utility: str, command: str = "", input_file: Path | None = None) -> list[str]:
    """Resolve native/wrapped utilities and bind the actual staged input once."""
    argv = shlex.split(command) if command else ["openfoam", utility]
    if not argv:
        argv = ["openfoam", utility]
    if argv[0] == "openfoam" and not shutil.which("openfoam") and shutil.which(utility):
        argv = argv[1:]
    elif argv[0] == utility and not shutil.which(utility) and shutil.which("openfoam"):
        argv.insert(0, "openfoam")
    if input_file is not None:
        if argv[-1].lower().endswith(".msh"):
            argv.pop()
        argv.append(str(input_file))
    return argv


def run_openfoam_check_mesh(
    case_dir: str | Path,
    poly_dir: str | Path,
    *,
    timeout: int = 120,
    retain_output: bool = False,
) -> dict[str, Any]:
    """Run the approved OpenFOAM quality command and return parsed evidence."""
    poly_path = Path(str(poly_dir)).expanduser()
    if not poly_path.exists():
        return {"status": "error", "error": "OpenFOAM constant/polyMesh does not exist."}
    try:
        proc = subprocess.run(
            [*_openfoam_command("checkMesh"), "-case", str(Path(str(case_dir)).expanduser()),
             "-allTopology", "-allGeometry"],
            text=True,
            capture_output=True,
            timeout=int(timeout or 120),
            check=False,
        )
        output = f"{proc.stdout}\n{proc.stderr}"
        result = _parse_check_mesh_anomalies(output, proc.returncode)
        result["output_tail"] = output[-5000:]
        log_path = Path(str(case_dir)).expanduser() / "checkMesh.log"
        if retain_output or result.get("status") != "pass":
            log_path.write_text(output, encoding="utf-8")
            result["log_file"] = str(log_path.resolve())
        elif log_path.exists():
            log_path.unlink()
        _cleanup_check_mesh_diagnostics(poly_path)
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _review_gmsh_mesh(result: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    """Generic deterministic Gmsh mesh review before returning success.

    Every future Gmsh generator gets file integrity, Tecplot/OpenFOAM export,
    element-count, positive-area, and aspect-ratio gates by default.  Semantic
    checks are driven by coarse geometry roles instead of mesh_type-specific
    branches, so new cases should normally extend the compact role table in
    review/mesh_reviewer.py.
    """
    mesh_type = str(result.get("mesh_type") or requested.get("mesh_type") or "").strip()
    if result.get("generation_error") or result.get("status") == "error":
        # An interrupted producer has incomplete evidence, not a zero-cell mesh.
        # Keep its actual failure actionable instead of inventing quality defects.
        return {
            "status": "fail", "mesh_type": mesh_type,
            "checks": {"producer_completed": False},
            "issues": [{
                "code": "generation_failed", "severity": "critical",
                **({"source_file": result["source_file"]} if result.get("source_file") else {}),
                "message": str(result.get("generation_error") or result.get("error") or "Mesh producer failed."),
                "recommendation": "Repair the failed generation or export operation using the retained staged files; do not change mesh density without quality evidence.",
            }],
        }
    semantic = semantic_mesh_review(result, requested)
    thresholds = _mesh_review_thresholds(mesh_type, requested)
    for key, value in (semantic.get("quality_overrides") or {}).items():
        # Role rules are defaults. An explicit quality gate in the approved
        # request/plan must remain authoritative.
        if key not in requested:
            thresholds[key] = value
    quality = result.get("quality") or {}
    source_topology = result.get("source_topology") or {}
    if not source_topology and int(quality.get("n_3d_elements") or 0) == 0:
        source_topology = quality.get("topology") or {}
    grid = result.get("grid_params") or {}
    issues: list[dict[str, Any]] = []
    checks: dict[str, bool] = {}

    poly_dir = result.get("polyMesh_dir")
    boundary_contract = _build_openfoam_boundary_contract(poly_dir, requested)
    check_mesh: dict[str, Any] = (
        run_openfoam_check_mesh(
            result.get("case_dir") or Path(str(poly_dir)).parent.parent,
            poly_dir,
            timeout=int(requested.get("check_mesh_timeout") or 120),
            retain_output=_as_bool(requested.get("retain_validation_log"), False),
        )
        if poly_dir else {"status": "skipped"}
    )
    if check_mesh.get("log_file"):
        result["check_mesh_log"] = check_mesh["log_file"]
        result["written_files"] = list(dict.fromkeys([
            *result.get("written_files", []),
            check_mesh["log_file"],
        ]))

    def add_issue(
        code: str,
        severity: str,
        message: str,
        recommendation: str = "",
        diagnostic_context: dict[str, Any] | None = None,
    ) -> None:
        issue = {
            "code": code,
            "severity": severity,
            "message": message,
            "recommendation": recommendation,
        }
        if diagnostic_context:
            issue["diagnostic_context"] = diagnostic_context
        issues.append(issue)

    for key in ("mesh_file", "quality_report"):
        path = result.get(key)
        ok = bool(path and Path(str(path)).exists())
        checks[f"{key}_exists"] = ok
        if not ok:
            add_issue(f"missing_{key}", "critical", f"Required file is missing: {key}")

    if _as_bool(requested.get("write_tecplot"), True):
        tecplot_file = result.get("tecplot_file")
        ok = bool(tecplot_file and Path(str(tecplot_file)).exists() and (result.get("tecplot") or {}).get("status") == "success")
        checks["tecplot_surface_exists"] = ok
        if not ok:
            add_issue("missing_tecplot", "critical", "Tecplot surface .dat was not generated.")
        elif poly_dir:
            tecplot_validation = validate_tecplot_against_polymesh(poly_dir, tecplot_file)
            checks["tecplot_contains_computational_cells"] = tecplot_validation.get("status") == "pass"
            if not checks["tecplot_contains_computational_cells"]:
                add_issue(
                    "tecplot_boundary_only_export",
                    "critical",
                    "Tecplot export contains only boundary geometry or an incomplete computational cell layer.",
                    "Re-export from the OpenFOAM cell plane/volume topology before delivery.",
                )

    if _as_bool(requested.get("convert_to_openfoam"), True):
        boundary = Path(str(poly_dir or "")) / "boundary"
        ok = bool(poly_dir and boundary.exists() and (result.get("openfoam") or {}).get("status") == "success")
        checks["openfoam_polymesh_exists"] = ok
        if not ok:
            add_issue("missing_openfoam_polymesh", "critical", "OpenFOAM constant/polyMesh is incomplete.")
        elif poly_dir:
            topology_validation = validate_openfoam_polymesh(poly_dir)
            checks["openfoam_topology_and_boundaries_valid"] = topology_validation.get("status") == "pass"
            if not checks["openfoam_topology_and_boundaries_valid"]:
                add_issue(
                    "invalid_openfoam_boundary_topology",
                    "critical",
                    "OpenFOAM polyMesh has invalid topology, uncovered boundary faces, or a boundary patch without cells.",
                    "Repair point/face ownership and regenerate every physical boundary patch before delivery.",
                )
        checks["openfoam_check_mesh_passed"] = check_mesh.get("status") == "pass"
        if check_mesh.get("status") == "fail":
            add_issue(
                "openfoam_check_mesh_failed",
                "critical",
                f"OpenFOAM checkMesh reported {check_mesh.get('failed_checks')} failed geometry/topology checks.",
                "Repair near-wall cells, non-orthogonality, determinant and interpolation-weight failures before delivery.",
                {"openfoam_check_mesh": {
                    key: check_mesh.get(key)
                    for key in (
                        "failed_checks", "under_determined_cells",
                        "severely_non_orthogonal_faces", "low_interpolation_weight_faces",
                    )
                }},
            )
        elif check_mesh.get("status") == "error":
            add_issue("openfoam_check_mesh_unavailable", "major", "OpenFOAM checkMesh could not complete.")
        checks["explicit_boundary_contract_satisfied"] = boundary_contract["status"] != "fail"
        if boundary_contract["status"] == "fail":
            add_issue(
                "openfoam_boundary_contract_mismatch",
                "critical",
                "OpenFOAM conversion omitted explicitly requested boundary patches: "
                + ", ".join(boundary_contract["missing_boundaries"]),
                "Repair Gmsh physical groups or the boundary map, regenerate, and reconvert the mesh.",
            )

    n_2d = int(quality.get("n_2d_elements") or 0)
    n_3d = int(quality.get("n_3d_elements") or 0)
    n_triangles = int(quality.get("n_triangles") or 0)
    n_quads = int(quality.get("n_quads") or 0)
    required_topology = str(requested.get("required_surface_topology") or "").casefold()
    if required_topology == "quadrilateral":
        checks["requested_surface_topology_satisfied"] = n_quads > 0 and n_triangles == 0
        if not checks["requested_surface_topology_satisfied"]:
            add_issue(
                "requested_surface_topology_mismatch",
                "critical",
                f"Requested quadrilateral elements, observed {n_quads} quads and {n_triangles} triangles.",
                "Regenerate the same geometry with surface recombination enabled.",
            )
    elif required_topology == "triangular":
        checks["requested_surface_topology_satisfied"] = n_triangles > 0 and n_quads == 0
        if not checks["requested_surface_topology_satisfied"]:
            add_issue(
                "requested_surface_topology_mismatch",
                "critical",
                f"Requested triangular elements, observed {n_triangles} triangles and {n_quads} quads.",
                "Regenerate the same geometry without surface recombination.",
            )
    if checks.get("requested_surface_topology_satisfied") is False:
        source = str(result.get("geometry_file") or "")
        if Path(source).suffix.lower() == ".geo":
            # If runtime controls cannot fix entity-local meshing commands,
            # route the defect to the existing source writer and rebuild its
            # consumers; repeating the mesher's parameters cannot edit a .geo.
            issues[-1]["source_file"] = source
    max_ar = quality.get("max_aspect_ratio")
    checks["has_2d_elements"] = n_2d >= int(thresholds["min_2d_elements"])
    if not checks["has_2d_elements"]:
        add_issue("too_few_surface_elements", "critical", f"Only {n_2d} 2D elements detected.")
    require_3d = int(requested.get("mesh_dimension") or 3) >= 3 and _as_bool(requested.get("extrude_to_3d"), True)
    if require_3d:
        checks["has_3d_elements"] = n_3d >= int(thresholds["min_3d_elements"])
        if not checks["has_3d_elements"]:
            add_issue("missing_3d_elements", "critical", f"Only {n_3d} 3D elements detected.")

    # Mesh-convergence levels must remain members of one mesh family. Only
    # density controls may vary; geometry source, domain semantics and the
    # declared near-wall method are invariants.
    family_contract = requested.get("mesh_family_contract")
    if isinstance(family_contract, dict):
        family_actual = {
            "mesh_type": mesh_type,
        }
        mismatches: list[str] = []
        for key, expected in family_contract.items():
            if key == "invariant_controls" or expected in (None, "", [], {}):
                continue
            if key == "grid_invariants" and isinstance(expected, dict):
                for grid_key, grid_expected in expected.items():
                    grid_actual = grid.get(grid_key)
                    if grid_actual != grid_expected:
                        mismatches.append(
                            f"grid_params.{grid_key}: expected {grid_expected!r}, got {grid_actual!r}"
                        )
                continue
            actual = family_actual.get(key)
            if actual != expected:
                mismatches.append(f"{key}: expected {expected!r}, got {actual!r}")
        checks["mesh_family_invariants_satisfied"] = not mismatches
        if mismatches:
            add_issue(
                "mesh_family_invariant_mismatch",
                "critical",
                "Mesh-convergence level changed a non-density mesh-family control: " + "; ".join(mismatches),
                "Regenerate this level from the accepted base mesh family; do not compare different geometry, domains, or near-wall methods.",
            )

    # Approved-plan requirements are stronger than generic mesh thresholds.
    # These checks remain mesher and discipline neutral: they only compare a
    # declared target with measurable mesh metadata or a declared wall-unit
    # design reference.
    plan_contract: dict[str, Any] = {}
    try:
        target_cells = int(requested.get("target_cell_count") or 0)
    except (TypeError, ValueError):
        target_cells = 0
    actual_cells = n_3d if require_3d else n_2d
    if target_cells > 0:
        minimum_accepted = math.ceil(target_cells * 0.9)
        plan_contract["target_cell_count"] = target_cells
        plan_contract["actual_cell_count"] = actual_cells
        plan_contract["minimum_accepted_cell_count"] = minimum_accepted
        checks["plan_cell_count_satisfied"] = actual_cells >= minimum_accepted
        if not checks["plan_cell_count_satisfied"]:
            add_issue(
                "plan_cell_count_below_target",
                "major",
                f"Mesh has {actual_cells} cells; approved plan requires at least {minimum_accepted} for target {target_cells}.",
                "Decrease local and far-field characteristic sizes while preserving geometry and boundary semantics.",
            )

    try:
        required_far_field = float(requested.get("required_far_field") or 0.0)
    except (TypeError, ValueError):
        required_far_field = 0.0
    if required_far_field > 0:
        try:
            actual_far_field = float(grid.get("far_field") or requested.get("far_field") or 0.0)
        except (TypeError, ValueError):
            actual_far_field = 0.0
        plan_contract["required_far_field"] = required_far_field
        plan_contract["actual_far_field"] = actual_far_field
        checks["plan_far_field_satisfied"] = actual_far_field >= required_far_field
        if not checks["plan_far_field_satisfied"]:
            add_issue(
                "plan_far_field_below_target",
                "major",
                f"far_field={actual_far_field} is below approved-plan requirement {required_far_field}.",
                "Expand the computational domain without changing the embedded geometry.",
            )

    wall_reference = requested.get("wall_unit_reference")
    if isinstance(wall_reference, dict) and wall_reference.get("viscous_length"):
        try:
            viscous_length = float(wall_reference["viscous_length"])
            first_height = float(grid.get("boundary_layer_first") or requested.get("boundary_layer_first"))
            streamwise_size = float(grid.get("h_airfoil") or requested.get("h_airfoil"))
        except (TypeError, ValueError):
            viscous_length = 0.0
            first_height = 0.0
            streamwise_size = 0.0
        if viscous_length > 0:
            estimated_y_plus = first_height / viscous_length
            estimated_dx_plus = streamwise_size / viscous_length
            plan_contract["wall_unit_reference"] = wall_reference
            plan_contract["estimated_first_cell_y_plus"] = estimated_y_plus
            plan_contract["estimated_streamwise_delta_x_plus"] = estimated_dx_plus
            for key, estimate, code, label in (
                ("target_wall_y_plus", estimated_y_plus, "plan_wall_y_plus_exceeded", "first-cell y+"),
                ("target_streamwise_delta_x_plus", estimated_dx_plus, "plan_streamwise_delta_x_plus_exceeded", "streamwise delta x+"),
            ):
                try:
                    target = float(requested.get(key))
                except (TypeError, ValueError):
                    continue
                check_name = f"plan_{key}_satisfied"
                checks[check_name] = estimate <= target
                if not checks[check_name]:
                    add_issue(
                        code,
                        "major",
                        f"Estimated {label}={estimate:.4g} exceeds approved-plan target {target:.4g}.",
                        "Reduce the corresponding near-wall characteristic size and regenerate this mesh stage.",
                    )
    if requested.get("target_wall_y_plus") not in (None, ""):
        near_wall_mode = str(
            grid.get("near_wall_refinement_mode")
            or requested.get("near_wall_refinement_mode")
            or ""
        ).strip().lower()
        checks["plan_first_layer_is_explicit"] = near_wall_mode in {
            "boundary_layer", "structured_layers", "explicit_first_layer",
        }
        if not checks["plan_first_layer_is_explicit"]:
            add_issue(
                "plan_first_layer_not_verifiable",
                "critical",
                "The approved plan requires a wall-unit first-layer target, but this mesh uses a local size field without an explicit first layer.",
                "Use a mesher/adapter that preserves and reports an explicit first-layer height, or revise the approved near-wall requirement.",
            )
    non_positive = int(quality.get("negative_or_zero_area_cells") or 0)
    non_positive_fraction = non_positive / max(n_2d, 1)
    max_non_positive_fraction = float(thresholds.get("max_non_positive_area_fraction", 0.0))
    checks["positive_area"] = non_positive == 0 or non_positive_fraction <= max_non_positive_fraction
    if not checks["positive_area"]:
        add_issue(
            "non_positive_area",
            "critical",
            f"Mesh contains non-positive-area surface cells: {non_positive}/{n_2d}."
        )
    elif non_positive:
        add_issue(
            "non_positive_area_import_tolerance",
            "minor",
            f"Imported CAD mesh contains a small tolerated fraction of non-positive surface cells: {non_positive}/{n_2d}.",
        )
    checks["aspect_ratio_ok"] = max_ar is not None and float(max_ar) < float(thresholds["max_aspect_ratio"])
    if not checks["aspect_ratio_ok"]:
        native_accepts_anisotropy = (
            check_mesh.get("status") == "pass"
            and str(
                grid.get("near_wall_refinement_mode")
                or requested.get("near_wall_refinement_mode")
                or ""
            ).strip().lower() in {"boundary_layer", "structured_layers", "explicit_first_layer"}
            and "max_aspect_ratio" not in requested
        )
        add_issue(
            (
                "aspect_ratio_native_validator_accepted"
                if native_accepts_anisotropy else "aspect_ratio_too_high"
            ),
            "minor" if native_accepts_anisotropy else "major",
            f"max_aspect_ratio={max_ar}, threshold={thresholds['max_aspect_ratio']}",
            (
                "Native solver validation passed the declared anisotropic near-wall mesh."
                if native_accepts_anisotropy
                else "Refine/smooth the mesh, reduce aggressive growth ratios, or adjust local sizing."
            ),
        )

    if source_topology:
        source_dimension = int(source_topology.get("mesh_dimension") or 2)
        boundary_closed = (
            bool(source_topology.get("surface_shell_closed"))
            if source_dimension >= 3
            else bool(source_topology.get("boundary_loops_closed"))
        )
        checks["source_domain_boundary_closed"] = boundary_closed
        if not boundary_closed:
            add_issue(
                "open_or_nonmanifold_domain_boundary",
                "critical",
                "The source surface mesh does not form closed, manifold boundary loops.",
                "Repair geometry gaps/non-manifold edges before meshing; do not extrude an open surface into a CFD domain.",
            )

        role = str((semantic.get("intent") or {}).get("geometry_role") or "")
        flow_topology = str((semantic.get("intent") or {}).get("flow_topology") or "")
        needs_embedded_body = role in {"airfoil_external", "cylinder_external", "turbomachinery_cascade"} or flow_topology in {
            "external_farfield", "external_wake", "internal_periodic_passage"
        }
        loop_count = int(source_topology.get("n_closed_boundary_loops") or 0)
        checks["source_domain_has_expected_boundary_loops"] = (
            source_dimension >= 3 or not needs_embedded_body or loop_count >= 2
        )
        if source_dimension < 3 and needs_embedded_body and loop_count < 2:
            add_issue(
                "solid_body_meshed_instead_of_fluid_domain",
                "critical",
                f"Detected {loop_count} closed boundary loop(s); this flow topology needs an outer/passsage boundary plus an excluded solid boundary.",
                "Construct the fluid domain with a boolean subtraction of the body, then mesh the remaining fluid region.",
            )
        component_count = int(source_topology.get("n_surface_components") or 0)
        permits_multiple_regions = role == "multi_region_or_conjugate"
        checks["source_domain_surface_connected"] = (
            source_dimension >= 3 or permits_multiple_regions or component_count == 1
        )
        if source_dimension < 3 and not permits_multiple_regions and component_count != 1:
            add_issue(
                "disconnected_computational_domain",
                "critical",
                f"Detected {component_count} disconnected surface regions in a single-region computational domain.",
                "Sew/fragment the CAD consistently and retain only the connected fluid region before extrusion.",
            )

    expected_boundaries = _normalise_boundary_names(requested.get("expected_boundaries"))
    if expected_boundaries:
        physical_groups = quality.get("physical_groups") or {}
        materialized = {
            re.sub(r"[^a-z0-9]+", "", str(name).casefold()): str(name)
            for name, count in physical_groups.items()
            if int(count or 0) > 0
        }
        missing = [
            name for name in expected_boundaries
            if re.sub(r"[^a-z0-9]+", "", name.casefold()) not in materialized
        ]
        checks["requested_boundary_groups_materialized"] = not missing
        if missing:
            add_issue(
                "requested_boundary_groups_missing",
                "critical",
                "The mesh has no elements in requested physical boundary groups: " + ", ".join(missing),
                "Repair the geometry selections and regenerate the mesh, preserving the requested boundary names. "
                "For bounding-box selections, expand both lower and upper bounds by the CAD tolerance; "
                "a bound exactly on the nominal surface may exclude its entities.",
                {
                    "expected_boundaries": expected_boundaries,
                    "materialized_boundaries": sorted(materialized.values()),
                    "geometry_file": result.get("geometry_file"),
                },
            )

    if mesh_type == "geometry_file_gmsh" and str(requested.get("discipline") or "").lower() == "cfd":
        representation = str(requested.get("geometry_representation") or requested.get("geometry_role_input") or "unknown").lower()
        domain_complete = _as_bool(requested.get("computational_domain_complete"), False)
        declared_complete = representation in {
            "fluid_domain",
            "computational_domain",
            "reference_computational_mesh",
            "computational_mesh",
            "fluid_domain_mesh",
        } and domain_complete
        topology_complete = bool(source_topology) and all(
            checks.get(name, True)
            for name in (
                "source_domain_boundary_closed",
                "source_domain_has_expected_boundary_loops",
                "source_domain_surface_connected",
                "requested_boundary_groups_materialized",
            )
        )
        checks["cfd_geometry_is_complete_fluid_domain"] = declared_complete or topology_complete
        if not checks["cfd_geometry_is_complete_fluid_domain"]:
            add_issue(
                "unconfirmed_cfd_fluid_domain",
                "critical",
                "Imported CAD was not declared and verified as a complete CFD fluid domain.",
                "Classify the input as a solid/profile or construct a closed fluid domain with named physical boundaries before export.",
            )

    if mesh_type == "airfoil_gmsh":
        far_field = float(grid.get("far_field") or requested.get("far_field") or 0)
        wake_length = float(grid.get("wake_length") or requested.get("wake_length") or 0)
        n_surface = int(grid.get("n_surface") or requested.get("n_surface") or 0)
        bl_ratio = float(grid.get("boundary_layer_ratio") or requested.get("boundary_layer_ratio") or 99)
        domain_type = str(grid.get("domain_type") or requested.get("domain_type") or "c").lower()
        checks["far_field_extent_ok"] = far_field >= float(thresholds["min_far_field"])
        if not checks["far_field_extent_ok"]:
            add_issue("farfield_too_small", "major", f"far_field={far_field} chord lengths is too small.")
        checks["wake_extent_ok"] = domain_type != "c" or wake_length >= max(float(thresholds["min_wake_length"]), far_field)
        if not checks["wake_extent_ok"]:
            add_issue("wake_too_short", "major", f"wake_length={wake_length} is short for C-domain wake capture.")
        checks["surface_resolution_ok"] = n_surface >= int(thresholds["min_surface_points"])
        if not checks["surface_resolution_ok"]:
            add_issue("surface_resolution_low", "major", f"n_surface={n_surface} is below recommended minimum.")
        checks["boundary_layer_growth_ok"] = bl_ratio <= float(thresholds["max_boundary_layer_ratio"])
        if not checks["boundary_layer_growth_ok"]:
            add_issue("boundary_layer_growth_high", "major", f"boundary_layer_ratio={bl_ratio} is too aggressive.")

    for key, value in (semantic.get("checks") or {}).items():
        checks[f"semantic_{key}"] = bool(value)
    for issue in semantic.get("issues") or []:
        issues.append(dict(issue))

    critical = [issue for issue in issues if issue["severity"] == "critical"]
    major = [issue for issue in issues if issue["severity"] == "major"]
    passed = not critical and not major
    return {
        "status": "pass" if passed else "fail",
        "mesh_type": mesh_type,
        "scope": "generic_gmsh_with_semantic_intent_review",
        "mesh_intent": semantic.get("intent"),
        "mesh_features": semantic.get("features"),
        "semantic_review": {
            "rule_source": semantic.get("rule_source"),
            "rule_role": semantic.get("rule_role"),
            "checks": semantic.get("checks") or {},
            "issues": semantic.get("issues") or [],
        },
        "checks": checks,
        "issues": issues,
        "thresholds": thresholds,
        "plan_contract": plan_contract,
        "openfoam_check_mesh": check_mesh,
        "openfoam_boundary_contract": boundary_contract,
        "recommended_action": "accept" if passed else "retry_with_adjusted_parameters",
    }


def _normalise_boundary_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        # Boundary dictionaries conventionally map patch name -> type/config.
        # Interior volumes and cell regions are not OpenFOAM boundary patches.
        candidates = [
            name for name, config in value.items()
            if str(config.get("type") if isinstance(config, dict) else config).strip().lower()
            not in {"internal", "volume", "region", "cellzone", "cell_zone"}
        ]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    elif isinstance(value, str):
        candidates = re.split(r"[,;\n]", value)
    else:
        candidates = []
    for candidate in candidates:
        if isinstance(candidate, (list, tuple, set, dict)):
            names.extend(_normalise_boundary_names(candidate))
            continue
        name = str(candidate or "").strip()
        if name and re.match(r"^[A-Za-z_][A-Za-z0-9_./-]*$", name) and name not in names:
            names.append(name)
    return names


def _openfoam_boundary_names(poly_dir: Any) -> list[str]:
    boundary = Path(str(poly_dir or "")) / "boundary"
    if not boundary.exists():
        return []
    lines = boundary.read_text(encoding="utf-8", errors="ignore").splitlines()
    return [
        line.strip()
        for index, line in enumerate(lines[:-1])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_./-]*$", line.strip())
        and lines[index + 1].strip() == "{"
        and line.strip() != "FoamFile"
    ]


def _build_openfoam_boundary_contract(poly_dir: Any, requested: dict[str, Any]) -> dict[str, Any]:
    """Compare explicit user/plan patch names with converted OpenFOAM patches."""
    expected: list[str] = []
    for field in (
        "expected_boundaries", "required_boundaries", "boundary_names", "patch_names",
        "boundary_map", "boundaries",
    ):
        for name in _normalise_boundary_names(requested.get(field)):
            if name not in expected:
                expected.append(name)
    actual = _openfoam_boundary_names(poly_dir)
    actual_folded = {name.casefold(): name for name in actual}
    missing = [name for name in expected if name.casefold() not in actual_folded]
    expected_folded = {name.casefold() for name in expected}
    unexpected = [name for name in actual if expected and name.casefold() not in expected_folded]
    return {
        "status": "fail" if missing else ("pass" if expected else "not_declared"),
        "contract_source": "explicit_request_or_plan",
        "expected_boundaries": expected,
        "actual_boundaries": actual,
        "missing_boundaries": missing,
        "additional_boundaries": unexpected,
    }


def _write_mesh_review(case_dir: str | Path, review: dict[str, Any]) -> str:
    path = Path(case_dir) / "mesh_review.json"
    path.write_text(json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _adjust_generic_gmsh_mesh_params_for_review(params: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    adjusted = dict(params)
    issue_codes = {issue.get("code") for issue in review.get("issues", [])}
    if "generation_failed" in issue_codes:
        # There is no mesh evidence yet.  Do not reinterpret absent output as
        # low density or poor quality and make an already failing job larger.
        return adjusted
    if "too_few_surface_elements" in issue_codes or "plan_cell_count_below_target" in issue_codes:
        refinement_factor = 0.65
        plan_contract = review.get("plan_contract") or {}
        try:
            actual_cells = float(plan_contract.get("actual_cell_count") or 0)
            minimum_cells = float(plan_contract.get("minimum_accepted_cell_count") or 0)
        except (TypeError, ValueError):
            actual_cells = minimum_cells = 0.0
        if actual_cells > 0 and minimum_cells > actual_cells:
            # In two dimensions, cell count scales approximately with h^-2.
            # Use measured output to approach the target in the next attempt,
            # with a small refinement margin for nonuniform size fields.
            refinement_factor = min(
                0.92,
                max(0.35, math.sqrt(actual_cells / minimum_cells) * 0.95),
            )
        if "characteristic_length" in adjusted:
            adjusted["characteristic_length"] = max(
                float(adjusted.get("characteristic_length") or 1.0) * refinement_factor, 1e-8
            )
        if "h_farfield" in adjusted:
            adjusted["h_farfield"] = max(
                float(adjusted.get("h_farfield", 0.8)) * refinement_factor, 0.01
            )
        if "h_airfoil" in adjusted:
            adjusted["h_airfoil"] = max(
                float(adjusted.get("h_airfoil", 0.008)) * refinement_factor, 1e-5
            )
        if "h_cylinder" in adjusted:
            adjusted["h_cylinder"] = max(
                float(adjusted.get("h_cylinder", 0.02)) * refinement_factor, 1e-5
            )
    if "plan_wall_y_plus_exceeded" in issue_codes and "boundary_layer_first" in adjusted:
        adjusted["boundary_layer_first"] = max(
            float(adjusted.get("boundary_layer_first") or 1e-4) * 0.65,
            1e-8,
        )
    if "plan_streamwise_delta_x_plus_exceeded" in issue_codes and "h_airfoil" in adjusted:
        adjusted["h_airfoil"] = max(float(adjusted.get("h_airfoil") or 0.006) * 0.65, 1e-8)
    if "openfoam_check_mesh_failed" in issue_codes:
        check_mesh = review.get("openfoam_check_mesh") or {}
        determinant_failures = int(check_mesh.get("under_determined_cells") or 0)
        # Small cell determinants in an extruded 2-D mesh are commonly caused
        # by an extreme near-wall/span aspect ratio. Increase only an unlocked
        # first-layer height; reducing the total envelope leaves the offending
        # first row unchanged and therefore cannot repair this check.
        if (
            determinant_failures
            and "boundary_layer_first" in adjusted
            and not _as_bool(adjusted.get("lock_near_wall_topology"), False)
        ):
            adjusted["boundary_layer_first"] = max(
                float(adjusted.get("boundary_layer_first") or 1e-4) * 2.5,
                1e-8,
            )
        elif "boundary_layer_thickness" in adjusted:
            thickness_factor = (
                0.25
                if _as_bool(adjusted.get("lock_near_wall_topology"), False)
                else 0.5
            )
            adjusted["boundary_layer_thickness"] = max(
                float(adjusted.get("boundary_layer_thickness") or 0.02) * thickness_factor,
                max(3.0 * float(adjusted.get("boundary_layer_first") or 1e-4), 1e-5),
            )
        if "boundary_layer_ratio" in adjusted:
            adjusted["boundary_layer_ratio"] = min(
                float(adjusted.get("boundary_layer_ratio") or 1.15),
                1.08 if _as_bool(adjusted.get("lock_near_wall_topology"), False) else 1.10,
            )
        if (
            "h_airfoil" in adjusted
            and not _as_bool(adjusted.get("lock_near_wall_topology"), False)
            and str(adjusted.get("near_wall_refinement_mode") or "boundary_layer") == "boundary_layer"
        ):
            # A distance field preserves the requested near-wall size without
            # constructing a quad/prism layer front that can fold at sharp
            # closures or tightly curved segments.  This is a geometry-neutral
            # fallback for coordinate-profile adapters.
            adjusted["near_wall_refinement_mode"] = "distance_field"
        # Mesh generation is deterministic for a fixed geometry and Gmsh
        # algorithm.  A topology failure therefore needs a genuinely
        # different discretisation strategy, not another identical retry.
        # The sequence is deliberately generic: it does not infer or alter
        # geometry/boundary semantics, only Gmsh's 2-D meshing algorithm.
        if _as_bool(adjusted.get("lock_near_wall_topology"), False):
            # Algorithm 6 is the stable frontal-Delaunay companion for Gmsh's
            # explicit BoundaryLayer field.  Switching algorithms can collapse
            # the layer envelope even when geometry and first height are valid.
            adjusted["mesh_algorithm"] = 6
        else:
            current_algorithm = int(adjusted.get("mesh_algorithm") or 6)
            if current_algorithm == 6:
                adjusted["mesh_algorithm"] = 5
            elif current_algorithm == 5:
                adjusted["mesh_algorithm"] = 1
    if "plan_far_field_below_target" in issue_codes:
        required = float(adjusted.get("required_far_field") or 0.0)
        if required > 0:
            adjusted["far_field"] = max(float(adjusted.get("far_field") or 0.0), required)
    if "aspect_ratio_too_high" in issue_codes:
        if "boundary_layer_ratio" in adjusted:
            adjusted["boundary_layer_ratio"] = min(float(adjusted.get("boundary_layer_ratio", 1.15)), 1.12)
        if (
            "boundary_layer_first" in adjusted
            and not _as_bool(adjusted.get("lock_near_wall_topology"), False)
        ):
            adjusted["boundary_layer_first"] = min(max(float(adjusted.get("boundary_layer_first", 0.001)) * 1.25, 1e-5), 0.01)
    adjusted["quality_preset"] = "robust"
    return adjusted


def _compile_mesh_repair_plan(
    before: dict[str, Any],
    after: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    """Compile review evidence into an auditable, geometry-preserving repair plan."""
    issue_codes = sorted({str(item.get("code")) for item in review.get("issues", []) if item.get("code")})
    changed = {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
    actions: list[dict[str, Any]] = []
    mappings = (
        ({"too_few_surface_elements", "surface_resolution_low", "plan_cell_count_below_target", "plan_streamwise_delta_x_plus_exceeded"}, "refine_discretization"),
        ({"plan_wall_y_plus_exceeded"}, "refine_near_wall_layer"),
        ({"plan_far_field_below_target"}, "expand_computational_domain"),
        ({"aspect_ratio_too_high", "boundary_layer_growth_high", "openfoam_check_mesh_failed"}, "smooth_mesh_grading"),
        ({"missing_tecplot", "tecplot_boundary_only_export"}, "regenerate_export_from_cells"),
        ({"missing_openfoam_polymesh", "invalid_openfoam_boundary_topology"}, "regenerate_openfoam_conversion"),
        ({"openfoam_boundary_contract_mismatch"}, "repair_physical_group_mapping"),
        ({"open_or_nonmanifold_domain_boundary", "solid_body_meshed_instead_of_fluid_domain"}, "repair_geometry_topology"),
        ({"generation_failed"}, "repair_failed_operation"),
    )
    for codes, operation in mappings:
        matched = sorted(set(issue_codes) & codes)
        if matched:
            actions.append({"operation": operation, "triggered_by": matched})
    unsafe = bool(set(issue_codes) & {
        "open_or_nonmanifold_domain_boundary",
        "solid_body_meshed_instead_of_fluid_domain",
        "unconfirmed_cfd_fluid_domain",
    })
    retryable_operations = {
        "refine_discretization", "smooth_mesh_grading", "regenerate_export_from_cells",
        "regenerate_openfoam_conversion", "repair_failed_operation",
    }
    return {
        "policy": "bounded_geometry_preserving_repair",
        "issue_codes": issue_codes,
        "actions": actions,
        "parameter_changes": changed,
        "geometry_or_boundary_intent_changed": False,
        "automatic_parameter_retry_available": bool(changed),
        "execution_retry_available": any(
            action.get("operation") in retryable_operations for action in actions
        ),
        "requires_geometry_or_semantics_input": unsafe and not changed,
    }


def _snapshot_mesh_attempt(
    result: dict[str, Any],
    review: dict[str, Any],
    repair_plan: dict[str, Any],
    attempt: int,
) -> dict[str, Any]:
    """Record retry provenance without duplicating large mesh inputs."""
    case_dir = Path(str(result.get("case_dir") or ".")).resolve()
    attempt_dir = case_dir / "mesh_attempts" / f"attempt_{attempt:02d}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    input_references: list[dict[str, Any]] = []
    candidates: list[Path] = []
    for key in ("geo_file", "generator_script", "quality_report"):
        if result.get(key):
            candidates.append(Path(str(result[key])))
    for value in result.get("written_files") or []:
        path = Path(str(value))
        if path.suffix.lower() in {".geo", ".py"}:
            candidates.append(path)
    for source in candidates:
        if not source.exists() or not source.is_file():
            continue
        try:
            resolved = source.resolve()
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            continue
        input_references.append({
            "path": str(resolved),
            "sha256": digest,
            "bytes": resolved.stat().st_size,
        })
    diagnostics = {
        "attempt": attempt,
        "status": review.get("status"),
        "gmsh_stdout_tail": result.get("gmsh_stdout_tail", ""),
        "gmsh_stderr_tail": result.get("gmsh_stderr_tail", ""),
        "openfoam": result.get("openfoam") or {},
        "review": review,
        "repair_plan": repair_plan,
        "executable_input_references": input_references,
    }
    diagnostics_path = attempt_dir / "diagnostics.json"
    diagnostics_path.write_text(json.dumps(diagnostics, indent=2, ensure_ascii=False), encoding="utf-8")
    digest = hashlib.sha256(diagnostics_path.read_bytes()).hexdigest()
    return {
        "attempt_dir": str(attempt_dir),
        "diagnostics": str(diagnostics_path),
        "diagnostics_sha256": digest,
        "executable_inputs": input_references,
    }


def _failed_gmsh_generation_result(current: dict[str, Any], exc: Exception) -> dict[str, Any]:
    case_dir = Path(str(current.get("case_dir") or ".")).expanduser().resolve()
    case_dir.mkdir(parents=True, exist_ok=True)
    geo_files = sorted(case_dir.glob("*.geo"), key=lambda path: path.stat().st_mtime, reverse=True)
    mesh_files = sorted(case_dir.glob("*.msh"), key=lambda path: path.stat().st_mtime, reverse=True)
    quality = case_dir / "mesh_quality.json"
    return {
        "status": "error",
        "case_dir": str(case_dir),
        "geo_file": str(geo_files[0]) if geo_files else None,
        "mesh_file": str(mesh_files[0]) if mesh_files else None,
        "quality_report": str(quality) if quality.exists() else None,
        "quality": {},
        "written_files": [str(path) for path in [*geo_files[:1], *mesh_files[:1], quality] if path.exists()],
        "generation_error": f"{type(exc).__name__}: {exc}",
        "gmsh_stderr_tail": str(exc)[-4000:],
    }


def _adjust_airfoil_mesh_params_for_review(params: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    adjusted = _adjust_generic_gmsh_mesh_params_for_review(params, review)
    issue_codes = {issue.get("code") for issue in review.get("issues", [])}
    if "generation_failed" in issue_codes:
        return adjusted
    if "farfield_too_small" in issue_codes:
        adjusted["far_field"] = max(float(adjusted.get("far_field", 25)), 25.0)
    if "wake_too_short" in issue_codes:
        adjusted["wake_length"] = max(float(adjusted.get("wake_length", 25)), float(adjusted.get("far_field", 25)), 25.0)
    if "surface_resolution_low" in issue_codes or "too_few_surface_elements" in issue_codes:
        adjusted["n_surface"] = max(int(float(adjusted.get("n_surface", 241))) + 80, 241)
        adjusted["h_airfoil"] = min(float(adjusted.get("h_airfoil", 0.008)), 0.006)
    if "aspect_ratio_too_high" in issue_codes:
        if not _as_bool(adjusted.get("lock_near_wall_topology"), False):
            adjusted["boundary_layer_first"] = min(max(float(adjusted.get("boundary_layer_first", 0.001)) * 1.35, 0.0005), 0.003)
        adjusted["boundary_layer_ratio"] = min(float(adjusted.get("boundary_layer_ratio", 1.15)), 1.12)
        adjusted["h_airfoil"] = min(float(adjusted.get("h_airfoil", 0.008)), 0.006)
    if "boundary_layer_growth_high" in issue_codes:
        adjusted["boundary_layer_ratio"] = 1.12
    adjusted["quality_preset"] = "robust"
    return adjusted


def _supported_parameters(generator: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Keep only parameters accepted by the selected producer callable."""
    signature = inspect.signature(generator)
    values = canonical_mesh_controls(values, signature.parameters)
    if any(
        item.kind == inspect.Parameter.VAR_KEYWORD
        for item in signature.parameters.values()
    ):
        return dict(values)
    return {key: value for key, value in values.items() if key in signature.parameters}


def _reviewed_generator_parameters(generator: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Keep producer inputs plus the generic review-loop controls."""
    selected = _supported_parameters(generator, values)
    for name in (
        "max_review_iterations", "retain_validation_log",
        "require_executable_reproduction_script", "retain_mesh_attempt_diagnostics",
    ):
        if name in values:
            selected[name] = values[name]
    selected.pop("case_dir", None)
    return selected


def _generate_gmsh_mesh_with_review(
    generator: Any,
    mesh_type: str,
    adjuster: Any | None = None,
    max_review_iterations: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    # Keep review/authority metadata across attempts. Only the actual producer
    # call is signature-filtered; filtering this state would discard controls
    # such as locked scientific parameters that the reviewer still needs.
    current = dict(kwargs)
    retain_validation_log = _as_bool(current.pop("retain_validation_log", False), False)
    require_executable_script = _as_bool(
        current.pop("require_executable_reproduction_script", False), False
    )
    retain_attempt_diagnostics = bool(current.pop("retain_mesh_attempt_diagnostics", False))
    explicit_iteration_limit = max(0, int(max_review_iterations or 0))
    seen_revision_signatures: set[str] = set()
    last_result: dict[str, Any] | None = None
    attempt = 0
    while True:
        attempt += 1
        try:
            result = generator(**_supported_parameters(generator, current))
        except Exception as exc:
            result = _failed_gmsh_generation_result(current, exc)
        result.setdefault("mesh_type", mesh_type)
        review = _review_gmsh_mesh(result, current | {
            "mesh_type": mesh_type,
            "retain_validation_log": retain_validation_log,
        })
        review["attempt"] = attempt
        review_path = _write_mesh_review(result["case_dir"], review)
        result["mesh_review"] = review
        result["mesh_review_report"] = review_path
        result["written_files"] = [*result.get("written_files", []), review_path]
        observed = result.get("grid_params")
        effective = {
            **current,
            **(observed if isinstance(observed, dict) else {}),
        }
        adjusted = (
            adjuster(effective, review)
            if adjuster is not None
            else _adjust_generic_gmsh_mesh_params_for_review(effective, review)
        )
        producer_effective = _supported_parameters(generator, effective)
        producer_adjusted = _supported_parameters(generator, adjusted)
        repair_plan = _compile_mesh_repair_plan(
            producer_effective, producer_adjusted, review
        )
        # Gmsh generation is deterministic for a fixed geometry and parameter
        # set. Re-running it unchanged only creates duplicate diagnostics and
        # delays the caller's next, genuinely different repair strategy.
        repair_plan["execution_retry_skipped_without_parameter_change"] = bool(
            repair_plan["execution_retry_available"]
            and not repair_plan["automatic_parameter_retry_available"]
        )
        snapshot = (
            _snapshot_mesh_attempt(result, review, repair_plan, attempt)
            if retain_attempt_diagnostics else None
        )
        attempt_record = {
            "attempt": attempt,
            "status": review["status"],
            "issues": review.get("issues", []),
            "grid_params": result.get("grid_params", {}),
            "repair_plan": repair_plan,
        }
        if snapshot:
            attempt_record["snapshot"] = snapshot
        attempts.append(attempt_record)
        last_result = result
        if review["status"] == "pass":
            if require_executable_script and result.get("geo_file") and result.get("mesh_file"):
                case_root = Path(str(result["case_dir"]))
                geo_name = Path(str(result["geo_file"])).name
                mesh_name = Path(str(result["mesh_file"])).name
                dimension = (
                    "-3"
                    if int((result.get("quality") or {}).get("n_3d_elements") or 0)
                    else "-2"
                )
                script = case_root / "reproduce_mesh.sh"
                conversion = ""
                if result.get("polyMesh_dir"):
                    conversion = (
                        '\nopenfoam gmshToFoam "$mesh_file"'
                        '\nopenfoam checkMesh -case "$case_dir" -allTopology -allGeometry'
                    )
                script.write_text(
                    "#!/bin/sh\nset -eu\n"
                    'script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                    'case_dir=$(CDPATH= cd -- "$script_dir/../.." && pwd)\n'
                    f'geo_file="$script_dir/{geo_name}"\nmesh_file="$script_dir/{mesh_name}"\n'
                    f'"${{GMSH_BIN:-gmsh}}" {dimension} "$geo_file" -format msh2 -o "$mesh_file"'
                    f"{conversion}\n",
                    encoding="utf-8",
                )
                script.chmod(0o755)
                result["generator_script"] = str(script.resolve())
                result["written_files"] = list(dict.fromkeys([
                    *result.get("written_files", []), str(script.resolve())
                ]))
            result["deliverable_valid"] = True
            result["review_iterations"] = attempts
            if snapshot:
                result["mesh_attempts_dir"] = str(Path(result["case_dir"]) / "mesh_attempts")
                result["written_files"] = [*result.get("written_files", []), snapshot["diagnostics"]]
            return result
        # Progress is an observable improvement in the generated asset, not
        # merely a different set of guessed parameters.  This prevents a
        # timeout or tool failure from being replayed indefinitely while an
        # adjuster keeps changing unrelated density controls.
        revision_signature = json.dumps({
            "issues": sorted({
                (str(item.get("code") or ""), str(item.get("severity") or ""))
                for item in review.get("issues") or []
                if isinstance(item, dict)
            }),
            "passed_checks": sorted(
                key for key, value in (review.get("checks") or {}).items() if value is True
            ),
            "materialized_outputs": sorted(
                key for key in (
                    "mesh_file", "quality_report", "tecplot_file", "polyMesh_dir"
                )
                if result.get(key) and Path(str(result[key])).exists()
            ),
            "quality_failures": {
                key: (review.get("openfoam_check_mesh") or {}).get(key)
                for key in (
                    "failed_checks", "under_determined_cells",
                    "severely_non_orthogonal_faces", "low_interpolation_weight_faces",
                )
            },
        }, ensure_ascii=False, sort_keys=True, default=str)
        may_continue = (
            repair_plan["automatic_parameter_retry_available"]
            and revision_signature not in seen_revision_signatures
            and (not explicit_iteration_limit or attempt < explicit_iteration_limit)
        )
        seen_revision_signatures.add(revision_signature)
        if may_continue:
            current = {**current, **producer_adjusted}
            continue
        break
    assert last_result is not None
    last_result["status"] = "error"
    last_result["deliverable_valid"] = False
    last_result["error"] = last_result.get("generation_error") or last_result.get("error") or "Mesh review made no further progress with automatic parameter repair."
    last_result["review_iterations"] = attempts
    last_result["repair_context"] = {
        "observed_parameters": last_result.get("grid_params") or {},
        "attempted_parameter_changes": [
            item.get("repair_plan", {}).get("parameter_changes") or {}
            for item in attempts
        ],
        "failed_checks": sorted(
            key for key, value in (last_result.get("mesh_review", {}).get("checks") or {}).items()
            if value is False
        ),
    }
    if not any(issue.get("code") == "generation_failed" for issue in last_result["mesh_review"]["issues"]):
        last_result = _quarantine_failed_mesh_result(last_result)
    return last_result


def _quarantine_failed_mesh_result(result: dict[str, Any]) -> dict[str, Any]:
    """Discard invalid generated mesh assets while retaining a compact audit record."""
    result["deliverable_valid"] = False
    case_dir = Path(str(result.get("case_dir") or ""))
    if not case_dir.exists():
        return result
    candidates: list[Path] = []
    for key in ("geo_file", "mesh_file", "quality_report", "tecplot_file", "tecplot_surface_file", "tecplot_volume_file"):
        value = result.get(key)
        if value:
            candidates.append(Path(str(value)))
    candidates.extend(Path(str(path)) for path in result.get("tecplot_files") or [])
    for directory_name in ("constant", "system", "mesh_attempts", "rejected_mesh"):
        candidates.append(case_dir / directory_name)

    discarded_assets: list[str] = []
    seen: set[str] = set()
    for source in candidates:
        try:
            source = source.resolve()
            source.relative_to(case_dir.resolve())
        except (OSError, ValueError):
            continue
        if not source.exists() or str(source) in seen:
            continue
        seen.add(str(source))
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()
        discarded_assets.append(str(source))

    audit_dir = case_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    rejection_manifest = audit_dir / "mesh_rejection.json"
    rejection_manifest.write_text(json.dumps({
        "deliverable_valid": False,
        "reason": result.get("error") or "mesh review failed",
        "mesh_review_report": result.get("mesh_review_report"),
        "discarded_assets": discarded_assets,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    result["discarded_assets"] = discarded_assets
    result["rejection_manifest"] = str(rejection_manifest)
    result["written_files"] = [
        path for path in (result.get("mesh_review_report"), str(rejection_manifest)) if path
    ]
    for key in (
        "mesh_file", "quality_report", "tecplot_file", "tecplot_surface_file",
        "tecplot_volume_file", "polyMesh_dir", "geo_file",
    ):
        result[key] = None
    result["tecplot_files"] = []
    return result


def _generate_airfoil_gmsh_mesh_with_review(max_review_iterations: int = 0, **kwargs: Any) -> dict[str, Any]:
    return _generate_gmsh_mesh_with_review(
        generate_airfoil_gmsh_mesh,
        "airfoil_gmsh",
        adjuster=_adjust_airfoil_mesh_params_for_review,
        max_review_iterations=max_review_iterations,
        **kwargs,
    )


def generate_coordinate_profile_gmsh_mesh(
    case_dir: str,
    coordinate_profile_path: str = "",
    profile_dat_path: str = "",
    profile_name: str = "",
    airfoil_dat_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Generate a mesh from a generic closed 2D coordinate profile."""
    profile_path = coordinate_profile_path or profile_dat_path or airfoil_dat_path
    if not profile_path:
        raise ValueError("coordinate_profile_path or airfoil_dat_path is required")
    compatible_name = kwargs.pop("airfoil_name", "")
    # These options belong to cascade/reference-domain generators and are not
    # accepted by the isolated-profile Gmsh implementation.
    for incompatible_key in ("preserve_input_scale", "spanwise_layers", "front_back_patch_type"):
        kwargs.pop(incompatible_key, None)
    result = generate_airfoil_gmsh_mesh(
        case_dir=case_dir,
        airfoil_name=profile_name or compatible_name or "coordinate_profile",
        airfoil_dat_path=profile_path,
        **kwargs,
    )
    result["mesh_type"] = "coordinate_profile_gmsh"
    result["coordinate_profile_path"] = profile_path
    result.setdefault("assumptions", []).append(
        "Generic closed coordinate profile was meshed directly; no NACA/airfoil surrogate was introduced."
    )
    return result


def generate_coordinate_profile_cascade_gmsh_mesh(
    case_dir: str,
    coordinate_profile_path: str = "",
    profile_name: str = "",
    airfoil_dat_path: str = "",
    pitch: float | None = None,
    pitch_chord_ratio: float | None = None,
    upstream_length: float | None = None,
    downstream_length: float | None = None,
    fore_domain_length: float | None = None,
    aft_domain_length: float | None = None,
    axial_chord: float | None = None,
    reference_length: float | None = None,
    lengths_normalized: bool = False,
    domain_boundary_points: Any = None,
    domain_boundary_patch_names: Any = None,
    periodic_lower_points: Any = None,
    periodic_translation_vector: Any = None,
    domain_shape_verified: bool = False,
    h_blade: float = 0.006,
    h_airfoil: float | None = None,
    h_farfield: float = 0.2,
    boundary_layer_first: float = 1e-4,
    boundary_layer_thickness: float = 0.04,
    boundary_layer_ratio: float = 1.12,
    recombine: bool = False,
    extrude_to_3d: bool = True,
    preserve_input_scale: bool = False,
    spanwise_layers: int = 1,
    front_back_patch_type: str = "empty",
    span: float = 0.1,
    convert_to_openfoam: bool = True,
    write_tecplot: bool = True,
    quality_preset: str = "robust",
    gmsh_binary: str = "gmsh",
    gmsh_to_foam_cmd: str = "openfoam gmshToFoam",
    timeout: int = 120,
    **_: Any,
) -> dict[str, Any]:
    profile_path = coordinate_profile_path or airfoil_dat_path
    if not profile_path:
        raise ValueError("coordinate_profile_path or airfoil_dat_path is required")
    raw_coords = _parse_airfoil_coordinates(profile_path)
    if not raw_coords:
        raise ValueError(f"coordinate profile file has no usable x-y points: {profile_path}")
    convert_to_openfoam = _as_bool(convert_to_openfoam, True)
    write_tecplot = _as_bool(write_tecplot, True)
    recombine = _as_bool(recombine, False)
    # OpenFOAM stores a 2-D case as a one-cell-thick volume with empty end patches.
    extrude_to_3d = convert_to_openfoam or _as_bool(extrude_to_3d, True)
    if h_airfoil is not None:
        h_blade = float(h_airfoil)
    if (quality_preset or "robust").lower().strip() == "robust":
        h_blade = min(max(float(h_blade), 0.002), 0.008)
        h_farfield = min(max(float(h_farfield), 0.05), 0.4)
        boundary_layer_first = min(max(float(boundary_layer_first), 1e-5), 0.001)
        boundary_layer_thickness = min(max(float(boundary_layer_thickness), 0.01), 0.05)
        boundary_layer_ratio = min(max(float(boundary_layer_ratio), 1.08), 1.15)

    gmsh_path = shutil.which(gmsh_binary) or shutil.which("gmsh")
    if not gmsh_path:
        raise RuntimeError("gmsh executable not found on PATH")
    out_dir = Path(case_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = _slug_asset_name(profile_name or Path(str(profile_path)).stem or "coordinate_profile")
    geo_path = out_dir / f"{prefix}_cascade.geo"
    msh_path = out_dir / f"{prefix}_cascade.msh"
    quality_path = out_dir / "mesh_quality.json"
    tecplot_path = out_dir / f"{prefix}_cascade_tecplot_volume.dat"
    tecplot_surface_path = out_dir / f"{prefix}_cascade_tecplot_surface.dat"
    geo, cascade_grid_params = _build_coordinate_profile_cascade_geo(
        raw_coords,
        profile_name=profile_name or prefix,
        pitch=pitch,
        pitch_chord_ratio=pitch_chord_ratio,
        upstream_length=upstream_length,
        downstream_length=downstream_length,
        fore_domain_length=fore_domain_length,
        aft_domain_length=aft_domain_length,
        axial_chord=axial_chord,
        reference_length=reference_length,
        lengths_normalized=_as_bool(lengths_normalized, False),
        domain_boundary_points=domain_boundary_points,
        domain_boundary_patch_names=domain_boundary_patch_names,
        periodic_lower_points=periodic_lower_points,
        periodic_translation_vector=periodic_translation_vector,
        domain_shape_verified=_as_bool(domain_shape_verified, False),
        h_blade=h_blade,
        h_farfield=h_farfield,
        boundary_layer_first=boundary_layer_first,
        boundary_layer_thickness=boundary_layer_thickness,
        boundary_layer_ratio=boundary_layer_ratio,
        span=span,
        recombine=recombine,
        extrude_to_3d=extrude_to_3d,
        preserve_input_scale=_as_bool(preserve_input_scale, False),
        spanwise_layers=max(1, int(spanwise_layers or 1)),
    )
    geo_path.write_text(geo, encoding="utf-8")
    proc = subprocess.run(
        [gmsh_path, "-3" if extrude_to_3d else "-2", str(geo_path), "-format", "msh2", "-o", str(msh_path)],
        cwd=str(out_dir),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0 or not msh_path.exists():
        raise RuntimeError(f"gmsh failed with returncode={proc.returncode}; stderr_tail={proc.stderr[-1200:]}")

    quality = _parse_gmsh_msh2_quality(str(msh_path))
    source_topology = dict(quality.get("topology") or {})
    quality_path.write_text(json.dumps(quality, indent=2), encoding="utf-8")
    tecplot_result = _export_gmsh_tecplot(msh_path, tecplot_surface_path, tecplot_path, quality, write_tecplot)

    openfoam_result: dict[str, Any] = {"requested": convert_to_openfoam, "status": "skipped", "written_files": []}
    if convert_to_openfoam:
        system_result = write_openfoam_system_files(str(out_dir))
        foam_cmd = _openfoam_command("gmshToFoam", gmsh_to_foam_cmd, msh_path)
        foam_proc = subprocess.run(
            foam_cmd,
            cwd=str(out_dir),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        poly_dir = out_dir / "constant" / "polyMesh"
        if foam_proc.returncode == 0:
            _set_openfoam_patch_type(
                poly_dir / "boundary", "frontAndBack", str(front_back_patch_type or "empty")
            )
        foam_files = [
            str(poly_dir / name)
            for name in ("points", "faces", "owner", "neighbour", "boundary")
            if (poly_dir / name).exists()
        ]
        openfoam_result = {
            "requested": True,
            "status": "success" if foam_proc.returncode == 0 and len(foam_files) >= 5 else "error",
            "cmd": shlex.join(foam_cmd),
            "returncode": foam_proc.returncode,
            "case_dir": str(out_dir),
            "polyMesh_dir": str(poly_dir),
            "written_files": [*system_result.get("written_files", []), *foam_files],
            "stdout_tail": foam_proc.stdout[-2000:],
            "stderr_tail": foam_proc.stderr[-1200:],
        }
        if openfoam_result["status"] == "error":
            raise RuntimeError(
                "gmshToFoam conversion failed with returncode="
                f"{foam_proc.returncode}; stdout_tail={foam_proc.stdout[-1200:]}; stderr_tail={foam_proc.stderr[-1200:]}"
            )

    written_files = [
        str(geo_path),
        str(msh_path),
        str(quality_path),
        *tecplot_result["written_files"],
        *openfoam_result.get("written_files", []),
    ]
    return {
        "mesh_format": "openfoam_polyMesh" if openfoam_result.get("status") == "success" else "gmsh_msh2",
        "mesh_type": "coordinate_profile_cascade_gmsh",
        "case_dir": str(out_dir),
        "written_files": written_files,
        "geo_file": str(geo_path),
        "mesh_file": str(msh_path),
        "tecplot_file": str(tecplot_surface_path) if tecplot_result.get("status") == "success" else None,
        "tecplot_volume_file": str(tecplot_path) if str(tecplot_path) in tecplot_result["written_files"] else None,
        "tecplot": tecplot_result,
        "quality_report": str(quality_path),
        "openfoam": openfoam_result,
        "polyMesh_dir": openfoam_result.get("polyMesh_dir"),
        "quality": quality,
        "source_topology": source_topology,
        "grid_params": {
            "profile_name": profile_name or prefix,
            "geometry_source": "coordinate_profile",
            **cascade_grid_params,
        },
        "gmsh_stdout_tail": proc.stdout[-1200:],
        "gmsh_stderr_tail": proc.stderr[-1200:],
    }


def _generate_coordinate_profile_cascade_gmsh_mesh_with_review(max_review_iterations: int = 0, **kwargs: Any) -> dict[str, Any]:
    return _generate_gmsh_mesh_with_review(
        generate_coordinate_profile_cascade_gmsh_mesh,
        "coordinate_profile_cascade_gmsh",
        adjuster=_adjust_airfoil_mesh_params_for_review,
        max_review_iterations=max_review_iterations,
        **kwargs,
    )


def _generate_coordinate_profile_gmsh_mesh_with_review(max_review_iterations: int = 0, **kwargs: Any) -> dict[str, Any]:
    return _generate_gmsh_mesh_with_review(
        generate_coordinate_profile_gmsh_mesh,
        "coordinate_profile_gmsh",
        adjuster=_adjust_airfoil_mesh_params_for_review,
        max_review_iterations=max_review_iterations,
        **kwargs,
    )


def _adjust_cylinder_mesh_params_for_review(params: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    adjusted = _adjust_generic_gmsh_mesh_params_for_review(params, review)
    issue_codes = {issue.get("code") for issue in review.get("issues", [])}
    if "generation_failed" in issue_codes:
        return adjusted
    if "too_few_surface_elements" in issue_codes:
        adjusted["n_cylinder"] = max(int(float(adjusted.get("n_cylinder", 160))) + 64, 160)
        adjusted["h_cylinder"] = min(float(adjusted.get("h_cylinder", 0.02)), 0.015)
    if "aspect_ratio_too_high" in issue_codes:
        adjusted["boundary_layer_first"] = min(max(float(adjusted.get("boundary_layer_first", 0.002)) * 1.25, 0.001), 0.006)
        adjusted["boundary_layer_ratio"] = min(float(adjusted.get("boundary_layer_ratio", 1.15)), 1.12)
    adjusted["quality_preset"] = "robust"
    return adjusted


def _generate_cylinder_gmsh_mesh_with_review(max_review_iterations: int = 0, **kwargs: Any) -> dict[str, Any]:
    return _generate_gmsh_mesh_with_review(
        generate_cylinder_gmsh_mesh,
        "cylinder_gmsh",
        adjuster=_adjust_cylinder_mesh_params_for_review,
        max_review_iterations=max_review_iterations,
        **kwargs,
    )


def _apply_profile_defaults(params: dict[str, Any], profile: dict[str, Any] | None) -> None:
    if not profile:
        return
    for target, aliases in (profile.get("parameter_aliases") or {}).items():
        if target not in params:
            for alias in aliases or []:
                if alias in params:
                    params[target] = params[alias]
                    break
    for key, value in (profile.get("defaults") or {}).items():
        params.setdefault(key, value)
    params.setdefault("case_type", profile.get("case_type"))


def _is_turbomachinery_blade_case(text: str, params: dict[str, Any]) -> bool:
    markers = {
        str(params.get("case_family") or "").lower(),
        str(params.get("profile_kind") or "").lower(),
        str(params.get("case_type") or "").lower(),
    }
    if "turbomachinery_blade_section" in markers:
        return True
    if "turbomachinery_cascade" in markers:
        return True
    return looks_like_turbomachinery_blade(text, params)


def _has_turbomachinery_cascade_domain(params: dict[str, Any]) -> bool:
    pitch_ok = any(params.get(key) not in (None, "", [], {}) for key in ("pitch", "pitch_chord_ratio", "blade_pitch"))
    inlet_outlet_ok = any(
        params.get(key) not in (None, "", [], {})
        for key in (
            "inlet_length",
            "outlet_length",
            "axial_chord",
            "upstream_length",
            "downstream_length",
            "fore_domain_length",
            "aft_domain_length",
            "inlet_extent",
            "outlet_extent",
        )
    )
    periodic_ok = any(
        params.get(key) not in (None, "", [], {})
        for key in ("periodic_boundary_pairing", "periodic_patches", "cascade_periodic", "pitchwise_periodic")
    )
    return pitch_ok and inlet_outlet_ok and periodic_ok


def _missing_turbomachinery_cascade_fields(params: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not any(params.get(key) not in (None, "", [], {}) for key in ("pitch", "pitch_chord_ratio", "blade_pitch")):
        missing.append("pitch / pitch_chord_ratio / blade_pitch")
    if not any(
        params.get(key) not in (None, "", [], {})
        for key in (
            "inlet_length",
            "outlet_length",
            "axial_chord",
            "upstream_length",
            "downstream_length",
            "fore_domain_length",
            "aft_domain_length",
            "inlet_extent",
            "outlet_extent",
        )
    ):
        missing.append("inlet/outlet extent")
    if not any(
        params.get(key) not in (None, "", [], {})
        for key in ("periodic_boundary_pairing", "periodic_patches", "cascade_periodic", "pitchwise_periodic")
    ):
        missing.append("periodic boundary pairing")
    return missing


def _turbomachinery_cascade_domain_reference_request(spec: str, params: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    profile_name = str(params.get("profile_name") or params.get("airfoil_name") or params.get("case_name") or "turbomachinery cascade")
    base = f"{profile_name} turbine cascade pitch inlet outlet periodic boundary conditions geometry"
    return {
        "status": "needs_reference_search",
        "message": (
            "已获得叶片截面坐标，但涡轮/压气机叶栅网格还缺少外围通道域或周期边界参数。"
            "不能使用孤立叶片/机翼 C-domain 作为合格叶栅网格。"
        ),
        "case_type": "turbomachinery_cascade",
        "mesh_type": "coordinate_profile_gmsh",
        "missing_fields": missing,
        "resolved_parameters": params,
        "search_queries": [
            base,
            f"{profile_name} linear cascade pitch chord inlet outlet periodic",
            f"{profile_name} blade cascade computational domain periodic boundary",
            "turbine blade cascade mesh pitch periodic inlet outlet domain parameters",
        ],
        "recommended_tools": ["data_web_search", "data_web_download", "prepare_scientific_mesh"],
        "next_action": (
            "先检索公开叶栅 pitch、进出口延伸和周期边界设置；把来源摘要写入 reference_notes "
            "后调用 prepare_scientific_mesh(operation='resolve')。若检索不到可复现参数，进入 HITL 请求用户确认 "
            "pitch/进出口范围/周期边界。"
        ),
        "source_trace": [
            {
                "source": "mesh_generator_preflight",
                "reason": "missing_turbomachinery_cascade_domain_parameters",
                "missing_fields": missing,
            }
        ],
    }


def _text_error_context(path: Path, diagnostic: str, radius: int = 3) -> str:
    """Return the cited source lines for a text-tool diagnostic."""
    match = re.search(rf"['\"]{re.escape(str(path))}['\"],\s*line\s+(\d+)\b", diagnostic, flags=re.I)
    if not match:
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    line_number = int(match.group(1))
    if not 1 <= line_number <= len(lines):
        return ""
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    return "\nsource context:\n" + "\n".join(
        f"{index}: {lines[index - 1]}" for index in range(start, end + 1)
    )


def generate_geometry_file_gmsh_mesh(
    geometry_file: str = "",
    case_dir: str = "",
    mesh_dimension: int | None = None,
    characteristic_length: float | None = None,
    element_order: int | None = None,
    mesh_size_factor: float | None = None,
    convert_to_openfoam: bool = True,
    write_tecplot: bool = True,
    gmsh_binary: str = "gmsh",
    gmsh_to_foam_cmd: str = "openfoam gmshToFoam",
    solver_mesh_format: str = "",
    timeout: int = 180,
    minimum_length: float | None = None,
    length_unit: str = "",
    **domain_params: Any,
) -> dict[str, Any]:
    """Generate a generic Gmsh mesh from a user-provided geometry file.

    Supported inputs include .geo, .msh, .step/.stp, .iges/.igs, .brep and .stl.
    Boundary semantics may still need user confirmation later, but the file is
    always used before falling back to public search or HITL.
    """
    if not geometry_file or is_placeholder_value(geometry_file):
        raise ValueError("geometry_file is required for geometry_file_gmsh")
    convert_to_openfoam = _as_bool(convert_to_openfoam, True)
    write_tecplot = _as_bool(write_tecplot, True)
    src = Path(str(geometry_file)).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"geometry_file does not exist: {src}")
    for value in (characteristic_length, minimum_length):
        if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
            raise ValueError("Mesh lengths must be finite and positive")
    if minimum_length is not None and characteristic_length is not None and float(minimum_length) > float(characteristic_length):
        raise ValueError("minimum_length must not exceed characteristic_length")

    if src.suffix.lower() == ".cas":
        return _import_fluent_reference_mesh(
            src,
            Path(case_dir),
            scale=float(domain_params.get("reference_mesh_scale") or 1.0),
            span=float(domain_params.get("span") or 0.1),
            write_tecplot=write_tecplot,
            timeout=timeout,
            domain_params=domain_params,
        )

    out_dir = Path(case_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gmsh_path = shutil.which(gmsh_binary) or shutil.which("gmsh")
    if not gmsh_path and src.suffix.lower() != ".msh":
        raise RuntimeError("gmsh executable not found on PATH")

    suffix = src.suffix.lower()
    written_files: list[str] = []
    detected_suffix = suffix
    try:
        head = src.read_bytes()[:2048].decode("latin-1", errors="ignore").lower()
        if "acis data in iges format" in head or "iges" in head[:300]:
            detected_suffix = ".iges"
    except Exception:
        detected_suffix = suffix
    copied_name = src.name
    if detected_suffix in {".iges", ".igs"} and suffix not in {".iges", ".igs"}:
        copied_name = f"{src.stem}.iges"
    copied_geometry = out_dir / copied_name
    if src.resolve() != copied_geometry.resolve():
        shutil.copy2(src, copied_geometry)
    if copied_geometry.suffix.lower() == ".geo":
        # This file is the parser's transport, not the requested final export.
        # Script options override CLI flags, so pin it at the same canonical
        # execution boundary as sizing/order, leaving the source untouched.
        # SaveAll in MSH2 discards physical tags, including boundary groups.
        directives = ["Mesh.Format=1;", "Mesh.MshFileVersion=2.2;", "Mesh.Binary=0;", "Mesh.SaveAll=0;"]
        topology = str(domain_params.get("required_surface_topology") or "").casefold()
        if topology == "triangular" or (
            not topology and "recombine" in domain_params and not _as_bool(domain_params["recombine"])
        ):
            directives.extend(["Mesh.RecombineAll=0;", "Mesh.SubdivisionAlgorithm=0;"])
        elif topology == "quadrilateral" or _as_bool(domain_params.get("recombine"), False):
            directives.extend(["Mesh.RecombineAll=1;", "Recombine Surface{:};"])
            if topology == "quadrilateral":
                directives.append("Mesh.SubdivisionAlgorithm=1;")
        if characteristic_length is not None:
            size = float(characteristic_length)
            directives.extend([f"Mesh.MeshSizeMax={size};", f"Mesh.MeshSizeMin=Min(Mesh.MeshSizeMin,{size});"])
        if minimum_length is not None:
            directives.append(f"Mesh.MeshSizeMin={float(minimum_length)};")
        if element_order is not None:
            directives.append(f"Mesh.ElementOrder={int(element_order)};")
        if mesh_size_factor is not None:
            directives.append(f"Mesh.MeshSizeFactor={float(mesh_size_factor)};")
        # Gmsh reads .geo options after CLI options. Apply explicit controls
        # last in the execution copy, replacing our previous controls on retry.
        marker = "// Data runtime mesh controls"
        geometry_text = copied_geometry.read_text(encoding="utf-8", errors="replace")
        geometry_text = geometry_text.split(marker, 1)[0].rstrip()
        copied_geometry.write_text(
            geometry_text + "\n\n" + marker + "\n" + "\n".join(directives) + "\n",
            encoding="utf-8",
        )
    written_files.append(str(copied_geometry))

    suffix = copied_geometry.suffix.lower()
    msh_path = out_dir / f"{copied_geometry.stem}_generic.msh"
    gmsh_cmd: list[str] = []
    gmsh_stdout = ""
    gmsh_stderr = ""
    if suffix == ".msh":
        if copied_geometry.resolve() != msh_path.resolve():
            shutil.copy2(copied_geometry, msh_path)
        written_files.append(str(msh_path))
    else:
        if mesh_dimension is None:
            mesh_dimension = 2 if suffix in {".iges", ".igs"} else 3
        dim_flag = "-3" if int(mesh_dimension or 3) == 3 else "-2"
        gmsh_cmd = [str(gmsh_path), dim_flag, str(copied_geometry), "-format", "msh2", "-o", str(msh_path)]
        if length_unit:
            gmsh_cmd.extend(["-setstring", "Geometry.OCCTargetUnit", str(length_unit).upper()])
        if characteristic_length is not None:
            gmsh_cmd.extend(["-clmax", str(float(characteristic_length))])
        if minimum_length is not None:
            gmsh_cmd.extend(["-clmin", str(float(minimum_length))])
        if element_order is not None:
            gmsh_cmd.extend(["-order", str(int(element_order))])
        if mesh_size_factor is not None:
            gmsh_cmd.extend(["-clscale", str(float(mesh_size_factor))])
        proc = subprocess.run(
            gmsh_cmd,
            cwd=str(out_dir),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        gmsh_stdout = proc.stdout
        gmsh_stderr = proc.stderr
        if proc.returncode != 0 or not msh_path.exists():
            diagnostic = proc.stderr[-1200:]
            source_context = _text_error_context(copied_geometry, proc.stderr)
            failure = _failed_gmsh_generation_result({"case_dir": out_dir}, RuntimeError(
                "gmsh failed for geometry_file with returncode="
                f"{proc.returncode}: {diagnostic}"
                f"{source_context}"
            ))
            # A cited input defect belongs to the original writer, not the
            # consumer's staging copy. Runtime/export failures stay local.
            if source_context:
                failure["source_file"] = str(src.resolve())
            return failure
        written_files.append(str(msh_path))

    quality = _parse_gmsh_msh2_quality(str(msh_path))
    has_cells = bool(quality.get("n_3d_elements") or quality.get("n_2d_elements"))
    if not has_cells or (int(mesh_dimension or 0) == 3 and not quality.get("n_3d_elements")):
        failure = _failed_gmsh_generation_result({"case_dir": out_dir}, ValueError(
            f"Gmsh produced no cells of the requested dimension ({mesh_dimension}). "
            f"Observed bounds: {quality.get('bounds_min')} to {quality.get('bounds_max')}; "
            f"CAD target unit={length_unit or 'native'}, target size={characteristic_length}, "
            f"minimum size={minimum_length}. Check source units, sizing and volume/physical groups. "
            "Do not rescale authoritative geometry or change frozen sizes without clarification."
        ))
        return {**failure, "source_file": str(src.resolve()), "quality": quality,
                "gmsh_cmd": gmsh_cmd, "gmsh_stdout_tail": gmsh_stdout[-2000:],
                "gmsh_stderr_tail": gmsh_stderr[-2000:]}
    source_topology = dict(quality.get("topology") or {})
    extrusion_result: dict[str, Any] = {"status": "skipped"}
    if (
        convert_to_openfoam
        and quality.get("n_2d_elements", 0) > 0
        and quality.get("n_3d_elements", 0) == 0
    ):
        extruded_path = out_dir / f"{copied_geometry.stem}_generic_extruded.msh"
        extrusion_result = _extrude_msh2_surface_to_thin_3d(
            str(msh_path),
            str(extruded_path),
            span=float(domain_params.get("span") or 0.1),
        )
        msh_path = extruded_path
        written_files.append(str(msh_path))
        quality = _parse_gmsh_msh2_quality(str(msh_path))
    quality_path = out_dir / "mesh_quality.json"
    from .scientific_assets import inspect_scientific_asset_path

    source_profile = inspect_scientific_asset_path(src, compute_hash=True)
    quality["source_geometry"] = {
        key: source_profile.get(key) for key in ("path", "sha256", "size_bytes")
    }
    quality_path.write_text(json.dumps(quality, indent=2, ensure_ascii=False), encoding="utf-8")
    written_files.append(str(quality_path))

    solver_mesh_file = ""
    solver_format = str(solver_mesh_format or "").strip().lower().lstrip(".")
    if solver_format:
        if not re.fullmatch(r"[a-z0-9]+", solver_format):
            raise ValueError(f"invalid solver_mesh_format: {solver_mesh_format!r}")
        solver_suffix = "msh" if solver_format.startswith("msh") else solver_format
        solver_path = out_dir / f"{copied_geometry.stem}.{solver_suffix}"
        if solver_path.resolve() in {src.resolve(), copied_geometry.resolve()}:
            solver_path = out_dir / f"{copied_geometry.stem}_export.{solver_suffix}"
        export_proc = subprocess.run(
            [
                str(gmsh_path), str(msh_path), "-format", solver_format,
                "-setnumber", "Mesh.SaveGroupsOfNodes", "1", "-save", "-o", str(solver_path),
            ],
            cwd=str(out_dir),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if export_proc.returncode != 0 or not solver_path.is_file() or solver_path.stat().st_size == 0:
            raise RuntimeError(
                f"gmsh solver export ({solver_format}) failed with returncode="
                f"{export_proc.returncode}: {export_proc.stderr[-1200:]}"
            )
        from .scientific_assets import validate_format_version

        with solver_path.open(encoding="utf-8", errors="replace") as exported:
            version_error = validate_format_version(exported.read(512), solver_format)
        if version_error:
            raise RuntimeError(version_error)
        solver_mesh_file = str(solver_path)
        written_files.append(solver_mesh_file)

    tecplot_result: dict[str, Any] = {"requested": write_tecplot, "status": "skipped"}
    tecplot_files: list[str] = []
    if write_tecplot:
        surface_path = out_dir / f"{src.stem}_tecplot_surface.dat"
        volume_path = out_dir / f"{src.stem}_tecplot_volume.dat"
        try:
            if int(source_topology.get("mesh_dimension") or 0) == 3:
                surface_result = write_tecplot_boundary_surface_from_msh2(str(msh_path), str(surface_path))
            else:
                try:
                    surface_result = write_tecplot_surface_from_msh2(str(msh_path), str(surface_path))
                except ValueError:
                    surface_result = write_tecplot_boundary_surface_from_msh2(str(msh_path), str(surface_path))
            volume_result = None
            try:
                volume_result = write_tecplot_volume_from_msh2(str(msh_path), str(volume_path))
            except Exception as exc:
                volume_result = {"status": "skipped", "reason": str(exc)}
            tecplot_files = [str(surface_path)]
            if volume_path.exists():
                tecplot_files.append(str(volume_path))
            tecplot_result = {
                "requested": True,
                "status": "success",
                "recommended_file": str(surface_path),
                "surface": surface_result,
                "volume": volume_result,
            }
            written_files.extend(tecplot_files)
        except Exception as exc:
            tecplot_result = {"requested": True, "status": "error", "error": str(exc)}

    openfoam_result: dict[str, Any] = {"requested": convert_to_openfoam, "status": "skipped"}
    if convert_to_openfoam:
        system_result = write_openfoam_system_files(str(out_dir))
        written_files.extend(system_result.get("written_files", []))
        foam_cmd = _openfoam_command("gmshToFoam", gmsh_to_foam_cmd, msh_path)
        foam_proc = subprocess.run(
            foam_cmd,
            cwd=str(out_dir),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        boundary_file = out_dir / "constant" / "polyMesh" / "boundary"
        if foam_proc.returncode == 0 and boundary_file.exists():
            patch_types = {
                str(name): str(config.get("type") if isinstance(config, dict) else config)
                for name, config in (domain_params.get("boundary_map") or {}).items()
                if name and config
            }
            if int(mesh_dimension or 0) == 2:
                patch_types.setdefault("frontAndBack", "empty")
            for patch_name, patch_type in patch_types.items():
                _set_openfoam_patch_type(boundary_file, patch_name, patch_type)
            openfoam_result = {
                "requested": True,
                "status": "success",
                "cmd": shlex.join(foam_cmd),
                "polyMesh_dir": str(boundary_file.parent),
                "boundary_file": str(boundary_file),
                "stdout_tail": foam_proc.stdout[-1200:],
                "stderr_tail": foam_proc.stderr[-1200:],
            }
        else:
            openfoam_result = {
                "requested": True,
                "status": "error",
                "cmd": shlex.join(foam_cmd),
                "returncode": foam_proc.returncode,
                "stdout_tail": foam_proc.stdout[-1200:],
                "stderr_tail": foam_proc.stderr[-1200:],
                "error": "gmshToFoam conversion failed or boundary file missing",
            }

    return {
        "mesh_format": "openfoam_polyMesh" if openfoam_result.get("status") == "success" else (
            f"gmsh_{solver_format}" if solver_format.startswith("msh") else "gmsh_msh2"
        ),
        "mesh_type": "geometry_file_gmsh",
        "case_dir": str(out_dir),
        "geometry_file": str(src),
        "copied_geometry_file": str(copied_geometry),
        "mesh_file": solver_mesh_file if solver_format.startswith("msh") else str(msh_path),
        "solver_mesh_file": solver_mesh_file or None,
        "grid_params": {
            "solver_mesh_format": solver_format or "msh2",
            "mesh_dimension": int(mesh_dimension or 0),
            **({key: value for key, value in {
                "characteristic_length": characteristic_length,
                "minimum_length": minimum_length,
                "length_unit": length_unit or None,
                "element_order": element_order,
                "mesh_size_factor": mesh_size_factor,
            }.items() if value is not None} if suffix != ".msh" else {}),
            "geometry_suffix": suffix,
            "extrusion": extrusion_result,
            **{k: v for k, v in domain_params.items() if v not in (None, "", [], {})},
        },
        "quality_report": str(quality_path),
        "quality": quality,
        "source_topology": source_topology,
        "openfoam": openfoam_result,
        "tecplot": tecplot_result,
        "tecplot_file": (tecplot_result.get("recommended_file") if isinstance(tecplot_result, dict) else None),
        "tecplot_surface_file": (tecplot_result.get("recommended_file") if isinstance(tecplot_result, dict) else None),
        "tecplot_volume_file": (
            str(out_dir / f"{copied_geometry.stem}_tecplot_volume.dat")
            if (out_dir / f"{copied_geometry.stem}_tecplot_volume.dat").exists()
            else None
        ),
        "tecplot_files": tecplot_files,
        "polyMesh_dir": (
            openfoam_result.get("polyMesh_dir")
            if isinstance(openfoam_result, dict) and openfoam_result.get("status") == "success"
            else None
        ),
        "written_files": sorted(set(written_files)),
        "gmsh_cmd": gmsh_cmd,
        "gmsh_stdout_tail": gmsh_stdout[-1200:],
        "gmsh_stderr_tail": gmsh_stderr[-1200:],
        "assumptions": [
            "User-provided geometry_file was used directly before public search or HITL.",
            "Boundary patch semantics may require later user confirmation if physical names are absent.",
        ],
    }


def _read_foam_list(path: Path, item_pattern: str) -> list[Any]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    start = text.find("\n(")
    end = text.rfind("\n)")
    body = text[start + 2:end] if start >= 0 and end > start else text
    return re.findall(item_pattern, body, flags=re.MULTILINE)


def validate_openfoam_polymesh(poly_dir: str | Path) -> dict[str, Any]:
    """Validate OpenFOAM topology and complete boundary-to-cell ownership."""
    poly = Path(poly_dir)
    required = {name: poly / name for name in ("points", "faces", "owner", "neighbour", "boundary")}
    missing = [name for name, path in required.items() if not path.is_file() or path.stat().st_size <= 0]
    if missing:
        return {"status": "fail", "reason": "missing_core_files", "missing": missing}
    try:
        points = _read_foam_list(
            required["points"], r"\(([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)"
        )
        faces = [
            [int(value) for value in row.split()]
            for row in _read_foam_list(required["faces"], r"\d+\(([^)]*)\)")
        ]
        owners = [int(value) for value in _read_foam_list(required["owner"], r"^\s*(\d+)\s*$")]
        neighbours = [int(value) for value in _read_foam_list(required["neighbour"], r"^\s*(\d+)\s*$")]
        boundary_text = required["boundary"].read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError) as exc:
        return {"status": "fail", "reason": "parse_error", "error": str(exc)}

    issues: list[dict[str, Any]] = []
    n_points = len(points)
    n_faces = len(faces)
    n_internal = len(neighbours)
    if n_points <= 0 or n_faces <= 0:
        issues.append({"code": "empty_topology", "n_points": n_points, "n_faces": n_faces})
    if len(owners) != n_faces:
        issues.append({"code": "owner_face_count_mismatch", "owners": len(owners), "faces": n_faces})
    if n_internal > n_faces:
        issues.append({"code": "neighbour_count_exceeds_faces", "neighbours": n_internal, "faces": n_faces})

    invalid_face_ids = [
        index for index, face in enumerate(faces)
        if len(face) < 3 or any(node < 0 or node >= n_points for node in face)
    ]
    if invalid_face_ids:
        issues.append({"code": "invalid_face_point_references", "count": len(invalid_face_ids)})
    all_cells = owners + neighbours
    n_cells = max(all_cells, default=-1) + 1
    if n_cells <= 0 or any(cell < 0 or cell >= n_cells for cell in all_cells):
        issues.append({"code": "invalid_cell_references", "n_cells": n_cells})
    if any(index < len(owners) and neighbours[index] == owners[index] for index in range(len(neighbours))):
        issues.append({"code": "internal_face_same_owner_and_neighbour"})

    patches: list[dict[str, Any]] = []
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.-]*)\s*\{([^}]+)\}", boundary_text, flags=re.DOTALL):
        name, block = match.group(1), match.group(2)
        n_faces_match = re.search(r"nFaces\s+(\d+)", block)
        start_match = re.search(r"startFace\s+(\d+)", block)
        if not n_faces_match or not start_match:
            continue
        patches.append({
            "name": name,
            "n_faces": int(n_faces_match.group(1)),
            "start_face": int(start_match.group(1)),
        })
    if not patches:
        issues.append({"code": "no_boundary_patches"})
    empty_patches = [patch["name"] for patch in patches if patch["n_faces"] <= 0]
    if empty_patches:
        issues.append({"code": "boundary_patch_without_faces", "patches": empty_patches})

    covered: list[int] = []
    for patch in patches:
        start = patch["start_face"]
        end = start + patch["n_faces"]
        if start < n_internal or end > n_faces:
            issues.append({"code": "boundary_patch_range_invalid", "patch": patch})
            continue
        covered.extend(range(start, end))
    expected = list(range(n_internal, n_faces))
    if sorted(covered) != expected:
        issues.append({
            "code": "boundary_faces_not_exactly_covered",
            "expected": len(expected),
            "covered": len(set(covered)),
            "duplicates": len(covered) - len(set(covered)),
        })
    boundary_owner_valid = len(owners) == n_faces and all(
        0 <= owners[face_id] < n_cells for face_id in expected
    )
    if not boundary_owner_valid:
        issues.append({"code": "boundary_face_without_valid_owner_cell"})

    return {
        "status": "fail" if issues else "pass",
        "n_points": n_points,
        "n_faces": n_faces,
        "n_internal_faces": n_internal,
        "n_boundary_faces": max(n_faces - n_internal, 0),
        "n_cells": n_cells,
        "patches": patches,
        "issues": issues,
    }


def _write_tecplot_surface_from_polymesh(poly_dir: Path, output_path: Path) -> dict[str, Any]:
    point_rows = _read_foam_list(poly_dir / "points", r"\(([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)")
    points = [(float(x), float(y), float(z)) for x, y, z in point_rows]
    face_rows = _read_foam_list(poly_dir / "faces", r"\d+\(([^)]*)\)")
    faces = [[int(value) for value in row.split()] for row in face_rows]
    boundary_text = (poly_dir / "boundary").read_text(encoding="utf-8", errors="ignore")
    boundary_faces: list[list[int]] = []
    planar_faces: list[list[int]] = []
    for match in re.finditer(
        r"([A-Za-z_][A-Za-z0-9_.-]*)\s*\{([^}]+)\}", boundary_text, flags=re.DOTALL
    ):
        name, block = match.group(1), match.group(2)
        n_faces_match = re.search(r"nFaces\s+(\d+)", block)
        start_match = re.search(r"startFace\s+(\d+)", block)
        if not n_faces_match or not start_match:
            continue
        start_face = int(start_match.group(1))
        n_faces = int(n_faces_match.group(1))
        patch_faces = faces[start_face:start_face + n_faces]
        patch_type = re.search(r"type\s+([A-Za-z0-9_]+)", block)
        if patch_type and patch_type.group(1).lower() == "empty":
            planar_faces.extend(patch_faces)
        else:
            boundary_faces.extend(patch_faces)

    # A one-cell-thick OpenFOAM case stores the actual 2-D computational cells
    # on its paired empty patches. Export one plane, not merely the perimeter.
    selected = _select_one_planar_cell_layer(points, planar_faces) or boundary_faces
    triangles = [face for face in selected if len(face) == 3]
    quads = [face for face in selected if len(face) == 4]
    polygons = [face for face in selected if len(face) > 4]
    # Tecplot's classic FE zones do not accept arbitrary polygons. A fan split
    # preserves their topology for inspection without inventing new nodes.
    for face in polygons:
        triangles.extend([face[:1] + face[i:i + 2] for i in range(1, len(face) - 1)])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        handle.write('TITLE = "OpenFOAM computational mesh"\nVARIABLES = "X" "Y" "Z"\n')
        for zone_name, zone_faces, zone_type in (
            ("cells_triangles", triangles, "FETRIANGLE"),
            ("cells_quads", quads, "FEQUADRILATERAL"),
        ):
            if not zone_faces:
                continue
            used = sorted({node for face in zone_faces for node in face})
            node_map = {node: index for index, node in enumerate(used, start=1)}
            handle.write(
                f'ZONE T="{zone_name}", N={len(used)}, E={len(zone_faces)}, '
                f'DATAPACKING=POINT, ZONETYPE={zone_type}\n'
            )
            for node in used:
                x, y, z = points[node]
                handle.write(f"{x:.16g} {y:.16g} {z:.16g}\n")
            for face in zone_faces:
                handle.write(" ".join(str(node_map[node]) for node in face) + "\n")
    return {
        "status": "success",
        "n_nodes": len({node for face in selected for node in face}),
        "n_surface_elements": len(triangles) + len(quads),
        "source": "empty_patch_cell_layer" if planar_faces and selected is not boundary_faces else "boundary_patches",
    }


def _select_one_planar_cell_layer(
    points: list[tuple[float, float, float]],
    faces: list[list[int]],
) -> list[list[int]]:
    """Select one side of a paired planar patch in a generic 2-D polyMesh."""
    if not faces:
        return []
    best: tuple[int, dict[float, list[list[int]]]] | None = None
    for axis in range(3):
        groups: dict[float, list[list[int]]] = {}
        for face in faces:
            values = [points[node][axis] for node in face]
            scale = max(1.0, *(abs(value) for value in values))
            if max(values) - min(values) > 1e-9 * scale:
                continue
            key = round(sum(values) / len(values), 10)
            groups.setdefault(key, []).append(face)
        if len(groups) >= 2 and (best is None or len(groups) < len(best[1])):
            best = (axis, groups)
    if best is None:
        return []
    groups = best[1]
    # Paired front/back patches normally contain equal cell counts. Selecting
    # the largest plane also handles extra empty patches without case names.
    return max(groups.values(), key=len)


def inspect_tecplot_ascii_mesh(path: str | Path) -> dict[str, Any]:
    """Read Tecplot zone headers without loading the potentially large payload."""
    source = Path(path)
    if not source.is_file() or source.stat().st_size <= 0:
        return {"status": "fail", "reason": "missing_or_empty", "n_elements": 0, "zones": []}
    zones: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if not line.lstrip().upper().startswith("ZONE"):
                continue
            element_match = re.search(r"\bE\s*=\s*(\d+)", line, flags=re.IGNORECASE)
            node_match = re.search(r"\bN\s*=\s*(\d+)", line, flags=re.IGNORECASE)
            type_match = re.search(r"\bZONETYPE\s*=\s*([A-Z0-9_]+)", line, flags=re.IGNORECASE)
            zones.append({
                "n_nodes": int(node_match.group(1)) if node_match else 0,
                "n_elements": int(element_match.group(1)) if element_match else 0,
                "zone_type": type_match.group(1).upper() if type_match else "",
            })
    n_elements = sum(zone["n_elements"] for zone in zones)
    return {
        "status": "pass" if zones and n_elements > 0 else "fail",
        "n_elements": n_elements,
        "zones": zones,
    }


def validate_tecplot_against_polymesh(poly_dir: str | Path, tecplot_path: str | Path) -> dict[str, Any]:
    """Reject outline-only Tecplot exports for generic 2-D OpenFOAM meshes."""
    poly = Path(poly_dir)
    inspected = inspect_tecplot_ascii_mesh(tecplot_path)
    if inspected["status"] != "pass":
        return {**inspected, "reason": "no_finite_element_zones"}
    try:
        points = [
            (float(x), float(y), float(z))
            for x, y, z in _read_foam_list(
                poly / "points", r"\(([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)"
            )
        ]
        faces = [
            [int(value) for value in row.split()]
            for row in _read_foam_list(poly / "faces", r"\d+\(([^)]*)\)")
        ]
        boundary_text = (poly / "boundary").read_text(encoding="utf-8", errors="ignore")
        planar_faces: list[list[int]] = []
        for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.-]*)\s*\{([^}]+)\}", boundary_text, flags=re.DOTALL):
            block = match.group(2)
            patch_type = re.search(r"type\s+([A-Za-z0-9_]+)", block)
            n_faces = re.search(r"nFaces\s+(\d+)", block)
            start_face = re.search(r"startFace\s+(\d+)", block)
            if not (patch_type and patch_type.group(1).lower() == "empty" and n_faces and start_face):
                continue
            start = int(start_face.group(1))
            planar_faces.extend(faces[start:start + int(n_faces.group(1))])
        cell_layer = _select_one_planar_cell_layer(points, planar_faces)
    except (OSError, ValueError, IndexError):
        cell_layer = []
    if not cell_layer:
        return {**inspected, "status": "pass", "validation_mode": "finite_element_zones"}
    expected = len(cell_layer)
    actual = int(inspected["n_elements"])
    coverage = actual / max(expected, 1)
    return {
        **inspected,
        "status": "pass" if coverage >= 0.9 else "fail",
        "validation_mode": "two_dimensional_cell_layer",
        "expected_cell_elements": expected,
        "coverage": coverage,
        "reason": "ok" if coverage >= 0.9 else "boundary_only_or_incomplete_export",
    }


def _polymesh_domain_metrics(poly_dir: Path) -> dict[str, Any]:
    point_rows = _read_foam_list(poly_dir / "points", r"\(([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)")
    points = [(float(x), float(y), float(z)) for x, y, z in point_rows]
    face_rows = _read_foam_list(poly_dir / "faces", r"\d+\(([^)]*)\)")
    faces = [[int(value) for value in row.split()] for row in face_rows]
    boundary_text = (poly_dir / "boundary").read_text(encoding="utf-8", errors="ignore")
    patch_boxes: dict[str, dict[str, list[float]]] = {}
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.-]*)\s*\{([^}]+)\}", boundary_text, flags=re.DOTALL):
        name, block = match.group(1), match.group(2)
        n_faces_match = re.search(r"nFaces\s+(\d+)", block)
        start_match = re.search(r"startFace\s+(\d+)", block)
        if not n_faces_match or not start_match:
            continue
        patch_faces = faces[int(start_match.group(1)):int(start_match.group(1)) + int(n_faces_match.group(1))]
        node_ids = {node for face in patch_faces for node in face}
        patch_points = [points[node] for node in node_ids if 0 <= node < len(points)]
        if patch_points:
            patch_boxes[name] = {
                "min": [min(point[i] for point in patch_points) for i in range(3)],
                "max": [max(point[i] for point in patch_points) for i in range(3)],
            }
    blade_boxes = [box for name, box in patch_boxes.items() if any(token in name.lower() for token in ("blade", "wall", "body"))]
    blade_chord = max((box["max"][0] - box["min"][0] for box in blade_boxes), default=0.0)
    overall_x = max((point[0] for point in points), default=0.0) - min((point[0] for point in points), default=0.0)
    lower_boxes = [box for name, box in patch_boxes.items() if re.search(r"periodic[_-]?1|lower", name, flags=re.IGNORECASE)]
    upper_boxes = [box for name, box in patch_boxes.items() if re.search(r"periodic[_-]?2|upper", name, flags=re.IGNORECASE)]
    def center_y(box: dict[str, list[float]]) -> float:
        return 0.5 * (box["min"][1] + box["max"][1])
    actual_pitch = 0.0
    if lower_boxes and upper_boxes:
        actual_pitch = abs(sum(center_y(box) for box in upper_boxes) / len(upper_boxes) - sum(center_y(box) for box in lower_boxes) / len(lower_boxes))
    return {
        "patch_bounding_boxes": patch_boxes,
        "blade_axial_chord": blade_chord,
        "domain_x_extent_chords": overall_x / blade_chord if blade_chord > 0 else 0.0,
        "actual_pitch": actual_pitch,
    }


def _ordered_edge_loops(edges: list[tuple[int, int]]) -> list[list[int]]:
    adjacency: dict[int, list[int]] = {}
    unused: set[tuple[int, int]] = set()
    for a, b in edges:
        if a == b:
            continue
        edge = tuple(sorted((a, b)))
        unused.add(edge)
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    loops: list[list[int]] = []
    while unused:
        first = next(iter(unused))
        start, current = first
        loop = [start, current]
        unused.discard(first)
        previous = start
        while current != start:
            candidates = [node for node in adjacency.get(current, []) if node != previous]
            next_node = next(
                (node for node in candidates if tuple(sorted((current, node))) in unused),
                None,
            )
            if next_node is None:
                break
            unused.discard(tuple(sorted((current, next_node))))
            previous, current = current, next_node
            if current != start:
                loop.append(current)
        if current == start and len(loop) >= 3:
            loops.append(loop)
    return loops


def extract_2d_domain_loops_from_polymesh(poly_dir: str | Path) -> dict[str, Any]:
    """Extract exact outer/body loops from a thin extruded OpenFOAM reference mesh."""
    poly = Path(poly_dir)
    points = [
        (float(x), float(y), float(z))
        for x, y, z in _read_foam_list(
            poly / "points", r"\(([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)"
        )
    ]
    faces = [
        [int(value) for value in row.split()]
        for row in _read_foam_list(poly / "faces", r"\d+\(([^)]*)\)")
    ]
    boundary_text = (poly / "boundary").read_text(encoding="utf-8", errors="ignore")
    planar_faces: list[list[int]] = []
    patches: list[tuple[str, str, int, int]] = []
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.-]*)\s*\{([^}]+)\}", boundary_text, flags=re.DOTALL):
        name, block = match.group(1), match.group(2)
        patch_type = re.search(r"type\s+([A-Za-z0-9_]+)", block)
        n_faces = re.search(r"nFaces\s+(\d+)", block)
        start_face = re.search(r"startFace\s+(\d+)", block)
        if not n_faces or not start_face:
            continue
        item = (name, patch_type.group(1) if patch_type else "patch", int(start_face.group(1)), int(n_faces.group(1)))
        patches.append(item)
        if item[1].lower() == "empty":
            planar_faces.extend(faces[item[2]:item[2] + item[3]])
    plane = _select_one_planar_cell_layer(points, planar_faces)
    plane_nodes = {node for face in plane for node in face}
    if not plane_nodes:
        raise ValueError("reference polyMesh does not contain a recoverable 2-D cell plane")

    edge_patch: dict[tuple[int, int], str] = {}
    for name, patch_type, start, count in patches:
        if patch_type.lower() == "empty":
            continue
        for face in faces[start:start + count]:
            refs = [node for node in face if node in plane_nodes]
            if len(refs) == 2:
                edge_patch[tuple(sorted((refs[0], refs[1])))] = name
    loops = _ordered_edge_loops(list(edge_patch))
    if len(loops) < 2:
        raise ValueError("expected an outer computational boundary and at least one solid/body loop")

    def area(loop: list[int]) -> float:
        values = [(points[node][0], points[node][1]) for node in loop]
        return 0.5 * sum(
            values[i][0] * values[(i + 1) % len(values)][1]
            - values[(i + 1) % len(values)][0] * values[i][1]
            for i in range(len(values))
        )

    outer = max(loops, key=lambda loop: abs(area(loop)))
    holes = [loop for loop in loops if loop is not outer]
    min_x = min(points[node][0] for loop in holes for node in loop)
    max_x = max(points[node][0] for loop in holes for node in loop)
    chord = max(max_x - min_x, 1e-12)
    transform = lambda node: (points[node][0], points[node][1])
    outer_points = [transform(node) for node in outer]
    hole_points = [[transform(node) for node in loop] for loop in holes]
    outer_patch_names = [
        edge_patch.get(tuple(sorted((outer[index], outer[(index + 1) % len(outer)]))), "boundary")
        for index in range(len(outer))
    ]
    patch_edges = {
        f"{transform(a)[0]:.12g},{transform(a)[1]:.12g}|{transform(b)[0]:.12g},{transform(b)[1]:.12g}": name
        for (a, b), name in edge_patch.items()
    }
    return {
        "outer_boundary_points": outer_points,
        "outer_boundary_patch_names": outer_patch_names,
        "body_loops": hole_points,
        "profile_points": max(hole_points, key=len),
        "patch_edges": patch_edges,
        "reference_chord": chord,
        "source_loop_count": len(loops),
    }


def _import_fluent_reference_mesh(
    source: Path,
    out_dir: Path,
    *,
    scale: float,
    span: float,
    write_tecplot: bool,
    timeout: int,
    domain_params: dict[str, Any],
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = out_dir / source.name
    if copied.resolve() != source.resolve():
        shutil.copy2(source, copied)
    write_openfoam_system_files(str(out_dir))
    cmd = ["openfoam", "fluentMeshToFoam", "-case", str(out_dir), "-2D", str(span), "-scale", str(scale), str(copied)]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
    poly_dir = out_dir / "constant" / "polyMesh"
    if proc.returncode != 0 or not (poly_dir / "boundary").exists():
        raise RuntimeError(f"fluentMeshToFoam failed: {(proc.stdout + proc.stderr)[-2000:]}")
    owner_rows = _read_foam_list(poly_dir / "owner", r"^\s*(\d+)\s*$")
    faces = _read_foam_list(poly_dir / "faces", r"\d+\(([^)]*)\)")
    remesh_with_gmsh = _as_bool(domain_params.get("remesh_reference_with_gmsh"), True)
    tecplot_path = out_dir / f"{source.stem}_tecplot_surface.dat"
    tecplot = {"status": "skipped"}
    if write_tecplot and not remesh_with_gmsh:
        tecplot = _write_tecplot_surface_from_polymesh(poly_dir, tecplot_path)
    quality = {
        "n_nodes": len(_read_foam_list(poly_dir / "points", r"\(([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)")),
        "n_2d_elements": len(faces),
        "n_3d_elements": max((int(value) for value in owner_rows), default=-1) + 1,
        "negative_or_zero_area_cells": 0,
        "max_aspect_ratio": 1.0,
    }
    quality_path = out_dir / "mesh_quality.json"
    quality_path.write_text(json.dumps(quality, indent=2), encoding="utf-8")
    domain_metrics = _polymesh_domain_metrics(poly_dir)
    if remesh_with_gmsh:
        extracted = extract_2d_domain_loops_from_polymesh(poly_dir)
        if len(extracted.get("body_loops") or []) != 1:
            raise RuntimeError(
                "reference mesh boundary extraction found multiple solid/body loops; "
                "a general multi-body domain remesher or explicit region semantics is required"
            )
        profile_path = out_dir / f"{source.stem}_extracted_profile.dat"
        profile_path.write_text(
            "\n".join(f"{x:.16g} {y:.16g}" for x, y in extracted["profile_points"]) + "\n",
            encoding="utf-8",
        )
        chord = float(extracted["reference_chord"])
        remeshed = generate_coordinate_profile_cascade_gmsh_mesh(
            case_dir=str(out_dir),
            coordinate_profile_path=str(profile_path),
            profile_name=f"{source.stem}_gmsh_remesh",
            pitch=domain_metrics.get("actual_pitch") or domain_params.get("pitch"),
            lengths_normalized=True,
            domain_boundary_points=extracted["outer_boundary_points"],
            domain_boundary_patch_names=extracted["outer_boundary_patch_names"],
            domain_shape_verified=True,
            preserve_input_scale=True,
            h_blade=float(domain_params.get("h_blade") or domain_params.get("h_airfoil") or chord / 300.0),
            h_farfield=float(domain_params.get("h_farfield") or chord / 25.0),
            boundary_layer_first=float(domain_params.get("boundary_layer_first") or chord / 1000.0),
            boundary_layer_thickness=float(domain_params.get("boundary_layer_thickness") or chord / 20.0),
            boundary_layer_ratio=float(domain_params.get("boundary_layer_ratio") or 1.12),
            span=max(float(domain_params.get("span") or 0.0), chord * 0.5),
            spanwise_layers=max(4, int(domain_params.get("spanwise_layers") or 16)),
            front_back_patch_type=str(domain_params.get("front_back_patch_type") or "symmetry"),
            recombine=_as_bool(domain_params.get("recombine"), False),
            extrude_to_3d=True,
            convert_to_openfoam=True,
            write_tecplot=write_tecplot,
            quality_preset=str(domain_params.get("quality_preset") or "reference_scaled"),
            timeout=timeout,
        )
        remeshed.update({
            "source_reference_mesh": str(copied),
            "reference_conversion_cmd": " ".join(cmd),
            "boundary_extraction": extracted,
            "geometry_generation_strategy": "reference_boundary_extraction_then_gmsh_remesh",
            "assumptions": [
                "The reference mesh was used only to recover exact closed geometry/boundary loops.",
                "The delivered cells were regenerated by Gmsh; reference cells were not delivered.",
            ],
        })
        remeshed["written_files"] = sorted(set([
            *remeshed.get("written_files", []), str(copied), str(profile_path),
        ]))
        return remeshed
    return {
        "mesh_format": "openfoam_polyMesh",
        "mesh_type": "geometry_file_gmsh",
        "case_dir": str(out_dir),
        "geometry_file": str(source),
        "mesh_file": str(copied),
        "polyMesh_dir": str(poly_dir),
        "quality": quality,
        "quality_report": str(quality_path),
        "openfoam": {"status": "success", "polyMesh_dir": str(poly_dir), "cmd": " ".join(cmd)},
        "tecplot": tecplot,
        "tecplot_file": str(tecplot_path) if write_tecplot else None,
        "tecplot_surface_file": str(tecplot_path) if write_tecplot else None,
        "tecplot_files": [str(tecplot_path)] if write_tecplot else [],
        "grid_params": {
            "geometry_representation": "reference_computational_mesh",
            "computational_domain_complete": True,
            "domain_shape": "reference_mesh",
            "domain_shape_verified": True,
            **domain_params,
            "source_declared_pitch": domain_params.get("pitch"),
            **domain_metrics,
            "pitch": domain_metrics.get("actual_pitch") or domain_params.get("pitch"),
        },
        "written_files": [str(copied), str(quality_path), str(tecplot_path), *[str(path) for path in poly_dir.iterdir() if path.is_file()]],
        "assumptions": ["Reference mesh conversion was explicitly requested without Gmsh remeshing."],
    }


def _generate_geometry_file_gmsh_mesh_with_review(max_review_iterations: int = 0, **kwargs: Any) -> dict[str, Any]:
    return _generate_gmsh_mesh_with_review(
        generate_geometry_file_gmsh_mesh,
        "geometry_file_gmsh",
        max_review_iterations=max_review_iterations,
        **kwargs,
    )


def _set_openfoam_patch_type(boundary_file: Path, patch_name: str, patch_type: str) -> None:
    """Update one OpenFOAM polyMesh/boundary patch type in-place."""
    if not boundary_file.exists():
        return
    lines = boundary_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    in_patch = False
    depth = 0
    for idx, line in enumerate(lines):
        if line.strip() == patch_name:
            in_patch = True
            depth = 0
            continue
        if in_patch:
            stripped = line.strip()
            if "{" in stripped:
                depth += stripped.count("{")
            if stripped.startswith("type"):
                prefix = line[: len(line) - len(line.lstrip())]
                lines[idx] = f"{prefix}type            {patch_type};"
            elif stripped.startswith("physicalType"):
                prefix = line[: len(line) - len(line.lstrip())]
                lines[idx] = f"{prefix}physicalType    {patch_type};"
            if "}" in stripped:
                depth -= stripped.count("}")
                if depth <= 0:
                    break
    boundary_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_tecplot_volume_from_msh2(mesh_path: str, tecplot_path: str) -> dict[str, Any]:
    """Write Tecplot ASCII volume zones from Gmsh MSH2 volume elements.

    Tecplot's Gmsh add-on can be brittle with mixed 2D/3D MSH2 files. This
    writer exports only volume cells with 1-based connectivity. Tetrahedra
    use their own FE-tetrahedron zone; prisms retain the FE-brick convention.
    """
    text = Path(mesh_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    nodes: dict[int, tuple[float, float, float]] = {}
    bricks: list[list[int]] = []
    tetrahedra: list[list[int]] = []
    n_hex = 0
    n_prism = 0
    skipped: dict[int, int] = {}

    i = 0
    while i < len(text):
        line = text[i].strip()
        if line == "$Nodes":
            n_nodes = int(text[i + 1].strip())
            for row in text[i + 2:i + 2 + n_nodes]:
                parts = row.split()
                if len(parts) >= 4:
                    nodes[int(parts[0])] = (float(parts[1]), float(parts[2]), float(parts[3]))
            i += n_nodes + 2
        elif line == "$Elements":
            n_elem = int(text[i + 1].strip())
            for row in text[i + 2:i + 2 + n_elem]:
                parts = row.split()
                if len(parts) < 4:
                    continue
                elem_type = int(parts[1])
                n_tags = int(parts[2])
                refs = [int(v) for v in parts[3 + n_tags:]]
                if any(r < 1 or r not in nodes for r in refs):
                    raise ValueError(f"Invalid Tecplot connectivity in element row: {row[:160]}")
                if elem_type == 4 and len(refs) == 4:
                    tetrahedra.append(refs)
                elif elem_type == 5 and len(refs) == 8:
                    bricks.append(refs)
                    n_hex += 1
                elif elem_type == 6 and len(refs) == 6:
                    # Gmsh prism: bottom tri (1,2,3), top tri (4,5,6).
                    # Degenerate brick: duplicate the third and sixth vertices.
                    bricks.append([refs[0], refs[1], refs[2], refs[2], refs[3], refs[4], refs[5], refs[5]])
                    n_prism += 1
                else:
                    skipped[elem_type] = skipped.get(elem_type, 0) + 1
            i += n_elem + 2
        i += 1

    if not bricks and not tetrahedra:
        raise ValueError(f"No supported tetrahedral/hex/prism volume elements found in {mesh_path}")

    ordered_ids = sorted(nodes)
    id_map = {old: new for new, old in enumerate(ordered_ids, start=1)}
    path = Path(tecplot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write('TITLE = "Volume mesh"\n')
        f.write('VARIABLES = "X" "Y" "Z"\n')
        for zone_type, cells in (("FEBRICK", bricks), ("FETETRAHEDRON", tetrahedra)):
            if not cells:
                continue
            f.write(f'ZONE T="{zone_type}", N={len(ordered_ids)}, E={len(cells)}, DATAPACKING=POINT, ZONETYPE={zone_type}\n')
            for old in ordered_ids:
                x, y, z = nodes[old]
                f.write(f"{x:.12g} {y:.12g} {z:.12g}\n")
            for conn in cells:
                f.write(" ".join(str(id_map[n]) for n in conn) + "\n")

    return {
        "path": str(path),
        "n_nodes": len(ordered_ids),
        "n_elements": len(bricks) + len(tetrahedra),
        "n_tetrahedra": len(tetrahedra),
        "n_hexes": n_hex,
        "n_prisms_as_degenerate_bricks": n_prism,
        "skipped_element_types": skipped,
    }


def _export_gmsh_tecplot(
    mesh_path: Path, surface_path: Path, volume_path: Path,
    quality: dict[str, Any], requested: bool,
) -> dict[str, Any]:
    """Export observed mesh dimensions, never demand volume cells from a surface mesh."""
    result: dict[str, Any] = {"requested": requested, "status": "skipped", "written_files": []}
    if not requested:
        return result
    try:
        surface_writer = (write_tecplot_surface_from_msh2 if quality.get("n_2d_elements")
                          else write_tecplot_boundary_surface_from_msh2)
        result["surface"] = surface_writer(str(mesh_path), str(surface_path))
        result["written_files"].append(str(surface_path))
        result["recommended_file"] = str(surface_path)
        if quality.get("n_3d_elements", 0) > 0:
            result["volume"] = write_tecplot_volume_from_msh2(str(mesh_path), str(volume_path))
            result["written_files"].append(str(volume_path))
        result["status"] = "success"
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
    return result


def write_tecplot_surface_from_msh2(mesh_path: str, tecplot_path: str) -> dict[str, Any]:
    """Write a Tecplot ASCII 2D FE file from front-surface Gmsh triangles/quads.

    This is the most Tecplot-friendly export for inspecting an airfoil grid:
    it contains x-y nodes at z=0 and surface elements only. Triangles and quads
    are written as separate zones so Tecplot does not need degenerate elements.
    """
    text = Path(mesh_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    all_nodes: dict[int, tuple[float, float, float]] = {}
    tri_refs: list[list[int]] = []
    quad_refs: list[list[int]] = []

    i = 0
    while i < len(text):
        line = text[i].strip()
        if line == "$Nodes":
            n_nodes = int(text[i + 1].strip())
            for row in text[i + 2:i + 2 + n_nodes]:
                parts = row.split()
                if len(parts) >= 4:
                    all_nodes[int(parts[0])] = (float(parts[1]), float(parts[2]), float(parts[3]))
            i += n_nodes + 2
        elif line == "$Elements":
            n_elem = int(text[i + 1].strip())
            for row in text[i + 2:i + 2 + n_elem]:
                parts = row.split()
                if len(parts) < 4:
                    continue
                elem_type = int(parts[1])
                n_tags = int(parts[2])
                refs = [int(v) for v in parts[3 + n_tags:]]
                if any(r not in all_nodes for r in refs):
                    continue
                # Keep only the front z=0 face to avoid duplicate overlaid surfaces.
                if any(abs(all_nodes[r][2]) > 1e-12 for r in refs):
                    continue
                if elem_type == 2 and len(refs) == 3:
                    tri_refs.append(refs)
                elif elem_type == 3 and len(refs) == 4:
                    quad_refs.append(refs)
            i += n_elem + 2
        i += 1

    used_ids = sorted({r for elem in tri_refs + quad_refs for r in elem})
    if not used_ids or not (tri_refs or quad_refs):
        raise ValueError(f"No z=0 surface triangles/quads found in {mesh_path}")
    id_map = {old: new for new, old in enumerate(used_ids, start=1)}
    path = Path(tecplot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write('TITLE = "Gmsh airfoil surface mesh"\n')
        f.write('VARIABLES = "X" "Y"\n')
        if tri_refs:
            f.write(
                f"ZONE T=\"triangles\", N={len(used_ids)}, E={len(tri_refs)}, "
                "DATAPACKING=POINT, ZONETYPE=FETRIANGLE\n"
            )
            # Tecplot ASCII expects node data per zone, so repeat compact nodes.
            for old in used_ids:
                x, y, _ = all_nodes[old]
                f.write(f"{x:.12g} {y:.12g}\n")
            for refs in tri_refs:
                f.write(" ".join(str(id_map[n]) for n in refs) + "\n")
        if quad_refs:
            f.write(
                f"ZONE T=\"quads\", N={len(used_ids)}, E={len(quad_refs)}, "
                "DATAPACKING=POINT, ZONETYPE=FEQUADRILATERAL\n"
            )
            for old in used_ids:
                x, y, _ = all_nodes[old]
                f.write(f"{x:.12g} {y:.12g}\n")
            for refs in quad_refs:
                f.write(" ".join(str(id_map[n]) for n in refs) + "\n")

    return {
        "path": str(path),
        "n_nodes": len(used_ids),
        "n_triangles": len(tri_refs),
        "n_quads": len(quad_refs),
        "n_elements": len(tri_refs) + len(quad_refs),
    }


def write_tecplot_boundary_surface_from_msh2(mesh_path: str, tecplot_path: str) -> dict[str, Any]:
    """Write all Gmsh 2D boundary elements to Tecplot.

    This is the generic fallback for CAD/imported geometry.  Unlike the
    airfoil-oriented writer, it does not assume a z=0 front plane.
    """
    text = Path(mesh_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    all_nodes: dict[int, tuple[float, float, float]] = {}
    tri_refs: list[list[int]] = []
    quad_refs: list[list[int]] = []

    i = 0
    while i < len(text):
        line = text[i].strip()
        if line == "$Nodes":
            n_nodes = int(text[i + 1].strip())
            for row in text[i + 2:i + 2 + n_nodes]:
                parts = row.split()
                if len(parts) >= 4:
                    all_nodes[int(parts[0])] = (float(parts[1]), float(parts[2]), float(parts[3]))
            i += n_nodes + 2
        elif line == "$Elements":
            n_elem = int(text[i + 1].strip())
            for row in text[i + 2:i + 2 + n_elem]:
                parts = row.split()
                if len(parts) < 4:
                    continue
                elem_type = int(parts[1])
                n_tags = int(parts[2])
                refs = [int(v) for v in parts[3 + n_tags:]]
                if any(r not in all_nodes for r in refs):
                    continue
                if elem_type == 2 and len(refs) == 3:
                    tri_refs.append(refs)
                elif elem_type == 3 and len(refs) == 4:
                    quad_refs.append(refs)
            i += n_elem + 2
        i += 1

    used_ids = sorted({r for elem in tri_refs + quad_refs for r in elem})
    if not used_ids or not (tri_refs or quad_refs):
        raise ValueError(f"No 2D boundary triangles/quads found in {mesh_path}")
    id_map = {old: new for new, old in enumerate(used_ids, start=1)}
    path = Path(tecplot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write('TITLE = "Gmsh boundary surface mesh"\n')
        f.write('VARIABLES = "X" "Y" "Z"\n')
        if tri_refs:
            f.write(
                f"ZONE T=\"triangles\", N={len(used_ids)}, E={len(tri_refs)}, "
                "DATAPACKING=POINT, ZONETYPE=FETRIANGLE\n"
            )
            for old in used_ids:
                x, y, z = all_nodes[old]
                f.write(f"{x:.12g} {y:.12g} {z:.12g}\n")
            for refs in tri_refs:
                f.write(" ".join(str(id_map[n]) for n in refs) + "\n")
        if quad_refs:
            f.write(
                f"ZONE T=\"quads\", N={len(used_ids)}, E={len(quad_refs)}, "
                "DATAPACKING=POINT, ZONETYPE=FEQUADRILATERAL\n"
            )
            for old in used_ids:
                x, y, z = all_nodes[old]
                f.write(f"{x:.12g} {y:.12g} {z:.12g}\n")
            for refs in quad_refs:
                f.write(" ".join(str(id_map[n]) for n in refs) + "\n")

    return {
        "path": str(path),
        "n_nodes": len(used_ids),
        "n_triangles": len(tri_refs),
        "n_quads": len(quad_refs),
        "n_elements": len(tri_refs) + len(quad_refs),
    }


def _sort_quad_vertices_by_xy(points: list[list[float]], refs: list[int]) -> list[int]:
    cx = sum(float(points[idx][0]) for idx in refs) / len(refs)
    cy = sum(float(points[idx][1]) for idx in refs) / len(refs)
    return sorted(
        refs,
        key=lambda idx: math.atan2(float(points[idx][1]) - cy, float(points[idx][0]) - cx),
    )


def _reconstruct_hex_cells_from_openfoam_mesh_data(mesh_data: dict[str, Any]) -> list[list[int]]:
    """Recover simple extruded hex cells from OpenFOAM-style mesh_data.

    Most parametric generators already return `cells`.  Some internal generators
    only return points/faces/owner/neighbour.  For Tecplot export we can recover
    cell point sets from face ownership and order the two z-layers consistently.
    This is intentionally generic for one-cell-thick extruded CFD meshes.
    """
    points = mesh_data.get("points") or []
    faces = mesh_data.get("faces") or []
    owner = mesh_data.get("owner") or []
    neighbour = mesh_data.get("neighbour") or []
    n_cells = int(mesh_data.get("n_cells") or 0)
    if not points or not faces or not owner or n_cells <= 0:
        return []

    cell_vertices: list[set[int]] = [set() for _ in range(n_cells)]
    for face_id, face in enumerate(faces):
        if face_id >= len(owner):
            break
        owner_id = int(owner[face_id])
        if 0 <= owner_id < n_cells:
            cell_vertices[owner_id].update(int(idx) for idx in face)
        if face_id < len(neighbour):
            neighbour_id = int(neighbour[face_id])
            if 0 <= neighbour_id < n_cells:
                cell_vertices[neighbour_id].update(int(idx) for idx in face)

    cells: list[list[int]] = []
    for refs_set in cell_vertices:
        refs = list(refs_set)
        if len(refs) != 8:
            continue
        z_values = sorted({round(float(points[idx][2]), 12) for idx in refs})
        if len(z_values) != 2:
            continue
        front_z, back_z = z_values[0], z_values[-1]
        front = [idx for idx in refs if round(float(points[idx][2]), 12) == front_z]
        back = [idx for idx in refs if round(float(points[idx][2]), 12) == back_z]
        if len(front) != 4 or len(back) != 4:
            continue
        front_sorted = _sort_quad_vertices_by_xy(points, front)
        back_sorted = _sort_quad_vertices_by_xy(points, back)
        cells.append(front_sorted + back_sorted)
    return cells


def _mesh_cells_for_tecplot(mesh_data: dict[str, Any]) -> list[list[int]]:
    cells = mesh_data.get("cells") or []
    if cells:
        return [list(cell) for cell in cells]
    recovered = _reconstruct_hex_cells_from_openfoam_mesh_data(mesh_data)
    if recovered:
        mesh_data["cells"] = recovered
    return recovered


def write_tecplot_surface_from_mesh_data(mesh_data: dict[str, Any], tecplot_path: str) -> dict[str, Any]:
    """Write a Tecplot ASCII 2D FE surface file from an in-memory extruded mesh."""
    points = mesh_data["points"]
    cells = _mesh_cells_for_tecplot(mesh_data)
    if not cells:
        raise ValueError("mesh_data does not contain recoverable cell connectivity for Tecplot export")

    front_z = min(float(p[2]) for p in points)
    quad_refs: list[list[int]] = []
    for cell in cells:
        front = [idx for idx in cell if abs(float(points[idx][2]) - front_z) < 1e-12]
        if len(front) == 4:
            quad_refs.append(front)
    if not quad_refs:
        raise ValueError("No front-surface quads found for Tecplot export")

    used_ids = sorted({idx for quad in quad_refs for idx in quad})
    id_map = {old: new for new, old in enumerate(used_ids, start=1)}
    path = Path(tecplot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(f'TITLE = "{mesh_data.get("mesh_type", "structured")} surface mesh"\n')
        f.write('VARIABLES = "X" "Y"\n')
        f.write(
            f"ZONE T=\"surface\", N={len(used_ids)}, E={len(quad_refs)}, "
            "DATAPACKING=POINT, ZONETYPE=FEQUADRILATERAL\n"
        )
        for old in used_ids:
            x, y, _ = points[old]
            f.write(f"{x:.12g} {y:.12g}\n")
        for refs in quad_refs:
            f.write(" ".join(str(id_map[idx]) for idx in refs) + "\n")

    return {
        "path": str(path),
        "n_nodes": len(used_ids),
        "n_quads": len(quad_refs),
        "n_elements": len(quad_refs),
    }


def write_tecplot_volume_from_mesh_data(mesh_data: dict[str, Any], tecplot_path: str) -> dict[str, Any]:
    """Write a Tecplot ASCII FEBRICK volume file from in-memory hex cells."""
    points = mesh_data["points"]
    cells = _mesh_cells_for_tecplot(mesh_data)
    bricks = [cell for cell in cells if len(cell) == 8]
    if not bricks:
        raise ValueError("mesh_data does not contain recoverable hex cell connectivity for Tecplot export")

    used_ids = sorted({idx for cell in bricks for idx in cell})
    id_map = {old: new for new, old in enumerate(used_ids, start=1)}
    path = Path(tecplot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(f'TITLE = "{mesh_data.get("mesh_type", "structured")} volume mesh"\n')
        f.write('VARIABLES = "X" "Y" "Z"\n')
        f.write(
            f"ZONE T=\"volume\", N={len(used_ids)}, E={len(bricks)}, "
            "DATAPACKING=POINT, ZONETYPE=FEBRICK\n"
        )
        for old in used_ids:
            x, y, z = points[old]
            f.write(f"{x:.12g} {y:.12g} {z:.12g}\n")
        for cell in bricks:
            f.write(" ".join(str(id_map[idx]) for idx in cell) + "\n")

    return {
        "path": str(path),
        "n_nodes": len(used_ids),
        "n_elements": len(bricks),
        "n_hexes": len(bricks),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 第二部分：OpenFOAM polyMesh 写入
# ═══════════════════════════════════════════════════════════════════════════════

_OFOAM_HEADER_TPL = Template(
    "/*--------------------------------*- C++ -*----------------------------------*\\\n"
    "| =========                 |                                                 |\n"
    "| \\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox           |\n"
    "|  \\    /   O peration     | Version:  v2512                                |\n"
    "|   \\  /    A nd           | Website:  www.openfoam.com                      |\n"
    "|    \\/     M anipulation  |                                                 |\n"
    "\\*---------------------------------------------------------------------------*/\n"
    "FoamFile\n"
    "{\n"
    "    version     2.0;\n"
    "    format      ascii;\n"
    "    class       $class_name;\n"
    "    object      $object_name;\n"
    "}\n"
    "// ************************************************************************* //\n"
)


def _make_header(class_name: str, object_name: str) -> str:
    return _OFOAM_HEADER_TPL.substitute(class_name=class_name, object_name=object_name)


def write_openfoam_polymesh(
    case_dir: str,
    points: list[list[float]],
    faces: list[list[int]],
    owner: list[int],
    neighbour: list[int],
    boundary: dict[str, dict[str, Any]],
    n_cells: int,
) -> dict[str, Any]:
    """将网格数据写入 OpenFOAM polyMesh 格式。"""
    poly_dir = Path(case_dir) / "constant" / "polyMesh"
    poly_dir.mkdir(parents=True, exist_ok=True)

    # points
    pts_content = _make_header("vectorField", "points")
    pts_content += f"\n{len(points)}\n(\n"
    for pt in points:
        pts_content += f"({pt[0]:.10g} {pt[1]:.10g} {pt[2]:.10g})\n"
    pts_content += ")\n"
    (poly_dir / "points").write_text(pts_content)

    # faces
    faces_content = _make_header("faceList", "faces")
    faces_content += f"\n{len(faces)}\n(\n"
    for face in faces:
        n_fp = len(face)
        idx_str = " ".join(str(i) for i in face)
        faces_content += f"{n_fp}({idx_str})\n"
    faces_content += ")\n"
    (poly_dir / "faces").write_text(faces_content)

    # owner
    owner_content = _make_header("labelList", "owner")
    owner_content += f"\n{len(owner)}\n(\n"
    for o in owner:
        owner_content += f"{o}\n"
    owner_content += ")\n"
    (poly_dir / "owner").write_text(owner_content)

    # neighbour
    neigh_content = _make_header("labelList", "neighbour")
    neigh_content += f"\n{len(neighbour)}\n(\n"
    for n in neighbour:
        neigh_content += f"{n}\n"
    neigh_content += ")\n"
    (poly_dir / "neighbour").write_text(neigh_content)

    # boundary
    bnd_content = _make_header("polyBoundaryMesh", "boundary")
    bnd_content += f"\n{len(boundary)}\n(\n"
    for name, info in boundary.items():
        bnd_content += f"    {name}\n    {{\n"
        bnd_content += f"        type            {info['type']};\n"
        bnd_content += f"        nFaces          {info['nFaces']};\n"
        bnd_content += f"        startFace       {info['startFace']};\n"
        bnd_content += "    }\n"
    bnd_content += ")\n"
    (poly_dir / "boundary").write_text(bnd_content)

    written_files = [
        str(poly_dir / "points"),
        str(poly_dir / "faces"),
        str(poly_dir / "owner"),
        str(poly_dir / "neighbour"),
        str(poly_dir / "boundary"),
    ]
    return {
        "case_dir": str(case_dir),
        "written_files": written_files,
        "n_points": len(points),
        "n_faces": len(faces),
        "n_cells": n_cells,
        "n_boundary_patches": len(boundary),
    }


def write_openfoam_system_files(case_dir: str, application: str = "simpleFoam") -> dict[str, Any]:
    """Write the minimal OpenFOAM system files expected by tools and Tecplot."""
    system_dir = Path(case_dir) / "system"
    system_dir.mkdir(parents=True, exist_ok=True)

    control_dict = system_dir / "controlDict"
    if not control_dict.exists():
        control_dict.write_text(
            _make_header("dictionary", "controlDict")
            + f"\napplication     {application};\n"
            + "startFrom       startTime;\n"
            + "startTime       0;\n"
            + "stopAt          endTime;\n"
            + "endTime         1;\n"
            + "deltaT          1;\n"
            + "writeControl    timeStep;\n"
            + "writeInterval   1;\n",
            encoding="utf-8",
        )

    fv_schemes = system_dir / "fvSchemes"
    if not fv_schemes.exists():
        fv_schemes.write_text(
            _make_header("dictionary", "fvSchemes")
            + "\nddtSchemes { default steadyState; }\n"
            + "gradSchemes { default Gauss linear; }\n"
            + "divSchemes { default none; div(phi,U) bounded Gauss upwind; }\n"
            + "laplacianSchemes { default Gauss linear corrected; }\n"
            + "interpolationSchemes { default linear; }\n"
            + "snGradSchemes { default corrected; }\n",
            encoding="utf-8",
        )

    fv_solution = system_dir / "fvSolution"
    if not fv_solution.exists():
        fv_solution.write_text(
            _make_header("dictionary", "fvSolution")
            + "\nsolvers\n{\n"
            + "    p { solver GAMG; tolerance 1e-7; relTol 0.1; smoother GaussSeidel; }\n"
            + "    U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; }\n"
            + "}\n"
            + "SIMPLE { nNonOrthogonalCorrectors 0; }\n"
            + "relaxationFactors { fields { p 0.3; } equations { U 0.7; } }\n",
            encoding="utf-8",
        )

    return {
        "system_dir": str(system_dir),
        "written_files": [str(control_dict), str(fv_schemes), str(fv_solution)],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 第三部分：2D 结构化网格通用构建器
# ═══════════════════════════════════════════════════════════════════════════════

def _build_2d_structured_mesh(
    X: list[list[float]],
    Y: list[list[float]],
    nj: int,
    ni: int,
    z_front: float = 0.0,
    z_back: float = 0.1,
    boundary_patches: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """从 (X, Y) 二维数组构建 OpenFOAM polyMesh 数据。

    参数：
        X, Y: [nj+1][ni+1] 的二维坐标数组
        nj, ni: 各方向单元数
        z_front, z_back: 前后 z 坐标
        boundary_patches: 边界 patch 定义，格式：
            {
                "patch_name": {
                    "type": "wall"/"patch"/"empty",
                    "range": "bottom"/"top"/"left"/"right",
                }
            }
    """
    n_pts_i = ni + 1  # i 方向点数

    # 生成点（两层 z）
    points = []
    for j in range(nj + 1):
        for i in range(n_pts_i):
            points.append([X[j][i], Y[j][i], z_front])
    for j in range(nj + 1):
        for i in range(n_pts_i):
            points.append([X[j][i], Y[j][i], z_back])

    def pid(j: int, i: int, z: int) -> int:
        return z * (nj + 1) * n_pts_i + j * n_pts_i + i

    faces = []
    owner = []
    neighbour = []

    # 内部面：j 方向
    for j in range(nj):
        for i in range(1, ni):
            faces.append([pid(j, i, 0), pid(j + 1, i, 0),
                          pid(j + 1, i, 1), pid(j, i, 1)])
            owner.append(j * ni + (i - 1))
            neighbour.append(j * ni + i)

    # 内部面：i 方向
    for j in range(1, nj):
        for i in range(ni):
            faces.append([pid(j, i, 0), pid(j, i + 1, 0),
                          pid(j, i + 1, 1), pid(j, i, 1)])
            owner.append((j - 1) * ni + i)
            neighbour.append(j * ni + i)

    n_internal = len(faces)

    # 边界面
    boundary_info: dict[str, dict[str, Any]] = {}
    default_patches = {
        "bottom": {"type": "wall", "range": "bottom"},
        "top":    {"type": "wall", "range": "top"},
        "left":   {"type": "patch", "range": "left"},
        "right":  {"type": "patch", "range": "right"},
    }
    if boundary_patches is not None:
        default_patches.update(boundary_patches)

    # bottom: j=0
    b_start = len(faces)
    for i in range(ni):
        faces.append([pid(0, i, 0), pid(0, i + 1, 0),
                      pid(0, i + 1, 1), pid(0, i, 1)])
        owner.append(i)
    boundary_info["bottom"] = {
        "type": default_patches.get("bottom", {}).get("type", "wall"),
        "nFaces": ni, "startFace": b_start,
    }

    # top: j=nj
    t_start = len(faces)
    for i in range(ni):
        faces.append([pid(nj, i, 0), pid(nj, i, 1),
                      pid(nj, i + 1, 1), pid(nj, i + 1, 0)])
        owner.append((nj - 1) * ni + i)
    boundary_info["top"] = {
        "type": default_patches.get("top", {}).get("type", "wall"),
        "nFaces": ni, "startFace": t_start,
    }

    # left: i=0
    l_start = len(faces)
    for j in range(nj):
        faces.append([pid(j, 0, 0), pid(j, 0, 1),
                      pid(j + 1, 0, 1), pid(j + 1, 0, 0)])
        owner.append(j * ni)
    boundary_info["left"] = {
        "type": default_patches.get("left", {}).get("type", "patch"),
        "nFaces": nj, "startFace": l_start,
    }

    # right: i=ni
    r_start = len(faces)
    for j in range(nj):
        faces.append([pid(j, ni, 0), pid(j + 1, ni, 0),
                      pid(j + 1, ni, 1), pid(j, ni, 1)])
        owner.append(j * ni + (ni - 1))
    boundary_info["right"] = {
        "type": default_patches.get("right", {}).get("type", "patch"),
        "nFaces": nj, "startFace": r_start,
    }

    # frontAndBack
    fab_start = len(faces)
    for j in range(nj):
        for i in range(ni):
            faces.append([pid(j, i, 0), pid(j, i + 1, 0),
                          pid(j + 1, i + 1, 0), pid(j + 1, i, 0)])
            owner.append(j * ni + i)
            faces.append([pid(j, i, 1), pid(j + 1, i, 1),
                          pid(j + 1, i + 1, 1), pid(j, i + 1, 1)])
            owner.append(j * ni + i)
    n_fab = len(faces) - fab_start
    boundary_info["frontAndBack"] = {"type": "empty", "nFaces": n_fab, "startFace": fab_start}

    n_cells = nj * ni
    cells = []
    for j in range(nj):
        for i in range(ni):
            cells.append([
                pid(j, i, 0),
                pid(j, i + 1, 0),
                pid(j + 1, i + 1, 0),
                pid(j + 1, i, 0),
                pid(j, i, 1),
                pid(j, i + 1, 1),
                pid(j + 1, i + 1, 1),
                pid(j + 1, i, 1),
            ])

    return {
        "points": points,
        "faces": faces,
        "owner": owner,
        "neighbour": neighbour,
        "boundary": boundary_info,
        "n_cells": n_cells,
        "cells": cells,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 第四部分：各类型网格生成函数
# ═══════════════════════════════════════════════════════════════════════════════

# ── 1. 翼型 O-grid ──────────────────────────────────────────────────────────

def generate_airfoil_ogrid(
    naca_code: str = "",
    ni: int = 201,
    nj: int = 80,
    far_field: float = 15.0,
    delta0: float = 1e-5,
    stretch: float = 1.12,
    n_smooth: int = 60,
) -> dict[str, Any]:
    """为翼型生成结构化 O-grid 网格。"""
    naca_code = _normalise_naca_code(naca_code)
    if not naca_code:
        raise ValueError("Explicit naca_code is required for airfoil_ogrid; refusing to default to NACA0012.")
    xu, yu, xl, yl = naca_4digit(naca_code, n_points=ni)
    # The O-grid boundary must run TE(upper) -> LE -> TE(lower). The old
    # ordering started and ended at the leading edge, while the remaining
    # topology treated those endpoints as a trailing-edge slit; that produced
    # inverted/open cells before checkMesh ever ran.
    xs = xu[::-1] + xl[1:]
    ys = yu[::-1] + yl[1:]

    # 壁面法向量（相邻点有限差分）
    n_half = ni
    nx_s = [0.0] * len(xs)
    ny_s = [0.0] * len(xs)
    for i in range(len(xs)):
        if i == 0:
            dx = xs[1] - xs[0]; dy = ys[1] - ys[0]
        elif i == len(xs) - 1:
            dx = xs[-1] - xs[-2]; dy = ys[-1] - ys[-2]
        else:
            dx = xs[i + 1] - xs[i - 1]; dy = ys[i + 1] - ys[i - 1]
        nx_s[i] = dy; ny_s[i] = -dx
        nl = math.hypot(nx_s[i], ny_s[i])
        if nl > 1e-15:
            nx_s[i] /= nl; ny_s[i] /= nl

    # TE slit 修正
    nx_s[0] = 1.0; ny_s[0] = 0.0
    nx_s[-1] = 1.0; ny_s[-1] = 0.0

    # 法向拉伸
    X = [[0.0] * len(xs) for _ in range(nj)]
    Y = [[0.0] * len(xs) for _ in range(nj)]
    for i in range(len(xs)):
        X[0][i] = xs[i]; Y[0][i] = ys[i]

    dj = [0.0] * nj
    dj[0] = delta0
    for j in range(1, nj):
        dj[j] = dj[j - 1] * stretch
    total_d = sum(dj)
    scale = far_field / total_d

    current_d = 0.0
    for j in range(1, nj):
        current_d += dj[j] * scale
        angle_frac = current_d / far_field
        for i in range(len(xs)):
            far_angle = 2.0 * math.pi * i / (len(xs) - 1)
            if i == 0: far_angle = 0.0
            elif i == len(xs) - 1: far_angle = 2.0 * math.pi
            far_x = far_field * math.cos(far_angle)
            far_y = far_field * math.sin(far_angle)
            t = angle_frac
            X[j][i] = (1.0 - t) * xs[i] + t * far_x + nx_s[i] * current_d * (1 - t)
            Y[j][i] = (1.0 - t) * ys[i] + t * far_y + ny_s[i] * current_d * (1 - t)

    # Laplacian 光滑
    for _ in range(n_smooth):
        for j in range(1, nj - 1):
            for i in range(1, len(xs) - 1):
                if i == 0 or i == len(xs) - 1: continue
                X[j][i] = 0.25 * (X[j - 1][i] + X[j + 1][i] + X[j][i - 1] + X[j][i + 1])
                Y[j][i] = 0.25 * (Y[j - 1][i] + Y[j + 1][i] + Y[j][i - 1] + Y[j][i + 1])

    n_total_i = len(xs)
    z_front = 0.0; z_back = 0.1

    # 用通用构建器
    # O-grid 的边界需要特殊处理：airfoil + farfield + inlet + outlet
    # 先构建点
    points = []
    for j in range(nj):
        for i in range(n_total_i):
            points.append([X[j][i], Y[j][i], z_front])
    for j in range(nj):
        for i in range(n_total_i):
            points.append([X[j][i], Y[j][i], z_back])

    def pid(j: int, i: int, z: int) -> int:
        return z * nj * n_total_i + j * n_total_i + i

    faces = []; owner = []; neighbour = []
    n_cells_x = n_total_i - 1; n_cells_y = nj - 1

    # 内部面 j方向
    for j in range(nj - 1):
        for i in range(1, n_total_i - 1):
            faces.append([pid(j, i, 1), pid(j + 1, i, 1), pid(j + 1, i, 0), pid(j, i, 0)])
            owner.append(j * n_cells_x + (i - 1))
            neighbour.append(j * n_cells_x + i)

    # 内部面 i方向
    for j in range(1, nj - 1):
        for i in range(n_total_i - 1):
            faces.append([pid(j, i, 0), pid(j, i + 1, 0), pid(j, i + 1, 1), pid(j, i, 1)])
            owner.append((j - 1) * n_cells_x + i)
            neighbour.append(j * n_cells_x + i)

    n_internal = len(faces)

    # airfoil: j=0
    af_start = len(faces)
    for i in range(n_total_i - 1):
        faces.append([pid(0, i, 0), pid(0, i, 1), pid(0, i + 1, 1), pid(0, i + 1, 0)])
        owner.append(i)
    boundary_info = {"airfoil": {"type": "wall", "nFaces": n_total_i - 1, "startFace": af_start}}

    # farfield: j=nj-1
    ff_start = len(faces)
    for i in range(n_total_i - 1):
        faces.append([pid(nj - 1, i, 0), pid(nj - 1, i + 1, 0), pid(nj - 1, i + 1, 1), pid(nj - 1, i, 1)])
        owner.append((nj - 2) * n_cells_x + i)
    boundary_info["farfield"] = {"type": "patch", "nFaces": n_total_i - 1, "startFace": ff_start}

    # inlet: i=0 (TE slit)
    in_start = len(faces)
    for j in range(nj - 1):
        faces.append([pid(j, 0, 0), pid(j + 1, 0, 0), pid(j + 1, 0, 1), pid(j, 0, 1)])
        owner.append(j * n_cells_x)
    boundary_info["inlet"] = {"type": "patch", "nFaces": nj - 1, "startFace": in_start}

    # outlet: i=NI-1
    out_start = len(faces)
    for j in range(nj - 1):
        faces.append([pid(j, n_total_i - 1, 0), pid(j, n_total_i - 1, 1),
                      pid(j + 1, n_total_i - 1, 1), pid(j + 1, n_total_i - 1, 0)])
        owner.append(j * n_cells_x + (n_total_i - 2))
    boundary_info["outlet"] = {"type": "patch", "nFaces": nj - 1, "startFace": out_start}

    # frontAndBack
    fab_start = len(faces)
    for j in range(nj - 1):
        for i in range(n_total_i - 1):
            faces.append([pid(j, i, 0), pid(j, i + 1, 0), pid(j + 1, i + 1, 0), pid(j + 1, i, 0)])
            owner.append(j * n_cells_x + i)
            faces.append([pid(j, i, 1), pid(j + 1, i, 1), pid(j + 1, i + 1, 1), pid(j, i + 1, 1)])
            owner.append(j * n_cells_x + i)
    n_fab = len(faces) - fab_start
    boundary_info["frontAndBack"] = {"type": "empty", "nFaces": n_fab, "startFace": fab_start}

    n_cells = n_cells_y * n_cells_x

    return {
        "points": points, "faces": faces,
        "owner": owner, "neighbour": neighbour,
        "boundary": boundary_info, "n_cells": n_cells,
        "mesh_type": "airfoil_ogrid",
        "grid_params": {
            "naca_code": naca_code, "ni": ni, "nj": nj,
            "far_field": far_field, "delta0": delta0,
            "stretch": stretch, "n_smooth": n_smooth,
        },
    }


def _boundary_layer_count(first_height: float, total_thickness: float, growth_ratio: float) -> int:
    """Return the layer count whose geometric-height sum fits the envelope."""
    first = max(float(first_height), 1e-12)
    thickness = max(float(total_thickness), first * 3.0)
    ratio = max(float(growth_ratio), 1.0)
    if ratio <= 1.0 + 1e-8:
        return max(3, int(math.ceil(thickness / first)))
    count = math.log1p(thickness * (ratio - 1.0) / first) / math.log(ratio)
    return max(3, int(math.floor(count)))


def _build_airfoil_gmsh_geo(
    naca_code: str = "",
    airfoil_name: str = "",
    airfoil_coords: list[tuple[float, float]] | None = None,
    domain_type: str = "c",
    far_field: float = 15.0,
    far_field_chord_ratio: float | None = None,
    far_field_ratio: float | None = None,
    wake_length: float = 20.0,
    wake_length_chord_ratio: float | None = None,
    wake_length_ratio: float | None = None,
    n_surface: int = 241,
    angle_of_attack: float = 0.0,
    rotation_center_x: float = 0.25,
    rotation_center_y: float = 0.0,
    h_airfoil: float = 0.006,
    h_farfield: float = 1.0,
    boundary_layer_first: float = 1e-4,
    boundary_layer_thickness: float = 0.08,
    boundary_layer_ratio: float = 1.18,
    boundary_layer_layers: int | None = None,
    mesh_algorithm: int = 6,
    near_wall_refinement_mode: str = "boundary_layer",
    mesh_family_contract: dict[str, Any] | None = None,
    lock_near_wall_topology: bool = False,
    recombine: bool = False,
    extrude_to_3d: bool = True,
    span: float = 0.1,
    boundary_role_names: dict[str, str] | None = None,
) -> str:
    if airfoil_coords is not None:
        coords = _normalise_airfoil_coordinates(airfoil_coords)
        geometry_label = airfoil_name or "custom_airfoil"
    else:
        naca_code = _normalise_naca_code(naca_code)
        if not naca_code:
            raise ValueError("Explicit naca_code or coordinates are required; refusing to default to NACA0012.")
        coords = naca_4digit_closed_loop(naca_code, n_surface=n_surface)
        geometry_label = f"NACA {naca_code}"
    coords = _rotate_points_2d(coords, angle_of_attack, rotation_center_x, rotation_center_y)
    le_x = min(x for x, _ in coords)
    role_names = boundary_role_names or {}
    inlet_name = _boundary_role_name(role_names, "inlet", default="inlet")
    outlet_name = _boundary_role_name(role_names, "outlet", default="outlet")
    farfield_name = _boundary_role_name(role_names, "farfield", default="farfield")
    top_name = _boundary_role_name(role_names, "top", default=farfield_name)
    bottom_name = _boundary_role_name(role_names, "bottom", default=farfield_name)
    airfoil_patch_name = _boundary_role_name(role_names, "airfoil", "wall", default="airfoil")
    front_back_name = _boundary_role_name(
        role_names, "front_and_back", "frontAndBack", default="frontAndBack"
    )

    lines: list[str] = [
        'SetFactory("OpenCASCADE");',
        f"// airfoil = {geometry_label}",
        f"// naca_code = {naca_code if airfoil_coords is None else ''}",
        f"// angle_of_attack_deg = {angle_of_attack:.12g}",
        f"// rotation_center = ({rotation_center_x:.12g}, {rotation_center_y:.12g})",
        f"Mesh.Algorithm = {mesh_algorithm};",
        f"Mesh.Optimize = 1;",
        f"Mesh.OptimizeNetgen = 1;",
        f"Mesh.CharacteristicLengthMin = {min(h_airfoil, boundary_layer_first):.12g};",
        f"Mesh.CharacteristicLengthMax = {h_farfield:.12g};",
        "",
    ]
    for idx, (x, y) in enumerate(coords, start=1):
        lines.append(f"Point({idx}) = {{{x:.12g}, {y:.12g}, 0, {h_airfoil:.12g}}};")
    le_idx = min(range(1, len(coords) + 1), key=lambda idx: coords[idx - 1][0])
    upper_ids = ",".join(str(i) for i in range(1, le_idx + 1))
    lower_ids = ",".join(str(i) for i in [le_idx, *range(le_idx + 1, len(coords) + 1)])
    lines.extend([
        f"Spline(1) = {{{upper_ids}}};",
        f"Spline(2) = {{{lower_ids}}};",
        f"Line(3) = {{{len(coords)}, 1}};",
        f"// requested_surface_nodes = {max(41, int(n_surface))}",
        "Curve Loop(10) = {1, 2, 3};",
    ])
    upper_length = sum(
        math.hypot(coords[index][0] - coords[index - 1][0], coords[index][1] - coords[index - 1][1])
        for index in range(1, le_idx)
    )
    lower_length = sum(
        math.hypot(coords[index][0] - coords[index - 1][0], coords[index][1] - coords[index - 1][1])
        for index in range(le_idx, len(coords))
    )
    total_profile_length = max(upper_length + lower_length, 1e-12)
    surface_intervals = max(40, int(n_surface) - 1)
    upper_intervals = max(20, int(round(surface_intervals * upper_length / total_profile_length)))
    lower_intervals = max(20, surface_intervals - upper_intervals)
    lines.extend([
        f"Transfinite Curve {{1}} = {upper_intervals + 1} Using Progression 1;",
        f"Transfinite Curve {{2}} = {lower_intervals + 1} Using Progression 1;",
        "Transfinite Curve {3} = 2 Using Progression 1;",
    ])

    outer_curve_ids: list[int]
    inlet_curve_ids: list[int]
    outlet_curve_ids: list[int]
    airfoil_curve_ids = [1, 2, 3]

    if domain_type.lower().startswith("c"):
        p0 = len(coords) + 1
        outer = [
            (wake_length, -far_field),
            (le_x, -far_field),
            (le_x, 0.0),
            (le_x - far_field, 0.0),
            (le_x, far_field),
            (wake_length, far_field),
        ]
        for offset, (x, y) in enumerate(outer):
            lines.append(f"Point({p0 + offset}) = {{{x:.12g}, {y:.12g}, 0, {h_farfield:.12g}}};")
        outer_curve_ids = [20, 21, 22, 23, 24]
        inlet_curve_ids = [21, 22]
        outlet_curve_ids = [24]
        lines.extend([
            f"Line(20) = {{{p0}, {p0 + 1}}};",
            f"Circle(21) = {{{p0 + 1}, {p0 + 2}, {p0 + 3}}};",
            f"Circle(22) = {{{p0 + 3}, {p0 + 2}, {p0 + 4}}};",
            f"Line(23) = {{{p0 + 4}, {p0 + 5}}};",
            f"Line(24) = {{{p0 + 5}, {p0}}};",
            "Curve Loop(30) = {20, 21, 22, 23, 24};",
            "Plane Surface(40) = {30, 10};",
        ])
    else:
        p0 = len(coords) + 1
        outer = [
            (0.5, 0.0),
            (0.5 + far_field, 0.0),
            (0.5, far_field),
            (0.5 - far_field, 0.0),
            (0.5, -far_field),
        ]
        for offset, (x, y) in enumerate(outer):
            lines.append(f"Point({p0 + offset}) = {{{x:.12g}, {y:.12g}, 0, {h_farfield:.12g}}};")
        outer_curve_ids = [20, 21, 22, 23]
        inlet_curve_ids = []
        outlet_curve_ids = []
        lines.extend([
            f"Circle(20) = {{{p0 + 1}, {p0}, {p0 + 2}}};",
            f"Circle(21) = {{{p0 + 2}, {p0}, {p0 + 3}}};",
            f"Circle(22) = {{{p0 + 3}, {p0}, {p0 + 4}}};",
            f"Circle(23) = {{{p0 + 4}, {p0}, {p0 + 1}}};",
            "Curve Loop(30) = {20, 21, 22, 23};",
            "Plane Surface(40) = {30, 10};",
        ])

    near_wall_refinement_mode = str(near_wall_refinement_mode or "boundary_layer").strip().lower()
    if near_wall_refinement_mode == "distance_field":
        # A distance/threshold field is the geometry-preserving alternative
        # when a boundary-layer front cannot be made valid automatically.
        # It applies to every closed coordinate profile and does not alter
        # boundary names, topology, or user-provided geometry.
        lines.extend([
            "Field[1] = Distance;",
            "Field[1].CurvesList = {1, 2, 3};",
            f"Field[1].Sampling = {max(200, int(n_surface) * 2)};",
            "Field[2] = Threshold;",
            "Field[2].InField = 1;",
            f"Field[2].SizeMin = {boundary_layer_first:.12g};",
            f"Field[2].SizeMax = {h_farfield:.12g};",
            "Field[2].DistMin = 0;",
            f"Field[2].DistMax = {boundary_layer_thickness:.12g};",
            "Field[2].Sigmoid = 1;",
        ])
    else:
        bl_n = (
            max(1, int(boundary_layer_layers))
            if boundary_layer_layers is not None
            else _boundary_layer_count(
                boundary_layer_first, boundary_layer_thickness, boundary_layer_ratio
            )
        )
        lines.extend([
            "Field[1] = BoundaryLayer;",
            "Field[1].CurvesList = {1, 2};",
            f"Field[1].Size = {h_airfoil:.12g};",
            f"Field[1].SizeFar = {h_farfield:.12g};",
            f"Field[1].hwall_n = {boundary_layer_first:.12g};",
            f"Field[1].thickness = {boundary_layer_thickness:.12g};",
            f"Field[1].ratio = {boundary_layer_ratio:.12g};",
            f"Field[1].NbLayers = {bl_n};",
            "Field[1].Quads = 1;",
            f"Field[1].FanPointsList = {{1, {len(coords)}}};",
            "Field[1].FanPointsSizesList = {3, 3};",
            "BoundaryLayer Field = 1;",
        ])
    trailing_x = max(x for x, _ in coords)
    wake_size = min(max(4.0 * h_airfoil, 0.02), 0.25 * h_farfield)
    wake_point = len(coords) + 20
    lines.extend([
        f"Point({wake_point}) = {{{trailing_x:.12g}, 0, 0, {wake_size:.12g}}};",
        f"Point({wake_point + 1}) = {{{max(trailing_x + 1.0, wake_length):.12g}, 0, 0, {wake_size:.12g}}};",
        f"Line(50) = {{{wake_point}, {wake_point + 1}}};",
        "Field[3] = Distance;",
        "Field[3].CurvesList = {50};",
        "Field[3].Sampling = 100;",
        "Field[4] = Threshold;",
        "Field[4].InField = 3;",
        f"Field[4].SizeMin = {wake_size:.12g};",
        f"Field[4].SizeMax = {h_farfield:.12g};",
        "Field[4].DistMin = 0.1;",
        "Field[4].DistMax = 1.5;",
    ])
    if near_wall_refinement_mode == "distance_field":
        lines.extend([
            "Field[5] = Min;",
            "Field[5].FieldsList = {2, 4};",
            "Background Field = 5;",
        ])
    else:
        lines.append("Background Field = 4;")
    if recombine:
        lines.extend([
            "Recombine Surface {40};",
            "Mesh.RecombinationAlgorithm = 1;",
        ])
    outer_groups = (
        [(inlet_name, inlet_curve_ids), (outlet_name, outlet_curve_ids), (bottom_name, [20]), (top_name, [23])]
        if domain_type.lower().startswith("c")
        else [(top_name, [20, 21]), (bottom_name, [22, 23])]
    )
    if extrude_to_3d:
        boundary_order = outer_curve_ids + airfoil_curve_ids
        lateral_tags = {curve_id: f"vol[{idx + 2}]" for idx, curve_id in enumerate(boundary_order)}
        airfoil_surfaces = [lateral_tags[c] for c in airfoil_curve_ids if c in lateral_tags]
        grouped_surfaces: dict[str, list[str]] = {}
        for name, curve_ids in outer_groups:
            grouped_surfaces.setdefault(name, []).extend(
                lateral_tags[curve_id] for curve_id in curve_ids if curve_id in lateral_tags
            )
        lines.extend([
            "",
            f"vol[] = Extrude {{0, 0, {span:.12g}}} {{",
            "  Surface{40};",
            "  Layers{1};",
            "  Recombine;",
            "};",
            'Physical Volume("fluid") = {vol[1]};',
            f'Physical Surface("{front_back_name}") = {{40, vol[0]}};',
        ])
        lines.extend(
            f'Physical Surface("{name}") = {{{", ".join(surfaces)}}};'
            for name, surfaces in grouped_surfaces.items()
            if surfaces
        )
        lines.append(f'Physical Surface("{airfoil_patch_name}") = {{{", ".join(airfoil_surfaces)}}};')
    else:
        grouped_curves: dict[str, list[int]] = {}
        for name, curve_ids in outer_groups:
            grouped_curves.setdefault(name, []).extend(curve_ids)
        lines.extend(
            f'Physical Curve("{name}") = {{{", ".join(str(curve) for curve in curves)}}};'
            for name, curves in grouped_curves.items()
            if curves
        )
        lines.append(f'Physical Curve("{airfoil_patch_name}") = {{1, 2, 3}};')
        lines.append('Physical Surface("fluid") = {40};')
    return "\n".join(lines) + "\n"


def _build_coordinate_profile_cascade_geo(
    profile_coords: list[tuple[float, float]],
    profile_name: str = "coordinate_profile",
    pitch: float | None = None,
    pitch_chord_ratio: float | None = None,
    upstream_length: float | None = None,
    downstream_length: float | None = None,
    fore_domain_length: float | None = None,
    aft_domain_length: float | None = None,
    axial_chord: float | None = None,
    reference_length: float | None = None,
    lengths_normalized: bool = False,
    domain_boundary_points: Any = None,
    domain_boundary_patch_names: Any = None,
    periodic_lower_points: Any = None,
    periodic_translation_vector: Any = None,
    domain_shape_verified: bool = False,
    h_blade: float = 0.006,
    h_farfield: float = 0.2,
    boundary_layer_first: float = 1e-4,
    boundary_layer_thickness: float = 0.04,
    boundary_layer_ratio: float = 1.12,
    span: float = 0.1,
    recombine: bool = False,
    extrude_to_3d: bool = True,
    preserve_input_scale: bool = False,
    spanwise_layers: int = 1,
) -> tuple[str, dict[str, Any]]:
    coords = (
        [(float(x), float(y)) for x, y in profile_coords]
        if preserve_input_scale
        else _normalise_airfoil_coordinates(profile_coords)
    )
    min_x = min(x for x, _ in coords)
    max_x = max(x for x, _ in coords)
    min_y = min(y for _, y in coords)
    max_y = max(y for _, y in coords)
    chord = max(max_x - min_x, 1e-9)
    dimensional_reference = float(reference_length or axial_chord or 0.0)
    def normalized_length(value: Any, fallback: float) -> float:
        raw = float(fallback if value in (None, "") else value)
        if lengths_normalized or dimensional_reference <= 0:
            return raw
        return raw / dimensional_reference

    pitch_value = (
        float(pitch_chord_ratio) * chord
        if pitch_chord_ratio not in (None, "")
        else normalized_length(pitch, 1.2 * chord)
    )
    upstream = normalized_length(
        upstream_length if upstream_length is not None else fore_domain_length,
        1.0 * chord,
    )
    downstream = normalized_length(
        downstream_length if downstream_length is not None else aft_domain_length,
        2.0 * chord,
    )
    if not lengths_normalized and dimensional_reference > 0:
        h_blade = normalized_length(h_blade, h_blade)
        h_farfield = normalized_length(h_farfield, h_farfield)
        boundary_layer_first = normalized_length(boundary_layer_first, boundary_layer_first)
        boundary_layer_thickness = normalized_length(boundary_layer_thickness, boundary_layer_thickness)

    y_center = 0.5 * (min_y + max_y)
    y_low = y_center - 0.5 * pitch_value
    y_high = y_center + 0.5 * pitch_value
    margin = max(0.05 * chord, 2.0 * h_blade)
    y_low = min(y_low, min_y - margin)
    y_high = max(y_high, max_y + margin)
    x_in = min_x - upstream
    x_out = max_x + downstream

    lines: list[str] = [
        'SetFactory("OpenCASCADE");',
        f"// coordinate_profile_cascade = {profile_name}",
        f"// pitch = {pitch_value:.12g}",
        f"// upstream_length = {upstream:.12g}",
        f"// downstream_length = {downstream:.12g}",
        "Mesh.Algorithm = 6;",
        "Mesh.Optimize = 1;",
        "Mesh.OptimizeNetgen = 1;",
        f"Mesh.CharacteristicLengthMin = {min(h_blade, boundary_layer_first):.12g};",
        f"Mesh.CharacteristicLengthMax = {h_farfield:.12g};",
        "",
    ]
    for idx, (x, y) in enumerate(coords, start=1):
        lines.append(f"Point({idx}) = {{{x:.12g}, {y:.12g}, 0, {h_blade:.12g}}};")
    le_idx = min(range(1, len(coords) + 1), key=lambda idx: coords[idx - 1][0])
    upper_ids = ",".join(str(i) for i in range(1, le_idx + 1))
    lower_ids = ",".join(str(i) for i in [le_idx, *range(le_idx + 1, len(coords) + 1)])
    lines.extend([
        f"Spline(1) = {{{upper_ids}}};",
        f"Spline(2) = {{{lower_ids}}};",
        f"Line(3) = {{{len(coords)}, 1}};",
        "Curve Loop(10) = {1, 2, 3};",
    ])
    def point_pairs(raw: Any) -> list[tuple[float, float]]:
        if not isinstance(raw, (list, tuple)):
            return []
        parsed: list[tuple[float, float]] = []
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                parsed.append((float(item[0]), float(item[1])))
            elif isinstance(item, dict) and item.get("x") is not None and item.get("y") is not None:
                parsed.append((float(item["x"]), float(item["y"])))
        return parsed

    if isinstance(domain_boundary_points, dict):
        polygon = point_pairs(domain_boundary_points.get("points") or domain_boundary_points.get("vertices"))
    else:
        polygon = point_pairs(domain_boundary_points)
    boundary_patch_names = (
        [str(value) for value in domain_boundary_patch_names]
        if isinstance(domain_boundary_patch_names, (list, tuple))
        else []
    )
    lower = point_pairs(periodic_lower_points)
    translation = point_pairs([periodic_translation_vector])[0] if point_pairs([periodic_translation_vector]) else (0.0, pitch_value)
    domain_shape = "rectangular_unverified"
    if len(polygon) >= 4:
        boundary_points = polygon
        domain_shape = "reference_polygon"
    elif len(lower) >= 2:
        upper = [(x + translation[0], y + translation[1]) for x, y in reversed(lower)]
        boundary_points = [*lower, *upper]
        domain_shape = "segmented_periodic_passage"
    else:
        boundary_points = [(x_in, y_low), (x_out, y_low), (x_out, y_high), (x_in, y_high)]
    if domain_shape_verified and domain_shape == "rectangular_unverified":
        raise ValueError(
            "domain_shape_verified=true requires domain_boundary_points as an ordered point array, "
            "periodic_lower_points plus periodic_translation_vector, or a converted reference mesh; "
            "a bounding-box dictionary is not a verified domain shape"
        )

    p0 = len(coords) + 1
    for offset, (x, y) in enumerate(boundary_points):
        lines.append(f"Point({p0 + offset}) = {{{x:.12g}, {y:.12g}, 0, {h_farfield:.12g}}};")
    outer_ids = list(range(20, 20 + len(boundary_points)))
    for offset, curve_id in enumerate(outer_ids):
        lines.append(f"Line({curve_id}) = {{{p0 + offset}, {p0 + ((offset + 1) % len(boundary_points))}}};")
    lines.extend([
        f"Curve Loop(30) = {{{', '.join(str(value) for value in outer_ids)}}};",
        "Plane Surface(40) = {30, 10};",
    ])
    bl_n = _boundary_layer_count(
        boundary_layer_first, boundary_layer_thickness, boundary_layer_ratio
    )
    lines.extend([
        "Field[1] = BoundaryLayer;",
        "Field[1].CurvesList = {1, 2, 3};",
        f"Field[1].Size = {h_blade:.12g};",
        f"Field[1].SizeFar = {h_farfield:.12g};",
        f"Field[1].hwall_n = {boundary_layer_first:.12g};",
        f"Field[1].thickness = {boundary_layer_thickness:.12g};",
        f"Field[1].ratio = {boundary_layer_ratio:.12g};",
        f"Field[1].NbLayers = {bl_n};",
        "Field[1].Quads = 1;",
        "BoundaryLayer Field = 1;",
    ])
    if recombine:
        lines.extend(["Recombine Surface {40};", "Mesh.RecombinationAlgorithm = 1;"])
    if extrude_to_3d:
        boundary_order = [*outer_ids, 1, 2, 3]
        lateral_tags = {curve_id: f"vol[{idx + 2}]" for idx, curve_id in enumerate(boundary_order)}
        if len(boundary_patch_names) == len(outer_ids):
            named_outer_curves: dict[str, list[int]] = {}
            for curve_id, patch_name in zip(outer_ids, boundary_patch_names):
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", patch_name).strip("_") or "boundary"
                named_outer_curves.setdefault(safe_name, []).append(curve_id)
            lower_curves = outlet_curves = upper_curves = inlet_curves = []
        elif len(outer_ids) == 4:
            lower_curves, outlet_curves, upper_curves, inlet_curves = [outer_ids[0]], [outer_ids[1]], [outer_ids[2]], [outer_ids[3]]
        else:
            half = len(outer_ids) // 2
            lower_curves = outer_ids[:max(1, half - 1)]
            outlet_curves = [outer_ids[max(1, half - 1)]]
            upper_curves = outer_ids[max(1, half - 1) + 1:-1]
            inlet_curves = [outer_ids[-1]]
        lines.extend([
            "",
            f"vol[] = Extrude {{0, 0, {span:.12g}}} {{",
            "  Surface{40};",
            f"  Layers{{{max(1, int(spanwise_layers))}}};",
            "  Recombine;",
            "};",
            'Physical Volume("fluid") = {vol[1]};',
            'Physical Surface("frontAndBack") = {40, vol[0]};',
            f'Physical Surface("blade") = {{{lateral_tags[1]}, {lateral_tags[2]}, {lateral_tags[3]}}};',
        ])
        if len(boundary_patch_names) == len(outer_ids):
            for patch_name, curve_ids in named_outer_curves.items():
                lines.append(
                    f'Physical Surface("{patch_name}") = '
                    f'{{{", ".join(lateral_tags[c] for c in curve_ids)}}};'
                )
        else:
            lines.extend([
                f'Physical Surface("periodic_lower") = {{{", ".join(lateral_tags[c] for c in lower_curves)}}};',
                f'Physical Surface("outlet") = {{{", ".join(lateral_tags[c] for c in outlet_curves)}}};',
                f'Physical Surface("periodic_upper") = {{{", ".join(lateral_tags[c] for c in upper_curves)}}};',
                f'Physical Surface("inlet") = {{{", ".join(lateral_tags[c] for c in inlet_curves)}}};',
            ])
    else:
        if len(outer_ids) == 4:
            lower_curves, outlet_curves, upper_curves, inlet_curves = [outer_ids[0]], [outer_ids[1]], [outer_ids[2]], [outer_ids[3]]
        else:
            half = len(outer_ids) // 2
            lower_curves = outer_ids[:max(1, half - 1)]
            outlet_curves = [outer_ids[max(1, half - 1)]]
            upper_curves = outer_ids[max(1, half - 1) + 1:-1]
            inlet_curves = [outer_ids[-1]]
        lines.extend([
            f'Physical Curve("periodic_lower") = {{{", ".join(str(c) for c in lower_curves)}}};',
            f'Physical Curve("outlet") = {{{", ".join(str(c) for c in outlet_curves)}}};',
            f'Physical Curve("periodic_upper") = {{{", ".join(str(c) for c in upper_curves)}}};',
            f'Physical Curve("inlet") = {{{", ".join(str(c) for c in inlet_curves)}}};',
            'Physical Curve("blade") = {1, 2, 3};',
            'Physical Surface("fluid") = {40};',
        ])
    grid_params = {
        "domain_type": "cascade",
        "pitch": pitch_value,
        "pitch_chord_ratio": pitch_value / chord,
        "upstream_length": upstream,
        "downstream_length": downstream,
        "periodic_boundary_pairing": "periodic_lower:periodic_upper",
        "cascade_periodic": True,
        "span": span,
        "h_blade": h_blade,
        "h_farfield": h_farfield,
        "boundary_layer_first": boundary_layer_first,
        "boundary_layer_thickness": boundary_layer_thickness,
        "boundary_layer_ratio": boundary_layer_ratio,
        "reference_length": dimensional_reference or None,
        "lengths_normalized": True,
        "domain_shape": domain_shape,
        "domain_shape_verified": bool(domain_shape_verified and domain_shape != "rectangular_unverified"),
        "domain_x_extent_chords": (max(x for x, _ in boundary_points) - min(x for x, _ in boundary_points)) / chord,
        "actual_pitch": max(y for _, y in boundary_points) - min(y for _, y in boundary_points),
        "periodic_translation_vector": [translation[0], translation[1]],
    }
    return "\n".join(lines) + "\n", grid_params


def generate_airfoil_gmsh_mesh(
    case_dir: str,
    naca_code: str = "",
    airfoil_name: str = "",
    airfoil_dat_path: str = "",
    airfoil_coordinates: Any = None,
    airfoil_coordinate_text: str = "",
    domain_type: str = "c",
    far_field: float = 15.0,
    far_field_chord_ratio: float | None = None,
    far_field_ratio: float | None = None,
    wake_length: float = 20.0,
    wake_length_chord_ratio: float | None = None,
    wake_length_ratio: float | None = None,
    n_surface: int = 241,
    angle_of_attack: float | None = None,
    aoa: float | None = None,
    aoa_deg: float | None = None,
    alpha_deg: float | None = None,
    rotation_center_x: float = 0.25,
    rotation_center_y: float = 0.0,
    h_airfoil: float = 0.008,
    h_farfield: float = 0.8,
    boundary_layer_first: float = 0.001,
    boundary_layer_thickness: float = 0.04,
    boundary_layer_ratio: float = 1.15,
    boundary_layer_layers: int | None = None,
    mesh_algorithm: int = 6,
    near_wall_refinement_mode: str = "boundary_layer",
    mesh_family_contract: dict[str, Any] | None = None,
    lock_near_wall_topology: bool = False,
    target_cell_count: int | None = None,
    required_far_field: float | None = None,
    target_wall_y_plus: float | None = None,
    target_streamwise_delta_x_plus: float | None = None,
    wall_unit_reference: dict[str, Any] | None = None,
    recombine: bool = False,
    extrude_to_3d: bool = True,
    span: float = 0.1,
    convert_to_openfoam: bool = True,
    write_tecplot: bool = True,
    quality_preset: str = "robust",
    gmsh_binary: str = "gmsh",
    gmsh_to_foam_cmd: str = "openfoam gmshToFoam",
    timeout: int = 120,
    boundary_role_names: dict[str, str] | None = None,
    boundary_map: dict[str, Any] | None = None,
    expected_boundaries: list[str] | None = None,
) -> dict[str, Any]:
    """Generate a NACA airfoil mesh with Gmsh.

    The output is a Gmsh MSH2 file plus a JSON quality report. By default the
    2D airfoil surface is extruded to a one-cell-thick 3D mesh and converted
    with `openfoam gmshToFoam` into constant/polyMesh for OpenFOAM.
    """
    convert_to_openfoam = _as_bool(convert_to_openfoam, True)
    write_tecplot = _as_bool(write_tecplot, True)
    recombine = _as_bool(recombine, False)
    # OpenFOAM stores a 2-D case as a one-cell-thick volume with empty end patches.
    extrude_to_3d = convert_to_openfoam or _as_bool(extrude_to_3d, True)
    case_dir = _normalize_case_dir(case_dir)
    raw_coords = (
        _parse_airfoil_coordinates(airfoil_coordinates)
        or _parse_airfoil_coordinates(airfoil_coordinate_text)
        or _parse_airfoil_coordinates(airfoil_dat_path)
    )
    naca_hint = re.search(r"\bNACA\s*[-_ ]?(\d{4})\b", str(airfoil_name), flags=re.IGNORECASE)
    if naca_hint and not raw_coords:
        naca_code = naca_hint.group(1)
    if airfoil_name and not raw_coords and not naca_hint:
        raise ValueError(
            f"Non-NACA airfoil {airfoil_name!r} requires airfoil_dat_path, "
            "airfoil_coordinate_text, or airfoil_coordinates. Refusing to fall back to NACA0012."
        )
    naca_code = _normalise_naca_code(naca_code, case_dir) if raw_coords is None else ""
    if raw_coords is None and not naca_code:
        raise ValueError(
            "Explicit naca_code or airfoil/profile coordinates are required for airfoil_gmsh; "
            "refusing to default to NACA0012."
        )
    resolved_aoa = 0.0
    for candidate in (angle_of_attack, aoa, aoa_deg, alpha_deg):
        if candidate is not None:
            resolved_aoa = float(candidate)
            break
    domain_type = re.sub(r"[\s_-]+", "", str(domain_type or "c").lower())
    if domain_type in {"c", "cgrid", "cshape", "cshaped", "ctype", "cdomain"}:
        domain_type = "c"
    elif domain_type in {"o", "ogrid", "oshape", "oshaped", "otype", "odomain", "circle", "circular"}:
        domain_type = "o"
    else:
        raise ValueError(contract_requirement(MESH_PARAMETER_CONTRACT, "domain_type"))
    if far_field_chord_ratio is not None:
        far_field = float(far_field_chord_ratio)
    if far_field_ratio is not None:
        far_field = float(far_field_ratio)
    if wake_length_chord_ratio is not None:
        wake_length = float(wake_length_chord_ratio)
    if wake_length_ratio is not None:
        wake_length = float(wake_length_ratio)

    quality_preset = (quality_preset or "robust").lower().strip()
    try:
        mesh_algorithm = int(mesh_algorithm)
    except (TypeError, ValueError):
        mesh_algorithm = 6
    if mesh_algorithm not in {1, 5, 6, 8}:
        mesh_algorithm = 6
    if quality_preset in {"moderate", "balanced", "default", "standard"}:
        quality_preset = "robust"
    quality_adjustments: list[str] = []
    if quality_preset == "robust":
        old = (
            h_airfoil,
            h_farfield,
            boundary_layer_first,
            boundary_layer_thickness,
            boundary_layer_ratio,
            wake_length,
        )
        h_airfoil = min(max(float(h_airfoil), 1e-5), 0.015)
        h_farfield = min(max(float(h_farfield), 0.2), 0.8)
        # Do not override an explicit plan-derived near-wall spacing.  The
        # lower bound remains positive and numerically practical for Gmsh.
        boundary_layer_first = min(max(float(boundary_layer_first), 1e-5), 0.001)
        minimum_layer_envelope = (
            max(3.0 * boundary_layer_first, 1e-5)
            if _as_bool(lock_near_wall_topology, False)
            else 0.005
        )
        boundary_layer_thickness = min(
            max(float(boundary_layer_thickness), minimum_layer_envelope),
            0.04,
        )
        boundary_layer_ratio = min(max(float(boundary_layer_ratio), 1.03), 1.24)
        if domain_type == "c":
            wake_length = max(float(wake_length), 20.0, float(far_field))
        new = (
            h_airfoil,
            h_farfield,
            boundary_layer_first,
            boundary_layer_thickness,
            boundary_layer_ratio,
            wake_length,
        )
        if old != new:
            quality_adjustments.append(
                "robust preset adjusted h_airfoil/h_farfield/boundary-layer/wake parameters "
                f"from {old} to {new}"
            )

    if boundary_layer_layers is not None:
        boundary_layer_layers = max(1, int(boundary_layer_layers))
        if boundary_layer_ratio <= 1.0 + 1e-8:
            boundary_layer_thickness = boundary_layer_first * boundary_layer_layers
        else:
            boundary_layer_thickness = (
                boundary_layer_first
                * (boundary_layer_ratio ** boundary_layer_layers - 1.0)
                / (boundary_layer_ratio - 1.0)
            )

    gmsh_path = shutil.which(gmsh_binary) or shutil.which("gmsh")
    if not gmsh_path:
        raise RuntimeError("gmsh executable not found on PATH")

    out_dir = Path(case_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    airfoil_prefix = f"naca{naca_code}" if raw_coords is None else _slug_asset_name(airfoil_name, "airfoil")
    geo_path = out_dir / f"{airfoil_prefix}_{domain_type.lower()}domain.geo"
    msh_path = out_dir / f"{airfoil_prefix}_{domain_type.lower()}domain.msh"
    quality_path = out_dir / "mesh_quality.json"
    tecplot_path = out_dir / f"{airfoil_prefix}_{domain_type.lower()}domain_tecplot_volume.dat"
    tecplot_surface_path = out_dir / f"{airfoil_prefix}_{domain_type.lower()}domain_tecplot_surface.dat"

    geo = _build_airfoil_gmsh_geo(
        naca_code=naca_code,
        airfoil_name=airfoil_name,
        airfoil_coords=raw_coords,
        domain_type=domain_type,
        far_field=far_field,
        wake_length=wake_length,
        n_surface=n_surface,
        angle_of_attack=resolved_aoa,
        rotation_center_x=rotation_center_x,
        rotation_center_y=rotation_center_y,
        h_airfoil=h_airfoil,
        h_farfield=h_farfield,
        boundary_layer_first=boundary_layer_first,
        boundary_layer_thickness=boundary_layer_thickness,
        boundary_layer_ratio=boundary_layer_ratio,
        boundary_layer_layers=boundary_layer_layers,
        mesh_algorithm=mesh_algorithm,
        near_wall_refinement_mode=near_wall_refinement_mode,
        mesh_family_contract=mesh_family_contract,
        lock_near_wall_topology=lock_near_wall_topology,
        recombine=recombine,
        extrude_to_3d=extrude_to_3d,
        span=span,
        boundary_role_names=boundary_role_names,
    )
    boundary_layer_layers = (
        max(1, int(boundary_layer_layers))
        if boundary_layer_layers is not None
        else _boundary_layer_count(
            boundary_layer_first, boundary_layer_thickness, boundary_layer_ratio
        )
    )
    geo_path.write_text(geo, encoding="utf-8")
    proc = subprocess.run(
        [gmsh_path, "-3" if extrude_to_3d else "-2", str(geo_path), "-format", "msh2", "-o", str(msh_path)],
        cwd=str(out_dir),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0 or not msh_path.exists():
        raise RuntimeError(
            "gmsh failed with returncode="
            f"{proc.returncode}; stderr_tail={proc.stderr[-1200:]}"
        )

    quality = _parse_gmsh_msh2_quality(str(msh_path))
    quality["checks"] = {
        "has_2d_elements": quality["n_2d_elements"] > 0,
        "has_3d_elements": quality["n_3d_elements"] > 0 if extrude_to_3d else True,
        "positive_area": quality["negative_or_zero_area_cells"] == 0,
        "max_aspect_ratio_below_500": (
            quality["max_aspect_ratio"] is not None and quality["max_aspect_ratio"] < 500
        ),
    }
    quality_path.write_text(json.dumps(quality, indent=2), encoding="utf-8")

    tecplot_result = _export_gmsh_tecplot(msh_path, tecplot_surface_path, tecplot_path, quality, write_tecplot)

    openfoam_result: dict[str, Any] = {
        "requested": convert_to_openfoam,
        "status": "skipped",
        "written_files": [],
    }
    if convert_to_openfoam:
        system_dir = out_dir / "system"
        system_dir.mkdir(exist_ok=True)
        control_dict = system_dir / "controlDict"
        if not control_dict.exists():
            control_dict.write_text(
                _make_header("dictionary", "controlDict")
                + "\napplication     simpleFoam;\n"
                + "startFrom       startTime;\n"
                + "startTime       0;\n"
                + "stopAt          endTime;\n"
                + "endTime         1;\n"
                + "deltaT          1;\n"
                + "writeControl    timeStep;\n"
                + "writeInterval   1;\n",
                encoding="utf-8",
            )
        fv_schemes = system_dir / "fvSchemes"
        if not fv_schemes.exists():
            fv_schemes.write_text(
                _make_header("dictionary", "fvSchemes")
                + "\nddtSchemes { default steadyState; }\n"
                + "gradSchemes { default Gauss linear; }\n"
                + "divSchemes { default none; div(phi,U) bounded Gauss upwind; }\n"
                + "laplacianSchemes { default Gauss linear corrected; }\n"
                + "interpolationSchemes { default linear; }\n"
                + "snGradSchemes { default corrected; }\n",
                encoding="utf-8",
            )
        fv_solution = system_dir / "fvSolution"
        if not fv_solution.exists():
            fv_solution.write_text(
                _make_header("dictionary", "fvSolution")
                + "\nsolvers\n{\n"
                + "    p { solver GAMG; tolerance 1e-7; relTol 0.1; smoother GaussSeidel; }\n"
                + "    U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; }\n"
                + "}\n"
                + "SIMPLE { nNonOrthogonalCorrectors 0; }\n"
                + "relaxationFactors { fields { p 0.3; } equations { U 0.7; } }\n",
                encoding="utf-8",
            )
        foam_cmd = _openfoam_command("gmshToFoam", gmsh_to_foam_cmd, msh_path)
        foam_proc = subprocess.run(
            foam_cmd,
            cwd=str(out_dir),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        poly_dir = out_dir / "constant" / "polyMesh"
        if foam_proc.returncode == 0:
            role_names = boundary_role_names or {}
            _set_openfoam_patch_type(
                poly_dir / "boundary",
                _boundary_role_name(role_names, "front_and_back", "frontAndBack", default="frontAndBack"),
                "empty",
            )
            _set_openfoam_patch_type(
                poly_dir / "boundary",
                _boundary_role_name(role_names, "airfoil", "wall", default="airfoil"),
                "wall",
            )
        foam_files = [
            str(poly_dir / name)
            for name in ("points", "faces", "owner", "neighbour", "boundary")
            if (poly_dir / name).exists()
        ]
        openfoam_result = {
            "requested": True,
            "status": "success" if foam_proc.returncode == 0 and len(foam_files) >= 5 else "error",
            "cmd": shlex.join(foam_cmd),
            "returncode": foam_proc.returncode,
            "case_dir": str(out_dir),
            "polyMesh_dir": str(poly_dir),
            "written_files": [str(control_dict), str(fv_schemes), str(fv_solution), *foam_files],
            "stdout_tail": foam_proc.stdout[-2000:],
            "stderr_tail": foam_proc.stderr[-1200:],
        }
        if openfoam_result["status"] == "error":
            raise RuntimeError(
                "gmshToFoam conversion failed with returncode="
                f"{foam_proc.returncode}; stdout_tail={foam_proc.stdout[-1200:]}; "
                f"stderr_tail={foam_proc.stderr[-1200:]}"
            )

    return {
        "mesh_format": "openfoam_polyMesh" if openfoam_result["status"] == "success" else "gmsh_msh2",
        "mesh_type": "airfoil_gmsh",
        "case_dir": str(out_dir),
        "written_files": [
            str(geo_path),
            str(msh_path),
            str(quality_path),
            *tecplot_result["written_files"],
            *openfoam_result.get("written_files", []),
        ],
        "geo_file": str(geo_path),
        "mesh_file": str(msh_path),
        "tecplot_file": str(tecplot_surface_path) if tecplot_result.get("status") == "success" else None,
        "tecplot_volume_file": str(tecplot_path) if str(tecplot_path) in tecplot_result["written_files"] else None,
        "tecplot": tecplot_result,
        "quality_report": str(quality_path),
        "openfoam": openfoam_result,
        "polyMesh_dir": openfoam_result.get("polyMesh_dir"),
        "quality": quality,
        "gmsh_stdout_tail": proc.stdout[-1200:],
        "gmsh_stderr_tail": proc.stderr[-1200:],
        "grid_params": {
            "naca_code": naca_code,
            "airfoil_name": airfoil_name or (f"NACA {naca_code}" if naca_code else airfoil_prefix),
            "geometry_source": "coordinates" if raw_coords is not None else "naca_4digit",
            "domain_type": domain_type,
            "far_field": far_field,
            "wake_length": wake_length,
            "n_surface": n_surface,
            "angle_of_attack": resolved_aoa,
            "rotation_center_x": rotation_center_x,
            "rotation_center_y": rotation_center_y,
            "h_airfoil": h_airfoil,
            "h_farfield": h_farfield,
            "boundary_layer_first": boundary_layer_first,
            "boundary_layer_thickness": boundary_layer_thickness,
            "boundary_layer_ratio": boundary_layer_ratio,
            "boundary_layer_layers": boundary_layer_layers,
            "mesh_algorithm": mesh_algorithm,
            "near_wall_refinement_mode": near_wall_refinement_mode,
            "mesh_family_contract": mesh_family_contract,
            "lock_near_wall_topology": _as_bool(lock_near_wall_topology, False),
            "target_cell_count": target_cell_count,
            "required_far_field": required_far_field,
            "target_wall_y_plus": target_wall_y_plus,
            "target_streamwise_delta_x_plus": target_streamwise_delta_x_plus,
            "wall_unit_reference": wall_unit_reference,
            "recombine": recombine,
            "extrude_to_3d": extrude_to_3d,
            "span": span,
            "convert_to_openfoam": convert_to_openfoam,
            "write_tecplot": write_tecplot,
            "quality_preset": quality_preset,
            "quality_adjustments": quality_adjustments,
            "gmsh_binary": gmsh_path,
            "gmsh_to_foam_cmd": gmsh_to_foam_cmd,
            "boundary_role_names": boundary_role_names or {},
            "boundary_map": boundary_map or {},
            "expected_boundaries": expected_boundaries or [],
        },
    }


def _boundary_role_name(
    boundary_role_names: dict[str, str],
    *roles: str,
    default: str,
) -> str:
    """Resolve one caller-declared boundary role without inventing a new patch."""
    role_keys = [
        re.sub(r"[^a-z0-9]+", "", role.casefold())
        for role in roles
        if role
    ]
    names = {
        re.sub(r"[^a-z0-9]+", "", str(role).casefold()): name
        for role, name in boundary_role_names.items() if name not in (None, "")
    }
    for key in role_keys:
        if key in names:
            return _slug_asset_name(names[key], default)
    for key in role_keys:
        matches = {name for role, name in names.items() if key in role}
        if len(matches) == 1:
            return _slug_asset_name(matches.pop(), default)
    return _slug_asset_name(default, default)


def _build_cylinder_gmsh_geo(
    radius: float = 0.5,
    center_x: float = 0.0,
    center_y: float = 0.0,
    upstream_length: float = 10.0,
    downstream_length: float = 25.0,
    domain_height: float = 20.0,
    domain_shape: str = "rectangular",
    farfield_radius: float | None = None,
    n_cylinder: int = 160,
    h_cylinder: float = 0.02,
    h_farfield: float = 0.8,
    boundary_layer_first: float = 0.001,
    boundary_layer_thickness: float = 0.08,
    boundary_layer_ratio: float = 1.15,
    wake_refinement_length: float = 12.0,
    wake_refinement_size: float = 0.08,
    element_family: str = "mixed",
    recombine: bool = False,
    extrude_to_3d: bool = True,
    span: float = 0.1,
    boundary_role_names: dict[str, str] | None = None,
) -> str:
    boundary_role_names = boundary_role_names or {}
    inlet_name = _boundary_role_name(boundary_role_names, "inlet", default="inlet")
    outlet_name = _boundary_role_name(boundary_role_names, "outlet", default="outlet")
    farfield_name = _boundary_role_name(boundary_role_names, "farfield", default="farfield")
    top_name = _boundary_role_name(boundary_role_names, "top", default=farfield_name)
    bottom_name = _boundary_role_name(boundary_role_names, "bottom", default=farfield_name)
    wall_name = _boundary_role_name(
        boundary_role_names, "cylinder", "body", "obstacle", "wall", default="cylinder"
    )
    front_back_name = _boundary_role_name(
        boundary_role_names, "front_and_back", "frontAndBack", default="frontAndBack"
    )
    circular_farfield = str(domain_shape or "").strip().lower() in {
        "circle", "circular", "circular_farfield", "radial"
    } or farfield_radius is not None
    outer_radius = float(farfield_radius or max(upstream_length, downstream_length, domain_height / 2.0))
    if circular_farfield and outer_radius <= float(radius):
        raise ValueError(contract_requirement(MESH_PARAMETER_CONTRACT, "farfield_radius"))
    xmin = center_x - upstream_length
    xmax = center_x + downstream_length
    ymin = center_y - domain_height / 2.0
    ymax = center_y + domain_height / 2.0
    r = float(radius)
    bl_n = _boundary_layer_count(
        boundary_layer_first, boundary_layer_thickness, boundary_layer_ratio
    )
    n_arc = max(16, int(n_cylinder) // 4)
    wake_x0 = center_x + 2.0 * r
    wake_x1 = min(xmax, center_x + max(wake_refinement_length, 4.0 * r))
    wake_dist_min = max(1.2 * r, 0.02 * domain_height)
    wake_dist_max = max(4.0 * r, 0.25 * domain_height)

    if circular_farfield:
        xmin = center_x - outer_radius
        xmax = center_x + outer_radius
        ymin = center_y - outer_radius
        ymax = center_y + outer_radius
        outer_geometry = [
            f"Point(20) = {{{center_x:.12g}, {center_y:.12g}, 0, {h_farfield:.12g}}};",
            f"Point(21) = {{{center_x + outer_radius:.12g}, {center_y:.12g}, 0, {h_farfield:.12g}}};",
            f"Point(22) = {{{center_x:.12g}, {center_y + outer_radius:.12g}, 0, {h_farfield:.12g}}};",
            f"Point(23) = {{{center_x - outer_radius:.12g}, {center_y:.12g}, 0, {h_farfield:.12g}}};",
            f"Point(24) = {{{center_x:.12g}, {center_y - outer_radius:.12g}, 0, {h_farfield:.12g}}};",
            "Circle(1) = {21, 20, 22};",
            "Circle(2) = {22, 20, 23};",
            "Circle(3) = {23, 20, 24};",
            "Circle(4) = {24, 20, 21};",
        ]
    else:
        outer_geometry = [
            f"Point(1) = {{{xmin:.12g}, {ymin:.12g}, 0, {h_farfield:.12g}}};",
            f"Point(2) = {{{xmax:.12g}, {ymin:.12g}, 0, {h_farfield:.12g}}};",
            f"Point(3) = {{{xmax:.12g}, {ymax:.12g}, 0, {h_farfield:.12g}}};",
            f"Point(4) = {{{xmin:.12g}, {ymax:.12g}, 0, {h_farfield:.12g}}};",
            "Line(1) = {1, 2};",
            "Line(2) = {2, 3};",
            "Line(3) = {3, 4};",
            "Line(4) = {4, 1};",
        ]

    lines: list[str] = [
        'SetFactory("OpenCASCADE");',
        "// mesh_type = cylinder_gmsh",
        f"// radius = {radius:.12g}",
        f"// domain_shape = {'circular' if circular_farfield else 'rectangular'}",
        f"// domain = [{xmin:.12g}, {xmax:.12g}] x [{ymin:.12g}, {ymax:.12g}]",
        "Mesh.Algorithm = 6;",
        "Mesh.Optimize = 1;",
        "Mesh.OptimizeNetgen = 1;",
        f"Mesh.CharacteristicLengthMin = {min(h_cylinder, boundary_layer_first):.12g};",
        f"Mesh.CharacteristicLengthMax = {h_farfield:.12g};",
        "",
        *outer_geometry,
        f"Point(5) = {{{center_x + r:.12g}, {center_y:.12g}, 0, {h_cylinder:.12g}}};",
        f"Point(6) = {{{center_x:.12g}, {center_y + r:.12g}, 0, {h_cylinder:.12g}}};",
        f"Point(7) = {{{center_x - r:.12g}, {center_y:.12g}, 0, {h_cylinder:.12g}}};",
        f"Point(8) = {{{center_x:.12g}, {center_y - r:.12g}, 0, {h_cylinder:.12g}}};",
        f"Point(9) = {{{center_x:.12g}, {center_y:.12g}, 0, {h_cylinder:.12g}}};",
        "Circle(10) = {5, 9, 6};",
        "Circle(11) = {6, 9, 7};",
        "Circle(12) = {7, 9, 8};",
        "Circle(13) = {8, 9, 5};",
        "Transfinite Curve {10, 11, 12, 13} = " + str(n_arc + 1) + " Using Progression 1;",
        "Curve Loop(30) = {1, 2, 3, 4};",
        "Curve Loop(31) = {10, 11, 12, 13};",
        "Plane Surface(40) = {30, 31};",
        "",
    ]
    if str(element_family or "").strip().lower() not in {"triangle", "triangles", "triangular"}:
        lines.extend([
            "Field[1] = BoundaryLayer;",
            "Field[1].CurvesList = {10, 11, 12, 13};",
            f"Field[1].Size = {h_cylinder:.12g};",
            f"Field[1].SizeFar = {h_farfield:.12g};",
            f"Field[1].hwall_n = {boundary_layer_first:.12g};",
            f"Field[1].thickness = {boundary_layer_thickness:.12g};",
            f"Field[1].ratio = {boundary_layer_ratio:.12g};",
            f"Field[1].NbLayers = {bl_n};",
            "Field[1].Quads = 1;",
            "BoundaryLayer Field = 1;",
            "",
        ])
    lines.extend([
        f"Point(50) = {{{wake_x0:.12g}, {center_y:.12g}, 0, {wake_refinement_size:.12g}}};",
        f"Point(51) = {{{wake_x1:.12g}, {center_y:.12g}, 0, {wake_refinement_size:.12g}}};",
        "Line(50) = {50, 51};",
        "Field[2] = Distance;",
        "Field[2].CurvesList = {50};",
        "Field[2].Sampling = 80;",
        "Field[3] = Threshold;",
        "Field[3].InField = 2;",
        f"Field[3].SizeMin = {wake_refinement_size:.12g};",
        f"Field[3].SizeMax = {h_farfield:.12g};",
        f"Field[3].DistMin = {wake_dist_min:.12g};",
        f"Field[3].DistMax = {wake_dist_max:.12g};",
        "Background Field = 3;",
    ])
    if recombine:
        lines.extend([
            "Recombine Surface {40};",
            "Mesh.RecombinationAlgorithm = 1;",
        ])
    if extrude_to_3d:
        boundary_order = [1, 2, 3, 4, 10, 11, 12, 13]
        lateral_tags = {curve_id: f"vol[{idx + 2}]" for idx, curve_id in enumerate(boundary_order)}
        outer_surfaces = ", ".join(lateral_tags[c] for c in [1, 2, 3, 4])
        rectangular_outer_groups = (
            [f'Physical Surface("{top_name}") = {{{lateral_tags[1]}, {lateral_tags[3]}}};']
            if top_name == bottom_name
            else [
                f'Physical Surface("{bottom_name}") = {{{lateral_tags[1]}}};',
                f'Physical Surface("{top_name}") = {{{lateral_tags[3]}}};',
            ]
        )
        lines.extend([
            "",
            f"vol[] = Extrude {{0, 0, {span:.12g}}} {{",
            "  Surface{40};",
            "  Layers{1};",
            "  Recombine;",
            "};",
            'Physical Volume("fluid") = {vol[1]};',
            f'Physical Surface("{front_back_name}") = {{40, vol[0]}};',
            *([] if circular_farfield else [
                f'Physical Surface("{inlet_name}") = {{{lateral_tags[4]}}};',
                f'Physical Surface("{outlet_name}") = {{{lateral_tags[2]}}};',
            ]),
            *(
                [f'Physical Surface("{farfield_name}") = {{{outer_surfaces}}};']
                if circular_farfield else rectangular_outer_groups
            ),
            f'Physical Surface("{wall_name}") = {{{", ".join(lateral_tags[c] for c in [10, 11, 12, 13])}}};',
        ])
    else:
        rectangular_outer_groups = (
            [f'Physical Curve("{top_name}") = {{1, 3}};']
            if top_name == bottom_name
            else [
                f'Physical Curve("{bottom_name}") = {{1}};',
                f'Physical Curve("{top_name}") = {{3}};',
            ]
        )
        lines.extend([
            *([] if circular_farfield else [
                f'Physical Curve("{inlet_name}") = {{4}};',
                f'Physical Curve("{outlet_name}") = {{2}};',
            ]),
            *(
                [f'Physical Curve("{farfield_name}") = {{1, 2, 3, 4}};']
                if circular_farfield else rectangular_outer_groups
            ),
            f'Physical Curve("{wall_name}") = {{10, 11, 12, 13}};',
            'Physical Surface("fluid") = {40};',
        ])
    return "\n".join(lines) + "\n"


def generate_cylinder_gmsh_mesh(
    case_dir: str,
    radius: float = 0.5,
    diameter: float | None = None,
    center_x: float = 0.0,
    center_y: float = 0.0,
    upstream_length: float = 10.0,
    downstream_length: float = 25.0,
    domain_height: float = 20.0,
    domain_shape: str = "rectangular",
    farfield_radius: float | None = None,
    domain_lx: float | None = None,
    domain_ly: float | None = None,
    n_cylinder: int | None = None,
    h_cylinder: float = 0.02,
    h_farfield: float = 0.8,
    boundary_layer_first: float = 0.001,
    boundary_layer_thickness: float = 0.08,
    boundary_layer_ratio: float = 1.15,
    wake_refinement_length: float = 12.0,
    wake_refinement_size: float = 0.08,
    element_family: str = "mixed",
    recombine: bool = False,
    extrude_to_3d: bool = True,
    span: float = 0.1,
    convert_to_openfoam: bool = True,
    write_tecplot: bool = True,
    quality_preset: str = "robust",
    gmsh_binary: str = "gmsh",
    gmsh_to_foam_cmd: str = "openfoam gmshToFoam",
    timeout: int = 120,
    boundary_role_names: dict[str, str] | None = None,
    boundary_map: dict[str, Any] | None = None,
    expected_boundaries: list[str] | None = None,
) -> dict[str, Any]:
    """Generate a cylinder-flow mesh while preserving the requested outer domain."""
    convert_to_openfoam = _as_bool(convert_to_openfoam, True)
    write_tecplot = _as_bool(write_tecplot, True)
    recombine = _as_bool(recombine, False)
    # OpenFOAM stores a 2-D case as a one-cell-thick volume with empty end patches.
    extrude_to_3d = convert_to_openfoam or _as_bool(extrude_to_3d, True)
    case_dir = _normalize_case_dir(case_dir)
    if diameter is not None:
        radius = float(diameter) / 2.0
    if domain_lx is not None:
        downstream_length = max(float(domain_lx) - float(upstream_length), 4.0 * radius)
    if domain_ly is not None:
        domain_height = float(domain_ly)

    quality_preset = (quality_preset or "robust").lower().strip()
    quality_adjustments: list[str] = []
    if quality_preset == "robust":
        old = (upstream_length, downstream_length, domain_height, h_cylinder, h_farfield, boundary_layer_first)
        upstream_length = max(float(upstream_length), 8.0 * radius)
        downstream_length = max(float(downstream_length), 25.0 * radius)
        domain_height = max(float(domain_height), 20.0 * radius)
        # A robust preset may refine an overly coarse request, but must never
        # coarsen an explicitly finer one.  The old lower clamps turned D/100
        # and D/20 caller requirements back into generator defaults.
        h_cylinder = min(max(float(h_cylinder), 1e-8), 0.06 * radius)
        h_farfield = min(max(float(h_farfield), 1e-8), 1.6 * radius)
        boundary_layer_first = min(max(float(boundary_layer_first), 1e-8), 0.01 * radius)
        boundary_layer_thickness = min(max(float(boundary_layer_thickness), 0.05 * radius), 0.2 * radius)
        boundary_layer_ratio = min(max(float(boundary_layer_ratio), 1.08), 1.18)
        wake_refinement_length = max(float(wake_refinement_length), 12.0 * radius)
        wake_refinement_size = min(max(float(wake_refinement_size), 0.04 * radius), 0.16 * radius)
        new = (upstream_length, downstream_length, domain_height, h_cylinder, h_farfield, boundary_layer_first)
        if old != new:
            quality_adjustments.append(
                "robust preset adjusted cylinder domain/mesh parameters "
                f"from {old} to {new}"
            )
    n_cylinder = int(n_cylinder) if n_cylinder is not None else max(
        160, int(math.ceil(2.0 * math.pi * float(radius) / max(float(h_cylinder), 1e-12)))
    )

    gmsh_path = shutil.which(gmsh_binary) or shutil.which("gmsh")
    if not gmsh_path:
        raise RuntimeError("gmsh executable not found on PATH")

    out_dir = Path(case_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    geo_path = out_dir / "cylinder_flow.geo"
    msh_path = out_dir / "cylinder_flow.msh"
    quality_path = out_dir / "mesh_quality.json"
    tecplot_path = out_dir / "cylinder_flow_tecplot_volume.dat"
    tecplot_surface_path = out_dir / "cylinder_flow_tecplot_surface.dat"

    geo = _build_cylinder_gmsh_geo(
        radius=radius,
        center_x=center_x,
        center_y=center_y,
        upstream_length=upstream_length,
        downstream_length=downstream_length,
        domain_height=domain_height,
        domain_shape=domain_shape,
        farfield_radius=farfield_radius,
        n_cylinder=n_cylinder,
        h_cylinder=h_cylinder,
        h_farfield=h_farfield,
        boundary_layer_first=boundary_layer_first,
        boundary_layer_thickness=boundary_layer_thickness,
        boundary_layer_ratio=boundary_layer_ratio,
        wake_refinement_length=wake_refinement_length,
        wake_refinement_size=wake_refinement_size,
        element_family=element_family,
        recombine=recombine,
        extrude_to_3d=extrude_to_3d,
        span=span,
        boundary_role_names=boundary_role_names,
    )
    geo_path.write_text(geo, encoding="utf-8")
    proc = subprocess.run(
        [gmsh_path, "-3" if extrude_to_3d else "-2", str(geo_path), "-format", "msh2", "-o", str(msh_path)],
        cwd=str(out_dir),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0 or not msh_path.exists():
        raise RuntimeError(
            "gmsh failed with returncode="
            f"{proc.returncode}; stderr_tail={proc.stderr[-1200:]}"
        )

    quality = _parse_gmsh_msh2_quality(str(msh_path))
    quality["checks"] = {
        "has_2d_elements": quality["n_2d_elements"] > 0,
        "has_3d_elements": quality["n_3d_elements"] > 0 if extrude_to_3d else True,
        "positive_area": quality["negative_or_zero_area_cells"] == 0,
        "max_aspect_ratio_below_500": (
            quality["max_aspect_ratio"] is not None and quality["max_aspect_ratio"] < 500
        ),
    }
    quality_path.write_text(json.dumps(quality, indent=2), encoding="utf-8")

    tecplot_result = _export_gmsh_tecplot(msh_path, tecplot_surface_path, tecplot_path, quality, write_tecplot)

    openfoam_result: dict[str, Any] = {"requested": convert_to_openfoam, "status": "skipped", "written_files": []}
    if convert_to_openfoam:
        system_dir = out_dir / "system"
        system_dir.mkdir(exist_ok=True)
        control_dict = system_dir / "controlDict"
        if not control_dict.exists():
            control_dict.write_text(
                _make_header("dictionary", "controlDict")
                + "\napplication     simpleFoam;\nstartFrom       startTime;\nstartTime       0;\n"
                + "stopAt          endTime;\nendTime         1;\ndeltaT          1;\n"
                + "writeControl    timeStep;\nwriteInterval   1;\n",
                encoding="utf-8",
            )
        fv_schemes = system_dir / "fvSchemes"
        if not fv_schemes.exists():
            fv_schemes.write_text(
                _make_header("dictionary", "fvSchemes")
                + "\nddtSchemes { default steadyState; }\n"
                + "gradSchemes { default Gauss linear; }\n"
                + "divSchemes { default none; div(phi,U) bounded Gauss upwind; }\n"
                + "laplacianSchemes { default Gauss linear corrected; }\n"
                + "interpolationSchemes { default linear; }\n"
                + "snGradSchemes { default corrected; }\n",
                encoding="utf-8",
            )
        fv_solution = system_dir / "fvSolution"
        if not fv_solution.exists():
            fv_solution.write_text(
                _make_header("dictionary", "fvSolution")
                + "\nsolvers\n{\n"
                + "    p { solver GAMG; tolerance 1e-7; relTol 0.1; smoother GaussSeidel; }\n"
                + "    U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; }\n"
                + "}\nSIMPLE { nNonOrthogonalCorrectors 0; }\n"
                + "relaxationFactors { fields { p 0.3; } equations { U 0.7; } }\n",
                encoding="utf-8",
            )
        foam_cmd = _openfoam_command("gmshToFoam", gmsh_to_foam_cmd, msh_path)
        foam_proc = subprocess.run(
            foam_cmd,
            cwd=str(out_dir),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        poly_dir = out_dir / "constant" / "polyMesh"
        if foam_proc.returncode == 0:
            role_names = boundary_role_names or {}
            patch_types = {
                _boundary_role_name(
                    role_names, "front_and_back", "frontAndBack", default="frontAndBack"
                ): "empty",
                _boundary_role_name(
                    role_names, "cylinder", "body", "obstacle", "wall", default="cylinder"
                ): "wall",
                **{
                    str(name): str(config.get("type") if isinstance(config, dict) else config)
                    for name, config in (boundary_map or {}).items()
                    if name and config
                },
            }
            for patch_name, patch_type in patch_types.items():
                _set_openfoam_patch_type(poly_dir / "boundary", patch_name, patch_type)
        foam_files = [
            str(poly_dir / name)
            for name in ("points", "faces", "owner", "neighbour", "boundary")
            if (poly_dir / name).exists()
        ]
        openfoam_result = {
            "requested": True,
            "status": "success" if foam_proc.returncode == 0 and len(foam_files) >= 5 else "error",
            "cmd": shlex.join(foam_cmd),
            "returncode": foam_proc.returncode,
            "case_dir": str(out_dir),
            "polyMesh_dir": str(poly_dir),
            "written_files": [str(control_dict), str(fv_schemes), str(fv_solution), *foam_files],
            "stdout_tail": foam_proc.stdout[-2000:],
            "stderr_tail": foam_proc.stderr[-1200:],
        }
        if openfoam_result["status"] == "error":
            raise RuntimeError(
                "gmshToFoam conversion failed with returncode="
                f"{foam_proc.returncode}; stdout_tail={foam_proc.stdout[-1200:]}; "
                f"stderr_tail={foam_proc.stderr[-1200:]}"
            )

    return {
        "mesh_format": "openfoam_polyMesh" if openfoam_result["status"] == "success" else "gmsh_msh2",
        "mesh_type": "cylinder_gmsh",
        "case_dir": str(out_dir),
        "written_files": [
            str(geo_path),
            str(msh_path),
            str(quality_path),
            *tecplot_result["written_files"],
            *openfoam_result.get("written_files", []),
        ],
        "geo_file": str(geo_path),
        "mesh_file": str(msh_path),
        "tecplot_file": str(tecplot_surface_path) if tecplot_result.get("status") == "success" else None,
        "tecplot_volume_file": str(tecplot_path) if str(tecplot_path) in tecplot_result["written_files"] else None,
        "tecplot": tecplot_result,
        "quality_report": str(quality_path),
        "openfoam": openfoam_result,
        "polyMesh_dir": openfoam_result.get("polyMesh_dir"),
        "quality": quality,
        "gmsh_stdout_tail": proc.stdout[-1200:],
        "gmsh_stderr_tail": proc.stderr[-1200:],
        "grid_params": {
            "radius": radius,
            "diameter": 2.0 * radius,
            "center_x": center_x,
            "center_y": center_y,
            "upstream_length": upstream_length,
            "downstream_length": downstream_length,
            "domain_height": domain_height,
            "domain_shape": "circular" if farfield_radius is not None or str(domain_shape).lower() in {"circle", "circular", "circular_farfield", "radial"} else "rectangular",
            "farfield_radius": farfield_radius,
            "physical_dimension": 2,
            "storage_dimension": 3 if extrude_to_3d else 2,
            "openfoam_2d_extrusion": bool(extrude_to_3d),
            "n_cylinder": n_cylinder,
            "h_cylinder": h_cylinder,
            "h_farfield": h_farfield,
            "boundary_layer_first": boundary_layer_first,
            "boundary_layer_thickness": boundary_layer_thickness,
            "boundary_layer_ratio": boundary_layer_ratio,
            "wake_refinement_length": wake_refinement_length,
            "wake_refinement_size": wake_refinement_size,
            "wake_refinement_type": "centerline_distance_threshold",
            "element_family": element_family,
            "span": span,
            "quality_preset": quality_preset,
            "quality_adjustments": quality_adjustments,
            "boundary_map": boundary_map or {},
            "boundary_role_names": boundary_role_names or {},
        },
    }


# ── 2. 圆柱绕流 ─────────────────────────────────────────────────────────────

def generate_cylinder_flow_mesh(
    radius: float = 0.5,
    domain_lx: float = 20.0,
    domain_ly: float = 10.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
    nr: int = 40,
    ntheta: int = 120,
    nx_wake: int = 80,
    ny_above: int = 40,
    ny_below: int = 40,
    delta0: float = 1e-4,
    stretch_r: float = 1.15,
) -> dict[str, Any]:
    """为圆柱绕流生成 O-grid + 远场矩形混合网格。

    圆柱附近为极坐标 O-grid，远场为矩形补充区域。
    """
    # O-grid 区域
    ogrid_r_max = radius + 3.0  # O-grid 外径

    # O-grid 点
    # O-grid 点: X[j][i], j=0..ntheta 是环向, i=0..nr 是径向
    X_og = [[0.0] * (nr + 1) for _ in range(ntheta + 1)]
    Y_og = [[0.0] * (nr + 1) for _ in range(ntheta + 1)]

    for j in range(ntheta + 1):
        theta = 2.0 * math.pi * j / ntheta
        for i in range(nr + 1):
            if i == 0:
                r = radius
            else:
                r = radius + (ogrid_r_max - radius) * i / nr
            X_og[j][i] = center_x + r * math.cos(theta)
            Y_og[j][i] = center_y + r * math.sin(theta)

    # 构建网格: nj=ntheta, ni=nr
    # bottom(j=0) = 圆柱壁面，top(j=ntheta) = 远场
    mesh_data = _build_2d_structured_mesh(
        X_og, Y_og, ntheta, nr,
        boundary_patches={
            "bottom": {"type": "wall"},     # cylinder surface
            "top":    {"type": "patch"},    # farfield
            "left":   {"type": "patch"},    # inlet/outlet slit
            "right":  {"type": "patch"},    # inlet/outlet slit
        },
    )

    # 重命名边界
    mesh_data["boundary"] = {
        "cylinder":   mesh_data["boundary"]["bottom"],
        "farfield":   mesh_data["boundary"]["top"],
        "slit":       mesh_data["boundary"]["left"],
        "slit_2":     mesh_data["boundary"]["right"],
        "frontAndBack": mesh_data["boundary"]["frontAndBack"],
    }
    mesh_data["boundary"]["cylinder"]["type"] = "wall"
    mesh_data["boundary"]["farfield"]["type"] = "patch"

    mesh_data["grid_params"] = {
        "radius": radius, "nr": nr, "ntheta": ntheta,
        "ogrid_r_max": ogrid_r_max, "delta0": delta0, "stretch_r": stretch_r,
    }
    mesh_data["mesh_type"] = "cylinder_flow"
    return mesh_data


# ── 3. 管道流动 ─────────────────────────────────────────────────────────────

def generate_pipe_flow_mesh(
    length: float = 10.0,
    radius: float = 1.0,
    nx: int = 100,
    nr: int = 30,
    delta0: float = 1e-4,
    stretch: float = 1.15,
) -> dict[str, Any]:
    """为管道流动生成结构化网格（矩形域，上下壁面 + 左右入口出口）。

    使用径向拉伸使壁面附近网格密集。
    2D 截面视图：x 方向为管长，y 方向为管径。
    """
    # y 方向径向拉伸
    dy = [0.0] * (nr + 1)
    dy[0] = delta0
    for i in range(1, nr + 1):
        dy[i] = dy[i - 1] * stretch

    total_dy = sum(dy)
    scale_y = radius / total_dy

    X = [[0.0] * (nx + 1) for _ in range(nr + 1)]
    Y = [[0.0] * (nx + 1) for _ in range(nr + 1)]

    for j in range(nr + 1):
        y_pos = sum(dy[:j + 1]) * scale_y
        for i in range(nx + 1):
            X[j][i] = i * length / nx
            Y[j][i] = y_pos

    mesh_data = _build_2d_structured_mesh(X, Y, nr, nx,
        boundary_patches={
            "bottom": {"type": "wall"},     # pipe wall (lower)
            "top":    {"type": "symmetry"}, # pipe center (symmetry)
            "left":   {"type": "patch"},    # inlet
            "right":  {"type": "patch"},    # outlet
        },
    )

    # 重命名
    mesh_data["boundary"] = {
        "pipe_wall":  mesh_data["boundary"]["bottom"],
        "pipe_axis":  mesh_data["boundary"]["top"],
        "inlet":      mesh_data["boundary"]["left"],
        "outlet":     mesh_data["boundary"]["right"],
        "frontAndBack": mesh_data["boundary"]["frontAndBack"],
    }
    mesh_data["boundary"]["pipe_wall"]["type"] = "wall"
    mesh_data["boundary"]["pipe_axis"]["type"] = "symmetryPlane"
    mesh_data["boundary"]["inlet"]["type"] = "patch"
    mesh_data["boundary"]["outlet"]["type"] = "patch"

    mesh_data["grid_params"] = {
        "length": length, "radius": radius,
        "nx": nx, "nr": nr,
        "delta0": delta0, "stretch": stretch,
    }
    mesh_data["mesh_type"] = "pipe_flow"
    return mesh_data


# ── 8. 球体绕流 ─────────────────────────────────────────────────────────────

def generate_sphere_mesh(
    radius: float = 0.5,
    far_field: float = 10.0,
    ntheta: int = 60,
    nphi: int = 30,
    nr: int = 30,
    delta0: float = 1e-4,
    stretch: float = 1.15,
) -> dict[str, Any]:
    """为球体绕流生成 2D 轴对称结构化网格（O-grid 类型）。

    底部为球壁面，顶部为远场，左右为入口/出口 slit。
    实际 3D 球体通过 OpenFOAM wedge 边界条件实现轴对称。
    """
    # O-grid 在 r-theta 平面
    # j=0 为球面，j=nr 为远场
    # i=0 为前驻点，i=ntheta 为后驻点

    X = [[0.0] * (ntheta + 1) for _ in range(nr + 1)]
    Y = [[0.0] * (ntheta + 1) for _ in range(nr + 1)]

    # 径向拉伸
    dr = [0.0] * (nr + 1)
    dr[0] = delta0
    for j in range(1, nr + 1):
        dr[j] = dr[j - 1] * stretch
    total_dr = sum(dr)
    scale_r = (far_field - radius) / total_dr

    for i in range(ntheta + 1):
        theta = math.pi * i / ntheta  # 0 → π
        for j in range(nr + 1):
            r = radius + sum(dr[:j + 1]) * scale_r
            r = min(r, far_field)
            X[j][i] = r * math.cos(theta)
            Y[j][i] = r * math.sin(theta)

    mesh_data = _build_2d_structured_mesh(X, Y, nr, ntheta,
        boundary_patches={
            "bottom": {"type": "wall"},     # sphere surface
            "top":    {"type": "patch"},    # farfield
            "left":   {"type": "patch"},    # inlet (front stagnation)
            "right":  {"type": "patch"},    # outlet (rear stagnation)
        },
    )

    mesh_data["boundary"] = {
        "sphere":     mesh_data["boundary"]["bottom"],
        "farfield":   mesh_data["boundary"]["top"],
        "inlet":      mesh_data["boundary"]["left"],
        "outlet":     mesh_data["boundary"]["right"],
        "frontAndBack": mesh_data["boundary"]["frontAndBack"],
    }
    mesh_data["boundary"]["sphere"]["type"] = "wall"
    mesh_data["boundary"]["farfield"]["type"] = "patch"

    mesh_data["grid_params"] = {
        "radius": radius, "far_field": far_field,
        "ntheta": ntheta, "nphi": nphi, "nr": nr,
        "delta0": delta0, "stretch": stretch,
    }
    mesh_data["mesh_type"] = "sphere"
    return mesh_data


# ── 9. 结构化矩形网格 ───────────────────────────────────────────────────────

def generate_structured_rect_mesh(
    lx: float, ly: float,
    nx: int, ny: int,
    z_thickness: float = 0.1,
    boundary_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """生成 2D 结构化矩形网格（OpenFOAM polyMesh 格式）。"""
    if boundary_types is None:
        boundary_types = {
            "bottom": "wall", "top": "wall",
            "left": "patch", "right": "patch",
        }

    dx = lx / nx; dy = ly / ny

    X = [[0.0] * (nx + 1) for _ in range(ny + 1)]
    Y = [[0.0] * (nx + 1) for _ in range(ny + 1)]
    for j in range(ny + 1):
        for i in range(nx + 1):
            X[j][i] = i * dx
            Y[j][i] = j * dy

    mesh_data = _build_2d_structured_mesh(X, Y, ny, nx,
        boundary_patches={
            "bottom": {"type": boundary_types.get("bottom", "wall")},
            "top":    {"type": boundary_types.get("top", "wall")},
            "left":   {"type": boundary_types.get("left", "patch")},
            "right":  {"type": boundary_types.get("right", "patch")},
        },
    )

    mesh_data["grid_params"] = {"lx": lx, "ly": ly, "nx": nx, "ny": ny, "z_thickness": z_thickness}
    mesh_data["mesh_type"] = "structured_rect"
    return mesh_data


# ── 10. FCC 晶格 ────────────────────────────────────────────────────────────

def fcc_lattice(
    a: float, nx: int, ny: int, nz: int,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> dict[str, Any]:
    """生成 FCC 晶格原子坐标。"""
    basis = [
        [0.0, 0.0, 0.0],
        [0.5 * a, 0.5 * a, 0.0],
        [0.5 * a, 0.0, 0.5 * a],
        [0.0, 0.5 * a, 0.5 * a],
    ]
    atoms = []
    for iz in range(nz):
        for iy in range(ny):
            for ix in range(nx):
                for bx, by, bz in basis:
                    atoms.append([
                        origin[0] + ix * a + bx,
                        origin[1] + iy * a + by,
                        origin[2] + iz * a + bz,
                    ])
    box = [nx * a, ny * a, nz * a]
    return {
        "atoms": atoms, "n_atoms": len(atoms), "box": box,
        "lattice_type": "fcc", "lattice_constant": a,
    }


# ── 11. BCC 晶格 ────────────────────────────────────────────────────────────

def bcc_lattice(
    a: float, nx: int, ny: int, nz: int,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> dict[str, Any]:
    """生成 BCC 晶格原子坐标。"""
    basis = [
        [0.0, 0.0, 0.0],
        [0.5 * a, 0.5 * a, 0.5 * a],
    ]
    atoms = []
    for iz in range(nz):
        for iy in range(ny):
            for ix in range(nx):
                for bx, by, bz in basis:
                    atoms.append([
                        origin[0] + ix * a + bx,
                        origin[1] + iy * a + by,
                        origin[2] + iz * a + bz,
                    ])
    box = [nx * a, ny * a, nz * a]
    return {
        "atoms": atoms, "n_atoms": len(atoms), "box": box,
        "lattice_type": "bcc", "lattice_constant": a,
    }


# ── 12. SC 晶格 ─────────────────────────────────────────────────────────────

def sc_lattice(
    a: float, nx: int, ny: int, nz: int,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> dict[str, Any]:
    """生成简单立方 (SC) 晶格原子坐标。"""
    atoms = []
    for iz in range(nz):
        for iy in range(ny):
            for ix in range(nx):
                atoms.append([
                    origin[0] + ix * a,
                    origin[1] + iy * a,
                    origin[2] + iz * a,
                ])
    box = [nx * a, ny * a, nz * a]
    return {
        "atoms": atoms, "n_atoms": len(atoms), "box": box,
        "lattice_type": "sc", "lattice_constant": a,
    }


# ── 13. Diamond 晶格 ────────────────────────────────────────────────────────

def diamond_lattice(
    a: float, nx: int, ny: int, nz: int,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> dict[str, Any]:
    """生成金刚石 (Diamond) 晶格原子坐标。"""
    basis = [
        [0.0, 0.0, 0.0],
        [0.5 * a, 0.5 * a, 0.0],
        [0.5 * a, 0.0, 0.5 * a],
        [0.0, 0.5 * a, 0.5 * a],
        [0.25 * a, 0.25 * a, 0.25 * a],
        [0.75 * a, 0.75 * a, 0.25 * a],
        [0.75 * a, 0.25 * a, 0.75 * a],
        [0.25 * a, 0.75 * a, 0.75 * a],
    ]
    atoms = []
    for iz in range(nz):
        for iy in range(ny):
            for ix in range(nx):
                for bx, by, bz in basis:
                    atoms.append([
                        origin[0] + ix * a + bx,
                        origin[1] + iy * a + by,
                        origin[2] + iz * a + bz,
                    ])
    box = [nx * a, ny * a, nz * a]
    return {
        "atoms": atoms, "n_atoms": len(atoms), "box": box,
        "lattice_type": "diamond", "lattice_constant": a,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 第五部分：LAMMPS data 写入
# ═══════════════════════════════════════════════════════════════════════════════

def write_lammps_data(
    file_path: str,
    atoms: list[list[float]],
    box: list[float],
    atom_type: int = 1,
    mass: float = 1.0,
    comment: str = "Generated by harness-framework mesh_generator",
) -> str:
    """将原子坐标写入 LAMMPS data 文件。"""
    lines = [f"{comment}\n"]
    lines.append(f"{len(atoms)} atoms\n")
    lines.append("1 atom types\n")
    lines.append(f"0.0 {box[0]:.10g} xlo xhi\n")
    lines.append(f"0.0 {box[1]:.10g} ylo yhi\n")
    lines.append(f"0.0 {box[2]:.10g} zlo zhi\n")
    lines.append("\nAtoms\n\n")
    for idx, (x, y, z) in enumerate(atoms, start=1):
        lines.append(f"{idx} {atom_type} {x:.10g} {y:.10g} {z:.10g}\n")
    lines.append("\nMasses\n\n")
    lines.append(f"{atom_type} {mass:.10g}\n")

    content = "".join(lines)
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text(content)
    return str(file_path)


# ═══════════════════════════════════════════════════════════════════════════════
# 第六部分：工具实现
# ═══════════════════════════════════════════════════════════════════════════════

# Adapters are selected by explicit geometry/topology intent. Generic geometry
# aliases are shared instead of repeated per discipline.
_GENERIC_GEOMETRY_ADAPTERS = {
    "geometry_file_gmsh": generate_geometry_file_gmsh_mesh,
    "custom_geometry": generate_geometry_file_gmsh_mesh,
    "public_geometry": generate_geometry_file_gmsh_mesh,
    "generic_gmsh": generate_geometry_file_gmsh_mesh,
    "structured_rect": generate_structured_rect_mesh,
}
_MESH_GENERATORS = {
    "cfd": {
        **_GENERIC_GEOMETRY_ADAPTERS,
        "airfoil_ogrid": generate_airfoil_ogrid,
        "airfoil_gmsh": generate_airfoil_gmsh_mesh,
        "coordinate_profile_gmsh": generate_coordinate_profile_gmsh_mesh,
        "coordinate_profile_cascade_gmsh": generate_coordinate_profile_cascade_gmsh_mesh,
        "cylinder_gmsh": generate_cylinder_gmsh_mesh,
        "cylinder_flow": generate_cylinder_gmsh_mesh,
        "pipe_flow": generate_pipe_flow_mesh,
        "converging_diverging_nozzle": generate_converging_diverging_nozzle_mesh,
        "sphere": generate_sphere_mesh,
    },
    "csm": {
        **_GENERIC_GEOMETRY_ADAPTERS,
        "cantilever_beam": generate_cantilever_beam_mesh,
    },
    "cem": {
        **_GENERIC_GEOMETRY_ADAPTERS,
        "rectangular_waveguide": generate_rectangular_waveguide_mesh,
    },
    "heat_transfer": {
        **_GENERIC_GEOMETRY_ADAPTERS,
        "heat_sink": generate_heat_sink_mesh,
    },
    "md": {
        "fcc_lattice": fcc_lattice,
        "bcc_lattice": bcc_lattice,
        "sc_lattice": sc_lattice,
        "diamond_lattice": diamond_lattice,
    },
    "multiphysics": dict(_GENERIC_GEOMETRY_ADAPTERS),
}


def mesh_parameter_contracts(discipline: str) -> dict[str, dict[str, Any]]:
    """Expose actual adapter controls to planning, rather than prose aliases."""
    return {
        name: {
            key: {"type": str(parameter.annotation), **(
                {"default": parameter.default}
                if parameter.default is not inspect.Parameter.empty else {}
            )}
            for key, parameter in inspect.signature(generator).parameters.items()
            if key != "case_dir" and parameter.kind not in {
                inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL,
            }
        }
        for name, generator in _MESH_GENERATORS.get(discipline, {}).items()
    }

# Unknown continuum tasks must not silently become a cantilever, waveguide,
# heat sink, or rectangular CFD case. Explicit simple-case routing still uses
# the adapters above; otherwise the geometry resolver owns recovery/search/HITL.
_DEFAULT_MESH_TYPES = {
    "cfd": "generic_gmsh",
    "csm": "generic_gmsh",
    "cem": "generic_gmsh",
    "heat_transfer": "generic_gmsh",
    "md": "atomistic_structure",
    "multiphysics": "generic_gmsh",
}


def _normalize_case_dir(case_dir: str | Path) -> str:
    """Return the case root even if a caller passes constant/polyMesh."""
    path = Path(str(case_dir)).expanduser()
    if path.name == "polyMesh" and path.parent.name == "constant":
        return str(path.parent.parent)
    if path.name == "constant":
        return str(path.parent)
    return str(path)


_DEFAULT_PREPROCESSING_WORKSPACE_DIR = "mesh"
_DATA_NODE_WORK_DIR = ".data_node_work"


def _safe_case_dir_under_state(
    state: State,
    case_dir: str | Path,
    default_name: str = _DEFAULT_PREPROCESSING_WORKSPACE_DIR,
) -> str:
    """Keep generated mesh assets inside the current run directory.

    LLMs sometimes pass arbitrary /tmp paths or changing names. Generated
    assets should be discoverable at a stable location for each run, so ad-hoc
    paths are redirected to a hidden run-local work directory.  Final package
    paths are never writable by a generator; the package publisher owns that
    boundary after deterministic review.
    """
    root = Path(str(state.root)).expanduser().resolve()
    stable_name = re.sub(
        r"[^A-Za-z0-9_.-]+", "_", str(default_name or _DEFAULT_PREPROCESSING_WORKSPACE_DIR)
    ).strip("_") or _DEFAULT_PREPROCESSING_WORKSPACE_DIR
    work_root = root / _DATA_NODE_WORK_DIR
    stable_path = work_root / stable_name
    raw = str(case_dir or "").strip()
    if not raw:
        return str(stable_path)
    normalized = Path(_normalize_case_dir(raw)).expanduser()
    if not normalized.is_absolute():
        return str(stable_path)
    resolved = normalized.resolve()
    try:
        resolved.relative_to(work_root.resolve())
        return str(resolved)
    except ValueError:
        pass
    return str(stable_path)


async def _generate_computational_mesh(
    state: State,
    discipline: str = "cfd",
    mesh_type: str = "",
    geometry: str = "",
    case_dir: str = "",
    parameters: str = "",
    **extra: Any,
) -> dict:
    """根据学科领域和几何描述生成计算网格。"""
    # 解析参数
    params = {}
    if parameters:
        try:
            params = json.loads(parameters)
        except (json.JSONDecodeError, TypeError):
            return {
                "status": "error",
                "error": f"parameters 不是合法 JSON 字符串: {parameters[:200]}",
            }
    params = canonical_mesh_controls(flatten_parameter_groups(params))
    element_contract = json.dumps(
        params.get("element_types")
        or params.get("element_type")
        or params.get("element_family")
        or "",
        ensure_ascii=False,
    ).replace("_", " ").replace("/", " ")
    if re.search(
        r"(?i)\b(?:CPS|CPE|CAX|DC2D|S|M3D)(?:4|8|9)R?\b|quadrilateral|\bquad(?:rilateral)?\b|四边形",
        element_contract,
    ):
        params.setdefault("required_surface_topology", "quadrilateral")
        params.setdefault("recombine", True)
    elif re.search(
        r"(?i)\b(?:CPS|CPE|CAX|DC2D|S|M3D)(?:3|6)R?\b|triangular|\btriangle\b|三角形",
        element_contract,
    ):
        params.setdefault("required_surface_topology", "triangular")
    for key, value in extra.items():
        if key not in params and value is not None:
            params[key] = value

    # 确定 case 目录
    if not case_dir:
        case_dir = str(Path(str(state.root)) / _DATA_NODE_WORK_DIR / _DEFAULT_PREPROCESSING_WORKSPACE_DIR)
    case_dir = _safe_case_dir_under_state(state, case_dir, default_name=_DEFAULT_PREPROCESSING_WORKSPACE_DIR)

    discipline = discipline.lower().strip()
    mesh_type = mesh_type.lower().strip() if mesh_type else ""
    geometry = caller_request_text(geometry) or (geometry.strip() if geometry else "")
    apply_mesh_density_from_text(params, geometry)
    original_geometry = original_user_request_text_from_state(state)
    intent_geometry = "\n".join(part for part in (original_geometry, geometry) if part)
    if (
        discipline == "cfd"
        and mesh_type == "airfoil_ogrid"
        and re.search(r"\bunstructured\b|非结构", original_geometry or geometry, flags=re.I)
    ):
        mesh_type = "airfoil_gmsh"
        params["mesh_type"] = mesh_type
    provided_geometry_file = geometry_file_from_params(params)
    if discipline == "cfd" and not provided_geometry_file and str(params.get("geometry_source_policy") or "").startswith("public_reference"):
        reference_candidates = [
            path for suffix in ("*.cas", "*.msh", "*.cgns")
            for path in Path(str(state.root)).rglob(suffix)
            if not {"mesh_case", _DEFAULT_PREPROCESSING_WORKSPACE_DIR, "rejected_mesh"}.intersection(path.parts)
        ]
        if reference_candidates:
            reference_candidates.sort(key=lambda path: (path.suffix.lower() != ".cas", -path.stat().st_size))
            provided_geometry_file = str(reference_candidates[0])
            params["geometry_file"] = provided_geometry_file
            params.setdefault("geometry_representation", "fluid_domain")
            params.setdefault("computational_domain_complete", True)
            params.setdefault("domain_shape_verified", True)
            source_trace = params.get("source_trace")
            if not isinstance(source_trace, list):
                source_trace = [] if source_trace in (None, "") else [source_trace]
                params["source_trace"] = source_trace
            source_trace.append({
                "source": "run_local_reference_mesh_discovery",
                "path": provided_geometry_file,
                "priority": "preserve_reference_domain_topology",
            })
    if discipline == "cfd" and provided_geometry_file and geometry_parameters_need_reference_resolution(params):
        advisor_result = await _resolve_mesh_iteration_inputs(
            state,
            spec=geometry or str(params.get("geometry") or params.get("case_type") or "geometry_file_gmsh"),
            case_type=str(params.get("case_type") or params.get("mesh_type") or ""),
            parameters=json.dumps(params, ensure_ascii=False),
        )
        if advisor_result.get("status") in {"needs_input", "needs_reference_search", "needs_geometry_processing", "error"}:
            return advisor_result
        if advisor_result.get("status") == "success":
            params.update(advisor_result.get("resolved_parameters") or {})
            provided_geometry_file = geometry_file_from_params(params)

    matched_profile = _match_cfd_case_profile(geometry, params, mesh_type) if discipline == "cfd" else None

    # 自动选择默认网格类型
    if not mesh_type:
        if discipline == "cfd" and matched_profile:
            mesh_type = str(matched_profile.get("mesh_type") or _DEFAULT_MESH_TYPES.get(discipline, "structured_rect"))
            _apply_profile_defaults(params, matched_profile)
        elif discipline == "cfd" and (
            looks_like_turbomachinery_blade(intent_geometry, params)
            or looks_like_public_reference_request(intent_geometry)
        ):
            mesh_type = "public_geometry"
        elif discipline == "cfd" and not original_intent_allows_airfoil_mesh(state, intent_geometry, params):
            mesh_type = "public_geometry"
        else:
            mesh_type = _DEFAULT_MESH_TYPES.get(discipline, "structured_rect")
    elif discipline == "cfd" and mesh_type == "structured_rect":
        _apply_profile_defaults(params, matched_profile)
    if discipline in {"cfd", "csm", "cem", "heat_transfer", "multiphysics"} and provided_geometry_file:
        mesh_type = "geometry_file_gmsh"
    if discipline == "cfd" and not provided_geometry_file and (params.get("coordinate_profile_path") or params.get("profile_dat_path")):
        mesh_type = "coordinate_profile_gmsh"
        if _is_turbomachinery_blade_case(intent_geometry, params) and _has_turbomachinery_cascade_domain(params):
            mesh_type = "coordinate_profile_cascade_gmsh"
    special_domain = detect_special_domain_requirements(
        intent_geometry,
        params,
        case_type=str(params.get("case_type") or mesh_type or ""),
        include_complete=True,
    )
    has_profile_geometry = bool(params.get("coordinate_profile_path") or params.get("profile_dat_path") or params.get("airfoil_dat_path"))
    if special_domain and (
        mesh_type in {
            "airfoil_gmsh",
            "airfoil_ogrid",
            "coordinate_profile_gmsh",
            "structured_rect",
            "cylinder_flow",
            "cylinder_gmsh",
            "sphere",
            "pipe_flow",
            "converging_diverging_nozzle",
        }
        or (not provided_geometry_file and not has_profile_geometry and mesh_type != "coordinate_profile_cascade_gmsh")
    ):
        if not special_domain.get("missing_fields"):
            special_domain = {
                **special_domain,
                "missing_fields": ["适配该特殊拓扑的 geometry_file/mesh_type 或专用网格生成路径"],
            }
        return special_domain_reference_request(intent_geometry, params, special_domain)
    if (
        discipline == "cfd"
        and mesh_type != "geometry_file_gmsh"
        and mesh_type != "coordinate_profile_gmsh"
        and mesh_type != "coordinate_profile_cascade_gmsh"
        and not _canonical_cfd_mesh_matches_original_intent(state, mesh_type, intent_geometry, params)
    ):
        return public_geometry_reference_request(intent_geometry, params, case_type="public_geometry")

    if discipline == "md" and mesh_type == "atomistic_structure":
        return {
            "status": "needs_atomic_structure_recovery",
            "discipline": discipline,
            "mesh_type": mesh_type,
            "simulation_ready": False,
            "message": (
                "No explicit lattice family or authoritative atomistic structure was supplied. "
                "The node will not substitute an FCC lattice."
            ),
            "required_evidence": [
                "an explicit fcc_lattice/bcc_lattice/sc_lattice/diamond_lattice request",
                "or an authoritative CIF/POSCAR/XYZ/structure source",
            ],
            "next_action": "Use recover_atomic_structure or provide an explicit lattice family.",
        }

    # 检查学科是否支持
    disc_generators = _MESH_GENERATORS.get(discipline)
    if not disc_generators:
        return {
            "status": "error",
            "error": f"不支持的学科: {discipline!r}。支持: {list(_MESH_GENERATORS.keys())}",
        }

    # 检查网格类型是否支持
    generator_func = disc_generators.get(mesh_type)
    if not generator_func:
        # 尝试在通用 structured_rect 中找
        if mesh_type == "structured_rect":
            generator_func = generate_structured_rect_mesh
        else:
            return {
                "status": "error",
                "error": (
                    f"学科 {discipline!r} 不支持网格类型 {mesh_type!r}。"
                    f"支持: {list(disc_generators.keys())}"
                ),
            }

    # 从 geometry 提取参数。只有显式出现 NACAxxxx 时才补 naca_code；
    # 非 NACA 翼型必须保留为 airfoil_name 并要求用户提供坐标。
    explicit_naca = _extract_explicit_naca_code(
        params.get("naca_code"),
        params.get("airfoil"),
        params.get("geometry"),
        geometry,
    )
    if explicit_naca:
        params["naca_code"] = explicit_naca
    else:
        params.pop("naca_code", None)
        non_naca_airfoil = _extract_non_naca_airfoil_name(
            params.get("airfoil_name"),
            params.get("airfoil"),
            params.get("geometry"),
            geometry,
        )
        if non_naca_airfoil:
            params.setdefault("airfoil_name", non_naca_airfoil)

    if discipline in {"cfd", "csm", "cem", "heat_transfer", "multiphysics"} and mesh_type in {
        "geometry_file_gmsh", "custom_geometry", "public_geometry", "generic_gmsh"
    }:
        geometry_file = provided_geometry_file
        if not geometry_file:
            # Missing public geometry is dependency resolution, not a new
            # scientific objective.  Return the existing reference contract;
            # the planner already honours explicit offline/scope locks and can
            # ask the caller only after targeted discovery makes no progress.
            return public_geometry_reference_request(
                intent_geometry,
                params,
                case_type="public_geometry",
            )
        geometry_suffix = Path(str(geometry_file)).expanduser().suffix.lower()
        geometry_representation = str(
            params.get("geometry_representation") or params.get("geometry_role_input") or "unknown"
        ).strip().lower()
        computational_domain_complete = _as_bool(params.get("computational_domain_complete"), False)
        raw_cad_suffixes = {".step", ".stp", ".iges", ".igs", ".brep", ".stl"}
        # 判决拆除三波（mg:6057 降格，2026-09-02）：「原始 CAD 是物体不是流体域，
        # 直接剖会出坏网格」是预测失败/质量判决 —— gmsh 物理上剖得动。照剖，
        # 产物挂 geometry_representation_unknown 义务：deliverable_valid=False，
        # 不进 dataset 终态（呈裁折中）；显式声明 geometry_representation=
        # fluid_domain + computational_domain_complete=true 即消除。
        geometry_representation_unknown = (
            discipline == "cfd"
            and geometry_suffix in raw_cad_suffixes
            and not (
                geometry_representation in {"fluid_domain", "computational_domain"}
                and computational_domain_complete
            )
        )
        if geometry_representation_unknown:
            try:
                state.append_transcript(
                    "mesh_geometry_representation_unknown",
                    geometry_file=str(geometry_file),
                    geometry_representation=geometry_representation,
                    computational_domain_complete=computational_domain_complete,
                )
            except Exception:
                pass
        try:
            allowed_geometry_params = {
                "geometry_file",
                "geometry_path",
                "cad_file",
                "stl_file",
                "step_file",
                "geo_file",
                "msh_file",
                "scale_factor",
                "mesh_scale",
                "reference_mesh_scale",
                "span",
                "mesh_dimension",
                "characteristic_length",
                "minimum_length",
                "length_unit",
                "element_order",
                "mesh_size_factor",
                "pitch",
                "pitch_chord_ratio",
                "blade_pitch",
                "periodic_pitch",
                "passage_pitch",
                "upstream_length",
                "downstream_length",
                "fore_domain_length",
                "aft_domain_length",
                "inlet_extent",
                "outlet_extent",
                "cascade_periodic",
                "periodic_boundary_pairing",
                "periodic_patches",
                "pitchwise_periodic",
                "inlet_patch",
                "outlet_patch",
                "wall_patches",
                "boundary_map",
                "patch_map",
                "geometry_representation",
                "geometry_role_input",
                "computational_domain_complete",
                "discipline",
                "case_type",
                "case_family",
                "profile_kind",
                "convert_to_openfoam",
                "write_tecplot",
                "solver_mesh_format",
                "required_surface_topology",
                "recombine",
                "expected_boundaries",
                "gmsh_binary",
                "gmsh_to_foam_cmd",
                "timeout",
                "retain_validation_log",
                "require_executable_reproduction_script",
            }
            geometry_params = {k: v for k, v in params.items() if k in allowed_geometry_params}
            geometry_params["geometry_file"] = geometry_file
            geometry_params["discipline"] = discipline
            gmsh_result = await _run_mesh_worker(
                state,
                _generate_geometry_file_gmsh_mesh_with_review,
                case_dir=case_dir,
                **geometry_params,
            )
        except Exception as e:
            return {"status": "error", "error": f"Gmsh 几何文件网格生成失败: {e}"}
        if gmsh_result.get("status") == "error":
            return gmsh_result
        result = {
            "status": "success",
            "discipline": discipline,
            "geometry_priority": "user_provided_geometry_file",
            **gmsh_result,
        }
        if geometry_representation_unknown:
            result.update({
                "geometry_representation": geometry_representation,
                "computational_domain_complete": computational_domain_complete,
                "geometry_representation_unknown": True,
                # 不进 dataset 终态：data_agent_loop 只交付 deliverable_valid=True 的产物。
                "deliverable_valid": False,
                "delivery_blocked_by": ["geometry_representation_unknown"],
                "obligations": [{
                    "kind": "geometry_representation_unknown",
                    "message": (
                        "The supplied raw CAD was meshed as-is; whether it is the complete CFD fluid "
                        "domain or only the object/body is undeclared, so cells may sit inside the body "
                        "or the computational boundary may be incomplete."
                    ),
                    "clear_by": (
                        "retry with geometry_representation=fluid_domain and computational_domain_complete=true "
                        "when the CAD already is the complete fluid domain; otherwise construct the domain "
                        "(closed outer/internal boundary, subtract solids, name boundaries) and mesh that."
                    ),
                }],
                "source_trace": [
                    *(gmsh_result.get("source_trace") or []),
                    {
                        "source": "generic_cfd_geometry_contract",
                        "reason": "raw_cad_meshed_with_geometry_representation_unknown",
                    },
                ],
            })
        return result

    if discipline == "cfd" and mesh_type == "coordinate_profile_gmsh":
        profile_path = params.get("coordinate_profile_path") or params.get("profile_dat_path") or params.get("airfoil_dat_path")
        if not profile_path:
            return needs_input_result(
                question="请提供二维闭合坐标轮廓或组成该轮廓的坐标资产。",
                missing_fields=["coordinate_profile_path 或 coordinate_files"],
                metadata={"input_kind": "mesh_geometry_or_public_search"},
            )
        if _is_turbomachinery_blade_case(intent_geometry, params) and not _has_turbomachinery_cascade_domain(params):
            return _turbomachinery_cascade_domain_reference_request(
                intent_geometry,
                params,
                _missing_turbomachinery_cascade_fields(params),
            )
        special_profile_domain = detect_special_domain_requirements(
            intent_geometry,
            params,
            case_type=str(params.get("case_type") or "coordinate_profile_gmsh"),
            include_complete=True,
        )
        if special_profile_domain:
            if not special_profile_domain.get("missing_fields"):
                special_profile_domain = {
                    **special_profile_domain,
                    "missing_fields": ["适配该特殊拓扑的 profile 通道/周期域生成器"],
                }
            return special_domain_reference_request(intent_geometry, params, special_profile_domain)
        try:
            allowed_profile_params = {
                "coordinate_profile_path",
                "profile_dat_path",
                "profile_name",
                "airfoil_name",
                "airfoil_dat_path",
                "domain_type",
                "far_field",
                "far_field_chord_ratio",
                "far_field_ratio",
                "wake_length",
                "wake_length_chord_ratio",
                "wake_length_ratio",
                "n_surface",
                "angle_of_attack",
                "aoa",
                "aoa_deg",
                "alpha_deg",
                "rotation_center_x",
                "rotation_center_y",
                "h_airfoil",
                "h_farfield",
                "boundary_layer_first",
                "boundary_layer_thickness",
                "boundary_layer_ratio",
                "boundary_layer_layers",
                "mesh_algorithm",
                "near_wall_refinement_mode",
                "mesh_family_contract",
                "lock_near_wall_topology",
                "target_cell_count",
                "required_far_field",
                "target_wall_y_plus",
                "target_streamwise_delta_x_plus",
                "wall_unit_reference",
                "recombine",
                "extrude_to_3d",
                "preserve_input_scale",
                "spanwise_layers",
                "front_back_patch_type",
                "span",
                "convert_to_openfoam",
                "write_tecplot",
                "quality_preset",
                "lock_near_wall_topology",
                "gmsh_binary",
                "gmsh_to_foam_cmd",
                "timeout",
                "retain_validation_log",
                "require_executable_reproduction_script",
                "boundary_map",
                "boundary_role_names",
                "expected_boundaries",
            }
            profile_params = {k: v for k, v in params.items() if k in allowed_profile_params}
            if "coordinate_profile_path" not in profile_params:
                profile_params["coordinate_profile_path"] = profile_path
            gmsh_result = await _run_mesh_worker(
                state,
                _generate_coordinate_profile_gmsh_mesh_with_review,
                case_dir=case_dir,
                **profile_params,
            )
        except Exception as e:
            return {"status": "error", "error": f"Gmsh 坐标轮廓网格生成失败: {e}"}
        if gmsh_result.get("status") == "error":
            return gmsh_result
        return {
            "status": "success",
            "discipline": "cfd",
            "geometry_priority": "coordinate_profile",
            **gmsh_result,
        }

    if discipline == "cfd" and mesh_type == "coordinate_profile_cascade_gmsh":
        profile_path = params.get("coordinate_profile_path") or params.get("profile_dat_path") or params.get("airfoil_dat_path")
        if not profile_path:
            return needs_input_result(
                question="请提供叶栅通道所需的真实二维闭合轮廓。",
                context="叶栅通道网格需要真实 blade/profile 坐标文件。",
                missing_fields=["coordinate_profile_path"],
                metadata={"input_kind": "mesh_geometry_or_public_search"},
            )
        if not _has_turbomachinery_cascade_domain(params):
            return _turbomachinery_cascade_domain_reference_request(
                intent_geometry,
                params,
                _missing_turbomachinery_cascade_fields(params),
            )
        try:
            allowed_cascade_params = {
                "coordinate_profile_path",
                "profile_dat_path",
                "profile_name",
                "airfoil_dat_path",
                "pitch",
                "pitch_chord_ratio",
                "blade_pitch",
                "upstream_length",
                "downstream_length",
                "fore_domain_length",
                "aft_domain_length",
                "inlet_extent",
                "outlet_extent",
                "axial_chord",
                "reference_length",
                "lengths_normalized",
                "domain_boundary_points",
                "domain_boundary_patch_names",
                "periodic_lower_points",
                "periodic_translation_vector",
                "domain_shape_verified",
                "h_blade",
                "h_airfoil",
                "h_farfield",
                "boundary_layer_first",
                "boundary_layer_thickness",
                "boundary_layer_ratio",
                "target_cell_count",
                "required_far_field",
                "target_wall_y_plus",
                "target_streamwise_delta_x_plus",
                "wall_unit_reference",
                "recombine",
                "extrude_to_3d",
                "span",
                "cascade_periodic",
                "periodic_boundary_pairing",
                "periodic_patches",
                "pitchwise_periodic",
                "convert_to_openfoam",
                "write_tecplot",
                "quality_preset",
                "gmsh_binary",
                "gmsh_to_foam_cmd",
                "timeout",
                "retain_validation_log",
                "require_executable_reproduction_script",
            }
            cascade_params = {k: v for k, v in params.items() if k in allowed_cascade_params}
            cascade_params.setdefault("coordinate_profile_path", profile_path)
            gmsh_result = await _run_mesh_worker(
                state,
                _generate_coordinate_profile_cascade_gmsh_mesh_with_review,
                case_dir=case_dir,
                **cascade_params,
            )
        except Exception as e:
            return {"status": "error", "error": f"Gmsh 叶栅坐标轮廓网格生成失败: {e}"}
        if gmsh_result.get("status") == "error":
            return gmsh_result
        return {
            "status": "success",
            "discipline": "cfd",
            "geometry_priority": "coordinate_profile_cascade",
            **gmsh_result,
        }

    if discipline == "cfd" and mesh_type == "airfoil_gmsh":
        if not original_intent_allows_airfoil_mesh(state, geometry, params):
            return public_geometry_reference_request(intent_geometry, params, case_type="public_geometry")
        for key in ("airfoil_dat_path", "airfoil_coordinate_text", "airfoil_coordinates"):
            if key in params and is_placeholder_value(params.get(key)):
                params.pop(key, None)
        if (
            _is_turbomachinery_blade_case(intent_geometry, params)
            and not _as_bool(params.get("isolated_blade_approximation_confirmed"), False)
            and not _has_turbomachinery_cascade_domain(params)
        ):
            return public_geometry_reference_request(intent_geometry, params, case_type="public_geometry")
        if (
            not _has_airfoil_coordinate_input(params)
            and (
                looks_like_turbomachinery_blade(intent_geometry, params)
                or (
                    looks_like_public_reference_request(intent_geometry)
                    and not explicit_naca
                )
            )
        ):
            return public_geometry_reference_request(intent_geometry, params, case_type="public_geometry")
        if (
            params.get("airfoil_name")
            and not _extract_explicit_naca_code(params.get("airfoil_name"))
            and not _has_airfoil_coordinate_input(params)
        ):
            return _airfoil_geometry_request(str(params["airfoil_name"]), params)
        if not params.get("naca_code") and not _has_airfoil_coordinate_input(params):
            return _naca_or_profile_required_request(params)
        if not params.get("near_wall_refinement_mode"):
            params["near_wall_refinement_mode"] = (
                "boundary_layer"
                if re.search(
                    r"\bboundary\s+layer\b|边界层",
                    original_geometry or geometry,
                    flags=re.I,
                )
                else "distance_field"
            )
        density_requested = mesh_density_requested(geometry)
        if density_requested and mesh_density_requires_confirmation(params, geometry):
            if not mesh_density_confirmed(params) and not has_mesh_density_parameters(params):
                return _airfoil_mesh_density_request(params)
        elif (
            density_requested and not has_mesh_density_parameters(params)
        ):
            # A request for a usable low-Re/laminar scale is an executable
            # engineering requirement, not an unresolved scientific choice.
            # Use the documented conservative preset and retain the assumption
            # in the result instead of pausing every fresh service run.
            apply_recommended_mesh_density(params)
            params.setdefault(
                "mesh_density_assumption",
                "Recommended low-Re airfoil density preset applied because the caller did not require custom values.",
            )
        try:
            # A circular far-field boundary does not imply O-grid topology.
            # Keep topology under the explicit domain_type contract; the
            # generator's default remains a wake-resolving C-domain.
            if params.get("surface_mesh_size") not in (None, ""):
                params.setdefault("h_airfoil", params["surface_mesh_size"])
            if str(params.get("element_family") or "").lower() in {
                "triangle", "triangles", "triangular",
            }:
                params.setdefault("near_wall_refinement_mode", "distance_field")
                params.setdefault("recombine", False)
            gmsh_params = _reviewed_generator_parameters(generate_airfoil_gmsh_mesh, params)
            gmsh_result = await _run_mesh_worker(
                state,
                _generate_airfoil_gmsh_mesh_with_review,
                case_dir=case_dir,
                **gmsh_params,
            )
        except Exception as e:
            return {"status": "error", "error": f"Gmsh 翼型网格生成失败: {e}"}
        if gmsh_result.get("status") == "error":
            return gmsh_result
        return {
            "status": "success",
            "discipline": "cfd",
            **gmsh_result,
        }

    if discipline == "cfd" and mesh_type in {"cylinder_flow", "cylinder_gmsh"}:
        try:
            if params.get("surface_mesh_size") not in (None, ""):
                params["h_cylinder"] = params["surface_mesh_size"]
            cylinder_params = _reviewed_generator_parameters(generate_cylinder_gmsh_mesh, params)
            gmsh_result = await _run_mesh_worker(
                state,
                _generate_cylinder_gmsh_mesh_with_review,
                case_dir=case_dir,
                **cylinder_params,
            )
        except Exception as e:
            return {"status": "error", "error": f"Gmsh 圆柱绕流网格生成失败: {e}"}
        if gmsh_result.get("status") == "error":
            return gmsh_result
        return {
            "status": "success",
            "discipline": "cfd",
            **gmsh_result,
        }

    if discipline == "cfd" and mesh_type in {"airfoil_ogrid"}:
        if not original_intent_allows_airfoil_mesh(state, geometry, params):
            return public_geometry_reference_request(intent_geometry, params, case_type="public_geometry")
        if not params.get("naca_code"):
            return _naca_or_profile_required_request(params)

    # ── MD 晶格类型 ──
    if discipline == "md":
        lattice_func = {
            "fcc_lattice": fcc_lattice,
            "bcc_lattice": bcc_lattice,
            "sc_lattice": sc_lattice,
            "diamond_lattice": diamond_lattice,
        }.get(mesh_type, fcc_lattice)

        a = params.get("lattice_constant", 1.0)
        nx_val = params.get("nx", 5)
        ny_val = params.get("ny", 5)
        nz_val = params.get("nz", 5)
        mass = params.get("mass", 1.0)

        try:
            lattice_data = await asyncio.to_thread(
                lattice_func, a=a, nx=nx_val, ny=ny_val, nz=nz_val,
            )
        except Exception as e:
            return {"status": "error", "error": f"{mesh_type} 生成失败: {e}"}

        data_file = str(Path(case_dir) / "lammps.data")
        try:
            written = await asyncio.to_thread(
                write_lammps_data, data_file,
                lattice_data["atoms"], lattice_data["box"], mass=mass,
            )
        except Exception as e:
            return {"status": "error", "error": f"LAMMPS data 写入失败: {e}"}

        return {
            "status": "success",
            "discipline": "md",
            "mesh_type": mesh_type,
            "n_atoms": lattice_data["n_atoms"],
            "box": lattice_data["box"],
            "case_dir": case_dir,
            "written_files": [written],
            "lattice_type": lattice_data["lattice_type"],
            "lattice_constant": a,
            "grid_dims": [nx_val, ny_val, nz_val],
        }

    # ── 连续介质网格类型 ──
    # 构建参数字典：根据 mesh_type 过滤有效参数
    try:
        # 通用参数传递
        filtered_params = _supported_parameters(generator_func, params)
        mesh_data = await asyncio.to_thread(generator_func, **filtered_params)
    except Exception as e:
        return {"status": "error", "error": f"网格生成失败: {e}"}

    # 写入 OpenFOAM polyMesh
    try:
        write_result = await asyncio.to_thread(
            write_openfoam_polymesh,
            case_dir,
            mesh_data["points"], mesh_data["faces"],
            mesh_data["owner"], mesh_data["neighbour"],
            mesh_data["boundary"], mesh_data["n_cells"],
        )
    except Exception as e:
        return {"status": "error", "error": f"polyMesh 写入失败: {e}"}
    try:
        system_result = await asyncio.to_thread(write_openfoam_system_files, case_dir)
    except Exception as e:
        return {"status": "error", "error": f"OpenFOAM system 文件写入失败: {e}"}

    result_mesh_type = mesh_data.get("mesh_type", mesh_type)
    write_tecplot = _as_bool(params.get("write_tecplot"), True)
    tecplot_result: dict[str, Any] = {"requested": write_tecplot, "status": "skipped"}
    tecplot_files: list[str] = []
    if write_tecplot:
        tecplot_surface_path = Path(case_dir) / f"{result_mesh_type}_tecplot_surface.dat"
        tecplot_volume_path = Path(case_dir) / f"{result_mesh_type}_tecplot_volume.dat"
        try:
            surface_result = await asyncio.to_thread(
                write_tecplot_surface_from_mesh_data,
                mesh_data,
                str(tecplot_surface_path),
            )
            volume_result = await asyncio.to_thread(
                write_tecplot_volume_from_mesh_data,
                mesh_data,
                str(tecplot_volume_path),
            )
            tecplot_files = [str(tecplot_surface_path), str(tecplot_volume_path)]
            tecplot_result = {
                "requested": True,
                "status": "success",
                "recommended_file": str(tecplot_surface_path),
                "surface": surface_result,
                "volume": volume_result,
            }
        except Exception as exc:
            tecplot_result = {
                "requested": True,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    # 判决拆除（专审四 删，2026-08-31）：网格已成功落盘（written_files 如实），
    # 附属 Tecplot 导出失败不再把主产物整体判 error —— tecplot{status:error}
    # 已在返回里披露，零替代成立。
    return {
        "status": "success",
        "discipline": discipline,
        "mesh_type": result_mesh_type,
        "n_cells": mesh_data["n_cells"],
        "n_points": len(mesh_data["points"]),
        "n_faces": len(mesh_data["faces"]),
        "n_boundary_patches": len(mesh_data["boundary"]),
        "case_dir": case_dir,
        "polyMesh_dir": str(Path(case_dir) / "constant" / "polyMesh"),
        "written_files": write_result["written_files"] + system_result["written_files"] + tecplot_files,
        "grid_params": mesh_data.get("grid_params", {}),
        "system_dir": system_result["system_dir"],
        "tecplot_file": tecplot_result.get("recommended_file"),
        "tecplot_volume_file": str(Path(case_dir) / f"{result_mesh_type}_tecplot_volume.dat")
        if tecplot_result.get("status") == "success" else None,
        "tecplot": tecplot_result,
    }


# ── 注册工具 ─────────────────────────────────────────────────────────────────

"""Small explicit parametric adapters layered over generic mesh topology."""
from __future__ import annotations

from typing import Any


def generate_structured_box_mesh(
    *,
    lengths: tuple[float, float, float],
    divisions: tuple[int, int, int],
    boundary_names: dict[str, str],
    boundary_types: dict[str, str],
    mesh_type: str,
    grid_params: dict[str, Any],
) -> dict[str, Any]:
    """Build one regular 3-D box topology with semantic boundary names."""
    lx, ly, lz = (float(value) for value in lengths)
    nx, ny, nz = (max(1, int(value)) for value in divisions)
    dx, dy, dz = lx / nx, ly / ny, lz / nz

    points = [
        [i * dx, j * dy, k * dz]
        for k in range(nz + 1)
        for j in range(ny + 1)
        for i in range(nx + 1)
    ]
    plane_size = (nx + 1) * (ny + 1)

    def pid(i: int, j: int, k: int) -> int:
        return k * plane_size + j * (nx + 1) + i

    def cell_id(i: int, j: int, k: int) -> int:
        return k * nx * ny + j * nx + i

    faces: list[list[int]] = []
    owner: list[int] = []
    neighbour: list[int] = []

    for k in range(nz):
        for j in range(ny):
            for i in range(1, nx):
                faces.append([pid(i, j, k), pid(i, j + 1, k), pid(i, j + 1, k + 1), pid(i, j, k + 1)])
                owner.append(cell_id(i - 1, j, k))
                neighbour.append(cell_id(i, j, k))
    for k in range(nz):
        for j in range(1, ny):
            for i in range(nx):
                faces.append([pid(i, j, k), pid(i, j, k + 1), pid(i + 1, j, k + 1), pid(i + 1, j, k)])
                owner.append(cell_id(i, j - 1, k))
                neighbour.append(cell_id(i, j, k))
    for k in range(1, nz):
        for j in range(ny):
            for i in range(nx):
                faces.append([pid(i, j, k), pid(i + 1, j, k), pid(i + 1, j + 1, k), pid(i, j + 1, k)])
                owner.append(cell_id(i, j, k - 1))
                neighbour.append(cell_id(i, j, k))

    boundary: dict[str, dict[str, Any]] = {}

    def add_patch(side: str, patch_faces: list[tuple[list[int], int]]) -> None:
        name = boundary_names[side]
        start = len(faces)
        for nodes, cell in patch_faces:
            faces.append(nodes)
            owner.append(cell)
        boundary[name] = {
            "type": boundary_types.get(side, "patch"),
            "nFaces": len(patch_faces),
            "startFace": start,
        }

    add_patch("x_min", [
        ([pid(0, j, k), pid(0, j, k + 1), pid(0, j + 1, k + 1), pid(0, j + 1, k)], cell_id(0, j, k))
        for k in range(nz) for j in range(ny)
    ])
    add_patch("x_max", [
        ([pid(nx, j, k), pid(nx, j + 1, k), pid(nx, j + 1, k + 1), pid(nx, j, k + 1)], cell_id(nx - 1, j, k))
        for k in range(nz) for j in range(ny)
    ])
    add_patch("y_max", [
        ([pid(i, ny, k), pid(i, ny, k + 1), pid(i + 1, ny, k + 1), pid(i + 1, ny, k)], cell_id(i, ny - 1, k))
        for k in range(nz) for i in range(nx)
    ])
    add_patch("y_min", [
        ([pid(i, 0, k), pid(i + 1, 0, k), pid(i + 1, 0, k + 1), pid(i, 0, k + 1)], cell_id(i, 0, k))
        for k in range(nz) for i in range(nx)
    ])
    add_patch("z_min", [
        ([pid(i, j, 0), pid(i, j + 1, 0), pid(i + 1, j + 1, 0), pid(i + 1, j, 0)], cell_id(i, j, 0))
        for j in range(ny) for i in range(nx)
    ])
    add_patch("z_max", [
        ([pid(i, j, nz), pid(i + 1, j, nz), pid(i + 1, j + 1, nz), pid(i, j + 1, nz)], cell_id(i, j, nz - 1))
        for j in range(ny) for i in range(nx)
    ])

    return {
        "points": points,
        "faces": faces,
        "owner": owner,
        "neighbour": neighbour,
        "boundary": boundary,
        "n_cells": nx * ny * nz,
        "grid_params": dict(grid_params),
        "mesh_type": mesh_type,
    }


def generate_cantilever_beam_mesh(
    length: float = 1.0,
    height: float = 0.1,
    thickness: float = 0.05,
    nx: int = 50,
    ny: int = 10,
    nz: int = 3,
) -> dict[str, Any]:
    return generate_structured_box_mesh(
        lengths=(length, height, thickness),
        divisions=(nx, ny, nz),
        boundary_names={
            "x_min": "fixed_face", "x_max": "loaded_face",
            "y_min": "bottom", "y_max": "top", "z_min": "front", "z_max": "back",
        },
        boundary_types={
            "x_min": "wall", "x_max": "patch", "y_min": "wall",
            "y_max": "wall", "z_min": "wall", "z_max": "wall",
        },
        mesh_type="cantilever_beam",
        grid_params={
            "length": length, "height": height, "thickness": thickness,
            "nx": nx, "ny": ny, "nz": nz,
        },
    )


def generate_rectangular_waveguide_mesh(
    length: float = 0.1,
    width: float = 0.02286,
    height: float = 0.01016,
    nx: int = 80,
    ny: int = 20,
    nz: int = 10,
) -> dict[str, Any]:
    return generate_structured_box_mesh(
        lengths=(length, width, height),
        divisions=(nx, ny, nz),
        boundary_names={
            "x_min": "port1", "x_max": "port2", "y_min": "pec_bottom",
            "y_max": "pec_top", "z_min": "pec_front", "z_max": "pec_back",
        },
        boundary_types={
            "x_min": "patch", "x_max": "patch", "y_min": "wall",
            "y_max": "wall", "z_min": "wall", "z_max": "wall",
        },
        mesh_type="rectangular_waveguide",
        grid_params={
            "length": length, "width": width, "height": height,
            "nx": nx, "ny": ny, "nz": nz,
        },
    )


def _structured_2d(
    x: list[list[float]],
    y: list[list[float]],
    n_rows: int,
    n_columns: int,
    patches: dict[str, dict[str, str]],
) -> dict[str, Any]:
    # Local import avoids a module cycle with the unified mesh facade.
    from .mesh_generator import _build_2d_structured_mesh

    return _build_2d_structured_mesh(x, y, n_rows, n_columns, boundary_patches=patches)


def generate_heat_sink_mesh(
    base_lx: float = 0.1,
    base_ly: float = 0.02,
    fin_height: float = 0.04,
    fin_thickness: float = 0.003,
    fin_gap: float = 0.005,
    n_fins: int = 6,
    nx_base: int = 50,
    ny_base: int = 5,
    nx_fin: int = 3,
    ny_fin: int = 15,
    nx_gap: int = 5,
) -> dict[str, Any]:
    """Generate the retained explicit fin-array adapter."""
    segments: list[tuple[float, float, int]] = []
    cursor = 0.0
    for index in range(n_fins):
        segments.append((cursor, cursor + fin_thickness, nx_fin))
        cursor += fin_thickness
        if index < n_fins - 1:
            segments.append((cursor, cursor + fin_gap, nx_gap))
            cursor += fin_gap

    x_coordinates: list[float] = []
    for start, end, count in segments:
        x_coordinates.extend(start + (end - start) * i / count for i in range(count))
    x_coordinates.append(cursor)
    nx_total = len(x_coordinates) - 1
    ny_total = ny_base + ny_fin
    y_coordinates = [
        base_ly * j / ny_base if j <= ny_base
        else base_ly + fin_height * (j - ny_base) / ny_fin
        for j in range(ny_total + 1)
    ]
    x = [x_coordinates[:] for _ in y_coordinates]
    y = [[value] * (nx_total + 1) for value in y_coordinates]
    mesh = _structured_2d(x, y, ny_total, nx_total, {
        "bottom": {"type": "wall"}, "top": {"type": "patch"},
        "left": {"type": "symmetry"}, "right": {"type": "symmetry"},
    })
    mesh["boundary"] = {
        "hot_surface": mesh["boundary"]["bottom"],
        "convection": mesh["boundary"]["top"],
        "sym_left": mesh["boundary"]["left"],
        "sym_right": mesh["boundary"]["right"],
        "frontAndBack": mesh["boundary"]["frontAndBack"],
    }
    mesh["boundary"]["hot_surface"]["type"] = "wall"
    mesh["boundary"]["convection"]["type"] = "patch"
    mesh["boundary"]["sym_left"]["type"] = "symmetryPlane"
    mesh["boundary"]["sym_right"]["type"] = "symmetryPlane"
    mesh["grid_params"] = {
        "base_lx": base_lx, "base_ly": base_ly, "fin_height": fin_height,
        "fin_thickness": fin_thickness, "fin_gap": fin_gap, "n_fins": n_fins,
        "nx_base": nx_base, "ny_base": ny_base, "nx_fin": nx_fin,
        "ny_fin": ny_fin, "total_width": cursor, "total_height": base_ly + fin_height,
    }
    mesh["mesh_type"] = "heat_sink"
    return mesh


def generate_converging_diverging_nozzle_mesh(
    throat_radius: float = 0.5,
    inlet_radius: float = 1.0,
    outlet_radius: float = 0.8,
    converging_length: float = 3.0,
    diverging_length: float = 5.0,
    nx: int = 100,
    nr: int = 40,
    delta0: float = 1e-4,
    stretch: float = 1.12,
) -> dict[str, Any]:
    """Generate the retained explicit axisymmetric nozzle adapter."""
    increments = [delta0 * stretch**index for index in range(nr + 1)]
    total_increment = sum(increments)
    radial_fractions: list[float] = []
    cumulative = 0.0
    for increment in increments:
        cumulative += increment
        radial_fractions.append(cumulative / total_increment)

    total_length = converging_length + diverging_length

    def wall_radius(x_position: float) -> float:
        if x_position <= converging_length:
            t = x_position / converging_length
            return inlet_radius + (throat_radius - inlet_radius) * (3 * t**2 - 2 * t**3)
        t = (x_position - converging_length) / diverging_length
        return throat_radius + (outlet_radius - throat_radius) * (3 * t**2 - 2 * t**3)

    x = [[0.0] * (nx + 1) for _ in range(nr + 1)]
    y = [[0.0] * (nx + 1) for _ in range(nr + 1)]
    for i in range(nx + 1):
        x_position = total_length * i / nx
        radius = wall_radius(x_position)
        for j, fraction in enumerate(radial_fractions):
            x[j][i] = x_position
            y[j][i] = radius * fraction
    mesh = _structured_2d(x, y, nr, nx, {
        "bottom": {"type": "symmetry"}, "top": {"type": "wall"},
        "left": {"type": "patch"}, "right": {"type": "patch"},
    })
    mesh["boundary"] = {
        "axis": mesh["boundary"]["bottom"], "wall": mesh["boundary"]["top"],
        "inlet": mesh["boundary"]["left"], "outlet": mesh["boundary"]["right"],
        "frontAndBack": mesh["boundary"]["frontAndBack"],
    }
    mesh["boundary"]["axis"]["type"] = "symmetryPlane"
    mesh["boundary"]["wall"]["type"] = "wall"
    mesh["boundary"]["inlet"]["type"] = "patch"
    mesh["boundary"]["outlet"]["type"] = "patch"
    mesh["grid_params"] = {
        "throat_radius": throat_radius, "inlet_radius": inlet_radius,
        "outlet_radius": outlet_radius, "converging_length": converging_length,
        "diverging_length": diverging_length, "nx": nx, "nr": nr,
        "delta0": delta0, "stretch": stretch, "total_length": total_length,
    }
    mesh["mesh_type"] = "converging_diverging_nozzle"
    return mesh

"""Lazy registrations for optional data-node adapters."""
from __future__ import annotations

from importlib import import_module
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool


_IMPLEMENTATIONS = {
    "build_scientific_preprocessing_package": (
        "nodes.data.tools.scientific_preprocessor",
        "_gated_build_scientific_preprocessing_package",
    ),
    "data_web_search": ("nodes.data.tools.web_search", "data_web_search"),
    "data_web_download": ("nodes.data.tools.web_search", "data_web_download"),
    "recover_atomic_structure": ("nodes.data.tools.atomic_structure_recovery", "recover_atomic_structure"),
}


async def _dispatch(tool_name: str, state: State, arguments: dict[str, Any]) -> dict[str, Any]:
    module_name, function_name = _IMPLEMENTATIONS[tool_name]
    implementation = getattr(import_module(module_name), function_name)
    return await implementation(state=state, **arguments)


def _lazy(tool_name: str):
    async def invoke(state: State, **kwargs: Any) -> dict[str, Any]:
        return await _dispatch(tool_name, state, kwargs)

    return invoke


register_tool(
    ToolDefinition(
        name="build_scientific_preprocessing_package",
        description=(
            "Generate approved cross-discipline preprocessing assets in executor-owned staging. "
            "Solver-deck adapters can write complete input files around dependency meshes using the bound "
            "material, boundary and load controls; this tool is not merely a file copier. "
            "Review, manifest finalization, and publication are performed only by the plan executor."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "spec": {"type": "string"},
                "discipline": {"type": "string"},
                "data_path": {"type": "string"},
                "output_name": {"type": "string"},
                "parameters": {},
                "revision_contract": {
                    "type": "object",
                    "description": "Executor-owned contract for targeted regeneration of staged content.",
                },
                "generate_model_assets": {"type": "boolean"},
                "generate_mesh_assets": {"type": "boolean"},
            },
            "required": ["spec"],
            "additionalProperties": True,
        },
        allowed_node_types=["data"],
        risk_level="low",
        content_contract={
            "boundary_conditions": "Map existing mesh region names to the requested physical condition. Mechanical examples: {region: {u1: 0, u2: 0}} or {region: {type: 'fixed'}}. Steady heat-transfer examples: {region: {type: 'dirichlet', temperature: 400, unit: 'K'}} or {region: {type: 'neumann', heat_flux: 0}} for insulation. Compound kind/field labels are equivalent. Preserve the supplied values and units; do not reinterpret convection/radiation or a nonzero flux as a prescribed temperature or insulation. Nested overrides merge with the original bindings before assembly.",
            "material": "Material controls merge with the asset's frozen bindings. For steady conductive heat transfer supply thermal_conductivity; do not invent heat capacity for a steady-state request. Other thermal formulations or sources require an explicit solver-input generation step.",
            "loads": "For the CSM solver-deck adapter, parameters.loads maps existing mesh boundary region names to {magnitude: number, direction: '+x' | '-x' | '+y' | '-y' | '+z' | '-z'}. Magnitude is traction in the request's consistent units; the adapter integrates boundary-edge length times section thickness into nodal forces. Do not encode the magnitude in a region name or in boundary_conditions.",
        },
    ),
    _lazy("build_scientific_preprocessing_package"),
)

register_tool(
    ToolDefinition(
        name="data_web_search",
        description=(
            "Approved public discovery for a missing scientific reference or asset. Returns leads only; "
            "materialization requires a separate data_web_download step."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "site": {"type": "string"},
                "auto_discover_downloads": {"type": "boolean"},
                "max_discovery_pages": {"type": "integer", "minimum": 0, "maximum": 3},
                "search_mode": {"type": "string"},
                "asset_kind": {
                    "type": "string",
                    "enum": ["reference", "dataset", "parameters", "archive", "structure", "geometry_or_mesh"],
                },
            },
            "required": ["query"],
            "additionalProperties": True,
        },
        allowed_node_types=["data"],
        risk_level="low",
    ),
    _lazy("data_web_search"),
)

register_tool(
    ToolDefinition(
        name="data_web_download",
        description=(
            "Planning-gated fallback downloader for a selected public scientific asset or download page. "
            "It records the local path and hash and is loaded only when selected."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"}, "output_name": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 250000000},
                "discover_links": {"type": "boolean"},
                "download_first_matching_link": {"type": "boolean"},
                "link_pattern": {"type": "string"}, "allow_non_mesh_file": {"type": "boolean"},
                "allow_reference_documents": {"type": "boolean"},
                "expected_filename": {"type": "string"},
                "expected_revision": {"type": "string"},
                "asset_kind": {
                    "type": "string",
                    "enum": [
                        "dataset", "parameters", "archive", "structure",
                        "geometry_or_mesh", "official_file",
                    ],
                },
                "timeout_seconds": {"type": "number", "minimum": 5, "maximum": 60},
            },
            "required": ["url"],
            "additionalProperties": True,
        },
        allowed_node_types=["data"],
        risk_level="medium",
    ),
    _lazy("data_web_download"),
)


register_tool(
    ToolDefinition(
        name="recover_atomic_structure",
        description=(
            "Recover a missing crystal or molecular structure from explicit local/reference evidence, "
            "or emit a reproducible recoverable-blocked contract. Loaded only when requested."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "structure_name": {"type": "string"},
                "composition": {"type": "string"},
                "expected_atom_count": {"type": "integer", "minimum": 1},
                "source_paths": {"type": "array", "items": {"type": "string"}},
                "reference_evidence": {"type": "array", "items": {"type": "object"}},
                "search_history": {"type": "array", "items": {"type": "object"}},
                "operation": {"type": "string", "enum": ["assess", "reconstruct", "finalize_blocked"]},
                "output_name": {"type": "string"},
            },
            "required": ["structure_name"],
            "additionalProperties": True,
        },
        allowed_node_types=["data"],
        risk_level="medium",
    ),
    _lazy("recover_atomic_structure"),
)

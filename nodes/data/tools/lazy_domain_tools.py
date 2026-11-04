"""Lazy registrations for optional data-node adapters."""
from __future__ import annotations

from importlib import import_module
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

DEFAULT_DOWNLOAD_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 300.0

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
            "material, section, boundary and load controls; changing these controls does not remesh "
            "or edit dependency geometry. Repair those assets at their upstream producers. "
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
            "geometry": "Solver-deck section thickness only (thickness or thickness_mm in consistent geometry units); this adapter does not alter dependency geometry or mesh sizing.",
            "boundary_conditions": "Map existing mesh region names to the requested physical condition. Mechanical examples: {region: {u1: 0, u2: 0}}, {region: {type: 'fixed'}}, or {region: {type: 'free'}} (no prescribed displacement). Numeric displacement assignments preserve their values; unsupported text must not be guessed as zero. Steady heat-transfer examples: {region: {type: 'dirichlet', temperature: 400, unit: 'K'}} or {region: {type: 'neumann', heat_flux: 0}} for insulation. Compound kind/field labels are equivalent. Preserve the supplied values and units; do not reinterpret convection/radiation or a nonzero flux as a prescribed temperature or insulation. Only constrain regions/DOFs required by the request; a named mesh region or applied load does not imply displacement fixation. Nested overrides merge with the original bindings; an explicit empty region object {} removes its prescribed condition without removing a separately declared load.",
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
            "HTTP GET downloader for a selected public scientific asset or discovery page, using its own "
            "HTTP client rather than shell networking. Select advertised file_download links or a fully "
            "specified subset request returning file bytes; a data_service URL is not necessarily a file. "
            "This tool does not implement OPeNDAP. It records the local path, source URL and hash."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"}, "output_name": {"type": "string"},
                "max_bytes": {
                    "type": "integer", "minimum": 1024, "default": DEFAULT_DOWNLOAD_MAX_BYTES,
                    "description": "Transfer byte budget; defaults to 512 MiB. Set a larger budget for a known larger file when resources permit.",
                },
                "discover_links": {"type": "boolean"},
                "discovery_only": {"type": "boolean", "description": "Inspect a bounded source preview and candidate links without materializing the dataset."},
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
                "timeout_seconds": {
                    "type": "number", "minimum": 5, "default": DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
                    "description": "HTTP connection/read inactivity timeout, not a total transfer duration limit. Active large transfers may take longer.",
                },
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

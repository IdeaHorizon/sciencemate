"""Canonical toolchain profile refresh for experiment runs.

This is deliberately a thin entry point. Build gates and manual environment
probes share the same build_env.sh plus collect_platform_profile fact chain.
"""
from __future__ import annotations

import json
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

try:
    from .env_provision import ensure_env_script
    from .repro_snapshot import collect_platform_profile
except ImportError:
    from tools.env_provision import ensure_env_script
    from tools.repro_snapshot import collect_platform_profile


async def _probe_toolchain(state: State, **kwargs: Any) -> dict[str, Any]:
    """Refresh the canonical platform profile under this run build environment."""
    if kwargs:
        return {"status": "error", "error": "probe_toolchain accepts no input"}
    try:
        env = ensure_env_script(state)
        env_path = env["env_path"]
        profile = collect_platform_profile(env_path=env_path)
        name = f"platform_profile_probe_toolchain_{state.run_id}"
        state.save_artifact(
            "platform_profile", name, json.dumps(profile, ensure_ascii=False, indent=2),
            metadata={
                "schema_version": "1.0",
                "generated_by": "probe_toolchain",
                "env_path": env_path,
                "build_env_sha256": env.get("sha256"),
            },
        )
        return {
            "status": "success", "artifact_name": name, "platform_profile": profile,
            "env_path": env_path, "build_env_sha256": env.get("sha256"),
        }
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


register_tool(
    ToolDefinition(
        name="probe_toolchain",
        description=(
            "Refresh the canonical platform_profile under Modules initialization and this "
            "run build_env.sh. It accepts no command/path/shell input, reuses the build-gate "
            "fact chain, and saves a platform_profile artifact. discover_resources remains "
            "for hardware and schedulers only."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        allowed_node_types=["experiment"], risk_level="low",
    ),
    _probe_toolchain,
)

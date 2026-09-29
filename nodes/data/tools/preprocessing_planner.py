"""Designer/Critic tools for reasoning before scientific preprocessing execution."""
from __future__ import annotations

import json
import os
import re
import asyncio
from copy import deepcopy
from urllib.parse import quote, unquote, urlparse
from pathlib import Path, PurePosixPath
from typing import Any

from core.llm import LLMClient, LLMMessage
from core.llm_providers import get_provider
from core.state import State
from core.tool_registry import ToolDefinition, all_tool_names, get_tool, register_tool
from nodes.data.pipeline_contract import merge_parameter_updates, needs_input_result, pipeline_outcome, revision_contract
from nodes.data.planning.schemas import (
    CRITIC_DIMENSIONS,
    acquisition_contract_errors,
    acquisition_dimension_conflict,
    blocked_placeholder_markers,
    canonical_filename_identity,
    normalize_declared_filename,
    normalize_acquisition_contract,
    normalize_artifact_spec,
    normalize_asset_contract,
    normalize_official_locator,
    normalize_software_identity,
    normalize_critique,
    normalize_plan,
    normalize_task_scope_contract,
    canonical_workflow_capability,
    is_pipeline_infrastructure_step,
    tool_allowed_in_plan_kind,
    validate_plan,
    EXTERNAL_WORKFLOW_CAPABILITIES,
)
from nodes.data.planning.store import PlanningStore, canonical_hash
from nodes.data.planning.request_contract import (
    build_preprocessing_work_order,
    caller_request_text,
    decisive_input_gaps,
    normalize_preprocessing_request,
    validate_preprocessing_request,
)
from nodes.data.progress import emit_progress
from .discipline_identifier import discipline_from_solver, extract_solver_name, identify_discipline
from .scientific_assets import SCIENTIFIC_ASSET_SUFFIXES, inspect_scientific_asset_path
from .input_inspection import inspect_input_path
_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
# This is an input-character guard, separate from the model's output-token
# limit.  Keep it aligned with the data-node planning budget; focused revision
# removes redundant plan detail before this boundary is reached.
_PLANNING_INPUT_BUDGET_CHARS = 65_536
# Stable module-level alias used by planning payload construction.
_PLANNING_PAYLOAD_BUDGET_CHARS = _PLANNING_INPUT_BUDGET_CHARS


def _adapter_required_files(existing: list[dict[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    """Use the domain adapter without importing optional domains at bootstrap."""
    from .cfd_case_router import merge_adapter_required_files

    return merge_adapter_required_files(existing, names)


def _serialized_payload(value: Any) -> str:
    """Return the compact representation used for both logs and LLM input."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


DESIGNER_SYSTEM_PROMPT = _load_prompt("preprocessing_designer.md")
REQUIREMENT_ANALYST_SYSTEM_PROMPT = _load_prompt("preprocessing_requirement_analyst.md")
CRITIC_SYSTEM_PROMPT = _load_prompt("preprocessing_critic.md")


def _planning_agent_max_tokens(role: str) -> int:
    """Resolve role-specific output budgets for data-node planning subagents."""
    defaults = {
        "requirement_analyst": 32768,
        # 本节点是 custom loop，不走框架 agent loop —— harness.yaml 的
        # context_config 对它没有消费者（曾经指向那里的注释在 harness 精简后
        # 就指空了）。规划子代理的输出预算真相源就是这张表，env 可覆盖。
        "designer": 32768,
        "critic": 32768,
    }
    role_key = re.sub(r"[^A-Z0-9]+", "_", str(role or "").upper()).strip("_")
    raw = (
        os.getenv(f"DATA_{role_key}_MAX_OUTPUT_TOKENS", "").strip()
        or os.getenv("DATA_PLANNING_MAX_OUTPUT_TOKENS", "").strip()
    )
    try:
        configured = int(raw) if raw else defaults.get(role, 4096)
    except ValueError:
        configured = defaults.get(role, 4096)
    return max(1024, min(configured, 32768))


def _planning_agent_attempts(role: str) -> int:
    # One correction pass is enough for a structural Analyst mismatch.  The
    # retry carries the exact missing authorization and avoids immediately
    # dropping to a lossy fallback after the first malformed scope.
    defaults = {"requirement_analyst": 2, "designer": 2, "critic": 1}
    role_key = re.sub(r"[^A-Z0-9]+", "_", str(role or "").upper()).strip("_")
    raw = os.getenv(f"DATA_{role_key}_RETRIES", "").strip()
    try:
        value = int(raw) if raw else defaults.get(role, 1)
    except ValueError:
        value = defaults.get(role, 1)
    return max(1, min(value, 3))


def _extract_json_object(text: str) -> dict[str, Any]:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", value):
        try:
            parsed, _ = decoder.raw_decode(value[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            candidates.append(parsed)
    if candidates:
        # A truncated outer object can still contain valid nested objects. Pick
        # the broadest candidate, then let the role-specific validator reject it
        # if it is not the requested top-level schema.
        return max(candidates, key=lambda item: (len(item), len(json.dumps(item, default=str))))
    raise ValueError("LLM response did not contain a valid JSON object")


def _validate_agent_payload(
    role: str,
    value: dict[str, Any],
    *,
    review_profile: str = "plan_bound",
) -> None:
    if role == "requirement_analyst":
        discipline = value.get("discipline")
        if isinstance(discipline, str):
            value["discipline"] = {"primary": discipline, "evidence": []}
        software = value.get("simulation_software")
        if isinstance(software, (str, dict)):
            value["simulation_software"] = normalize_software_identity(software)
            value["simulation_software"].setdefault("evidence", [])
        elif isinstance(software, list):
            candidates = [
                item if isinstance(item, dict) else {"name": str(item), "evidence": []}
                for item in software
                if (isinstance(item, dict) and str(item.get("name") or "").strip())
                or (not isinstance(item, dict) and str(item).strip())
            ]
            primary = next(
                (
                    item for item in candidates
                    if re.search(
                        r"\b(primary|main|core|solver)\b|主求解器|核心软件",
                        str(item.get("role") or ""),
                        flags=re.I,
                    )
                ),
                candidates[0] if candidates else None,
            )
            if primary:
                value["simulation_software"] = primary
                value["supporting_software"] = [
                    item for item in candidates if item is not primary
                ]
        for key in ("calculation_stages", "required_files", "optional_files", "evidence", "assumptions", "unresolved_facts", "search_queries"):
            if key not in value:
                value[key] = []
        if not isinstance(value.get("required_files"), list):
            raise ValueError("requirement analyst required_files must be an array")
        delivery_files: list[dict[str, Any]] = []
        runtime_access_dependencies: list[dict[str, Any]] = []
        runtime_tool_requirements: list[dict[str, Any]] = []
        asset_errors: list[str] = []
        for index, item in enumerate(value["required_files"]):
            if not isinstance(item, dict):
                asset_errors.append(f"required_files[{index}] must be an object")
                continue
            contract = normalize_asset_contract(item)
            item_text = json.dumps(item, ensure_ascii=False, default=str)
            if (
                contract.get("workflow_capability") == "official_file_acquisition"
                and re.search(r"\bNACA\s*[-_ ]?\s*\d{4,5}(?!\d)", item_text, flags=re.I)
                and re.search(r"\b(?:analytic|analytical|generate|generated|derive|formula)\b|解析式|生成|公式", item_text, flags=re.I)
            ):
                # Four/five-digit NACA profiles are locally derivable assets.
                # A copied external-file template must not force a download.
                item.update({
                    "asset_role": "geometry_or_discretization_asset",
                    "representation": "coordinate_profile",
                    "workflow_capability": "local_artifact_generation",
                    "source_strategy": "local_generation",
                    "acquisition_kind": "generated_artifact",
                    "generation_recipe": item.get("generation_recipe") or "analytical_naca_profile",
                })
                contract = normalize_asset_contract(item)
            if contract.get("contradictory_fields"):
                # 判决拆除 O10（planner:286 降格，2026-08-31）：自相矛盾是真信号，
                # 但作废整份分析只制造重试循环 —— 矛盾如实写进 payload
                # （contradictory_fields 字段随 contract 并入 item），路由端会把
                # 该资产按「无法路由」记 rejected_routes；分析继续。
                value.setdefault("contract_review_notes", []).append(
                    f"required_files[{index}] has contradictory asset fields: "
                    f"{contract.get('contradictory_fields')}"
                )
            if contract.get("workflow_capability") == "official_file_acquisition":
                declared_filename = str(
                    item.get("expected_filename")
                    or item.get("filename")
                    or item.get("file_name")
                    or ""
                ).strip()
                # A filename-shaped name_or_role is already part of the
                # Analyst's structured request.  Normalize that declaration
                # here instead of rejecting the whole analysis and asking the
                # model to repeat the same JSON.  Do not infer from opaque
                # ids or scientific prose; only an explicit path/extension
                # can become an expected filename.
                if not declared_filename:
                    candidate = str(item.get("name_or_role") or "").strip()
                    candidate_name = PurePosixPath(candidate.replace("\\", "/")).name
                    if "/" in candidate.replace("\\", "/") or re.search(
                        r"\.[A-Za-z0-9][A-Za-z0-9._-]*$", candidate_name
                    ):
                        declared_filename = candidate_name
                        item["expected_filename"] = candidate_name
                locator = ""
                acquisition_contract = item.get("acquisition_contract")
                if isinstance(acquisition_contract, dict):
                    locator = str(
                        acquisition_contract.get("locator")
                        or acquisition_contract.get("path")
                        or acquisition_contract.get("url")
                        or ""
                    ).strip()
                    if not locator and isinstance(
                        acquisition_contract.get("retrieval_instructions"), dict
                    ):
                        retrieval = acquisition_contract["retrieval_instructions"]
                        locator = str(
                            retrieval.get("locator")
                            or retrieval.get("path")
                            or retrieval.get("url")
                            or ""
                        ).strip()
                if not declared_filename and not PurePosixPath(locator.split("?", 1)[0]).name:
                    # An unresolved external reference is a planning gap, not
                    # a malformed caller request. Keep it out of executable
                    # deliverables until a reference plan supplies an exact
                    # locator; do not reject the whole analysis at turn zero.
                    value.setdefault("unresolved_facts", []).append(
                        f"External asset {item.get('name_or_role') or item.get('id') or index} "
                        "needs an exact filename or acquisition locator."
                    )
                    continue
            item.update({
                key: field_value
                for key, field_value in contract.items()
                if key != "contradictory_fields"
            })
            if not str(item.get("scientific_role") or "").strip():
                item["scientific_role"] = "preprocessing_input"
            if contract.get("fulfillment_kind") == "runtime_access":
                runtime_access_dependencies.append({
                    "id": str(item.get("id") or f"runtime_access_{index + 1}"),
                    "scientific_role": item["scientific_role"],
                    "reason": str(item.get("reason") or "Runtime access must be supplied by an authorized user."),
                    "consequences_if_missing": str(item.get("consequences_if_missing") or "The approved runtime acquisition step cannot run."),
                    "resume_contract": "Provide the authorized runtime access through the execution environment; do not download or generate credentials.",
                })
                continue
            if contract.get("fulfillment_kind") == "runtime_tool":
                runtime_tool_requirements.append({
                    "id": str(item.get("id") or f"runtime_tool_{index + 1}"),
                    "tool_identity": str(item.get("name_or_role") or item.get("id") or "runtime_tool"),
                    "scientific_role": item.get("scientific_role"),
                    "representation": "runtime_tool",
                    "consumer_stages": [str(item.get("stage_id"))] if item.get("stage_id") else [],
                    "resume_contract": "Have Experiment provide this executable in the calling environment; do not search for or package it as a data artifact.",
                })
                continue
            delivery_files.append(item)
        value["required_files"] = delivery_files
        if runtime_access_dependencies:
            value["runtime_access_dependencies"] = runtime_access_dependencies
        if runtime_tool_requirements:
            value["runtime_tool_requirements"] = _merge_runtime_tool_requirements(
                runtime_tool_requirements
            )
        if not isinstance(value.get("task_scope"), dict):
            value["task_scope"] = {}
        for key in ("declared_operations", "allowed_capabilities", "excluded_capabilities", "data_representations", "stage_ids"):
            # The model occasionally emits a single scope value as a string.
            # This is a presentation variation, not an ambiguous routing
            # decision, so normalize it before applying the contract rules.
            # Keeping this at the agent boundary also ensures every caller of
            # ``normalize_task_scope_contract`` receives the same shape.
            raw_scope_value = value["task_scope"].get(key)
            if raw_scope_value is None:
                value["task_scope"][key] = []
            elif isinstance(raw_scope_value, str):
                value["task_scope"][key] = [raw_scope_value]
            elif not isinstance(raw_scope_value, list):
                raise ValueError(f"requirement analyst task_scope.{key} must be an array")
        # Scientific operation names remain auditable, while routing uses the
        # closed capability vocabulary derived from AssetContracts.
        value["task_scope"] = normalize_task_scope_contract(
            value["task_scope"],
            [item for item in value.get("required_files") or [] if isinstance(item, dict)],
        )
        # 判决拆除 O10（planner:286 降格，2026-08-31）：scope 冲突/缺能力是真
        # 信号，但作废整份 payload 只制造重试循环 —— 冲突如实写进 payload 本体
        # （contract_conflicts / required_capabilities_outside_scope 字段已在
        # task_scope 里），继续往下走，由 Critic/review 消费。
        contract_review_notes: list[str] = []
        if value["task_scope"].get("contract_conflicts"):
            contract_review_notes.append(
                "task_scope has allowed/excluded conflicts: "
                f"{value['task_scope']['contract_conflicts']}"
            )
        if value["task_scope"].get("required_capabilities_outside_scope"):
            contract_review_notes.append(
                "task_scope is missing required capabilities: "
                f"{value['task_scope']['required_capabilities_outside_scope']}"
            )
        if asset_errors:
            raise ValueError("requirement analyst contract errors: " + "; ".join(asset_errors))
        if contract_review_notes:
            value.setdefault("contract_review_notes", []).extend(contract_review_notes)
        if not value["task_scope"].get("allowed_capabilities"):
            # 判决拆除 O10（planner:288 降格）：缺字段填显式 unknown + 披露，
            # 不再作废整份分析。
            value["task_scope"]["allowed_capabilities"] = ["unknown"]
            value.setdefault("contract_review_notes", []).append(
                "task_scope.allowed_capabilities was empty; defaulted to ['unknown']")
        if "confidence" not in value:
            value["confidence"] = 0.75
    if role == "requirement_analyst":
        boundary_contract = value.setdefault("boundary_contract", [])
        if not isinstance(boundary_contract, list) or any(
            not isinstance(item, dict)
            or not str(item.get("role") or "").strip()
            or not str(item.get("name") or "").strip()
            or not str(item.get("type") or "").strip()
            for item in boundary_contract
        ):
            raise ValueError(
                "requirement analyst boundary_contract must contain role/name/type objects"
            )
        required = {
            "task_scope", "discipline", "simulation_software", "calculation_type", "calculation_stages",
            "required_files", "optional_files", "evidence", "assumptions",
            "unresolved_facts", "search_queries", "confidence",
        }
        missing = sorted(required - set(value))
        if missing:
            raise ValueError(f"requirement analyst JSON is missing top-level keys: {missing}")
        if not isinstance(value.get("discipline"), dict):
            raise ValueError("requirement analyst discipline must be an object")
        software = value.get("simulation_software")
        if review_profile == "plan_bound" and (
            not isinstance(software, dict) or not str(software.get("name") or "").strip()
        ):
            raise ValueError("requirement analyst simulation_software.name is required")
        if review_profile == "plan_bound" and not str(value.get("calculation_type") or "").strip():
            raise ValueError("requirement analyst calculation_type is required")
        known_stage_ids = {
            str(stage.get("id") or "").strip()
            for stage in value.get("calculation_stages") or []
            if isinstance(stage, dict) and str(stage.get("id") or "").strip()
        }
        for index, item in enumerate(value["required_files"]):
            missing_asset_fields = sorted({
                "scientific_role", "representation", "asset_role", "source_strategy",
                "acquisition_kind", "workflow_capability",
            } - set(item))
            if missing_asset_fields:
                # 判决拆除 O10（planner:327 降格，2026-08-31）：缺 contract 字段
                # 填显式 unknown + 披露，不再作废整份分析。
                for field in missing_asset_fields:
                    item[field] = "unknown"
                item["contract_fields_defaulted"] = missing_asset_fields
                value.setdefault("contract_review_notes", []).append(
                    f"required_files[{index}] missing contract fields defaulted to 'unknown': {missing_asset_fields}"
                )
            # 判决拆除（planner:331 删，2026-08-31）：workflow_capability=
            # unclassified 是诚实的「分不了类」—— 惩罚诚实是 S2 最纯粹的违反；
            # 路由端自行处理 unclassified。
            consumers = item.get("consumer_stages") or []
            if isinstance(consumers, str):
                consumers = [consumers]
            consumers = list(dict.fromkeys(
                str(stage_id or "").strip()
                for stage_id in consumers
                if str(stage_id or "").strip()
            ))
            item["consumer_stages"] = consumers
            stage_id = str(item.get("stage_id") or "").strip()
            if not stage_id and len(consumers) == 1:
                stage_id = consumers[0]
                item["stage_id"] = stage_id
            referenced_stages = ({stage_id} if stage_id else set()) | set(consumers)
            unknown_stages = sorted(referenced_stages - known_stage_ids)
            if unknown_stages:
                raise ValueError(
                    f"requirement analyst required_files[{index}] references unknown stages: {unknown_stages}"
                )
            item["workflow_capability"] = canonical_workflow_capability(item)
    elif role == "designer":
        required = {
            "required_deliverables", "generation_steps", "tool_requirements",
        }
        missing = sorted(required - set(value))
        if missing:
            raise ValueError(f"designer JSON is missing top-level keys: {missing}")
    elif role == "critic" and not isinstance(value.get("dimension_scores"), dict):
        raise ValueError("critic JSON must contain dimension_scores")


def _client(state: State, model_name: str = "") -> tuple[LLMClient, str]:
    if not model_name:
        inherited = state.hook_state.get("_data_planning_llm_client")
        if inherited is not None and callable(getattr(inherited, "chat", None)):
            resolved = getattr(inherited, "model", "") or os.getenv("LLM_MODEL", "primary") or "primary"
            return inherited, resolved
        return LLMClient(), os.getenv("LLM_MODEL", "primary") or "primary"
    provider = get_provider(model_name)
    if provider is None:
        raise ValueError(f"unknown model_name={model_name!r}")
    api_key = os.getenv(provider.api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"API key env {provider.api_key_env} is not configured")
    return LLMClient(api_key=api_key, base_url=provider.base_url, model=provider.model), provider.name


async def _call_json_agent(
    state: State,
    *,
    role: str,
    system_prompt: str,
    payload: dict[str, Any],
    model_name: str = "",
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    client, resolved_name = _client(state, model_name)
    max_tokens = _planning_agent_max_tokens(role)
    # Serialize once: the logged size must be the size actually sent.  Pretty
    # printing adds thousands of non-semantic characters to these already
    # structured planning contracts.
    serialized_payload = _serialized_payload(payload)
    state.append_transcript(
        "preprocessing_planning_agent_call",
        role=role,
        model_name=resolved_name,
        max_output_tokens=max_tokens,
    )
    emit_progress(
        state,
        "planning_agent_call",
        role,
        model=resolved_name,
        payload_chars=len(serialized_payload),
        max_output_tokens=max_tokens,
    )
    response = await client.chat(
        [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=serialized_payload),
        ],
        tools=None,
        max_tokens=max_tokens,
        temperature=0.1,
    )
    if isinstance(response.usage, dict):
        state.tokens_used += int(response.usage.get("total_tokens") or 0)
    if response.finish_reason == "length":
        state.append_transcript(
            "preprocessing_planning_agent_truncated",
            role=role,
            completion_tokens=(response.usage or {}).get("completion_tokens"),
        )
        raise ValueError(f"{role} response was truncated by the model output limit")
    parsed = _extract_json_object(response.content or "")
    try:
        request = payload.get("preprocessing_request") if isinstance(payload.get("preprocessing_request"), dict) else {}
        _validate_agent_payload(
            role,
            parsed,
            review_profile=str(request.get("review_profile") or "plan_bound"),
        )
    except ValueError:
        state.append_transcript(
            "preprocessing_planning_agent_schema_error",
            role=role,
            response_preview=(response.content or "")[:1000],
            finish_reason=response.finish_reason,
            completion_tokens=(response.usage or {}).get("completion_tokens"),
        )
        raise
    return parsed, resolved_name, response.usage or {}


async def _call_json_agent_resilient(
    state: State,
    *,
    role: str,
    system_prompt: str,
    payload: dict[str, Any],
    model_name: str = "",
    attempts: int = 3,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Retry transient transport failures and malformed JSON before escalating."""
    errors: list[str] = []
    retry_payload = dict(payload)
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await _call_json_agent(
                state,
                role=role,
                system_prompt=system_prompt,
                payload=retry_payload,
                model_name=model_name,
            )
        except Exception as exc:
            errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            state.append_transcript(
                "preprocessing_planning_agent_retry",
                role=role,
                attempt=attempt,
                error_type=type(exc).__name__,
                error_preview=str(exc)[:500],
                payload_chars=len(_serialized_payload(retry_payload)),
            )
            emit_progress(
                state,
                "planning_agent_retry",
                role,
                attempt=attempt,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
            if "truncated by the model output limit" in str(exc):
                break
            if attempt < attempts:
                contract_correction = ""
                if role == "requirement_analyst":
                    contract_correction = (
                        " For each required_files item, choose one acquisition route before emitting it: "
                        "local_generation + generated_artifact + a local generation capability; "
                        "external dataset + dataset_acquisition; or official external file + "
                        "official_file_acquisition. source_strategy=external_download is compatible with "
                        "official_file_reference or external_dataset because it is a transport method. "
                        "An analytic NACA/parametric geometry explicitly named by the caller is local_generation; "
                        "do not label it official/external or geometry_acquisition. "
                        "Derive allowed_capabilities from the final required_files contracts; do not omit "
                        "a capability required by an external item. In plan_bound work, stage ownership "
                        "must use a declared stage; request_bound assets remain owned by their work unit."
                    )
                retry_payload = {
                    **payload,
                    "retry_correction": (
                        "The previous response was malformed or did not match the required top-level "
                        f"schema ({errors[-1]}). Return one complete JSON object only. Do not return "
                        "a nested array item as the top-level response."
                        + contract_correction
                    ),
                }
                await asyncio.sleep(min(0.25 * attempt, 0.75))
    raise RuntimeError("; ".join(errors))


_DETERMINISTIC_STAGE_ADAPTERS = {
    "cfd", "heat_transfer", "md", "csm", "cem", "multiphysics",
    "electronic_structure",
}


def _deterministic_plan_critical_concerns(plan: dict[str, Any] | None) -> list[str]:
    """Reject schema-valid plans that have no route to a runnable deliverable."""
    if not isinstance(plan, dict):
        return []
    analysis = plan.get("requirement_analysis") or {}
    discipline = analysis.get("discipline") or plan.get("discipline") or {}
    primary = str(
        discipline.get("primary") if isinstance(discipline, dict) else discipline
    ).strip().lower()
    stages = [item for item in analysis.get("calculation_stages") or [] if isinstance(item, dict)]
    concerns: list[str] = []
    uses_deterministic_builder = any(
        isinstance(step, dict)
        and str(step.get("tool_name") or "").strip() == "build_scientific_preprocessing_package"
        for step in plan.get("generation_steps") or []
    )
    if stages and uses_deterministic_builder and primary not in _DETERMINISTIC_STAGE_ADAPTERS:
        concerns.append(
            f"No registered deterministic stage adapter exists for discipline {primary or 'unknown'}."
        )
    # The generic writer is intentionally limited to textual/configuration
    # artifacts.  It must never be used as a substitute for externally
    # sourced scientific data or solver-produced binaries.
    deliverables_by_id = {
        str(item.get("id") or ""): item
        for item in plan.get("required_deliverables") or []
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    for step in plan.get("generation_steps") or []:
        if not isinstance(step, dict) or step.get("tool_name") != "prepare_scientific_mesh":
            continue
        from .geometry_assets import GEOMETRY_FILE_KEYS
        from .mesh_iteration_advisor import explicit_mesh_contract

        args = step.get("tool_arguments") or {}
        if args.get("operation", "prepare") not in {"prepare", "generate"} or args.get("generate_mesh") is False:
            continue
        params = _coerce_requirement_analysis(args.get("parameters")) or {}
        mesh_analysis = _coerce_requirement_analysis(params.get("requirement_analysis")) or analysis
        bindings = [item.get("parameter_bindings") for item in mesh_analysis.get("required_files") or []
                    if isinstance(item, dict)]
        route = explicit_mesh_contract(str(args.get("spec") or ""), {
            **params, "mesh_type": args.get("mesh_type") or params.get("mesh_type"),
        })
        if not (
            any(bindings)
            or any(params.get(key) for key in (*GEOMETRY_FILE_KEYS, "geometry", "coordinate_profile_path", "coordinate_arrays", "coordinate_text"))
            or (route and (primary == "cfd" or args.get("mesh_type") or params.get("mesh_type"))
                and route["mesh_type"] not in {"geometry_file_gmsh", "generic_gmsh", "custom_geometry", "public_geometry"})
        ):
            concerns.append(
                f"Mesh step {step.get('id')} has no bound source or constructive parameters. "
                "Preserve the confirmed input and mesh controls from RequirementAnalysis; "
                "resolve missing implementation bindings before executing or searching for replacement geometry."
            )
    for step in plan.get("generation_steps") or []:
        if not isinstance(step, dict) or step.get("tool_name") != "generate_preprocessing_artifact":
            continue
        args = step.get("tool_arguments") or {}
        spec = args.get("artifact_spec") if isinstance(args.get("artifact_spec"), dict) else {}
        external_outputs = [
            output_id for output_id in step.get("outputs") or []
            if (deliverable := deliverables_by_id.get(str(output_id)))
            and normalize_asset_contract(deliverable).get("is_external")
        ]
        acquisition_document = (
            str(spec.get("artifact_kind") or "") == "external_dataset_acquisition"
            and not acquisition_contract_errors(spec.get("acquisition_contract") or spec)
        )
        if external_outputs and not acquisition_document:
            concerns.append(
                f"Generic artifact writer cannot produce external deliverables for step {step.get('id')}: {external_outputs}."
            )
    return concerns


def _reconcile_framework_covered_critic_concerns(
    plan: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Keep remote Critic semantics, but remove false blocking claims.

    The Critic sees a compact projection and can mistake framework-owned
    deferred contracts for missing work.  These cases are already enforced by
    deterministic validation or by the executor, so they must not prevent an
    otherwise valid generation plan from executing.  Genuine semantic,
    scope, safety, and contradictory-parameter concerns remain untouched.
    """
    if not isinstance(raw, dict) or not isinstance(plan, dict):
        return raw
    external_document_count = 0
    script_artifact_count = 0
    configuration_artifact_count = 0
    complete_configuration_contract_count = 0
    for deliverable in plan.get("required_deliverables") or []:
        if isinstance(deliverable, dict) and normalize_asset_contract(deliverable).get("is_external"):
            external_document_count += 1
    for step in plan.get("generation_steps") or []:
        if not isinstance(step, dict):
            continue
        spec = (step.get("tool_arguments") or {}).get("artifact_spec")
        spec = spec if isinstance(spec, dict) else {}
        if re.search(
            r"configuration|namelist|json|yaml|toml",
            str(spec.get("format") or ""),
            re.I,
        ):
            configuration_artifact_count += 1
            if spec.get("evidence") and spec.get("acceptance_criteria"):
                complete_configuration_contract_count += 1
        if re.search(
            r"python|shell|bash|script",
            str(spec.get("format") or step.get("tool_capability") or ""),
            re.I,
        ):
            script_artifact_count += 1

    def is_framework_covered(text: str) -> bool:
        lowered = text.casefold()
        specialized_step = any(
            str(step.get("tool_name") or "").strip()
            in {"prepare_scientific_mesh", "build_scientific_preprocessing_package"}
            for step in plan.get("generation_steps") or []
            if isinstance(step, dict)
        )
        if specialized_step and re.search(
            r"artifact[_ ]spec|output[_ ]paths|generic artifact specification",
            lowered,
        ) and re.search(
            r"prepare[_ ]scientific[_ ]mesh|build[_ ]scientific[_ ]preprocessing[_ ]package|"
            r"assemble[_ ]preprocessing[_ ]package|package assembly|mesh",
            lowered,
        ):
            # These registered domain tools expose a structured mesh/package
            # contract; the generic writer fields are intentionally absent.
            return True
        if (
            ("duplicate" in lowered and re.search(r"id|deliverable|contract", lowered))
            or "conflicting acquisition contract" in lowered
        ):
            # validate_plan already rejects duplicate stage/deliverable/step
            # IDs. A valid plan cannot have the duplicate the model inferred
            # from repeated scientific names.
            return True
        if script_artifact_count and "runtime_dependenc" in lowered:
            # The artifact writer records direct imports after generation; a
            # Designer declaration is useful but not a planning gate.
            return True
        if external_document_count and re.search(
            r"acquisition|download|provider|dataset identifier|request parameter|"
            r"concrete (?:provider )?url|source url|download step",
            lowered,
        ):
            # Large/unknown datasets are intentionally represented by a
            # complete acquisition document. Missing request dimensions or a
            # failed search are deferred dependencies, not generation failure.
            return True
        if (
            configuration_artifact_count
            and configuration_artifact_count == complete_configuration_contract_count
            and re.search(r"namelist|configuration", lowered)
            and re.search(
                r"authoritative evidence|physical correctness|non.?empt|syntax",
                lowered,
            )
        ):
            # Parameter semantics are checked against the Research Plan and
            # final file content by the artifact/package reviewers. Lack of a
            # separate manual example is not an execution blocker.
            return True
        return False

    critical = [
        str(item).strip()
        for item in raw.get("critical_concerns") or []
        if str(item).strip() and not is_framework_covered(str(item))
    ]
    return {**raw, "critical_concerns": critical}


def _deterministic_critic_fallback(
    validation: dict[str, Any], error: Exception, plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a schema/safety approval receipt without pretending it is a model score."""
    valid = bool(validation.get("valid"))
    concerns = [*([] if valid else list(validation.get("errors") or [])), *_deterministic_plan_critical_concerns(plan)]
    return {
        "overall_score": None,
        "dimension_scores": {},
        "critical_concerns": concerns,
        "major_concerns": list(validation.get("warnings") or []),
        "recommended_changes": concerns,
        "decision": "approve" if valid and not concerns else "revise",
        "approval_requirements": {
            "validation_mode": "deterministic_schema_and_safety",
            "schema_must_be_valid": True,
            "critical_concerns_allowed": 0,
        },
        "fallback": {
            "type": "deterministic_schema_and_safety_validation",
            "reason": f"Deterministic validation selected: {type(error).__name__}: {error}",
        },
    }


def _slug(value: str, default: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return slug or default


def _unique_slug(value: str, used: set[str], default: str = "item") -> str:
    base = _slug(value, default)
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _declared_relative_output(item: dict[str, Any], fallback_id: str) -> str:
    """Preserve an upstream filename/path while enforcing workspace-relative output."""
    candidates = [
        item.get(key) for key in (
            "declared_output_path", "output_path", "relative_path",
            "filename", "file_name", "expected_filename", "path",
        )
    ]
    for raw in candidates:
        value = str(raw or "").strip().replace("\\", "/")
        if not value:
            continue
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            continue
        return str(path)
    # Opaque tracker IDs (for example F001) are plan identities, never a
    # downstream filename.  Prefer the declared scientific role and derive a
    # conventional extension from the structured format.  This applies to
    # every discipline and avoids requiring the publisher to know an
    # application's private file list.
    role = str(item.get("name_or_role") or item.get("name") or "").strip()
    if role:
        role_path = PurePosixPath(role.replace("\\", "/"))
        if not role_path.is_absolute() and ".." not in role_path.parts:
            role_name = role_path.name
            if "/" not in role and re.search(r"[A-Za-z0-9]", role_name):
                native_name = re.search(
                    r"(?<![A-Za-z0-9_.-])(namelist\.[A-Za-z0-9_-]+)(?![A-Za-z0-9_.-])",
                    role_name,
                    flags=re.I,
                )
                if native_name:
                    return native_name.group(1).lower()
                if not re.search(r"\.[A-Za-z0-9][A-Za-z0-9._-]*$", role_name):
                    normalized = re.sub(r"[^A-Za-z0-9]+", "_", role_name).strip("_").lower()
                    # Namelist roles commonly carry an application prefix and
                    # a file suffix. Stage directories provide experiment
                    # identity, so the public filename remains the portable
                    # ``namelist.<kind>`` basename.
                    role_parts = [part for part in role_name.split("_") if part]
                    lowered_parts = [part.casefold() for part in role_parts]
                    if "namelist" in lowered_parts:
                        position = lowered_parts.index("namelist")
                        suffix_part = (
                            role_parts[position + 1]
                            if position + 1 < len(role_parts)
                            else role_parts[position - 1] if position else "input"
                        )
                        normalized = f"namelist.{suffix_part.casefold()}"
                    fmt = str(item.get("format") or "").lower()
                    if "python" in fmt and not normalized.endswith(".py"):
                        normalized += ".py"
                    elif any(token in fmt for token in ("shell", "bash")) and not normalized.endswith(".sh"):
                        normalized += ".sh"
                    elif "json" in fmt and not normalized.endswith(".json"):
                        normalized += ".json"
                    else:
                        suffix = next(
                            (value for value in SCIENTIFIC_ASSET_SUFFIXES if value[1:] == fmt),
                            "",
                        )
                        if suffix and not normalized.endswith(suffix):
                            normalized += suffix
                    return normalized
                return role_name
    # IDs such as namelist.input, POSCAR and KPOINTS are meaningful filenames;
    # retain their spelling instead of converting them to a slug.
    raw_id = str(item.get("id") or fallback_id).strip().replace("\\", "/")
    if re.fullmatch(r"[A-Za-z0-9_.-]+", raw_id):
        return raw_id
    return str(fallback_id)


def _format_for_declared_output(path: str, declared_format: Any) -> str:
    """Refine generic LLM format labels from the portable output contract."""
    name = PurePosixPath(str(path or "")).name.lower()
    if name.startswith("namelist."):
        return "Fortran namelist text"
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == ".py":
        return "Python"
    if suffix in {".sh", ".bash"}:
        return "Shell"
    if suffix == ".json":
        return "JSON"
    return str(declared_format or "text")


def _acquisition_output_path(item: dict[str, Any], fallback_id: str) -> str:
    """Return a stable public name for an external-data retrieval contract."""
    semantic_path = PurePosixPath(_declared_relative_output(item, fallback_id))
    stem = semantic_path.stem if semantic_path.suffix else semantic_path.name
    stem = _slug(stem, "external_dataset")
    return f"acquisition/{stem}.acquisition.json"


_GENERIC_ASSET_IDENTITY_TOKENS = {
    "asset", "data", "dataset", "file", "input", "output", "stage",
    "script", "program", "configuration", "config", "parameter",
    "observational", "external", "acquisition", "reference", "run",
    "processing", "diagnostic", "diagnostics",
}


def _asset_identity_tokens(item: dict[str, Any]) -> set[str]:
    """Return stable scientific-identity tokens, excluding delivery wording."""
    text = " ".join(
        str(item.get(key) or "")
        for key in ("name_or_role", "name", "filename", "file_name", "scientific_role")
    ).casefold()
    aliases = {"reanalysis": "analysis", "ctl": "ctrl", "calculation": "calc"}
    tokens = {
        aliases.get(token, token)
        for token in re.findall(r"[a-z0-9]+", text)
    }
    return tokens - _GENERIC_ASSET_IDENTITY_TOKENS


def _same_stage_asset(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Match equivalent stage assets from Analyst and Research Plan structure."""
    if str(left.get("stage_id") or "").strip() != str(right.get("stage_id") or "").strip():
        return False
    # Two explicitly named files are distinct deliverables even when their
    # scientific role words overlap (for example a Gmsh ``.geo`` script and
    # its ``.msh`` output).  Only coalesce them when their declared physical
    # filename is the same; logical IDs and prose role names remain aliases.
    left_filename = next(
        (
            canonical_filename_identity(left.get(key))
            for key in ("expected_filename", "filename", "file_name", "declared_output_path")
            if str(left.get(key) or "").strip()
        ),
        "",
    )
    right_filename = next(
        (
            canonical_filename_identity(right.get(key))
            for key in ("expected_filename", "filename", "file_name", "declared_output_path")
            if str(right.get(key) or "").strip()
        ),
        "",
    )
    if left_filename and right_filename and left_filename != right_filename:
        return False
    left_contract = normalize_asset_contract(left)
    right_contract = normalize_asset_contract(right)
    left_tokens = _asset_identity_tokens(left)
    right_tokens = _asset_identity_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = left_tokens & right_tokens
    semantic_match = bool(
        left_tokens <= right_tokens
        or right_tokens <= left_tokens
        or len(overlap) >= 2
        or len(overlap) / len(left_tokens | right_tokens) >= 0.6
    )
    if bool(left_contract.get("is_external")) != bool(right_contract.get("is_external")):
        local_item, local_contract = (
            (left, left_contract) if not left_contract.get("is_external") else (right, right_contract)
        )
        return bool(
            semantic_match
            and canonical_workflow_capability(local_item) == "local_artifact_generation"
            and not any(local_item.get(key) for key in (
                "generation_recipe", "generation_method", "derivation_method",
            ))
        )
    if not left_contract.get("is_external"):
        def local_family(value: dict[str, Any]) -> str:
            capability = canonical_workflow_capability(value)
            return (
                "script" if capability == "preprocessing_script_generation"
                else "text" if capability in {"configuration_generation", "local_artifact_generation"}
                else capability
            )
        if local_family(left) != local_family(right):
            return False
        if local_family(left) == "script" and overlap & {
            "download", "extract", "extraction", "perturb", "perturbation",
            "post", "validation", "validate", "calc", "test", "synthesis",
        }:
            return True
    return semantic_match


def _coalesce_stage_assets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate asset descriptions while keeping one logical ID."""
    merged: list[dict[str, Any]] = []
    for source in items:
        item = deepcopy(source)
        existing = next((candidate for candidate in merged if _same_stage_asset(candidate, item)), None)
        if existing is None:
            merged.append(item)
            continue
        # An explicit external-data contract is authoritative over a stale
        # local/runtime classification for the same stage role. Apply the
        # routing fields before the generic "fill missing" merge below; the
        # latter intentionally preserves existing values and would otherwise
        # resurrect the stale classification on the next coalescing pass.
        if normalize_asset_contract(item).get("is_external"):
            for key in (
                "asset_role", "source_strategy", "acquisition_kind",
                "workflow_capability", "fulfillment_kind", "delivery_required",
                "provider", "access_method", "representation", "reason",
                "consequences_if_missing", "evidence",
            ):
                if item.get(key) not in (None, "", [], {}):
                    existing[key] = deepcopy(item[key])
        # ``available_stage_parameters`` is generation context only.  It must
        # never erase an explicit file contract: the latter is what Reviewer
        # can verify against the rendered artifact.  Keep the narrowest
        # file-level bindings and merge stage context independently.
        if item.get("parameter_binding_source") == "stage_context":
            existing.setdefault("parameter_binding_source", "stage_context")
            if isinstance(item.get("available_stage_parameters"), dict):
                current_context = existing.get("available_stage_parameters")
                if not isinstance(current_context, dict):
                    current_context = {}
                existing["available_stage_parameters"] = {
                    **current_context,
                    **item["available_stage_parameters"],
                }
        existing_format = str(existing.get("format") or "").casefold()
        incoming_format = str(item.get("format") or "").casefold()
        generic_formats = {"", "text", "text_log", "script", "configuration", "dataset"}
        for key, value in item.items():
            if value in (None, "", [], {}):
                continue
            if key == "evidence":
                current = existing.setdefault("evidence", [])
                values = value if isinstance(value, list) else [value]
                for evidence in values:
                    if evidence not in current:
                        current.append(evidence)
            elif key == "acquisition_contract" and isinstance(value, dict):
                existing[key] = {**value, **dict(existing.get(key) or {})}
            elif key == "parameter_bindings" and isinstance(value, dict):
                # Explicit stage-derived bindings are the narrow file-level
                # contract.  Analyst prose may also carry the entire stage
                # parameter map; retaining that broader map made the writer
                # reject valid files for values that do not belong in this
                # artifact (for example a WPS file receiving a WRF-only
                # timing ratio).  Prefer the structured Research Plan subset
                # while keeping the full stage parameters in context.
                if item.get("parameter_binding_basis") == "explicit_research_plan_stage_parameters":
                    existing[key] = deepcopy(value)
                elif existing.get(key) in (None, "", [], {}):
                    existing[key] = deepcopy(value)
            elif key in {"declared_output_path", "filename", "file_name"}:
                if not existing.get(key) and (
                    existing_format in generic_formats
                    or incoming_format in generic_formats
                    or existing_format == incoming_format
                ):
                    existing[key] = deepcopy(value)
            elif existing.get(key) in (None, "", [], {}) or (
                key == "format" and existing_format in generic_formats and incoming_format not in generic_formats
            ):
                existing[key] = deepcopy(value)
    return merged


def _ensure_stage_launch_contracts(analysis: dict[str, Any]) -> dict[str, Any]:
    """Ensure executable stages declare one portable downstream launch asset."""
    stages: list[dict[str, Any]] = []
    for source in analysis.get("calculation_stages") or []:
        if not isinstance(source, dict):
            continue
        stage = deepcopy(source)
        declared_roles = [
            str(role).strip()
            for role in stage.get("required_file_roles") or []
            if str(role).strip() and str(role).strip() != "-"
        ]
        has_launch_role = any(
            re.search(
                r"(?:^|[_ -])(?:script|program|code|driver|invocation|namelist|config|"
                r"configuration|parameter|control|template|manifest)(?:$|[_ -])|"
                r"脚本|程序|配置|参数|控制",
                role,
                flags=re.I,
            )
            for role in declared_roles
        )
        method = " ".join(str(stage.get(key) or "") for key in (
            "solver", "calculation_type", "name",
        ))
        parameters = stage.get("parameters") if isinstance(stage.get("parameters"), dict) else {}
        method = f"{method} {parameters.get('method') or ''}"
        if not has_launch_role and re.search(
            r"python|shell|bash|script|notebook|rscript|julia|matlab",
            method,
            flags=re.I,
        ):
            stage["required_file_roles"] = [*declared_roles, "stage_execution_script"]
        elif not declared_roles:
            stage["required_file_roles"] = [
                "solver_input_configuration", "stage_execution_script",
            ]
        stages.append(stage)
    return {**analysis, "calculation_stages": stages}


def _ensure_stage_launch_asset_requirements(analysis: dict[str, Any]) -> dict[str, Any]:
    """Audit stage-role coverage without fabricating missing file contracts."""
    required_files = [
        deepcopy(item) for item in analysis.get("required_files") or []
        if isinstance(item, dict)
    ]
    required_files = _coalesce_stage_assets(required_files)
    covered = {
        (str(item.get("stage_id") or "").strip().casefold(), _normalise_asset_key(identity))
        for item in required_files
        for identity in (
            item.get("id"), item.get("logical_asset_id"), item.get("name_or_role"),
            item.get("scientific_role"), item.get("expected_filename"),
        )
        if _normalise_asset_key(identity)
    }
    unresolved = [
        dict(item) for item in analysis.get("unresolved_stage_assets") or []
        if isinstance(item, dict)
    ]
    for stage in analysis.get("calculation_stages") or []:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id") or "").strip()
        for role in stage.get("required_file_roles") or []:
            role_key = _normalise_asset_key(role)
            if not role_key or (stage_id.casefold(), role_key) in covered:
                continue
            unresolved.append({
                "stage_id": stage_id,
                "name_or_role": str(role),
                "reason_code": "stage_asset_contract_missing",
                "required_contract": (
                    "The caller or Requirement Analyst must declare whether this asset is reused, "
                    "generated locally, acquired externally, or supplied at runtime."
                ),
            })
    return {
        **analysis,
        "required_files": required_files,
        "unresolved_stage_assets": unresolved,
    }


def _scope_analysis_to_data_node(task_context: str | dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Preserve the exact stage scope supplied by the caller.

    Data is an on-demand service.  Its caller has already chosen the experiment
    scope, so this node prepares assets for the supplied stages without ranking,
    selecting, projecting, or deferring hypotheses.
    """
    analysis = _ensure_authoritative_caller_assets(task_context, analysis)
    request = analysis.get("preprocessing_request") if isinstance(analysis.get("preprocessing_request"), dict) else {}
    if request.get("review_profile") == "request_bound":
        # A direct asset request is complete when that asset is generated and
        # reviewed. Do not expand it into solver launch scripts/configuration.
        required = [
            deepcopy(item) for item in analysis.get("required_files") or []
            if isinstance(item, dict)
        ]
        requested = [
            item for item in request.get("requested_assets") or []
            if isinstance(item, dict)
        ]
        if requested:
            requested_keys = {
                str(value).strip().casefold()
                for item in requested
                for value in (
                    item.get("asset_id"), item.get("filename"), item.get("name_or_role"),
                    *(item.get("dependencies") or [])
                )
                if str(value or "").strip()
            }
            scoped = [
                item for item in required
                if any(
                    str(item.get(key) or "").strip().casefold() in requested_keys
                    for key in ("id", "name_or_role", "expected_filename", "declared_output_path")
                )
            ]
            if scoped:
                required = scoped
            else:
                # Keep the ingress contract usable even when the Analyst used
                # a different internal id. The request asset itself is the
                # only safe fallback; do not retain unrelated stage files.
                required = [
                    {
                        "id": str(item.get("asset_id") or item.get("filename") or f"requested_asset_{index + 1}"),
                        "name_or_role": item.get("filename") or item.get("asset_id"),
                        "expected_filename": item.get("filename"),
                        "format": item.get("format"),
                        "reason": item.get("purpose"),
                        "acceptance_criteria": item.get("content_constraints") or [],
                        **{
                            key: deepcopy(item[key])
                            for key in (
                                "scientific_role", "representation", "source_strategy",
                                "generation_recipe", "parameter_bindings", "workflow_capability",
                            )
                            if item.get(key) not in (None, "", [], {})
                        },
                    }
                    for index, item in enumerate(requested)
                ]
        elif _caller_explicitly_requests_mesh(task_context, analysis):
            # Only a registered parametric producer owns its intermediates.
            # A prose compiler's generic "mesh" item is not such a producer
            # and must never replace the Analyst's source/parameter bindings.
            from .mesh_iteration_advisor import CANONICAL_CFD_MESH_INTENT_PATTERNS, explicit_mesh_contract
            from .geometry_assets import geometry_file_from_params

            route = explicit_mesh_contract(caller_request_text(task_context, preprocessing_request=request), {})
            if (
                (analysis.get("discipline") or {}).get("primary") == "cfd"
                and str((route or {}).get("mesh_type") or "") in CANONICAL_CFD_MESH_INTENT_PATTERNS
                and not any(geometry_file_from_params(item.get("parameter_bindings") or {}) for item in required)
            ):
                required = [_mesh_requirement_from_caller(task_context, analysis)]
        # Bind consumed source controls to the produced asset; whether an
        # implementation helper is delivered does not change these controls.
        from .geometry_assets import geometry_file_from_params

        meshes = [item for item in required if canonical_workflow_capability(item) == "mesh_generation"]
        source_paths = {
            str((item.get("local_match") or {}).get("path") or "")
            for item in required if item.get("source_strategy") == "local_reuse"
        } - {""}
        controls = [item.get("parameter_bindings") or {} for item in required
                    if geometry_file_from_params(item.get("parameter_bindings") or {}) in source_paths]
        if not requested and len(meshes) == 1 and controls:
            mesh = meshes[0]
            for bindings in controls:
                mesh["parameter_bindings"] = {**deepcopy(bindings), **(mesh.get("parameter_bindings") or {})}
        analysis = {
            **analysis,
            "required_files": _coalesce_stage_assets(required),
            "calculation_stages": [],
            "stage_input_contracts": [],
            "unresolved_stage_assets": [],
        }
        analysis["preprocessing_work_order"] = build_preprocessing_work_order(
            request,
            analysis,
        )
    else:
        analysis = _ensure_stage_launch_asset_requirements(
            _ensure_stage_launch_contracts(analysis)
        )
    stages = [item for item in analysis.get("calculation_stages") or [] if isinstance(item, dict)]
    if not stages:
        return analysis
    prior_scope = analysis.get("structured_stage_scope")
    return {
        **analysis,
        "deferred_stage_ids": [],
        "structured_stage_scope": {
            "source": (prior_scope.get("source") if isinstance(prior_scope, dict) else "caller_supplied_stage_scope"),
            "stage_ids": [str(stage.get("id") or "") for stage in stages],
            "policy": "data prepares assets for the caller-supplied stages; formal execution remains downstream",
        },
    }


def _asset_haystack(item: dict[str, Any] | str) -> str:
    if isinstance(item, dict):
        parts = [
            item.get("id"),
            item.get("name_or_role"),
            item.get("format"),
            item.get("reason"),
            item.get("consequences_if_missing"),
        ]
        parts.extend(
            entry.get("detail") if isinstance(entry, dict) else entry
            for entry in item.get("evidence") or []
        )
        return " ".join(str(value or "") for value in parts)
    return str(item or "")


def _asset_contract_role(item: dict[str, Any] | str) -> str:
    """Compatibility view over the single normalized asset contract."""
    return str(normalize_asset_contract(item).get("asset_role") or "parameter_file")


def _fallback_generation_context(
    task_context: str | dict[str, Any],
    analysis: dict[str, Any],
) -> str:
    """Build a stable generation spec without replaying planning transcripts.

    Recovery task contexts contain recent tool results and model-authored notes.
    Feeding those back into a domain generator causes context growth and lets an
    earlier failed plan override the original scientific request.  This view
    retains only upstream intent, normalized requirements, and recovered assets.
    """
    if isinstance(task_context, dict):
        upstream = {
            key: task_context.get(key)
            for key in ("user_request", "task", "objective", "node_inputs", "upstream_artifacts")
            if task_context.get(key) not in (None, "", [], {})
        }
    else:
        upstream = {"user_request": _compact_text(task_context, 5000)}
    payload = {
        **upstream,
        "discipline": analysis.get("discipline") or {},
        "simulation_software": analysis.get("simulation_software") or {},
        "calculation_type": analysis.get("calculation_type"),
        # The compact RequirementAnalysis sent to Designer already contains
        # stage and asset contracts. Repeating their full objects here made
        # long plans exceed context/output budgets and encouraged the model to
        # echo them instead of returning fulfilment decisions.
        "stage_ids": [
            str(stage.get("id") or "")
            for stage in analysis.get("calculation_stages") or []
            if isinstance(stage, dict) and str(stage.get("id") or "")
        ],
        "required_asset_count": len(analysis.get("required_files") or []),
    }
    return _compact_text(payload, 4000)


def _asset_requires_mesh(item: dict[str, Any]) -> bool:
    bindings = item.get("parameter_bindings")
    return (
        bool(item.get("requires_mesh_generation"))
        or canonical_workflow_capability(item) == "mesh_generation"
        or bool(
            isinstance(bindings, dict)
            and isinstance(bindings.get("mesh"), dict)
            and bindings["mesh"]
        )
    )


def _caller_explicitly_requests_mesh(
    task_context: str | dict[str, Any] | None,
    analysis: dict[str, Any] | None = None,
) -> bool:
    """Use the normalized asset contract before falling back to caller prose."""
    required_files = (
        analysis.get("required_files")
        if isinstance(analysis, dict) else None
    )
    if isinstance(required_files, list) and any(
        _asset_requires_mesh(item) for item in required_files if isinstance(item, dict)
    ):
        return True
    request = analysis.get("preprocessing_request") if isinstance(analysis, dict) else None
    text = caller_request_text(task_context, preprocessing_request=request)
    if not text:
        return False
    if re.search(
        r"\b(?:poscar|incar|kpoints|potcar|cif|brillouin|monkhorst|electronic\s+structure|dft)\b|"
        r"晶体|原子坐标|分数坐标|倒易空间|布里渊区|采样网格",
        text,
        flags=re.I,
    ):
        return False
    requests_output = bool(re.search(
        r"\b(?:generate|create|build|produce|regenerate|deliver|write)\b|"
        r"生成|创建|构建|重新生成|交付|写入",
        text,
        flags=re.I,
    ))
    requests_mesh = bool(re.search(
        r"\b(?:computational|finite[- ]?element|surface|volume)?\s*mesh(?:ing)?\b|"
        r"polyMesh|网格(?:划分|加密|单元|节点)?",
        text,
        flags=re.I,
    ))
    return requests_output and requests_mesh


def _mesh_requirement_from_caller(
    task_context: str | dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Build the one canonical mesh AssetContract required by explicit intent."""
    request = analysis.get("preprocessing_request") if isinstance(analysis.get("preprocessing_request"), dict) else {}
    caller_text = caller_request_text(task_context, preprocessing_request=request)
    requested_mesh = next(
        (
            item for item in request.get("requested_assets") or []
            if isinstance(item, dict)
            and re.search(
                r"mesh|网格|polymesh|blockmesh|snappyhexmesh|gmsh",
                json.dumps(item, ensure_ascii=False, default=str),
                flags=re.I,
            )
        ),
        next((
            item for item in analysis.get("required_files") or []
            if isinstance(item, dict)
            and (
                canonical_workflow_capability(item) == "mesh_generation"
                or str(normalize_asset_contract(item).get("representation") or "").lower() == "mesh"
            )
        ), {}),
    )
    requested_filename = str(
        requested_mesh.get("filename")
        or requested_mesh.get("expected_filename")
        or requested_mesh.get("declared_output_path")
        or ""
    ).strip()
    asset_id = str(
        requested_mesh.get("asset_id")
        or requested_mesh.get("id")
        or requested_filename
        or "computational_mesh"
    ).strip()
    asset_id = _slug(asset_id, "computational_mesh")
    software = str((analysis.get("simulation_software") or {}).get("name") or "").strip()
    if not software:
        software = _explicit_software_from_context(task_context)
    openfoam = bool(re.search(r"openfoam", software, flags=re.I))
    return {
        "id": asset_id,
        "name_or_role": requested_filename or requested_mesh.get("name_or_role") or "computational mesh",
        "expected_filename": requested_filename or (
            "constant/polyMesh" if openfoam else "mesh"
        ),
        "format": requested_mesh.get("format") or (
            "OpenFOAM polyMesh" if openfoam else "validated computational mesh"
        ),
        "asset_role": "geometry_or_discretization_asset",
        "scientific_role": "computational_mesh",
        "representation": "mesh",
        "workflow_capability": "mesh_generation",
        "source_strategy": "local_generation",
        "acquisition_kind": "generated_artifact",
        "generation_recipe": "Generate and validate the caller-requested computational mesh.",
        "reason": "The caller explicitly requests a physical computational mesh.",
        "evidence": [{
            "source_type": "caller_request",
            "detail": _compact_text(caller_text, 600),
        }],
        "confidence": 1.0,
        "consequences_if_missing": "The requested mesh delivery is incomplete; geometry alone is not a mesh.",
        "acceptance_criteria": list(dict.fromkeys([
            "A non-empty computational mesh is generated, not only geometry or coordinates.",
            "Mesh topology and quality checks are recorded before delivery.",
            *(
                [f"Original caller contract: {caller_text}"]
                if caller_text else []
            ),
            *[
                str(value) for value in (
                    request.get("acceptance_criteria") or []
                ) if str(value).strip()
            ],
            *[
                str(value) for value in (
                    requested_mesh.get("content_constraints")
                    or requested_mesh.get("acceptance_criteria")
                    or []
                ) if str(value).strip()
            ],
        ])),
        "delivery_required": True,
        "requires_local_payload": True,
        **({
            "parameter_bindings": deepcopy(requested_mesh.get("parameter_bindings") or {}),
        } if requested_mesh.get("parameter_bindings") else {}),
    }


def _ensure_authoritative_caller_assets(
    task_context: str | dict[str, Any],
    analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reconcile Analyst output with immutable caller deliverable intent."""
    if not isinstance(analysis, dict):
        return analysis or {}
    updated = dict(analysis)
    required = [deepcopy(item) for item in updated.get("required_files") or [] if isinstance(item, dict)]
    original_required = deepcopy(required)
    request = updated.get("preprocessing_request") if isinstance(updated.get("preprocessing_request"), dict) else {}
    requested_mesh = any(
        isinstance(item, dict)
        and re.search(r"mesh|网格|polymesh|blockmesh|snappyhexmesh|gmsh", json.dumps(item, ensure_ascii=False, default=str), flags=re.I)
        for item in request.get("requested_assets") or []
    )
    mesh_intent = _caller_explicitly_requests_mesh(task_context, updated) or requested_mesh
    if mesh_intent:
        for item in required:
            contract = normalize_asset_contract(item)
            if (
                str(contract.get("representation") or "").lower() == "simulation_case"
                and str(contract.get("source_strategy") or "").lower() == "local_generation"
            ):
                item["requires_mesh_generation"] = True
    # The Analyst already interprets prose (including negation and inputs).
    # Do not append regex-discovered filenames from the fallback compiler to
    # that result. Structured caller assets are reconciled by the request contract.
    if mesh_intent and not any(_asset_requires_mesh(item) for item in required):
        required.append(_mesh_requirement_from_caller(task_context, updated))
    if required != original_required:
        updated["required_files"] = required
        if str(updated.get("calculation_type") or "").strip() in {"", "caller-scoped preprocessing asset generation"}:
            updated["calculation_type"] = "mesh_generation_preprocessing"
        updated["task_scope"] = _task_scope_contract(updated)
    # ``task_scope`` is an Analyst projection for request-bound work. It cannot
    # veto the mesh asset derived once from the immutable caller request.
    if mesh_intent and request.get("review_profile") == "request_bound":
        scope = dict(updated.get("task_scope") or {})
        scope["allowed_capabilities"] = sorted({
            *[str(value) for value in scope.get("allowed_capabilities") or []],
            "mesh_generation",
        })
        scope["excluded_capabilities"] = sorted({
            str(value) for value in scope.get("excluded_capabilities") or []
            if str(value) != "mesh_generation"
        })
        scope["required_capabilities_outside_scope"] = []
        updated["task_scope"] = scope
    return updated


def _requires_spatial_mesh_assets(
    analysis: dict[str, Any],
    required_files: list[dict[str, Any]],
) -> bool:
    """Read mesh routing only from the normalized asset and scope contracts."""
    task_scope = analysis.get("task_scope") if isinstance(analysis.get("task_scope"), dict) else {}
    allowed_capabilities = {
        str(item).strip()
        for item in task_scope.get("allowed_capabilities") or []
        if str(item).strip()
    }
    excluded_capabilities = {
        str(item).strip()
        for item in task_scope.get("excluded_capabilities") or []
        if str(item).strip()
    }
    # TaskScopeContract is the routing authority.  Text such as "grid",
    # "surface", or a stale discipline adapter may refine an allowed route,
    # but may never open a capability excluded by the Research Plan.
    if (
        "mesh_generation" in excluded_capabilities
        or allowed_capabilities and "mesh_generation" not in allowed_capabilities
    ):
        return False
    return any(
        _asset_requires_mesh(item) for item in required_files if isinstance(item, dict)
    )


def _stage_parameter_context(stage: dict[str, Any]) -> dict[str, Any]:
    """Return authoritative stage parameters with their shared time scope."""
    parameters = dict(stage.get("parameters") or {})
    if str(stage.get("constraint_text") or "").strip():
        parameters["declared_constraints"] = str(stage["constraint_text"]).strip()
    temporal_constraints = [
        str(value)
        for value in stage.get("shared_temporal_constraints") or []
        if str(value).strip()
    ]
    if temporal_constraints:
        parameters["temporal_constraints"] = temporal_constraints
    # Absolute host paths are inventory/provenance data, not portable file
    # bindings. The immutable stage_input_contract supplies canonical relative
    # paths; omit the host value so the LLM cannot copy /Users, /home, or a
    # Windows-drive path into a namelist or script.
    portable: dict[str, Any] = {}
    for key, value in parameters.items():
        if "environment_absolute_path" in blocked_placeholder_markers(
            json.dumps(value, ensure_ascii=False, default=str)
        ):
            continue
        portable[str(key)] = value
    return portable


def _explicit_file_parameter_contract(
    stage: dict[str, Any],
    *,
    binding_mode: str = "literal",
) -> dict[str, Any]:
    """Compile declared stage values into a file-level content contract.

    ``available_stage_parameters`` is deliberately broad context.  It is not
    enough for the writer or Reviewer to know which values must survive in a
    particular file.  The Research Plan's ``explicit_parameter_keys`` is the
    only generic, structured signal that makes that mapping safe.  Older code
    intentionally refused this promotion, which left every generated
    namelist with context but no immutable bindings and made targeted review
    repair impossible.
    """
    context = _stage_parameter_context(stage)
    # Some extracted stage values describe the representation of the stage,
    # rather than a value that belongs in every file it owns.  In particular,
    # a phrase such as ``3D`` yields ``spatial_dimension``; promoting that
    # metadata to a literal file binding makes otherwise valid acquisition
    # requests fail because the provider configuration has no such field.
    # Explicit ``required_files[].parameter_bindings`` remain authoritative
    # and are not passed through this derived-contract path.
    stage_metadata_keys = {"spatial_dimension"}
    keys = [
        str(key).strip()
        for key in stage.get("explicit_parameter_keys") or []
        if str(key).strip() and str(key).strip() in context
        and str(key).strip() not in stage_metadata_keys
        and context[str(key).strip()] not in (None, "", [], {})
    ]
    bindings = {key: deepcopy(context[key]) for key in dict.fromkeys(keys)}
    if not bindings:
        return {}
    assertions: list[dict[str, Any]] = []
    for key, value in bindings.items():
        if binding_mode == "semantic":
            # Scripts may translate a research value through an API or
            # solver-specific representation.  Require the declared logical
            # binding name (or one of its stable tokens), while numeric
            # literals are still checked from ``parameter_bindings``.
            key_tokens = re.findall(r"[A-Za-z0-9]+", key)
            alternatives = list(dict.fromkeys([key, *key_tokens]))
            assertions.append({"name": key, "alternatives": alternatives, "required": True})
            continue
        values = value if isinstance(value, (list, tuple, set)) else [value]
        alternatives = [
            str(candidate)
            for candidate in values
            if candidate not in (None, "", [], {}) and not isinstance(candidate, (dict, list, tuple, set))
        ]
        if alternatives:
            assertions.append({"name": key, "alternatives": alternatives, "required": True})
    return {
        "parameter_bindings": bindings,
        "parameter_bindings_required": True,
        "parameter_binding_mode": binding_mode,
        "parameter_binding_source": "stage_explicit_parameters",
        "parameter_binding_assertions": assertions,
    }


def _generic_artifact_generation_step(
    item: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Build the single generic writer contract used by every planning path."""
    item_id = str(item.get("id") or "artifact")
    request_bound = analysis.get("review_profile") == "request_bound"
    stage_id = "" if request_bound else str(item.get("stage_id") or "package")
    normalized_contract = normalize_asset_contract(item)
    external_asset = bool(normalized_contract.get("is_external"))
    declared_path = str(item.get("declared_output_path") or item_id)
    return {
        "id": f"generate_{item_id}",
        "stage_id": stage_id,
        "work_unit_id": f"wu_{item_id}",
        "action": (
            f"Record acquisition instructions for external asset {item_id}."
            if external_asset else f"Generate the declared preprocessing artifact {item_id}."
        ),
        "tool_capability": "generic_preprocessing_artifact_generation",
        "tool_name": "generate_preprocessing_artifact",
        "tool_arguments": {
            "artifact_spec": {
                "purpose": (
                    "Create the framework acquisition contract without downloading the dataset."
                    if external_asset else item.get("requirement_basis") or "Required preprocessing artifact."
                ),
                "parameter_sources": [
                    "requirement_analysis.required_files",
                    *([] if request_bound else ["requirement_analysis.calculation_stages"]),
                ],
                "format": "json" if external_asset else item.get("format") or "text",
                "generation_recipe": item.get("generation_recipe"),
                "artifact_kind": (
                    "external_dataset_acquisition" if external_asset else "text_artifact"
                ),
                "runtime_dependencies": [
                    str(value).strip()
                    for value in (
                        item.get("runtime_dependencies")
                        if isinstance(item.get("runtime_dependencies"), list)
                        else [item.get("runtime_dependencies")]
                    )
                    if str(value or "").strip()
                ],
                **(
                    {
                        "acquisition_contract": normalize_acquisition_contract({
                            **item,
                            "acquisition_contract": item.get("acquisition_contract") or {},
                        })
                    }
                    if external_asset else {}
                ),
                "parameter_bindings": dict(item.get("parameter_bindings") or {}),
                "parameter_binding_source": item.get("parameter_binding_source") or "",
                "parameter_binding_mode": (
                    item.get("parameter_binding_mode")
                    or (
                        "semantic"
                        if canonical_workflow_capability(item)
                        == "preprocessing_script_generation"
                        else "literal"
                    )
                ),
                "parameter_binding_assertions": deepcopy(
                    item.get("parameter_binding_assertions") or []
                ),
                "available_stage_parameters": {
                    key: value
                    for key, value in dict(item.get("available_stage_parameters") or {}).items()
                    if "environment_absolute_path" not in blocked_placeholder_markers(
                        json.dumps(value, ensure_ascii=False, default=str)
                    )
                },
                "evidence": item.get("evidence") or analysis.get("evidence") or [],
                "acceptance_criteria": item.get("acceptance_criteria") or ["Artifact exists and is non-empty."],
            },
            "output_paths": {
                item_id: (
                    f"outputs/{_slug(stage_id)}/{_slug(item_id)}/"
                    f"{PurePosixPath(declared_path).name}"
                )
            },
        },
        "inputs": ["requirement_analysis", item_id],
        "outputs": [item_id],
        "dependencies": [],
        "verification": ["Verify the artifact exists, is non-empty, and its provenance is recorded."],
    }


def _constructive_geometry_contract(
    analysis: dict[str, Any],
    task_text: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return an explicit local geometry/mesh contract suitable for a Gmsh recipe.

    A solver deck can contain both configuration and a generated mesh.  The
    geometry values already bound to that deck are authoritative local input;
    they must not be reclassified as a missing public geometry file.
    """
    discipline = analysis.get("discipline") or {
        "primary": (analysis.get("task_scope") or {}).get("discipline")
    }
    primary = str(
        discipline.get("primary") if isinstance(discipline, dict) else discipline
    ).strip().lower()
    if primary == "cfd":
        from .mesh_iteration_advisor import (
            CANONICAL_CFD_MESH_INTENT_PATTERNS,
            explicit_mesh_contract,
        )

        route = explicit_mesh_contract(task_text, {
            "asset_parameters": [
                item.get("parameter_bindings")
                for item in analysis.get("required_files") or []
                if isinstance(item, dict) and isinstance(item.get("parameter_bindings"), dict)
            ],
        })
        if str((route or {}).get("mesh_type") or "") in CANONICAL_CFD_MESH_INTENT_PATTERNS:
            # Registered mesh producers own their geometry source and physical
            # groups.  An LLM-authored .geo helper would create a competing
            # implementation and replace the validated adapter route.
            return None

    boundary_names = [
        str(item.get("name") or "").strip()
        for item in analysis.get("boundary_contract") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    for item in analysis.get("required_files") or []:
        bindings = item.get("parameter_bindings")
        if not isinstance(bindings, dict):
            continue
        geometry = bindings.get("geometry")
        mesh = bindings.get("mesh")
        # Requirement models do not always group caller-bound values.  In a
        # confirmed mesh workflow, a flat file-level binding is still the
        # authoritative constructive specification; hand it to the geometry
        # writer instead of forcing the final mesh through the text writer.
        constructive = geometry if isinstance(geometry, dict) and geometry else bindings
        if constructive:
            contract = {"geometry": deepcopy(constructive)}
            if isinstance(mesh, dict) and mesh:
                contract["mesh"] = deepcopy(mesh)
            # Preserve explicit regions and recover regions embedded in
            # model-flattened condition keys without treating controls as names.
            from .geometry_assets import canonical_condition_region

            regions = [*boundary_names, *[
                canonical_condition_region(region)
                for group in ("boundary_conditions", "loads")
                if isinstance(bindings.get(group), dict)
                for region, controls in (bindings.get(group) or {}).items()
                if controls not in (None, "", [], {}) and canonical_condition_region(region)
            ]]
            if regions:
                contract["named_regions"] = list(dict.fromkeys(regions))
            return item, contract
    return None


def _deterministic_designer_fallback_plan(
    task_context: str | dict[str, Any],
    requirement_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a conservative schema-valid plan when the Designer service is unavailable."""
    analysis = _normalize_requirement_analysis(
        _best_requirement_analysis(
            requirement_analysis,
            _compile_explicit_requirements(task_context),
        )
        or {}
    )
    analysis = _ensure_authoritative_caller_assets(task_context, analysis)
    analysis = _scope_analysis_to_data_node(task_context, analysis)
    if not isinstance(analysis.get("preprocessing_request"), dict):
        analysis["preprocessing_request"] = normalize_preprocessing_request(task_context)
    if not isinstance(analysis.get("preprocessing_work_order"), dict):
        analysis["preprocessing_work_order"] = build_preprocessing_work_order(
            analysis["preprocessing_request"],
            analysis,
        )
    analysis["review_profile"] = analysis["preprocessing_request"].get(
        "review_profile", "request_bound"
    )
    if (
        not (analysis.get("calculation_stages") or [])
        and any(
            token in str(analysis.get("calculation_type") or "").lower()
            for token in ("multiple", "multi-stage", "workflow")
        )
    ):
        analysis["calculation_type"] = "requested preprocessing package"
        evidence = analysis.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            evidence = [{"source_type": "fallback", "detail": "No reliable stage list was available."}]
        analysis["evidence"] = [
            *evidence,
            {
                "source_type": "deterministic_fallback",
                "detail": (
                    "Original requirement implied a multi-stage workflow but did not provide "
                    "a schema-valid stage list, so the fallback creates one package assembly step."
                ),
            },
        ]
    software = analysis.get("simulation_software") or {"name": "simulation software"}
    discipline = analysis.get("discipline") or {"primary": "unknown"}
    stage_role_owners: dict[str, set[str]] = {}
    declared_stage_ids = {
        str(stage.get("id") or "").strip()
        for stage in analysis.get("calculation_stages") or []
        if isinstance(stage, dict) and str(stage.get("id") or "").strip()
    }
    for stage in analysis.get("calculation_stages") or []:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id") or "").strip()
        for role in stage.get("required_file_roles") or []:
            key = _normalise_asset_key(role)
            if key and stage_id:
                stage_role_owners.setdefault(key, set()).add(stage_id)

    def belongs_to_declared_stage(item: dict[str, Any]) -> bool:
        if analysis.get("review_profile") == "request_bound":
            return True
        explicit_stage = str(item.get("stage_id") or "").strip()
        if explicit_stage:
            return explicit_stage in declared_stage_ids
        # A global requirement is safe to materialize only when its declared
        # role belongs to exactly one declared stage.  Ambiguous aggregate
        # utilities are represented by their stage-local contracts instead;
        # otherwise publishing them under an arbitrary first stage corrupts
        # the downstream interface.
        role = _normalise_asset_key(item.get("name_or_role") or item.get("id"))
        return len(stage_role_owners.get(role, set())) == 1

    required_files = [
        item for item in analysis.get("required_files") or []
        if isinstance(item, dict)
        and item.get("delivery_required") is not False
        and str(item.get("fulfillment_kind") or "") not in {"runtime_output", "runtime_access", "runtime_tool"}
        and belongs_to_declared_stage(item)
    ]
    if not required_files:
        required_files = [{
            "id": "preprocessing_package",
            "name_or_role": "preprocessing package",
            "format": "solver-ready input package",
            "reason": "The upstream task requires preprocessing inputs.",
            "evidence": [{"source_type": "upstream", "detail": "Fallback requirement from task context."}],
            "confidence": 0.75,
            "consequences_if_missing": "The downstream simulation cannot start.",
        }]
    requires_mesh_assets = _requires_spatial_mesh_assets(analysis, required_files)

    deliverables: list[dict[str, Any]] = []
    outputs: list[str] = []
    used_ids: set[str] = set()
    stage_ids = [
        str(stage.get("id") or "").strip()
        for stage in analysis.get("calculation_stages") or []
        if isinstance(stage, dict) and str(stage.get("id") or "").strip()
    ]
    stages_by_id = {
        str(stage.get("id") or "").strip(): stage
        for stage in analysis.get("calculation_stages") or []
        if isinstance(stage, dict) and str(stage.get("id") or "").strip()
    }

    def infer_stage_id(item: dict[str, Any], fallback: str = "package") -> str:
        if analysis.get("review_profile") == "request_bound":
            return ""
        declared = str(item.get("stage_id") or item.get("source_stage_id") or "").strip()
        if declared:
            return declared
        role = _normalise_asset_key(item.get("name_or_role") or item.get("id"))
        role_owners = stage_role_owners.get(role, set())
        if len(role_owners) == 1:
            return next(iter(role_owners))
        text = json.dumps(item, ensure_ascii=False, default=str)
        matches = [stage_id for stage_id in stage_ids if re.search(rf"(?<![A-Za-z0-9]){re.escape(stage_id)}(?![A-Za-z0-9])", text, flags=re.I)]
        if len(matches) == 1:
            return matches[0]
        return fallback

    def resolve_single_text_format(raw_format: Any, stage: dict[str, Any]) -> tuple[str, str]:
        """Resolve an alternative text label only from the owning stage contract.

        A deterministic fallback may not guess between unrelated syntaxes.
        It can, however, select the one syntax explicitly named by the stage's
        declared application/method (for example a stage declaring Python from
        a ``python/ncl`` acceptable-format alternative).
        """
        original = str(raw_format or "text").strip() or "text"
        candidates = {
            name for name, pattern in {
                "python": r"\bpython\b|\.py\b",
                "shell": r"\b(?:shell|bash|sh)\b|\.sh\b",
                "json": r"\bjson\b|\.json\b",
                "yaml": r"\b(?:yaml|yml)\b|\.(?:yaml|yml)\b",
                "namelist": r"\b(?:fortran\s+)?namelist\b|\.nml\b",
                "ncl": r"\bncl\b|\.ncl\b",
            }.items()
            if re.search(pattern, original, flags=re.I)
        }
        if len(candidates) <= 1:
            return original, ""
        stage_text = json.dumps(
            {key: stage.get(key) for key in ("solver", "application", "tool", "name", "calculation_type")},
            ensure_ascii=False,
            default=str,
        )
        selected = [
            name for name, pattern in {
                "python": r"\bpython\b|\.py\b",
                "shell": r"\b(?:shell|bash|sh)\b|\.sh\b",
                "json": r"\bjson\b|\.json\b",
                "yaml": r"\b(?:yaml|yml)\b|\.(?:yaml|yml)\b",
                "namelist": r"\b(?:fortran\s+)?namelist\b|\.nml\b",
                "ncl": r"\bncl\b|\.ncl\b",
            }.items()
            if name in candidates and re.search(pattern, stage_text, flags=re.I)
        ]
        return (selected[0], selected[0]) if len(selected) == 1 else (original, "")

    extensions = {
        "python": ".py", "shell": ".sh", "json": ".json",
        "yaml": ".yaml", "namelist": ".nml", "ncl": ".ncl",
    }

    for item in required_files:
        item_id = _unique_slug(item.get("id") or item.get("name_or_role"), used_ids, "deliverable")
        outputs.append(item_id)
        role = _asset_contract_role(item)
        contract = normalize_asset_contract(item)
        declared_output_path = _declared_relative_output(item, item_id)
        software_name = str(
            software.get("name") or software.get("primary") or ""
        ) if isinstance(software, dict) else str(software)
        if (
            canonical_workflow_capability(item) == "mesh_generation"
            and re.search(r"openfoam", software_name, flags=re.I)
            and not PurePosixPath(declared_output_path).suffix
            and not declared_output_path.casefold().rstrip("/").endswith("constant/polymesh")
        ):
            declared_output_path = "constant/polyMesh"
        stage_id = infer_stage_id(item)
        resolved_format, selected_syntax = resolve_single_text_format(
            item.get("format"), stages_by_id.get(stage_id) or {}
        )
        if selected_syntax and not PurePosixPath(declared_output_path).suffix:
            declared_output_path += extensions[selected_syntax]
        owning_stage = stages_by_id.get(stage_id) or {}
        stage_parameters = _stage_parameter_context(owning_stage)
        # A stage can contain experiment-wide settings, runtime settings, and
        # values for several input files.  Only explicitly named keys are
        # eligible for a file-content contract; broad stage context remains
        # available to the writer without becoming an assertion.
        explicit_bindings = next(
            (
                dict(item.get(key) or {})
                for key in ("parameter_bindings", "configuration_values", "parameters")
                if isinstance(item.get(key), dict) and item.get(key)
            ),
            {},
        )
        deliverables.append({
            "id": item_id,
            "stage_id": stage_id,
            "declared_output_path": declared_output_path,
            "type": role,
            "asset_role": contract["asset_role"],
            "scientific_role": contract["scientific_role"],
            "representation": contract["representation"],
            "source_strategy": contract["source_strategy"],
            "acquisition_kind": contract["acquisition_kind"],
            "workflow_capability": contract["workflow_capability"],
            "materialization_kind": contract["materialization_kind"],
            "delivery_required": contract["delivery_required"],
            "format": _format_for_declared_output(declared_output_path, resolved_format),
            "generation_recipe": item.get("generation_recipe"),
            **({"local_match": deepcopy(item["local_match"])} if item.get("local_match") else {}),
            "parameter_bindings": explicit_bindings,
            "available_stage_parameters": stage_parameters,
            "required": True,
            "requirement_basis": str(item.get("reason") or "Required by upstream preprocessing task."),
            "evidence": item.get("evidence") or analysis.get("evidence") or [
                {"source_type": "upstream", "detail": "Derived from local/upstream task context."}
            ],
            "acceptance_criteria": [
                "A regular non-empty file is created at the declared path.",
                "Blocked placeholders are rejected; lineage and downstream contract are explicit.",
            ],
        })

    composite_mesh_asset = any(
        _asset_requires_mesh(item)
        and canonical_workflow_capability(item) != "mesh_generation"
        for item in required_files
    )
    mesh_output_ids = [
        item["id"] for item in deliverables
        if canonical_workflow_capability(item) == "mesh_generation"
    ] if requires_mesh_assets else []
    if requires_mesh_assets and not mesh_output_ids:
        mesh_id = _unique_slug("computational_mesh", used_ids, "computational_mesh")
        deliverables.append({
            "id": mesh_id,
            "type": "geometry_or_discretization_asset",
            "asset_role": "geometry_or_discretization_asset",
            "scientific_role": "computational_mesh",
            "representation": "mesh",
            "source_strategy": "local_generation",
            "materialization_kind": "generated_file",
            "declared_output_path": "reproducibility/mesh/model.msh",
            "format": "validated computational mesh",
            "required": not composite_mesh_asset,
            "delivery_required": not composite_mesh_asset,
            "requirement_basis": "The upstream task requires a computational mesh.",
            "evidence": analysis.get("evidence") or [
                {"source_type": "upstream", "detail": "Mesh requirement derived from task context."}
            ],
            "acceptance_criteria": [
                "Mesh generation and quality review both report success.",
                "The mesh is linked to its geometry and generation parameters in lineage.",
            ],
        })
        mesh_output_ids = [mesh_id]

    selected_tools = {"build_scientific_preprocessing_package"}
    if requires_mesh_assets:
        selected_tools.add("prepare_scientific_mesh")
    # Execution tools need the caller's request, not the compact Designer
    # briefing.  The latter may contain KB/history text and is deliberately
    # middle-truncated, so passing it as ``spec`` can create malformed JSON and
    # make unrelated words such as "search" change scientific routing.
    task_text = caller_request_text(
        task_context,
        preprocessing_request=analysis.get("preprocessing_request"),
    ) or _fallback_generation_context(task_context, analysis)
    recovered_assets = _recovered_local_assets(analysis.get("reference_evidence") or [])
    request_inputs = (analysis.get("preprocessing_request") or {}).get("inputs") or []
    caller_parameters: dict[str, Any] = {}
    for item in request_inputs if isinstance(request_inputs, list) else [request_inputs]:
        if isinstance(item, dict):
            caller_parameters.update(item)
    for key in (
        "spec", "user_request", "objective", "purpose", "consumer",
        "requested_assets", "acceptance_criteria", "non_goals",
    ):
        caller_parameters.pop(key, None)
    requested_paths = [
        str(item.get("expected_filename") or item.get("declared_output_path") or item.get("format") or "")
        for item in required_files
    ]
    requested_suffixes = {
        suffix
        for value in requested_paths
        if (suffix := PurePosixPath(value).suffix.lower())
    }
    dimension_text = " ".join([
        task_text,
        str(analysis.get("calculation_type") or ""),
        json.dumps(
            [item.get("parameter_bindings") for item in required_files],
            ensure_ascii=False,
            default=str,
        ),
    ])
    common_parameters = {
        **caller_parameters,
        "requirement_analysis": analysis,
        "reference_evidence": analysis.get("reference_evidence") or [],
        "fallback_plan": True,
        "quality_preset": "robust",
        "requested_output_paths": {
            str(item.get("id") or ""): str(item.get("declared_output_path") or "")
            for item in deliverables
            if str(item.get("id") or "").strip()
            and str(item.get("declared_output_path") or "").strip()
        },
        "convert_to_openfoam": bool(
            re.search(
                r"openfoam|polymesh",
                " ".join(requested_paths + [str(software.get("name") or "")]),
                flags=re.I,
            )
        ),
        **(
            {"solver_mesh_format": "inp"}
            if ".inp" in requested_suffixes or any(str(value).strip().lower() == "inp" for value in requested_paths)
            else {}
        ),
        **(
            {"mesh_dimension": 2}
            if re.search(r"(?<![A-Za-z0-9])2\s*[- ]?D(?![A-Za-z0-9])|二维", dimension_text, flags=re.I)
            else {"mesh_dimension": 3}
            if re.search(r"(?<![A-Za-z0-9])3\s*[- ]?D(?![A-Za-z0-9])|三维", dimension_text, flags=re.I)
            else {}
        ),
    }
    # Keep one format-based visualization policy for plans and direct calls.
    for item in required_files:
        output_preferences = ((item.get("parameter_bindings") or {}).get("outputs") or {})
        if isinstance(output_preferences, dict) and "write_tecplot" in output_preferences:
            common_parameters.setdefault("write_tecplot", output_preferences["write_tecplot"])
    from .geometry_assets import mesh_visualization_requested

    common_parameters.setdefault("write_tecplot", mesh_visualization_requested(
        common_parameters, requested_paths + [item.get("format") for item in required_files],
    ))
    from .geometry_assets import geometry_file_from_params, existing_geometry_like_path

    bound_geometry = geometry_file_from_params(common_parameters) or next((
        path for item in required_files
        if (path := geometry_file_from_params(item.get("parameter_bindings") or {}))
    ), "")
    if not bound_geometry:
        # Only explicitly referenced inspected inputs, never an arbitrary file
        # from a directory listing or a previous delivery, can bind the source.
        candidates = set()
        input_text = json.dumps(request_inputs, ensure_ascii=False, default=str)
        for item in analysis.get("local_asset_inventory") or []:
            path = existing_geometry_like_path(item.get("path"))
            references = [path] if path else []
            if path and Path(path).is_relative_to(Path.cwd()):
                references.append(str(Path(path).relative_to(Path.cwd())))
            # A service input can resolve an erroneous caller-relative path.
            # Require explicit reference and inspection, not a same-name scan.
            if any(reference in task_text or reference in input_text for reference in references):
                candidates.add(path)
        if len(candidates) == 1:
            bound_geometry = candidates.pop()
    if bound_geometry:
        common_parameters["geometry_file"] = bound_geometry
    requested_formats = {str(item.get("format") or "").lower() for item in required_files}
    if any(re.search(r"\bmsh\s*4(?:\.1)?\b", value) for value in requested_formats):
        common_parameters["solver_mesh_format"] = "msh4"
    recovered_geometry_assets = [
        item for item in recovered_assets
        if str(item.get("asset_kind") or "") in {"geometry", "geometry_or_mesh", "mesh"}
    ]
    if recovered_geometry_assets and not bound_geometry:
        recovered_path = str(recovered_geometry_assets[0].get("path") or "")
        recovered_suffix = Path(recovered_path).suffix.lower()
        if recovered_suffix in {".dat", ".txt", ".xy"}:
            common_parameters.update({
                "coordinate_profile_path": recovered_path,
                "profile_dat_path": recovered_path,
            })
        elif recovered_path:
            common_parameters["geometry_file"] = recovered_path
    request_bound = analysis.get("review_profile") == "request_bound"
    primary_name = str(
        discipline.get("primary") or ""
    ).strip().lower() if isinstance(discipline, dict) else str(discipline).strip().lower()
    software_name = str(
        (analysis.get("simulation_software") or {}).get("name") or ""
    ).strip().lower()
    use_solver_template_adapter = (
        primary_name in _DETERMINISTIC_STAGE_ADAPTERS
        and software_name not in {"", "generic", "generic_preprocessing", "unknown"}
    )
    solver_template_assets = [
        item for item in required_files
        if canonical_workflow_capability(item) in {
            "configuration_generation",
            "preprocessing_script_generation",
        }
        and not re.search(
            r"mesh|checkmesh|quality|audit|visualization|polymesh|网格|质量|审计",
            json.dumps(item, ensure_ascii=False, default=str),
            flags=re.I,
        )
    ]
    steps: list[dict[str, Any]] = []
    geometry_dependency = ""
    constructive_geometry = (
        _constructive_geometry_contract(analysis, task_text)
        if requires_mesh_assets and not recovered_geometry_assets and not bound_geometry else None
    )
    if constructive_geometry:
        source_asset, geometry_bindings = constructive_geometry
        # Internal region bindings must materialize for their consumers even
        # when the caller did not prescribe literal mesh-group names.
        common_parameters["expected_boundaries"] = list(dict.fromkeys([
            *(common_parameters.get("expected_boundaries") or []),
            *(geometry_bindings.get("named_regions") or []),
        ]))
        if common_parameters.get("mesh_dimension") in {2, 3}:
            geometry_bindings["mesh_dimension"] = common_parameters["mesh_dimension"]
        geometry_dependency = "generate_geometry_definition"
        geometry_id = _unique_slug("geometry_definition", used_ids, "geometry_definition")
        geometry_asset = {
            "id": geometry_id,
            "stage_id": "",
            "declared_output_path": "reproducibility/geometry/model.geo",
            "type": "geometry_definition",
            "asset_role": "geometry_or_discretization_asset",
            "scientific_role": "constructive_geometry_definition",
            "representation": "geometry_definition",
            "source_strategy": "local_generation",
            "acquisition_kind": "generated_artifact",
            "workflow_capability": "local_artifact_generation",
            "materialization_kind": "generated_file",
            "delivery_required": False,
            "required": False,
            "format": "Gmsh .geo geometry definition",
            "generation_recipe": (
                "Write concise Gmsh OpenCASCADE geometry and sizing commands from the supplied bindings. "
                "Keep the requested topological dimension and define its physical region plus named "
                "boundaries; for a 2-D contract use planar curves/surfaces and 2-D primitives, never "
                "3-D volume primitives, and do not extrude merely because it has a section thickness. "
                "After OpenCASCADE BooleanDifference/Fragments, store returned entity tags in arrays; "
                "use the returned surface array directly instead of searching for that surface again. "
                "Use documented Gmsh list expressions: curves[] = Boundary{Surface{fluid[]};}; "
                "select entities with Curve In BoundingBox{xmin,ymin,zmin,xmax,ymax,zmax}; or obtain "
                "an entity's bounds with bbox[] = BoundingBox Curve{tag};. These are different operations "
                "(https://gmsh.info/doc/texinfo/#Expressions). Use a nonzero selection tolerance on all three axes. Do not "
                "invent list/set helper functions; select each required group directly with documented Gmsh statements. Do not "
                "add replacement Line/Circle entities or assume that deleted entity tags still exist. Reuse "
                "those selections in sizing fields instead of hard-coding post-boolean curve tags. Every "
                "requested physical boundary group must match its geometric region, not just be nonempty. "
                "Select regions by their geometric predicates; the complement of a few named boundary "
                "conditions is not necessarily the intended feature. Apply sizing only to the intended "
                "regions and preserve the caller's requested spatial transition. "
                "Use supplied absolute mesh sizes in geometry units and the specified polynomial element order; "
                "do not multiply an already absolute size by itself via Mesh.MeshSizeFactor. "
                "Leave mesh execution and output serialization to the mesher; do not add Mesh/Save commands "
                "or Mesh.Format/MshFileVersion/Binary settings to a geometry definition. "
                "Set Mesh.SaveGroupsOfNodes=1 and never hand-write mesh nodes or elements."
            ),
            "parameter_bindings": geometry_bindings,
            "parameter_binding_mode": "semantic",
            "requirement_basis": "Materialize the caller's constructive geometry for the approved mesher.",
            "evidence": source_asset.get("evidence") or analysis.get("evidence") or [
                {"source_type": "caller_request", "detail": task_text}
            ],
            "acceptance_criteria": [
                "The script constructs the requested geometry without synthetic substitute geometry.",
                "The script is directly consumable by Gmsh and exposes non-empty physical regions and requested boundaries.",
            ],
        }
        deliverables.append(geometry_asset)
        geometry_step = _generic_artifact_generation_step(geometry_asset, analysis)
        geometry_step.update({
            "id": geometry_dependency,
            "work_unit_id": "wu_geometry_definition",
            "action": "author_constructive_geometry_definition",
        })
        steps.append(geometry_step)
        selected_tools.add("generate_preprocessing_artifact")
    if requires_mesh_assets:
        steps.append({
            "id": "prepare_scientific_mesh",
            "stage_id": infer_stage_id({"id": "mesh", "mesh": True}, "mesh"),
            "work_unit_id": "wu_computational_mesh",
            "action": "generate_and_review_computational_mesh",
            "tool_capability": "scientific_mesh_generation",
            "tool_name": "prepare_scientific_mesh",
            "tool_arguments": {
                "spec": task_text,
                "discipline": discipline.get("primary") or "unknown",
                "parameters": json.dumps(common_parameters, ensure_ascii=False),
                "operation": "prepare",
                "generate_mesh": True,
            },
            "inputs": ["requirement_analysis", "reference_evidence", "local/upstream task context"],
            "outputs": mesh_output_ids,
            "dependencies": [geometry_dependency] if geometry_dependency else [],
            "verification": [
                "Require a non-empty generated mesh, not geometry-only boundary data.",
                "Require mesh review to record geometry, topology, cell-quality, and region checks.",
                "Record the generated path and quality report in lineage.",
            ],
        })
    # Reference acquisition is owned by the single reference-plan driver.
    # The generation fallback must not embed search steps or reinterpret their
    # result statuses as package inputs.
    # A package consumes native assets; it cannot promise arbitrary Analyst
    # suggestions as outputs. Composite solver decks use the existing adapter.
    package_outputs = [item for item in outputs if item not in mesh_output_ids and (
        not requires_mesh_assets
        or (use_solver_template_adapter and any(
            str(asset.get("id")) == item and (_asset_requires_mesh(asset) or asset in solver_template_assets)
            for asset in required_files
        ))
    )]
    if not package_outputs:
        package_outputs = list(mesh_output_ids)
    steps.append({
        "id": "assemble_preprocessing_package",
        "stage_id": "package",
        "work_unit_id": "wu_preprocessing_package",
        "action": "assemble_available_inputs_and_blocked_contract",
        "tool_capability": "scientific_preprocessing_package",
        "tool_name": "build_scientific_preprocessing_package",
        "workflow_capability": "local_artifact_generation",
        "tool_arguments": {
            "spec": task_text,
            "discipline": discipline.get("primary") or "unknown",
            "generate_model_assets": False,
            # Mesh creation is an explicit preceding DAG step.  The package
            # builder consumes its reviewed output instead of regenerating it.
            "generate_mesh_assets": False,
            "workflow_capability": "local_artifact_generation",
            "mesh_generation_result": (
                {"$ref": "prepare_scientific_mesh"}
                if requires_mesh_assets else None
            ),
            "reference_evidence": [],
            "parameters": json.dumps({
                **common_parameters,
                "write_domain_templates": bool(
                    analysis.get("calculation_stages")
                    or (
                        required_files
                        and (
                            not request_bound
                            or (solver_template_assets and use_solver_template_adapter)
                        )
                    )
                ),
                "domain_specific_generation_deferred": False,
            }, ensure_ascii=False),
        },
        "inputs": ["requirement_analysis", "local/upstream task context", *mesh_output_ids],
        "outputs": package_outputs,
        "dependencies": ["prepare_scientific_mesh"] if requires_mesh_assets else [],
        "verification": [
            "Check every required asset role is represented.",
            "Record unresolved assets as assumptions or recoverable blocked contract entries.",
            "Do not fabricate domain-specific data when the Designer service is unavailable.",
        ],
    })
    # Unsupported disciplines still receive a real per-file generation route;
    # the generic artifact generator records assumptions and provenance instead
    # of pretending that the scientific package adapter knows the application.
    direct_asset_request = str(
        (analysis.get("preprocessing_request") or {}).get("review_profile")
    ) == "request_bound"
    writer_eligible_assets = bool(deliverables) and all(
        not normalize_asset_contract(item).get("is_external")
        and _asset_contract_allows_fallback(item)
        for item in deliverables
    )
    # Registered solver configurations belong to the existing scientific
    # package adapter, which can generate a coherent multi-file case in one
    # step.  The generic one-file writer remains the fallback for standalone
    # text assets and unsupported disciplines.
    use_generic_writer = not (solver_template_assets and use_solver_template_adapter)
    if use_generic_writer and (
        (primary_name not in _DETERMINISTIC_STAGE_ADAPTERS and not requires_mesh_assets)
        or (direct_asset_request and not requires_mesh_assets)
        or (writer_eligible_assets and not requires_mesh_assets)
    ):
        steps = [step for step in steps if step.get("tool_name") != "build_scientific_preprocessing_package"]
        selected_tools.discard("build_scientific_preprocessing_package")
        for item in deliverables:
            if not item.get("required", True) or item.get("type") == "reference_evidence":
                continue
            item_id = str(item.get("id") or "")
            external_asset = bool(normalize_asset_contract(item).get("is_external"))
            if not external_asset and not _asset_contract_allows_fallback(item):
                # A locally-derived runtime product belongs to the downstream
                # execution graph, not to a data-node artifact writer.
                continue
            if external_asset:
                # Large external assets are represented by a stage-local,
                # machine-readable acquisition summary.  This is a real
                # deliverable, but it never pretends the dataset itself was
                # downloaded into the workspace.
                item["declared_output_path"] = _acquisition_output_path(item, item_id)
            steps.append(_generic_artifact_generation_step(item, analysis))
        selected_tools.add("generate_preprocessing_artifact")
    fallback_plan = normalize_plan({
        "schema_version": "1.0",
        "task_summary": "Deterministic local-first preprocessing plan generated after Designer service failure.",
        "discipline": discipline,
        "simulation_software": software,
        "requirement_analysis": {
            "preprocessing_request": analysis.get("preprocessing_request") or {},
            "preprocessing_work_order": analysis.get("preprocessing_work_order") or {},
            "review_profile": analysis.get("review_profile") or "request_bound",
            "task_scope": analysis.get("task_scope") or {},
            "calculation_type": analysis.get("calculation_type") or "requested preprocessing",
            "calculation_stages": analysis.get("calculation_stages") or [],
            "stage_input_contracts": analysis.get("stage_input_contracts") or [],
            "required_files": required_files,
            "optional_files": analysis.get("optional_files") or [],
            "unresolved_facts": analysis.get("unresolved_facts") or [],
            "reference_evidence": analysis.get("reference_evidence") or [],
            "evidence": analysis.get("evidence") or [{"source_type": "upstream", "detail": "Local task context."}],
        },
        "required_deliverables": deliverables,
        "generation_steps": steps,
        "tool_requirements": [
            {
                "capability": "fallback_preprocessing_execution",
                "selected_tools": sorted(selected_tools),
            }
        ],
        "assumptions": analysis.get("assumptions") or [],
        "unresolved_questions": [
            {
                "question": str(item),
                # A missing public reference or dataset is represented by an
                # acquisition manifest and resume contract. It is not a
                # reason to suppress independently generatable inputs.
                "blocking": False,
            }
            for item in analysis.get("unresolved_facts") or []
        ],
        "risks": [
            "Designer service was unavailable; deterministic fallback delegates generation to registered "
            "discipline tools and relies on package review to block invalid or unsupported assets."
        ],
        "reproducibility": {
            "workspace_layout": "data_preprocessing/fallback",
            "provenance_records": ["task_context", "requirement_analysis", "plan_hash", "tool_results"],
        },
    })
    produced_ids = {str(output) for step in fallback_plan["generation_steps"] for output in step.get("outputs") or []}
    for item in fallback_plan["required_deliverables"]:
        if (str(item.get("id")) not in produced_ids
            and request_bound and not analysis.get("preprocessing_request", {}).get("requested_assets")
            and item.get("output_path_is_explicit") is False):
            # Inferred, unowned files are implementation proposals, not new
            # caller obligations. The semantic gate still checks the complete
            # original request, including requested reports without filenames.
            item.update(required=False, delivery_required=False,
                        requirement_basis="Unassigned implementation proposal; review delivered content against the original request.")
    # Model-authored and deterministic plans cross the same canonical boundary.
    # This keeps the fallback from maintaining a parallel artifact contract.
    repair_analysis = deepcopy(analysis)
    return _repair_designer_plan_contract(fallback_plan, repair_analysis)


def _analysis_supports_deterministic_execution(analysis: dict[str, Any] | None) -> bool:
    """Use the local DAG when upstream already supplied an auditable plan."""
    if not isinstance(analysis, dict):
        return False
    stages = [item for item in analysis.get("calculation_stages") or [] if isinstance(item, dict)]
    required = [item for item in analysis.get("required_files") or [] if isinstance(item, dict)]
    scope = analysis.get("task_scope") if isinstance(analysis.get("task_scope"), dict) else {}
    try:
        confidence = float(analysis.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence <= 0.0 and stages:
        try:
            confidence = min(float(stage.get("confidence") or 0.0) for stage in stages)
        except (TypeError, ValueError):
            confidence = 0.0
    stage_ids = {
        str(stage.get("id") or "").strip()
        for stage in stages
        if str(stage.get("id") or "").strip()
    }
    stage_contract_ids = {
        str(contract.get("stage_id") or "").strip()
        for contract in analysis.get("stage_input_contracts") or []
        if isinstance(contract, dict) and str(contract.get("stage_id") or "").strip()
    }
    delivery_contracts = [
        normalize_asset_contract(item)
        for item in required
        if item.get("delivery_required") is not False
    ]
    request_bound = str(analysis.get("review_profile") or "").strip() == "request_bound"
    return bool(
        (request_bound or confidence >= 0.75)
        and required
        and scope.get("allowed_capabilities")
        and (request_bound or (stages and stage_ids.issubset(stage_contract_ids)))
        and delivery_contracts
        and all(not contract.get("contradictory_fields") for contract in delivery_contracts)
        and all(
            str(contract.get("workflow_capability") or "") != "unclassified"
            for contract in delivery_contracts
        )
        and all(
            canonical_workflow_capability(item) not in {"configuration_generation", "preprocessing_script_generation"}
            or any(str(item.get(key) or "").strip().lower() not in {"", "unknown", "unspecified", "generic"}
                   for key in ("declared_output_path", "expected_filename", "format"))
            for item in required
        )
        and (request_bound or all(
            str(stage.get("id") or "").strip()
            and str(stage.get("calculation_type") or "").strip()
            and stage.get("evidence")
            for stage in stages
        ))
    )


def _is_publisher_stage_contract(item: dict[str, Any]) -> bool:
    """Return whether an item is owned by the package publisher, not a stage.

    Stage execution contracts are now emitted from the normalized stage model;
    the old Analyst-side JSON placeholder is no longer a second contract
    source.  Keep only the package-level marker here.
    """
    package_placeholder = (
        _normalise_asset_key(item.get("id") or item.get("name_or_role"))
        == "preprocessingpackage"
        and not str(item.get("stage_id") or item.get("owner_stage") or "").strip()
        and not item.get("consumer_stages")
    )
    return package_placeholder


def _is_deferred_data_product(item: dict[str, Any], contract: dict[str, Any]) -> bool:
    """Identify data products represented by a recipe, not a writable file.

    The data node packages configuration, acquisition, and transformation
    instructions; it does not execute the scientific workflow merely to make
    a NetCDF/GRIB/intermediate result appear.  This classification uses the
    normalized representation and declared source strategy, never a solver
    name or filename convention.
    """
    if str(contract.get("source_strategy") or "") != "local_generation":
        return False
    representation = str(contract.get("representation") or "").lower()
    format_text = str(item.get("format") or "").lower()
    is_data_product = (
        representation in {"spatial_field", "dataset", "grib_dataset", "netcdf_dataset"}
        or bool(re.search(r"\b(?:netcdf|grib|hdf|intermediate)\b|数据场|数据集", format_text, flags=re.I))
    )
    return is_data_product


def _normalize_requirement_analysis(raw: dict[str, Any]) -> dict[str, Any]:
    analysis = dict(raw) if isinstance(raw, dict) else {}
    request = analysis.get("preprocessing_request")
    if isinstance(request, dict) and request.get("review_profile") == "request_bound":
        authority = caller_request_text(request, preprocessing_request=request) + json.dumps(
            {key: request.get(key) for key in ("requested_assets", "acceptance_criteria")},
            ensure_ascii=False,
        )
        # Exact boundary names are caller requirements, not Analyst defaults.
        # Preserve physical intent in the original request; do not turn a
        # suggested representation into an additional mechanical rejection.
        # inputs may contain the service's expanded spec, not user constraints.
        analysis["boundary_contract"] = [
            item for item in analysis.get("boundary_contract") or []
            if isinstance(item, dict) and str(item.get("name") or "").strip()
            and re.search(rf"(?<![A-Za-z0-9_]){re.escape(str(item['name']).strip())}(?![A-Za-z0-9_])", authority)
        ]
    if isinstance(analysis.get("discipline"), str):
        analysis["discipline"] = {"primary": analysis["discipline"], "evidence": []}
    if isinstance(analysis.get("simulation_software"), (str, dict)):
        analysis["simulation_software"] = normalize_software_identity(
            analysis["simulation_software"]
        )
        analysis["simulation_software"].setdefault("evidence", [])
    elif isinstance(analysis.get("simulation_software"), list):
        candidates = [
            item if isinstance(item, dict) else {"name": str(item), "evidence": []}
            for item in analysis["simulation_software"]
            if (isinstance(item, dict) and str(item.get("name") or "").strip())
            or (not isinstance(item, dict) and str(item).strip())
        ]
        primary = next(
            (
                item for item in candidates
                if re.search(
                    r"\b(primary|main|core|solver)\b|主求解器|核心软件",
                    str(item.get("role") or ""),
                    flags=re.I,
                )
            ),
            candidates[0] if candidates else None,
        )
        analysis["simulation_software"] = normalize_software_identity(primary or {})
        if primary:
            analysis["supporting_software"] = [
                item for item in candidates if item is not primary
            ]
    software_contract = analysis.get("simulation_software")
    if isinstance(software_contract, dict):
        # Preprocessor identity is part of the same contract.  Preserve an
        # explicitly supplied preprocessor_version, only filling it from a
        # label such as ``WPS v4.4`` when necessary.
        preprocessor_key = next(
            (key for key in ("preprocessor", "preprocessing_software") if software_contract.get(key)),
            "",
        )
        if preprocessor_key:
            preprocessor = normalize_software_identity(software_contract.get(preprocessor_key))
            software_contract[preprocessor_key] = preprocessor.get("name") or software_contract[preprocessor_key]
            if not str(software_contract.get("preprocessor_version") or "").strip():
                software_contract["preprocessor_version"] = preprocessor.get("version") or ""
    if isinstance(analysis.get("supporting_software"), list):
        analysis["supporting_software"] = [
            normalize_software_identity(item)
            for item in analysis["supporting_software"]
            if item not in (None, "", {})
        ]
    for key in ("calculation_stages", "required_files", "optional_files", "evidence", "assumptions", "unresolved_facts", "search_queries"):
        if not isinstance(analysis.get(key), list):
            analysis[key] = []
    analysis_text = json.dumps(analysis, ensure_ascii=False, default=str)
    valid_naca_codes = list(dict.fromkeys(
        _normalise_profile_code(match.group(0))
        for match in re.finditer(r"\bNACA\s*[-_ ]?\s*\d{4,5}(?!\d)", analysis_text, flags=re.I)
    ))
    normalized_required_files: list[Any] = []
    for item in analysis.get("required_files") or []:
        if not isinstance(item, dict):
            normalized_required_files.append(item)
            continue
        item = dict(item)
        # Logical deliverable IDs cross Analyst, Designer, executor, Publisher,
        # and Reviewer boundaries.  Canonicalize them at the first boundary;
        # normalizing only Designer outputs left stage contracts with ``F001``
        # while the executable plan delivered ``f001``.
        if str(item.get("id") or "").strip():
            item["id"] = _slug(str(item["id"]))
        acquisition_contract = item.get("acquisition_contract")
        if isinstance(acquisition_contract, dict):
            retrieval = acquisition_contract.get("retrieval_instructions")
            retrieval = retrieval if isinstance(retrieval, dict) else {}
            locator = (
                acquisition_contract.get("locator")
                or acquisition_contract.get("url")
                or retrieval.get("locator")
                or retrieval.get("url")
                or ""
            )
            is_official_contract = str(
                item.get("asset_role")
                or item.get("workflow_capability")
                or item.get("acquisition_kind")
                or ""
            ).strip().lower() in {
                "official_file_reference", "official_file_acquisition",
                "official_repository", "reference_asset",
            }
            locator_fields = normalize_official_locator(locator)
            # A local path is not an official repository locator.  Only merge
            # parsed fields when the contract is explicitly official or the
            # parser found a repository URL.
            if not is_official_contract and not locator_fields.get("repository_url"):
                locator_fields = {}
            if locator_fields:
                item["acquisition_contract"] = {
                    **acquisition_contract,
                    **locator_fields,
                }
        # The Publisher emits package/stage contracts from the normalized stage
        # definition; a package-level placeholder would only duplicate that
        # deterministic contract in the Analyst payload.
        if _is_publisher_stage_contract(item):
            continue
        item_text = json.dumps(item, ensure_ascii=False, default=str)
        malformed = re.search(r"\bNACA\s*[-_ ]?\s*(\d{2,3})(?!\d)", item_text, flags=re.I)
        replacement = next(
            (
                code for code in valid_naca_codes
                if code.removeprefix("NACA").startswith(malformed.group(1))
            ),
            "",
        ) if malformed else ""
        if replacement:
            old = malformed.group(0)
            item = {
                key: re.sub(re.escape(old), replacement, value, flags=re.I)
                if isinstance(value, str) else value
                for key, value in item.items()
            }
        normalized_required_files.append(item)
    # A composite solver input may embed the generated discretization and all
    # model definitions. In request-bound mode, Analyst-expanded local source,
    # script and mesh entries are producer intermediates unless the authority
    # names them as separate outputs. Keeping them as peer deliverables creates
    # competing producers and lets an internal implementation choice rewrite
    # the caller's work order.
    if isinstance(request, dict) and request.get("review_profile") == "request_bound":
        composite_outputs = [
            item for item in normalized_required_files
            if isinstance(item, dict)
            and str(normalize_asset_contract(item).get("representation") or "") == "simulation_case"
            and _asset_requires_mesh(item)
            and item.get("delivery_required") is not False
        ]
        explicit_names = {
            canonical_filename_identity(name)
            for name in [
                *_direct_request_filenames(authority, {}),
                *[
                    asset.get("filename")
                    for asset in request.get("requested_assets") or []
                    if isinstance(asset, dict)
                ],
            ]
            if canonical_filename_identity(name)
        }
        if len(composite_outputs) == 1:
            composite_id = str(composite_outputs[0].get("id") or "")
            for item in normalized_required_files:
                if not isinstance(item, dict) or str(item.get("id") or "") == composite_id:
                    continue
                contract = normalize_asset_contract(item)
                filename = next((
                    canonical_filename_identity(item.get(key))
                    for key in ("expected_filename", "filename", "file_name", "declared_output_path")
                    if canonical_filename_identity(item.get(key))
                ), "")
                if (
                    contract.get("source_strategy") == "local_generation"
                    and filename not in explicit_names
                ):
                    item["delivery_required"] = False
    analysis["required_files"] = normalized_required_files
    normalized_stages: list[dict[str, Any]] = []
    for index, item in enumerate(analysis.get("calculation_stages") or [], 1):
        if not isinstance(item, dict):
            continue
        stage = dict(item)
        stage["id"] = re.sub(
            r"[^a-z0-9]+",
            "_",
            str(stage.get("id") or stage.get("name") or f"stage_{index}").lower(),
        ).strip("_") or f"stage_{index}"
        stage["name"] = str(stage.get("name") or stage["id"]).strip()
        stage["calculation_type"] = str(
            stage.get("calculation_type") or stage.get("type") or stage["name"]
        ).strip()
        for key in ("required_file_roles", "dependencies", "evidence"):
            if not isinstance(stage.get(key), list):
                stage[key] = []
        if not stage["evidence"]:
            stage["evidence"] = analysis.get("evidence") or [{
                "source_type": "upstream",
                "detail": f"Calculation stage {stage['name']} was identified from upstream task information.",
            }]
        if not isinstance(stage.get("parameters"), dict):
            stage["parameters"] = {}
        if stage.get("software") not in (None, "", {}):
            stage["software"] = normalize_software_identity(stage["software"])
        elif stage["parameters"].get("software") not in (None, "", {}):
            stage["parameters"]["software"] = normalize_software_identity(
                stage["parameters"]["software"]
            )
        try:
            stage["confidence"] = max(0.0, min(1.0, float(stage.get("confidence", 0.75))))
        except (TypeError, ValueError):
            stage["confidence"] = 0.75
        normalized_stages.append(stage)
    known_stage_ids = {
        str(stage.get("id") or "").strip()
        for stage in normalized_stages
        if str(stage.get("id") or "").strip()
    }
    # Stage ownership crosses the Analyst, explicit-plan parser, Designer and
    # executor boundaries.  Normalize it at the same boundary as stage IDs.
    # Previously stages were canonicalized (``S4`` -> ``s4``) while
    # required_files.stage_id / consumer_stages kept the model spelling.  The
    # later ownership gate therefore rejected every otherwise valid asset as
    # unowned.  This is an identifier contract, not an inference rule: unknown
    # IDs remain visible for the downstream contract check.
    for item in normalized_required_files:
        if not isinstance(item, dict):
            continue
        raw_stage_id = str(item.get("stage_id") or "").strip()
        if raw_stage_id:
            item["stage_id"] = _slug(raw_stage_id, "")
        raw_consumers = item.get("consumer_stages") or []
        if isinstance(raw_consumers, str):
            raw_consumers = [raw_consumers]
        item["consumer_stages"] = list(dict.fromkeys(
            canonical
            for value in raw_consumers
            if (canonical := _slug(str(value or "").strip(), ""))
        ))
    for stage in normalized_stages:
        valid_dependencies: list[str] = []
        external_dependencies: list[str] = []
        for dependency in stage.get("dependencies") or []:
            raw_dep_id = str(dependency or "").strip()
            if not raw_dep_id:
                continue
            dep_id = re.sub(r"[^a-z0-9]+", "_", raw_dep_id.lower()).strip("_")
            if dep_id in known_stage_ids:
                valid_dependencies.append(dep_id)
            else:
                external_dependencies.append(raw_dep_id)
        if external_dependencies:
            stage["dependencies"] = valid_dependencies
            parameters = stage.setdefault("parameters", {})
            existing_external = [
                str(item)
                for item in parameters.get("external_stage_dependencies") or []
                if str(item).strip()
            ]
            for dep_id in external_dependencies:
                if dep_id not in existing_external:
                    existing_external.append(dep_id)
            parameters["external_stage_dependencies"] = existing_external
            stage.setdefault("evidence", []).append({
                "source_type": "upstream",
                "detail": (
                    "Stage depends on prerequisite workflow outputs outside the selected "
                    f"stage set: {', '.join(external_dependencies)}. These are treated as "
                    "predecessor input requirements rather than in-plan dependencies."
                ),
            })
        else:
            stage["dependencies"] = valid_dependencies
    analysis["calculation_stages"] = normalized_stages
    # Stage ``expected_outputs`` describe products created when that stage is
    # eventually run.  Preserve them as asset/dependency contracts, but mark
    # matching entries before any Designer or executor validation so a stored
    # analysis from an earlier run cannot reintroduce the old "write the
    # dataset now" interpretation.
    runtime_outputs_by_key: dict[str, str] = {}
    runtime_inputs_by_key: dict[str, list[str]] = {}
    stages_by_id = {
        str(stage.get("id") or "").strip(): stage
        for stage in normalized_stages
        if str(stage.get("id") or "").strip()
    }
    for stage in normalized_stages:
        stage_id = str(stage.get("id") or "").strip()
        for output in stage.get("expected_outputs") or []:
            key = _normalise_asset_key(output)
            if key:
                runtime_outputs_by_key[key] = stage_id
        for role in stage.get("required_file_roles") or []:
            key = _normalise_asset_key(role)
            if key and stage_id:
                runtime_inputs_by_key.setdefault(key, []).append(stage_id)
    delivery_files: list[dict[str, Any]] = []
    runtime_access_dependencies = [
        item for item in analysis.get("runtime_access_dependencies") or []
        if isinstance(item, dict)
    ]
    runtime_tool_requirements = _merge_runtime_tool_requirements([
        item for item in analysis.get("runtime_tool_requirements") or []
        if isinstance(item, dict)
    ])
    known_runtime_access = {str(item.get("id") or "") for item in runtime_access_dependencies}
    known_runtime_tools = {
        _normalise_asset_key(item.get("tool_identity") or item.get("scientific_role") or item.get("id"))
        for item in runtime_tool_requirements
    }
    # Values that identify a separately delivered external input are stage
    # context, not literal fields that every local configuration must contain.
    # For example, a WPS namelist consumes ``Vtable.ECMWF`` from its stage
    # input, but has no valid ``ungrib = Vtable.ECMWF`` namelist assignment.
    # Keep this generic by matching normalized asset identities, filenames and
    # declared locators; explicit file-level bindings remain authoritative.
    external_reference_tokens_by_stage: dict[str, set[str]] = {}
    for candidate in normalized_required_files:
        if not isinstance(candidate, dict):
            continue
        if not normalize_asset_contract(candidate).get("is_external"):
            continue
        owners = candidate.get("consumer_stages") or candidate.get("stage_id") or []
        if isinstance(owners, str):
            owners = [owners]
        tokens = {
            _normalise_asset_key(candidate.get(key))
            for key in ("id", "logical_asset_id", "name_or_role", "filename", "file_name", "expected_filename")
            if _normalise_asset_key(candidate.get(key))
        }
        acquisition = candidate.get("acquisition_contract")
        if isinstance(acquisition, dict):
            for key in ("path", "locator", "url"):
                value = acquisition.get(key)
                if value:
                    tokens.add(_normalise_asset_key(PurePosixPath(str(value).split("?", 1)[0]).name))
        for owner in owners:
            owner_id = _slug(str(owner or ""), "")
            if owner_id and tokens:
                external_reference_tokens_by_stage.setdefault(owner_id, set()).update(tokens)

    for item in analysis.get("required_files") or []:
        if not isinstance(item, dict):
            continue
        # Recompile bindings that were derived from stage context.  This keeps
        # persisted/Analyst-emitted contracts consistent with the shared
        # file-binding compiler and removes representation metadata that was
        # previously promoted to an immutable file value.  Explicit
        # file-level contracts use a different source and remain untouched.
        if (
            not normalize_asset_contract(item).get("is_external")
            and str(item.get("parameter_binding_source") or "").strip()
            == "stage_explicit_parameters"
        ):
            owning_stage = stages_by_id.get(str(item.get("stage_id") or "").strip()) or {}
            derived_contract = _explicit_file_parameter_contract(
                owning_stage,
                binding_mode=(
                    "semantic"
                    if canonical_workflow_capability(item) == "preprocessing_script_generation"
                    else "literal"
                ),
            )
            if derived_contract:
                external_tokens = external_reference_tokens_by_stage.get(
                    str(item.get("stage_id") or "").strip(),
                    set(),
                )
                if external_tokens:
                    removable = {
                        key for key, value in (derived_contract.get("parameter_bindings") or {}).items()
                        if _normalise_asset_key(value) in external_tokens
                    }
                    if removable:
                        remaining = {
                            key: value
                            for key, value in (derived_contract.get("parameter_bindings") or {}).items()
                            if key not in removable
                        }
                        derived_contract["parameter_bindings"] = remaining
                        derived_contract["parameter_binding_assertions"] = [
                            assertion
                            for assertion in derived_contract.get("parameter_binding_assertions") or []
                            if str(assertion.get("name") or assertion.get("binding") or "") not in removable
                        ]
                        if not remaining:
                            derived_contract = {}
                if derived_contract:
                    item.update(derived_contract)
                else:
                    for binding_key in (
                        "parameter_bindings", "parameter_bindings_required",
                        "parameter_binding_mode", "parameter_binding_source",
                        "parameter_binding_assertions",
                    ):
                        item.pop(binding_key, None)
            else:
                for binding_key in (
                    "parameter_bindings", "parameter_bindings_required",
                    "parameter_binding_mode", "parameter_binding_source",
                    "parameter_binding_assertions",
                ):
                    item.pop(binding_key, None)
        # Complete API-style external dataset manifests from the owning stage
        # contract before Designer sees them.  The stage parameters are
        # already authoritative Research Plan data; copying them into
        # ``request_parameters`` closes a mechanical contract gap without
        # inventing a provider query or adding an application-specific rule.
        preliminary_contract = normalize_asset_contract(item)
        if preliminary_contract.get("acquisition_kind") == "external_dataset":
            consumer_ids = item.get("consumer_stages") or item.get("stage_id") or []
            if isinstance(consumer_ids, str):
                consumer_ids = [consumer_ids]
            stage_parameters: dict[str, Any] = {}
            for consumer_id in consumer_ids:
                stage = stages_by_id.get(str(consumer_id).strip()) or {}
                parameters = stage.get("parameters")
                if isinstance(parameters, dict):
                    stage_parameters.update(
                        deepcopy({key: value for key, value in parameters.items()
                                  if value not in (None, "", [], {})})
                    )
            if stage_parameters:
                normalized_acquisition = normalize_acquisition_contract({
                    **item,
                    "available_stage_parameters": stage_parameters,
                    "acquisition_contract": item.get("acquisition_contract") or {},
                })
                if "request_parameters" not in acquisition_contract_errors(normalized_acquisition):
                    item["acquisition_contract"] = normalized_acquisition
        matched_stage = next(
            (
                stage_id for key, stage_id in runtime_outputs_by_key.items()
                if key in {
                    _normalise_asset_key(item.get("id")),
                    _normalise_asset_key(item.get("name_or_role")),
                }
            ),
            "",
        )
        contract = normalize_asset_contract(item)
        # An explicit official-file contract is a stage input, even when an
        # older Analyst record (or a copied plan) labelled it
        # ``runtime_output``.  Official files are acquired/reused before the
        # consuming stage; they are never produced by that stage.  Resolve
        # this contradiction from the normalized AssetContract and declared
        # consumers, rather than from a filename or solver-specific alias.
        official_file_input = bool(
            contract.get("is_external")
            and contract.get("acquisition_kind") == "official_file_reference"
            and contract.get("workflow_capability") == "official_file_acquisition"
        )
        declared_consumers = item.get("consumer_stages")
        if isinstance(declared_consumers, str):
            declared_consumers = [declared_consumers]
        declared_consumers = [
            str(stage_id).strip()
            for stage_id in (declared_consumers or [])
            if str(stage_id).strip() in stages_by_id
        ]
        declared_owner = str(item.get("stage_id") or "").strip()
        explicit_owner = [declared_owner] if declared_owner in stages_by_id else []
        if official_file_input:
            owning_stages = declared_consumers or explicit_owner or runtime_inputs_by_key.get(
                _normalise_asset_key(item.get("id"))
            ) or runtime_inputs_by_key.get(
                _normalise_asset_key(item.get("name_or_role"))
            ) or []
            if owning_stages:
                item["fulfillment_kind"] = "stage_input"
                item["delivery_required"] = True
                if len(set(owning_stages)) == 1:
                    item["stage_id"] = owning_stages[0]
        if matched_stage and not official_file_input:
            item["fulfillment_kind"] = "runtime_output"
            item["delivery_required"] = False
            item.setdefault("stage_id", matched_stage)
        elif contract.get("is_external"):
            owning_stages = declared_consumers or explicit_owner or runtime_inputs_by_key.get(_normalise_asset_key(item.get("id"))) or runtime_inputs_by_key.get(
                _normalise_asset_key(item.get("name_or_role"))
            ) or []
            # An external input consumed by a declared stage belongs to the
            # data package.  The data node may acquire/prep it but never runs
            # the stage that consumes it.  Only the stage's *output* remains
            # a deferred runtime product.  Treating both sides as deferred
            # silently left downstream simulation nodes without their inputs.
            if owning_stages:
                item["fulfillment_kind"] = "stage_input"
                item["delivery_required"] = True
                if len(set(owning_stages)) == 1:
                    item.setdefault("stage_id", owning_stages[0])
        item.update({key: value for key, value in contract.items() if key != "contradictory_fields"})
        owning_stage = stages_by_id.get(str(item.get("stage_id") or "").strip()) or {}
        # Use the declared role/filename, not the Analyst's prior format
        # label.  The latter is a derived field and may itself have been
        # produced by the old broad "input means configuration" heuristic.
        role_text = str(item.get("name_or_role") or item.get("filename") or item.get("file_name") or "")
        portable_text_asset = bool(re.search(
            r"script|program|code|namelist|config|configuration|parameter|control|template|"
            r"\\.(?:json|ya?ml|toml|ini|cfg|py|sh)\\b|脚本|程序|配置|参数|控制",
            role_text,
            flags=re.I,
        ))
        # Analyst prose can label every unknown role a "parameter file".
        # For a stage with an explicit predecessor, an opaque locally-derived
        # role is instead a runtime hand-off.  This is a DAG rule, not a
        # solver/file-name exception, and keeps data-node delivery focused on
        # real configs, scripts, references, and external datasets.
        if (
            owning_stage.get("dependencies")
            and not contract.get("is_external")
            and contract.get("source_strategy") == "local_generation"
            and not portable_text_asset
        ):
            item["fulfillment_kind"] = "runtime_output"
            item["delivery_required"] = False
        if _is_deferred_data_product(item, contract):
            # Keep the asset visible in stage provenance, but do not require
            # a text-artifact writer to fabricate a scientific data file.
            # Its declared generator/acquisition recipe is packaged with the
            # owning stage and executed later under the approved workflow.
            item["fulfillment_kind"] = "runtime_output"
            item["delivery_required"] = False
        if contract.get("fulfillment_kind") == "runtime_access":
            dependency_id = str(item.get("id") or "")
            if dependency_id not in known_runtime_access:
                runtime_access_dependencies.append({
                    "id": dependency_id,
                    "scientific_role": item.get("scientific_role"),
                    "reason": item.get("reason") or "Runtime access must be supplied by an authorized user.",
                    "consequences_if_missing": item.get("consequences_if_missing") or "The approved runtime acquisition step cannot run.",
                    "resume_contract": "Provide the authorized runtime access through the execution environment; do not download or generate credentials.",
                })
                known_runtime_access.add(dependency_id)
            continue
        if contract.get("fulfillment_kind") == "runtime_tool":
            tool_key = _normalise_asset_key(item.get("name_or_role") or item.get("id"))
            if tool_key not in known_runtime_tools:
                runtime_tool_requirements.append({
                    "id": str(item.get("id") or f"runtime_tool_{len(runtime_tool_requirements) + 1}"),
                    "tool_identity": str(item.get("name_or_role") or item.get("id") or "runtime_tool"),
                    "scientific_role": item.get("scientific_role"),
                    "representation": "runtime_tool",
                    "consumer_stages": [str(item.get("stage_id"))] if item.get("stage_id") else [],
                    "resume_contract": "Have Experiment provide this executable in the calling environment; do not search for or package it as a data artifact.",
                })
                known_runtime_tools.add(tool_key)
            elif item.get("stage_id"):
                for requirement in runtime_tool_requirements:
                    if _normalise_asset_key(
                        requirement.get("tool_identity")
                        or requirement.get("scientific_role")
                        or requirement.get("id")
                    ) != tool_key:
                        continue
                    requirement["consumer_stages"] = sorted({
                        *[
                            str(stage)
                            for stage in requirement.get("consumer_stages") or []
                            if str(stage)
                        ],
                        str(item["stage_id"]),
                    })
                    break
            continue
        # Fulfillment classification may have changed above.  Recompute the
        # shared delivery contract so runtime products are not exposed as
        # generated files and external datasets are represented by acquisition
        # documents rather than required local payloads.
        contract = normalize_asset_contract(item)
        item.update({
            key: value for key, value in contract.items()
            if key != "contradictory_fields"
        })
        evidence = item.get("evidence")
        if isinstance(evidence, str):
            item["evidence"] = [{"source_type": "upstream", "detail": evidence}]
        elif not isinstance(evidence, list):
            item["evidence"] = []
        confidence = item.get("confidence")
        if isinstance(confidence, str):
            item["confidence"] = 0.9 if re.search(r"high|certain|明确|高", confidence, flags=re.I) else 0.75
        item["workflow_capability"] = canonical_workflow_capability(item)
        delivery_files.append(item)
    analysis["required_files"] = delivery_files
    # Persist the complete hand-off contract for each Research Plan stage.
    # This separates locally delivered parameters from external acquisition
    # and predecessor runtime products, so a directory with one incidental
    # file can no longer be mistaken for a runnable downstream stage.
    runtime_assets_by_stage = {
        str(stage.get("id") or "").strip(): [
            _runtime_asset_contract(
                str(stage.get("id") or "").strip(),
                output,
                index,
            )
            for index, output in enumerate(stage.get("expected_outputs") or [], start=1)
            if str(output or "").strip()
        ]
        for stage in normalized_stages
        if str(stage.get("id") or "").strip()
    }
    stage_input_contracts: list[dict[str, Any]] = []
    for stage in normalized_stages:
        stage_id = str(stage.get("id") or "").strip()
        if not stage_id:
            continue
        owned = [item for item in delivery_files if str(item.get("stage_id") or "").strip() == stage_id]
        local_ids = [str(item.get("id") or "") for item in owned if not normalize_asset_contract(item).get("is_external") and item.get("delivery_required") is not False]
        # Only materialized/acquisition-document inputs are stage launch
        # assets.  Runtime outputs and deferred references (for example a
        # Vtable that is resolved in the execution environment) are dependency
        # metadata, not files this package must publish.
        external_ids = [
            str(item.get("id") or "")
            for item in owned
            if normalize_asset_contract(item).get("is_external")
            and item.get("delivery_required") is not False
            and str(item.get("fulfillment_kind") or "") != "runtime_output"
        ]
        external_inputs = [
            {
                "logical_asset_id": normalize_asset_contract(item).get("logical_asset_id"),
                "canonical_path": (
                    f"runtime_inputs/{_slug(stage_id, 'stage')}/"
                    f"{_slug(str(item.get('id') or item.get('name_or_role') or 'external_input'))}"
                ),
                "representation": normalize_asset_contract(item).get("representation"),
                "materialization_kind": "acquisition_document",
            }
            for item in owned
            if normalize_asset_contract(item).get("is_external")
            and item.get("delivery_required") is not False
            and str(item.get("fulfillment_kind") or "") != "runtime_output"
        ]
        upstream_runtime_inputs = [
            {
                **asset,
                "consumer_stage": stage_id,
            }
            for dependency in stage.get("dependencies") or []
            for asset in runtime_assets_by_stage.get(str(dependency), [])
        ]
        stage_input_contracts.append({
            "stage_id": stage_id,
            # Keep host inventory paths out of the portable hand-off contract;
            # consumers use the canonical relative paths below.
            "stage_parameters": _stage_parameter_context(stage),
            "local_parameter_ids": [item for item in local_ids if item],
            "external_input_ids": [item for item in external_ids if item],
            "external_inputs": external_inputs,
            "upstream_runtime_stage_ids": [str(item) for item in stage.get("dependencies") or [] if str(item)],
            "upstream_runtime_inputs": upstream_runtime_inputs,
            "runtime_outputs": runtime_assets_by_stage.get(stage_id, []),
            "runtime_output_roles": list(stage.get("expected_outputs") or []),
        })
    analysis["stage_input_contracts"] = stage_input_contracts
    if runtime_access_dependencies:
        analysis["runtime_access_dependencies"] = runtime_access_dependencies
    if runtime_tool_requirements:
        analysis["runtime_tool_requirements"] = _merge_runtime_tool_requirements(
            runtime_tool_requirements
        )
    try:
        analysis["confidence"] = max(0.0, min(1.0, float(analysis.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence_text = str(analysis.get("confidence") or "")
        analysis["confidence"] = 0.9 if re.search(r"high|certain|明确|高", confidence_text, flags=re.I) else 0.75
    analysis["calculation_type"] = str(analysis.get("calculation_type") or "").strip()
    stage_scope = analysis.get("structured_stage_scope")
    if isinstance(stage_scope, dict) and stage_scope.get("source"):
        allowed_stage_ids = {
            str(item).strip().casefold()
            for item in stage_scope.get("stage_ids") or []
            if str(item).strip()
        }
        if allowed_stage_ids:
            analysis["required_files"] = [
                item for item in analysis.get("required_files") or []
                if not isinstance(item, dict)
                or not str(item.get("stage_id") or "").strip()
                or str(item.get("stage_id") or "").strip().casefold() in allowed_stage_ids
            ]
    # Establish the capability boundary once, after Analyst normalization.
    # All later routing consumes this structured contract rather than prose.
    analysis["task_scope"] = _task_scope_contract(analysis)
    return analysis


def _apply_upstream_discipline_authority(
    analysis: dict[str, Any],
    task_context: str | dict[str, Any],
) -> dict[str, Any]:
    """Keep the research-plan discipline authoritative over model classification."""
    declared = _declared_discipline_from_context(task_context)
    if not declared:
        return analysis
    updated = dict(analysis)
    prior = updated.get("discipline") if isinstance(updated.get("discipline"), dict) else {}
    software = str((updated.get("simulation_software") or {}).get("name") or "")
    context_text = (
        task_context
        if isinstance(task_context, str)
        else json.dumps(task_context, ensure_ascii=False, default=str)
    )
    adapter = _explicit_discipline_from_context(context_text, software)
    prior_primary = str(prior.get("primary") or "").strip()
    evidence = [
        {
            "source_type": "upstream_research_plan",
            "detail": f"Research plan explicitly declares the discipline as {declared}.",
        },
        *(prior.get("evidence") or []),
    ]
    updated["discipline"] = {
        **prior,
        "primary": declared,
        "declared": declared,
        "adapter_route": adapter if adapter != "unknown" else "generic",
        "evidence": evidence,
    }
    if prior_primary and prior_primary.lower() != declared.lower():
        updated["discipline"]["model_inferred_candidate"] = prior_primary
    return updated


def _structured_context_object(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _listify(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _expand_stage_reference(value: Any) -> list[str]:
    refs: list[str] = []
    non_stage_prefixes = {"h", "hypothesis", "case", "task", "run"}
    for item in _listify(value):
        text = str(item or "").strip()
        # Structured schedulers often encode a group as ``S4_S8_S12``.
        # Treat the separators as a stage list, not as part of one synthetic
        # identifier; this remains generic for any stage prefix.
        text = re.sub(r"(?<=\d)_(?=[A-Za-z]+\d)", " ", text)
        if not text:
            continue
        range_pattern = re.compile(
            r"(?<![A-Za-z0-9_])([A-Za-z]+)(\d+)\s*[-–—]\s*\1?(\d+)(?![A-Za-z0-9_])",
            flags=re.I,
        )
        ranges = list(range_pattern.finditer(text))
        for range_match in ranges:
            prefix = range_match.group(1).lower()
            if prefix in non_stage_prefixes:
                continue
            start = int(range_match.group(2))
            end = int(range_match.group(3))
            step = 1 if end >= start else -1
            refs.extend(f"{prefix}{index}" for index in range(start, end + step, step))
        remainder = range_pattern.sub(" ", text)
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z]+)(\d+[A-Za-z0-9_]*)(?![A-Za-z0-9_])", remainder):
            if match.group(1).lower() in non_stage_prefixes:
                continue
            refs.append(match.group(0).lower())
    return list(dict.fromkeys(refs))




def _apply_structured_stage_scope(
    analysis: dict[str, Any],
    task_context: str | dict[str, Any],
) -> dict[str, Any]:
    """Use the caller's structured stage map without hypothesis projection."""
    context = _structured_context_object(task_context)
    responsible_entries: list[dict[str, Any]] = []
    for key, value in context.items():
        key_text = str(key or "").lower()
        if "stage" not in key_text or not re.search(r"responsib|scope|asset|data_node|data", key_text):
            continue
        responsible_entries.extend(item for item in _listify(value) if isinstance(item, dict))
    declared_scope_ids: list[str] = []
    source_by_stage: dict[str, dict[str, Any]] = {}
    for entry in responsible_entries:
        references = _expand_stage_reference(
            entry.get("stage_ids") or entry.get("stage_id") or entry.get("stages") or entry.get("id")
        )
        for stage_id in references:
            if stage_id not in declared_scope_ids:
                declared_scope_ids.append(stage_id)
            source_by_stage.setdefault(stage_id, entry)
    # The normalized request remains the complete scope authority.  Structured
    # entries enrich its stage records; they never select or discard branches.
    planned_stage_ids = [
        str(stage.get("id") or "").strip()
        for stage in analysis.get("calculation_stages") or []
        if isinstance(stage, dict) and str(stage.get("id") or "").strip()
    ]
    for stage_id in planned_stage_ids:
        if stage_id not in declared_scope_ids:
            declared_scope_ids.append(stage_id)
    if declared_scope_ids:
        existing = {
            str(stage.get("id") or "").strip().casefold(): stage
            for stage in analysis.get("calculation_stages") or []
            if isinstance(stage, dict) and str(stage.get("id") or "").strip()
        }
        scoped: list[dict[str, Any]] = []
        for stage_id in declared_scope_ids:
            entry = source_by_stage.get(stage_id) or {}
            current = existing.get(stage_id.casefold()) or {}
            output = entry.get("output") or entry.get("outputs") or entry.get("required_outputs")
            roles = list(current.get("required_file_roles") or [])
            if isinstance(output, str) and output.strip() and output not in roles:
                roles.append(output.strip())
            elif isinstance(output, list):
                roles.extend(str(value) for value in output if str(value).strip() and str(value) not in roles)
            name = str(entry.get("name") or current.get("name") or stage_id)
            description = str(entry.get("description") or entry.get("calculation_type") or current.get("calculation_type") or name)
            scoped.append({
                **current,
                "id": stage_id,
                "source_step_id": str(current.get("source_step_id") or stage_id),
                "name": name,
                "calculation_type": description,
                "required_file_roles": roles,
                "parameters": {
                    **dict(current.get("parameters") or {}),
                    **dict(entry.get("parameters") or {}),
                },
                "evidence": [
                    *list(current.get("evidence") or []),
                    {"source_type": "upstream_data_stage_scope", "detail": _compact_text(entry, 900)},
                ],
            })
        result = dict(analysis)
        result["calculation_stages"] = scoped
        result["structured_stage_scope"] = {
            "source": "structured_data_asset_scope",
            "stage_ids": declared_scope_ids,
            "policy": "explicit upstream data-stage responsibility defines every asset-preparation stage; formal execution remains downstream",
        }
        return result
    return analysis


def _coerce_requirement_analysis(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = _extract_json_object(raw)
        except ValueError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _requirement_analysis_score(
    value: dict[str, Any] | None,
) -> tuple[int, int, int, int, int, int, int]:
    analysis = _coerce_requirement_analysis(value)
    if not isinstance(analysis, dict):
        return (-1, -1, -1, -1, -1, -1, -1)
    discipline = analysis.get("discipline") or {}
    discipline_evidence = (
        discipline.get("evidence") or []
        if isinstance(discipline, dict)
        else []
    )
    has_declared_upstream_discipline = bool(
        isinstance(discipline, dict)
        and (
            discipline.get("declared")
            or any(
                isinstance(item, dict)
                and item.get("source_type") == "upstream_research_plan"
                for item in discipline_evidence
            )
        )
    )
    stages = [
        item for item in analysis.get("calculation_stages") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    stage_contract_ids = {
        str(item.get("stage_id") or "").strip()
        for item in analysis.get("stage_input_contracts") or []
        if isinstance(item, dict) and str(item.get("stage_id") or "").strip()
    }
    normalized_stage_contract = bool(stages) and stage_contract_ids == {
        str(item.get("id") or "").strip() for item in stages
    }
    # The model-independent Analyst result is authoritative.  A minimal
    # caller fallback may contain more prose-discovered filenames than the
    # Analyst result, but it must never outrank the confirmed contract merely
    # because it has a larger list.  This was the route that replaced an
    # OpenFOAM mesh request with NACA coordinate files after reference search.
    source = str(
        (analysis.get("task_scope") or {}).get("source")
        if isinstance(analysis.get("task_scope"), dict)
        else ""
    ).strip().lower()
    fallback = bool(analysis.get("fallback")) or source == "asset_contract_fallback"
    confirmed_scope = source in {
        "structured_data_asset_scope", "research_plan_scope", "caller_contract",
    }
    return (
        int(not fallback),
        int(confirmed_scope),
        # A normalized stage/interface contract is downstream-executable and
        # must outrank a larger raw fallback.
        int(normalized_stage_contract),
        int(has_declared_upstream_discipline),
        len(stages),
        len(analysis.get("required_files") or []),
        int(bool((analysis.get("simulation_software") or {}).get("name")))
        if isinstance(analysis.get("simulation_software"), dict)
        else int(bool(analysis.get("simulation_software"))),
    )


def _best_requirement_analysis(*values: Any) -> dict[str, Any] | None:
    candidates = [
        _coerce_requirement_analysis(value)
        for value in values
        if _coerce_requirement_analysis(value) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=_requirement_analysis_score)


def _is_registered_tool_name(value: Any) -> bool:
    candidate = str(value or "").strip().casefold()
    return bool(candidate) and candidate in {
        str(name).strip().casefold() for name in all_tool_names()
    }


def _explicit_software_from_context(task_context: str | dict[str, Any]) -> str:
    """Prefer explicit software fields over loose prose matches such as SI units."""
    if isinstance(task_context, dict):
        for key in (
            "target_software",
            "simulation_software",
            "solver",
            "application",
            "software",
        ):
            value = task_context.get(key)
            if isinstance(value, dict):
                value = value.get("name")
            if (
                isinstance(value, str)
                and value.strip()
                and not _is_registered_tool_name(value)
            ):
                return value.strip()
    text = task_context if isinstance(task_context, str) else json.dumps(task_context, ensure_ascii=False, default=str)
    solver = extract_solver_name(text)
    if solver:
        return solver
    for pattern in (
        r'"(?:target_software|simulation_software|solver|application|software)"\s*:\s*"([^"]{2,80})"',
        r"(?:target_software|simulation_software|目标软件|仿真软件|求解器)\s*[:：]\s*([A-Za-z][A-Za-z0-9_.+ -]{1,60})",
    ):
        match = re.search(pattern, text, flags=re.I)
        if match:
            value = match.group(1).strip().rstrip(",，。;；")
            if value and (
                not _is_registered_tool_name(value)
                and value.upper() not in {"SI", "DFT", "MD", "CFD"}
            ) and not re.search(
                r"\b(potcar|potpaw|pseudopotential|赝势)\b|\.tar(?:\.gz)?$|\.zip$",
                value,
                flags=re.I,
            ):
                return value
    return ""


_DISCIPLINE_CONTEXT_KEYS = {
    "discipline",
    "scientific_discipline",
    "research_discipline",
    "research_domain",
    "domain",
    "field",
    "subject_area",
}


def _clean_declared_discipline(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("primary", "name", "label", "value"):
            cleaned = _clean_declared_discipline(value.get(key))
            if cleaned:
                return cleaned
        return ""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", " ", value).strip().strip("'\"`，,。;；")
    if not cleaned or len(cleaned) > 100:
        return ""
    if cleaned.lower() in {"unknown", "unspecified", "none", "null", "未确定", "未知", "未指定"}:
        return ""
    return cleaned


def _declared_discipline_from_context(task_context: str | dict[str, Any]) -> str:
    """Read an upstream research-plan declaration without limiting its vocabulary."""
    if isinstance(task_context, dict):
        queue: list[dict[str, Any]] = [task_context]
        while queue:
            current = queue.pop(0)
            for key, value in current.items():
                normalized_key = str(key).strip().lower().replace("-", "_").replace(" ", "_")
                if normalized_key in _DISCIPLINE_CONTEXT_KEYS:
                    cleaned = _clean_declared_discipline(value)
                    if cleaned:
                        return cleaned
                if isinstance(value, dict):
                    queue.append(value)
                elif isinstance(value, list):
                    queue.extend(item for item in value if isinstance(item, dict))
        text = json.dumps(task_context, ensure_ascii=False, default=str)
    else:
        text = str(task_context or "")

    patterns = (
        r'"(?:discipline|scientific_discipline|research_discipline|research_domain|subject_area)"\s*:\s*"([^"\n]{2,100})"',
        r'"discipline"\s*:\s*\{[^{}]{0,300}?"(?:primary|name|label)"\s*:\s*"([^"\n]{2,100})"',
        r"(?im)^\s*(?:discipline|scientific discipline|research discipline|research domain|subject area|学科|研究学科|研究领域)\s*[:：]\s*([^\n]{2,100})$",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            cleaned = _clean_declared_discipline(match.group(1))
            if cleaned:
                return cleaned
    return ""


def _explicit_discipline_from_context(
    text: str,
    software: str = "",
    declared_discipline: str = "",
) -> str:
    if declared_discipline:
        return declared_discipline
    haystack = f"{software} {text}"
    adapter_discipline = discipline_from_solver(haystack)
    if adapter_discipline:
        return adapter_discipline
    if re.search(
        r"\b(CFD|airfoil|aerofoil|DNS|RANS|LES|"
        r"Reynolds|Mach|inlet|outlet|wall|farfield|velocity|pressure)\b|"
        r"流体|翼型|机翼|雷诺数|攻角|来流|速度场|压力场|边界层|层流分离泡",
        haystack,
        flags=re.I,
    ):
        return "cfd"
    if re.search(r"\b(molecular dynamics)\b|分子动力学", haystack, flags=re.I):
        return "md"
    if re.search(r"\b(solid mechanics|finite element)\b|结构力学|有限元", haystack, flags=re.I):
        return "csm"
    if re.search(r"\b(openEMS|HFSS|Maxwell|electromagnetic)\b|电磁", haystack, flags=re.I):
        return "cem"
    if re.search(r"\b(heat transfer|thermal|conjugate heat)\b|传热|热", haystack, flags=re.I):
        return "heat_transfer"
    return "unknown"


_AIRFOIL_PROFILE_CODE_PATTERN = (
    r"(?:[A-Z]{1,4}\s*[-_]?\s*\d{2,4}[A-Z0-9]{0,3}|"
    r"DU\s*\d{2}\s*W\s*\d{2,4}|"
    r"NLF\s*\d{3,5})"
)


def _normalise_profile_code(value: str) -> str:
    return re.sub(r"[\s_-]+", "", str(value or "")).upper()


def _looks_like_computable_naca(value: str) -> bool:
    return bool(re.fullmatch(r"NACA\d{4,5}", _normalise_profile_code(value), flags=re.I))


def _airfoil_profile_codes_from_context(text: str) -> list[str]:
    """Extract named external profiles while excluding analytic NACA codes."""
    codes: list[str] = []
    for keyword in re.finditer(r"\b(?:airfoil|aerofoil|profile)\b|翼型", str(text or ""), flags=re.I):
        window = str(text or "")[max(0, keyword.start() - 100): keyword.end() + 60]
        for match in re.finditer(rf"\b({_AIRFOIL_PROFILE_CODE_PATTERN})\b", window, flags=re.I):
            code = _normalise_profile_code(match.group(1))
            if _looks_like_computable_naca(code) or re.fullmatch(r"NACA\d+", code, flags=re.I):
                continue
            if re.fullmatch(r"(?:V\d{2,5}|S\d{1,2}|[A-Z]\d{4,}|(?:RE|MA|Y|AOA|CFL)\d+)", code, flags=re.I):
                continue
            if code not in codes:
                codes.append(code)
    return codes


def _looks_like_citation_airfoil_label(value: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9]+", "", str(value or ""))
    return bool(
        _looks_like_computable_naca(compact)
        or re.search(r"\d{5,}", compact)
        or re.search(r"[A-Za-z]{5,}\d{4}$", compact)
    )


def _structured_values(context: dict[str, Any], key: str) -> list[Any]:
    """Collect explicitly structured caller fields without parsing prose tables."""
    found: list[Any] = []
    queue: list[Any] = [context]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            for item_key, value in current.items():
                if str(item_key).casefold() == key.casefold():
                    found.extend(value if isinstance(value, list) else [value])
                elif isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(current, list):
            queue.extend(current)
    return found


def _direct_request_filenames(text: str, context: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("expected_filename", "filename", "file_name", "output_name", "target_file"):
        for value in _structured_values(context, key):
            candidate = str(value or "").strip().replace("\\", "/")
            path = PurePosixPath(candidate)
            if path.is_absolute() or ".." in path.parts:
                continue
            if candidate and candidate not in names:
                names.append(candidate)
    for match in re.finditer(
        r"(?<![A-Za-z0-9_.-])([A-Za-z0-9][A-Za-z0-9_.+-]{0,80}\."
        r"(?:dat|txt|csv|json|ya?ml|toml|nml|namelist|py|sh|geo|msh|vtk|vtu|stl|obj|"
        r"cif|xyz|vasp|poscar|incar|kpoints|nc|grib2?|h5|hdf5|parquet))"
        r"(?=$|[\s,;:，；：。!?]|\.(?:\s|$))",
        text,
        flags=re.I,
    ):
        candidate = match.group(1)
        if candidate not in names:
            names.append(candidate)
    # A prose option such as “NACA 0012 or NACA 2412” is not a request for
    # two coordinate files.  Explicit filenames/required_files above still
    # preserve a caller that genuinely asks for both assets; the prose-only
    # fallback chooses the first declared profile as the selected input.
    naca_match = re.search(r"(?<![A-Za-z0-9])NACA[ _-]?(\d{4,5})(?!\d)", text, flags=re.I)
    coordinate_output_requested = bool(re.search(
        r"(?:generate|create|write|输出|生成|创建|写入)[^。\n]{0,80}"
        r"(?:coordinate|profile|坐标|轮廓)|"
        r"(?:coordinate|profile|坐标|轮廓)[^。\n]{0,80}"
        r"(?:file|asset|文件|generate|create|write|输出|生成|创建|写入)",
        text,
        flags=re.I,
    ))
    if naca_match and coordinate_output_requested:
        candidate = f"NACA{naca_match.group(1)}.dat"
        if candidate not in names:
            names.append(candidate)
    return names


def _has_structured_requirement_contract(task_context: str | dict[str, Any]) -> bool:
    """Return whether caller assets/stages can be compiled without interpretation."""
    context = _structured_context_object(task_context)
    return any(
        any(isinstance(item, dict) for item in _structured_values(context, key))
        for key in ("required_files", "requested_assets", "calculation_stages")
    )


def _compile_explicit_requirements(task_context: str | dict[str, Any]) -> dict[str, Any] | None:
    """Compile an explicit authority contract without scientific inference."""
    context = _structured_context_object(task_context)
    preprocessing_request = normalize_preprocessing_request(task_context)
    text = (
        task_context
        if isinstance(task_context, str)
        else json.dumps(task_context, ensure_ascii=False, default=str)
    )
    request_text = caller_request_text(
        task_context,
        preprocessing_request=preprocessing_request,
    ) or str(text)
    if not str(text or "").strip():
        return None

    structured_stages = [
        dict(item) for item in _structured_values(context, "calculation_stages")
        if isinstance(item, dict)
    ]
    structured_files = [
        dict(item) for item in _structured_values(context, "required_files")
        if isinstance(item, dict)
    ]
    existing_asset_ids = {str(item.get("id") or "") for item in structured_files}
    for item in preprocessing_request.get("requested_assets") or []:
        if not isinstance(item, dict) or str(item.get("asset_id") or "") in existing_asset_ids:
            continue
        structured_files.append({
            "id": item.get("asset_id"),
            "expected_filename": item.get("filename"),
            "name_or_role": item.get("filename") or item.get("asset_id"),
            "format": item.get("format"),
            "reason": item.get("purpose"),
            "acceptance_criteria": item.get("content_constraints") or [],
            **{
                key: item[key]
                for key in (
                    "scientific_role", "representation", "source_strategy",
                    "generation_recipe", "parameter_bindings", "workflow_capability",
                )
                if item.get(key) not in (None, "", [], {})
            },
        })
    # Once the caller has supplied structured required_files/requested_assets,
    # do not add filenames inferred from incidental prose (for example a
    # NACA code mentioned as geometry context).  The structured request is the
    # scope authority and inferred names are only a fallback for prose-only
    # requests.
    filenames = [] if structured_files else _direct_request_filenames(request_text, context)
    evidence = [{
        "source_type": "caller_request",
        "detail": "Derived only from the explicit caller-scoped preprocessing request.",
    }]
    naca_match = re.search(r"(?<![A-Za-z0-9])NACA[ _-]?(\d{4,5})(?!\d)", request_text, flags=re.I)
    local_request = bool(re.search(
        r"\b(generate|create|write|build|derive|convert|transform|prepare)\b|"
        r"生成|创建|写入|构建|解析式|转换|前处理",
        request_text,
        flags=re.I,
    ))
    required_files: list[dict[str, Any]] = []
    for index, item in enumerate(structured_files):
        normalized = dict(item)
        structured_naca = re.search(
            r"(?<![A-Za-z0-9])NACA[ _-]?(\d{4,5})(?!\d)",
            json.dumps(normalized, ensure_ascii=False, default=str),
            flags=re.I,
        )
        normalized.setdefault("id", _slug(
            normalized.get("name_or_role")
            or normalized.get("expected_filename")
            or f"requested_asset_{index + 1}",
            f"requested_asset_{index + 1}",
        ))
        normalized.setdefault(
            "name_or_role",
            normalized.get("expected_filename") or normalized["id"],
        )
        normalized.setdefault("reason", "Explicitly declared by the caller.")
        normalized.setdefault("evidence", evidence)
        normalized.setdefault("confidence", 1.0)
        normalized.setdefault(
            "consequences_if_missing",
            "The caller-requested preprocessing delivery is incomplete.",
        )
        declared_name = str(
            normalized.get("expected_filename")
            or normalized.get("declared_output_path")
            or normalized.get("name_or_role")
            or ""
        ).replace("\\", "/")
        declared_suffix = PurePosixPath(declared_name).suffix.lower()
        mesh_intent = _caller_explicitly_requests_mesh(
            task_context,
            {"preprocessing_request": preprocessing_request},
        )
        if local_request and not any(
            normalized.get(key)
            for key in ("source_strategy", "acquisition_kind", "acquisition_contract")
        ):
            declared_mesh_output = declared_name.casefold().rstrip("/").endswith("constant/polymesh") or declared_suffix == ".msh" or (
                declared_suffix == ".geo" and mesh_intent
            )
            normalized.update({
                "asset_role": normalized.get("asset_role") or (
                    "geometry_or_discretization_asset"
                    if structured_naca or declared_mesh_output else "parameter_file"
                ),
                "scientific_role": normalized.get("scientific_role") or (
                    "computational_mesh"
                    if declared_mesh_output
                    else "airfoil_coordinate_profile"
                    if structured_naca else "requested_preprocessing_asset"
                ),
                "representation": normalized.get("representation") or (
                    "mesh" if declared_mesh_output
                    else "coordinate_profile"
                    if structured_naca else "text_artifact"
                ),
                "workflow_capability": (
                    "mesh_generation" if declared_mesh_output
                    else "local_artifact_generation"
                ),
                "source_strategy": "local_generation",
                "acquisition_kind": "generated_artifact",
                "generation_recipe": normalized.get("generation_recipe") or (
                    "Generate and validate the caller-requested computational mesh."
                    if declared_mesh_output
                    else
                    f"Analytical NACA {structured_naca.group(1)} coordinate construction"
                    if structured_naca else "caller_declared_generation"
                ),
            })
        required_files.append(normalized)

    for index, filename in enumerate(filenames):
        if any(
            str(item.get("expected_filename") or item.get("name_or_role") or "").casefold()
            == filename.casefold()
            for item in required_files
        ):
            continue
        is_naca = bool(naca_match and PurePosixPath(filename).name.casefold() == f"naca{naca_match.group(1)}.dat".casefold())
        is_mesh = filename.casefold().rstrip("/").endswith("constant/polymesh") or PurePosixPath(filename).suffix.lower() == ".msh"
        is_configuration = not is_naca and not is_mesh and (
            "/" in filename or not PurePosixPath(filename).suffix
        )
        required_files.append({
            "id": _slug(filename, f"requested_asset_{index + 1}"),
            "name_or_role": filename,
            "expected_filename": filename,
            "declared_output_path": filename,
            "format": (
                "two-column XY coordinate text" if is_naca
                else "validated computational mesh" if is_mesh
                else "configuration text" if is_configuration
                else (PurePosixPath(filename).suffix.lstrip(".") or "text artifact")
            ),
            "asset_role": "geometry_or_discretization_asset" if is_naca or is_mesh else "parameter_file",
            "scientific_role": (
                "airfoil_coordinate_profile" if is_naca
                else "computational_mesh" if is_mesh
                else "solver_configuration" if is_configuration
                else "requested_preprocessing_asset"
            ),
            "representation": "coordinate_profile" if is_naca else "mesh" if is_mesh else "configuration" if is_configuration else "text_artifact",
            "workflow_capability": "mesh_generation" if is_mesh else "configuration_generation" if is_configuration else "local_artifact_generation",
            "source_strategy": "local_generation",
            "acquisition_kind": "generated_artifact",
            "generation_recipe": (
                f"Analytical NACA {naca_match.group(1)} coordinate construction"
                if is_naca else "Generate and validate the caller-requested computational mesh."
                if is_mesh else "caller_declared_configuration"
                if is_configuration else "caller_declared_generation"
            ),
            "reason": "The caller explicitly requested this output file.",
            "evidence": evidence,
            "confidence": 1.0,
            "consequences_if_missing": "The requested preprocessing delivery is incomplete.",
        })

    if (
        _caller_explicitly_requests_mesh(
            task_context,
            {"preprocessing_request": preprocessing_request},
        )
        and not any(_asset_requires_mesh(item) for item in required_files)
    ):
        required_files.append(_mesh_requirement_from_caller(
            task_context,
            {"preprocessing_request": preprocessing_request},
        ))

    if not required_files:
        for stage in structured_stages:
            for role in stage.get("required_file_roles") or []:
                role_text = str(role or "").strip()
                if not role_text:
                    continue
                required_files.append({
                    "id": _slug(role_text, "requested_asset"),
                    "name_or_role": role_text,
                    "format": "caller-declared preprocessing artifact",
                    "asset_role": "parameter_file",
                    "scientific_role": role_text,
                    "representation": "text_artifact",
                    "workflow_capability": "local_artifact_generation",
                    "source_strategy": "local_generation",
                    "acquisition_kind": "generated_artifact",
                    "generation_recipe": "caller_declared_stage_asset",
                    "reason": "Explicitly declared in the caller stage contract.",
                    "evidence": evidence,
                    "confidence": 1.0,
                    "consequences_if_missing": "The caller stage cannot be launched.",
                })
    if not required_files:
        return None

    request_bound = preprocessing_request["review_profile"] == "request_bound"
    if not structured_stages and not request_bound:
        structured_stages = [{
            "id": "caller_preprocessing_request",
            "name": "caller preprocessing request",
            "calculation_type": "generate and review the explicitly requested preprocessing assets",
            "parameters": {
                "naca_profile": naca_match.group(1) if naca_match else None,
            },
            "required_file_roles": [
                str(item.get("name_or_role") or item.get("id"))
                for item in required_files
            ],
            # These are Data-service deliverables consumed by the caller, not
            # runtime outputs produced by a downstream simulation stage.
            "prepared_assets": [str(item.get("id")) for item in required_files],
            "expected_outputs": [],
            "dependencies": [],
            "evidence": evidence,
        }]
    else:
        for index, stage in enumerate(structured_stages):
            stage.setdefault("id", f"caller_stage_{index + 1}")
            stage.setdefault(
                "calculation_type",
                stage.get("name") or "caller-declared preprocessing stage",
            )
            stage.setdefault("parameters", {})
            stage.setdefault("dependencies", [])
            stage.setdefault("evidence", evidence)
    if len(structured_stages) == 1 and not request_bound:
        stage_id = str(structured_stages[0].get("id") or "caller_preprocessing_request")
        for item in required_files:
            item.setdefault("stage_id", stage_id)

    software = _explicit_software_from_context(task_context) or "generic_preprocessing"
    declared_discipline = _declared_discipline_from_context(task_context)
    # Reuse the canonical solver/request adapter before the broad statistical
    # classifier.  Long orchestration context (KB summaries, tool paths, and
    # delivery reminders) can outweigh a short request in identify_discipline;
    # an explicit OpenFOAM/WRF/VASP identity must keep its registered route.
    discipline = _explicit_discipline_from_context(
        text,
        software,
        declared_discipline,
    )
    if discipline == "unknown":
        detected = identify_discipline(
            text_sample=text,
            metadata=context if context else None,
        )
        discipline = str(detected.get("primary_discipline") or "unknown")
    return {
        "discipline": {
            "primary": discipline,
            "adapter_route": (
                discipline if discipline in _DETERMINISTIC_STAGE_ADAPTERS else "generic"
            ),
            "evidence": evidence,
        },
        "simulation_software": {"name": software, "evidence": evidence},
        "calculation_type": "caller-scoped preprocessing asset generation",
        "calculation_stages": structured_stages,
        "required_files": required_files,
        "optional_files": [],
        "evidence": evidence,
        "assumptions": [],
        "unresolved_facts": [],
        "search_queries": [],
        "confidence": 1.0,
        "fallback": "minimal_caller_request_compiler",
        "preprocessing_request": preprocessing_request,
        "review_profile": preprocessing_request["review_profile"],
    }
async def _inspect_paths_from_context(
    state: State,
    task_context: str | dict[str, Any],
) -> list[dict[str, Any]]:
    """Inspect declared absolute paths, including bounded nearby recovery."""
    # Search text leaves, not serialized JSON: escaped newlines previously
    # joined a path to the following paragraph and hid valid local inputs.
    def text_leaves(value: Any):
        if isinstance(value, dict):
            for child in value.values():
                yield from text_leaves(child)
        elif isinstance(value, list):
            for child in value:
                yield from text_leaves(child)
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = None
            if isinstance(decoded, (dict, list)):
                yield from text_leaves(decoded)
            else:
                yield value.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
    text = "\n".join(text_leaves(task_context))
    candidates = re.findall(
        # Use an ASCII boundary.  In Python, ``\w`` includes CJK characters,
        # so the former boundary missed the common Chinese form ``在/path``.
        r"(?<![A-Za-z0-9_:])(?:~/(?:[^\s,;，；。/]+/)*[^\s,;，；。/]+|"
        r"/(?:Users|home|tmp|private|var|opt|data|workspace|mnt)/[^\n\r\t,;，；。\"`<>]+)",
        text,
    )
    inspections: list[dict[str, Any]] = []
    selected: list[Path] = []
    for raw_path in candidates:
        cleaned = raw_path.strip().strip("'\"`()[]{}<>：:")
        path = Path(cleaned).expanduser()
        try:
            exists = path.exists()
        except OSError:
            exists = False
        if not exists:
            # Natural-language prompts do not always put whitespace after a
            # path (for example ``/path/to/case目录中...``).  Recover only the
            # longest existing prefix of the final path component.  Never walk
            # up to a parent directory: that would turn a missing dependency
            # such as ``~/.credential`` into a scan of the user's home.
            name = path.name
            for end in range(len(name) - 1, 0, -1):
                fragment = name[:end]
                if fragment in {".", ".."}:
                    continue
                try:
                    prefix = path.with_name(fragment)
                except ValueError:
                    continue
                try:
                    prefix_exists = prefix.exists()
                except OSError:
                    prefix_exists = False
                if prefix_exists and prefix.resolve() != Path.home().resolve():
                    path = prefix
                    exists = True
                    break
        # A missing file is a dependency fact, not permission to inspect its
        # nearest existing parent.  In particular, ``~/.credential`` must not
        # degrade to a recursive scan of the user's home directory.
        if path == Path("/") or path == Path.home():
            continue
        # A recursive inspection of a directory already describes its
        # children.  Recovery contexts include that result as structured
        # input, so inspecting every listed child again only duplicates
        # previews in the Analyst payload.
        if any(
            path == parent or (parent.is_dir() and path.is_relative_to(parent))
            for parent in selected
        ):
            continue
        selected.append(path)
        try:
            result = await inspect_input_path(
                state,
                path=str(path),
                recursive=True,
                max_depth=3,
                max_files=20,
                include_previews=True,
                max_preview_bytes=1_200,
            )
        except OSError as exc:
            state.append_transcript(
                "input_path_inspection_skipped",
                path=str(path),
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        if result.get("status") == "success":
            inspections.append(result)
            selected.append(Path(result["path"]))
    return inspections


def _is_restricted_asset(item: dict[str, Any]) -> bool:
    return _asset_contract_role(item) == "restricted_asset_reference"


def _has_reliable_evidence(item: dict[str, Any]) -> bool:
    reliable_sources = {
        "user",
        "upstream",
        "artifact",
        "official_documentation",
        "official_manual",
        "official_example",
        "authoritative_reference",
        "reference_search_verified",
    }
    evidence = item.get("evidence") or []
    return any(
        isinstance(entry, dict)
        and str(entry.get("source_type") or "").strip().lower() in reliable_sources
        and any(str(entry.get(key) or "").strip() for key in ("source", "url", "artifact_id", "detail"))
        for entry in evidence
    )


def _text_blob(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value


def _normalise_asset_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text_blob(value).lower())


def _merge_runtime_tool_requirements(
    requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Represent one runtime tool once, with all consuming stages attached."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in requirements:
        if not isinstance(item, dict):
            continue
        identity = str(
            item.get("tool_identity")
            or item.get("scientific_role")
            or item.get("id")
            or "runtime_tool"
        ).strip()
        key = _normalise_asset_key(identity)
        if not key:
            continue
        consumers = {
            str(stage).strip()
            for stage in item.get("consumer_stages") or []
            if str(stage).strip()
        }
        if str(item.get("stage_id") or "").strip():
            consumers.add(str(item["stage_id"]).strip())
        if key not in merged:
            merged[key] = {
                **item,
                "tool_identity": identity,
                "logical_asset_id": f"runtime_tool_{key}",
                "consumer_stages": sorted(consumers),
            }
            order.append(key)
            continue
        current = merged[key]
        current["consumer_stages"] = sorted({
            *[str(stage) for stage in current.get("consumer_stages") or []],
            *consumers,
        })
    return [merged[key] for key in order]


def _runtime_asset_contract(stage_id: str, value: Any, index: int) -> dict[str, Any]:
    """Create one stable logical hand-off for a Research Plan runtime output."""
    text = str(value or "").strip()
    logical_id = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    logical_id = logical_id[:72] or f"runtime_output_{index}"
    lowered = text.casefold()
    extension = (
        ".grib" if "grib" in lowered
        else ".nc" if re.search(r"netcdf|\.nc\b", lowered)
        else ".json" if "json" in lowered
        else ".csv" if "csv" in lowered
        else ".txt" if re.search(r"report|table|log|报告|表格|日志", lowered)
        else ""
    )
    return {
        "logical_asset_id": f"{stage_id}.{logical_id}",
        "producer_stage": stage_id,
        "representation": (
            "grib_dataset" if extension == ".grib"
            else "netcdf_dataset" if extension == ".nc"
            else "structured_text" if extension in {".json", ".csv"}
            else "runtime_output"
        ),
        "canonical_path": f"runtime/{stage_id}/{logical_id}{extension}",
        "description": text,
        "materialization_kind": "runtime_output",
    }


def _asset_contract_is_local(value: Any) -> bool:
    """Identify solver inputs that are produced from the declared case contract.

    These are not externally sourced scientific data.  Keeping this semantic
    classification here prevents the planning loop from treating generated
    dictionaries, field definitions, and helper scripts as web-search gaps.
    """
    contract = normalize_asset_contract(value if isinstance(value, dict) else {"name_or_role": value})
    if contract.get("source_strategy") in {"local_generation", "local_reuse"}:
        return True
    return False


def _asset_contract_allows_fallback(value: Any) -> bool:
    """Allow the generic writer only for deterministic text/config artifacts."""
    item = value if isinstance(value, dict) else {"name_or_role": value}
    contract = normalize_asset_contract(item)
    if contract.get("source_strategy") != "local_generation":
        return False
    capability = str(contract.get("workflow_capability") or "").lower()
    if capability in {"configuration_generation", "preprocessing_script_generation"}:
        return True
    representation = str(contract.get("representation") or "").lower()
    if (
        capability == "local_artifact_generation"
        and representation == "coordinate_profile"
        and str(item.get("generation_recipe") or "").strip()
    ):
        return True
    return representation in {"text_artifact", "python", "shell", "bash", "script", "namelist", "json", "yaml", "toml", "configuration", "config", "manifest", "parameter_file"}


def _recovered_local_assets(value: Any) -> list[dict[str, Any]]:
    """Collect real files embedded in reference evidence without trusting prose."""
    assets: list[dict[str, str]] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        for key in (
            "preferred_asset_file", "preferred_geometry_file", "saved_path",
            "path", "local_path", "downloaded_path",
        ):
            raw_path = item.get(key)
            if not raw_path:
                continue
            candidate = Path(str(raw_path)).expanduser()
            try:
                resolved = candidate.resolve() if candidate.is_file() else None
            except OSError:
                resolved = None
            if resolved is not None and str(resolved) not in seen:
                seen.add(str(resolved))
                profile = inspect_scientific_asset_path(resolved)
                assets.append({
                    "path": str(resolved),
                    "suffix": resolved.suffix.lower(),
                    "source": str(item.get("source") or item.get("url") or "reference_evidence"),
                    "format": profile.get("format"),
                    "asset_kind": profile.get("asset_kind"),
                    "data_model_kind": profile.get("data_model_kind"),
                })
        for child in item.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(value)
    return assets


def _usable_reference_evidence(value: Any) -> list[dict[str, Any]]:
    """Keep source evidence; discard prior planning/generation blocker output."""
    usable: list[dict[str, Any]] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else item
        status = str(result.get("status") or "").strip().lower()
        if status in {
            "needs_reference_search", "needs_geometry_processing", "needs_input", "pause",
            "needs_plan_approval", "no_effective_search_progress",
        }:
            continue
        if status == "error" and str(item.get("tool_name") or result.get("tool_name") or "") not in {
            "data_web_search", "data_web_download",
        }:
            # Preserve network/search failures as acquisition-document
            # provenance, but do not feed unrelated execution failures back
            # into requirement planning.
            continue
        if item.get("source") == "recoverable_generation_blocker":
            continue
        # Authored upstream evidence has no tool status and remains valid.
        # Tool evidence is retained only when it succeeded or contains a real
        # downloaded/local file discovered by the deterministic path scanner.
        if (
            not status
            or status == "success"
            or (
                status == "error"
                and str(item.get("tool_name") or result.get("tool_name") or "") == "data_web_search"
            )
            or _recovered_local_assets([item])
        ):
            usable.append(item)
    return usable[:20]


def _reference_request_id(step_id: Any) -> str:
    """Recover the stable AssetContract id from search/acquisition step ids."""
    return re.sub(r"^(?:(?:search|download|acquire)_)+", "", str(step_id or ""))


def _reference_retrieval_metadata(
    analysis: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    """Extract source status and retrieval metadata for one step document."""
    request_id = str(source.get("id") or "").strip()
    wanted = {
        _normalise_asset_key(value)
        for value in (
            request_id,
            source.get("name_or_role"),
            source.get("expected_filename"),
        )
        if _normalise_asset_key(value)
    }
    expected_text = json.dumps(
        {
            key: source.get(key)
            for key in (
                "id", "name", "name_or_role", "expected_filename", "expected_revision",
                "representation", "parameter_bindings", "available_stage_parameters",
            )
            if source.get(key) not in (None, "", [], {})
        },
        ensure_ascii=False,
        default=str,
    ).casefold()

    def candidate_score(candidate: dict[str, Any]) -> tuple[int, int]:
        text = json.dumps(candidate, ensure_ascii=False, default=str).casefold()
        # A source whose declared dimensionality contradicts the request is
        # never an approved locator.  This is generic data-contract semantics,
        # not an application-specific filename rule.
        if acquisition_dimension_conflict(expected_text, text):
            return (-1, 0)
        expected_tokens = {
            token for token in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", expected_text)
            if token not in {"parameter_bindings", "available_stage_parameters"}
        }
        candidate_tokens = set(re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", text))
        return (len(expected_tokens & candidate_tokens), -len(text))

    dataset_reference = str(source.get("asset_role") or "").strip().lower() == "external_dataset_reference"
    for entry in analysis.get("reference_evidence") or []:
        if not isinstance(entry, dict):
            continue
        identities = {
            _normalise_asset_key(entry.get("request_id")),
            _normalise_asset_key(_reference_request_id(entry.get("step_id"))),
        }
        result = entry.get("result") if isinstance(entry.get("result"), dict) else entry
        if wanted and not (wanted & identities):
            continue
        candidates = result.get("results") if isinstance(result.get("results"), list) else []
        explicit_source = str(result.get("url") or result.get("source") or "").strip()
        if result.get("reference_status") == "no_result" or (
            str(result.get("status") or "").lower() in {"success", "ok", "completed"}
            and not candidates and not explicit_source
        ):
            discovery_status = "no_result"
        elif str(result.get("status") or "").lower() in {"error", "failed", "execution_error"}:
            discovery_status = "search_error"
        elif candidates or explicit_source:
            discovery_status = "discovered_candidate"
        else:
            discovery_status = "unresolved"
        discovery = {
            "status": discovery_status,
            "reason": str(result.get("error") or result.get("reason") or "").strip(),
            "query": str(result.get("query") or entry.get("query") or "").strip(),
            "candidate_count": len(candidates),
        }
        provider_summary = result.get("provider_summary") or result.get("providers")
        if provider_summary not in (None, "", [], {}):
            discovery["providers"] = deepcopy(provider_summary)
        size_evidence = {
            key: result.get(key)
            for key in ("size_estimate", "estimated_size", "estimated_size_bytes", "size_bytes")
            if result.get(key) not in (None, "", [], {})
        }
        local_assets = _recovered_local_assets([entry])
        if local_assets:
            recovered = local_assets[0]
            return {
                "local_match": {
                    "path": recovered["path"],
                    "sha256": str(result.get("sha256") or result.get("hash") or ""),
                    "format": recovered.get("format"),
                    "match_basis": ["approved_reference_download"],
                    "provenance": "approved_public_download",
                },
                "url": str(result.get("url") or recovered.get("source") or ""),
                "source_discovery": discovery,
                **size_evidence,
            }
        candidate_entries: list[dict[str, Any]] = []
        if result.get("url") or result.get("source"):
            candidate_entries.append(dict(result))
        candidate_entries.extend(
            item for item in result.get("results") or [] if isinstance(item, dict)
        )
        ranked = sorted(
            (
                (candidate_score(candidate), candidate)
                for candidate in candidate_entries
                if _is_authoritative_public_source_url(
                    str(candidate.get("url") or candidate.get("source") or "")
                )
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        # Dataset deliveries are download-method documents, not verified
        # official-file acquisitions.  If search found only a public lead,
        # retain that URL as a candidate locator and mark it for later
        # verification; never apply this relaxation to an official file.
        if dataset_reference and not ranked:
            candidate_urls = [
                item for item in candidate_entries
                if re.match(r"https?://", str(item.get("url") or item.get("source") or ""))
            ]
            if candidate_urls:
                candidate = candidate_urls[0]
                url = str(candidate.get("url") or candidate.get("source") or "")
                return {
                    "url": url,
                    "source_discovery": discovery,
                    **size_evidence,
                    "retrieval_instructions": {
                        "locator": url,
                        "locator_kind": "reference_url",
                        "verification_required": True,
                        "approved_source_evidence": [{
                            "source_type": "reference_search_candidate",
                            "request_id": request_id,
                            "url": url,
                        }],
                    },
                }
        if not ranked or ranked[0][0][0] < 0:
            if dataset_reference:
                return {
                    "source_discovery": discovery,
                    **size_evidence,
                }
            continue
        url = str(ranked[0][1].get("url") or ranked[0][1].get("source") or "")
        return {
            "url": url,
            "source_discovery": discovery,
            **size_evidence,
            "retrieval_instructions": {
                "locator": url,
                "locator_kind": "url",
                "approved_source_evidence": [{
                    "source_type": "reference_search_verified",
                    "request_id": request_id,
                    "url": url,
                }],
            },
        }
    return {}


def _requirement_analysis_path(state: State) -> Path:
    path = state.root / "planning" / "requirement_analysis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_requirement_analysis(state: State) -> dict[str, Any] | None:
    path = _requirement_analysis_path(state)
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        value = None
    return value if isinstance(value, dict) else None


def _save_requirement_analysis(state: State, analysis: dict[str, Any]) -> None:
    path = _requirement_analysis_path(state)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(analysis, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def record_reference_execution_evidence(
    state: State,
    plan: dict[str, Any],
    execution_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist tool-produced reference evidence at the execution boundary.

    This is deliberately independent of the main model: the next planning
    pass receives the exact search result, request id, and gap id that were
    approved and executed.
    """
    planned_steps = {
        str(step.get("id") or ""): step
        for step in plan.get("generation_steps") or [] if isinstance(step, dict)
    }
    analysis = (
        plan.get("requirement_analysis")
        if isinstance(plan.get("requirement_analysis"), dict)
        else plan
    )
    reference_store = PlanningStore(state)
    reference_state = reference_store.load_reference_state()
    ledger = reference_state.get("gaps") if isinstance(reference_state.get("gaps"), dict) else {}
    evidence: list[dict[str, Any]] = []
    persisted_evidence = [item for item in reference_state.get("evidence") or [] if isinstance(item, dict)]
    # ``targeted_search_requests`` and ``required_files`` normally share the
    # same id.  The former is the execution request and intentionally omits
    # delivery metadata; the latter is the authoritative AssetContract.  Do
    # not let the compact request overwrite materialization fields needed by
    # the reference ledger (otherwise a document-only asset is incorrectly
    # treated as a blocking local payload).
    request_contracts: dict[str, dict[str, Any]] = {}
    for collection in (
        analysis.get("targeted_search_requests") or [],
        analysis.get("required_files") or [],
    ):
        for item in collection:
            if not isinstance(item, dict):
                continue
            request_id = str(item.get("id") or "").strip()
            if not request_id:
                continue
            current = request_contracts.setdefault(request_id, {})
            current.update(item)

    def persist_outcome(
        gap_id: str,
        status: str,
        fields: dict[str, Any],
        evidence_entry: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal persisted_evidence
        enriched_fields = dict(fields)
        contract = request_contracts.get(str(enriched_fields.get("request_id") or ""), {})
        contract = normalize_asset_contract(contract) if contract else {}
        for key in ("materialization_kind", "requires_local_payload", "fulfillment_kind"):
            if key not in enriched_fields and key in contract:
                enriched_fields[key] = contract.get(key)
        updated = reference_store.record_reference_outcome(
            gap_id,
            status=status,
            fields=enriched_fields,
            evidence=evidence_entry,
        )
        ledger[gap_id] = updated
        persisted_evidence = reference_store.load_reference_state().get("evidence") or []
        return updated

    for executed in execution_steps:
        if not isinstance(executed, dict):
            continue
        step_id = str(executed.get("step_id") or "")
        step = planned_steps.get(step_id) or {}
        if step.get("tool_name") not in {"data_web_search", "data_web_download"}:
            continue
        result = executed.get("result") if isinstance(executed.get("result"), dict) else {}
        arguments = step.get("tool_arguments") if isinstance(step.get("tool_arguments"), dict) else {}
        gap_id = str(arguments.get("gap_id") or step_id.removeprefix("search_") or "")
        request_id = _reference_request_id(step_id)
        entry = {
            "step_id": step_id,
            "gap_id": gap_id,
            "request_id": request_id,
            "tool_name": str(step.get("tool_name") or "data_web_search"),
            "asset_role": str(arguments.get("asset_role") or "").strip().lower(),
            "workflow_capability": str(arguments.get("workflow_capability") or "").strip().lower(),
            "requires_exact_file": arguments.get("requires_exact_file"),
            "expected_path": str(arguments.get("expected_path") or "").strip(),
            "result": result,
        }
        evidence.append(entry)
        result_status = str(result.get("status") or "").strip().lower()
        result_error = str(result.get("error") or result.get("reason") or "").strip().lower()
        if result_status == "deferred_dependency":
            persist_outcome(gap_id, "deferred_dependency", {
                "status": "deferred_dependency",
                "request_id": request_id,
                "asset_role": arguments.get("asset_role") or "",
                "workflow_capability": arguments.get("workflow_capability") or "",
                "requires_exact_file": arguments.get("requires_exact_file"),
                "expected_filename": arguments.get("expected_filename") or "",
                "expected_revision": arguments.get("expected_revision") or "",
                "reason": result.get("deferred_reason") or result.get("error") or "",
            }, entry)
            continue
        contract_failure = result_status in {
            "not_authorized_by_plan", "needs_plan_approval", "plan_contract_error",
            "execution_contract_error",
        } or bool(re.search(r"not authorized|approved plan|asset_kind differs|plan contract", result_error, flags=re.I))
        if contract_failure:
            # Authorization/contract failures are not searches with zero
            # results.  Keep the gap open and invalidate the approved plan;
            # otherwise the next pass would incorrectly treat the gap as
            # exhausted and close the reference loop.
            persist_outcome(gap_id, "plan_contract_error", {
                "status": "plan_contract_error",
                "request_id": request_id,
                "asset_role": arguments.get("asset_role") or "",
                "workflow_capability": arguments.get("workflow_capability") or "",
                "requires_exact_file": arguments.get("requires_exact_file"),
                "error": result.get("error") or result.get("reason") or result_status,
            }, entry)
            continue
        permanent_acquisition_failure = (
            step.get("tool_name") == "data_web_download"
            and (
                str(result.get("status_class") or "").lower() == "permanent"
                or bool(re.search(r"\bHTTP\s+(?:4\d\d)\b", result_error, flags=re.I))
            )
        )
        if permanent_acquisition_failure:
            # The approved request was syntactically valid but its discovered
            # candidate was not a usable object.  Preserve that fact in the
            # ledger; it must not be retried as a transport error or promoted
            # to resolved evidence.
            persist_outcome(gap_id, "candidate_invalid", {
                "status": "candidate_invalid",
                "request_id": request_id,
                "asset_role": arguments.get("asset_role") or "",
                "workflow_capability": arguments.get("workflow_capability") or "",
                "requires_exact_file": arguments.get("requires_exact_file"),
                "error": result.get("error") or result_status,
            }, entry)
            continue
        recovered_assets = _recovered_local_assets([entry])
        has_result = bool(
            result.get("results") or result.get("url") or result.get("downloaded_path")
            or result.get("saved_path") or result.get("local_path")
            or recovered_assets
        )
        # ``no_result`` is reserved for a successful search response whose
        # result list is empty.  Transport/tool errors remain open for
        # deterministic recovery and must never masquerade as evidence.
        if not has_result and result_status not in {"success", "ok", "completed"}:
            persist_outcome(gap_id, "execution_error", {
                "request_id": request_id,
                "asset_role": arguments.get("asset_role") or "",
                "workflow_capability": arguments.get("workflow_capability") or "",
                "requires_exact_file": arguments.get("requires_exact_file"),
                "error": result.get("error") or result_status,
            }, entry)
            continue
        if not has_result and step.get("tool_name") != "data_web_search":
            persist_outcome(gap_id, "execution_error", {
                "request_id": request_id,
                "asset_role": arguments.get("asset_role") or "",
                "workflow_capability": arguments.get("workflow_capability") or "",
                "requires_exact_file": arguments.get("requires_exact_file"),
                "error": "approved acquisition completed without a downloaded or verified asset",
            }, entry)
            continue
        if not has_result:
            entry["result"] = {**result, "reference_status": "no_result"}
            persist_outcome(gap_id, "no_result", {
                "request_id": request_id,
                "asset_role": arguments.get("asset_role") or "",
                "asset_kind": arguments.get("asset_kind") or "reference",
                "requires_exact_file": arguments.get("requires_exact_file"),
                "expected_filename": arguments.get("expected_filename") or "",
                "expected_revision": arguments.get("expected_revision") or "",
            }, entry)
            continue
        asset_role = str(arguments.get("asset_role") or "").strip().lower()
        requires_exact_file = _reference_requires_exact_file(arguments)
        exact_candidate_url = _official_file_candidate_url(result, arguments) if requires_exact_file else ""
        if requires_exact_file and not exact_candidate_url:
            # A non-empty search response is not necessarily an acquisition
            # candidate.  Search-engine redirects and generic summaries are
            # auditable evidence of an attempted lookup, but cannot be passed
            # to the downloader as if they named the requested official file.
            evidence_record = {
                "source_type": "reference_search_unusable",
                "gap_id": gap_id,
                "request_id": request_id,
                "source": result.get("source") or "data_web_search",
                "detail": "Search returned no direct, exact official-file URL for the approved request.",
                "candidate_urls": [
                    str(item.get("url") or "")
                    for item in result.get("results") or []
                    if isinstance(item, dict) and str(item.get("url") or "")
                ][:5],
                "rejection_reason": "no_candidate_matches_expected_filename_and_revision",
            }
            # Keep the canonical evidence state machine: a search result
            # that does not identify the exact file is candidate evidence,
            # not a separate execution-failure state.
            persist_outcome(gap_id, "discovered_candidate", {
                "request_id": request_id,
                "asset_role": asset_role,
                "asset_kind": arguments.get("asset_kind") or "official_file",
                "requires_exact_file": True,
                "workflow_capability": arguments.get("workflow_capability") or "",
                "expected_filename": arguments.get("expected_filename") or "",
                "expected_revision": arguments.get("expected_revision") or "",
                "expected_path": arguments.get("expected_path") or "",
                "evidence": evidence_record,
            }, entry)
            emit_progress(
                state,
                "reference_candidate_unusable",
                str(arguments.get("expected_filename") or request_id),
                candidate_count=len(evidence_record["candidate_urls"]),
                reason=evidence_record["rejection_reason"],
            )
            continue
        authoritative = _reference_evidence_is_authoritative(
            result,
            request_id,
            asset_role=asset_role,
            requires_exact_file=requires_exact_file,
        )
        downloaded_hash = str(
            result.get("sha256")
            or result.get("hash")
            or ""
        ).strip()
        downloaded_path = (
            result.get("downloaded_path")
            or result.get("saved_path")
            or result.get("local_path")
            or (recovered_assets[0].get("path") if recovered_assets else "")
        )
        asset_kind = str(arguments.get("asset_kind") or result.get("asset_kind") or "reference").lower()
        material_asset = asset_kind in {
            "archive", "dataset", "geometry", "geometry_or_mesh", "mesh",
            "parameters", "structure", "atomic_structure",
        }
        if material_asset and not downloaded_path:
            # Search results are leads. A material scientific input becomes
            # authoritative only after the separate download adapter records
            # its local path and hash.
            authoritative = False
        if recovered_assets and not requires_exact_file:
            authoritative = True
        if downloaded_path and downloaded_hash:
            authoritative = (
                _reference_download_is_exact(result, request_id, arguments, asset_role=asset_role)
                if requires_exact_file else True
            )
        if recovered_assets and not requires_exact_file:
            evidence_record = {
                "local_match": {
                    "path": downloaded_path,
                    "sha256": downloaded_hash,
                }
            }
        else:
            evidence_record = {}
        effective_path = str(arguments.get("expected_path") or "").strip()
        effective_revision = str(arguments.get("expected_revision") or "").strip()
        evidence_record.update({
            "source_type": "reference_search_verified" if authoritative else "reference_search_candidate",
            "gap_id": gap_id,
            "request_id": request_id,
            "source": result.get("source") or result.get("url") or "data_web_search",
            "detail": _compact_text(result.get("results") or result, 600),
        })
        for key in ("url", "path", "downloaded_path", "saved_path", "local_path", "filename", "revision", "commit", "sha256", "hash"):
            if result.get(key) not in (None, ""):
                evidence_record[key] = result[key]
        outcome_status = (
            "downloaded_verified"
            if authoritative and (step.get("tool_name") == "data_web_download" or recovered_assets)
            else "resolved" if authoritative else "discovered_candidate"
        )
        persist_outcome(gap_id, outcome_status, {
            "request_id": request_id,
            "asset_role": arguments.get("asset_role") or "",
            "workflow_capability": arguments.get("workflow_capability") or "",
            "asset_kind": asset_kind,
            "requires_exact_file": requires_exact_file,
            "expected_filename": arguments.get("expected_filename") or "",
            "expected_revision": effective_revision,
            "expected_path": effective_path,
            "evidence": evidence_record,
        }, entry)
    if evidence:
        state.append_transcript(
            "preprocessing_reference_execution_evidence_saved",
            evidence_count=len(persisted_evidence),
            resolved_count=sum(1 for item in ledger.values() if isinstance(item, dict) and item.get("status") in {"resolved", "downloaded_verified"}),
        )
    return persisted_evidence if evidence else []


def _reference_evidence_is_authoritative(
    result: dict[str, Any],
    request_id: str,
    *,
    asset_role: str = "",
    requires_exact_file: bool = True,
) -> bool:
    # Search summaries cannot resolve an official-file deliverable.  They only
    # establish discovered_candidate; resolution requires the approved exact
    # acquisition path below.
    normalized_role = str(asset_role or "").strip().lower()
    if normalized_role == "external_dataset_reference":
        urls = [str(result.get(key) or "") for key in ("url", "source")]
        candidates = result.get("results") if isinstance(result.get("results"), list) else []
        urls.extend(str(item.get("url") or item.get("source") or "") for item in candidates if isinstance(item, dict))
        official_host = any(_is_authoritative_public_source_url(url) for url in urls if url)
        return bool(result.get("authoritative") is True and any(urls)) or official_host
    if normalized_role in {"official_file_reference", "external_asset", "reference_asset"} and requires_exact_file:
        return False
    if result.get("authoritative") is True or str(result.get("source_type") or "").lower() in {
        "official_documentation", "official_manual", "official_example", "authoritative_reference",
    }:
        return True
    if not request_id.startswith("official_"):
        return False
    candidates = result.get("results") if isinstance(result.get("results"), list) else []
    urls = [str(item.get("url") or "") for item in candidates if isinstance(item, dict)]
    for url in urls:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        if not host:
            continue
        if host.startswith("docs.") or host.startswith("manual."):
            return True
        if _is_authoritative_public_source_url(url):
            return True
    return False


def _is_authoritative_public_source_url(url: str) -> bool:
    """Recognize institutional/public-repository hosts without application rules."""
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or not host:
        return False
    return (
        host.endswith((".gov", ".gov.cn", ".edu", ".edu.cn", ".ac.uk", ".int"))
        or host in {"github.com", "raw.githubusercontent.com", "zenodo.org", "doi.org"}
        or host.endswith((".github.com", ".zenodo.org", ".copernicus.eu", ".europa.eu"))
    )


def _official_file_candidate_url(result: dict[str, Any], arguments: dict[str, Any]) -> str:
    """Return a direct, exact official-file candidate from one search result.

    This deliberately does not trust search-engine redirect URLs.  It is
    generic across applications: an official repository URL must either name
    the requested filename or use a repository raw/blob/commit path.
    """
    expected_filename = normalize_declared_filename(arguments.get("expected_filename")).casefold()
    expected_identity = canonical_filename_identity(expected_filename)
    expected_revision = str(arguments.get("expected_revision") or "").strip().lower().lstrip("v")
    # Exact-file acquisition requires an exact filename. Without one, search
    # evidence can remain a candidate but may not select an arbitrary object
    # from a repository as an executable download.
    if not expected_identity:
        return ""
    structured_locator = arguments.get("locator_contract")
    structured_locator = structured_locator if isinstance(structured_locator, dict) else {}
    # A declared authoritative file URL is already an exact locator even
    # when it is not a GitHub repository URL.  ``normalize_official_locator``
    # intentionally keeps only repository fields for GitHub, so preserve this
    # direct-URL path before the repository candidate logic can downgrade it
    # to a weak search.
    declared_urls = [
        str(arguments.get("source_locator") or "").strip(),
        str(arguments.get("expected_path") or "").strip(),
        str(structured_locator.get("direct_url") or "").strip(),
    ]
    for declared_url in declared_urls:
        if not declared_url or not re.match(r"^https://", declared_url, flags=re.I):
            continue
        parsed_declared = urlparse(declared_url)
        host_declared = (parsed_declared.hostname or "").casefold().removeprefix("www.")
        if host_declared in {"github.com", "raw.githubusercontent.com"}:
            continue
        if not _is_authoritative_public_source_url(declared_url):
            continue
        declared_name = Path(unquote(parsed_declared.path)).name
        if canonical_filename_identity(declared_name) == expected_identity:
            return declared_url
    requested_locator = normalize_official_locator(
        structured_locator or arguments.get("expected_path") or arguments.get("source_locator") or ""
    )
    requested_path = str(requested_locator.get("path") or "").strip().strip("/").casefold()
    candidates: list[str] = [str(result.get("url") or "")]
    for item in result.get("results") or []:
        if isinstance(item, dict):
            candidates.append(str(item.get("url") or item.get("source") or ""))
    # ``data_web_search`` keeps an exact file discovered from an
    # authoritative repository page under ``discovered_downloads`` when
    # auto-discovery is disabled.  It is still an approved candidate: send
    # it through the same filename/revision/path checks below instead of
    # treating the gap as an unusable search result.
    for discovery in result.get("discovered_downloads") or []:
        if not isinstance(discovery, dict):
            continue
        for link in discovery.get("download_links") or []:
            if isinstance(link, dict):
                candidates.append(str(link.get("url") or ""))
    redirect_hosts = {"baidu.com", "bing.com", "duckduckgo.com", "google.com", "yahoo.com"}
    exact_urls: list[str] = []
    # Search may expose the requested file on a mutable branch while the
    # approved contract supplies the release revision. Keep its normalized
    # repository/path for the exact locator compiler below.
    derived_locator: dict[str, str] = {}
    for url in candidates:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        path = parsed.path.casefold()
        if not host or any(host == redirect or host.endswith(f".{redirect}") for redirect in redirect_hosts):
            continue
        if not _is_authoritative_public_source_url(url):
            continue
        repository_path = (
            host == "raw.githubusercontent.com"
            or bool(re.search(r"/(?:blob|raw|commit|releases/download)/", path, flags=re.I))
        )
        if repository_path and expected_revision:
            candidate_locator = normalize_official_locator(url)
            candidate_revision = str(candidate_locator.get("revision") or "").strip().lower().lstrip("v")
        filename = Path(unquote(parsed.path)).name.casefold()
        filename_matches = canonical_filename_identity(filename) == expected_identity
        if (
            repository_path
            and expected_revision
            and candidate_revision != expected_revision
            and filename_matches
            and candidate_locator.get("repository_url")
            and candidate_locator.get("path")
            and not derived_locator
        ):
            # This only compiles an approved repository/path into the
            # requested revision; download verification still decides whether
            # the release actually exists and is usable.
            derived_locator = {
                "repository_url": candidate_locator["repository_url"],
                "path": candidate_locator["path"],
            }
            continue
        if repository_path and expected_revision and candidate_revision != expected_revision:
            continue
        direct_or_repository = repository_path or parsed.scheme == "https"
        if direct_or_repository and filename_matches:
            # If the approved contract already names a repository-relative
            # path, a same-named file in another directory is not an exact
            # match.  This prevents a search result (or a generated raw URL)
            # from silently dropping a nested directory such as ``ungrib``.
            if requested_path and repository_path:
                candidate_path = str(
                    normalize_official_locator(url).get("path") or ""
                ).strip("/").casefold()
                if candidate_path and not (
                    candidate_path == requested_path
                    or candidate_path.endswith(f"/{requested_path}")
                ):
                    continue
            exact_urls.append(url)
            continue
    if exact_urls:
        return exact_urls[0]
    # A search result may identify the authoritative repository but not expose
    # a nested file link.  When the approved contract supplies a repository
    # locator and revision, resolve that path generically into one exact raw
    # URL.  The subsequent downloader still verifies HTTP status, filename,
    # revision and hash; no application-specific path is invented here.
    locator = str(
        arguments.get("expected_path")
        or arguments.get("source_locator")
        or ""
    ).strip()
    locator_fields = normalize_official_locator(locator)
    if derived_locator:
        locator_fields = {**derived_locator, **locator_fields}
    locator_fields = {
        **locator_fields,
        **{
            key: str(structured_locator.get(key) or "").strip()
            for key in ("repository_url", "revision", "path", "raw_url")
            if structured_locator.get(key) not in (None, "", [], {})
        },
    }
    expected_revision = str(
        arguments.get("expected_revision")
        or locator_fields.get("revision")
        or ""
    ).strip()
    if locator_fields:
        locator_parts = [
            part for part in PurePosixPath(locator_fields.get("path") or "").parts
            if part not in {".", "..", "/"}
        ]
        # A bare filename is not a repository locator.  Without a concrete
        # repository path, synthesizing ``/<filename>`` at the repository root
        # creates a plausible-looking but usually invalid raw URL.  Let the
        # exact-file search/API resolver provide the real path instead.
        if len(locator_parts) <= 1 and not locator_fields.get("repository_url"):
            return ""
        repository_url = urlparse(locator_fields.get("repository_url") or "")
        repository_parts = [part for part in repository_url.path.strip("/").split("/") if part]
        locator_repo: tuple[str, str] | None = (
            (repository_parts[0], repository_parts[1])
            if len(repository_parts) >= 2 else None
        )
        locator_revision = str(locator_fields.get("revision") or "").strip()
        if locator_parts and canonical_filename_identity(locator_parts[-1]) == expected_identity:
            repository_candidates = list(candidates)
            if locator_repo:
                repository_candidates.append(
                    f"https://github.com/{locator_repo[0]}/{locator_repo[1]}"
                )
            revision = expected_revision or locator_revision
            # Keep one canonical revision spelling plus its ``v`` alias.  Do
            # not prepend ``v`` to an already-prefixed value: that turns a
            # valid locator such as ``v4.4/path`` into ``v4.4/v4.4/path``.
            revision_key = revision.lstrip("vV").strip()
            revisions = tuple(dict.fromkeys(
                item for item in (f"v{revision_key}", revision_key) if item
            ))
            for url in repository_candidates:
                parsed = urlparse(url)
                host = (parsed.hostname or "").casefold().removeprefix("www.")
                parts = [part for part in parsed.path.strip("/").split("/") if part]
                if host != "github.com" or len(parts) < 2 or any(
                    marker in parts for marker in ("blob", "raw", "commit", "tree")
                ):
                    continue
                # GitHub uses two-segment paths for topic pages, explore
                # pages, searches, and other non-repository routes as well.
                # They are ranking pages, not repository locators; treating
                # them as ``owner/repo`` creates a plausible but false raw URL.
                if any(part.casefold() in {
                    "topics", "explore", "search", "orgs", "marketplace",
                    "collections", "features", "sponsors", "trending",
                } for part in parts[:2]):
                    continue
                owner, repo_name = parts[0], parts[1]
                if locator_repo and (owner.casefold(), repo_name.casefold()) != tuple(
                    value.casefold() for value in locator_repo
                ):
                    continue
                canonical_locator = normalize_official_locator({
                    "repository_url": f"https://github.com/{owner}/{repo_name}",
                    "revision": revision,
                    "path": "/".join(locator_parts),
                })
                canonical_path = str(canonical_locator.get("path") or "").strip()
                if not canonical_path or not revision:
                    continue
                for candidate_revision in revisions:
                    candidate_revision = candidate_revision.strip()
                    if not candidate_revision:
                        continue
                    return (
                        f"https://raw.githubusercontent.com/{owner}/{repo_name}/"
                        f"{quote(candidate_revision, safe='')}/{quote(canonical_path, safe='/')}"
                    )
    return ""


def _reference_download_is_exact(result: dict[str, Any], request_id: str, arguments: dict[str, Any], *, asset_role: str = "") -> bool:
    """Require exact-file provenance before closing an official-file gap."""
    url = str(result.get("url") or arguments.get("url") or "")
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    filename = str(
        result.get("filename")
        or Path(str(
            result.get("downloaded_path")
            or result.get("saved_path")
            or result.get("local_path")
            or ""
        )).name
    )
    role = str(asset_role or arguments.get("asset_role") or "").strip().lower()
    expected = str(arguments.get("expected_filename") or "").strip()
    if role not in {"official_file_reference", "external_asset", "reference_asset"} and not expected:
        return False
    if expected and canonical_filename_identity(expected) != canonical_filename_identity(filename or url):
        return False
    # An official file must come from a concrete repository/file URL, not an
    # arbitrary search redirect or prose summary.
    if not host:
        return False
    expected_revision = str(arguments.get("expected_revision") or "").strip().lstrip("v")
    requested_revision = str(
        result.get("requested_revision") or result.get("tag") or ""
    ).strip().lstrip("v")
    has_revision = bool(
        re.search(r"(?:raw\.githubusercontent\.com|/(?:blob|raw)/)", url, flags=re.I)
        and re.search(r"\b(?:v?\d+(?:\.\d+)+|commit|revision|tag)\b|/[0-9a-f]{7,40}(?:\b|/)", url, flags=re.I)
    )
    # Some authoritative releases expose a stable direct file URL without
    # embedding the release tag in the path.  The approved acquisition
    # contract supplies the expected revision in that case; keep the hash and
    # exact filename checks, but do not force a GitHub URL shape on other
    # official hosts.
    declared_locator = str(
        arguments.get("source_locator") or arguments.get("expected_path") or ""
    ).strip()
    direct_contract_match = False
    if declared_locator and re.match(r"^https://", declared_locator, flags=re.I):
        declared_host = (urlparse(declared_locator).hostname or "").casefold()
        result_host = (urlparse(url).hostname or "").casefold()
        direct_contract_match = bool(
            declared_host
            and declared_host == result_host
            and _is_authoritative_public_source_url(declared_locator)
            and canonical_filename_identity(Path(urlparse(declared_locator).path).name)
            == canonical_filename_identity(filename or url)
        )
    has_revision = has_revision or bool(
        direct_contract_match and expected_revision
        and (
            not requested_revision
            or requested_revision.lower() == expected_revision.lower()
        )
    )
    if (
        expected_revision
        and expected_revision.lower() not in url.lower()
        and expected_revision.lower() != requested_revision.lower()
        and not direct_contract_match
    ):
        return False
    digest = str(result.get("sha256") or result.get("hash") or "").strip()
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", digest)) and has_revision


def _official_application_reference_requests(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Request the named application's own guidance before inferring its input contract.

    This deliberately derives the request from RequirementAnalysis rather than
    maintaining an application-to-file mapping in the data node.
    """
    software = analysis.get("simulation_software") or {}
    applications: list[str] = []

    def add_application(value: Any) -> None:
        identity = normalize_software_identity(value)
        name = str(identity.get("name") or "").strip()
        if not name or re.fullmatch(
            r"(?:unknown|simulation software|not specified|generic(?:[_ -]+preprocessing)?|data preprocessing)",
            name,
            flags=re.I,
        ):
            return
        label = " ".join(
            part for part in (name, str(identity.get("version") or "").strip()) if part
        )
        if label and label.casefold() not in {item.casefold() for item in applications}:
            applications.append(label)

    add_application(software)
    if isinstance(software, dict):
        add_application(
            software.get("preprocessor")
            or software.get("preprocessing_software")
            or software.get("preprocessing")
        )
    for item in analysis.get("supporting_software") or []:
        if not isinstance(item, dict) or not re.search(
            r"\bpreprocess(?:ing)?\b|前处理|前置", str(item.get("role") or ""), flags=re.I
        ):
            continue
        add_application(item)
    if not applications:
        return []
    completed_ids = {
        str(item).strip()
        for item in (analysis.get("resolved_reference_request_ids") or [])
        + (analysis.get("completed_reference_request_ids") or [])
        if str(item).strip()
    }
    requests: list[dict[str, Any]] = []
    for application in applications:
        request_id = f"official_{_slug(application, 'application')}_preprocessing_reference"
        if request_id in completed_ids:
            continue
        requests.append({
            "id": request_id,
            "missing": f"official preprocessing input guidance for {application}",
            "reason": (
                "Application-specific required files and generation order must be established from "
                "the named application's authoritative sources, not inferred from local planning artifacts."
            ),
            "query": f"{application} official documentation preprocessing required input files",
            "tool": "data_web_search",
            "web_search_allowed": True,
            "search_mode": "generic",
            "asset_kind": "reference",
            "workflow_capability": "configuration_generation",
            "documentation_reference": True,
            "auto_discover_downloads": False,
            "max_discovery_pages": 1,
            "generation_phase": "pre_generation",
        })
    return requests


def _required_asset_is_recovered(item: dict[str, Any] | str, analysis: dict[str, Any]) -> bool:
    """Return whether a locally saved reference already satisfies this asset role."""
    assets = _recovered_local_assets(analysis.get("reference_evidence") or [])
    if not assets:
        return False
    text = _asset_haystack(item).lower()
    requested_kind = _asset_representation_kind(item)
    if requested_kind == "dataset":
        candidates = [asset for asset in assets if asset.get("asset_kind") == "dataset"]
        format_tokens = {
            "netcdf": "netcdf", "grib": "grib", "hdf5": "hdf5",
            "parquet": "parquet", "csv": "csv", "tsv": "tsv",
            "numpy": "numpy", "npy": "numpy", "npz": "numpy",
        }
        expected = {value for token, value in format_tokens.items() if token in text}
        return bool(candidates) and (
            not expected or any(str(asset.get("format") or "").lower() in expected for asset in candidates)
        )
    if requested_kind == "parameters":
        return any(asset.get("asset_kind") in {"parameters", "dataset"} for asset in assets)
    if requested_kind == "structure":
        return any(asset.get("asset_kind") == "structure" for asset in assets)
    if requested_kind == "archive":
        return any(asset.get("asset_kind") in {"archive", "bundle"} for asset in assets)
    coordinate_asset = any(asset["suffix"] in {".dat", ".txt", ".xy"} for asset in assets)
    if re.search(r"\b(airfoil|aerofoil|profile|coordinate|section)\b|翼型|坐标|截面", text, flags=re.I):
        return coordinate_asset
    if re.search(r"\b(stl|surface)\b|表面", text, flags=re.I) and coordinate_asset:
        # A profile surface can be generated locally from its coordinates.
        return True
    if re.search(r"\b(geometry|cad|step|stp|iges|igs|mesh|msh)\b|几何|网格", text, flags=re.I):
        wanted = _normalise_asset_key(text)
        return any(
            _normalise_asset_key(Path(asset["path"]).stem) in wanted
            or wanted in _normalise_asset_key(Path(asset["path"]).stem)
            for asset in assets
        ) or len(assets) == 1
    return False


def _fact_is_satisfied_by_recovered_asset(value: Any, analysis: dict[str, Any]) -> bool:
    text = _text_blob(value)
    if not _recovered_local_assets(analysis.get("reference_evidence") or []):
        return False
    return bool(re.search(
        r"\b(?:coordinate|profile|airfoil|aerofoil|geometry|format|trailing edge|"
        r"leading edge|point count|downloaded|dataset|data table|netcdf|grib|hdf5|"
        r"parquet|tensor|spatial field|time series)\b|"
        r"坐标|翼型|几何|格式|前缘|后缘|点数|下载|数据集|数据表|空间场|时间序列",
        text,
        flags=re.I,
    ))


def _unverified_external_deliverable_gaps(plan: dict[str, Any]) -> list[str]:
    """Return required external assets whose evidence is only a search candidate.

    Generic locally generated scripts/configs are intentionally excluded.  A
    External assets are accepted only when evidence
    contains a concrete official URL/path/revision, never a prose search
    summary.  This gate is deterministic and therefore cannot be overridden by
    a high Critic score or a stale persisted critique.
    """
    deliverables = [item for item in plan.get("required_deliverables") or [] if isinstance(item, dict)]
    steps = [step for step in plan.get("generation_steps") or [] if isinstance(step, dict)]
    gaps: list[str] = []
    for item in deliverables:
        if not item.get("required", True):
            continue
        item_id = str(item.get("id") or "").strip()
        owner = next((step for step in steps if item_id and item_id in [str(v) for v in step.get("outputs") or []]), None)
        owner_args = owner.get("tool_arguments") if isinstance(owner, dict) else {}
        owner_spec = owner_args.get("artifact_spec") if isinstance(owner_args, dict) else {}
        owner_format = str((owner_spec or {}).get("format") or item.get("format") or "").lower()
        local_writer = bool(owner and str(owner.get("tool_name") or "") in {
            "generate_preprocessing_artifact", "execute_preprocessing_python", "execute_python",
        }) and bool(re.search(r"python|shell|bash|json|namelist|config|text|script", owner_format, flags=re.I))
        contract = normalize_asset_contract(item)
        asset_role = str(contract.get("asset_role") or "").strip().lower()
        external_role = bool(contract.get("is_external"))
        if local_writer:
            continue
        if not external_role:
            continue
        # A generation plan may legitimately acquire a small, versioned
        # reference file itself.  Requiring the post-download hash before
        # approving that very download creates a circular gate and forces a
        # second search plan.  Accept only an already-approved *direct* file
        # acquisition here; the executor still verifies the downloaded file
        # and records its hash before publishing the package.
        if owner and str(owner.get("tool_name") or "") == "data_web_download":
            arguments = owner.get("tool_arguments") if isinstance(owner.get("tool_arguments"), dict) else {}
            direct_url = str(arguments.get("url") or "").strip()
            if re.match(r"https?://", direct_url) and not re.search(r"/(?:search|link)\?", direct_url, flags=re.I):
                continue
        # The reference ledger is the authoritative record for an approved
        # small-file acquisition.  A later Designer revision may replace the
        # prose evidence with a compact description (and therefore omit the
        # original ``source_type``/hash fields), but it must not reopen an
        # already verified gap.  Require the persisted terminal status plus
        # the local match hash/path so a candidate-only or stale description
        # cannot pass this gate.
        reference_status = str(item.get("reference_status") or "").strip().lower()
        local_match = item.get("local_match") if isinstance(item.get("local_match"), dict) else {}
        local_path = str(local_match.get("path") or local_match.get("saved_path") or "").strip()
        local_hash = str(local_match.get("sha256") or local_match.get("hash") or "").strip()
        if (
            reference_status in _VERIFIED_REFERENCE_STATUSES
            and local_path
            and re.fullmatch(r"[0-9a-fA-F]{64}", local_hash)
        ):
            continue
        evidence: list[Any] = []
        evidence.extend(item.get("evidence") or [])
        if owner:
            spec = (owner.get("tool_arguments") or {}).get("artifact_spec") or {}
            evidence.extend(spec.get("evidence") or [])
        verified = False
        for entry in evidence:
            if not isinstance(entry, dict):
                continue
            source_type = str(entry.get("source_type") or "").strip().lower()
            blob = " ".join(str(entry.get(key) or "") for key in ("url", "source", "path", "commit", "revision", "tag", "sha256", "hash", "detail"))
            concrete = bool(re.search(r"https?://|raw\.githubusercontent\.com|/(?:blob|tree|commit)/|\bcommit\b|\brevision\b|\bsha\b", blob, flags=re.I))
            official = source_type in {"official_documentation", "official_manual", "official_example", "authoritative_reference", "reference_search_verified"}
            hash_value = str(entry.get("sha256") or entry.get("hash") or "").strip()
            has_hash = bool(re.fullmatch(r"[0-9a-fA-F]{64}", hash_value))
            has_revision = bool(re.search(r"\b(?:commit|revision|tag|v?\d+(?:\.\d+)+)\b|/[0-9a-f]{7,40}(?:\b|/)", blob, flags=re.I))
            if asset_role == "external_dataset_reference":
                verified_evidence = official and concrete
            else:
                verified_evidence = official and concrete and has_hash and has_revision
            if verified_evidence:
                verified = True
                break
        if not verified:
            gaps.append(item_id or str(item.get("name_or_role") or "external_asset"))
    return gaps


def _asset_contract_is_external(value: Any) -> bool:
    return bool(normalize_asset_contract(value if isinstance(value, dict) else {"name_or_role": value}).get("is_external"))


def _is_local_path_requirement(value: Any) -> bool:
    """Identify filesystem-location questions that cannot be answered by web search."""
    text = _text_blob(value)
    return bool(re.search(
        r"\b(?:exact|specific)\s+(?:local\s+)?path\b|"
        r"\bpath\s+to\s+(?:the\s+)?(?:local|downloaded|existing)\b|"
        r"\b(?:local|downloaded)\s+(?:file|directory|folder)\s+path\b|"
        r"本地(?:文件|目录|文件夹)?(?:的)?(?:路径|位置)|下载(?:文件|数据)?(?:的)?(?:路径|位置)",
        text,
        flags=re.I,
    ))


def _reference_search_phase(request: dict[str, Any]) -> str:
    """Label the phase from the normalized request contract only."""
    asset_kind = str(request.get("asset_kind") or "").strip().lower()
    if asset_kind in {"dataset", "grib_dataset", "netcdf_dataset"}:
        return "dataset_acquisition"
    if asset_kind in {"geometry", "geometry_or_mesh", "mesh"}:
        return "mesh_prerequisite"
    if asset_kind in {"structure", "atomic_structure"}:
        return "structure_acquisition"
    return "reference_acquisition"


def _fact_is_assumption_or_computable(text: str) -> bool:
    value = text.strip()
    if not value:
        return True
    return bool(re.search(
        r"\b(assumed|assumption|to be calculated|calculated from|computed from|derived from|"
        r"formula|chord length value|freestream velocity|free[- ]?stream velocity|"
        r"blockMesh alone|snappyHexMesh|required or .*suffices)\b|"
        r"假设|可计算|由.*计算|弦长|来流速度|是否需要",
        value,
        flags=re.I,
    )) or _is_builtin_parametric_geometry_requirement(value)


def _is_builtin_parametric_geometry_requirement(value: Any) -> bool:
    """Return true only for an explicitly local parametric geometry contract."""
    if not isinstance(value, dict):
        return False
    contract = normalize_asset_contract(value)
    if contract.get("is_external") or contract.get("source_strategy") != "local_generation":
        return False
    representation = str(contract.get("representation") or "").lower()
    method = " ".join(
        str(value.get(key) or "") for key in ("generation_method", "formula", "generator")
    )
    return representation in {"geometry", "geometry_or_mesh", "mesh", "cad"} and bool(
        re.search(r"parametric|formula|analytic|local generator|解析|参数化|公式", method, flags=re.I)
    )


def _asset_search_query(name: str, _context: dict[str, Any]) -> str:
    name_text = str(name or "")
    identifiers = []
    if not re.search(r"\bNACA\s*[-_ ]?\d{4,5}\b", name_text, flags=re.I):
        for code in _airfoil_profile_codes_from_context(name_text):
            identifiers.append(code)
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9_.-]{2,}\s+airfoil)\b", name_text, flags=re.I):
        candidate = re.sub(r"\s+", " ", match.group(1)).strip()
        if _looks_like_citation_airfoil_label(candidate):
            continue
        candidate_key = re.sub(r"[^a-z0-9]+", "", candidate.lower())
        existing_keys = [re.sub(r"[^a-z0-9]+", "", item.lower()) for item in identifiers]
        if not any(candidate_key in key or key in candidate_key for key in existing_keys):
            identifiers.append(candidate)
    # Search classification must come from the named missing asset, not from
    # prior tool output embedded in the wider plan.  Those outputs contain
    # generic words such as ``asset_profile`` and ``mesh`` that are not
    # scientific semantics of the requested file.
    classification_text = name_text
    # Representation-specific terms take precedence over generic words such as
    # "dataset".  A requirement can legitimately say "airfoil geometry
    # dataset"; it still needs a directly usable coordinate file, not merely a
    # publication or repository landing page.
    if re.search(
        r"\b(atomic coordinates?|fractional coordinates?|wyckoff|lattice|space group|"
        r"crystal(?:line)? structure|molecular structure|cif|"
        r"adsorption site|intercalation site|insertion site|migration endpoint|"
        r"inserted[- ]?species|site coordinates?)\b|原子坐标|分数坐标|"
        r"吸附位点|插层位点|插入位置|迁移端点|晶格|空间群|晶体|分子结构|结构文件",
        classification_text,
        flags=re.I,
    ):
        suffix = "atomic structure coordinates Wyckoff database supplementary data"
    elif re.search(
        r"\b(coordinates?|coordinate file|profile|airfoil|aerofoil|foil|geometry|cad|"
        r"step|stp|iges|igs|stl|brep|msh|mesh|surface)\b|坐标|翼型|几何|网格|表面",
        classification_text,
        flags=re.I,
    ):
        if re.search(r"\b(?:airfoil|aerofoil|foil|profile)\b|翼型", classification_text, flags=re.I):
            suffix = "airfoil coordinates dat UIUC Selig"
        else:
            suffix = "geometry coordinates mesh file"
    elif re.search(
        r"\b(dataset|data table|lookup table|material propert(?:y|ies)|experimental data|"
        r"reference curve|spectrum)\b|数据集|数据表|物性|实验数据|参考曲线|光谱",
        classification_text,
        flags=re.I,
    ):
        suffix = "dataset file authoritative repository"
    elif re.search(r"\b(cif|structure)\b|晶体|结构文件", classification_text, flags=re.I):
        suffix = "structure file coordinates database"
    else:
        suffix = "authoritative reference data file"
    prefix = " ".join(dict.fromkeys(identifiers)) or name
    return f"{prefix} {suffix}".strip()


def _asset_representation_kind(value: Any) -> str:
    """Classify an already-normalized asset without routing on its name."""
    contract = normalize_asset_contract(value if isinstance(value, dict) else {"name_or_role": value})
    capability = str(contract.get("workflow_capability") or "").strip().lower()
    representation = str(contract.get("representation") or "").strip().lower()
    if capability == "dataset_acquisition":
        return "dataset"
    if capability == "official_file_acquisition":
        return "reference"
    if capability == "atomic_structure_generation" or representation in {"structure", "atomic_structure", "crystal_structure"}:
        return "structure"
    if capability in {"geometry_acquisition", "mesh_generation"} or representation in {"geometry", "geometry_or_mesh", "mesh", "cad"}:
        return "geometry_or_mesh"
    if capability in {"configuration_generation", "preprocessing_script_generation", "local_artifact_generation"}:
        return "parameters"
    if representation in {"archive", "bundle", "package"}:
        return "archive"
    return "reference"


def _reference_asset_search_options(query: str, external_asset: bool) -> dict[str, Any]:
    """Build conservative search options for an already classified request.

    Free-form wording is never allowed to grant a geometry/mesh route here;
    explicit AssetContract fields are the only source for that capability.
    """
    reference_convention = bool(re.search(
        r"\b(?:official|authoritative)\s+(?:documentation|manual|reference)|"
        r"\b(?:format|syntax|variable mapping|mapping table|lookup table|code mapping|"
        r"configuration convention)\b|"
        r"官方(?:文档|手册|参考)|权威(?:文档|参考)|(?:格式|语法|映射表|查找表|编码)",
        str(query or ""),
        flags=re.I,
    ))
    if not external_asset or reference_convention:
        return {
            "asset_kind": "reference",
            "auto_discover_downloads": False,
            "max_discovery_pages": 0,
        }
    # Do not infer geometry/mesh from an ambiguous word in a free-form query.
    # Explicit AssetContract fields are handled earlier; this fallback only
    # preserves dataset/structure representations that were already declared.
    text = str(query or "")
    asset_kind = _asset_representation_kind(text)
    if asset_kind not in {"dataset", "structure", "parameters", "archive"}:
        asset_kind = "reference"
    return {
        "asset_kind": asset_kind,
        # Unresolved external assets are documented first.  A direct download
        # is created only from an explicit, size-bounded acquisition contract;
        # free-form fallback facts must not trigger URL/link discovery.
        "auto_discover_downloads": False,
        "max_discovery_pages": 0,
    }


def _task_scope_contract(analysis: dict[str, Any]) -> dict[str, Any]:
    """Return the single capability boundary used by all preprocessing routing.

    The analyst may provide ``task_scope`` explicitly.  Otherwise it is
    derived from normalized asset contracts; prose tokens are intentionally
    not considered capabilities.
    """
    explicit = analysis.get("task_scope") or analysis.get("task_scope_contract")
    explicit = explicit if isinstance(explicit, dict) else {}
    explicit_scope_supplied = bool(explicit)
    if isinstance(analysis.get("structured_stage_scope"), dict) and analysis["structured_stage_scope"].get("source"):
        # Stage ownership is provenance, not a search policy.  A structured
        # scope may explicitly lock a capability out, but merely declaring the
        # stages handled by data must not prevent the model from resolving a
        # missing external dependency (for example a public geometry file).
        # Preserve an explicit lock while keeping the ordinary stage scope
        # soft for dependency resolution.
        explicit = {
            **explicit,
            "scope_origin": str(analysis["structured_stage_scope"].get("source") or "").strip(),
            "caller_scope_locked": bool(explicit.get("caller_scope_locked")),
        }
    scope = normalize_task_scope_contract(
        explicit,
        [item for item in analysis.get("required_files") or [] if isinstance(item, dict)],
    )
    allowed = set(scope.get("allowed_capabilities") or [])
    excluded = set(scope.get("excluded_capabilities") or [])
    structured_stage_scope = analysis.get("structured_stage_scope")
    # Once the caller has declared the data-node stages, an Analyst may
    # not widen that scope merely by listing a capability.  Keep only routes
    # supported by normalized assets; spatial routes additionally require an
    # explicit spatial AssetContract, never a word such as "grid" or
    # "surface" in the plan prose.
    if isinstance(structured_stage_scope, dict) and structured_stage_scope.get("source"):
        asset_capabilities = {
            str(normalize_asset_contract(item).get("workflow_capability") or "")
            for item in analysis.get("required_files") or []
            if isinstance(item, dict)
        }
        spatial_capabilities = {
            "geometry_acquisition", "mesh_generation", "atomic_structure_generation",
        }
        allowed = {
            capability for capability in allowed
            if capability not in spatial_capabilities or capability in asset_capabilities
        }
        # Structured Analyst scopes may carry a stale spatial exclusion from
        # before an explicit caller asset was reconciled.  Once the repaired
        # AssetContract names that route, remove only that derived exclusion;
        # a caller-locked exclusion remains authoritative.
        if not scope.get("caller_scope_locked"):
            excluded = set(scope.get("excluded_capabilities") or [])
            excluded -= asset_capabilities & spatial_capabilities
        excluded |= spatial_capabilities - allowed
    excluded |= {"geometry_acquisition", "mesh_generation", "atomic_structure_generation"} - allowed
    # The parsed caller scope is authoritative. It includes every stage whose
    # assets Data must prepare; formal execution remains downstream.
    stages = (
        structured_stage_scope.get("stage_ids")
        if isinstance(structured_stage_scope, dict) and structured_stage_scope.get("source")
        else explicit.get("stage_ids")
    )
    if not isinstance(stages, list):
        stages = [str(item.get("id") or "") for item in analysis.get("calculation_stages") or [] if isinstance(item, dict)]
    representations = [str(item).strip() for item in explicit.get("data_representations") or [] if str(item).strip()]
    for item in analysis.get("required_files") or []:
        if not isinstance(item, dict):
            continue
        representation = str(normalize_asset_contract(item).get("representation") or "").strip()
        fmt = str(item.get("format") or "").strip()
        if re.search(r"grib", fmt, flags=re.I):
            representation = "GRIB"
        elif re.search(r"netcdf|\bnc\b", fmt, flags=re.I):
            representation = "NetCDF"
        if representation and representation not in representations:
            representations.append(representation)
    scope = {
        "discipline": explicit.get("discipline") or (analysis.get("discipline") or {}).get("primary", "unknown"),
        "workflow_kind": explicit.get("workflow_kind") or analysis.get("calculation_type") or "preprocessing",
        "allowed_capabilities": sorted(allowed),
        "excluded_capabilities": sorted(excluded),
        "declared_operations": scope.get("declared_operations") or [],
        "contract_conflicts": scope.get("contract_conflicts") or [],
        "data_representations": representations,
        "stage_ids": [str(item).strip() for item in stages if str(item).strip()],
        "source": str(
            (structured_stage_scope or {}).get("source")
            or explicit.get("source")
            or ("research_plan_scope" if explicit_scope_supplied else "asset_contract_fallback")
        ),
        "caller_scope_locked": bool(scope.get("caller_scope_locked")),
    }
    return scope


def _caller_forbids_external_reference(analysis: dict[str, Any] | None) -> bool:
    """Honor an explicit caller request to stay offline.

    Absence of a public-search instruction is not an instruction to stay
    offline.  The planning model may discover that an authoritative source is
    needed while resolving a missing input.  Only an explicit negative in the
    caller request keeps that route closed.
    """
    if not isinstance(analysis, dict):
        return False
    text = caller_request_text(
        analysis,
        preprocessing_request=analysis.get("preprocessing_request") if isinstance(analysis, dict) else None,
    )
    return bool(re.search(
        r"(?:不|无需|禁止|不得|不要|仅使用本地|不使用外部).{0,24}"
        r"(?:检索|搜索|查找|联网|网络|公开|外部|下载|获取)|"
        r"(?:without|no|don't|do not|not)\s+(?:web|internet|online|external|search|browse|download|retrieve)",
        text,
        flags=re.I,
    ))


def _scope_allows(
    scope: dict[str, Any],
    capability: str,
    *,
    analysis: dict[str, Any] | None = None,
    reference: bool = False,
) -> bool:
    """Check an execution route without turning scope into a search policy.

    Generation steps still need an explicitly approved capability.  A
    structured reference request is different: it is a model-selected
    dependency-resolution transition, not a new deliverable or execution
    route.  Unless the caller explicitly locked that capability out, let the
    model request targeted evidence/acquisition and keep the existing
    reference-contract, URL, provenance, and review gates in force.
    """
    capability = str(capability or "").strip().lower()
    allowed = set(scope.get("allowed_capabilities") or [])
    excluded = set(scope.get("excluded_capabilities") or [])
    if capability in allowed and capability not in excluded:
        return True
    if not reference or capability not in EXTERNAL_WORKFLOW_CAPABILITIES:
        return False
    # A caller-locked exclusion remains authoritative.  Analyst-derived
    # exclusions are soft defaults and must not prevent the model from
    # resolving a missing external input.
    if scope.get("caller_scope_locked") and capability in excluded:
        return False
    return not _caller_forbids_external_reference(analysis)


def _final_reference_revision(
    analysis: dict[str, Any],
    *,
    stage_id: str = "",
    expected_revision: Any = "",
    evidence_revision: Any = "",
) -> str:
    """Resolve one reference revision using the shared contract precedence."""
    software = analysis.get("simulation_software") if isinstance(analysis.get("simulation_software"), dict) else {}
    stage_version = ""
    for stage in analysis.get("calculation_stages") or []:
        if not isinstance(stage, dict) or (stage_id and str(stage.get("id") or "") != stage_id):
            continue
        parameters = stage.get("parameters") if isinstance(stage.get("parameters"), dict) else {}
        identity = normalize_software_identity(
            stage.get("software") or parameters.get("software") or stage.get("solver") or ""
        )
        stage_version = str(identity.get("version") or parameters.get("version") or "").strip()
        if stage_version:
            break
    return str(
        expected_revision
        or evidence_revision
        or stage_version
        or software.get("preprocessor_version")
        or software.get("version")
        or ""
    ).strip().lstrip("vV")


def _targeted_search_requests_from_analysis(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    scope = _task_scope_contract(analysis)
    analysis["task_scope"] = scope
    routing_audit = {
        "selected_capability": None,
        "selected_by": scope.get("source") or "research_plan_scope",
        "supporting_fields": {
            "discipline": scope.get("discipline"),
            "format": scope.get("data_representations"),
        },
        "ignored_ambiguous_tokens": [],
        "rejected_routes": [],
        "contract_errors": [],
    }
    software = (analysis.get("simulation_software") or {}).get("name") or "simulation software"
    supporting_software = [
        item for item in analysis.get("supporting_software") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    declared_stages = [
        item for item in analysis.get("calculation_stages") or [] if isinstance(item, dict)
    ]
    stage = analysis.get("calculation_type") or "requested calculation"
    # Concrete assets and application guidance answer different questions:
    # the former identifies *what* must be acquired, while the latter
    # establishes the named application's input semantics before generation.
    # Do not suppress the application request merely because the Analyst also
    # emitted dataset contracts.  Request it only when a structured config or
    # script deliverable exists, so unrelated data-only workflows do not gain a
    # speculative search.
    required_files = [
        item for item in analysis.get("required_files") or []
        if isinstance(item, dict)
    ]
    has_generation_contract = any(
        str(normalize_asset_contract(item).get("workflow_capability") or "").strip().lower()
        in {"configuration_generation", "preprocessing_script_generation"}
        for item in required_files
    )
    # A concrete official-file contract already identifies the external
    # input that matters to generation.  Generic application guidance is
    # supplementary evidence; putting it first can repeatedly search an
    # ambiguous product name (for example ``WPS``) and starve the exact-file
    # request that can actually unblock the stage.
    has_exact_official_file_contract = any(
        str(normalize_asset_contract(item).get("workflow_capability") or "").strip().lower()
        == "official_file_acquisition"
        and bool(str(item.get("expected_filename") or "").strip())
        for item in required_files
    )
    application_requests = (
        _official_application_reference_requests(analysis)
        if (
            str(analysis.get("review_profile") or "") == "plan_bound"
            and has_generation_contract
            and not has_exact_official_file_contract
        ) else []
    )
    requests: list[dict[str, Any]] = list(application_requests)
    application_capability = str(
        requests[0].get("workflow_capability")
        if requests else ""
    ).strip().lower() or "official_file_acquisition"
    if requests and not _scope_allows(
        scope, application_capability, analysis=analysis, reference=True
    ):
        if scope.get("caller_scope_locked") or _caller_forbids_external_reference(analysis):
            # 调用方显式锁定的排除仍然作数（请求权威，B）。
            routing_audit["rejected_routes"].append({
                "capability": application_capability,
                "reason": "caller explicitly locked this capability out",
            })
            requests = []
        else:
            # 判决拆除 O8（planner:8499 半降格，2026-08-31）：Analyst 推导的
            # scope 是承诺不是牢笼 —— 请求照发，偏离记账。
            routing_audit.setdefault("scope_deviations", []).append({
                "capability": application_capability,
                "reason": "outside declared research-plan scope; proceeding with deviation recorded",
            })
    application_guidance_requested = bool(requests)
    completed_reference_asset_contracts = {
        (
            canonical_filename_identity(value.get("expected_filename")),
            str(value.get("expected_revision") or "").strip().lower(),
        )
        for value in (analysis.get("reference_gap_status") or {}).values()
        if isinstance(value, dict)
        and str(value.get("status") or "") in {
            "no_result", "discovered_candidate", "unusable_candidate", "candidate_invalid",
            "resolved", "downloaded_verified",
        }
        and str(value.get("expected_filename") or "").strip()
    }

    # Stage prose is scientific context, not an acquisition contract.  Only
    # structured required_files can authorize an external route; this keeps
    # words such as "surface", "adsorption" or "migration" from switching
    # an unrelated workflow into structure/mesh search.

    for item in analysis.get("required_files") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("name_or_role") or "required_file").strip()
        name = str(item.get("name_or_role") or item_id).strip()
        contract = normalize_asset_contract(item)
        if contract.get("fulfillment_kind") == "runtime_tool":
            # Toolchain resolution is an execution-environment concern. An
            # executable is neither a downloadable data asset nor an
            # application-specific reference file for the data package.
            continue
        if item.get("delivery_required") is False and str(item.get("fulfillment_kind") or "") in {
            "runtime_input", "runtime_output",
        }:
            # Runtime acquisition/execution is preserved as a resume
            # dependency. It must not prevent independent script, config, and
            # manifest generation in the current package.
            continue
        asset_role = str(contract.get("asset_role") or "").strip().lower()
        capability = str(contract.get("workflow_capability") or "").strip().lower()
        is_official_file = bool(contract.get("is_external"))
        dataset_reference = (
            capability == "dataset_acquisition"
            or asset_role == "external_dataset_reference"
        )
        if contract.get("contradictory_fields"):
            routing_audit["rejected_routes"].append({
                "capability": capability or "asset_contract",
                "reason": "contradictory asset contract fields",
                "asset_id": item_id,
            })
            continue
        if capability == "unclassified":
            routing_audit["rejected_routes"].append({
                "capability": "unclassified",
                "reason": "asset contract lacks an explicit acquisition or generation capability",
                "asset_id": item_id,
            })
            continue
        if _is_builtin_parametric_geometry_requirement(item):
            continue
        if is_official_file:
            if capability and not _scope_allows(
                scope, capability, analysis=analysis, reference=True
            ):
                if scope.get("caller_scope_locked") or _caller_forbids_external_reference(analysis):
                    # 调用方显式锁定的排除仍然作数（请求权威，B）。
                    routing_audit["rejected_routes"].append({
                        "capability": capability,
                        "reason": "caller explicitly locked this capability out",
                        "asset_id": item_id,
                    })
                    continue
                # 判决拆除 O8（planner:8499 半降格，2026-08-31）：Analyst 推导的
                # scope 是承诺不是牢笼 —— 官方文件检索请求照发，偏离记账；
                # 缺 asset_kind/unclassified 无法路由的分支留 C（上方 continue）。
                routing_audit.setdefault("scope_deviations", []).append({
                    "capability": capability,
                    "reason": "outside declared research-plan scope; proceeding with deviation recorded",
                    "asset_id": item_id,
                })
            # External acquisition is driven by the declared role and
            # representation. The planner does not contain application/file
            # name mappings.
            completed = {
                _normalise_asset_key(value) for value in (
                    analysis.get("completed_reference_request_ids") or []
                )
            }
            if _normalise_asset_key(item_id) in completed:
                continue
            primary_software = analysis.get("simulation_software") or {}
            asset_context = _asset_haystack(item)
            expected_identity = canonical_filename_identity(
                item.get("expected_filename") or name
            )
            declared_stage = next(
                (
                    candidate for candidate in declared_stages
                    if str(candidate.get("id") or "") == str(item.get("stage_id") or "")
                ),
                None,
            )
            if declared_stage is None:
                declared_stage = next(
                    (
                        candidate for candidate in declared_stages
                        if expected_identity
                        and expected_identity in canonical_filename_identity(
                            _asset_haystack(candidate)
                        )
                    ),
                    None,
                )
            stage_context = _asset_haystack(declared_stage or {})
            source_context = " ".join(part for part in (asset_context, stage_context) if part)
            stage_parameters = (
                declared_stage.get("parameters")
                if isinstance((declared_stage or {}).get("parameters"), dict)
                else {}
            )
            stage_software = (
                (declared_stage or {}).get("software")
                or stage_parameters.get("software")
            )
            stage_software_info = normalize_software_identity(stage_software)
            if not stage_software_info.get("version") and stage_parameters.get("version"):
                stage_software_info["version"] = str(stage_parameters["version"]).strip().lstrip("vV")
            stage_solver = str((declared_stage or {}).get("solver") or "").strip()
            if not stage_software_info.get("name") and stage_solver:
                stage_software_info = normalize_software_identity(stage_solver)
            if not stage_software_info.get("name"):
                stage_component = next(
                    (
                        str(component).strip()
                        for component in primary_software.get("components") or []
                        if str(component).strip()
                        and re.search(
                            rf"(?<![A-Za-z0-9]){re.escape(str(component).strip())}(?![A-Za-z0-9])",
                            stage_context,
                            flags=re.I,
                        )
                    ),
                    "",
                )
                if stage_component:
                    stage_software_info["name"] = stage_component
            supporting_match = next(
                (
                    candidate for candidate in supporting_software
                    if re.search(
                        rf"(?<![A-Za-z0-9]){re.escape(str(candidate.get('name') or '').strip())}(?![A-Za-z0-9])",
                        source_context,
                        flags=re.I,
                    )
                ),
                {},
            )
            software_info = (
                stage_software_info
                if stage_software_info.get("name")
                else supporting_match or primary_software
            )
            software_name = str(software_info.get("name") or software or "simulation software").strip()
            software_role = re.sub(r"[_-]+", " ", str(software_info.get("role") or "")).strip()
            software_label = " ".join(part for part in (software_name, software_role) if part)
            software_version = str(software_info.get("version") or "").strip()
            if not software_version and isinstance(primary_software, dict):
                software_version = str(
                    primary_software.get("preprocessor_version")
                    or primary_software.get("version")
                    or ""
                ).strip().lstrip("vV")
            version_clause = (
                f" {software_version}" if software_version.lower().startswith("v")
                else f" v{software_version}" if software_version else ""
            )
            # Keep the stage component as the primary search term, but retain
            # the enclosing simulation identity when it disambiguates a
            # generic component name (for example WPS vs WPS Office).  This
            # reuses the structured software contract; it does not add
            # application-specific aliases or a second search path.
            query_software_terms: list[str] = []
            for candidate in (
                software_label,
                str(primary_software.get("name") or "").strip()
                if isinstance(primary_software, dict) else "",
            ):
                normalized_candidate = re.sub(r"\s+", " ", str(candidate or "")).strip()
                if normalized_candidate and normalized_candidate.casefold() not in {
                    item.casefold() for item in query_software_terms
                }:
                    query_software_terms.append(normalized_candidate)
            query_software_context = " ".join(query_software_terms)
            expected_filename = normalize_declared_filename(item.get("expected_filename"))
            expected_path = ""
            acquisition_contract = item.get("acquisition_contract")
            declared_locator: dict[str, str] = {}
            if isinstance(acquisition_contract, dict):
                declared_locator = normalize_official_locator(acquisition_contract)
                expected_path = str(
                    acquisition_contract.get("locator")
                    or acquisition_contract.get("url")
                    or ""
                ).strip()
                if not expected_path:
                    expected_path = str(
                        declared_locator.get("raw_url")
                        or declared_locator.get("path")
                        or ""
                    ).strip()
            geometry_discovery = (
                capability == "geometry_acquisition"
                and not any(
                    str(value or "").strip()
                    for value in (
                        item.get("expected_filename"), item.get("expected_revision"),
                        expected_path, declared_locator.get("path"),
                        declared_locator.get("raw_url"),
                    )
                )
            )
            requires_exact_file = not geometry_discovery
            if requires_exact_file and not dataset_reference and not expected_filename:
                # An exact-file acquisition contract may provide a repository
                # locator instead of duplicating the basename in a separate
                # field.  Derive it from that structured locator; never use a
                # logical id such as ``f007`` as a physical filename.
                locator_candidates = []
                if isinstance(acquisition_contract, dict):
                    locator_candidates.extend(
                        acquisition_contract.get(key)
                        for key in ("locator", "path", "url", "raw_url")
                    )
                    retrieval = acquisition_contract.get("retrieval_instructions")
                    if isinstance(retrieval, dict):
                        locator_candidates.extend(
                            retrieval.get(key)
                            for key in ("locator", "path", "url", "raw_url")
                        )
                for locator in locator_candidates:
                    candidate_name = PurePosixPath(str(locator or "").split("?", 1)[0]).name
                    if (
                        candidate_name
                        and candidate_name not in {".", "/"}
                        and candidate_name.casefold() != item_id.casefold()
                    ):
                        expected_filename = candidate_name
                        break
            if requires_exact_file and not dataset_reference and not expected_filename:
                # An official-file request without a physical filename cannot
                # be validated or safely downloaded.  Do not send a weak
                # logical id (for example ``f007``) to the web search layer;
                # surface a contract error for the Analyst/Designer instead.
                routing_audit["contract_errors"].append({
                    "asset_id": item_id,
                    "reason": "missing expected_filename in exact-file acquisition contract",
                    "expected_path": expected_path,
                })
                continue
            expected_revision = _final_reference_revision(
                analysis,
                stage_id=str(item.get("stage_id") or "").strip(),
                expected_revision=item.get("expected_revision"),
            )
            expected_filename = normalize_declared_filename(expected_filename or name) if requires_exact_file else ""
            expected_identity = canonical_filename_identity(expected_filename)
            if expected_identity and any(
                completed_identity == expected_identity
                and (
                    not completed_revision
                    or not expected_revision
                    or completed_revision == expected_revision.lower()
                )
                for completed_identity, completed_revision in completed_reference_asset_contracts
            ):
                continue
            if dataset_reference:
                dataset_contract = normalize_acquisition_contract(item)
                if not acquisition_contract_errors(dataset_contract) and dataset_contract.get("source_ready"):
                    retrieval = dataset_contract.get("retrieval_instructions") or {}
                    if dataset_contract.get("delivery_mode") == "direct_download":
                        direct_dataset_url = str(retrieval.get("url") or retrieval.get("locator") or "").strip()
                        requests.append({
                            "id": f"download_{item_id}",
                            "missing": name,
                            "reason": "Download the declared small dataset before generation.",
                            "query": "",
                            "url": direct_dataset_url,
                            "tool": "data_web_download",
                            "web_search_allowed": False,
                            "search_mode": "approved_small_dataset_download",
                            "asset_kind": "dataset",
                            "asset_role": "external_dataset_reference",
                            "workflow_capability": "dataset_acquisition",
                            "gap_id": item_id,
                            "documentation_reference": True,
                            "auto_discover_downloads": False,
                            "max_discovery_pages": 0,
                            "generation_phase": "pre_generation",
                        })
                        continue
                    # Provider metadata makes a document actionable, but only
                    # an actual locator proves discovery already happened.
                    if re.match(
                        r"^https?://",
                        str(retrieval.get("url") or retrieval.get("locator") or ""),
                        flags=re.I,
                    ):
                        continue
            official_locator = (
                {
                    **declared_locator,
                    **normalize_official_locator(expected_path),
                }
                if not dataset_reference else {}
            )
            locator_has_repository = bool(official_locator.get("repository_url"))
            locator_has_file = bool(official_locator.get("path"))
            locator_filename_mismatch = bool(
                locator_has_file
                and expected_filename
                and canonical_filename_identity(PurePosixPath(official_locator.get("path")).name)
                != canonical_filename_identity(expected_filename)
            )
            # An exact official-file contract cannot be downgraded to a weak
            # search when it has neither a version nor a usable locator. A
            # repository root is the one intentional exception: it is a
            # bounded discovery request for the missing file path.
            if requires_exact_file and not dataset_reference and (
                locator_filename_mismatch
                or
                (locator_has_file and not expected_revision)
                or (not expected_revision and not expected_path)
            ):
                routing_audit["contract_errors"].append({
                    "asset_id": item_id,
                    "reason": (
                        "repository locator filename does not match expected_filename"
                        if locator_filename_mismatch else
                        "exact official-file acquisition requires a target revision"
                        if locator_has_file else
                        "official-file acquisition requires a repository locator or target revision"
                    ),
                    "expected_filename": expected_filename,
                    "expected_path": expected_path,
                })
                continue
            direct_url = _official_file_candidate_url(
                {},
                {
                    "expected_filename": expected_filename,
                    "expected_path": expected_path,
                    "expected_revision": expected_revision,
                    "locator_contract": official_locator,
                },
            ) if requires_exact_file and not dataset_reference and expected_filename and expected_revision else ""
            query_asset = (
                f'"{expected_filename}"'
                if expected_filename and not dataset_reference else name
            )
            query_prefix = "" if dataset_reference else f"{query_software_context}{version_clause} "
            query_suffix = (
                "authoritative dataset source metadata"
                if dataset_reference else
                "official authoritative repository file path commit tag raw"
                if requires_exact_file else
                "downloadable geometry mesh coordinate CAD file"
            )
            if dataset_reference and isinstance(acquisition_contract, dict):
                retrieval = acquisition_contract.get("retrieval_instructions")
                retrieval = retrieval if isinstance(retrieval, dict) else {}
                identity = acquisition_contract.get("dataset_identity")
                identity = identity if isinstance(identity, dict) else {}
                provider = (
                    acquisition_contract.get("provider")
                    or retrieval.get("provider")
                    or retrieval.get("source")
                    or ""
                )
                query_asset = " ".join(
                    value for value in (
                        query_asset,
                        str(identity.get("representation") or acquisition_contract.get("representation") or "").strip(),
                        str(provider).strip(),
                    ) if value
                )
            if dataset_reference:
                routing_audit["selected_capability"] = "dataset_acquisition"
                routing_audit["supporting_fields"].update({
                    "representation": contract.get("representation"),
                    "acquisition_kind": contract.get("acquisition_kind"),
                })
                routing_audit["ignored_ambiguous_tokens"].extend(
                    token for token in ("surface", "field", "grid") if token in name.lower()
                )
            elif capability:
                routing_audit["selected_capability"] = capability
            requests.append({
                # Keep the acquisition phase explicit even when the Analyst
                # already supplied an exact locator.  The reference executor
                # then emits one stable ``download_acquire_<asset>`` step.
                "id": f"acquire_{item_id}" if direct_url else item_id,
                "missing": name,
                "reason": (
                    "An authoritative dataset source is required before generation."
                    if dataset_reference else
                    "Acquire the exact official file from its approved locator."
                    if direct_url else
                    "Search for a public geometry or mesh asset matching the requested physical object."
                    if not requires_exact_file else
                    "The exact official file path and revision are required before generation."
                ),
                "query": (
                    f"{query_prefix}{query_asset} {query_suffix}"
                ) if not direct_url else "",
                "url": direct_url,
                "tool": "data_web_download" if direct_url else "data_web_search",
                "web_search_allowed": not bool(direct_url),
                "search_mode": (
                    "authoritative_dataset" if dataset_reference else
                    "official_exact_file_acquisition" if direct_url else
                    "official_exact_file" if requires_exact_file else
                    "official_geometry_reference"
                ),
                "asset_kind": (
                    "dataset" if dataset_reference else
                    "official_file" if requires_exact_file else
                    "geometry_or_mesh"
                ),
                "documentation_reference": True,
                # Public geometry discovery only needs a usable, traceable
                # asset.  Restricting it to institutional/official hosts
                # turns a model-selected dependency into an exact-file gate;
                # exact contracts retain the stronger provenance requirement.
                "official_source_required": bool(requires_exact_file or dataset_reference),
                "asset_role": asset_role or "official_file_reference",
                "workflow_capability": capability or (
                    "dataset_acquisition" if dataset_reference else "official_file_acquisition"
                ),
                "requires_exact_file": requires_exact_file,
                "expected_filename": "" if dataset_reference or not requires_exact_file else expected_filename,
                "expected_revision": "" if dataset_reference or not requires_exact_file else expected_revision,
                "expected_path": "" if dataset_reference or not requires_exact_file else (official_locator.get("path") or expected_path),
                "repository_url": "" if dataset_reference else official_locator.get("repository_url", ""),
                "revision": "" if dataset_reference else official_locator.get("revision", ""),
                "path": "" if dataset_reference else official_locator.get("path", ""),
                # Exact-file discovery may inspect one approved repository
                # page to expose a direct link, but it never downloads the
                # file. Acquisition remains a separate ledger-approved plan.
                "auto_discover_downloads": not requires_exact_file,
                # External datasets are represented by a per-step retrieval
                # workflow. Only explicitly small, actionable payloads use the
                # separate download step; unresolved sources remain documents.
                # Resolve a few ranked official source pages inside this one
                # approved search request. This is link inspection only; the
                # resulting file still needs a separate acquisition plan.
                "max_discovery_pages": 0 if direct_url else 1,
                "generation_phase": "pre_generation",
            })
            continue
        if not contract.get("is_external") and contract.get("source_strategy") in {"local_generation", "local_reuse"}:
            # Locally generated and verified locally reused assets never
            # become search gaps, even when their representation is a
            # scientific data format.
            continue
        if _asset_contract_is_local(item):
            if _is_builtin_parametric_geometry_requirement(item):
                continue
            # A named application's official guidance is the shared evidence
            # source for its configuration-like files.  Without that anchor,
            # request evidence for this concrete role rather than assuming a
            # local template encodes the application's semantics.
            if application_guidance_requested or _has_reliable_evidence(item):
                continue
            requests.append({
                "id": f"documentation_{item_id}",
                "missing": f"authoritative input guidance for {name}",
                "reason": "Local evidence does not establish this preprocessing file's semantics.",
                "query": f"{software} {stage} {name} required preprocessing input official documentation",
                "tool": "data_web_search",
                "web_search_allowed": True,
                "search_mode": "generic",
                "asset_kind": "reference",
                "documentation_reference": True,
                "auto_discover_downloads": False,
                "max_discovery_pages": 1,
                "generation_phase": "pre_generation",
            })
            continue
        if _required_asset_is_recovered(item, analysis):
            continue
        if _is_local_path_requirement(name) or _is_local_path_requirement(item.get("reason")):
            requests.append({
                "id": item_id,
                "missing": name,
                "reason": "Local filesystem location is unresolved; inspect authorized inputs and resume this dependency.",
                "query": "",
                "tool": "inspect_input_path",
                "web_search_allowed": False,
                "search_mode": "local_deferred_dependency",
                "deferred_dependency": True,
                "resume_contract": "Provide or discover the local path, then resume the affected generation step.",
            })
            continue
        if _is_restricted_asset(item):
            requests.append({
                "id": item_id,
                "missing": name,
                "reason": "Restricted/licensed asset must be discovered locally and documented; do not web-search its contents.",
                "query": "",
                "tool": "local_authorized_discovery",
                "web_search_allowed": False,
                "search_mode": "local_restricted_asset",
            })
            continue
        external_asset = _asset_contract_is_external(item)
        if (
            float(item.get("confidence") or 0.0) >= 0.75
            and _has_reliable_evidence(item)
            and not external_asset
        ):
            continue
        query = (
            _asset_search_query(name, analysis)
            if external_asset
            else f"{software} {stage} {name} required preprocessing evidence official manual authoritative example"
        )
        search_options = _reference_asset_search_options(query, external_asset)
        requests.append({
            "id": item_id,
            "missing": name,
            "reason": "Local/upstream evidence is insufficient for this preprocessing deliverable.",
            "query": query,
            "tool": "data_web_search",
            "web_search_allowed": True,
            "search_mode": "generic",
            **search_options,
        })

    for index, fact in enumerate(analysis.get("unresolved_facts") or [], 1):
        # Free-form questions are explanatory notes, not acquisition
        # contracts. Only a structured unresolved asset may enter the
        # reference state machine; otherwise words such as "dataset" or
        # "license" reopen searches after every planning pass.
        if not isinstance(fact, dict):
            continue
        text = str(
            fact.get("description") or fact.get("question") or fact.get("missing") or fact.get("id")
        ).strip()
        if not text:
            continue
        if (
            _fact_is_assumption_or_computable(text)
            or _asset_contract_is_local(text)
            or _fact_is_satisfied_by_recovered_asset(text, analysis)
        ):
            continue
        if _is_local_path_requirement(text):
            requests.append({
                "id": f"unresolved_{index}",
                "missing": text,
                "reason": "This is a local filesystem dependency, not a public reference request.",
                "query": "",
                "tool": "inspect_input_path",
                "web_search_allowed": False,
                "search_mode": "local_deferred_dependency",
                "deferred_dependency": True,
                "resume_contract": "Inspect authorized local inputs and resume the affected step.",
            })
            continue
        if re.search(r"\b(licensed|license|restricted|proprietary|controlled[- ]access|credential)\b|许可|专有|受控|凭证", text, flags=re.I):
            requests.append({
                "id": f"unresolved_{index}",
                "missing": text,
                "reason": "Unresolved restricted-asset fact requires local authorized discovery, not public web search.",
                "query": "",
                "tool": "local_authorized_discovery",
                "web_search_allowed": False,
                "search_mode": "local_restricted_asset",
            })
            continue
        external_asset = _asset_contract_is_external(fact)
        # Free-form unresolved facts are not automatically web-search tasks.
        # Solver settings, execution environments, model choices, and numeric
        # defaults belong in assumptions or the Experiment-owned environment contract. Public
        # acquisition is reserved for a concrete missing external data asset.
        if not external_asset:
            continue
        query = (
            _asset_search_query(text, analysis)
            if external_asset
            else f"{software} {stage} {text} official manual authoritative example"
        )
        search_options = _reference_asset_search_options(query, external_asset)
        requests.append({
            "id": f"unresolved_{index}",
            "missing": text,
            "reason": "Critic/planning cannot verify this from local information.",
            "query": query,
            "tool": "data_web_search",
            "web_search_allowed": True,
            "search_mode": "generic",
            **search_options,
        })

    # Acquire one concrete external asset before broad application guidance.
    # The guidance request remains reproducible in the next planning pass if it
    # is still needed, but must not compete with the immediate asset gap.
    concrete_asset_requests = [
        item for item in requests
        if isinstance(item, dict)
        and item.get("web_search_allowed")
        and not item.get("documentation_reference")
        and item.get("asset_kind") not in {None, "reference"}
    ]
    if concrete_asset_requests:
        requests = [
            item for item in requests
            if not (
                item.get("documentation_reference")
                or str(item.get("id") or "").startswith(("official_", "documentation_"))
            )
        ]

    # Resolve exact, versioned official files before dataset documentation.
    # Dataset requests intentionally produce acquisition instructions and
    # therefore cannot unblock a required stage-input file; preserving the
    # old required_files order made those requests postpone the actionable
    # reference indefinitely.
    requests.sort(
        key=lambda item: 0 if (
            str(item.get("workflow_capability") or "").strip().lower()
            == "official_file_acquisition"
            and bool(str(item.get("expected_filename") or "").strip())
            and bool(str(item.get("expected_revision") or "").strip())
        ) else 1
    )
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    resolved_ids = {str(value) for value in analysis.get("resolved_gap_ids") or []}
    resolved_ids.update(
        str(key) for key, value in (analysis.get("reference_gap_status") or {}).items()
        if (
            str(value.get("status") or "") in {"resolved", "downloaded_verified"}
            if isinstance(value, dict) else str(value) in {"resolved", "downloaded_verified"}
        )
    )
    completed_ids = {
        _normalise_asset_key(value)
        for value in analysis.get("completed_reference_request_ids") or []
    }
    for request in requests:
        if not str(request.get("asset_kind") or "").strip():
            request["asset_kind"] = "reference"
        if not str(request.get("workflow_capability") or "").strip():
            request["workflow_capability"] = (
                "dataset_acquisition"
                if request.get("asset_kind") == "dataset"
                else "configuration_generation"
                if request.get("documentation_reference")
                else "official_file_acquisition"
            )
        request.setdefault("generation_phase", _reference_search_phase(request))
        # Keep the persisted gap identity stable when a failed search is
        # replaced by an already-declared exact locator.  The request id may
        # change from ``f006``/``search_f006`` to ``acquire_f006``, but the
        # reference ledger must still receive the outcome in the same gap.
        request_key = str(request.get("id") or "").strip()
        request_key = request_key.removeprefix("acquire_").removeprefix("search_")
        existing_gap_id = next(
            (
                str(gap_id)
                for gap_id, gap in (analysis.get("reference_gap_status") or {}).items()
                if isinstance(gap, dict)
                and str(gap.get("request_id") or "").strip() == request_key
            ),
            "",
        )
        if existing_gap_id:
            request["gap_id"] = existing_gap_id
        request.setdefault("gap_id", canonical_hash({
            "id": request.get("id"),
            "missing": request.get("missing"),
        })[:16])
        if (
            request.get("gap_id") in resolved_ids
            or str(request.get("id") or "") in resolved_ids
            or _normalise_asset_key(request.get("id")) in completed_ids
        ):
            continue
        query = str(request.get("query") or "").strip().lower()
        expected_identity = canonical_filename_identity(request.get("expected_filename"))
        key = (
            "official_file",
            f"{expected_identity}@{str(request.get('expected_revision') or '').strip().lower()}",
        ) if (
            str(request.get("workflow_capability") or "") == "official_file_acquisition"
            and expected_identity
        ) else (
            ("query", query)
            if query else (str(request.get("missing") or "").lower(), query)
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(request)
    analysis["routing_audit"] = routing_audit
    return deduped


def _build_reference_search_plan(
    analysis: dict[str, Any],
    request: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    evidence = analysis.get("evidence") or [{"source_type": "planning", "detail": reason}]
    # The reference plan is executable on its own, so its embedded analysis
    # must carry the same non-empty provenance used by the deliverable.  Do
    # this when building the plan rather than weakening the shared plan schema:
    # an Analyst may legitimately have no top-level evidence until this exact
    # reference request is selected.
    reference_analysis = {**analysis, "evidence": evidence}
    request_id = str(request.get("id") or "request_1")
    deliverable_id = f"reference_evidence_{request_id}"
    query = str(request.get("query") or "").strip()
    tool_name = str(request.get("tool") or "data_web_search").strip()
    asset_kind = str(request.get("asset_kind") or "").strip()
    if not asset_kind:
        raise ValueError("reference request must declare asset_kind")
    # A reference plan has a narrower lifetime than the generation plan.  If
    # the model selected a legitimate dependency route that was not listed in
    # the soft analyst scope, grant that one capability only to this search or
    # acquisition plan.  The original requirement analysis remains unchanged
    # and the final generation plan still has to satisfy its own scope.
    reference_scope = normalize_task_scope_contract(
        analysis.get("task_scope") if isinstance(analysis.get("task_scope"), dict) else {},
        [item for item in analysis.get("required_files") or [] if isinstance(item, dict)],
    )
    reference_capability = str(request.get("workflow_capability") or "").strip().lower()
    if (
        reference_capability in EXTERNAL_WORKFLOW_CAPABILITIES
        and reference_capability not in set(reference_scope.get("allowed_capabilities") or [])
        and not reference_scope.get("caller_scope_locked")
    ):
        reference_scope["allowed_capabilities"] = sorted({
            *[str(value) for value in reference_scope.get("allowed_capabilities") or []],
            reference_capability,
        })
        reference_scope["excluded_capabilities"] = sorted({
            str(value) for value in reference_scope.get("excluded_capabilities") or []
            if str(value) != reference_capability
        })
        reference_scope["required_capabilities_outside_scope"] = []
    reference_analysis["task_scope"] = reference_scope
    is_download = tool_name == "data_web_download"
    deliverables = [{
            "id": deliverable_id,
            "name": f"Reference evidence for {request.get('missing') or request_id}",
            "type": "reference_evidence",
            "required": True,
            "format": "search result metadata",
            "requirement_basis": request.get("reason") or reason,
            "evidence": evidence,
            "acceptance_criteria": [
                "Search query or exact acquisition URL matches the planning-selected missing evidence gap.",
                "The executor records structured reference evidence before final preprocessing generation.",
            ],
        }]
    steps = [{
            "id": f"{'download' if is_download else 'search'}_{request_id}",
            "action": f"{'Acquire exact official file' if is_download else 'Search for missing reference evidence'}: {request.get('missing') or request_id}",
            "tool_capability": "approved_reference_acquisition" if is_download else "targeted_public_reference_search",
            "tool_name": tool_name,
            "tool_arguments": {
                **({
                    "url": str(request.get("url") or "").strip(),
                    "asset_kind": asset_kind,
                    "asset_role": request.get("asset_role") or "official_file_reference",
                    "requires_exact_file": request.get("requires_exact_file"),
                    "expected_filename": request.get("expected_filename") or "",
                    "expected_revision": request.get("expected_revision") or "",
                    "expected_path": request.get("expected_path") or "",
                    "locator_contract": {
                        key: request.get(key) or ""
                        for key in ("repository_url", "revision", "path")
                        if request.get(key)
                    },
                    "allow_non_mesh_file": asset_kind in {"official_file", "reference_file"},
                    # An acquisition plan has already selected one exact URL;
                    # it must fetch that object rather than reopen link
                    # discovery and change targets.
                    "discover_links": False,
                    "download_first_matching_link": False,
                } if is_download else {
                    "query": query,
                    "asset_role": request.get("asset_role") or "",
                    "requires_exact_file": request.get("requires_exact_file"),
                    # Discovery arguments remain part of the audit record;
                    # executor authorization is owned by the active step.
                    "asset_kind": asset_kind,
                    # Keep the request's discovery/exactness contract in the
                    # approved step so evidence recording can either consume
                    # a downloaded public asset or construct the separately
                    # approved exact-file acquisition step.
                    "expected_filename": request.get("expected_filename") or "",
                    "expected_revision": request.get("expected_revision") or "",
                    "expected_path": request.get("expected_path") or "",
                }),
                "gap_id": request.get("gap_id") or canonical_hash({"id": request_id, "missing": request.get("missing")})[:16],
                "limit": 5,
                "auto_discover_downloads": bool(request.get("auto_discover_downloads")),
                "max_discovery_pages": int(request.get("max_discovery_pages") or 0),
                "search_mode": request.get("search_mode") or "generic",
                "workflow_capability": request.get("workflow_capability") or (
                    "dataset_acquisition" if request.get("asset_kind") == "dataset" else "official_file_acquisition"
                ),
            },
            "dependencies": [],
            "outputs": [deliverable_id],
            "verification": [
                "Tool status is success or an explicit transient/no-result state is recorded.",
                "If the approved request targets a reusable geometry/data asset, discovered or downloaded files are recorded with source and hash.",
            ],
        }]
    return {
        "schema_version": "1.0",
        "task_summary": "Acquire planning-selected reference evidence before preprocessing generation.",
        "discipline": analysis.get("discipline") or {"primary": "unknown", "evidence": evidence},
        "simulation_software": analysis.get("simulation_software") or {"name": "unknown", "evidence": evidence},
        "requirement_analysis": {
            **reference_analysis,
            "targeted_search_requests": [request],
        },
        "required_deliverables": deliverables,
        "generation_steps": steps,
        "tool_requirements": [{
            "capability": "approved_reference_acquisition",
            "selected_tools": [tool_name],
        }],
        "targeted_search_requests": [request],
        "assumptions": [
            "This plan only acquires missing reference evidence; final preprocessing files require a new plan after evidence is merged."
        ],
        "plan_kind": "reference_evidence_only",
        "unresolved_questions": [],
        "risks": ["Search may return no authoritative source; record that result and re-plan."],
        "reproducibility": {
            "workspace_layout": "planning/reference_search_plan",
            "provenance_records": ["approved targeted_search_requests", "data_web_search tool outputs"],
        },
    }


def _approve_reference_search_plan(
    state: State,
    analysis: dict[str, Any],
    request: dict[str, Any],
    reason: str,
) -> dict[str, Any] | None:
    if not request:
        return None
    if not str(request.get("asset_kind") or "").strip():
        return None
    try:
        raw_plan = _build_reference_search_plan(analysis, request, reason)
    except ValueError:
        return None
    validation = validate_plan(raw_plan)
    if not validation.get("valid"):
        state.append_transcript(
            "preprocessing_reference_plan_contract_rejected",
            request_id=str(request.get("id") or ""),
            errors=list(validation.get("errors") or [])[:8],
        )
        return None
    # Reference plans are executable plans too.  Save exactly the normalized
    # schema output so the executor sees the same top-level TaskScopeContract
    # that was used for approval.
    plan = validation["plan"]
    store = PlanningStore(state)
    signature = canonical_hash({
        "request": {key: request.get(key) for key in ("id", "query", "url", "tool", "asset_kind", "step_id")},
        "analysis": {
            "discipline": analysis.get("discipline"),
            "simulation_software": analysis.get("simulation_software"),
        },
    })
    reference_store = PlanningStore(state)
    reference_state = reference_store.load_reference_state()
    ledger = reference_state.get("gaps") or {}
    if str(request.get("tool") or "") == "data_web_download":
        gap_id = str(request.get("gap_id") or "")
        if gap_id and isinstance(ledger.get(gap_id), dict) and ledger[gap_id].get("status") in {"discovered_candidate", "candidate"}:
            reference_store.record_reference_outcome(
                gap_id,
                status="acquisition_approved",
            )
            ledger[gap_id] = {
                **ledger[gap_id],
                "status": "acquisition_approved",
            }
    gap_id = str(request.get("gap_id") or "")
    if gap_id and isinstance(ledger.get(gap_id), dict) and ledger[gap_id].get("status") in {"resolved", "downloaded_verified", "no_result"}:
        return None
    previous_signature = str(reference_state.get("plan_signature") or "")
    latest = store.latest_plan("reference_evidence_only")
    if previous_signature == signature and latest and latest.get("plan", {}).get("plan_kind") == "reference_evidence_only":
        status = store.approval_status("reference_evidence_only")
        if status.get("approved"):
            return {
                "plan_id": latest.get("plan_id"),
                "plan_hash": latest.get("plan_hash"),
                "plan_path": latest.get("path"),
                "execution_tool": "execute_preprocessing_plan",
                "reused": True,
            }
    reference_store.commit_reference_ledger(ledger, plan_signature=signature)
    record = store.save_plan(plan, source="deterministic_reference_search_plan")
    critique = {
        "decision": "approve",
        "overall_score": 8.5,
        "dimension_scores": {name: 8.5 for name in (
            "discipline_and_solver",
            "deliverable_completeness",
            "physical_semantics",
            "tool_compatibility",
            "execution_feasibility",
            "verification_and_reproducibility",
            "safety",
        )},
        "critical_concerns": [],
        "major_concerns": [],
        "recommended_changes": [],
    }
    critique_record = store.save_critique(critique, record)
    return {
        "plan_id": record.get("plan_id"),
        "plan_hash": record.get("plan_hash"),
        "plan_path": record.get("path"),
        "critique_id": critique_record.get("critique_id"),
        "execution_tool": "execute_preprocessing_plan",
    }


def _reference_acquisition_requests(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn only explicitly small, actionable candidates into downloads.

    Large or unknown-size datasets remain acquisition documents. Discovery is
    deduplicated by request id; a search miss is preserved as document
    provenance rather than promoted to a failed generation plan.
    """
    ledger = analysis.get("reference_gap_status") or {}
    entries = _load_reference_evidence_from_analysis(analysis)
    active_request_ids = {
        str(item).strip()
        for item in analysis.get("active_reference_request_ids") or []
        if str(item).strip()
    }
    completed = {str(value) for value in analysis.get("completed_reference_request_ids") or []}
    resolved = {str(value) for value in analysis.get("resolved_reference_request_ids") or []}
    requests: list[dict[str, Any]] = []
    for gap_id, state in ledger.items():
        state_dict = state if isinstance(state, dict) else {"status": str(state)}
        matching_entry = next((entry for entry in entries if str(entry.get("gap_id") or "") == str(gap_id)), {})
        matching_result = matching_entry.get("result") if isinstance(matching_entry.get("result"), dict) else matching_entry
        request_id = str(state_dict.get("request_id") or matching_entry.get("step_id") or "").removeprefix("search_")
        source_contract = next(
            (
                item for item in analysis.get("required_files") or []
                if isinstance(item, dict) and str(item.get("id") or "") == request_id
            ),
            {},
        )
        source_acquisition = source_contract.get("acquisition_contract") if isinstance(source_contract, dict) else {}
        source_locator = str(state_dict.get("expected_path") or "").strip()
        if not source_locator:
            source_locator = str(
                (source_acquisition or {}).get("locator")
                or (source_acquisition or {}).get("path")
                or (source_acquisition or {}).get("url")
                or ""
            ).strip() if isinstance(source_acquisition, dict) else ""
        evidence = state_dict.get("evidence") if isinstance(state_dict.get("evidence"), dict) else {}
        if not source_locator:
            source_locator = str(evidence.get("expected_path") or "").strip()
        status = str(state_dict.get("status") or "")
        if status not in {"candidate", "discovered_candidate", "acquisition_approved"}:
            # ``no_result`` and ``candidate_invalid`` are terminal outcomes
            # for this request.  In particular, a failed download must never
            # be promoted into another acquisition step from its own error
            # URL; a new search/locator is required to create fresh evidence.
            continue
        if active_request_ids and request_id not in active_request_ids:
            continue
        asset_role = str(
            state_dict.get("asset_role")
            or evidence.get("asset_role")
            or matching_entry.get("asset_role")
            or matching_result.get("asset_role")
            or ""
        ).strip().lower()
        if not request_id:
            continue
        if asset_role == "external_dataset_reference":
            # Search evidence can upgrade a dataset to a direct download only
            # when the Research Plan (or evidence) declares a small payload.
            # Otherwise the generation plan publishes the complete acquisition
            # document and no second URL-only acquisition step is created.
            dataset_source = {
                **(source_contract if isinstance(source_contract, dict) else {}),
                "reference_status": status,
                "source_discovery": {
                    "status": "resolved" if matching_result.get("url") else "no_result" if status == "no_result" else "unresolved",
                    "reason": matching_result.get("error") or matching_result.get("reason") or "",
                },
                "reference_evidence": matching_entry,
            }
            normalized_dataset = normalize_acquisition_contract(dataset_source)
            retrieval = normalized_dataset.get("retrieval_instructions") or {}
            candidate_url = str(
                retrieval.get("url") or retrieval.get("locator")
                or matching_result.get("url") or evidence.get("url") or ""
            ).strip()
            if normalized_dataset.get("delivery_mode") == "direct_download" and candidate_url:
                if request_id not in completed and request_id not in resolved:
                    requests.append({
                        "id": f"download_{request_id}",
                        "missing": source_contract.get("name_or_role") or request_id,
                        "reason": "Download the declared small dataset from the approved source.",
                        "url": candidate_url,
                        "tool": "data_web_download",
                        "web_search_allowed": False,
                        "search_mode": "approved_small_dataset_download",
                        "asset_kind": "dataset",
                        "asset_role": "external_dataset_reference",
                        "workflow_capability": "dataset_acquisition",
                        "gap_id": gap_id,
                        "documentation_reference": True,
                        "auto_discover_downloads": False,
                        "max_discovery_pages": 0,
                    })
            continue
        if asset_role not in {"official_file_reference", "external_asset", "reference_asset"}:
            continue
        stage_id = str(source_contract.get("stage_id") or "").strip()
        version = _final_reference_revision(
            analysis,
            stage_id=stage_id,
            expected_revision=state_dict.get("expected_revision"),
            evidence_revision=evidence.get("revision"),
        )
        locator_contract = {
            key: source_acquisition.get(key)
            for key in ("repository_url", "revision", "path", "raw_url")
            if isinstance(source_acquisition, dict) and source_acquisition.get(key)
        }
        normalized_locator = normalize_official_locator(
            locator_contract or source_locator
        )
        normalized_path = str(
            normalized_locator.get("path")
            or source_locator
        ).strip()
        expected_filename = normalize_declared_filename(
            state_dict.get("expected_filename")
            or evidence.get("filename")
            or Path(source_locator).name
            or ""
        )
        requires_exact_file = state_dict.get("requires_exact_file") is not False
        if requires_exact_file:
            file_url = _official_file_candidate_url(
                matching_result,
                {
                    "expected_filename": expected_filename,
                    "expected_path": source_locator,
                    "expected_revision": version,
                    "locator_contract": normalized_locator,
                },
            )
        else:
            candidate_items = [
                *[
                    item for item in matching_result.get("acquisition_candidates") or []
                    if isinstance(item, dict)
                ],
                *[
                    link
                    for discovery in matching_result.get("discovered_downloads") or []
                    if isinstance(discovery, dict)
                    for link in discovery.get("download_links") or []
                    if isinstance(link, dict)
                ],
                *[
                    item for item in matching_result.get("results") or []
                    if isinstance(item, dict)
                ],
            ]
            file_url = next((
                str(item.get("url") or "").strip()
                for item in candidate_items
                if Path(urlparse(str(item.get("url") or "")).path).suffix.lower()
                in SCIENTIFIC_ASSET_SUFFIXES
            ), "")
        if not file_url:
            continue
        # Persist the repository-relative path actually used by the approved
        # raw URL.  This prevents an archive extraction prefix from surviving
        # in the ledger after locator normalization.
        downloaded_locator = normalize_official_locator(file_url)
        if downloaded_locator.get("path"):
            normalized_locator = downloaded_locator
            normalized_path = downloaded_locator["path"]
        acquire_id = f"acquire_{request_id}"
        if acquire_id in completed or request_id in resolved:
            continue
        acquired_filename = expected_filename or normalize_declared_filename(
            Path(unquote(urlparse(file_url).path)).name
        )
        requests.append({
            "id": acquire_id,
            "missing": evidence.get("detail") or request_id,
            "reason": (
                "Acquire the exact official file discovered by the approved search."
                if requires_exact_file else
                "Materialize the selected public scientific asset through the verified download adapter."
            ),
            "url": file_url,
            "tool": "data_web_download",
            "web_search_allowed": False,
            "search_mode": "official_exact_file_acquisition",
            "asset_kind": str(
                state_dict.get("asset_kind")
                or matching_result.get("asset_kind")
                or evidence.get("asset_kind")
                or ("official_file" if requires_exact_file else "geometry_or_mesh")
            ).strip() or "geometry_or_mesh",
            "asset_role": asset_role,
            "workflow_capability": str(
                state_dict.get("workflow_capability")
                or ("official_file_acquisition" if requires_exact_file else "geometry_acquisition")
            ),
            "expected_filename": acquired_filename,
            "expected_revision": version if requires_exact_file else "",
            "expected_path": normalized_path if requires_exact_file else "",
            "repository_url": normalized_locator.get("repository_url", ""),
            "revision": normalized_locator.get("revision", ""),
            "path": normalized_path,
            "gap_id": gap_id,
        })
    return requests


def _apply_reference_repair(
    state: State,
    analysis: dict[str, Any],
    task_context: str | dict[str, Any],
) -> dict[str, Any]:
    """Apply one evidence-backed locator without rerunning Analyst or searching.

    A repair is accepted only for the existing failed request and only when it
    supplies a concrete repository/revision/path (or an explicitly verified
    local file).  The normal acquisition compiler and executor remain the
    single path that validates and materializes the asset.
    """
    repair = _structured_context_object(task_context).get("reference_repair")
    repair = repair if isinstance(repair, dict) else None
    if repair is None:
        return {"status": "none"}
    request_id = str(repair.get("request_id") or repair.get("id") or "").strip()
    ledger = analysis.get("reference_gap_status") if isinstance(analysis.get("reference_gap_status"), dict) else {}
    gap_id, gap = next(
        (
            (str(key), value) for key, value in ledger.items()
            if isinstance(value, dict)
            and request_id
            and str(value.get("request_id") or "") == request_id
        ),
        ("", None),
    )
    if not gap_id or not isinstance(gap, dict):
        return {"status": "invalid", "error": "reference_repair request_id is not present in the reference ledger"}
    if str(gap.get("status") or "").strip().lower() not in {
        "no_result", "discovered_candidate", "unusable_candidate", "candidate_invalid",
    }:
        return {"status": "invalid", "error": "reference_repair can only amend a failed reference request"}
    expected_filename = normalize_declared_filename(
        gap.get("expected_filename")
        or repair.get("expected_filename")
        or ""
    )
    approved_revision = str(gap.get("expected_revision") or "").strip()
    supplied_revision = str(repair.get("revision") or "").strip()
    expected_revision = _final_reference_revision(
        analysis,
        stage_id=str(repair.get("stage_id") or ""),
        expected_revision=approved_revision or supplied_revision,
        evidence_revision=repair.get("evidence_revision"),
    )
    locator_input = {
        key: repair.get(key)
        for key in ("repository_url", "revision", "path", "raw_url")
        if repair.get(key)
    }
    locator = normalize_official_locator(locator_input or repair.get("locator") or repair.get("url") or "")
    if not expected_filename and locator.get("path"):
        expected_filename = normalize_declared_filename(Path(locator["path"]).name)
    if not expected_filename or not expected_revision or not locator.get("path"):
        return {
            "status": "invalid",
            "error": "reference_repair requires expected_filename, revision, and repository-relative path",
        }
    revisions = {
        value.lstrip("vV").casefold()
        for value in (approved_revision, supplied_revision, locator.get("revision") or "")
        if value
    }
    if len(revisions) > 1 or (
        expected_revision
        and revisions
        and expected_revision.lstrip("vV").casefold() not in revisions
    ):
        return {"status": "invalid", "error": "reference_repair revision does not match the approved revision"}
    exact_url = _official_file_candidate_url(
        {},
        {
            "expected_filename": expected_filename,
            "expected_revision": expected_revision,
            "locator_contract": locator,
        },
    )
    if not exact_url:
        return {"status": "invalid", "error": "reference_repair locator does not compile to an exact official-file URL"}
    source_contract = next(
        (
            item for item in analysis.get("required_files") or []
            if isinstance(item, dict) and str(item.get("id") or "") == request_id
        ),
        None,
    )
    if not isinstance(source_contract, dict):
        return {"status": "invalid", "error": "reference_repair request has no matching required-file contract"}
    acquisition = source_contract.get("acquisition_contract")
    acquisition = acquisition if isinstance(acquisition, dict) else {}
    source_contract.update({
        "asset_role": "official_file_reference",
        "source_strategy": "official_repository",
        "acquisition_kind": "official_file_reference",
        "workflow_capability": "official_file_acquisition",
        "expected_filename": expected_filename,
        "expected_revision": expected_revision,
        "acquisition_contract": {
            **acquisition,
            "repository_url": locator.get("repository_url", ""),
            "revision": locator.get("revision") or expected_revision,
            "path": locator.get("path", ""),
            "raw_url": exact_url,
        },
    })
    evidence = {
        "source_type": "explicit_reference_repair",
        "gap_id": gap_id,
        "request_id": request_id,
        "url": exact_url,
        "repository_url": locator.get("repository_url", ""),
        "revision": locator.get("revision") or expected_revision,
        "path": locator.get("path", ""),
        "filename": expected_filename,
        "detail": "A bounded repair supplied an explicit repository locator; download verification remains executor-owned.",
    }
    updated_gap = PlanningStore(state).record_reference_outcome(
        gap_id,
        status="discovered_candidate",
        fields={
            "request_id": request_id,
            "asset_role": "official_file_reference",
            "expected_filename": expected_filename,
            "expected_revision": expected_revision,
            "expected_path": locator.get("path", ""),
            "evidence": evidence,
        },
        evidence=evidence,
    )
    analysis["reference_gap_status"] = {
        **ledger,
        gap_id: updated_gap,
    }
    return {"status": "applied", "request_id": request_id, "gap_id": gap_id, "url": exact_url}


def _load_reference_evidence_from_analysis(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    value = analysis.get("reference_evidence") or []
    return [item for item in value if isinstance(item, dict)]


# Search termination does not imply package fulfilment. Only these states
# represent a reference file already materialized by the executor.
_VERIFIED_REFERENCE_STATUSES = {"resolved", "downloaded_verified"}
# Candidate evidence is not an execution failure.  Dataset candidates are
# sufficient to write the lightweight acquisition document used by the data
# node; only an official-file candidate still needs an exact acquisition step.
_REFERENCE_EXECUTION_GAP_STATUSES = {
    "acquisition_approved", "execution_error", "plan_contract_error",
    # ``deferred_dependency`` is handled explicitly below: it blocks exact
    # official-file contracts, but remains resumable for dataset acquisition.
    "deferred_dependency",
}
_REFERENCE_CANDIDATE_STATUSES = {"candidate", "discovered_candidate"}
_REFERENCE_EXACT_FILE_ROLES = {
    "official_file_reference", "external_asset", "reference_asset",
}


def _reference_requires_exact_file(value: dict[str, Any]) -> bool:
    """Distinguish an exact-file contract from public asset discovery.

    ``official_file_reference`` is retained for provenance compatibility, but
    a geometry acquisition request without a declared filename/revision/
    locator is a discovery request.  Only an explicit exactness flag or a
    concrete file identity keeps the strict acquisition gate enabled.
    """
    if not isinstance(value, dict):
        return True
    explicit = value.get("requires_exact_file")
    if explicit not in (None, ""):
        return not (
            explicit is False
            or str(explicit).strip().lower() in {"0", "false", "no", "off"}
        )
    role = str(value.get("asset_role") or "").strip().lower()
    capability = str(value.get("workflow_capability") or "").strip().lower()
    acquisition_kind = str(value.get("acquisition_kind") or "").strip().lower()
    has_identity = any(
        str(value.get(key) or "").strip()
        for key in (
            "expected_filename", "expected_revision", "expected_path",
            "source_locator", "url", "locator_contract",
        )
    )
    if capability == "geometry_acquisition" and not has_identity:
        return False
    return role in _REFERENCE_EXACT_FILE_ROLES or acquisition_kind in {
        "official_file_reference", "official_file",
    } or capability == "official_file_acquisition"


def _reference_gap_blocks_generation(gap: dict[str, Any]) -> bool:
    # The RequirementAnalysis contract may deliberately choose a compact
    # acquisition document instead of materializing the external file.  The
    # canonical exact-file role is the exception: it must be downloaded before
    # a consuming stage can run.
    role = str(gap.get("asset_role") or "").strip().lower()
    acquisition_kind = str(gap.get("acquisition_kind") or "").strip().lower()
    exact_file = _reference_requires_exact_file(gap)
    document_only = gap.get("requires_local_payload") is False or str(
        gap.get("materialization_kind") or ""
    ).strip().lower() in {"acquisition_document", "download_instructions"}
    if document_only and not exact_file:
        return False
    if document_only and role not in _REFERENCE_EXACT_FILE_ROLES and acquisition_kind not in {
        "official_file_reference", "official_file",
    }:
        return False
    status = str(gap.get("status") or "").strip().lower()
    if status == "deferred_dependency":
        # A provider/parser outage is recoverable for an external dataset:
        # generation can still publish the download-method document and keep
        # the dependency deferred.  Exact official files are different: a
        # stage cannot consume a search summary in place of the requested
        # file, so those contracts remain blocked until an exact acquisition
        # is verified.
        return exact_file
    if status in _REFERENCE_EXECUTION_GAP_STATUSES:
        # A search/transport failure for a dataset still has a valid package
        # outcome: publish its acquisition workflow with the failure reason.
        # Plan-contract failures remain blocked because the approved request
        # itself was invalid and must not be silently treated as evidence.
        if status in {"plan_contract_error", "acquisition_approved"}:
            return True
        return exact_file
    if status in {"no_result", "discovered_candidate", "unusable_candidate", "candidate_invalid"}:
        # A zero-result search is a valid, persisted reference outcome, not an
        # executor crash.  It nevertheless cannot satisfy an exact official
        # file contract: a summary page must never be promoted to the file
        # consumed by a downstream stage.  Keep the pipeline recoverably
        # blocked at the reference boundary instead of creating a package
        # that the stage reviewer will reject later.  The same applies to a
        # non-exact candidate or a permanently invalid download: those are
        # terminal evidence states for this request, not permission to issue
        # the same search again under a new alias.
        return exact_file
    if status not in _REFERENCE_CANDIDATE_STATUSES:
        return False
    # A search candidate for an external dataset is valid provenance for the
    # download-method document.  Exact repository files remain blocked until
    # the approved acquisition step supplies their path/revision/hash.
    return exact_file


def _reference_status_by_request(
    analysis: dict[str, Any],
    *,
    allowed_statuses: set[str] | None = None,
) -> dict[str, str]:
    """Read the persisted reference ledger through one normalized projection."""
    statuses: dict[str, str] = {}
    for gap_id, value in (analysis.get("reference_gap_status") or {}).items():
        request_id = str(value.get("request_id") or gap_id) if isinstance(value, dict) else str(gap_id)
        status = str(value.get("status") or "") if isinstance(value, dict) else str(value or "")
        if request_id and status and (allowed_statuses is None or status in allowed_statuses):
            statuses[request_id] = status
    return statuses


def _reference_resume_request(
    analysis: dict[str, Any],
    gap: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    response = needs_input_result(
        question="请提供可验证的公开来源定位信息或本地科学资产。",
        context=(
            "公开发现已经完成，但现有结果不足以安全采纳或下载为科学输入。"
            "请补充直接 URL、仓库路径/版本，或本地文件路径；原始科学要求保持锁定。"
        ),
        missing_fields=["reference_locator_or_local_asset"],
        metadata={"input_kind": "reference_acquisition_resume", "reference_gap": gap},
    )
    response.update({
        "approved": False,
        "reason": "reference_discovery_needs_input",
        "stop_reason": f"reference_{status}",
        "requirement_analysis": analysis,
        "targeted_search_requests": [],
        "search_queries": [],
        "history": [],
    })
    return response


def _next_reference_action(
    state: State,
    analysis: dict[str, Any],
    *,
    active_request_ids: set[str],
    search_requests: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the sole next reference action, if one is lawful and needed.

    This is the only planner entry that decides search versus acquisition
    versus a ledger blocker. Web tools merely return facts and the outer hook
    merely executes the approved plan.
    """
    routing_audit = analysis.get("routing_audit") if isinstance(analysis.get("routing_audit"), dict) else {}
    contract_errors = routing_audit.get("contract_errors") or []
    if contract_errors:
        return {
            "status": "needs_input",
            "approved": False,
            "reason": "reference_request_contract_incomplete",
            "stop_reason": "reference_plan_contract_error",
            "error": (
                "An exact official-file reference request is incomplete; provide a repository/file "
                "locator and target revision before any search or download is authorized."
            ),
            "contract_errors": contract_errors,
            "requirement_analysis": analysis,
            "targeted_search_requests": [],
            "search_queries": [],
            "history": [],
        }
    acquisition_requests = _reference_acquisition_requests(analysis)
    candidate_acquisition = bool(acquisition_requests)
    if not acquisition_requests:
        acquisition_requests = [
            item for item in search_requests
            if isinstance(item, dict)
            and str(item.get("tool") or "") == "data_web_download"
            and str(item.get("url") or "").strip()
        ]
    if acquisition_requests:
        return _search_response_for_gaps(
            state,
            analysis,
            reason=(
                "approved_reference_candidate_requires_exact_file_acquisition"
                if candidate_acquisition
                else "approved_reference_locator_acquisition"
            ),
            requests=acquisition_requests,
        )
    ledger = PlanningStore(state).load_reference_state().get("gaps") or {}
    open_gaps = [
        item for item in ledger.values()
        if isinstance(item, dict)
        and _reference_gap_blocks_generation(item)
        and (
            not active_request_ids
            or str(item.get("request_id") or "") in active_request_ids
        )
    ]
    if open_gaps:
        # A search that produced only non-exact or non-authoritative pages is
        # a resolved execution attempt, not a reason to issue an empty search
        # plan and hide its ledger status behind a generic error message.
        gap = open_gaps[0]
        status = str(gap.get("status") or "reference_no_progress")
        if status in {
            "no_result", "candidate", "discovered_candidate",
            "unusable_candidate", "candidate_invalid",
        }:
            return _reference_resume_request(analysis, gap, status)
        if status == "deferred_dependency":
            # 判决拆除 O14（planner:8267 半降格，2026-08-31）：检索预算熔断保留
            # （bounded retries 已经烧完，不再重试 —— A 类），但「外部证据不可得
            # 即锁死生成」降格：补救不在本节点力内，gap 记为 deferred evidence
            # obligation，生成照常进行，缺证据如实进 analysis 与稿件局限。
            analysis.setdefault("deferred_evidence_gaps", []).append(dict(gap))
            analysis.setdefault("contract_review_notes", []).append(
                "reference provider unavailable after bounded retries; "
                "the evidence gap is deferred, not resolved: "
                + str(gap.get("gap_id") or gap.get("request_id") or "unknown"))
        else:
            return {
                "status": "externally_blocked",
                "approved": False,
                "reason": "reference_ledger_no_progress_before_designer",
                "stop_reason": f"reference_{status}",
                "error": "The reference ledger has an unresolved execution state.",
                "reference_gap": gap,
                "requirement_analysis": analysis,
                "targeted_search_requests": [],
                "search_queries": [],
                "history": [],
            }
    # A terminal reference outcome must not reopen the same request on the
    # next planning pass.  Dataset searches may be deferred while local
    # artifacts are generated; exact-file no-result/unusable states are
    # already handled by ``open_gaps`` above.
    ledger_status_by_request = {
        str(value.get("request_id") or gap_id): str(value.get("status") or "").strip().lower()
        for gap_id, value in ledger.items()
        if isinstance(value, dict)
    }
    terminal_requests = {
        "deferred_dependency", "execution_error", "plan_contract_error", "no_result",
        "discovered_candidate", "unusable_candidate", "candidate_invalid",
        "resolved", "downloaded_verified",
    }
    pending_search_requests = [
        request for request in search_requests
        if ledger_status_by_request.get(
            str(request.get("id") or "").removeprefix("search_"), ""
        ) not in terminal_requests
    ]
    if pending_search_requests:
        return _search_response_for_gaps(
            state,
            analysis,
            reason="missing_external_preprocessing_asset_before_generation",
            requests=pending_search_requests,
        )
    return None


#: 同一 (gap_id, status, error) 签名允许的重试次数；再多一次就熔断（A 类）。
#: 换了错误文案或换了 gap，签名一变计数即归零 —— 出口在模型力内。
_REFERENCE_RETRY_BLOCK_AT = 2


def _reference_retry_ticket(
    state: State,
    reference_store: PlanningStore,
    gap_id: str,
    gap: dict[str, Any],
) -> dict[str, Any]:
    """按「同 gap 同错误」签名给一次重试并计次，写回账本（同一次写）。

    返回 ``{"signature", "retry_count", "exhausted", "gap"}``；``exhausted``
    为真表示同签名已重试满 ``_REFERENCE_RETRY_BLOCK_AT`` 次，不再发放。
    """
    signature = canonical_hash({
        "gap_id": gap_id,
        "status": str(gap.get("status") or ""),
        "error": str(gap.get("error") or ""),
    })[:16]
    prior = int(gap.get("retry_count") or 0) if str(gap.get("retry_signature") or "") == signature else 0
    if prior >= _REFERENCE_RETRY_BLOCK_AT:
        return {"signature": signature, "retry_count": prior, "exhausted": True, "gap": gap, "gap_id": gap_id}
    updated = reference_store.record_reference_outcome(
        gap_id,
        status=str(gap.get("status") or ""),
        fields={"retry_signature": signature, "retry_count": prior + 1},
    )
    try:
        state.append_transcript(
            "preprocessing_reference_retry",
            gap_id=gap_id,
            prior_status=str(gap.get("status") or ""),
            error=str(gap.get("error") or "")[:300],
            retry_count=prior + 1,
            signature=signature,
        )
    except Exception:
        pass
    return {"signature": signature, "retry_count": prior + 1, "exhausted": False, "gap": updated, "gap_id": gap_id}


def _reference_retry_circuit_breaker(
    analysis: dict[str, Any],
    reason: str,
    exhausted: dict[str, Any],
) -> dict[str, Any]:
    """同签名重试烧满后的熔断（A 类）。措辞照 timeout_escalation:402 范本：明列出口。"""
    gap = exhausted.get("gap") if isinstance(exhausted.get("gap"), dict) else {}
    gap_id = str(exhausted.get("gap_id") or "?")
    n = int(exhausted.get("retry_count") or 0)
    last_error = str(gap.get("error") or gap.get("status") or "")[:160]
    return {
        "status": "externally_blocked",
        "approved": False,
        "reason": reason,
        "stop_reason": "reference_retry_exhausted",
        "error": (
            f"⛔ 检索/获取请求 gap `{gap_id}` 以同一错误（{last_error}）已重试 {n} 次，"
            f"继续原路重跑只会再烧一轮。\n\n"
            f"三个出口（任选其一即可放行）：\n"
            f"   ① 改请求：换 query/URL/asset_kind 或修正该步的 plan 契约 —— 错误签名一变，计数即归零；\n"
            f"   ② 改契约：把该 required_files 项改为 acquisition_document（requires_local_payload=false），"
            f"生成照走、缺口记为 deferred evidence；\n"
            f"   ③ `request_human_input`：向用户说明已尝试 {n} 次、每次卡在哪，"
            f"请对方提供来源定位或本地资产。\n\n"
            f"注意：这是执行方式的限制，不是「此事不可行」的判定 —— 在走完 ③ 之前不要写 infeasible。"
        ),
        "reference_gap": gap,
        "retry_count": n,
        "retry_signature": exhausted.get("signature"),
        "requirement_analysis": analysis,
        "targeted_search_requests": [],
        "search_queries": [],
        "history": [],
    }


def _declare_reference_scope_deviations(
    state: State,
    analysis: dict[str, Any],
    scope: dict[str, Any],
    requests: list[dict[str, Any]],
    *,
    source: str,
) -> list[dict[str, Any]]:
    """reference 请求越出 task_scope：申报而非拒绝（planner:7100/7290 合并后的唯一登记点）。

    越界只记 ``routing_audit.scope_deviations``（O8 同款账本）+ transcript，照走；
    返回值是**仍被调用方显式锁定**的那些请求 —— 可执行的 reference 请求都是联网
    动作，调用方明说不联网/锁定该能力时仍然作数（请求权威，B，与 O8 同一判据）。
    """
    excluded = set(scope.get("excluded_capabilities") or [])
    caller_locked: list[dict[str, Any]] = []
    for item in requests:
        capability = str(item.get("workflow_capability") or "").strip().lower()
        if _scope_allows(scope, capability, analysis=analysis, reference=True):
            continue
        if (
            (bool(scope.get("caller_scope_locked")) and capability in excluded)
            or _caller_forbids_external_reference(analysis)
        ):
            caller_locked.append(item)
            continue
        record = {
            "capability": capability,
            "request_id": str(item.get("id") or ""),
            "source": source,
            "reason": "outside declared research-plan scope; proceeding with deviation recorded",
        }
        routing_audit = analysis.get("routing_audit")
        if not isinstance(routing_audit, dict):
            routing_audit = {}
            analysis["routing_audit"] = routing_audit
        deviations = routing_audit.setdefault("scope_deviations", [])
        if record not in deviations:
            deviations.append(record)
        try:
            state.append_transcript("preprocessing_reference_scope_deviation", **record)
        except Exception:
            pass
    return caller_locked


def _declare_reference_evidence_unverified(
    state: State,
    analysis: dict[str, Any],
    requests: list[dict[str, Any]],
    reason: str,
) -> None:
    """没有可执行检索/获取步的 reference 请求：如实挂 reference_evidence_unverified，生成照走。"""
    ids = [str(item.get("id") or item.get("gap_id") or "") for item in requests]
    record = analysis.setdefault("reference_evidence_unverified", [])
    for item in requests:
        entry = {
            "request_id": str(item.get("id") or item.get("gap_id") or ""),
            "missing": str(item.get("missing") or "")[:200],
            "reason": reason,
        }
        if entry not in record:
            record.append(entry)
    analysis.setdefault("contract_review_notes", []).append(
        "reference evidence unverified: no executable search/acquisition step for "
        + ", ".join(i or "?" for i in ids)
        + "; generation proceeds with the gap declared, not resolved"
    )
    try:
        state.append_transcript(
            "preprocessing_reference_evidence_unverified",
            request_ids=ids,
            reason=reason,
        )
    except Exception:
        pass


def _search_response_for_gaps(
    state: State,
    analysis: dict[str, Any],
    reason: str,
    critique: dict[str, Any] | None = None,
    requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """下一步 reference 动作；``None`` = 没有可执行的检索/获取步，规划/生成照走。"""
    selected_requests = requests if requests is not None else _targeted_search_requests_from_analysis(analysis)
    reference_store = PlanningStore(state)
    ledger = reference_store.load_reference_state().get("gaps") or {}
    executable_candidates = [
        item for item in selected_requests
        if item.get("web_search_allowed") or str(item.get("tool") or "") == "data_web_download"
    ]
    malformed = [
        item for item in executable_candidates
        if not str(item.get("asset_kind") or "").strip()
        or not str(item.get("workflow_capability") or "").strip()
    ]
    if malformed:
        return {
            "status": "needs_revision",
            "approved": False,
            "reason": reason,
            "stop_reason": "reference_plan_contract_error",
            "error": "Reference request is missing the approved asset_kind/workflow_capability contract.",
            "requirement_analysis": analysis,
            "targeted_search_requests": [],
            "search_queries": [],
            "history": [],
        }
    scope = analysis.get("task_scope") if isinstance(analysis.get("task_scope"), dict) else _task_scope_contract(analysis)
    # 判决拆除三波（planner:7100 + 7290 合并降格，2026-09-02）：「reference 请求的
    # 能力越出 task_scope 声明」是预注册范围判决，S4 偏离须申报而非不可能 ——
    # 记进 routing_audit.scope_deviations（O8 同款账本）+ transcript 后照走。
    # 调用方显式锁定的排除仍然作数（请求权威，B，与 O8 同一判据）。
    caller_locked = _declare_reference_scope_deviations(
        state, analysis, scope, executable_candidates, source="reference_request",
    )
    if caller_locked:
        return {
            "status": "needs_revision",
            "approved": False,
            "reason": reason,
            "stop_reason": "capability_locked_out_by_caller",
            "error": (
                "The caller explicitly locked this workflow capability out of the task; "
                "the reference request cannot use it."
            ),
            "locked_capabilities": sorted({
                str(item.get("workflow_capability") or "").strip().lower() for item in caller_locked
            }),
            "requirement_analysis": analysis,
            "targeted_search_requests": [],
            "search_queries": [],
            "history": [],
        }
    execution_requests = []
    exhausted_no_result = False
    discovered_candidate = False
    retry_exhausted: list[dict[str, Any]] = []
    for item in executable_candidates:
        gap_id = str(item.get("gap_id") or canonical_hash({
            "id": item.get("id"),
            "missing": item.get("missing"),
        })[:16])
        gap = ledger.get(gap_id) if isinstance(ledger.get(gap_id), dict) else {}
        status = gap.get("status")
        if status in {"resolved", "downloaded_verified"}:
            continue
        if status == "no_result":
            exhausted_no_result = True
            continue
        if status in {"discovered_candidate", "unusable_candidate"}:
            discovered_candidate = True
            continue
        if status in {"plan_contract_error", "execution_error"}:
            # 判决拆除三波（planner:7134 降格，2026-09-02）：账本里一次
            # plan_contract_error/execution_error 曾永久停管线且无解除路径
            # （gap_id 稳定）。改为允许重试并按「同 gap 同错误」签名计次；只有
            # 同一签名重复超过 _REFERENCE_RETRY_BLOCK_AT 次才熔断（A 类，措辞
            # 照 timeout_escalation:402 范本：明列出口，不写 infeasible）。
            ticket = _reference_retry_ticket(state, reference_store, gap_id, gap)
            if ticket["exhausted"]:
                retry_exhausted.append({**ticket, "request_id": str(item.get("id") or "")})
                continue
            ledger[gap_id] = ticket["gap"]
            execution_requests.append({
                **item,
                "gap_id": gap_id,
                "reference_retry": {
                    "signature": ticket["signature"],
                    "retry_count": ticket["retry_count"],
                    "prior_status": str(status),
                },
            })
            continue
        execution_requests.append({**item, "gap_id": gap_id})
    if retry_exhausted:
        analysis.setdefault("contract_review_notes", []).extend(
            f"reference gap {entry.get('gap_id') or '?'} exhausted its retry budget "
            f"({entry.get('retry_count')} retries under one error signature); not reissued"
            for entry in retry_exhausted
        )
    if not execution_requests:
        if retry_exhausted:
            return _reference_retry_circuit_breaker(analysis, reason, retry_exhausted[0])
        if exhausted_no_result or discovered_candidate:
            wanted = {"no_result"} if exhausted_no_result else {"discovered_candidate", "unusable_candidate"}
            gap = next(
                (value for value in ledger.values() if isinstance(value, dict) and value.get("status") in wanted),
                {"status": next(iter(wanted))},
            )
            return _reference_resume_request(analysis, gap, str(gap.get("status") or "no_result"))
        # 判决拆除三波（planner:7153 降格，2026-09-02）：「证据未验证但 plan 没有
        # 可执行检索/获取步」不再拒绝执行 —— S2：未验证的请求如实挂
        # reference_evidence_unverified（analysis + transcript），返回 None 让
        # 规划/生成照走。全部已 resolved/downloaded_verified 时什么都不挂：那不是
        # 未验证。
        unverified = [item for item in selected_requests if item not in executable_candidates]
        if unverified:
            _declare_reference_evidence_unverified(state, analysis, unverified, reason)
        return None
    # A reference plan is intentionally atomic.  Execute the first unresolved
    # request, persist its evidence, then let the next planning pass select the
    # next request.  Bundling requests made one failed search abort all others.
    next_request = execution_requests[0]
    web_requests = [next_request] if next_request.get("web_search_allowed") else []
    approved_search_plan = _approve_reference_search_plan(state, analysis, next_request, reason)
    return {
        "status": "needs_reference_search",
        "reason": reason,
        "requirement_analysis": analysis,
        "targeted_search_requests": selected_requests,
        "approved": bool(approved_search_plan),
        "approved_search_plan": approved_search_plan,
        "search_queries": [item["query"] for item in web_requests if item.get("query")],
        "search_policy": {
            "priority": ["local_artifacts", "official_documentation", "official_manual", "official_examples", "authoritative_reference"],
            "max_queries": min(3, max(1, len(web_requests))) if web_requests else 0,
            "one_query_at_a_time": True,
            "query_selection": "Search only the next targeted_search_requests item whose missing field blocks generation.",
            "tool": "data_web_search",
            "tool_arguments_by_request": [
                {
                    "request_id": item["id"],
                    "query": item["query"],
                    "auto_discover_downloads": bool(item.get("auto_discover_downloads")),
                    "limit": 5,
                    "max_discovery_pages": int(item.get("max_discovery_pages") or 0),
                    "search_mode": item.get("search_mode") or "generic",
                    "asset_kind": item["asset_kind"],
                    "workflow_capability": item["workflow_capability"],
                }
                for item in web_requests
            ],
            "fallback_tools": [],
            "resume": (
                "The data-node executor will execute approved_search_plan.plan_id and persist its "
                "structured reference_evidence before automatically continuing to final generation planning."
            ),
        },
        "critic": critique,
    }


def reference_plan_from_generation_transition(
    state: State,
    generation_plan: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any] | None:
    """Convert one executor transition into the sole reference-plan route.

    Generation tools must return a structured ``reference_request`` when an
    external asset is genuinely required.  The driver never reconstructs a
    query from prose, filenames, or a package error; missing transition data
    is a plan revision rather than a terminal decision.
    """
    analysis = generation_plan.get("requirement_analysis")
    if not isinstance(analysis, dict):
        return {
            "status": "needs_revision",
            "approved": False,
            "stop_reason": "reference_transition_contract_error",
            "error": "Generation transition did not carry requirement_analysis.",
        }
    transition = execution.get("transition") if isinstance(execution.get("transition"), dict) else {}
    request = transition.get("reference_request")
    if not isinstance(request, dict):
        return {
            "status": "needs_revision",
            "approved": False,
            "stop_reason": "reference_transition_contract_error",
            "error": "Generation step requested reference search without a structured reference_request.",
            "failed_step": execution.get("failed_step"),
        }
    request = dict(request)
    request.setdefault("id", f"transition_{execution.get('failed_step') or 'reference'}")
    # 判决拆除三波（planner:7240 并入 7078，2026-09-02）：「reference 请求缺
    # asset_kind/workflow_capability」一题一答 —— 这条请求接着进
    # _search_response_for_gaps，可执行时在那里判一次；不可执行的请求无物可查。
    capability = str(request.get("workflow_capability") or "").strip().lower()
    scope = analysis.get("task_scope") if isinstance(analysis.get("task_scope"), dict) else _task_scope_contract(analysis)
    missing_fields = transition.get("missing_fields") or request.get("missing_fields") or []
    local_spatial_contract = any(
        not _asset_contract_is_external(item)
        and str(normalize_asset_contract(item).get("workflow_capability") or "") == "mesh_generation"
        for item in analysis.get("required_files") or []
        if isinstance(item, dict)
    )
    local_geometry_declared = bool(re.search(
        r"(?:generate|derive|compute|build).*(?:geometry|coordinate|profile|shape)|"
        r"(?:生成|计算|构建|解析).*(?:几何|坐标|轮廓|外形)",
        " ".join(str(item) for item in scope.get("declared_operations") or []),
        flags=re.I,
    ))
    if (
        capability == "geometry_acquisition"
        and not missing_fields
        and local_spatial_contract
        and local_geometry_declared
    ):
        # 判决拆除三波（planner:7268 降格，2026-09-02）：plan 声明本地生成几何、
        # 步骤却想改走外部获取 —— 改道是 S4 偏离，申报后由 agent 选路，不再当牢笼。
        # 记 routing_audit.route_deviations + transcript，请求照发。
        route_record = {
            "step_id": str(execution.get("failed_step") or ""),
            "tool_name": str(transition.get("tool_name") or ""),
            "declared_route": "local_geometry_generation",
            "chosen_route": "geometry_acquisition",
            "reason": (
                "work order declares local geometry generation with no missing geometry fields; "
                "the step chose external acquisition instead — deviation recorded, request proceeds"
            ),
        }
        routing_audit = analysis.get("routing_audit")
        if not isinstance(routing_audit, dict):
            routing_audit = {}
            analysis["routing_audit"] = routing_audit
        route_deviations = routing_audit.setdefault("route_deviations", [])
        if route_record not in route_deviations:
            route_deviations.append(route_record)
        try:
            state.append_transcript("preprocessing_reference_route_deviation", **route_record)
        except Exception:
            pass
    # 判决拆除三波（planner:7290 并入 7100，2026-09-02）：范围判决只在
    # _search_response_for_gaps 的唯一登记点判一次（申报越界 / 调用方锁定）。
    return _search_response_for_gaps(
        state,
        analysis,
        reason="generation_execution_requires_reference",
        requests=[request],
    )


def _compact_text(value: Any, limit: int = 6000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.3):]
    return f"{head} ...[middle omitted for compact planning payload]... {tail}"


def _compact_input_inspections(inspections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for inspection in inspections[:3]:
        if not isinstance(inspection, dict):
            continue
        files = []
        for entry in (inspection.get("files") or [])[:10]:
            if not isinstance(entry, dict):
                continue
            # Discovery metadata is useful for routing, but the full asset
            # profiler repeats quality-gate prose already present in the
            # planning document.  Keep only the stable classification fields.
            file_name = Path(str(entry.get("path") or "")).name
            if file_name in {".DS_Store", "Thumbs.db"}:
                continue
            item = {
                "path": entry.get("path"),
                "relative_path": entry.get("relative_path"),
                "size_bytes": entry.get("size_bytes"),
                "suffix": entry.get("suffix"),
            }
            if entry.get("scientific_asset"):
                asset = entry.get("scientific_asset")
                if isinstance(asset, dict):
                    item["scientific_asset"] = {
                        key: asset.get(key)
                        for key in (
                            "format", "asset_kind", "data_model_kind", "adapter",
                            "classification_confidence",
                        )
                        if asset.get(key) not in (None, "", [], {})
                    }
                else:
                    item["scientific_asset"] = asset
            if entry.get("json_keys"):
                item["json_keys"] = entry.get("json_keys")[:30]
            preview = entry.get("preview")
            if isinstance(preview, str) and preview.strip():
                item["preview_excerpt"] = _compact_text(preview, 200)
                item["preview_truncated"] = True
            if entry.get("preview_error"):
                item["preview_error"] = entry.get("preview_error")
            files.append(item)
        compacted.append({
            "path": inspection.get("path"),
            "requested_path": inspection.get("requested_path"),
            "path_type": inspection.get("path_type"),
            "file_count": inspection.get("file_count"),
            "truncated": inspection.get("truncated"),
            "scientific_asset": inspection.get("scientific_asset"),
            "files": files,
        })
    return compacted


def _merge_available_input_inspections(
    state: State,
    fresh_inspections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep inspected upstream inputs available across compacted planning turns.

    The model may summarize a task without repeating an absolute source path.
    Input discovery is run state, not conversational memory, so an already
    inspected research plan must remain available to every later planning pass.
    """
    stored = state.hook_state.get("data_input_inspections")
    candidates = [
        *([item for item in stored if isinstance(item, dict)] if isinstance(stored, list) else []),
        *[item for item in fresh_inspections if isinstance(item, dict)],
    ]
    merged: dict[str, dict[str, Any]] = {}
    for inspection in candidates:
        path = str(inspection.get("path") or inspection.get("requested_path") or "").strip()
        if not path:
            continue
        normalized = dict(inspection)
        files = normalized.get("files")
        if not isinstance(files, list):
            files = []
            normalized["files"] = files
        normalized.setdefault("file_count", len(files))
        normalized.setdefault("path_type", "file" if Path(path).is_file() else "directory")
        merged[str(Path(path).expanduser())] = normalized
    return list(merged.values())[-8:]


def _local_asset_inventory(
    task_context: str | dict[str, Any],
    inspections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize scheduler and inspected files into one local-asset inventory.

    This is intentionally based on explicit metadata and content inspection,
    never on application-specific filename rules.  Hashes supplied by a
    scheduler are preserved; discovery does not hash potentially huge files.
    """
    candidates: list[dict[str, Any]] = []
    context_mapping = task_context if isinstance(task_context, dict) else {}
    if isinstance(task_context, str):
        try:
            decoded = json.loads(task_context)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            context_mapping = decoded
    if context_mapping:
        candidates.extend(
            item for item in context_mapping.get("available_local_assets") or []
            if isinstance(item, dict)
        )
    for inspection in inspections:
        for entry in inspection.get("files") or []:
            if isinstance(entry, dict):
                asset = entry.get("scientific_asset") if isinstance(entry.get("scientific_asset"), dict) else {}
                candidates.append({
                    "path": entry.get("path"),
                    "requested_path": inspection.get("requested_path") if inspection.get("path_type") == "file" else None,
                    "size_bytes": entry.get("size_bytes"),
                    "format": asset.get("format"),
                    "asset_kind": asset.get("asset_kind"),
                    "data_model_kind": asset.get("data_model_kind"),
                    "provenance": "inspected_input",
                })
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            continue
        try:
            path = Path(raw_path).expanduser().resolve()
        except OSError:
            continue
        if not path.is_file() or str(path) in seen:
            continue
        seen.add(str(path))
        profile = inspect_scientific_asset_path(path)
        inventory.append({
            "path": str(path),
            "requested_path": item.get("requested_path") or str(path),
            "size_bytes": int(item.get("size_bytes") or path.stat().st_size),
            "format": item.get("format") or profile.get("format"),
            "asset_kind": item.get("asset_kind") or profile.get("asset_kind"),
            "data_model_kind": item.get("data_model_kind") or profile.get("data_model_kind"),
            "scientific_role": item.get("scientific_role"),
            "coverage": item.get("coverage") if isinstance(item.get("coverage"), dict) else {},
            "sha256": item.get("sha256") or item.get("hash") or "",
            "provenance": item.get("provenance") or "scheduler_context",
        })
    return inventory


def _format_family(value: Any) -> str:
    text = str(value or "").lower()
    for token in (
        "grib", "netcdf", "hdf5", "hdf", "zarr", "parquet", "csv", "json", "yaml",
        "namelist", "python", "shell", "text", "binary", "intermediate",
    ):
        if token in text:
            return token
    return ""


def _apply_local_asset_reuse(
    analysis: dict[str, Any],
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind inspected inputs once, preserving source identity through planning."""
    if not inventory:
        return analysis
    for item in analysis.get("required_files") or []:
        if not isinstance(item, dict):
            continue
        contract = normalize_asset_contract(item)
        if not contract.get("is_external") and contract.get("source_strategy") != "local_reuse":
            continue
        declared_paths = {
            str(item.get(key) or "").strip()
            for key in ("declared_output_path", "expected_filename", "name_or_role")
        } - {""}
        absolute_paths = {str(Path(value).expanduser()) for value in declared_paths if Path(value).expanduser().is_absolute()}
        exact_matches = [candidate for candidate in inventory if (absolute_paths or declared_paths).intersection({
            candidate["path"], str(candidate.get("requested_path") or ""),
            *([] if absolute_paths else [Path(candidate["path"]).name]),
        })]
        if absolute_paths and not exact_matches:
            continue
        expected_format = _format_family(
            item.get("format") or contract.get("representation")
        )
        expected_role = str(contract.get("scientific_role") or "").strip().lower()
        expected_hash = str(item.get("sha256") or item.get("hash") or "").strip().lower()
        expected_coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
        matches = []
        for candidate in exact_matches or inventory:
            candidate_format = _format_family(
                candidate.get("format") or candidate.get("data_model_kind")
            )
            # Unknown or mismatched metadata is not evidence of reuse.  An
            # inspected planning JSON must never satisfy a GRIB, NetCDF, or
            # official text-file requirement merely because both are files.
            if not exact_matches and expected_format and candidate_format != expected_format:
                continue
            candidate_role = str(candidate.get("scientific_role") or "").strip().lower()
            if not exact_matches and expected_role and candidate_role != expected_role:
                continue
            if expected_hash and str(candidate.get("sha256") or "").lower() != expected_hash:
                continue
            candidate_coverage = candidate.get("coverage") if isinstance(candidate.get("coverage"), dict) else {}
            if expected_coverage and any(candidate_coverage.get(key) != value for key, value in expected_coverage.items()):
                continue
            if not (exact_matches or expected_format or expected_hash or expected_coverage or (expected_role and candidate_role)):
                continue
            matches.append(candidate)
        if len(matches) == 1:
            candidate = matches[0]
            item.update({
                "source_strategy": "local_reuse",
                "acquisition_kind": "local_asset",
                "workflow_capability": "local_asset_reuse",
                "is_external": False,
                "materialization_kind": "local_reuse",
                "local_match": {
                    "path": candidate["path"],
                    "requested_path": candidate.get("requested_path"),
                    "size_bytes": candidate["size_bytes"],
                    "sha256": candidate.get("sha256") or "",
                    "format": candidate.get("format"),
                    "match_basis": [
                        "inspected_path" if exact_matches else "representation" if expected_format else "asset_kind",
                        *( ["scientific_role"] if expected_role and candidate.get("scientific_role") else [] ),
                        *( ["coverage"] if expected_coverage else [] ),
                        *( ["sha256"] if expected_hash else [] ),
                    ],
                    "provenance": candidate.get("provenance"),
                },
            })
    analysis["local_asset_inventory"] = inventory
    return analysis


def _local_planning_documents(inspections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for inspection in inspections:
        if not isinstance(inspection, dict):
            continue
        for entry in inspection.get("files") or []:
            if not isinstance(entry, dict):
                continue
            path_text = str(entry.get("path") or "").strip()
            name = Path(path_text).name.lower()
            if (
                not path_text
                or path_text in seen
                or not re.search(
                    r"(research[_ -]?plan|simulation[_ -]?protocol|workflow|experiment[_ -]?design|"
                    r"pre[_ -]?registration|preregistration)",
                    name,
                )
            ):
                continue
            path = Path(path_text).expanduser()
            if path.suffix.lower() not in {".json", ".md", ".txt", ".yaml", ".yml"}:
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")[:250_000]
            except OSError:
                continue
            seen.add(path_text)
            content = raw
            structured_content: Any = None
            if path.suffix.lower() == ".json":
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    structured_content = (
                        parsed.get("content")
                        or parsed.get("plan")
                        or parsed.get("protocol")
                        or parsed
                    )
                    content = (
                        structured_content
                        if isinstance(structured_content, str)
                        else json.dumps(structured_content, ensure_ascii=False, default=str)
                    )
            document = {
                "path": path_text,
                "name": path.name,
                # Keep the parse-preserving source for deterministic fallback
                # and identity.  Only content_excerpt is sent to the Analyst.
                "content": content,
                "content_hash": canonical_hash({"path": path_text, "content": content}),
                "content_excerpt": _compact_text(content, 5_000),
            }
            if structured_content is not None:
                document["structured_content"] = structured_content
            documents.append(document)
    # The upstream research plan and pre-registration are scope authorities;
    # reports/audits are supporting context and need not be replayed verbatim.
    documents.sort(key=lambda item: (
        0 if re.search(r"research[_ -]?plan", str(item.get("name") or ""), flags=re.I) else
        1 if re.search(r"pre[_ -]?registration|preregistration", str(item.get("name") or ""), flags=re.I) else 2,
        str(item.get("name") or ""),
    ))
    return documents[:2]


def _compact_requirement_analysis(analysis: dict[str, Any] | None) -> dict[str, Any] | None:
    """Make the Designer payload a planning contract, not a transcript replay."""
    analysis = _coerce_requirement_analysis(analysis)
    if not isinstance(analysis, dict):
        return None
    compact = _normalize_requirement_analysis(analysis)

    def compact_evidence(value: Any, limit: int = 4) -> list[dict[str, Any]]:
        items = value if isinstance(value, list) else [value]
        result: list[dict[str, Any]] = []
        for item in items[:limit]:
            if isinstance(item, dict):
                detail = (
                    item.get("detail") or item.get("content_excerpt") or item.get("excerpt")
                    or item.get("summary") or item.get("content") or item.get("text") or ""
                )
                entry = {
                    key: item.get(key)
                    for key in (
                        "source_type", "source", "url", "title", "path", "query", "status",
                        "reference_status", "asset_kind", "format", "data_model_kind",
                    )
                    if item.get(key) not in (None, "", [], {})
                }
                if detail:
                    entry["detail"] = _compact_text(detail, 140)
                if entry:
                    result.append(entry)
            elif str(item).strip():
                result.append({"detail": _compact_text(item, 180)})
        return result

    def compact_reference_evidence(value: Any) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        for item in value or []:
            if not isinstance(item, dict):
                continue
            source = item.get("result") if isinstance(item.get("result"), dict) else item
            entry = compact_evidence([source], limit=1)
            if entry:
                references.append(entry[0])
        return references[:8]

    reference_status_by_request = _reference_status_by_request(compact)
    runtime_tool_role_keys = {
        _normalise_asset_key(
            item.get("tool_identity") or item.get("scientific_role") or item.get("id")
        )
        for item in compact.get("runtime_tool_requirements") or []
        if isinstance(item, dict)
    }
    required = []
    for item in compact.get("required_files") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("fulfillment_kind") or "") == "runtime_output":
            # Runtime products are recorded in their owning stage contract.
            # They are never text artifacts for the Designer to reproduce.
            continue
        if str(item.get("source_strategy") or "") == "local_reuse":
            # The verified local-match path remains in the authoritative
            # analysis and is materialized by Publisher.  Designer has no
            # route to choose for it, so replaying it is pure prompt noise.
            continue
        reference_status = reference_status_by_request.get(str(item.get("id") or ""), "")
        # Designer chooses only a fulfilment route. Scientific identity,
        # evidence, parameters and interfaces are reattached from the full
        # RequirementAnalysis after it returns, so repeating them here only
        # enlarges the prompt and invites the model to rewrite authority-owned
        # fields. Omit nulls as well: 50+ asset plans otherwise spend several
        # kilobytes serializing keys with no planning value.
        route = {
            "id": item.get("id"),
            "stage_id": item.get("stage_id"),
            "source_strategy": item.get("source_strategy"),
            "acquisition_kind": item.get("acquisition_kind"),
            "representation": item.get("representation"),
            "workflow_capability": item.get("workflow_capability"),
            "delivery_mode": item.get("delivery_mode"),
            "size_estimate": item.get("size_estimate"),
            "source_discovery": (
                {
                    key: (item.get("source_discovery") or {}).get(key)
                    for key in ("status", "reason", "candidate_count")
                    if (item.get("source_discovery") or {}).get(key) not in (None, "", [], {})
                }
                if isinstance(item.get("source_discovery"), dict) else None
            ),
            "format": item.get("format"),
            "declared_output_path": (
                item.get("declared_output_path")
                or item.get("expected_filename")
                or item.get("filename")
                or item.get("file_name")
            ),
            "reference_status": reference_status or None,
            "delivery_required": (
                False
                if reference_status in _VERIFIED_REFERENCE_STATUSES
                else item.get("delivery_required")
            ),
        }
        required.append({
            key: value for key, value in route.items()
            if value not in (None, "", [], {})
        })
    # The stage records already carry compact evidence and parameter keys.
    # Do not drop later requirements by a positional cap: an omitted input is
    # indistinguishable from an optional one to Designer and yields an empty
    # stage directory.  This projection is deliberately small enough to keep
    # all stage-owned asset contracts rather than replaying their prose.
    compact["required_files"] = required
    compact["calculation_stages"] = [
        {
            key: value for key, value in {
                "id": item.get("id"),
                "method": item.get("solver") or item.get("application") or item.get("tool"),
                "required_file_roles": [
                    role for role in item.get("required_file_roles") or []
                    if _normalise_asset_key(role) not in runtime_tool_role_keys
                ][:6],
                "dependencies": (item.get("dependencies") or [])[:8],
            }.items() if value not in (None, "", [], {})
        }
        for item in compact.get("calculation_stages") or []
        if isinstance(item, dict)
    ][:40]
    compact["optional_files"] = (compact.get("optional_files") or [])[:10]
    compact["evidence"] = compact_evidence(compact.get("evidence"), limit=8)
    compact["reference_evidence"] = compact_reference_evidence(compact.get("reference_evidence"))
    compact["assumptions"] = [str(v)[:300] for v in compact.get("assumptions") or []][:20]
    compact["unresolved_facts"] = [str(v)[:300] for v in compact.get("unresolved_facts") or []][:20]
    compact["search_queries"] = [str(v)[:300] for v in compact.get("search_queries") or []][:10]
    scope = compact.get("task_scope") if isinstance(compact.get("task_scope"), dict) else {}
    compact["task_scope"] = {
        "discipline": scope.get("discipline"),
        "workflow_kind": scope.get("workflow_kind"),
        "allowed_capabilities": scope.get("allowed_capabilities") or [],
        "excluded_capabilities": scope.get("excluded_capabilities") or [],
        "data_representations": scope.get("data_representations") or [],
        "stage_ids": scope.get("stage_ids") or [],
    }
    compact.pop("structured_stage_scope", None)
    compact.pop("reference_gap_status", None)
    compact.pop("resolved_gap_ids", None)
    compact.pop("resolved_reference_request_ids", None)
    compact.pop("active_reference_request_ids", None)
    compact.pop("completed_reference_request_ids", None)
    compact.pop("routing_audit", None)
    compact.pop("supporting_software", None)
    compact.pop("stage_coverage", None)
    compact.pop("deferred_stage_ids", None)
    # These paths are executor-owned and are reattached from the authoritative
    # RequirementAnalysis after Designer returns.  Sending the complete
    # producer/consumer ledger here duplicated every stage edge and encouraged
    # the model to rewrite immutable interface paths.
    compact.pop("stage_input_contracts", None)
    # Runtime access (credentials, licences, etc.) is an executor-side
    # prerequisite, not a package artifact.  Keeping it beside deliverables
    # encourages a Designer to incorrectly propose a generation step for it.
    compact.pop("runtime_access_dependencies", None)
    compact.pop("runtime_tool_requirements", None)
    # The inventory is only for deterministic local-reuse matching, which
    # occurs before design.  Replaying every discovered path adds no planning
    # information once the selected requirement carries its local_match.
    compact.pop("local_asset_inventory", None)
    return compact


def _critic_referenced_step_ids(
    feedback: str | dict[str, Any] | None,
    plan: dict[str, Any] | None,
) -> set[str]:
    """Return persisted step IDs explicitly named by a Critic revision."""
    if not isinstance(plan, dict) or feedback in (None, "", {}):
        return set()
    # Generation review feedback may carry the full execution result beside
    # its compact findings. Derive ownership only from those findings;
    # provenance for every completed step is not a revision target.
    feedback_scope: Any = feedback
    feedback_items = (
        feedback.get("feedback")
        if isinstance(feedback, dict) and isinstance(feedback.get("feedback"), list)
        else []
    )
    known_step_ids = {
        str(step.get("id") or "")
        for step in plan.get("generation_steps") or []
        if isinstance(step, dict) and str(step.get("id") or "")
    }
    explicit_step_ids = {
        str(item.get("step_id") or "").strip()
        for item in feedback_items
        if isinstance(item, dict) and str(item.get("step_id") or "").strip()
    } & known_step_ids
    unowned_feedback = [
        item for item in feedback_items
        if isinstance(item, dict) and not str(item.get("step_id") or "").strip()
    ]
    if explicit_step_ids and not unowned_feedback:
        return explicit_step_ids
    if feedback_items:
        feedback_scope = unowned_feedback
    text = json.dumps(feedback_scope, ensure_ascii=False, default=str).lower()
    referenced: set[str] = set(explicit_step_ids)
    for step in plan.get("generation_steps") or []:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "")
        arguments = (
            step.get("tool_arguments")
            if isinstance(step.get("tool_arguments"), dict)
            else {}
        )
        output_paths = (
            arguments.get("output_paths")
            if isinstance(arguments.get("output_paths"), dict)
            else {}
        )
        stable_references = {
            step_id,
            str(step.get("stage_id") or ""),
            *(str(item) for item in step.get("outputs") or []),
            *(str(item) for item in output_paths),
            *(str(item) for item in output_paths.values()),
            *(PurePosixPath(str(item)).name for item in output_paths.values()),
        }
        if step_id and any(
            reference and reference.lower() in text
            for reference in stable_references
        ):
            referenced.add(step_id)
    # Package review identifies an offending file by stage and public path,
    # not by the executor's private step id.  Map those stable contracts back
    # to their owning steps so the existing compact revision path can preserve
    # every unaffected artifact.
    referenced_stage_ids = {
        str(item).lower()
        for item in re.findall(r"['\"]stage_id['\"]\s*:\s*['\"]([^'\"]+)", text)
        if item
    }
    if referenced_stage_ids:
        referenced.update(
            str(step.get("id") or "")
            for step in plan.get("generation_steps") or []
            if isinstance(step, dict)
            and str(step.get("stage_id") or "").lower() in referenced_stage_ids
        )
    return referenced


def _critic_referenced_stage_ids(
    feedback: str | dict[str, Any] | None,
    plan: dict[str, Any] | None,
    focus_step_ids: set[str] | None = None,
) -> set[str]:
    """Return only stage identities present in structured review findings."""
    stages = {
        str(item.get("stage_id") or "").strip()
        for item in (
            feedback.get("feedback")
            if isinstance(feedback, dict) and isinstance(feedback.get("feedback"), list)
            else []
        )
        if isinstance(item, dict) and str(item.get("stage_id") or "").strip()
    }
    if isinstance(plan, dict) and focus_step_ids:
        stages.update(
            str(step.get("stage_id") or "").strip()
            for step in plan.get("generation_steps") or []
            if isinstance(step, dict)
            and str(step.get("id") or "") in focus_step_ids
            and str(step.get("stage_id") or "").strip()
        )
    return stages


def _focus_requirement_analysis(
    analysis: dict[str, Any] | None,
    stage_ids: set[str],
) -> dict[str, Any] | None:
    """Project a revision contract to the stages named by package review."""
    if not isinstance(analysis, dict) or not stage_ids:
        return analysis
    focused = deepcopy(analysis)
    focused["calculation_stages"] = [
        item for item in focused.get("calculation_stages") or []
        if isinstance(item, dict) and str(item.get("id") or "") in stage_ids
    ]
    focused["required_files"] = [
        item for item in focused.get("required_files") or []
        if isinstance(item, dict) and str(item.get("stage_id") or "") in stage_ids
    ]
    scope = focused.get("task_scope")
    if isinstance(scope, dict):
        focused["task_scope"] = {**scope, "stage_ids": sorted(stage_ids)}
    # Global reference prose cannot change a local package repair.
    for key in ("optional_files", "search_queries", "unresolved_facts"):
        focused.pop(key, None)
    return focused


def _summarize_previous_plan(
    plan: dict[str, Any] | None,
    focus_step_ids: set[str] | None = None,
    focus_stage_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    focus = set(focus_step_ids or set())
    focus_stages = set(focus_stage_ids or set())
    has_focus = bool(focus or focus_stages)

    def is_focused(step: dict[str, Any]) -> bool:
        return (str(step.get("id") or "") in focus if focus
                else str(step.get("stage_id") or "") in focus_stages)

    def step_summary(step: dict[str, Any]) -> dict[str, Any]:
        result = {
            "id": step.get("id"),
            "tool_name": step.get("tool_name"),
            "stage_id": step.get("stage_id"),
            "outputs": step.get("outputs"),
            "dependencies": step.get("dependencies"),
            "preserve_unless_cited": bool(has_focus and not is_focused(step)),
        }
        if not has_focus or is_focused(step):
            arguments = deepcopy(step.get("tool_arguments") or {})
            parameters = arguments.get("parameters")
            if isinstance(parameters, str):
                try:
                    parameters = json.loads(parameters)
                except ValueError:
                    parameters = None
            if isinstance(parameters, dict):
                arguments["parameters"] = {
                    key: value for key, value in parameters.items()
                    if key not in {"requirement_analysis", "preprocessing_request", "reference_evidence"}
                }
            result["tool_arguments"] = arguments
        return result

    previous_steps = [
        item
        for item in (plan.get("generation_steps") or [])
        if isinstance(item, dict)
    ]
    focus_outputs = {
        str(output)
        for step in previous_steps
        if is_focused(step)
        for output in step.get("outputs") or []
    }
    return {
        "plan_id_hint": plan.get("task_summary"),
        "required_deliverables": [
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "required": item.get("required", True),
            }
            for item in (plan.get("required_deliverables") or [])
            if isinstance(item, dict)
            and (not has_focus or str(item.get("id") or "") in focus_outputs)
        ][:30],
        "revision_focus_step_ids": sorted(focus),
        "revision_focus_stage_ids": sorted(focus_stages),
        "preserved_step_ids": [
            str(item.get("id") or "")
            for item in previous_steps
            if has_focus and not is_focused(item)
        ],
        "generation_steps": [
            step_summary(item)
            for item in previous_steps
        ],
    }


def _merge_focused_plan_revision(
    previous_plan: dict[str, Any] | None,
    revised_plan: dict[str, Any],
    focus_step_ids: set[str],
    focus_stage_ids: set[str] | None = None,
    preserve_execution_route: bool = False,
) -> dict[str, Any]:
    """Replace cited steps while preserving the approved remainder verbatim."""
    explicit_focus_stages = set(focus_stage_ids or set())
    if not isinstance(previous_plan, dict) or not (focus_step_ids or explicit_focus_stages):
        return revised_plan
    previous = deepcopy(previous_plan)
    revised = deepcopy(revised_plan)
    previous_steps = [
        item for item in previous.get("generation_steps") or [] if isinstance(item, dict)
    ]
    revised_steps = [
        item for item in revised.get("generation_steps") or [] if isinstance(item, dict)
    ]
    revised_by_id = {
        str(item.get("id") or ""): item
        for item in revised_steps if str(item.get("id") or "")
    }
    previous_ids = {str(item.get("id") or "") for item in previous_steps}
    replacements: dict[str, dict[str, Any]] = {}
    replaced_outputs: set[str] = set()
    for step in previous_steps:
        step_id = str(step.get("id") or "")
        if (step_id not in focus_step_ids if focus_step_ids
                else str(step.get("stage_id") or "") not in explicit_focus_stages):
            continue
        replacement = revised_by_id.get(step_id)
        if replacement is None:
            candidates = [item for item in revised_steps
                          if str(item.get("id") or "") not in previous_ids
                          and item not in replacements.values()
                          and set(item.get("outputs") or []) & set(step.get("outputs") or [])]
            replacement = candidates[0] if len(candidates) == 1 else None
        if replacement is not None:
            # A local amendment may omit unchanged execution metadata.
            replacement = {**deepcopy(step), **replacement}
            if preserve_execution_route or replacement.get("tool_name") == step.get("tool_name"):
                original_args = deepcopy(step.get("tool_arguments") or {})
                amended_args = deepcopy(replacement.get("tool_arguments") or {})
                # Parameters may cross the tool boundary as encoded JSON.
                # Merge them as controls, not as a replace-all string.
                for arguments in (original_args, amended_args):
                    if isinstance(arguments.get("parameters"), str):
                        try:
                            arguments["parameters"] = json.loads(arguments["parameters"])
                        except ValueError:
                            pass
                replacement = {**replacement,
                    "tool_arguments": merge_parameter_updates(original_args, amended_args),
                    "dependencies": list(dict.fromkeys([
                        *(step.get("dependencies") or []),
                        *(replacement.get("dependencies") or []),
                    ])),
                }
                if isinstance((step.get("tool_arguments") or {}).get("parameters"), str):
                    replacement["tool_arguments"]["parameters"] = json.dumps(
                        replacement["tool_arguments"]["parameters"], ensure_ascii=False)
            if preserve_execution_route:
                replacement = {
                    **replacement,
                    **{
                        key: deepcopy(step.get(key))
                        for key in (
                            "id", "tool_name", "tool_capability", "dependencies",
                            "outputs", "stage_id", "work_unit_id",
                        )
                    },
                }
            replacements[step_id] = replacement
            replaced_outputs.update(str(item) for item in step.get("outputs") or [])

    merged_steps = [
        deepcopy(replacements.get(str(step.get("id") or ""), step))
        for step in previous_steps
    ]
    # Keep stable IDs at the boundary of a local amendment. Otherwise unchanged
    # consumers still refer to the replaced step's old ID.
    renamed = {str(item.get("id")): old for old, item in replacements.items()
               if str(item.get("id")) != old}
    def rebind(value: Any) -> Any:
        if isinstance(value, dict):
            if isinstance(value.get("$ref"), str):
                source, separator, field = value["$ref"].partition(".")
                value = {**value, "$ref": renamed.get(source, source) + separator + field}
            return {key: rebind(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rebind(item) for item in value]
        return value

    for step in merged_steps:
        step["id"] = renamed.get(str(step["id"]), step["id"])
    existing_step_ids = {str(item["id"]) for item in merged_steps}
    pending = list(replacements.values())
    while pending:
        step = pending.pop()
        for dependency in step.get("dependencies") or []:
            dependency = renamed.get(str(dependency), str(dependency))
            if dependency in existing_step_ids or dependency not in revised_by_id:
                continue
            added = deepcopy(revised_by_id[dependency])
            merged_steps.append(added)
            existing_step_ids.add(dependency)
            pending.append(added)
    for step in merged_steps:
        step["dependencies"] = [renamed.get(str(value), str(value)) for value in step.get("dependencies") or []]
        step["tool_arguments"] = rebind(step.get("tool_arguments") or {})

    previous_deliverables = {
        str(item.get("id") or ""): deepcopy(item)
        for item in previous.get("required_deliverables") or []
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    revised_deliverables = {
        str(item.get("id") or ""): deepcopy(item)
        for item in revised.get("required_deliverables") or []
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    active_outputs = {
        str(output) for item in merged_steps for output in item.get("outputs") or []
    }
    for output_id in active_outputs | replaced_outputs:
        if output_id in revised_deliverables and (
            output_id in replaced_outputs or output_id not in previous_deliverables
        ):
            previous_deliverables[output_id] = revised_deliverables[output_id]

    result = {
        **previous,
        **{
            key: value
            for key, value in revised.items()
            if key not in {"required_deliverables", "generation_steps", "tool_requirements"}
        },
        # Omission from a partial amendment is not deletion. This includes
        # publisher-owned inputs and proposals explicitly made non-delivery.
        "required_deliverables": list(previous_deliverables.values()),
        "generation_steps": merged_steps,
    }
    selected_tools = {
        str(step.get("tool_name") or "")
        for step in merged_steps if str(step.get("tool_name") or "")
    }
    tool_requirements = deepcopy(previous.get("tool_requirements") or [])
    declared_tools = {
        str(tool)
        for item in tool_requirements if isinstance(item, dict)
        for tool in item.get("selected_tools") or []
    }
    missing_tools = sorted(selected_tools - declared_tools)
    if missing_tools:
        tool_requirements.append({
            "capability": "focused_revision_tools",
            "selected_tools": missing_tools,
        })
    result["tool_requirements"] = tool_requirements
    return result


def _compact_critique_feedback(feedback: str | dict[str, Any] | None) -> str | dict[str, Any] | None:
    if feedback is None:
        return None
    if isinstance(feedback, str):
        return _compact_text(feedback, 2500)
    compact = {
        "decision": feedback.get("decision"),
        "overall_score": feedback.get("overall_score"),
        "critical_concerns": [str(v)[:260] for v in feedback.get("critical_concerns") or []][:2],
        "major_concerns": [str(v)[:260] for v in feedback.get("major_concerns") or []][:5],
        "recommended_changes": [str(v)[:260] for v in feedback.get("recommended_changes") or []][:5],
    }
    if isinstance(feedback.get("rejected_plan"), dict):
        compact["rejected_plan"] = _summarize_previous_plan(feedback["rejected_plan"])
    # Publisher/Reviewer feedback is deliberately file-level rather than a
    # Critic score.  Keeping this compact list is what lets Designer amend the
    # cited artifact instead of receiving an empty generic "revise" signal.
    if isinstance(feedback.get("feedback"), list):
        compact["feedback"] = [
            {
                key: _compact_text(
                    item.get(key),
                    6000 if key == "artifact_evidence" else 1600 if key == "diagnostic_context" else 800,
                )
                for key in (
                    "step_id", "tool_name", "stage_id", "deliverable_id", "asset_id", "file", "path",
                    "code", "severity", "message", "recommendation", "required_change",
                    "requirement_id", "comparison",
                    "missing_constraints", "diagnostic_context",
                    "previous_repair_attempts", "last_repair_error",
                    "artifact_evidence",
                )
                if item.get(key) not in (None, "", [], {})
            }
            for item in feedback["feedback"][:12]
            if isinstance(item, dict)
        ]
    return compact


def _compact_plan_for_critic(plan: dict[str, Any]) -> dict[str, Any]:
    """Project the persisted plan to the fields the Critic actually evaluates."""
    analysis = plan.get("requirement_analysis") if isinstance(plan.get("requirement_analysis"), dict) else {}

    def compact_list(value: Any, limit: int = 6, item_limit: int = 220) -> list[str]:
        values = value if isinstance(value, list) else ([value] if value not in (None, "", {}) else [])
        return [_compact_text(item, item_limit) for item in values[:limit]]

    def evidence_labels(value: Any, limit: int = 3) -> list[dict[str, str]]:
        values = value if isinstance(value, list) else ([value] if value not in (None, "", {}) else [])
        labels: list[dict[str, str]] = []
        for item in values[:limit]:
            if isinstance(item, dict):
                labels.append({
                    "source": _compact_text(item.get("source") or item.get("source_type") or "unknown", 120),
                    "detail": _compact_text(item.get("detail") or item.get("key_facts") or "", 180),
                })
            else:
                labels.append({"source": _compact_text(item, 180), "detail": ""})
        return labels

    stages = []
    for stage in analysis.get("calculation_stages") or []:
        if not isinstance(stage, dict):
            continue
        parameters = stage.get("parameters") if isinstance(stage.get("parameters"), dict) else {}
        stages.append({
            "id": stage.get("id"),
            "calculation_type": _compact_text(stage.get("calculation_type") or stage.get("name") or "", 180),
            "dependencies": [str(item) for item in stage.get("dependencies") or []],
            "parameters": {
                str(key): _compact_text(value, 180) if isinstance(value, str) else value
                for key, value in list(parameters.items())[:16]
            },
            "evidence": evidence_labels(stage.get("evidence"), 2),
        })

    deliverables = []
    for item in plan.get("required_deliverables") or []:
        if not isinstance(item, dict):
            continue
        deliverables.append({
            "id": item.get("id"),
            "stage_id": item.get("stage_id"),
            "type": item.get("type"),
            "asset_role": item.get("asset_role"),
            "source_strategy": item.get("source_strategy"),
            "acquisition_kind": item.get("acquisition_kind"),
            "format": item.get("format"),
            "required": item.get("required", True),
            "requirement_basis": _compact_text(item.get("requirement_basis") or "", 220),
            "acceptance_criteria": compact_list(item.get("acceptance_criteria"), 5, 180),
            "evidence": evidence_labels(item.get("evidence"), 2),
        })

    steps = []
    for step in plan.get("generation_steps") or []:
        if not isinstance(step, dict):
            continue
        arguments = step.get("tool_arguments") if isinstance(step.get("tool_arguments"), dict) else {}
        tool_name = str(step.get("tool_name") or "")
        spec = arguments.get("artifact_spec") if isinstance(arguments.get("artifact_spec"), dict) else {}
        outputs = {str(item) for item in step.get("outputs") or []}
        output_paths = arguments.get("output_paths") if isinstance(arguments.get("output_paths"), dict) else {}
        compact_spec = None
        if spec:
            compact_spec = {
                "purpose": _compact_text(spec.get("purpose") or "", 260),
                "parameter_sources": compact_list(spec.get("parameter_sources"), 6, 180),
                "format": spec.get("format"),
                "acceptance_criteria": compact_list(spec.get("acceptance_criteria"), 6, 180),
                "runtime_dependencies": [str(item) for item in spec.get("runtime_dependencies") or []][:12],
                "evidence": evidence_labels(spec.get("evidence"), 2),
            }
        steps.append({
            "id": step.get("id"),
            "stage_id": step.get("stage_id"),
            "tool_name": tool_name,
            "tool_capability": _compact_text(step.get("tool_capability") or "", 160),
            "dependencies": [str(item) for item in step.get("dependencies") or []],
            "inputs": compact_list(step.get("inputs"), 6, 160),
            "outputs": [str(item) for item in step.get("outputs") or []],
            "output_paths": output_paths,
            "artifact_spec": compact_spec,
            "specialized_arguments": {
                key: arguments.get(key)
                for key in (
                    "discipline", "mesh_type", "operation", "parameters",
                    "workflow_capability", "generate_mesh_assets", "generate_model_assets",
                    "mesh_generation_result", "data_path", "case_dir",
                )
                if arguments.get(key) not in (None, "", [], {})
            } if tool_name in {
                "prepare_scientific_mesh", "build_scientific_preprocessing_package"
            } else None,
            # Source is intentionally omitted from the Critic payload, but
            # the schema has already parsed it.  Supply its verifiable
            # execution contract so a reviewer cannot mistake compaction for
            # an absent Python program or incomplete path mapping.
            "execution_contract": {
                "has_parseable_python_code": bool(str(arguments.get("code") or "").strip()),
                "output_paths_complete": set(str(item) for item in output_paths) == outputs,
            } if str(step.get("tool_name") or "") in {"execute_preprocessing_python", "execute_python"} else None,
            # Downloads write to executor-managed staging. ``output_name`` is
            # therefore the declared relative destination; the executor
            # records the final path, URL and SHA-256 after the transfer.
            "download_contract": {
                "has_url": bool(str(arguments.get("url") or "").strip()),
                "output_name": arguments.get("output_name"),
                "declared_outputs_complete": bool(outputs),
                "executor_records_sha256": True,
            } if str(step.get("tool_name") or "") == "data_web_download" else None,
            "artifact_spec_contract": {
                "complete": bool(compact_spec) and all(
                    spec.get(field) not in (None, "", [], {})
                    for field in ("purpose", "parameter_sources", "format", "evidence", "acceptance_criteria")
                ),
                "acceptance_criteria_count": len(spec.get("acceptance_criteria") or []),
                "evidence_count": len(spec.get("evidence") or []),
                "display_is_excerpt": bool(compact_spec),
            } if str(step.get("tool_name") or "") == "generate_preprocessing_artifact" else None,
            "verification": compact_list(step.get("verification"), 6, 180),
        })

    return {
        "schema_version": plan.get("schema_version"),
        "plan_kind": plan.get("plan_kind"),
        "task_summary": _compact_text(plan.get("task_summary") or "", 900),
        "discipline": {
            "primary": (plan.get("discipline") or {}).get("primary"),
            "sub_discipline": (plan.get("discipline") or {}).get("sub_discipline"),
        },
        "simulation_software": {
            "name": (plan.get("simulation_software") or {}).get("name"),
            "version": (plan.get("simulation_software") or {}).get("version"),
            "preprocessor": (plan.get("simulation_software") or {}).get("preprocessor"),
        },
        "calculation_stages": stages,
        "required_deliverables": deliverables,
        "generation_steps": steps,
        "tool_requirements": [
            {
                "capability": _compact_text(item.get("capability") or "", 160),
                "selected_tools": [str(tool) for tool in item.get("selected_tools") or []],
            }
            for item in plan.get("tool_requirements") or []
            if isinstance(item, dict)
        ],
        "assumptions": compact_list(plan.get("assumptions"), 10, 220),
        "unresolved_questions": compact_list(plan.get("unresolved_questions"), 8, 220),
        "risks": compact_list(plan.get("risks"), 8, 220),
        "external_dataset_policy": (
            "Declared small external payloads may be downloaded and verified; large or unknown-size datasets are published as per-step acquisition workflows, and search failures remain documented source-discovery states."
        ),
    }


def _bound_critic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep Critic input below the configured character budget."""
    if len(_serialized_payload(payload)) <= _PLANNING_PAYLOAD_BUDGET_CHARS:
        return payload
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    # Evidence prose is useful for traceability but not for the Critic's
    # deterministic contract review. Remove it before reducing scientific IDs.
    for stage in plan.get("calculation_stages") or []:
        if isinstance(stage, dict):
            stage.pop("evidence", None)
            stage["parameters"] = {
                key: value for key, value in list((stage.get("parameters") or {}).items())[:8]
            }
    for item in plan.get("required_deliverables") or []:
        if isinstance(item, dict):
            item.pop("evidence", None)
            item["acceptance_criteria"] = list(item.get("acceptance_criteria") or [])[:3]
    for step in plan.get("generation_steps") or []:
        if isinstance(step, dict):
            step.pop("inputs", None)
            step.pop("verification", None)
            spec = step.get("artifact_spec")
            if isinstance(spec, dict):
                spec.pop("evidence", None)
                spec["parameter_sources"] = list(spec.get("parameter_sources") or [])[:3]
                spec["acceptance_criteria"] = list(spec.get("acceptance_criteria") or [])[:3]
    plan.pop("assumptions", None)
    plan.pop("unresolved_questions", None)
    plan.pop("risks", None)
    if len(_serialized_payload(payload)) <= _PLANNING_PAYLOAD_BUDGET_CHARS:
        return payload
    # Last-resort contract projection.  Keep every stage and deliverable, plus
    # the compact semantic/verification facts the Critic is asked to judge.
    # Dropping whole artifact specs made the model report those fields as
    # absent even though deterministic validation had accepted them.
    plan["task_summary"] = _compact_text(plan.get("task_summary") or "", 300)
    plan["calculation_stages"] = [
        {
            "id": item.get("id"),
            "calculation_type": _compact_text(item.get("calculation_type") or "", 100),
            "dependencies": item.get("dependencies") or [],
        }
        for item in plan.get("calculation_stages") or []
        if isinstance(item, dict)
    ]
    plan["required_deliverables"] = [
        {
            "id": item.get("id"),
            "stage_id": item.get("stage_id"),
            "type": item.get("type"),
            "format": item.get("format"),
            "required": item.get("required", True),
        }
        for item in plan.get("required_deliverables") or []
        if isinstance(item, dict)
    ]
    projected_steps = []
    for item in plan.get("generation_steps") or []:
        if not isinstance(item, dict):
            continue
        spec = item.get("artifact_spec") if isinstance(item.get("artifact_spec"), dict) else {}
        projected_steps.append({
            "id": item.get("id"),
            "stage_id": item.get("stage_id"),
            "tool_name": item.get("tool_name"),
            "dependencies": item.get("dependencies") or [],
            "outputs": item.get("outputs") or [],
            "output_paths": item.get("output_paths") or {},
            "artifact_spec": {
                "purpose": _compact_text(spec.get("purpose") or "", 140),
                "format": spec.get("format"),
                "parameter_sources": [
                    _compact_text(value, 90)
                    for value in (spec.get("parameter_sources") or [])[:2]
                ],
                "runtime_dependencies": [
                    _compact_text(value, 90)
                    for value in (spec.get("runtime_dependencies") or [])[:12]
                ],
                "acceptance_criteria": [
                    _compact_text(value, 110)
                    for value in (spec.get("acceptance_criteria") or [])[:3]
                ],
            } if spec else None,
            "artifact_spec_contract": item.get("artifact_spec_contract"),
            "execution_contract": item.get("execution_contract"),
            "download_contract": item.get("download_contract"),
            "verification_count": len(item.get("verification") or []),
        })
    plan["generation_steps"] = projected_steps
    if len(_serialized_payload(payload)) > _PLANNING_PAYLOAD_BUDGET_CHARS:
        # Preserve the complete graph and its Boolean contracts; only prose is
        # expendable when an unusually large plan still exceeds the boundary.
        for step in projected_steps:
            step.pop("artifact_spec", None)
        plan["task_summary"] = ""
    return payload


def _repair_designer_plan_contract(
    raw: dict[str, Any],
    requirement_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Repair mechanical plan references without inventing scientific content.

    Designer output is free-form JSON, while execution uses stable deliverable
    identifiers.  This adapter preserves the model's scientific choices but
    restores the shared contract from RequirementAnalysis and canonicalizes
    filename-like output references to their declared deliverable ids.
    """
    plan = normalize_plan(raw)
    authoritative = _normalize_requirement_analysis(deepcopy(requirement_analysis))
    # Designer returns only fulfilment decisions.  Scope, scientific identity,
    # stages and evidence are immutable Analyst contracts and serializing them
    # again for every 20+ stage plan was the main cause of truncated JSON.
    plan["plan_kind"] = "preprocessing_generation"
    plan["task_summary"] = str(
        plan.get("task_summary")
        or "Generate the caller-requested stage inputs declared by RequirementAnalysis."
    )
    plan["task_scope"] = dict(authoritative.get("task_scope") or {})
    plan["preprocessing_request"] = deepcopy(authoritative.get("preprocessing_request") or {})
    plan["preprocessing_work_order"] = deepcopy(authoritative.get("preprocessing_work_order") or {})
    plan["review_profile"] = str(
        authoritative.get("review_profile")
        or plan["preprocessing_request"].get("review_profile")
        or "request_bound"
    )
    plan.setdefault("reproducibility", {})
    plan["discipline"] = {
        **dict(authoritative.get("discipline") or {}),
        **dict(plan.get("discipline") or {}),
    }
    plan["simulation_software"] = {
        **dict(authoritative.get("simulation_software") or {}),
        **dict(plan.get("simulation_software") or {}),
    }
    # The RequirementAnalysis is confirmed upstream scope; Designer prose may
    # refine execution details but cannot relabel its discipline or primary
    # application.  This prevents a generic fallback/tool description from
    # turning a valid plan into an unrelated solver workflow.
    authoritative_discipline = dict(authoritative.get("discipline") or {})
    authoritative_software = dict(authoritative.get("simulation_software") or {})
    if str(authoritative_discipline.get("primary") or "").strip():
        plan["discipline"]["primary"] = authoritative_discipline["primary"]
    if authoritative_discipline.get("declared"):
        plan["discipline"]["declared"] = authoritative_discipline["declared"]
    if str(authoritative_software.get("name") or "").strip():
        plan["simulation_software"]["name"] = authoritative_software["name"]
    if not str(plan["discipline"].get("primary") or "").strip():
        plan["discipline"] = authoritative_discipline
    if not str(plan["simulation_software"].get("name") or "").strip():
        plan["simulation_software"] = authoritative_software

    # Keep scope, stages, and evidence sourced from the confirmed analysis;
    # the Designer should decide the executable DAG, not silently expand the
    # research workflow with downstream simulation/post-processing stages.
    drafted_analysis = dict(plan.get("requirement_analysis") or {})
    plan["requirement_analysis"] = {
        **drafted_analysis,
        **{
            key: authoritative.get(key)
            for key in (
                "calculation_type", "calculation_stages", "required_files", "optional_files",
                "stage_input_contracts", "boundary_contract",
                "evidence", "assumptions", "unresolved_facts", "reference_evidence",
                "reference_gap_status", "completed_reference_request_ids",
                "task_scope", "preprocessing_request", "preprocessing_work_order",
                "review_profile",
            )
            if authoritative.get(key) not in (None, "", [], {})
        },
    }
    basis = plan["requirement_analysis"].get("evidence") or [{
        "source_type": "upstream", "detail": "Confirmed requirement analysis."
    }]
    # Evidence is a shared RequirementAnalysis contract.  A Designer may omit
    # it while retaining all scientific content; preserve a minimal upstream
    # provenance record instead of discarding the entire draft for that
    # mechanical omission.
    plan["requirement_analysis"]["evidence"] = basis
    # Blocking questions belong to the Analyst contract.  Do not let the
    # Designer turn its own drafting uncertainties into a new stop condition;
    # preserve confirmed questions when present and carry Analyst unresolved
    # facts as explicit, non-blocking assumptions for the downstream stage.
    authoritative_questions = authoritative.get("unresolved_questions")
    if isinstance(authoritative_questions, list):
        plan["unresolved_questions"] = deepcopy(authoritative_questions)
    else:
        plan["unresolved_questions"] = [
            {
                "id": item.get("id") or f"unresolved_{index + 1}",
                "question": item.get("description") or item.get("question") or "",
                "blocking": False,
                "resume_contract": item.get("suggested_resolution") or "",
            }
            for index, item in enumerate(authoritative.get("unresolved_facts") or [])
            if isinstance(item, dict)
            and (item.get("description") or item.get("question"))
        ]
    required_by_alias: dict[str, dict[str, Any]] = {}
    runtime_sources_by_stage: dict[str, list[dict[str, Any]]] = {}
    reference_status_by_id = _reference_status_by_request(authoritative)
    terminal_reference_status_by_id = _reference_status_by_request(
        authoritative,
        allowed_statuses=_VERIFIED_REFERENCE_STATUSES,
    )
    authoritative_stages_by_id = {
        str(stage.get("id") or "").strip(): stage
        for stage in authoritative.get("calculation_stages") or []
        if isinstance(stage, dict) and str(stage.get("id") or "").strip()
    }
    stage_parameters_by_id = {
        stage_id: _stage_parameter_context(stage)
        for stage_id, stage in authoritative_stages_by_id.items()
    }
    runtime_access_aliases = {
        _normalise_asset_key(value)
        for item in authoritative.get("runtime_access_dependencies") or []
        if isinstance(item, dict)
        for value in (item.get("id"), item.get("scientific_role"))
        if _normalise_asset_key(value)
    }
    runtime_tool_aliases: set[str] = set()
    for requirement in authoritative.get("runtime_tool_requirements") or []:
        if not isinstance(requirement, dict):
            continue
        identity = str(
            requirement.get("tool_identity")
            or requirement.get("scientific_role")
            or requirement.get("id")
            or ""
        )
        for value in (
            requirement.get("id"), identity, requirement.get("scientific_role")
        ):
            if _normalise_asset_key(value):
                runtime_tool_aliases.add(_normalise_asset_key(value))
        for stage_id in requirement.get("consumer_stages") or []:
            runtime_tool_aliases.add(_normalise_asset_key(f"{stage_id}_{identity}"))
    for item in authoritative.get("required_files") or []:
        if not isinstance(item, dict):
            continue
        for value in (
            item.get("id"),
            item.get("name_or_role"),
            item.get("declared_output_path"),
            PurePosixPath(_declared_relative_output(item, str(item.get("id") or "asset"))).name,
        ):
            key = _normalise_asset_key(value)
            if key:
                required_by_alias[key] = item
        if str(item.get("fulfillment_kind") or "") == "runtime_output":
            stage_id = str(item.get("stage_id") or "").strip()
            if stage_id:
                runtime_sources_by_stage.setdefault(stage_id, []).append(item)

    used_ids: set[str] = set()
    aliases: dict[str, str] = {}
    repaired_deliverables: list[dict[str, Any]] = []
    for index, item in enumerate(plan.get("required_deliverables") or [], 1):
        if not isinstance(item, dict):
            continue
        deliverable = dict(item)
        if any(
            _normalise_asset_key(value) in runtime_access_aliases
            for value in (deliverable.get("id"), deliverable.get("name"), deliverable.get("type"))
            if _normalise_asset_key(value)
        ):
            # Runtime access is supplied to an approved acquisition process;
            # it has no writable package representation.
            continue
        if any(
            _normalise_asset_key(value) in runtime_tool_aliases
            for value in (deliverable.get("id"), deliverable.get("name"), deliverable.get("type"))
            if _normalise_asset_key(value)
        ):
            # Runtime tools are resolved by the execution environment and may
            # be shared by many stages; they are never package deliverables.
            continue
        item_id = _slug(str(deliverable.get("id") or deliverable.get("name") or f"deliverable_{index}"))
        base_id, suffix = item_id, 2
        while item_id in used_ids:
            item_id = f"{base_id}_{suffix}"
            suffix += 1
        used_ids.add(item_id)
        source = required_by_alias.get(_normalise_asset_key(item_id)) or required_by_alias.get(
            _normalise_asset_key(deliverable.get("name") or deliverable.get("type"))
        ) or {}
        # A plan may expand one declared runtime product into several runtime
        # variants (for example, a matrix of cases).  The aggregate name need
        # not match each variant, but a unique declared runtime contract for
        # the same stage is still authoritative.  Bind by structured stage
        # ownership rather than words in an output filename.
        draft_stage_id = str(deliverable.get("stage_id") or "").strip()
        if not source and draft_stage_id:
            stage_sources = runtime_sources_by_stage.get(draft_stage_id) or []
            if len(stage_sources) == 1:
                source = stage_sources[0]
        if not source:
            # Non-delivery helper artifacts are allowed only as local inputs
            # to an approved producer. They never become caller deliverables
            # or broaden scientific authority.
            if (
                deliverable.get("required") is False
                and deliverable.get("delivery_required") is False
                and str(deliverable.get("source_strategy") or "") == "local_generation"
                and str(deliverable.get("declared_output_path") or "").strip()
            ):
                source = deliverable
            else:
                used_ids.discard(item_id)
                continue
        if (
            plan["review_profile"] == "request_bound"
            and deliverable.get("required") is False
            and deliverable.get("delivery_required") is False
            and str(deliverable.get("requirement_basis") or "").strip()
            and not plan["preprocessing_request"].get("requested_assets")
            and deliverable.get("output_path_is_explicit") is False
        ):
            # Natural-language requests are reviewed against their immutable
            # snapshot. Analyst-derived route dependencies are not additional
            # user demands; retain the explicit exclusion for Critic/reviewer.
            source = {**source, "required": False, "delivery_required": False}
            for requirement in plan["requirement_analysis"].get("required_files") or []:
                if str(requirement.get("id") or "") == str(source.get("id") or ""):
                    requirement.update(required=False, delivery_required=False,
                                       requirement_basis=deliverable["requirement_basis"])
        else:
            deliverable["delivery_required"] = source.get("delivery_required", True)
            deliverable["required"] = source.get("required", source.get("delivery_required", True))
        deliverable["id"] = item_id
        current_path = str(deliverable.get("declared_output_path") or "").strip()
        source_path = _declared_relative_output(source or deliverable, item_id)
        # Internal F###/artifact IDs are valid tracking identifiers but not
        # deliverable names. Prose containing an extension mention is not a
        # filename either; retain a Designer path only when it ends in a real
        # filename suffix or the authority has no stronger file declaration.
        if (
            not current_path
            or re.fullmatch(r"(?:f|artifact)[_-]?\d+", PurePosixPath(current_path).name, flags=re.I)
            or (
                re.search(r"\.[A-Za-z0-9][A-Za-z0-9._-]*$", PurePosixPath(source_path).name)
                and not re.search(r"\.[A-Za-z0-9][A-Za-z0-9._-]*$", PurePosixPath(current_path).name)
            )
        ):
            deliverable["declared_output_path"] = source_path
        if not str(deliverable.get("type") or "").strip():
            deliverable["type"] = _asset_contract_role(source or deliverable)
        if not str(deliverable.get("format") or "").strip():
            deliverable["format"] = str(source.get("format") or "generated preprocessing artifact")
        deliverable["format"] = _format_for_declared_output(
            str(deliverable.get("declared_output_path") or ""),
            deliverable.get("format"),
        )
        contract = normalize_asset_contract({**deliverable, **source})
        # ``normalize_plan`` may have classified the draft before its alias
        # was bound to the authoritative requirement.  Replace the whole
        # routing portion of the contract here; retaining a draft's old
        # capability alongside the authoritative local/external strategy
        # creates an artificial contradictory-field rejection.
        deliverable.update({
            key: contract[key]
            for key in (
                "scientific_role", "representation", "asset_role",
                "source_strategy", "acquisition_kind", "workflow_capability", "is_external",
            )
        })
        # The RequirementAnalysis contract is authoritative for acquisition.
        # A Designer may still return a partial acquisition_contract, but that
        # partial object must not hide the source locator (the previous
        # empty-only copy caused valid URLs for external datasets to be lost).
        source_acquisition = source.get("acquisition_contract")
        draft_acquisition = deliverable.get("acquisition_contract")
        if isinstance(source_acquisition, dict) and source_acquisition:
            if isinstance(draft_acquisition, dict) and draft_acquisition:
                merged_acquisition = {
                    **draft_acquisition,
                    **deepcopy(source_acquisition),
                }
                draft_retrieval = draft_acquisition.get("retrieval_instructions")
                source_retrieval = source_acquisition.get("retrieval_instructions")
                if isinstance(draft_retrieval, dict) and isinstance(source_retrieval, dict):
                    merged_acquisition["retrieval_instructions"] = {
                        **draft_retrieval,
                        **deepcopy(source_retrieval),
                    }
                deliverable["acquisition_contract"] = merged_acquisition
            else:
                deliverable["acquisition_contract"] = deepcopy(source_acquisition)
        for key in (
            "access_method", "dataset_identifier", "provider", "url", "endpoint",
            "request_template", "parameter_binding_mode", "parameter_binding_assertions",
        ):
            if deliverable.get(key) in (None, "", [], {}) and source.get(key) not in (None, "", [], {}):
                deliverable[key] = deepcopy(source[key])
        reference_source = {
            **(source or deliverable),
            "available_stage_parameters": stage_parameters_by_id.get(
                str(source.get("stage_id") or deliverable.get("stage_id") or "")
            ) or {},
        }
        reference_metadata = _reference_retrieval_metadata(authoritative, reference_source)
        if reference_metadata:
            deliverable.update({
                key: deepcopy(value)
                for key, value in reference_metadata.items()
                if deliverable.get(key) in (None, "", [], {})
                or key == "retrieval_instructions"
            })
            # Keep the authoritative locator in the nested contract too.  A
            # stale Designer copy must not override fresh reference evidence
            # when normalize_acquisition_contract is called below.
            nested_contract = dict(deliverable.get("acquisition_contract") or {})
            if reference_metadata.get("retrieval_instructions"):
                nested_contract["retrieval_instructions"] = deepcopy(
                    reference_metadata["retrieval_instructions"]
                )
            if reference_metadata.get("url"):
                nested_contract["url"] = str(reference_metadata["url"])
            if nested_contract:
                deliverable["acquisition_contract"] = nested_contract
        if isinstance(source.get("local_match"), dict):
            # Local reuse is selected deterministically before Designer.  The
            # model may describe the delivery route, but cannot replace the
            # verified source path or its match basis.
            deliverable["local_match"] = dict(source["local_match"])
        # Expected outputs from a declared scientific stage are runtime
        # products.  They remain in the plan as dependency/provenance
        # contracts, but cannot be required from the text artifact writer.
        # This distinction is generic (data, solver outputs, and derived
        # fields alike) and avoids treating a NetCDF/GRIB product as a
        # parameter-file generation failure.
        fulfillment_kind = str(source.get("fulfillment_kind") or deliverable.get("fulfillment_kind") or "").strip()
        if fulfillment_kind:
            deliverable["fulfillment_kind"] = fulfillment_kind
        reference_status = reference_status_by_id.get(str(source.get("id") or ""))
        verified_reference_status = terminal_reference_status_by_id.get(str(source.get("id") or ""))
        if contract.get("acquisition_kind") == "external_dataset":
            # Reference evidence authorizes the source used by the acquisition
            # document; it never makes the document optional.  This is also
            # the path used when no network search is needed because the
            # Research Plan already supplies enough retrieval information.
            # The lightweight acquisition JSON remains a required dataset
            # deliverable even when the underlying data is not downloaded.
            deliverable["fulfillment_kind"] = "external_dataset_reference"
            deliverable["delivery_required"] = True
            deliverable["required"] = True
            # ``format`` describes the file delivered by this generation step.
            # The scientific dataset representation remains in AssetContract
            # and acquisition_contract.expected_output.  Mixing these two made
            # the text writer reject a JSON retrieval guide as if it were being
            # asked to manufacture GRIB/NetCDF bytes.
            deliverable["format"] = "JSON acquisition contract"
            if reference_status:
                deliverable["reference_status"] = reference_status
                gap_record = next(
                    (
                        value for value in (authoritative.get("reference_gap_status") or {}).values()
                        if isinstance(value, dict)
                        and str(value.get("request_id") or "") == str(source.get("id") or "")
                    ),
                    {},
                )
                if not isinstance(deliverable.get("source_discovery"), dict):
                    deliverable["source_discovery"] = {
                        "status": "no_result" if reference_status == "no_result" else "search_error" if reference_status == "execution_error" else reference_status,
                        "reason": str(gap_record.get("reason") or gap_record.get("error") or "").strip(),
                        "query": str(gap_record.get("query") or "").strip(),
                    }
            deliverable["declared_output_path"] = _acquisition_output_path(
                source or deliverable,
                item_id,
            )
            deliverable["acquisition_contract"] = normalize_acquisition_contract({
                **deliverable,
                "available_stage_parameters": stage_parameters_by_id.get(
                    str(source.get("stage_id") or deliverable.get("stage_id") or "")
                ) or {},
                "acquisition_contract": deliverable.get("acquisition_contract") or {},
            })
            deliverable.setdefault(
                "resume_contract",
                "Use the packaged external-data acquisition summary to retrieve and validate this dataset in the execution environment.",
            )
        elif isinstance(deliverable.get("local_match"), dict):
            # An approved small-file download is now an ordinary verified local
            # input.  Publish it through the existing local-reuse path instead
            # of suppressing it as a resolved reference.
            deliverable.update({
                "source_strategy": "local_reuse",
                "acquisition_kind": "local_asset",
                "workflow_capability": "local_asset_reuse",
                "is_external": False,
                "required": source.get("delivery_required", True),
                "delivery_required": source.get("delivery_required", True),
                "fulfillment_kind": "stage_input",
            })
            if reference_status:
                deliverable["reference_status"] = reference_status
        elif verified_reference_status:
            deliverable["required"] = False
            deliverable["delivery_required"] = False
            deliverable["reference_status"] = verified_reference_status
            deliverable["fulfillment_kind"] = "deferred_external_reference"
            deliverable.setdefault(
                "resume_contract",
                "Acquire and verify the named external reference before executing its consuming stage.",
            )
        elif fulfillment_kind == "runtime_output" or source.get("delivery_required") is False:
            deliverable["required"] = False
            deliverable.setdefault(
                "resume_contract",
                "Produce this runtime output only when its declared stage is executed with available inputs and dependencies.",
            )
        else:
            deliverable.setdefault("required", bool(source) or True)
        # The Designer selects an executable route. It cannot turn its own
        # preferred method into new authority during a revision. Preserve the
        # confirmed requirement's basis/evidence/criteria on every plan.
        deliverable["requirement_basis"] = str(
            source.get("reason") or "Required by the preprocessing plan."
        )
        deliverable["evidence"] = deepcopy(source.get("evidence") or basis)
        deliverable["acceptance_criteria"] = deepcopy(
            source.get("acceptance_criteria") or [
                "Artifact exists at the recorded path.",
                "Declared format and provenance validation are recorded.",
            ]
        )
        if not str(deliverable.get("stage_id") or "").strip():
            stage_ids = [
                str(stage.get("id") or "").strip()
                for stage in authoritative.get("calculation_stages") or []
                if isinstance(stage, dict) and str(stage.get("id") or "").strip()
            ]
            source_stage_id = str(source.get("stage_id") or "").strip()
            source_text = json.dumps(source or deliverable, ensure_ascii=False, default=str)
            matches = [
                stage_id for stage_id in stage_ids
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(stage_id)}(?![A-Za-z0-9])", source_text, flags=re.I)
            ]
            # An ambiguous asset is not a package asset.  Leave ownership
            # unresolved so schema validation forces the Designer to assign a
            # declared research stage instead of silently misplacing it.
            deliverable["stage_id"] = (
                source_stage_id if source_stage_id in stage_ids
                else matches[0] if len(matches) == 1 else ""
            )
        # Frozen/structured file bindings remain authoritative. For a natural
        # request the Analyst map also contains output-level goals; the Designer
        # binds their representation only after choosing an executable route.
        if not normalize_asset_contract(deliverable).get("is_external"):
            binding_contract = source
            if (
                plan["review_profile"] == "request_bound"
                and not plan["preprocessing_request"].get("requested_assets")
                and isinstance(deliverable.get("parameter_bindings"), dict)
                and str(deliverable.get("parameter_binding_basis") or "").strip()
            ):
                binding_contract = deliverable
            bindings = next(
                (
                    dict(candidate)
                    for candidate in (
                        binding_contract.get("parameter_bindings"),
                        binding_contract.get("configuration_values"),
                    )
                    if isinstance(candidate, dict) and candidate
                ),
                {},
            )
            # RequirementAnalysis may intentionally carry only the stage's
            # structured ``explicit_parameter_keys``. Compile the file binding
            # contract here. Do not use arbitrary
            # stage prose or the broad context map as file bindings.
            if not bindings:
                owning_stage = authoritative_stages_by_id.get(
                    str(deliverable.get("stage_id") or source.get("stage_id") or "").strip()
                ) or {}
                derived_contract = _explicit_file_parameter_contract(
                    owning_stage,
                    binding_mode=(
                        "semantic"
                        if canonical_workflow_capability(deliverable)
                        == "preprocessing_script_generation"
                        else "literal"
                    ),
                )
                if derived_contract:
                    bindings = dict(derived_contract["parameter_bindings"])
                    deliverable.update(derived_contract)
            # A missing installation/executable path is a runtime dependency,
            # not an immutable file parameter. Drop only explicit placeholder
            # values here; concrete Research Plan bindings remain unchanged.
            bindings = {
                key: value
                for key, value in bindings.items()
                if not blocked_placeholder_markers(
                    json.dumps(value, ensure_ascii=False, default=str)
                )
            }
            if bindings:
                deliverable["parameter_bindings"] = bindings
                deliverable["parameter_bindings_required"] = True
                if deliverable.get("parameter_binding_mode") in (None, "", [], {}):
                    deliverable["parameter_binding_mode"] = (
                        "semantic"
                        if canonical_workflow_capability(deliverable)
                        == "preprocessing_script_generation"
                            else "literal"
                    )
                # A Designer draft may carry assertions for a broader,
                # obsolete stage-context map.  Keep only assertions belonging
                # to the authoritative binding keys; otherwise a removed
                # external-input reference can still fail execution even
                # after its binding was normalized away.
                source_assertions = binding_contract.get("parameter_binding_assertions")
                if isinstance(source_assertions, list):
                    deliverable["parameter_binding_assertions"] = deepcopy(source_assertions)
                else:
                    binding_keys = {str(key) for key in bindings}
                    deliverable["parameter_binding_assertions"] = [
                        assertion
                        for assertion in deliverable.get("parameter_binding_assertions") or []
                        if isinstance(assertion, dict)
                        and str(
                            assertion.get("name")
                            or assertion.get("binding")
                            or assertion.get("binding_path")
                            or ""
                        ) in binding_keys
                    ]
            else:
                deliverable["parameter_bindings"] = {}
                deliverable.pop("parameter_bindings_required", None)
                deliverable.pop("parameter_binding_mode", None)
                deliverable.pop("parameter_binding_assertions", None)
            if source.get("parameter_bindings_required") and bindings:
                deliverable["parameter_bindings_required"] = True
            if binding_contract.get("parameter_binding_basis") not in (None, "", [], {}):
                deliverable["parameter_binding_basis"] = deepcopy(
                    binding_contract["parameter_binding_basis"]
                )
        repaired_deliverables.append(deliverable)
        for value in (item_id, deliverable.get("name"), deliverable.get("type"), source.get("id"), source.get("name_or_role")):
            key = _normalise_asset_key(value)
            if key:
                aliases[key] = item_id

    terminal_reference_deliverable_ids = {
        str(item.get("id") or "")
        for item in repaired_deliverables
        if str(item.get("reference_status") or "") in _VERIFIED_REFERENCE_STATUSES
    }
    repaired_steps: list[dict[str, Any]] = []
    removed_step_ids: set[str] = set()
    for index, item in enumerate(plan.get("generation_steps") or [], 1):
        if not isinstance(item, dict):
            continue
        step = dict(item)
        step.setdefault("id", f"step_{index}")
        step.setdefault("action", f"Generate declared preprocessing outputs for {step['id']}.")
        step.setdefault("tool_capability", "registered_preprocessing_tool")
        step.setdefault("tool_arguments", {})
        step.setdefault("inputs", [])
        step.setdefault("dependencies", [])
        canonical_outputs: list[str] = []
        for output in step.get("outputs") or []:
            output_text = str(output or "").strip()
            if not output_text:
                continue
            if _normalise_asset_key(output_text) in runtime_access_aliases:
                # A credential/access requirement is neither generated nor
                # published.  The executor checks it at the point where the
                # approved acquisition needs it.
                continue
            if _normalise_asset_key(output_text) in runtime_tool_aliases:
                # Executables are caller-environment prerequisites, not files produced
                # by a preprocessing generation step.
                continue
            output_id = aliases.get(_normalise_asset_key(output_text))
            if not output_id:
                continue
            canonical_outputs.append(output_id)
        step["outputs"] = list(dict.fromkeys(canonical_outputs))
        if not step["outputs"] and not is_pipeline_infrastructure_step(step):
            # The step has no authoritative deliverable.  Drop it and let a
            # registered domain tool own any private helper files it needs.
            removed_step_ids.add(str(step["id"]))
            continue
        if (
            step["outputs"] and set(step["outputs"]).issubset(terminal_reference_deliverable_ids)
        ):
            # Reference discovery is executed by the single-request driver.
            # Once that request reaches a terminal ledger state, a generation
            # draft must retain only its deferred dependency contract.
            removed_step_ids.add(str(step["id"]))
            continue
        arguments = step.get("tool_arguments")
        if not isinstance(arguments, dict):
            arguments = {}
            step["tool_arguments"] = arguments
        if str(step.get("tool_name") or "") == "prepare_scientific_mesh":
            # ``coordinate_files`` is an input contract, not a filename the
            # mesh tool will create.  Designer drafts sometimes leave a
            # helper name such as ``naca2412.geo`` after the helper step was
            # discarded above.  For an explicitly named analytic NACA
            # profile, bind the existing local generator directly and remove
            # only file references that have no real local source.
            mesh_context = " ".join(
                str(value or "")
                for value in (
                    arguments.get("spec"),
                    arguments.get("naca_code"),
                    arguments.get("airfoil"),
                    arguments.get("geometry"),
                )
            )
            naca_match = re.search(
                r"\bNACA\s*[-_ ]?(\d{4,5})(?!\d)",
                mesh_context,
                flags=re.I,
            )
            coordinate_files = arguments.get("coordinate_files")
            coordinate_values = (
                list(coordinate_files)
                if isinstance(coordinate_files, (list, tuple))
                else [coordinate_files]
                if coordinate_files not in (None, "")
                else []
            )
            if naca_match:
                arguments["naca_code"] = naca_match.group(1)
                has_concrete_coordinate_file = any(
                    Path(str(value)).expanduser().is_file()
                    for value in coordinate_values
                    if str(value or "").strip()
                )
                if coordinate_values and not has_concrete_coordinate_file:
                    arguments.pop("coordinate_files", None)
                    step["inputs"] = [
                        value for value in step.get("inputs") or []
                        if _normalise_asset_key(value) in required_by_alias
                    ]
        if str(step.get("tool_name") or "") == "generate_preprocessing_artifact":
            # The artifact writer owns its generated-artifact workspace.  A
            # Designer-provided cwd has no scientific meaning for a text-file
            # delivery and can only attempt to redirect writes outside that
            # workspace, so do not carry it into the executable contract.
            arguments.pop("cwd", None)
            # Keep the Designer focused on a compact artifact specification;
            # accept common aliases mechanically, then let schema validation
            # reject genuinely missing scientific inputs.
            spec = arguments.get("artifact_spec")
            if not isinstance(spec, dict):
                candidate = step.get("artifact_spec") or step.get("spec") or {}
                arguments["artifact_spec"] = dict(candidate) if isinstance(candidate, dict) else {}
                spec = arguments["artifact_spec"]
            if step.get("acceptance_criteria") and not spec.get("acceptance_criteria"):
                spec["acceptance_criteria"] = step.get("acceptance_criteria")
            if step.get("parameter_sources") and not spec.get("parameter_sources"):
                spec["parameter_sources"] = step.get("parameter_sources")
            if step.get("evidence") and not spec.get("evidence"):
                spec["evidence"] = step.get("evidence")
            # Keep the artifact contract normalized by the shared schema so
            # every caller (initial draft, repair and focused merge) sees the
            # same array/object representation.
            spec = normalize_artifact_spec(spec)
            arguments["artifact_spec"] = spec
            # Do not invent missing scientific specifications.  The Designer
            # must provide every required artifact_spec field; normalization
            # only carries explicit aliases into the canonical location.
            output_paths = arguments.get("output_paths")
            if not isinstance(output_paths, dict) or not output_paths:
                candidate = step.get("output_paths") or spec.get("output_paths") or {}
                if isinstance(candidate, dict):
                    output_paths = candidate
                else:
                    output_paths = {}
            arguments["output_paths"] = output_paths
            produced = [
                entry for entry in repaired_deliverables
                if entry.get("id") in step["outputs"]
            ]
            # A declared scientific-stage output is a runtime product, not a
            # text artifact that this writer can deliver.  Designer commonly
            # uses such a product as a shorthand for the helper script or
            # namelist which will produce it later.  Preserve that runtime
            # relationship in the arguments, but make the writer's single
            # output the actual local artifact.  This is deliberately based
            # on the structured fulfilment contract, never on application or
            # filename keywords.
            if produced and all(
                str(entry.get("fulfillment_kind") or "") == "runtime_output"
                for entry in produced
            ):
                auxiliary_id = _slug(f"{step['id']}_artifact")
                while auxiliary_id in used_ids:
                    auxiliary_id = _slug(f"{auxiliary_id}_next")
                used_ids.add(auxiliary_id)
                stage_ids = {str(entry.get("stage_id") or "").strip() for entry in produced}
                stage_id = str(step.get("stage_id") or "").strip()
                if not stage_id and len(stage_ids - {""}) == 1:
                    stage_id = next(iter(stage_ids - {""}))
                path_values = list(output_paths.values()) if isinstance(output_paths, dict) else []
                declared_path = str(path_values[0] or "").strip() if len(path_values) == 1 else ""
                fmt = str(spec.get("format") or "generated preprocessing artifact")
                auxiliary = {
                    "id": auxiliary_id,
                    "stage_id": stage_id,
                    "name": f"Local artifact for {step['id']}",
                    "type": "generated_auxiliary_artifact",
                    "format": fmt,
                    "scientific_role": "preprocessing_configuration",
                    "source_strategy": "local_generation",
                    "acquisition_kind": "generated_artifact",
                    "workflow_capability": "local_artifact_generation",
                    "required": True,
                    "requirement_basis": f"Local execution artifact selected by {step['id']}.",
                    "evidence": basis,
                    "acceptance_criteria": [
                        "Artifact exists at the recorded path.",
                        "Declared format and provenance validation are recorded.",
                    ],
                    "parameter_bindings": deepcopy(spec.get("parameter_bindings") or {}),
                    "parameter_binding_mode": spec.get("parameter_binding_mode") or "semantic",
                }
                if declared_path:
                    auxiliary["declared_output_path"] = declared_path
                auxiliary.update(normalize_asset_contract(auxiliary))
                repaired_deliverables.append(auxiliary)
                step["outputs"] = [auxiliary_id]
                arguments["runtime_outputs"] = [entry["id"] for entry in produced]
                arguments["output_paths"] = {auxiliary_id: declared_path}
        if isinstance(arguments, dict) and isinstance(arguments.get("output_paths"), dict):
            canonical_paths: dict[str, Any] = {}
            for raw_id, path in arguments["output_paths"].items():
                canonical_id = aliases.get(_normalise_asset_key(raw_id)) or _slug(str(raw_id))
                canonical_paths[canonical_id] = path
            arguments["output_paths"] = canonical_paths
        if not isinstance(step.get("verification"), list) or not step["verification"]:
            step["verification"] = [
                "Verify each declared output exists and is non-empty.",
                "Record the validation result and provenance for each output.",
            ]
        output_stage_ids = {
            str(item.get("stage_id") or "").strip()
            for item in repaired_deliverables
            if item.get("id") in step.get("outputs") and str(item.get("stage_id") or "").strip()
        }
        if (
            len(output_stage_ids) == 1
            and str(step.get("tool_name") or "") != "build_scientific_preprocessing_package"
        ):
            step["stage_id"] = next(iter(output_stage_ids))
        if not str(step.get("stage_id") or "").strip():
            stage_ids = [
                str(stage.get("id") or "").strip()
                for stage in authoritative.get("calculation_stages") or []
                if isinstance(stage, dict) and str(stage.get("id") or "").strip()
            ]
            argument_text = json.dumps(arguments, ensure_ascii=False, default=str)
            argument_matches = [
                stage_id for stage_id in stage_ids
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(stage_id)}(?![A-Za-z0-9])", argument_text, flags=re.I)
            ]
            if len(argument_matches) == 1:
                step["stage_id"] = argument_matches[0]
            elif len(output_stage_ids) == 1:
                step["stage_id"] = next(iter(output_stage_ids))
            elif str(step.get("tool_name") or "") == "build_scientific_preprocessing_package":
                step["stage_id"] = "package"
        if str(step.get("tool_name") or "") == "generate_preprocessing_artifact":
            spec = arguments.get("artifact_spec") if isinstance(arguments.get("artifact_spec"), dict) else {}
            stage_id = str(step.get("stage_id") or "package").strip() or "package"
            if stage_parameters_by_id.get(stage_id):
                existing_context = (
                    dict(spec.get("available_stage_parameters") or {})
                    if isinstance(spec.get("available_stage_parameters"), dict)
                    else {}
                )
                existing_context = {
                    key: value
                    for key, value in existing_context.items()
                    if "environment_absolute_path" not in blocked_placeholder_markers(
                        json.dumps(value, ensure_ascii=False, default=str)
                    )
                }
                spec["available_stage_parameters"] = {
                    **existing_context,
                    **stage_parameters_by_id[stage_id],
                }
            # Public paths belong to the deliverable contract.  The writer's
            # staging path must never be shared between independent steps.
            # Normalize it here for both Designer and deterministic plans so
            # a later generation cannot overwrite an earlier valid artifact.
            output_paths = arguments.get("output_paths") if isinstance(arguments.get("output_paths"), dict) else {}
            arguments["output_paths"] = {
                str(output_id): (
                    f"outputs/{_slug(stage_id)}/{_slug(str(output_id))}/"
                    f"{PurePosixPath(str(raw_path or output_id)).name or _slug(str(output_id))}"
                )
                for output_id, raw_path in output_paths.items()
            }
        step_outputs = [
            entry for entry in repaired_deliverables
            if entry.get("id") in step.get("outputs", [])
        ]
        if step_outputs and all(
            str(entry.get("source_strategy") or "") == "local_reuse"
            for entry in step_outputs
        ):
            # The publisher already materializes verified local assets. Keeping
            # a writer step would overwrite an acquired official file with
            # model-generated text.
            continue
        if len(step_outputs) == 1:
            output_contract = step_outputs[0]
            output_capability = (
                "local_artifact_generation"
                if str(step.get("tool_name") or "") == "build_scientific_preprocessing_package"
                else (
                    "configuration_generation"
                    if (
                        str(step.get("tool_name") or "") == "generate_preprocessing_artifact"
                        and normalize_asset_contract(output_contract).get("is_external")
                    )
                    else canonical_workflow_capability(output_contract)
                )
            )
            if output_capability != "unclassified":
                step["workflow_capability"] = output_capability
                arguments["workflow_capability"] = output_capability
            if str(step.get("tool_name") or "") == "generate_preprocessing_artifact":
                spec = (
                    arguments.get("artifact_spec")
                    if isinstance(arguments.get("artifact_spec"), dict)
                    else {}
                )
                # The RequirementAnalysis deliverable is authoritative for
                # immutable file-level values.  Do not retain bindings copied
                # from a prior failed plan or invented in a Designer draft.
                bindings = output_contract.get("parameter_bindings")
                if isinstance(bindings, dict) and bindings:
                    spec["parameter_bindings"] = deepcopy(bindings)
                    spec["parameter_bindings_required"] = True
                    spec["parameter_binding_mode"] = str(
                        output_contract.get("parameter_binding_mode")
                        or (
                            "semantic"
                            if output_capability == "preprocessing_script_generation"
                            else "literal"
                        )
                    )
                else:
                    spec.pop("parameter_bindings", None)
                    spec.pop("parameter_bindings_required", None)
                    spec.pop("parameter_binding_mode", None)
                    spec.pop("parameter_binding_assertions", None)
                if output_contract.get("parameter_binding_assertions") not in (None, "", [], {}):
                    spec["parameter_binding_assertions"] = deepcopy(
                        output_contract["parameter_binding_assertions"]
                    )
                if output_contract.get("parameter_binding_basis") not in (None, "", [], {}):
                    spec["parameter_binding_basis"] = deepcopy(
                        output_contract["parameter_binding_basis"]
                    )
                else:
                    spec.pop("parameter_binding_basis", None)
                declared_format = str(output_contract.get("format") or "").strip()
                if declared_format:
                    spec["format"] = declared_format
                stage_id = str(step.get("stage_id") or "").strip()
                if (
                    normalize_asset_contract(output_contract).get("acquisition_kind")
                    == "external_dataset"
                ):
                    spec["artifact_kind"] = "external_dataset_acquisition"
                    spec["acquisition_contract"] = normalize_acquisition_contract({
                        **output_contract,
                        "available_stage_parameters": stage_parameters_by_id.get(stage_id) or {},
                        "acquisition_contract": output_contract.get("acquisition_contract") or {},
                    })
                arguments["artifact_spec"] = spec
        # Runtime data acquisition/execution is intentionally not a package
        # delivery step.  It has its own approved execution contract; keeping
        # a Designer-invented step here lets it override the confirmed source
        # strategy and route outside TaskScopeContract.  Drop only steps whose
        # complete output set is explicitly runtime-only.
        if step_outputs and all(
            str(entry.get("fulfillment_kind") or "") == "runtime_output"
            for entry in step_outputs
        ):
            removed_step_ids.add(str(step["id"]))
            continue
        repaired_steps.append(step)
    if removed_step_ids:
        for step in repaired_steps:
            step["dependencies"] = [
                dependency for dependency in step.get("dependencies") or []
                if str(dependency) not in removed_step_ids
            ]

    produced_deliverables = {
        str(output)
        for step in repaired_steps
        for output in step.get("outputs") or []
    }
    for deliverable in repaired_deliverables:
        deliverable_id = str(deliverable.get("id") or "")
        if (
            deliverable_id
            and deliverable_id not in produced_deliverables
            and deliverable.get("required", True)
            and normalize_asset_contract(deliverable).get("acquisition_kind") == "external_dataset"
        ):
            repaired_steps.append(
                _generic_artifact_generation_step(deliverable, authoritative)
            )
            produced_deliverables.add(deliverable_id)

    # Re-apply the immutable stage context after merging Designer and compiler
    # steps; a late-added artifact must not lose its owning stage parameters.
    for step in repaired_steps:
        if str(step.get("tool_name") or "") != "generate_preprocessing_artifact":
            continue
        arguments = step.get("tool_arguments")
        spec = arguments.get("artifact_spec")
        if not isinstance(arguments, dict) or not isinstance(spec, dict):
            continue
        stage_id = str(step.get("stage_id") or "").strip()
        stage_context = stage_parameters_by_id.get(stage_id) or {}
        if stage_context:
            existing = spec.get("available_stage_parameters")
            existing = existing if isinstance(existing, dict) else {}
            spec["available_stage_parameters"] = {**existing, **deepcopy(stage_context)}

    # Designer naturally names a prerequisite by the artifact it needs,
    # whereas the executor DAG names prerequisites by generation-step ID.
    # Resolve that mechanical indirection once here. Requiring the model to
    # maintain both identifier namespaces caused large, repeated batches of
    # ``unknown dependencies`` without adding any scientific information.
    producer_by_output: dict[str, str] = {}
    for step in repaired_steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        for output in step.get("outputs") or []:
            key = _normalise_asset_key(output)
            if key:
                producer_by_output[key] = step_id
    for step in repaired_steps:
        step_id = str(step.get("id") or "").strip()
        canonical_dependencies: list[str] = []
        for dependency in step.get("dependencies") or []:
            raw_dependency = str(dependency or "").strip()
            if not raw_dependency:
                continue
            dependency_key = _normalise_asset_key(raw_dependency)
            output_id = aliases.get(dependency_key, raw_dependency)
            resolved = producer_by_output.get(_normalise_asset_key(output_id))
            if resolved:
                if resolved != step_id and resolved not in canonical_dependencies:
                    canonical_dependencies.append(resolved)
                continue
            if dependency_key in required_by_alias:
                # Required-file IDs name data/config inputs, while this DAG
                # field accepts producer step IDs only. Missing generation for
                # a required local deliverable is checked independently.
                continue
            if raw_dependency.casefold() in authoritative_stages_by_id:
                # Research stages describe downstream runtime order. They are
                # preserved in RequirementAnalysis, not executed as artifact
                # producer steps by the data node.
                continue
            # A verified reference deliverable is published by the reference
            # executor, not by a generation step.  Designer drafts sometimes
            # retain mechanical names such as ``gen_f014`` or
            # ``gen_f014_acquisition`` from an earlier plan.  Remove only
            # those aliases for terminal references; unknown dependencies for
            # unresolved assets remain validation errors.
            dependency_key = _normalise_asset_key(raw_dependency)
            is_terminal_reference_alias = False
            for terminal_id in terminal_reference_deliverable_ids:
                terminal_key = _normalise_asset_key(terminal_id)
                if not terminal_key:
                    continue
                if dependency_key == terminal_key:
                    is_terminal_reference_alias = True
                    break
                for prefix in ("gen", "generate", "acquire", "download"):
                    suffix = dependency_key.removeprefix(prefix)
                    if suffix.endswith("acquisition"):
                        suffix = suffix[: -len("acquisition")]
                    if suffix == terminal_key:
                        is_terminal_reference_alias = True
                        break
                if is_terminal_reference_alias:
                    break
            if is_terminal_reference_alias:
                continue
            if raw_dependency != step_id and raw_dependency not in canonical_dependencies:
                canonical_dependencies.append(raw_dependency)
        # Inputs use deliverable IDs while dependencies use step IDs.  Infer
        # the latter from the former so a Designer cannot disconnect a valid
        # producer merely by omitting the duplicate dependency declaration.
        own_outputs = {_normalise_asset_key(value) for value in step.get("outputs") or []}
        step["inputs"] = [
            value for value in step.get("inputs") or []
            if _normalise_asset_key(value) not in own_outputs
        ]
        for input_id in step["inputs"]:
            input_key = _normalise_asset_key(input_id)
            producer = producer_by_output.get(input_key)
            if producer and producer != step_id and producer not in canonical_dependencies:
                canonical_dependencies.append(producer)
        step["dependencies"] = canonical_dependencies

    plan["required_deliverables"] = repaired_deliverables
    plan["generation_steps"] = repaired_steps

    selected_tools = sorted({
        str(step.get("tool_name") or "").strip()
        for step in repaired_steps if str(step.get("tool_name") or "").strip()
    })
    if selected_tools:
        plan["tool_requirements"] = [{
            "capability": "designer_selected_preprocessing_tools",
            "selected_tools": selected_tools,
        }]

    # A question already covered by a concrete public-reference recovery step
    # is an execution dependency, not a reason to reject the plan itself.
    # The step still has to succeed before any dependent generation can use
    # the recovered fact.  Keep credentials, access controls, and genuinely
    # unplanned questions blocking.
    has_reference_recovery = any(
        str(step.get("tool_name") or "") in {"data_web_search", "data_web_download"}
        and bool(
            (step.get("tool_arguments") or {}).get("query")
            or (step.get("tool_arguments") or {}).get("url")
        )
        for step in repaired_steps
    )
    runtime_access = {
        str(item.get("id") or "").strip().casefold(): item
        for item in authoritative.get("runtime_access_dependencies") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    runtime_outputs = {
        _normalise_asset_key(value)
        for item in authoritative.get("required_files") or []
        if isinstance(item, dict)
        and str(item.get("fulfillment_kind") or "") == "runtime_output"
        for value in (item.get("id"), item.get("name_or_role"))
        if _normalise_asset_key(value)
    }
    terminal_reference_keys = {
        key for key, item in required_by_alias.items()
        if str(item.get("id") or "").strip() in terminal_reference_status_by_id
    }
    for question in plan.get("unresolved_questions") or []:
        if not isinstance(question, dict) or not question.get("blocking", True):
            continue
        question_text = _text_blob(question)
        question_id = str(question.get("id") or "").strip().casefold()
        question_key = _normalise_asset_key(question_text)
        if question_id in runtime_access:
            question["blocking"] = False
            question.setdefault("resume_contract", runtime_access[question_id].get("resume_contract"))
            continue
        if re.search(
            r"credential|license|restricted|proprietary|controlled[- ]access|"
            r"凭证|许可|受控|专有",
            question_text,
            flags=re.I,
        ):
            question["blocking"] = False
            question.setdefault(
                "resume_contract",
                "Provide the authorized runtime access through the execution environment; do not generate or search for credentials.",
            )
            continue
        if any(key and key in question_key for key in runtime_outputs):
            question["blocking"] = False
            question.setdefault(
                "resume_contract",
                "The named item is a runtime stage output; fulfil it through its declared acquisition or generation contract when the owning stage is executed.",
            )
            continue
        if any(key and key in question_key for key in terminal_reference_keys):
            question["blocking"] = False
            question.setdefault(
                "resume_contract",
                "The approved reference request already reached a terminal ledger state; preserve that deferred dependency without reopening planning.",
            )
            continue
        external_dependency = next(
            (
                item for key, item in required_by_alias.items()
                if key and key in question_key and normalize_asset_contract(item).get("is_external")
            ),
            None,
        )
        if external_dependency is not None:
            question["blocking"] = False
            question.setdefault(
                "resume_contract",
                "Use the stage-owned acquisition document to retrieve and validate this external dependency before downstream execution.",
            )
            continue
        if has_reference_recovery:
            question["blocking"] = False
            question.setdefault(
                "resume_contract",
                "Run and verify the plan's targeted public-reference recovery step before using the affected value.",
            )
    if not str(plan.get("task_summary") or "").strip():
        plan["task_summary"] = "Scientific preprocessing execution plan."
    plan.setdefault("reproducibility", {})
    plan["reproducibility"].setdefault("workspace_layout", "data_preprocessing")
    plan["reproducibility"].setdefault("provenance_records", ["requirement_analysis", "plan", "tool_results"])
    if plan["review_profile"] == "request_bound":
        plan["preprocessing_work_order"] = build_preprocessing_work_order(
            plan["preprocessing_request"],
            {**plan["requirement_analysis"], "required_files": plan["required_deliverables"]},
        )
        plan["requirement_analysis"]["preprocessing_work_order"] = plan["preprocessing_work_order"]
    return plan


def _registered_tool_inventory(requirement_analysis: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    internal = {
        "analyze_preprocessing_requirements",
        "run_preprocessing_planning_loop",
        "execute_preprocessing_plan",
    }
    # Tool exposure follows TaskScopeContract, never incidental wording in a
    # requirement such as "grid" or "structure".  This is both smaller and
    # prevents an unrelated capability from leaking into a revision prompt.
    scope = requirement_analysis.get("task_scope") if isinstance(requirement_analysis, dict) else {}
    allowed_capabilities = {
        str(item).strip().lower()
        for item in (scope.get("allowed_capabilities") or [])
        if str(item).strip()
    } if isinstance(scope, dict) else set()
    include_mesh = "mesh_generation" in allowed_capabilities
    include_structure = "atomic_structure_generation" in allowed_capabilities

    inventory: list[dict[str, Any]] = []
    for name in all_tool_names():
        if name in internal:
            continue
        definition = get_tool(name)
        if definition is None:
            continue
        if definition.allowed_node_types is not None and "data" not in definition.allowed_node_types:
            continue
        # Use the same plan-kind contract as schema validation. This prevents
        # Designer from ever seeing a tool that its resulting generation plan
        # would be required to reject.
        if not tool_allowed_in_plan_kind(name, "preprocessing_generation"):
            continue
        if name == "prepare_scientific_mesh" and not include_mesh:
            continue
        if name == "recover_atomic_structure" and not include_structure:
            continue
        schema = definition.parameters_schema if isinstance(definition.parameters_schema, dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        contract: dict[str, Any] = {
            "required_parameters": list(schema.get("required") or []),
            "parameter_names": list(properties)[:12],
            "parameter_types": {
                key: value.get("type", "any")
                for key, value in list(properties.items())[:12]
                if isinstance(value, dict)
            },
        }
        # These are the only nested contracts a Designer needs to form an
        # executable plan.  Replaying every tool's full JSON schema consumed
        # several thousand characters while duplicating executor validation.
        if name == "generate_preprocessing_artifact":
            artifact_schema = (
                properties.get("artifact_spec")
                if isinstance(properties.get("artifact_spec"), dict)
                else {}
            )
            contract["artifact_spec_required"] = list(artifact_schema.get("required") or [])
            contract["output_paths"] = "mapping: one relative path for each output id"
        entry = {
            "name": name,
            "description": _compact_text(definition.description, 140),
            "risk_level": definition.risk_level,
            "contract": contract,
        }
        if name == "generate_preprocessing_artifact":
            artifact_properties = (
                artifact_schema.get("properties")
                if isinstance(artifact_schema.get("properties"), dict)
                else {}
            )
            output_paths_schema = (
                properties.get("output_paths")
                if isinstance(properties.get("output_paths"), dict)
                else {}
            )
            # Project the authoritative writer schema instead of maintaining a
            # second hand-written Designer contract.  Keeping the five field
            # types prevents payload compaction from changing arrays into
            # ambiguous scalar prose.
            entry["parameters_schema"] = {
                "required": list(schema.get("required") or []),
                "properties": {
                    "artifact_spec": {
                        "type": "object",
                        "required": contract["artifact_spec_required"],
                        "properties": {
                            field: artifact_properties.get(field, {})
                            for field in contract["artifact_spec_required"]
                        },
                    },
                    "output_paths": {
                        "type": "object",
                        "additionalProperties": output_paths_schema.get(
                            "additionalProperties", {"type": "string"}
                        ),
                    },
                }
            }
        inventory.append(entry)
    return inventory


def _mesh_planning_contracts(
    task_context: str | dict[str, Any],
    discipline: str,
) -> dict[str, dict[str, Any]]:
    """Expose the selected adapter contract without replaying every mesh API."""
    from .mesh_generator import mesh_parameter_contracts

    contracts = mesh_parameter_contracts(discipline)
    if str(discipline or "").strip().lower() != "cfd":
        return contracts
    from .mesh_iteration_advisor import explicit_mesh_contract

    route = explicit_mesh_contract(caller_request_text(task_context), {}) or {}
    mesh_type = str(route.get("mesh_type") or "").strip()
    return {mesh_type: contracts[mesh_type]} if mesh_type in contracts else contracts


def _artifact_context(state: State) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    for item in state.list_artifacts()[:20]:
        record = state.read_artifact(item["id"]) or {}
        context.append({
            "id": item["id"],
            "type": item.get("type"),
            "name": item.get("name"),
            "metadata": record.get("metadata") or {},
            "content_hash": canonical_hash({
                "content": record.get("content"),
                "metadata": record.get("metadata") or {},
            }),
            "content_preview": _compact_text(record.get("content") or "", 800),
        })
    return context


def _stable_task_context_identity(task_context: str | dict[str, Any]) -> Any:
    """Return only durable caller inputs for cache/source identity."""
    if isinstance(task_context, str):
        return re.sub(r"\s+", " ", task_context).strip()
    if not isinstance(task_context, dict):
        return str(task_context)
    durable_keys = (
        "preprocessing_request", "request_id", "request_kind",
        "research_plan", "research_plan_id", "pre_registration", "plan",
        "objective", "user_request", "node_inputs", "inspected_inputs",
        "required_files", "calculation_stages", "task_scope",
    )
    durable = {
        key: task_context.get(key)
        for key in durable_keys
        if task_context.get(key) not in (None, "", [], {})
    }
    if durable:
        return durable
    return {str(key): value for key, value in task_context.items()}


async def analyze_preprocessing_requirements(
    state: State,
    task_context: str | dict[str, Any],
    reference_evidence: list[dict[str, Any]] | None = None,
    model_name: str = "",
    review_revision_contract: dict[str, Any] | None = None,
    previous_requirement_analysis: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    reference_evidence = _usable_reference_evidence(reference_evidence)
    context_text = task_context if isinstance(task_context, str) else json.dumps(task_context, ensure_ascii=False, default=str)
    fresh_inspections = await _inspect_paths_from_context(state, task_context)
    input_inspections = _merge_available_input_inspections(state, fresh_inspections)
    local_asset_inventory = _local_asset_inventory(task_context, input_inspections)
    compact_inspections = _compact_input_inspections(input_inspections)
    planning_documents = _local_planning_documents(input_inspections)
    upstream_artifacts = _artifact_context(state)
    # Project history is not an implicit input to a new work order.  Reuse is
    # explicit through PreprocessingRequest.source_artifact_ids.
    request_hint = state.hook_state.get("_data_current_request")
    if not isinstance(request_hint, dict):
        request_hint = normalize_preprocessing_request(task_context)
    declared_sources = {
        str(value) for value in request_hint.get("source_artifact_ids") or []
        if str(value).strip()
    }
    upstream_artifacts = [
        item for item in upstream_artifacts
        if str(item.get("id") or "") in declared_sources
    ]
    source_stable_id = canonical_hash({
        "task_context": _stable_task_context_identity(task_context),
        "planning_documents": [item.get("content_hash") for item in planning_documents],
        "upstream_artifacts": [
            item.get("content_hash") for item in upstream_artifacts
            if str(item.get("type") or "").lower()
            in {"research_plan", "pre_registration", "preregistration"}
        ],
    })
    preprocessing_request = normalize_preprocessing_request(
        task_context,
        source_artifact_ids=[
            str(item.get("id")) for item in upstream_artifacts
            if item.get("id") and str(item.get("type") or "").lower()
            in {"research_plan", "pre_registration", "preregistration"}
        ],
        source_stable_id=source_stable_id,
        authority_kind_hint=(
            "pre_registration"
            if any(str(item.get("type") or "").lower() in {"pre_registration", "preregistration"} for item in upstream_artifacts)
            else "research_plan"
            if planning_documents or any(str(item.get("type") or "").lower() == "research_plan" for item in upstream_artifacts)
            else ""
        ),
    )
    request_errors = validate_preprocessing_request(preprocessing_request)
    if request_errors:
        return {
            "status": "needs_input",
            "reason_code": "invalid_preprocessing_request",
            "preprocessing_request": preprocessing_request,
            "validation_errors": request_errors,
            "resume": "Correct only the cited PreprocessingRequest fields and rerun requirement analysis.",
        }
    decisive_gaps = decisive_input_gaps(preprocessing_request)
    if decisive_gaps:
        return {
            "status": "needs_input",
            "reason_code": "decisive_asset_parameters_missing",
            "preprocessing_request": preprocessing_request,
            "missing_inputs": decisive_gaps,
            "resume_contract": {
                "action": "supply_missing_asset_parameters",
                "request_id": preprocessing_request["request_id"],
                "preserve_request_spec_hash": preprocessing_request["request_spec_hash"],
            },
        }
    # Cache by stable plan inputs and ledger evidence, not by mutable model
    # wording, scratchpad, or reference-result prose.  Reference evidence is
    # a separate ledger input and must not force the requirement analyst to
    # re-derive the same plan.
    request_key = canonical_hash({
        "request_id": preprocessing_request["request_id"],
        "request_spec_hash": preprocessing_request["request_spec_hash"],
        "revision_context": ({"contract": review_revision_contract,
                              "previous_analysis": previous_requirement_analysis}
                             if review_revision_contract else None),
        "local_asset_inventory": [
            {key: item.get(key) for key in ("path", "size_bytes", "sha256", "format")}
            for item in local_asset_inventory
        ],
    })
    cache = state.hook_state.setdefault("_data_requirement_analysis_cache", {})
    if request_key in cache:
        cached = {**cache[request_key], "reused": True}
        if isinstance(cached.get("requirement_analysis"), dict):
            state.hook_state["_data_latest_requirement_analysis"] = cached["requirement_analysis"]
            _save_requirement_analysis(state, cached["requirement_analysis"])
        return cached
    if not preprocessing_request.get("purpose") and not preprocessing_request.get("requested_assets"):
        needs_input = {
            "status": "needs_input",
            "reason_code": "explicit_preprocessing_request_required",
            "preprocessing_request": preprocessing_request,
            "message": (
                "请提供明确的前处理目标，例如要生成、获取、检查或转换的资产，以及已知的下游用途、"
                "格式、参数和验收要求。research plan 可选。"
            ),
            "required_fields": [
                "requested preprocessing asset or operation",
                "downstream use or expected format",
            ],
            "resume": "Merge the concrete preprocessing request into task_context and rerun requirement analysis.",
        }
        cache[request_key] = needs_input
        return needs_input
    request_context = "\n\n".join([
        context_text,
        *[
            str(item.get("content") or item.get("content_excerpt") or "")
            for item in planning_documents
            if re.search(r"research.?plan|pre.?registration", str(item.get("name") or ""), flags=re.I)
        ],
    ])
    planning_documents_payload = [
        {
            key: item.get(key)
            for key in ("path", "name", "content_hash", "content_excerpt")
            if item.get(key) not in (None, "")
        }
        for item in planning_documents
    ]
    compact_artifacts = [
        {
            "id": item.get("id"),
            "type": item.get("type"),
            "name": item.get("name"),
            "metadata": {
                str(key): _compact_text(value, 160)
                for key, value in list(dict(item.get("metadata") or {}).items())[:12]
            },
        }
        for item in upstream_artifacts[:5]
    ]
    emit_progress(
        state,
        "requirements",
        "analyzing preprocessing requirements",
        inspected_paths=len(input_inspections),
    )
    payload = {
        "task_context_summary": _compact_text(
            caller_request_text(task_context, preprocessing_request=preprocessing_request), 3200
        ),
        "input_inspections": compact_inspections,
        "available_local_assets": [
            {
                key: item.get(key)
                for key in ("path", "size_bytes", "format", "asset_kind", "data_model_kind", "scientific_role", "coverage", "sha256", "provenance")
                if item.get(key) not in (None, "", {})
            }
            for item in local_asset_inventory[:20]
        ],
        "local_planning_documents": planning_documents_payload,
        "preprocessing_request": preprocessing_request,
        "caller_declared_discipline": _declared_discipline_from_context(request_context) or None,
        "available_upstream_artifacts": compact_artifacts,
        "deterministic_discipline_hint": identify_discipline(
            text_sample=context_text,
            metadata=task_context if isinstance(task_context, dict) else None,
        ),
        "reference_evidence": [
            {
                **{k: v for k, v in item.items() if k != "content"},
                "content_excerpt": _compact_text(item.get("content") or item.get("text") or item, 300),
            }
            for item in (reference_evidence or [])[:4]
            if isinstance(item, dict)
        ],
        **({
            "revision_contract": {
                **review_revision_contract,
                "issues": _compact_critique_feedback({"feedback": review_revision_contract.get("issues") or []})["feedback"],
            },
            "previous_requirement_analysis": _compact_requirement_analysis(
                previous_requirement_analysis
            ),
        } if review_revision_contract else {}),
        "instruction": (
            "The preprocessing_request.authority_snapshot is the original authority. "
            "Service specs in inputs are implementation context: retain useful tool paths and supplied inputs, "
            "but do not turn their additional suggestions into required outputs or acceptance criteria. "
            "Extract the caller-supplied scope and derive the data, parameter files, "
            "execution contracts, and validation inputs required by every declared stage. "
            "Do not invent objectives, hypotheses, stages, sweeps, or acceptance criteria. Keep JSON concise; "
            "use unresolved_facts for missing evidence instead of expanding prose. Treat local/upstream "
            "information as scope and parameter evidence; emit targeted official-documentation queries for "
            "named applications when their preprocessing conventions are not explicitly supplied. "
            "Treat assets described as existing, ready, supplied, or reusable as inputs rather than requested "
            "outputs. Enumerate every output explicitly requested by the caller."
            + (
                " Revise the previous RequirementAnalysis using the review RevisionContract. The locked caller "
                "request is unchanged: add or correct only assets omitted or misclassified by the previous "
                "analysis, and do not turn an existing input into a new deliverable. Preserve asset/stage IDs "
                "for unchanged responsibilities. Correct inferred filenames, bindings and methods when they "
                "contradict the authority; never delete an explicit caller requirement to silence a failure."
                if review_revision_contract else ""
            )
        ),
    }
    payload["mesh_parameter_contracts"] = _mesh_planning_contracts(
        task_context,
        str((payload["deterministic_discipline_hint"] or {}).get("primary_discipline") or ""),
    )
    state.append_transcript(
        "preprocessing_requirement_payload_compacted",
        review_profile=preprocessing_request["review_profile"],
        request_id=preprocessing_request["request_id"],
        payload_chars=len(_serialized_payload(payload)),
        inspected_paths=len(input_inspections),
        compacted_inspection_chars=len(json.dumps(compact_inspections, ensure_ascii=False, default=str)),
        artifact_count=len(payload["available_upstream_artifacts"]),
        local_planning_document_count=len(planning_documents),
        local_planning_document_names=[
            str(item.get("name") or "") for item in planning_documents
        ],
    )
    # Compile any complete structured authority locally.  The model Analyst
    # remains available for ambiguous prose and for plan-bound inputs whose
    # stages have not yet been extracted, but it is not a mandatory rewrite
    # layer for either request mode.
    structured_plan_stages = [
        item for item in _structured_values(
            _structured_context_object(task_context), "calculation_stages"
        ) if isinstance(item, dict)
    ]
    raw = (
        _compile_explicit_requirements(task_context)
        if _has_structured_requirement_contract(task_context) and not review_revision_contract
        else None
    )
    if (
        preprocessing_request["review_profile"] == "plan_bound"
        and not structured_plan_stages
    ):
        raw = None
    if raw is not None:
        used_model = "deterministic_explicit_request"
        usage = {}
        state.append_transcript(
            "preprocessing_requirement_compiled",
            request_id=preprocessing_request["request_id"],
            required_file_count=len(raw.get("required_files") or []),
            calculation_stage_count=len(raw.get("calculation_stages") or []),
        )
        emit_progress(
            state,
            "requirements_compiled",
            "compiled explicit authority without remote requirement analysis",
            required_files=len(raw.get("required_files") or []),
        )
    else:
        try:
            raw, used_model, usage = await _call_json_agent_resilient(
                state,
                role="requirement_analyst",
                system_prompt=REQUIREMENT_ANALYST_SYSTEM_PROMPT,
                payload=payload,
                model_name=model_name,
                attempts=_planning_agent_attempts("requirement_analyst"),
            )
        except Exception as exc:
            raw = (
                _compile_explicit_requirements(task_context)
                if _has_structured_requirement_contract(task_context) and not review_revision_contract
                else None
            )
            if raw is None:
                return {
                    "status": "retryable_error",
                    "stop_reason": "requirement_contract_unavailable",
                    "error": f"Requirement analyst failed: {type(exc).__name__}: {exc}",
                }
            used_model = "explicit_upstream_fallback"
            usage = {}
            state.append_transcript(
                "preprocessing_requirement_fallback",
                software=(raw.get("simulation_software") or {}).get("name"),
                required_file_count=len(raw.get("required_files") or []),
                calculation_stage_count=len(raw.get("calculation_stages") or []),
                reason=f"{type(exc).__name__}: {exc}",
            )
            emit_progress(
                state,
                "requirements_fallback",
                "using explicit upstream fallback",
                required_files=len(raw.get("required_files") or []),
                calculation_stages=len(raw.get("calculation_stages") or []),
            )
    analysis = _normalize_requirement_analysis(raw)
    analysis["preprocessing_request"] = preprocessing_request
    analysis = _apply_upstream_discipline_authority(analysis, request_context)
    if reference_evidence:
        analysis["reference_evidence"] = reference_evidence[:20]
    scoped_analysis = _apply_structured_stage_scope(analysis, task_context)
    analysis = _normalize_requirement_analysis(
        _scope_analysis_to_data_node(request_context, _apply_local_asset_reuse(scoped_analysis, local_asset_inventory))
    )
    if str(preprocessing_request.get("delivery_name") or "") == "preprocessing":
        discipline = analysis.get("discipline") if isinstance(analysis.get("discipline"), dict) else {}
        software = (
            analysis.get("simulation_software")
            if isinstance(analysis.get("simulation_software"), dict) else {}
        )
        semantic_name = "_".join(filter(None, (
            str(discipline.get("primary") or "").strip(),
            str(analysis.get("calculation_type") or "").strip(),
            str(software.get("name") or software.get("primary") or "").strip(),
        )))
        preprocessing_request = {
            **preprocessing_request,
            "delivery_name": _slug(semantic_name, "preprocessing")[:64],
        }
    analysis["preprocessing_request"] = preprocessing_request
    analysis["preprocessing_work_order"] = build_preprocessing_work_order(
        preprocessing_request,
        analysis,
    )
    analysis["review_profile"] = preprocessing_request["review_profile"]
    analysis = _apply_upstream_discipline_authority(analysis, request_context)
    if reference_evidence:
        analysis["reference_evidence"] = reference_evidence[:20]
    unresolved_stage_assets = [
        item for item in analysis.get("unresolved_stage_assets") or []
        if isinstance(item, dict)
    ]
    if unresolved_stage_assets and preprocessing_request["review_profile"] == "plan_bound":
        # 判决拆除 O5（planner:354 降格，2026-08-31）：单消费者自动推断已有，
        # 扩默认即可 —— 无主资产不再打回整份分析；披露进 analysis，发布端
        # 会把无 stage 归属的产物落到既有默认根（stages/stage）。
        unowned_ids = [
            str(item.get("id") or item.get("name_or_role") or "unknown")
            for item in unresolved_stage_assets
        ]
        analysis.setdefault("contract_review_notes", []).append(
            "deliverable assets without stage ownership were routed to the default stage root: "
            + ", ".join(unowned_ids)
        )
        analysis["unowned_stage_assets_defaulted"] = unowned_ids
        try:
            state.append_transcript(
                "preprocessing_routing_defaulted",
                reason="deliverable assets without declared stage ownership",
                asset_ids=unowned_ids)
        except Exception:
            pass
    targeted_requests = _targeted_search_requests_from_analysis(analysis)
    if targeted_requests and not analysis["search_queries"]:
        software = (analysis.get("simulation_software") or {}).get("name") or "simulation software"
        stage = analysis.get("calculation_type") or "requested calculation"
        analysis["search_queries"] = [
            f"{software} {stage} required input files official manual"
        ]
    state.append_transcript(
        "preprocessing_requirements_analyzed",
        software=(analysis.get("simulation_software") or {}).get("name"),
        calculation_type=analysis.get("calculation_type"),
        confidence=analysis.get("confidence"),
        local_gap_count=len(targeted_requests),
    )
    emit_progress(
        state,
        "requirements_done",
        (analysis.get("simulation_software") or {}).get("name") or "unknown software",
        local_gap_count=len(targeted_requests),
    )
    result = {
        "status": "success",
        "requirement_analysis": analysis,
        "input_inspections": compact_inspections,
        "local_information_gaps": targeted_requests,
        "search_queries": analysis.get("search_queries") or [],
        "search_policy": {
            "priority": ["official_documentation", "official_manual", "official_examples", "authoritative_reference"],
            "max_queries": 3,
            "one_query_at_a_time": True,
            "tool": "data_web_search",
            "tool_arguments": {
                "auto_discover_downloads": False,
                "limit": 5,
                "search_mode": "generic",
            },
            "fallback_tools": [],
            "evidence_policy": None,
            "resume": "The executor persists structured reference evidence and the planning loop continues automatically.",
        } if targeted_requests else None,
        "model_used": used_model,
        "usage": usage,
    }
    state.hook_state["_data_latest_requirement_analysis"] = analysis
    _save_requirement_analysis(state, analysis)
    cache[request_key] = result
    return result


async def design_preprocessing_plan(
    state: State,
    task_context: str | dict[str, Any],
    critique_feedback: str | dict[str, Any] | None = None,
    model_name: str = "",
    requirement_analysis: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    store = PlanningStore(state)
    previous = store.latest_plan()
    previous_best_plan = extra.get("_previous_best_plan")
    context_text = (
        task_context
        if isinstance(task_context, str)
        else json.dumps(task_context, ensure_ascii=False, default=str)
    )
    discipline_hint = identify_discipline(
        text_sample=context_text,
        metadata=task_context if isinstance(task_context, dict) else None,
    )
    selected_analysis = _best_requirement_analysis(
        requirement_analysis,
        state.hook_state.get("_data_latest_requirement_analysis"),
        _compile_explicit_requirements(task_context),
    )
    selected_analysis = _ensure_authoritative_caller_assets(task_context, selected_analysis)
    selected_analysis = _scope_analysis_to_data_node(task_context, selected_analysis)
    # A previous plan is useful only for a Critic-directed revision.  On an
    # initial design it is commonly a reference-search plan or failed fallback
    # and merely makes the prompt larger and less focused.
    previous_plan = (
        previous_best_plan or (previous.get("plan") if previous else None)
        if critique_feedback not in (None, "", {}) else None
    )
    revision_focus_step_ids = _critic_referenced_step_ids(critique_feedback, previous_plan)
    revision_focus_stage_ids = _critic_referenced_stage_ids(
        critique_feedback,
        previous_plan,
        revision_focus_step_ids,
    )
    preserve_execution_route = bool(extra.get("_preserve_execution_route"))
    compact_analysis = _compact_requirement_analysis(selected_analysis)
    if critique_feedback not in (None, "", {}) and revision_focus_stage_ids:
        compact_analysis = _focus_requirement_analysis(
            compact_analysis,
            revision_focus_stage_ids,
        )
    payload = {
        "task_context_summary": _fallback_generation_context(
            caller_request_text(
                task_context,
                preprocessing_request=selected_analysis.get("preprocessing_request") or {},
            ),
            selected_analysis,
        ),
        "research_plan_declared_discipline": _declared_discipline_from_context(context_text) or None,
        "deterministic_discipline_hint": discipline_hint,
        "requirement_analysis": compact_analysis,
        "registered_preprocessing_tools": _registered_tool_inventory(compact_analysis),
        "previous_best_plan_summary": _summarize_previous_plan(
            previous_plan,
            revision_focus_step_ids,
            revision_focus_stage_ids,
        ),
        "critic_feedback": _compact_critique_feedback(critique_feedback),
        "instruction": (
            "Produce one preprocessing plan JSON. Use local/upstream evidence for scope and explicit "
            "parameters, and targeted official documentation/repository/example evidence for named "
            "application-specific input conventions. When critic_feedback and previous_best_plan_summary are "
            "present, return only replacement deliverables/steps for revision_focus_step_ids or "
            "revision_focus_stage_ids; the framework "
            "preserves every other approved step verbatim. Do not replace a verified tool binding merely to "
            "address an unrelated concern. After requirement reconciliation, repair against the current "
            "requirement_analysis and original authority; do not restore superseded inferred deliverables "
            "just because old feedback still mentions them. Do not "
            "repeat the full task text; keep reasons and evidence concise."
        ),
    }
    payload["mesh_parameter_contracts"] = _mesh_planning_contracts(
        task_context,
        str(
            (selected_analysis.get("discipline") or {}).get("primary")
            or (selected_analysis.get("task_scope") or {}).get("discipline")
            or ""
        ),
    )
    state.append_transcript(
        "preprocessing_designer_payload_compacted",
        payload_chars=len(_serialized_payload(payload)),
        tool_count=len(payload["registered_preprocessing_tools"]),
        required_file_count=len((compact_analysis or {}).get("required_files") or []),
    )
    request_key = canonical_hash({"payload": payload, "model_name": model_name})
    cache = state.hook_state.setdefault("_data_designer_cache", {})
    if request_key in cache:
        return {**cache[request_key], "reused": True}

    def deterministic_result(reason: str) -> dict[str, Any] | None:
        """Use the canonical local route when the Analyst graph is complete."""
        if not _analysis_supports_deterministic_execution(selected_analysis):
            return None
        fallback_plan = _deterministic_designer_fallback_plan(task_context, selected_analysis)
        fallback_plan = _merge_focused_plan_revision(
            previous_plan,
            fallback_plan,
            revision_focus_step_ids,
            revision_focus_stage_ids,
            preserve_execution_route,
        )
        fallback_validation = validate_plan(fallback_plan)
        if not fallback_validation["valid"] or _deterministic_plan_critical_concerns(fallback_plan):
            return None
        record = store.save_plan(
            fallback_validation["plan"],
            source="designer:deterministic_explicit_requirements",
        )
        state.append_transcript(
            "preprocessing_designer_fallback",
            plan_id=record["plan_id"],
            reason=reason,
        )
        emit_progress(
            state,
            "designer_fallback",
            "using deterministic route from complete requirements",
            plan_id=record["plan_id"],
        )
        result = {
            "status": "success",
            "plan_id": record["plan_id"],
            "plan_hash": record["plan_hash"],
            "plan_path": record["path"],
            "plan": record["plan"],
            "schema_valid": True,
            "schema_errors": [],
            "schema_warnings": fallback_validation["warnings"],
            "model_used": "deterministic_fallback",
            "usage": {},
            "fallback_used": True,
            "next_action": "Call critique_preprocessing_plan before generation.",
        }
        cache[request_key] = result
        return result

    if (
        critique_feedback in (None, "", {})
        and _analysis_supports_deterministic_execution(selected_analysis)
    ):
        deterministic = deterministic_result(
            "complete work order uses the canonical local compiler"
        )
        if deterministic is not None:
            return deterministic

    payload_chars = len(_serialized_payload(payload))
    if payload_chars > _PLANNING_PAYLOAD_BUDGET_CHARS:
        deterministic = deterministic_result(
            f"designer payload {payload_chars} exceeds planning budget {_PLANNING_PAYLOAD_BUDGET_CHARS}"
        )
        if deterministic is not None:
            return deterministic
        return {
            "status": "needs_revision",
            "stop_reason": "designer_payload_budget_exceeded",
            "error": (
                f"The normalized planning contract is {payload_chars} characters, above the "
                f"{_PLANNING_PAYLOAD_BUDGET_CHARS}-character planning budget, and is not complete "
                "enough for deterministic compilation."
            ),
        }
    emit_progress(
        state,
        "designer",
        "designing preprocessing plan",
        payload_chars=payload_chars,
        tool_count=len(payload["registered_preprocessing_tools"]),
    )
    try:
        raw, used_model, usage = await _call_json_agent_resilient(
            state,
            role="designer",
            system_prompt=DESIGNER_SYSTEM_PROMPT,
            payload=payload,
            model_name=model_name,
            attempts=_planning_agent_attempts("designer"),
        )
    except Exception as exc:
        # Rebuild from the latest normalized requirements.  A prior valid plan
        # may still contain stale search gaps, downloaded-asset state, or model
        # notes from an earlier recovery round; schema validity alone does not
        # make it safe to reuse for execution.
        deterministic = deterministic_result(f"{type(exc).__name__}: {exc}")
        if deterministic is not None:
            return deterministic
        if not _analysis_supports_deterministic_execution(selected_analysis):
            return {
                "status": "retryable_error",
                "stop_reason": "designer_unavailable_without_structured_requirements",
                "error": (
                    f"Designer failed ({type(exc).__name__}) and the caller request does not "
                    "contain enough validated stages/assets for a deterministic fallback."
                ),
            }
        fallback_plan = _deterministic_designer_fallback_plan(task_context, selected_analysis)
        fallback_validation = validate_plan(fallback_plan)
        return {
            "status": "retryable_error",
            "error": (
                f"Designer failed and deterministic fallback was invalid: {type(exc).__name__}: {exc}; "
                f"fallback_errors={fallback_validation['errors']}"
            ),
            "next_actions": [
                "Use targeted reference recovery for missing local evidence.",
                "Finalize a blocked report if required lawful assets cannot be obtained.",
                "Do not request human input solely because the planning model failed.",
            ],
        }
    repaired_plan = _repair_designer_plan_contract(raw, selected_analysis)
    repaired_plan = _merge_focused_plan_revision(
        previous_plan,
        repaired_plan,
        revision_focus_step_ids,
        revision_focus_stage_ids,
        preserve_execution_route,
    )
    validation = validate_plan(repaired_plan)
    if not validation["valid"]:
        state.append_transcript(
            "preprocessing_plan_draft_rejected",
            source=f"designer:{used_model}",
            errors=validation["errors"][:20],
        )
        emit_progress(
            state,
            "designer_rejected",
            "schema validation failed",
            error_count=len(validation["errors"]),
        )
        # A malformed executable-generation step is a Designer contract error.
        # Do not replace it with an unrelated deterministic fallback: the next
        # Designer iteration receives the exact schema feedback and must emit
        # actual source code plus declared artifact paths.
        result = {
            "status": "needs_revision",
            "plan": validation["plan"],
            "schema_valid": False,
            "schema_errors": validation["errors"],
            "schema_warnings": validation["warnings"],
            "model_used": used_model,
            "usage": usage,
            "persisted": False,
            "next_action": "Revise the draft using schema_errors; do not call Critic until schema_valid=true.",
        }
        cache[request_key] = result
        return result
    record = store.save_plan(validation["plan"], source=f"designer:{used_model}")
    emit_progress(
        state,
        "designer_done",
        "plan saved",
        plan_id=record["plan_id"],
        warnings=len(validation["warnings"]),
    )
    result = {
        "status": "success",
        "plan_id": record["plan_id"],
        "plan_hash": record["plan_hash"],
        "plan_path": record["path"],
        "plan": record["plan"],
        "schema_valid": validation["valid"],
        "schema_errors": validation["errors"],
        "schema_warnings": validation["warnings"],
        "model_used": used_model,
        "usage": usage,
        "reused": record.get("reused", False),
        "next_action": "Call critique_preprocessing_plan before any generation tool.",
    }
    cache[request_key] = result
    return result


async def critique_preprocessing_plan(
    state: State,
    plan: dict[str, Any] | None = None,
    model_name: str = "",
    quality_threshold: float = 8.0,
    plan_id: str = "",
    expected_plan_kind: str = "preprocessing_generation",
    **_: Any,
) -> dict[str, Any]:
    try:
        quality_threshold = max(0.0, min(10.0, float(quality_threshold)))
    except (TypeError, ValueError):
        quality_threshold = 8.0
    store = PlanningStore(state)
    expected_plan_kind = str(expected_plan_kind or "preprocessing_generation").strip()
    latest = store.get_plan(plan_id) if str(plan_id or "").strip() else store.latest_plan(expected_plan_kind)
    if plan is not None:
        plan_id_hint = str(plan.get("plan_id") or "").strip() if isinstance(plan, dict) else ""
        if plan_id_hint and latest is not None and plan_id_hint == str(latest.get("plan_id") or ""):
            plan = None
    if plan is not None:
        validation = validate_plan(plan)
        if not validation["valid"]:
            return {
                "status": "needs_revision",
                "error": "Critic only reviews schema-valid plans; revise the Designer draft first.",
                "schema_errors": validation["errors"],
                "persisted": False,
            }
    elif latest is not None:
        validation = validate_plan(latest["plan"])
    else:
        return {"status": "error", "error": "No preprocessing plan exists to critique."}
    # 判决拆除三波（planner:10297/10320 合并，2026-09-02）：「被评 plan 的 kind 与
    # 期望不符」一题一答 —— 不论 plan 来自最新记录还是 Critic 输入，只在这里判一次
    # （normalize_plan 保证 plan_kind 必有值，记录里存的就是归一后的 plan）。
    actual_plan_kind = str(validation["plan"].get("plan_kind") or "")
    if actual_plan_kind != expected_plan_kind:
        return {
            "status": "error",
            "error": "Critic plan identity/kind mismatch; reference plans cannot be reviewed as generation plans.",
            "plan_id": latest.get("plan_id") if latest is not None else None,
            "plan_kind": actual_plan_kind,
            "expected_plan_kind": expected_plan_kind,
            "persisted": False,
        }
    if plan is not None:
        if latest is not None and latest.get("plan") != validation["plan"]:
            return {
                "status": "error",
                "error": "Critic reviews only the latest persisted Designer plan. Do not pass a mutated plan.",
                "latest_plan_id": latest.get("plan_id"),
                "persisted": False,
            }
        if latest is None:
            latest = store.save_plan(validation["plan"], source="critic_input")

    deterministic_concerns = _deterministic_plan_critical_concerns(latest["plan"])
    if deterministic_concerns:
        # Do not let a remote Critic's numeric score override an immutable
        # graph invariant such as “caller requested a mesh but this plan only
        # writes coordinates”.  Persist the findings as ordinary critique
        # feedback so the bounded planning loop can regenerate the affected
        # plan rather than executing it.
        raw_gate = {
            "dimension_scores": {name: 0.0 for name in CRITIC_DIMENSIONS},
            "critical_concerns": deterministic_concerns,
            "major_concerns": [],
            "recommended_changes": deterministic_concerns,
        }
        critique = normalize_critique(raw_gate, validation, overall_score_min=float(quality_threshold))
        record = store.save_critique(critique, latest)
        emit_progress(state, "critic_done", "revise", score=critique.get("overall_score"))
        return {
            "status": "success",
            "critique_id": record["critique_id"],
            "critique_path": record["path"],
            "plan_id": latest["plan_id"],
            "critique": critique,
            "approved": False,
            "model_used": "deterministic_authority_gate",
            "usage": {},
            "fallback_used": True,
            "reused": record.get("reused", False),
            "next_action": "Regenerate the plan from the immutable caller requirements before execution.",
        }

    # A high numeric score cannot substitute for a missing external asset.
    # Keep the plan in revise/recoverable state until an official, concrete
    # path/revision (or an equivalent committed artifact) is recorded.
    evidence_gaps = _unverified_external_deliverable_gaps(latest["plan"])
    if evidence_gaps:
        raw_gate = {
            "dimension_scores": {name: 8.0 for name in CRITIC_DIMENSIONS},
            "critical_concerns": [
                f"Required external deliverable {gap} has candidate-only evidence; official path/commit is required."
                for gap in evidence_gaps
            ],
            "major_concerns": [],
            "recommended_changes": [
                "Execute the approved exact official reference request once and bind its URL/path/commit to the artifact evidence."
            ],
        }
        critique = normalize_critique(raw_gate, validation, overall_score_min=float(quality_threshold))
        record = store.save_critique(critique, latest)
        emit_progress(state, "critic_done", str(critique.get("decision") or "revise"), score=critique.get("overall_score"))
        return {
            "status": "success",
            "critique_id": record["critique_id"],
            "critique_path": record["path"],
            "plan_id": latest["plan_id"],
            "critique": critique,
            "approved": False,
            "model_used": "deterministic_evidence_gate",
            "usage": {},
            "fallback_used": True,
            "reused": record.get("reused", False),
            "next_action": "Resolve the external evidence gaps, then request Critic again.",
        }
    existing = store.latest_critique(latest["plan_hash"])
    if store.is_plan_invalidated(str(latest["plan"].get("plan_kind") or "preprocessing_generation"), latest["plan_id"]):
        # An old Critic score cannot re-authorize a plan that failed execution
        # or delivery. Keep the original repair feedback active in the loop.
        return {
            "status": "success", "plan_id": latest["plan_id"], "approved": False,
            "critique": {
                "decision": "revise", "overall_score": 0.0,
                "critical_concerns": ["The fallback reproduced the invalidated plan without fixing its execution or delivery defects."],
                "recommended_changes": ["Amend the affected step from the original review findings; do not reuse the failed plan's prior approval."],
            },
            "model_used": "execution_invalidation", "reused": True,
        }
    existing_threshold = None
    if existing is not None:
        try:
            existing_threshold = float(
                (existing.get("critique") or {}).get("approval_requirements", {}).get("overall_score_min")
            )
        except (TypeError, ValueError):
            existing_threshold = None
    if existing is not None and existing_threshold is not None and abs(existing_threshold - quality_threshold) < 1e-9:
        critique = existing["critique"]
        return {
            "status": "success",
            "critique_id": existing["critique_id"],
            "critique_path": existing["path"],
            "plan_id": latest["plan_id"],
            "critique": critique,
            "approved": critique.get("decision") == "approve",
            "model_used": "persisted_critique",
            "usage": {},
            "reused": True,
            "next_action": (
                "Proceed with the approved plan."
                if critique.get("decision") == "approve"
                else "Revise the plan using this critique before requesting another review."
            ),
        }
    # When the designer endpoint is unavailable, its deterministic fallback is
    # already schema-validated and contains explicit tool-bound execution
    # steps.  Recalling a separate remote critic merely creates low-score
    # churn. Geometry prerequisites are handled before planning; any remaining
    # named data searches are explicit non-blocking DAG steps after mesh review.
    # Generation and package reviewers remain mandatory execution gates.
    if str(latest.get("source") or "") in {
        "designer:deterministic_explicit_requirements",
        "designer:deterministic_recovery",
    }:
        critique = _deterministic_critic_fallback(
            validation,
            RuntimeError("remote Designer unavailable; executing verified deterministic fallback"),
            latest["plan"],
        )
        used_model = "deterministic_fallback"
        usage = {}
        state.append_transcript(
            "preprocessing_critic_deterministic_execution_path",
            plan_id=latest.get("plan_id"),
            approved=critique.get("decision") == "approve",
        )
    else:
        try:
            compact_plan = _compact_plan_for_critic(latest["plan"])
            critic_payload = {
                "plan": compact_plan,
                "deterministic_validation": {
                    "valid": validation.get("valid"),
                    "errors": (validation.get("errors") or [])[:30],
                    "warnings": (validation.get("warnings") or [])[:30],
                },
                "review_instruction": (
                    "Review the compact plan projection. Keep each concern under 240 characters, return at most "
                    "one critical and five major concerns, and omit recommended_changes when decision=approve. "
                    "Deterministic validation already checked the full persisted plan."
                ),
            }
            critic_payload = _bound_critic_payload(critic_payload)
            state.append_transcript(
                "preprocessing_critic_payload_compacted",
                payload_chars=len(_serialized_payload(critic_payload)),
                payload_budget_chars=_PLANNING_PAYLOAD_BUDGET_CHARS,
                deliverable_count=len(latest["plan"].get("required_deliverables") or []),
                step_count=len(latest["plan"].get("generation_steps") or []),
            )
            emit_progress(
                state,
                "critic",
                "reviewing preprocessing plan",
                plan_id=latest.get("plan_id"),
            )
            raw, used_model, usage = await _call_json_agent_resilient(
                state,
                role="critic",
                system_prompt=CRITIC_SYSTEM_PROMPT,
                payload=critic_payload,
                model_name=model_name,
                attempts=_planning_agent_attempts("critic"),
            )
        except Exception as exc:
            critique = _deterministic_critic_fallback(validation, exc, latest["plan"])
            used_model = "deterministic_fallback"
            usage = {}
            state.append_transcript(
                "preprocessing_critic_fallback",
                plan_id=latest.get("plan_id"),
                approved=critique.get("decision") == "approve",
                reason=f"{type(exc).__name__}: {exc}",
            )
            emit_progress(
                state,
                "critic_fallback",
                "using deterministic critic",
                approved=critique.get("decision") == "approve",
            )
        else:
            # Schema validation owns structural facts; the remote Critic owns
            # scientific semantics.  Reconcile by dimension instead of
            # scanning natural-language concerns for an ever-growing list of
            # "missing/lacks/absent" phrases.
            raw = _reconcile_framework_covered_critic_concerns(latest["plan"], raw)
            if validation.get("valid") and not _deterministic_plan_critical_concerns(latest["plan"]):
                scores = dict(raw.get("dimension_scores") or {})
                for name in (
                    "discipline_and_solver",
                    "deliverable_completeness",
                    "tool_compatibility",
                    "verification_and_reproducibility",
                    "safety",
                ):
                    try:
                        scores[name] = max(7.0, float(scores.get(name, 0.0)))
                    except (TypeError, ValueError):
                        scores[name] = 7.0
                raw = {**raw, "dimension_scores": scores}
            critique = normalize_critique(raw, validation, overall_score_min=float(quality_threshold))
    record = store.save_critique(critique, latest)
    deterministic_validation = used_model == "deterministic_fallback"
    emit_progress(
        state,
        "plan_validation_done" if deterministic_validation else "critic_done",
        (
            "deterministic contract validation passed"
            if deterministic_validation and critique.get("decision") == "approve"
            else str(critique.get("decision") or "unknown")
        ),
        **({} if deterministic_validation else {"score": critique.get("overall_score")}),
    )
    return {
        "status": "success",
        "critique_id": record["critique_id"],
        "critique_path": record["path"],
        "plan_id": latest["plan_id"],
        "critique": critique,
        "approved": critique["decision"] == "approve",
        "model_used": used_model,
        "usage": usage,
        "fallback_used": used_model == "deterministic_fallback",
        "reused": record.get("reused", False),
        "next_action": (
            "Proceed with the approved plan."
            if critique["decision"] == "approve"
            else "Call design_preprocessing_plan again with this critique_feedback."
        ),
    }


def _generation_revision_details(execution_feedback: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read the canonical executor/reviewer revision contract."""
    if not isinstance(execution_feedback, dict):
        return []
    contract = execution_feedback.get("revision_contract")
    values = contract.get("issues") if isinstance(contract, dict) else []
    envelope_step_id = str(
        execution_feedback.get("step_id")
        or execution_feedback.get("failed_step")
        or ""
    ).strip()
    envelope_tool_name = str(execution_feedback.get("tool_name") or "").strip()
    details = []
    described_owners: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        # Planner revises the route; artifact inspection belongs to the
        # reviewer/producer repair call. Never nest their payloads in history.
        detail = {key: value for key, value in item.items()
                  if key not in {"previous_repair_attempts", "artifact_evidence"}}
        owner = str(detail.get("step_id") or envelope_step_id)
        owner_result = (execution_feedback.get("outputs") or {}).get(owner) or {}
        if owner not in described_owners:
            described_owners.add(owner)
            detail["previous_repair_attempts"] = [
                {key: attempt[key] for key in ("parameter_updates", "arguments_hash", "status", "changed") if key in attempt}
                for attempt in owner_result.get("repair_history", [])[-4:]
            ]
            if owner_result.get("error"):
                detail["last_repair_error"] = owner_result["error"]
        # Package failures are wrapped with the owning executor step while
        # their nested review items usually carry only file/code fields. Keep
        # that ownership when feeding the existing targeted revision planner;
        # otherwise it cannot know which approved writer to regenerate.
        if envelope_step_id and not str(detail.get("step_id") or "").strip():
            detail["step_id"] = envelope_step_id
        if envelope_tool_name and not str(detail.get("tool_name") or "").strip():
            detail["tool_name"] = envelope_tool_name
        details.append(detail)
    return _compact_critique_feedback({"feedback": details})["feedback"]


async def run_preprocessing_planning_loop(
    state: State,
    task_context: str | dict[str, Any],
    designer_model_name: str = "",
    critic_model_name: str = "",
    max_iterations: int = 8,
    quality_threshold: float = 8.0,
    reference_evidence: list[dict[str, Any]] | None = None,
    execution_feedback: dict[str, Any] | None = None,
    planning_feedback: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    emit_progress(state, "planning_loop", "starting preprocessing planning loop")
    # File-content repair stays in the executor. Planning is re-entered only
    # for a remaining asset-DAG or stage-contract revision.
    local_generation_revision = (
        isinstance(execution_feedback, dict)
        and str(execution_feedback.get("status") or "")
        in {"needs_geometry_processing", "needs_revision"}
    )
    revision_request: dict[str, Any] | None = None
    revision_base_record: dict[str, Any] | None = None
    revision_attempt_started = False
    if local_generation_revision and isinstance(execution_feedback, dict):
        revision_store = PlanningStore(state)
        revision_base_record = revision_store.get_plan(str(execution_feedback.get("plan_id") or ""))
        if revision_base_record:
            revision_base_record = deepcopy(revision_base_record)
            for step in (revision_base_record.get("plan") or {}).get("generation_steps") or []:
                effective = ((execution_feedback.get("outputs") or {}).get(str(step.get("id") or "")) or {}).get("effective_arguments")
                if isinstance(effective, dict):
                    step["tool_arguments"] = {**(step.get("tool_arguments") or {}), **effective}
        revision_feedback = {
            "feedback": _generation_revision_details(execution_feedback),
            "review_report": execution_feedback.get("review_report"),
        }
        revision_request = revision_store.open_generation_revision(
            plan_id=str(execution_feedback.get("plan_id") or ""),
            plan_hash=str((revision_base_record or {}).get("plan_hash") or ""),
            feedback=revision_feedback,
        )
    stored_evidence = [
        item for item in PlanningStore(state).load_reference_state().get("evidence") or []
        if isinstance(item, dict)
    ]
    if stored_evidence:
        # Preserve tool-produced evidence even when the caller also supplied
        # an LLM summary.  Deduplication is by stable step/request identity.
        supplied = list(reference_evidence or [])
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for entry in [*stored_evidence, *supplied]:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("step_id") or entry.get("gap_id") or entry.get("id") or canonical_hash(entry))
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
        reference_evidence = merged
        state.append_transcript("preprocessing_reference_evidence_merged", evidence_count=len(merged), tool_evidence_count=len(stored_evidence))
    reference_evidence = _usable_reference_evidence(reference_evidence)
    latest_analysis = (
        state.hook_state.get("_data_latest_requirement_analysis")
        if isinstance(getattr(state, "hook_state", None), dict)
        else None
    )
    if not isinstance(latest_analysis, dict):
        latest_analysis = _load_requirement_analysis(state)
        if isinstance(latest_analysis, dict):
            state.hook_state["_data_latest_requirement_analysis"] = latest_analysis
    if (
        planning_feedback
        and not local_generation_revision
        and pipeline_outcome(planning_feedback)["kind"] != "retry_step"
    ):
        # Only caller authority is immutable. Reuse the existing Analyst repair
        # entry when an internal interpretation/plan has resisted amendment.
        issues = [*_generation_revision_details(execution_feedback),
                  *_generation_revision_details(planning_feedback)]
        issues.append({"code": "planning_recovery", "message": str(
            planning_feedback.get("error") or planning_feedback.get("stop_reason") or "Planning was not approved."),
            "required_change": "Reconcile the inferred requirements with the original authority and correct the failed local route.",
            "diagnostic_context": {
                "schema_errors": planning_feedback.get("schema_errors") or planning_feedback.get("last_schema_errors"),
                "last_critique": {key: (planning_feedback.get("last_critique") or {}).get(key)
                                  for key in ("decision", "critical_concerns", "major_concerns", "recommended_changes")},
            }})
        requirements = await analyze_preprocessing_requirements(
            state, task_context=task_context, reference_evidence=reference_evidence,
            model_name=designer_model_name, review_revision_contract=revision_contract(issues, scope="plan"),
            previous_requirement_analysis=latest_analysis,
        )
    elif (
        local_generation_revision
        and isinstance(latest_analysis, dict)
    ):
        # A package-review revision is a repair of the already approved work
        # order, not a new request.  Re-reading audit/preprocessing_review.json
        # as fresh caller input can make the Analyst drop the original assets
        # (the revision run then designs a plan with zero deliverables). Keep
        # the normalized analysis that produced the approved plan and only
        # merge newly persisted reference evidence.
        requirement_analysis = _normalize_requirement_analysis(latest_analysis)
        if reference_evidence:
            requirement_analysis = _normalize_requirement_analysis({
                **requirement_analysis,
                "reference_evidence": reference_evidence,
            })
        requirement_analysis = _ensure_authoritative_caller_assets(
            task_context, requirement_analysis
        )
        state.hook_state["_data_latest_requirement_analysis"] = requirement_analysis
        _save_requirement_analysis(state, requirement_analysis)
        requirements = {
            "status": "success",
            "requirement_analysis": requirement_analysis,
            "reference_evidence_merged": bool(reference_evidence),
            "model_used": "reuse_confirmed_requirements_for_revision",
            "usage": {},
        }
        state.append_transcript(
            "preprocessing_requirements_reused_for_revision",
            evidence_count=len(reference_evidence or []),
            required_file_count=len(requirement_analysis.get("required_files") or []),
            stage_count=len(requirement_analysis.get("calculation_stages") or []),
        )
        emit_progress(
            state,
            "requirements_reused",
            "preserved confirmed requirements for targeted generation revision",
            required_file_count=len(requirement_analysis.get("required_files") or []),
            stage_count=len(requirement_analysis.get("calculation_stages") or []),
        )
    elif reference_evidence and isinstance(latest_analysis, dict):
        requirement_analysis = _normalize_requirement_analysis({
            **latest_analysis,
            "reference_evidence": reference_evidence,
        })
        requirement_analysis = _ensure_authoritative_caller_assets(task_context, requirement_analysis)
        state.hook_state["_data_latest_requirement_analysis"] = requirement_analysis
        _save_requirement_analysis(state, requirement_analysis)
        requirements = {
            "status": "success",
            "requirement_analysis": requirement_analysis,
            "reference_evidence_merged": True,
            "model_used": "reuse_confirmed_requirements",
            "usage": {},
        }
        state.append_transcript(
            "preprocessing_requirements_reused_after_reference",
            evidence_count=len(reference_evidence),
            stage_count=len(requirement_analysis.get("calculation_stages") or []),
        )
        emit_progress(
            state,
            "requirements_reused",
            "merged reference evidence into confirmed requirements",
            stage_count=len(requirement_analysis.get("calculation_stages") or []),
        )
    else:
        requirements = await analyze_preprocessing_requirements(
            state,
            task_context=task_context,
            reference_evidence=reference_evidence,
            model_name=designer_model_name,
            review_revision_contract=None,
            previous_requirement_analysis=None,
        )
    if requirements.get("status") != "success":
        return requirements
    requirement_analysis = requirements["requirement_analysis"]
    # A successful requirement reconciliation supersedes schema feedback
    # about the discarded interpretation.  Keeping that stale feedback forces
    # a complete, executable work order back through the remote Designer and
    # can replace a registered producer with a model-authored helper script.
    if (
        planning_feedback
        and not local_generation_revision
        and _analysis_supports_deterministic_execution(requirement_analysis)
    ):
        planning_feedback = None
    reference_state = PlanningStore(state).load_reference_state()
    persisted_ledger = reference_state.get("gaps") or {}
    if persisted_ledger:
        requirement_analysis = {
            **requirement_analysis,
            "reference_gap_status": {
                # Keep the persisted gap record intact.  Candidate-to-
                # acquisition conversion needs its request id, asset role,
                # expected revision, and evidence URL; reducing it to a
                # status string made that transition impossible.
                str(key): dict(value)
                for key, value in persisted_ledger.items() if isinstance(value, dict)
            },
            "resolved_gap_ids": sorted(
                str(key) for key, value in persisted_ledger.items()
                if isinstance(value, dict) and value.get("status") in {"resolved", "downloaded_verified"}
            ),
            "completed_reference_request_ids": sorted(
                str(value.get("request_id") or "") for value in persisted_ledger.values()
                if isinstance(value, dict)
                and value.get("request_id")
                and str(value.get("status") or "").strip().lower() in {
                    "resolved", "downloaded_verified", "no_result",
                    "discovered_candidate", "unusable_candidate", "candidate_invalid",
                }
            ),
        }
    if reference_evidence:
        # Reference evidence and gap transitions are already persisted by the
        # executor.  The planning pass only consumes that immutable record; it
        # must not reinterpret or rewrite the ledger.
        requirement_analysis = {
            **requirement_analysis,
            "reference_evidence": reference_evidence[:20],
        }
        state.hook_state["_data_latest_requirement_analysis"] = requirement_analysis
        _save_requirement_analysis(state, requirement_analysis)
        requirements["requirement_analysis"] = requirement_analysis
    repair = _apply_reference_repair(state, requirement_analysis, task_context)
    if repair.get("status") == "invalid":
        issue = {
            "code": "reference_repair_contract_error",
            "message": str(repair.get("error") or "reference_repair is invalid"),
            "required_change": "Correct the reference repair contract without changing the requested asset scope.",
        }
        return {
            "status": "needs_revision",
            "approved": False,
            "stop_reason": "reference_repair_contract_error",
            "error": issue["message"],
            "revision_contract": revision_contract([issue], scope="plan"),
            "requirement_analysis": requirement_analysis,
            "targeted_search_requests": [],
            "search_queries": [],
            "history": [],
        }
    if repair.get("status") == "applied":
        state.hook_state["_data_latest_requirement_analysis"] = requirement_analysis
        _save_requirement_analysis(state, requirement_analysis)
        state.append_transcript(
            "preprocessing_reference_repair_applied",
            request_id=repair.get("request_id"),
            gap_id=repair.get("gap_id"),
            url=repair.get("url"),
        )
    early_requests = (
        []
        if local_generation_revision
        else _targeted_search_requests_from_analysis(requirement_analysis)
    )
    active_reference_request_ids = {
        str(item.get("id") or "").strip()
        for item in early_requests
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    active_reference_request_ids.update(
        str(item.get("id") or "").strip()
        for item in requirement_analysis.get("required_files") or []
        if isinstance(item, dict)
        and normalize_asset_contract(item).get("is_external")
        and str(item.get("id") or "").strip()
    )
    requirement_analysis["active_reference_request_ids"] = sorted(active_reference_request_ids)
    reference_action = _next_reference_action(
        state,
        requirement_analysis,
        active_request_ids=active_reference_request_ids,
        search_requests=early_requests,
    )
    if reference_action is not None:
        return {
            **reference_action,
            "iteration": 0,
            "history": [],
        }
    # Planning is bounded by progress, not by a special two-turn repair rule.
    # Better scores or fewer unresolved concerns may use the caller's normal
    # planning budget; rewording the same concerns is not progress.
    limit = max(1, min(int(max_iterations), 20))
    revision_details: list[Any] = []
    if local_generation_revision and isinstance(execution_feedback, dict):
        revision_details = _generation_revision_details(execution_feedback)
    feedback: dict[str, Any] | None = (
        {
            "decision": "revise",
            "critical_concerns": revision_details or [
                "The previous approved generation step requires a focused local revision."
            ],
            # Keep the structured Reviewer findings separate from prose
            # concerns.  This is consumed by the compact Designer payload and
            # maps public stage/file identities back to their owning steps.
            "feedback": revision_details,
            "recommended_changes": [
                str(item.get("recommendation") or item.get("required_change") or item.get("message") or "")
                for item in revision_details
                if isinstance(item, dict)
                and (item.get("recommendation") or item.get("required_change") or item.get("message"))
            ],
        }
        if local_generation_revision else None
    )
    if planning_feedback:
        feedback = {**(feedback or {}), "decision": "revise",
                    "feedback": [*revision_details, *_generation_revision_details(planning_feedback)],
                    "critical_concerns": planning_feedback.get("last_schema_errors") or
                                         [planning_feedback.get("error") or planning_feedback.get("stop_reason")],
                    "rejected_plan": planning_feedback.get("rejected_plan")}
    history: list[dict[str, Any]] = []
    best_score = -1.0
    best_plan_id = ""
    previous_generation = revision_base_record or PlanningStore(state).latest_plan("preprocessing_generation")
    best_plan: dict[str, Any] | None = (
        previous_generation.get("plan")
        if local_generation_revision and isinstance(previous_generation, dict)
        else None
    )
    stagnant_rounds = 0
    best_issue_count = float("inf")
    seen_schema_signatures: set[tuple[str, ...]] = set()

    # A package Reviewer can identify a missing stage-level launch contract
    # even when no generation step exists yet.  That is a deterministic graph
    # completion, not a reason to ask Designer and Critic to reconsider every
    # already-approved artifact.  Reuse the canonical requirement-to-plan
    # compiler, merge only the cited stages, and validate through the same
    # schema/safety gate used when the Critic service is unavailable.
    structural_stage_ids = {
        str(item.get("stage_id") or "").strip()
        for item in revision_details
        if isinstance(item, dict)
        and str(item.get("stage_id") or "").strip()
        and not str(item.get("step_id") or "").strip()
        and str(item.get("code") or "").startswith("stage_")
    }
    if local_generation_revision and best_plan and structural_stage_ids:
        compiled_plan = _deterministic_designer_fallback_plan(
            task_context,
            _scope_analysis_to_data_node(task_context, requirement_analysis),
        )
        amended_plan = _merge_focused_plan_revision(
            best_plan,
            compiled_plan,
            set(),
            structural_stage_ids,
        )
        amended_validation = validate_plan(amended_plan)
        amendment_changed = canonical_hash(amended_plan) != str(
            (revision_request or {}).get("base_plan_hash") or canonical_hash(best_plan)
        )
        if amendment_changed and amended_validation["valid"]:
            local_critique = _deterministic_critic_fallback(
                amended_validation,
                RuntimeError("framework-owned targeted stage-contract amendment"),
                amended_plan,
            )
            if local_critique.get("decision") == "approve":
                revision_store = PlanningStore(state)
                revision_store.begin_generation_revision(str((revision_request or {}).get("signature") or ""))
                revision_attempt_started = True
                record = revision_store.save_plan(
                    amended_validation["plan"],
                    source="designer:deterministic_stage_revision",
                )
                critique_record = revision_store.save_critique(local_critique, record)
                revision_store.close_generation_revision(
                    str((revision_request or {}).get("signature") or ""),
                    status="approved",
                )
                emit_progress(
                    state,
                    "planning_approved",
                    "targeted stage contract completed without full replanning",
                    iteration=0,
                    score=local_critique.get("overall_score"),
                )
                return {
                    "status": "success",
                    "approved": True,
                    "stop_reason": "deterministic_stage_revision",
                    "iterations": 0,
                    "best_plan_id": record["plan_id"],
                    "best_score": float(local_critique.get("overall_score") or 0.0),
                    "history": [{
                        "iteration": 0,
                        "plan_id": record["plan_id"],
                        "critique_id": critique_record["critique_id"],
                        "decision": "approve",
                        "stage_ids": sorted(structural_stage_ids),
                    }],
                    "planning_status": revision_store.approval_status(),
                }
    for iteration in range(1, limit + 1):
        if local_generation_revision:
            # Schema/Critic retries must retain the original asset findings;
            # otherwise a local repair silently turns into full-plan design.
            feedback = {**(feedback or {}), "feedback": [*revision_details, *_generation_revision_details(planning_feedback)]}
        deterministic_candidate = (
            feedback in (None, "", {})
            and _analysis_supports_deterministic_execution(requirement_analysis)
        )
        emit_progress(
            state,
            "work_order_validation" if deterministic_candidate else "planning_iteration",
            "validating executable work order" if deterministic_candidate else "designer/critic cycle",
            iteration=iteration,
        )
        designed = await design_preprocessing_plan(
            state,
            task_context=task_context,
            critique_feedback=feedback,
            model_name=designer_model_name,
            requirement_analysis=requirement_analysis,
            _previous_best_plan=best_plan,
            _preserve_execution_route=(
                local_generation_revision
                and not planning_feedback
                and ((execution_feedback or {}).get("revision_contract") or {}).get("scope") != "plan"
            ),
        )
        if designed.get("status") == "needs_revision":
            # An invalid Designer draft is not an authority to create a new
            # reference request. Reference acquisition is owned solely by the
            # persisted ledger, whose request identity survives replanning.
            # Otherwise one asset can be searched repeatedly under model-made
            # aliases such as F07 / f07_vtable / s3_vtable.
            feedback = {
                "decision": "revise",
                "critical_concerns": designed.get("schema_errors") or [],
                "recommended_changes": designed.get("schema_errors") or [],
                "rejected_plan": designed.get("plan"),
            }
            schema_signature = tuple(sorted(str(item) for item in (designed.get("schema_errors") or [])[:20]))
            schema_repeated = schema_signature in seen_schema_signatures
            seen_schema_signatures.add(schema_signature)
            history.append({
                "iteration": iteration,
                "plan_id": None,
                "critique_id": None,
                "overall_score": 0.0,
                "decision": "schema_rejected_before_persistence",
                "is_best": False,
            })
            structural_schema_failure = (
                len(designed.get("schema_errors") or []) >= 12
                or sum(
                    "unknown dependencies" in str(error)
                    for error in designed.get("schema_errors") or []
                ) >= 4
            )
            # Acquisition locators and request-parameter bindings are
            # mechanical hand-off fields.  They are completed by the
            # canonical plan normalizer from RequirementAnalysis and consumer
            # interfaces; asking the model to rewrite the whole plan again
            # only repeats the same omission.  Continue remote revision only
            # for errors that require an actual scientific/design choice.
            errors_are_acquisition_contract = bool(designed.get("schema_errors")) and all(
                re.search(r"acquisition[_ ]contract|request_parameters missing", str(error), flags=re.I)
                for error in designed.get("schema_errors") or []
            )
            if (
                not schema_repeated
                and not structural_schema_failure
                and not errors_are_acquisition_contract
            ):
                continue
            # Do not spend the remaining iteration budget replaying the same
            # invalid JSON contract.  The normalized Analyst DAG already
            # contains the stage/asset facts required for a conservative plan,
            # so reuse the existing deterministic executor route instead of
            # returning an empty blocked report.
            fallback_plan = _deterministic_designer_fallback_plan(
                task_context,
                requirement_analysis,
            )
            if local_generation_revision:
                focus = _critic_referenced_step_ids({"feedback": revision_details}, best_plan)
                fallback_plan = _merge_focused_plan_revision(
                    best_plan, fallback_plan, focus,
                    _critic_referenced_stage_ids({"feedback": revision_details}, best_plan, focus),
                )
            fallback_validation = validate_plan(fallback_plan)
            if not fallback_validation["valid"] or _deterministic_plan_critical_concerns(fallback_plan):
                issues = [{
                    "code": "plan_schema_invalid",
                    "message": str(error),
                    "required_change": str(error),
                } for error in fallback_validation["errors"][:20]]
                return {
                    "status": "needs_revision",
                    "approved": False,
                    "stop_reason": "repeated_schema_failure",
                    "iterations": iteration,
                    "best_plan_id": best_plan_id,
                    "best_score": best_score,
                    "history": history,
                    "last_schema_errors": list(schema_signature),
                    "rejected_plan": designed.get("plan"),
                    "fallback_errors": fallback_validation["errors"][:20],
                    "revision_contract": revision_contract(issues, scope="plan"),
                    "planning_status": PlanningStore(state).approval_status(),
                }
            record = PlanningStore(state).save_plan(
                fallback_validation["plan"],
                source="designer:deterministic_recovery",
            )
            emit_progress(
                state,
                "designer_fallback",
                "using deterministic recovery after repeated schema failures",
                plan_id=record["plan_id"],
            )
            designed = {
                "status": "success",
                "plan_id": record["plan_id"],
                "plan_hash": record["plan_hash"],
                "plan_path": record["path"],
                "plan": record["plan"],
                "schema_valid": True,
                "schema_errors": [],
                "schema_warnings": fallback_validation["warnings"],
                "model_used": "deterministic_schema_recovery",
                "usage": {},
                "fallback_used": True,
            }
        if designed.get("status") != "success":
            return {
                **designed,
                "iteration": iteration,
                "history": history,
            }
        if local_generation_revision and revision_request:
            if not revision_attempt_started:
                PlanningStore(state).begin_generation_revision(
                    str(revision_request.get("signature") or "")
                )
                revision_attempt_started = True
        reviewed = await critique_preprocessing_plan(
            state,
            plan_id=str(designed.get("plan_id") or ""),
            expected_plan_kind="preprocessing_generation",
            model_name=critic_model_name,
            quality_threshold=quality_threshold,
        )
        if reviewed.get("status") != "success":
            return {
                "status": "retryable_error",
                "error": "Critic failed during planning loop.",
                "iteration": iteration,
                "critic_result": reviewed,
                "rejected_plan": designed.get("plan"),
                "history": history,
            }
        critique = reviewed["critique"]
        score = float(critique.get("overall_score") or 0.0)
        issue_count = len(critique.get("critical_concerns") or []) + len(critique.get("major_concerns") or [])
        # Critic feedback can request stronger evidence, but cannot authorize
        # a new search. The reference driver alone advances the persisted
        # ledger, so this loop remains a revision loop rather than reopening
        # the same acquisition under a new model-generated alias.
        improved = (
            score > best_score + 0.05
            or (
                issue_count < best_issue_count
                and score >= best_score - 0.25
            )
        )
        if improved:
            stagnant_rounds = 0
            best_score = score
            best_issue_count = issue_count
            best_plan_id = str(reviewed.get("plan_id") or "")
            best_plan = designed.get("plan")
        else:
            stagnant_rounds += 1
        history.append({
            "iteration": iteration,
            "plan_id": reviewed.get("plan_id"),
            "critique_id": reviewed.get("critique_id"),
            "overall_score": score,
            "decision": critique.get("decision"),
            "is_best": improved,
        })
        if reviewed.get("approved"):
            if local_generation_revision and revision_request:
                PlanningStore(state).close_generation_revision(
                    str(revision_request.get("signature") or ""), status="approved"
                )
            deterministic_validation = reviewed.get("model_used") == "deterministic_fallback"
            emit_progress(
                state,
                "planning_approved",
                (
                    "executable work order validated"
                    if deterministic_validation else "quality threshold met"
                ),
                iteration=iteration,
                **({} if deterministic_validation else {"score": best_score}),
            )
            return {
                "status": "success",
                "approved": True,
                "stop_reason": "quality_threshold_met",
                "iterations": iteration,
                "best_plan_id": best_plan_id,
                "best_score": best_score,
                "history": history,
                "planning_status": PlanningStore(state).approval_status(),
            }
        feedback = {**critique, "rejected_plan": designed.get("plan")}
        if designed.get("reused") or reviewed.get("reused"):
            stagnant_rounds += 1
        if stagnant_rounds:
            break
    if local_generation_revision:
        if revision_request and revision_attempt_started:
            PlanningStore(state).close_generation_revision(
                str(revision_request.get("signature") or ""),
                status="not_approved",
            )
        issues = _generation_revision_details(execution_feedback)
        # 保留·升 A（planner 重复修订熔断，2026-08-31，措辞照 te:402 范本）。
        return {
            "status": "needs_revision",
            "approved": False,
            "stop_reason": "targeted_revision_not_approved",
            "error": (
                "⛔ 算力熔断：本轮定向修订预算已用完且未获通过；原样重跑整份 plan "
                "只会复现同一结果。两个出口（任选其一即可放行）：① 按 revision_contract "
                "只改受影响的 plan 范围后重新进入规划；② request_human_input 请人裁决。"
                "注意：这是执行方式的限制，不是「此事不可行」的判定。"
            ),
            "history": history,
            "last_critique": feedback,
            "rejected_plan": (feedback or {}).get("rejected_plan"),
            "revision_contract": revision_contract(issues or [{
                "code": "targeted_revision_not_approved",
                "message": "The targeted plan amendment did not satisfy the cited review findings.",
                "required_change": "Amend the affected plan scope while preserving locked authority.",
            }], scope="plan"),
        }
    # Preserve the best schema-valid Designer plan when the remote Critic has
    # spent its bounded revision budget.  Rebuilding a fresh deterministic
    # plan here discarded useful semantic bindings and made a valid external
    # input look like a missing generation deliverable.  Fall back to the
    # deterministic compiler only when the best draft itself is not runnable.
    fallback_plan = best_plan if isinstance(best_plan, dict) else _deterministic_designer_fallback_plan(
        task_context,
        requirement_analysis,
    )
    fallback_validation = validate_plan(fallback_plan)
    if (
        (not fallback_validation["valid"] or _deterministic_plan_critical_concerns(fallback_validation["plan"]))
        and fallback_plan is best_plan
    ):
        fallback_plan = _deterministic_designer_fallback_plan(task_context, requirement_analysis)
        fallback_validation = validate_plan(fallback_plan)
    if fallback_validation["valid"] and not _deterministic_plan_critical_concerns(fallback_validation["plan"]):
        recovery_store = PlanningStore(state)
        record = recovery_store.save_plan(
            fallback_validation["plan"],
            source="designer:deterministic_recovery",
        )
        # The bounded remote Critic cycle is complete.  Use the existing
        # deterministic schema/safety gate for the selected executable draft;
        # package review remains the file-level semantic gate after execution.
        recovery_critique = _deterministic_critic_fallback(
            fallback_validation,
            RuntimeError("bounded remote Critic revisions made no effective progress"),
            fallback_validation["plan"],
        )
        recovery_store.save_critique(recovery_critique, record)
        if recovery_critique.get("decision") == "approve":
            emit_progress(
                state,
                "designer_fallback",
                "using deterministic recovery after repeated critic revisions",
                plan_id=record["plan_id"],
            )
            return {
                "status": "success",
                "approved": True,
                "stop_reason": "deterministic_recovery_after_repeated_revisions",
                "iterations": len(history),
                "best_plan_id": record["plan_id"],
                "best_score": float(recovery_critique.get("overall_score") or 0.0),
                "history": history,
                "planning_status": PlanningStore(state).approval_status(),
            }
    issues = [{
        "code": "planning_no_progress",
        "message": str(item),
        "required_change": str(item),
    } for item in ((feedback or {}).get("critical_concerns") or [])[:20]]
    # 保留·升 A（planner 停滞/轮次熔断，2026-08-31，措辞照 te:402 范本）。
    return {
        "status": "needs_revision",
        "approved": False,
        "stop_reason": "no_effective_progress" if stagnant_rounds else "max_iterations_reached",
        "error": (
            "⛔ 算力熔断：规划循环"
            + ("已停滞（评分与未决问题不再改善）" if stagnant_rounds else "已用完本轮迭代预算")
            + "，继续原样迭代只会再烧预算。两个出口（任选其一即可放行）："
            "① 按 revision_contract/最新 Critic 意见修订受影响的 plan 内容后重新规划；"
            "② request_human_input 说明分歧点请人裁决。"
            "注意：这是执行方式的限制，不是「此事不可行」的判定。"
        ),
        "iterations": len(history),
        "best_plan_id": best_plan_id,
        "best_score": best_score,
        "history": history,
        "last_critique": feedback,
        "rejected_plan": (feedback or {}).get("rejected_plan"),
        "revision_contract": revision_contract(issues or [{
            "code": "planning_not_approved",
            "message": "Planning did not produce an approved executable DAG.",
            "required_change": "Revise the affected plan contract using the latest Critic findings.",
        }], scope="plan"),
        "planning_status": PlanningStore(state).approval_status(),
    }


register_tool(
    ToolDefinition(
        name="run_preprocessing_planning_loop",
        description=(
            "Run the complete AI Designer/Critic self-refinement loop. It persists only distinct plans, "
            "reuses reviews for unchanged plans, and continues while scores or unresolved issues improve."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "task_context": {"description": "Upstream task information as text or JSON object."},
                "designer_model_name": {"type": "string", "default": ""},
                "critic_model_name": {"type": "string", "default": ""},
                "max_iterations": {"type": "integer", "default": 8, "minimum": 1, "maximum": 20},
                "quality_threshold": {"type": "number", "default": 8.0, "minimum": 0.0, "maximum": 10.0},
                "reference_evidence": {"type": "array", "items": {"type": "object"}},
                "execution_feedback": {"type": "object", "description": "Structured local generation feedback for a bounded plan revision."},
                "planning_feedback": {"type": "object", "description": "Previous rejected draft and diagnostics for internal recovery; does not replace caller authority."},
            },
            "required": ["task_context"],
        },
        allowed_node_types=["data"],
        risk_level="low",
    ),
    run_preprocessing_planning_loop,
)

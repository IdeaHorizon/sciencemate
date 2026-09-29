"""Deterministic preflight contract for one Experiment run.

This is deliberately node-local.  It validates the inputs and execution
contract consumed by Experiment; it does not inspect how an upstream node
created those inputs or enforce policy for other nodes.
"""
from __future__ import annotations

from typing import Any



# 执行阶段词表的唯一真相源：safe_run_bash / safe_execute_python / submit_job 的
# schema enum 从这里取（派发口按 schema 核值），`_STAGE_ALIASES` 从这里派生——
# 两份词表不再各自演化（判决拆除·第三波「一题一答」，2026-09-02）。
EXECUTION_STAGES: tuple[str, ...] = ("diagnostic", "toolchain_build", "simulation")
# run contract 里的历史写法（build/compile）是读侧别名，只进不出。
_STAGE_ALIASES = {
    **{stage: ("build" if stage == "toolchain_build" else stage) for stage in EXECUTION_STAGES},
    "build": "build", "compile": "build",
}


def _register_prereg_dispatch_blocker(state: Any, *, kind: str, candidates: list[str] | None = None, declared_id: str | None = None) -> None:
    """Escalate a caller-owned prereg selection failure exactly once per run."""
    candidates = [str(item) for item in (candidates or []) if str(item)]
    identity = declared_id or "|".join(candidates) or "none"
    key = f"_prereg_dispatch_blocker:{kind}:{identity}"
    if state.hook_state.get(key):
        return
    blocker = {
        "blocker_id": f"{state.run_id}:prereg_dispatch:{kind}",
        "reporting_node": "experiment",
        "category": "other",
        "summary": "Experiment cannot select the frozen pre_registration for this run; only the orchestrator may declare it.",
        "evidence_paths": [],
        "requested_action": "Re-dispatch experiment with node_inputs.prereg_artifact_id set to one listed frozen pre_registration artifact ID.",
        "suggested_owner": "_orchestrator",
        "retryable_after_change": True,
        "prereg_candidates": candidates,
        "declared_prereg_artifact_id": declared_id,
    }
    state.hook_state.setdefault("blockers", []).append(blocker)
    state.hook_state[key] = True
    # The blocker is the control-plane record. Transcript observability must
    # not hide it when a diagnostic/recovery caller supplies a State whose
    # run directory has not been materialized yet.
    try:
        state.append_transcript("experiment_prereg_dispatch_blocked", **blocker)
    except OSError:
        pass


def _any_frozen_prereg(state: Any) -> dict[str, Any] | None:
    """项目里是否存在**冻结过**的预注册（任一身份的最新冻结版）。

    版本原语（RFC 2026-08-18）之后，"哪份 prereg 治理本轮"由 run contract
    绑定版本三元组回答；这里只回答存在性。旧实现取 `list_artifacts[-1]` ——
    在身份碎裂时代那是抽签（可能抽到一份草稿），现在也不再需要：head 若是
    修订草稿，latest_frozen_artifact 仍能给出冻结版，未冻结草稿则如实返 None。
    """
    try:
        entries = state.list_artifacts("pre_registration")
    except Exception:
        return None
    for entry in reversed(entries):
        try:
            frozen = state.latest_frozen_artifact(str(entry.get("id") or ""))
        except Exception:
            frozen = None
        if isinstance(frozen, dict):
            return frozen
    return None


def _flatten_params(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix or "value": value}
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            flattened.update(_flatten_params(item, path))
        else:
            flattened[path] = item
    return flattened


def audit_execution_contract(
    state: Any, execution_params: dict[str, Any] | None, *, stage: str,
    runner: str | None = None,
) -> dict[str, Any]:
    """Authorize a primary simulation only when it exactly matches frozen prereg parameters."""
    try:
        try:
            from tools.run_contract import (
                load_run_contract, run_role_non_applicability_reason,
            )
        except ImportError:
            from nodes.experiment.tools.run_contract import (
                load_run_contract, run_role_non_applicability_reason,
            )
        contract = load_run_contract(state)
    except Exception as exc:
        return {"passed": False, "reason": f"cannot load run contract: {type(exc).__name__}",
                "stage": stage, "blocking_reasons": ["run_contract"]}
    requested_stage = str(stage or "simulation").strip().lower()
    normalized_stage = _STAGE_ALIASES.get(requested_stage)
    if normalized_stage is None:
        return {"passed": False, "stage": requested_stage, "applicable": False,
                "reason": "stage must be diagnostic, build, or simulation",
                "blocking_reasons": ["stage_invalid"]}
    if normalized_stage != "simulation":
        return {"passed": True, "stage": normalized_stage, "applicable": False,
                "reason": "non-simulation stage does not consume scientific execution parameters",
                "blocking_reasons": []}
    # 本项目有多份冻结 prereg 而调用方没指名 —— 这时替它选一份就是抽签，
    # 抽错的后果是"拿上一轮的参数卡本轮方案"，且节点无从自救（E2E v20）。
    # 吵着挡住，并把候选和该怎么办一起给出来。
    ambiguous = contract.get("ambiguous_preregs")
    if ambiguous:
        _register_prereg_dispatch_blocker(state, kind="ambiguous_preregs", candidates=list(ambiguous))
        return {
            "passed": False, "stage": normalized_stage, "applicable": True,
            "reason": (
                "本轮无法唯一确定 frozen pre_registration，已向 orchestrator 登记 blocker："
                + "、".join(ambiguous)
                + "。请保留已有工作并结束本轮；orchestrator 必须在重派时于 node_inputs 里加 "
                  "prereg_artifact_id=\"<上面其中一个>\" 指名本轮的预注册；"
                  "跨 session 继承的旧预注册不会自动失效，所以必须显式声明。"
            ),
            "blocking_reasons": ["prereg_ambiguous"],
            "candidates": ambiguous,
        }
    pending = contract.get("pending_amendments")
    if pending:
        # 修订草稿挂着时不许静默按旧冻结版跑 —— 那是"赶在修订落地前抢跑一轮"
        # 的作弊窗口（RFC 2026-08-18）。两条出路都写明，让调度方选。
        items = "; ".join(
            f"{item.get('artifact_id')}（草稿 v{item.get('draft_version')} 未冻结，"
            f"最近冻结版 v{item.get('latest_frozen_version')}）"
            for item in pending if isinstance(item, dict)
        )
        _register_prereg_dispatch_blocker(
            state, kind="prereg_amendment_pending",
            candidates=[str(item.get("artifact_id")) for item in pending
                        if isinstance(item, dict)],
        )
        return {
            "passed": False, "stage": normalized_stage, "applicable": True,
            "reason": (
                "预注册有未冻结的修订草稿挂着，本轮没有合法的绑定版本：" + items
                + "。二选一：① hypothesis 完成修订并 freeze_artifact 后重派；"
                  "② orchestrator 显式声明本轮按旧冻结版跑 —— 重派时在 "
                  "node_inputs 里同时给 prereg_artifact_id 和 "
                  "prereg_version=<上面的冻结版号>。不声明就不跑，防止实验"
                  "抢在修订落地前按旧承诺出结果。"
            ),
            "blocking_reasons": ["prereg_amendment_pending"],
            "pending_amendments": pending,
        }
    if contract.get("contract_source") == "declared_prereg_not_found":
        _register_prereg_dispatch_blocker(state, kind="declared_prereg_not_found", declared_id=str(contract.get("declared_prereg_id") or ""))
        return {
            "passed": False, "stage": normalized_stage, "applicable": True,
            "reason": (
                "node_inputs.prereg_artifact_id 指向的预注册不存在或尚未冻结。"
                "先确认它已 freeze，或改指一份已冻结的。"
            ),
            "blocking_reasons": ["declared_prereg_not_found"],
        }
    # 这道门问的是"这趟有没有正式结论要交"——直接读那条声明，不再借道
    # analysis_eligible。副作用是被记过执行前提见证的运行也照样进参数等值审计：
    # 见证只记账、不豁免（owner 2026-09-11）。
    primary = bool(contract.get("requires_hypothesis_verdict"))
    try:
        try:
            from .input_delivery import active_input_delivery_entries
        except ImportError:
            from tools.input_delivery import active_input_delivery_entries
        delivery = {
            spec_id: entry.get("delivery") or {}
            for spec_id, entry in active_input_delivery_entries(state).items()
            if isinstance(entry, dict)
        }
    except Exception as exc:
        return {
            "passed": False,
            "stage": normalized_stage,
            "applicable": True,
            "reason": f"input delivery ledger cannot be audited: {type(exc).__name__}",
            "blocking_reasons": ["input_delivery_ledger_unreadable"],
        }
    # 只有**有科学权威**的交付才把本轮拉进"参数必须与冻结 prereg 逐键相等"模式。
    # preprocessing_service_request（无 prereg 背书的网格/输入包请求）verified 之后
    # 若也算数，一个 secondary simulation 就会被要求拿出它根本没有的 expected_params，
    # 下面那条 expected_params_missing 会硬 fail 且无解 —— 补出口反倒造出新死锁。
    # 缺键按 True 处理：历史记录和外部构造的 state 保持原语义。
    requires_match = primary or any(
        isinstance(item, dict) and item.get("verified") and item.get("scientific_authority", True)
        for item in delivery.values())
    if not requires_match:
        return {"passed": True, "stage": normalized_stage, "applicable": False,
                "reason": "secondary simulation without a formal input contract cannot yield a confirmatory verdict",
                "blocking_reasons": [],
                **({"not_applicable_reason": role_reason}
                   if (role_reason := run_role_non_applicability_reason(contract))
                   else {})}
    if not contract.get("execution_contract_valid", True):
        return {"passed": False, "stage": normalized_stage, "applicable": True,
                "reason": "execution_contract must be version 1 with scientific_params and runtime_params objects",
                "blocking_reasons": ["execution_contract_invalid"]}
    structured_contract = isinstance(contract.get("execution_contract"), dict)
    if structured_contract:
        if not isinstance(execution_params, dict) or "derived_params" in execution_params:
            return {"passed": False, "stage": normalized_stage, "applicable": True,
                    "reason": "v1 execution_contract accepts framework-derived params only as outputs",
                    "blocking_reasons": ["derived_params_forbidden"]}
        unexpected_sections = sorted(set(execution_params) - {"scientific_params", "runtime_params"})
        scientific_params = execution_params.get("scientific_params")
        runtime_params = execution_params.get("runtime_params", {})
        if unexpected_sections or not isinstance(scientific_params, dict) or not isinstance(runtime_params, dict):
            return {"passed": False, "stage": normalized_stage, "applicable": True,
                    "reason": "v1 execution_params must contain scientific_params and optional runtime_params only",
                    "blocking_reasons": ["execution_params_structure_invalid"]}
        actual_params = scientific_params
    else:
        actual_params = execution_params
    expected = contract.get("expected_params")
    if expected is None or expected == {}:
        return {"passed": False, "stage": normalized_stage, "applicable": True,
                "runner": runner,
                "reason": "simulation consuming a formal input package requires non-empty frozen preregistration expected_params",
                "blocking_reasons": ["expected_params_missing"]}
    if not isinstance(expected, dict):
        return {"passed": False, "stage": normalized_stage, "applicable": True,
                "runner": runner,
                "reason": "simulation preregistration expected_params must be an object",
                "blocking_reasons": ["expected_params_invalid"]}
    if not isinstance(actual_params, dict) or not actual_params:
        return {"passed": False, "stage": normalized_stage, "applicable": True,
                "blocking_reasons": ["execution_params_missing"]}
    expected_flat, actual_flat = _flatten_params(expected), _flatten_params(actual_params)
    missing = sorted(key for key in expected_flat if key not in actual_flat)
    unexpected = sorted(key for key in actual_flat if key not in expected_flat)
    mismatched = [{"parameter": key, "expected": expected_flat[key], "actual": actual_flat[key]}
                  for key in expected_flat if key in actual_flat and expected_flat[key] != actual_flat[key]]
    passed = not missing and not unexpected and not mismatched
    return {"passed": passed, "stage": normalized_stage, "applicable": True,
            "runner": runner,
            "expected_params": expected, "execution_params": execution_params, "scientific_params": actual_params,
            "missing_expected_parameters": missing,
            "unexpected_execution_parameters": unexpected,
            "mismatched_parameters": mismatched,
            "blocking_reasons": ([] if passed else
                (["execution_params_missing_fields"] if missing else []) +
                (["execution_params_unregistered_fields"] if unexpected else []) +
                (["execution_params_mismatch"] if mismatched else [])),
            "reason": "frozen preregistration parameters match this simulation" if passed else
                      "execution parameters differ from frozen preregistration"}

def audit_experiment_preflight(
    state: Any, *, phase: str = "experiment_start",
) -> dict[str, Any]:
    """Return a fail-closed, JSON-serializable preflight result.

    The framework already blocks a missing/unfrozen pre_registration before
    the loop starts.  Rechecking it here keeps the node-local gate explicit
    and also makes the result testable when the hook/tool is called directly.
    """
    checks: list[dict[str, Any]] = []

    prereg = _any_frozen_prereg(state)
    checks.append({
        "name": "pre_registration",
        "passed": prereg is not None,
        "blocking": True,
        "reason": (
            "frozen pre_registration present"
            if prereg is not None
            else "missing or unfrozen pre_registration"
        ),
    })

    try:
        try:
            from tools.run_contract import (
                load_run_contract, run_role_non_applicability_reason,
            )
        except ImportError:
            from nodes.experiment.tools.run_contract import (
                load_run_contract, run_role_non_applicability_reason,
            )
        contract = load_run_contract(state)
    except Exception as exc:
        contract = {}
        checks.append({
            "name": "run_contract",
            "passed": False,
            "blocking": True,
            "reason": f"cannot load run contract: {type(exc).__name__}",
        })
    else:
        selected_prereg_id = str(contract.get("prereg_artifact_id") or "")
        if contract.get("ambiguous_preregs"):
            _register_prereg_dispatch_blocker(state, kind="ambiguous_preregs", candidates=list(contract["ambiguous_preregs"]))
        elif contract.get("contract_source") == "declared_prereg_not_found":
            _register_prereg_dispatch_blocker(state, kind="declared_prereg_not_found", declared_id=str(contract.get("declared_prereg_id") or ""))
        try:
            selected_prereg = state.read_artifact(selected_prereg_id) if selected_prereg_id else None
        except Exception:
            selected_prereg = None
        selected_meta = (selected_prereg or {}).get("metadata") or {}
        selected_ok = bool(selected_prereg and selected_meta.get("frozen"))
        checks.append({
            "name": "declared_pre_registration",
            "passed": selected_ok,
            "blocking": False,
            "prereg_artifact_id": selected_prereg_id or None,
            "reason": ("declared frozen pre_registration is readable" if selected_ok else
                       "no uniquely selected frozen pre_registration; declare prereg_artifact_id when multiple preregs exist"),
        })
        role = contract.get("run_role")
        requested_stage = str(contract.get("stage") or "simulation").strip().lower()
        stage = _STAGE_ALIASES.get(requested_stage)
        stage_ok = stage is not None
        # #726 第一刀：去 stage。"要不要强制声明 prereg" 是 primary scientific 的
        # 身份义务,不由 caller 的 stage 决定（primary ⟹ scientific 已由 run_contract
        # 保证,operation 不得有 primary）。下方独立的 stage 合法性 check 保留不动
        # ——那是 C 类协议校验（值必须是 diagnostic/build/simulation）,不是科学门。
        scientific_run = bool(role == "primary")
        role_reason = run_role_non_applicability_reason(contract)
        checks[0]["blocking"] = scientific_run
        if not scientific_run and role_reason:
            checks[0]["not_applicable_reason"] = role_reason
        for check in checks:
            if check.get("name") == "declared_pre_registration":
                check["blocking"] = scientific_run
                if not scientific_run and role_reason:
                    check["not_applicable_reason"] = role_reason
        checks.append({
            "name": "stage", "passed": stage_ok, "blocking": True,
            "stage": stage or requested_stage,
            "reason": "valid experiment stage" if stage_ok else "stage must be diagnostic, build, or simulation",
        })
        contract_ok = role in {"primary", "secondary"}
        checks.append({
            "name": "run_contract",
            "passed": contract_ok,
            "blocking": True,
            "run_role": role,
            "run_role_source": contract.get("run_role_source"),
            "requires_hypothesis_verdict": bool(
                contract.get("requires_hypothesis_verdict")),
            "review_eligible": bool(contract.get("review_eligible", True)),
            "contract_source": contract.get("contract_source"),
            "reason": (
                "run completion contract is consistent"
                if contract_ok else
                "valid run roles are primary or secondary"
            ),
        })
        expected = contract.get("expected_params")
        expected_ok = isinstance(expected, dict) and bool(expected)
        expected_check = {
            "name": "frozen_expected_params",
            "passed": (not scientific_run) or expected_ok,
            "blocking": scientific_run,
            # 话术跟着判据走：原先 passed 看 scientific_run、话术看 eligible，
            # operational+primary 会失败却打出成功那句。
            "reason": (
                "non-primary run does not require frozen expected_params"
                if not scientific_run else
                "primary run has non-empty frozen expected_params"
                if expected_ok else
                "primary run requires non-empty frozen pre_registration metadata.expected_params"
            ),
        }
        if not scientific_run and role_reason:
            expected_check["not_applicable_reason"] = role_reason
        checks.append(expected_check)

    try:
        try:
            from tools.path_roles import validate_path_roles
        except ImportError:
            from nodes.experiment.tools.path_roles import validate_path_roles
        path_result = validate_path_roles(state)
        path_ok = bool(path_result.get("valid"))
        path_reason = "path roles are valid" if path_ok else "; ".join(
            str(x) for x in (path_result.get("errors") or [])
        )[:500]
    except Exception as exc:
        path_ok = False
        path_reason = f"cannot validate path roles: {type(exc).__name__}"
    checks.append({
        "name": "path_roles",
        "passed": path_ok,
        "blocking": True,
        "reason": path_reason,
    })

    blocking_failures = [c["name"] for c in checks if c.get("blocking") and not c.get("passed")]
    return {
        "passed": not blocking_failures,
        "phase": phase,
        "checks": checks,
        "blocking_reasons": blocking_failures,
        "reason": (
            "Experiment 启动前契约检查通过"
            if not blocking_failures else
            "Experiment 启动前契约检查失败：" + ", ".join(blocking_failures)
        ),
    }

"""Fail-fast-style checks for the experiment YAML contract."""
from __future__ import annotations

from pathlib import Path

import yaml

from core.bootstrap import bootstrap
from core.loop_hooks import get_loop_hook
from core.tool_registry import _REGISTRY


def test_experiment_yaml_tools_and_hooks_are_registered():
    bootstrap(force=True)
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )

    missing_tools = [name for name in config["tools"] if name not in _REGISTRY.tools]
    missing_hooks = [name for name in config["loop_hooks"] if get_loop_hook(name) is None]

    assert missing_tools == []
    assert missing_hooks == []


def test_experiment_declares_main_closure_outputs_without_legacy_qc():
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    # 成员是契约，顺序按实际冻结顺序 raw→clean→log（2026-09-09 #919：这里原本
    # 把倒序钉死成常量，反而让 YAML 与 rules 的矛盾长期存活 —— 断言应该验语义，
    # 不是验某个排列）。顺序本身另由 test_harness_triplet_contract.py 单独把关。
    assert set(config["required_output_artifact_types"]) == {
        "raw_results", "clean_results", "experiment_log",
    }
    # quality_checks 已从 Core 删除；Experiment 的专属证据完整性由 freeze gates
    # 与 contract_audit 的可回放审计实现，不能留下 YAML 死配置制造假安全感。
    assert "quality_checks" not in (config.get("completion_criteria") or {})


def test_experiment_declares_the_scheduler_path_input_contract():
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    description = config["expected_inputs"]["path_roles"]
    assert "run_root" in description
    assert "build_root" in description
    assert "approved_write_root" in description


def test_experiment_declares_the_full_scientific_dispatch_contract():
    """调用方必须从 callee_contracts 获得真实的 Experiment 词表。"""
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    expected = config["expected_inputs"]

    assert {
        "experiment_spec",
        "prereg_artifact_id",
        "prereg_version",
        "stage",
        "dataset_artifact_id",
        "input_package_artifact_id",
        "execution_params",
        "path_roles",
    }.issubset(expected)
    assert "run_role" not in expected
    assert "正整数" in expected["prereg_version"]
    assert "prereg_artifact_id" in expected["prereg_version"]
    assert "simulation" in expected["stage"]
    assert "diagnostic" in expected["stage"]
    assert "toolchain_build" in expected["stage"]
    assert "verify_dataset_consumption" in expected["dataset_artifact_id"]
    assert "完全一致" in expected["execution_params"]


def test_experiment_data_dispatch_includes_the_required_user_note():
    """Experiment delegates formal input preparation through the managed Data dispatch contract."""
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    rules = "\n".join(config["rules"])

    assert "dispatch_data_request(spec_id=..., user_note=...)" in rules
    assert "user_note=" in rules
    assert "reconcile_data_dispatch()" in rules
    assert "reconcile_data_dispatch" in config["tools"]


def test_reviewer_spec_uses_raw_results_as_the_evidence_interface():
    spec = (Path(__file__).resolve().parents[1] / "review_spec.md").read_text(encoding="utf-8")
    assert "raw_results" in spec
    assert "record_kind: operation" in spec
    assert "operation_verification_receipt" in spec
    assert "evidence_integrity" in spec


def test_reviewer_spec_keeps_final_scientific_adjudication_in_analysis():
    spec = (Path(__file__).resolve().parents[1] / "review_spec.md").read_text(encoding="utf-8")
    assert "execution_assessment_quality" in spec
    assert "才负责最终的 per-hypothesis 科学裁决" in spec
    assert "`validated` / `refuted` 状态更新" in spec
    assert "experiment 节点接管了原 analysis 节点的 per-hypothesis verdict" not in spec


def test_experiment_exposes_managed_submission_not_legacy_job_bookkeeping():
    """登记工具不会调度或唤醒 experiment，不能作为长任务执行路线。"""
    config = yaml.safe_load(
        Path(__file__).resolve().parents[1].joinpath("harness.yaml").read_text(encoding="utf-8")
    )
    assert {"submit_job", "job_status"}.issubset(config["tools"])
    assert "declare_job" not in config["tools"]
    assert "job_progress" not in config["tools"]


def test_experiment_exposes_the_per_run_network_access_request() -> None:
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert "fetch_resource" in config["tools"]
    assert "request_network_access" in config["tools"]


def test_gpu_skill_is_conditional_hook_not_global_context():
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    assert "gpu-hpc-porting" not in config.get("skills", [])
    assert "gpu_skill_injector" in config["loop_hooks"]


def test_gpu_skill_stays_resolvable_after_leaving_yaml_skills():
    """从 skills: 移除只改常驻注入，不该让 skill 本身消失。

    即使不常驻注入，明确 CUDA 的任务仍必须能解析到节点本地 skill；
    缺失时 injector 会留下 transcript 事件并提示官方文档。
    """
    from core.skill_registry import get_skill

    bootstrap(force=True)
    assert get_skill("gpu-hpc-porting") is not None


def test_recovery_runs_before_contract_audit():
    """兜底记录必须先落地，contract_audit 才能看到它并按自动记录 fail-closed。

    这个次序此前只写在注释里；顺序一旦被调换，审计会读到"没有 experiment_log"
    而不是"只有自动记录"，两种结论的下游处理完全不同。
    """
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    hooks = config["loop_hooks"]
    assert hooks.index("secondary_experiment_log_recovery") < \
        hooks.index("sediment_closure_advisor") < hooks.index("experiment_contract_audit")
    assert {"resolve_prereg_hypotheses", "declare_inconclusive_verdict", "assess_sediment_candidate",
            "declare_no_sediment", "preview_experiment_contract", "freeze_artifact"}.issubset(config["tools"])


def test_turn_start_compat_entry_only_lists_turn_start_hooks():
    """兼容入口只跑 on_turn_start：放只有 on_end 的 hook 进去会被静默跳过。"""
    from nodes.experiment import hooks as experiment_hooks

    bootstrap(force=True)
    for name in experiment_hooks._TURN_START_COMPAT_HOOKS:
        hook = get_loop_hook(name)
        assert hook is not None, name
        assert hook.on_turn_start is not None, name


def test_invalid_declared_route_records_and_lets_the_build_run():
    """判决拆除 O12（sb:3153 降格，2026-08-31）。

    contract 校验照跑、errors 如实进 transcript（build_contract_invalid，
    含 build_proceeded=True），但构建不再被拦（gate 返回 None）。
    """
    from nodes.experiment.tools.safe_bash import _build_contract_gate

    class State:
        def __init__(self):
            self.events = []

        def list_artifacts(self, artifact_type=None):
            if artifact_type == "declared_route":
                return [{"id": "route-1", "type": "declared_route"}]
            return []

        def read_artifact(self, artifact_id):
            assert artifact_id == "route-1"
            return {"content": "{}"}

        def append_transcript(self, event, **fields):
            self.events.append((event, fields))

    state = State()
    result = _build_contract_gate(state)

    assert result is None
    recorded = [fields for event, fields in state.events if event == "build_contract_invalid"]
    assert recorded, state.events
    assert recorded[0].get("build_proceeded") is True
    assert recorded[0].get("errors")


def test_experiment_exposes_only_the_preflighted_log_freeze_route():
    """"只有过门才能冻"从工具选型升级成了结构不变量。

    旧断言（不许挂 freeze_artifact）防的是"绕过特化工具走无门的通用冻结"。
    六合一后门跟着**类型**走（FREEZE_GATES），挂哪个工具都绕不开 —— 逃逸口
    在结构上不存在。断言随之翻转：唯一的 freeze_artifact 在，特化名全部消失，
    三类证据件的门都已注册。
    """
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8"))
    assert "freeze_artifact" in config["tools"]
    for legacy in ("freeze_clean_results", "freeze_experiment_log",
                   "freeze_raw_results", "freeze_and_register"):
        assert legacy not in config["tools"]

    import nodes.experiment.tools.contract_audit  # noqa: F401  门在 import 时注册
    from shared.tools.library.artifacts_extra import FREEZE_GATES
    for t in ("experiment_log", "clean_results", "raw_results"):
        assert t in FREEZE_GATES, f"{t} 的冻结门没注册"


def test_experiment_remains_the_unique_producing_owner_of_result_artifacts():
    """Owner protection is derived from required outputs, not duplicated in Core."""
    from shared.tools.builtin import _producing_output_owners

    bootstrap(force=True)
    owners = _producing_output_owners()
    assert owners.get("raw_results") == {"experiment"}
    assert owners.get("clean_results") == {"experiment"}


def test_safe_execute_python_exposes_input_package_artifact_id():
    """The public tool schema must expose the parameter consumed by its preflight."""
    bootstrap(force=True)
    properties = _REGISTRY.tools["safe_execute_python"].parameters_schema["properties"]
    assert properties["input_package_artifact_id"]["type"] == "string"
    assert "requirements" not in properties


def test_safe_execution_tool_schemas_expose_route_step_id():
    """A route gate cannot require a model argument that the tool schema hides."""
    bootstrap(force=True)
    for tool_name in ("safe_run_bash", "safe_execute_python"):
        properties = _REGISTRY.tools[tool_name].parameters_schema["properties"]
        assert properties["route_step_id"]["type"] == "string"


def test_operation_completion_external_job_ref_uses_immutable_container_id():
    """The public local-job reference must expose Docker identity, not host PID data."""
    bootstrap(force=True)
    item = _REGISTRY.tools["record_operation_completion"].parameters_schema[
        "properties"
    ]["external_job_refs"]["items"]
    properties = item["properties"]
    assert properties["container_runtime_id"]["pattern"] == (
        "^[0-9a-f]{64}" + chr(36)
    )
    assert "process_group_id" not in properties
    assert "process_start_ticks" not in properties


# ---- 验收项：面向模型的封闭词表必须把合法取值送到调用方手上 ----------------
#
# 两轮真实 E2E（2026-09-01/02，e2e_realistic_scientific）在同一次运行里给出了
# 干净的对照，除了「报错说不说合法形式」没有第二个变量：
#
#   说清合法形式的门                          代价
#     content_digest → "必须是 sha256:<64hex>"   1 轮
#     bytes mismatch → "declared X, actual Y"    从未出过问题
#     clean_results.reason → 说了要非空           1 轮
#   只说违规的门
#     storage_binding.roles → "含不支持的语义角色"  22 轮，最后靠猜
#     raw_results sha256 → 算出来了却只说 mismatch  20+ 轮，且为了拿这一个值
#                                                往科学路线 DAG 里插了步骤
#
# 合法取值送达有两条通道，schema 那条更好（调用前就看得见，不必先撞墙）：
#   ① 工具 parameters_schema 里的 "enum"
#   ② 拒绝消息里插值该常量，或逐字列全部成员
# 至少满足一条。新增面向模型的封闭词表时把它登记到下表。
#
# 见 docs/bug-families.md BF-12。

_MODEL_FACING_VOCABULARIES = [
    ("execution_envelope.py", "_ROLE_NAMES"),
    ("execution_envelope.py", "_ASSURANCE_CLASSES"),
    ("execution_envelope.py", "CANONICAL_ROLES"),      # 作为 roles 的值域
    ("execution_route.py", "_RECOVERY_FAILURE_CLASSES"),
    ("resource_manager.py", "_ANALYZED_FINALIZE_OUTCOMES"),
    ("resource_manager.py", "_OPERATION_FINALIZE_OUTCOMES"),
]


def _vocabulary_reaches_the_caller(module_name: str, constant: str) -> tuple[bool, str]:
    import ast

    path = Path(__file__).resolve().parents[1] / "tools" / module_name
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # `ast.get_source_segment` 每调用一次就把整份源码 splitlines 一遍；在
    # resource_manager.py / safe_bash.py（各 9k+ 行）上按节点走一遍是平方级的，
    # 这一条测试因此实测 17s，撞上 CI 的「没有测试可以慢过 15 秒」闸。
    # 行表切一次就够，判据逐字不变（同 padded=False 的取段语义）。
    _lines = source.splitlines(keepends=True)

    def _segment(node) -> str:
        lineno = getattr(node, "lineno", None)
        end_lineno = getattr(node, "end_lineno", None)
        if lineno is None or end_lineno is None:
            return ""
        first, last = lineno - 1, end_lineno - 1
        if first == last:
            return _lines[first][node.col_offset:node.end_col_offset]
        chunk = [_lines[first][node.col_offset:]]
        chunk.extend(_lines[first + 1:last])
        chunk.append(_lines[last][:node.end_col_offset])
        return "".join(chunk)

    def _mentions(node) -> bool:
        return constant in _segment(node)

    for node in ast.walk(tree):
        # ① schema enum：调用前就看得见
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "enum"
                        and _mentions(value)):
                    return True, "schema enum"
        # ② 消息里插值该常量（含 *_HINT 之类派生名）
        if isinstance(node, ast.FormattedValue) and _mentions(node.value):
            return True, "插值进消息"

    # ③ 某条**消息**逐字列出了全部成员。只认消息位置 —— 任何提到全部成员的
    #    长文档串都能蒙混过关的话，这条规则就是空转（实测：_SEDIMENT_TYPES 与
    #    _EXECUTION_MODES 这两个非拒绝门会假阳性通过）。
    members = _vocabulary_members(module_name, constant)

    def _lists_all(node) -> bool:
        return (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and all(member in node.value for member in members))

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant)
                        and key.value in {"error", "hint", "detail", "description"}
                        and _lists_all(value)):
                    return True, "消息逐字列全"
        if isinstance(node, ast.Call):
            func = node.func
            appends_error = (
                isinstance(func, ast.Attribute) and func.attr == "append"
                and "error" in _segment(func.value)
            )
            raises_message = (
                isinstance(func, ast.Name) and func.id.endswith(("Error", "Unavailable"))
            )
            if (appends_error or raises_message) and any(
                    _lists_all(arg) for arg in node.args):
                return True, "消息逐字列全"
    return False, "合法取值没有任何通道送到调用方"


def _vocabulary_members(module_name: str, constant: str) -> set[str]:
    import importlib

    module = importlib.import_module(
        f"nodes.experiment.tools.{module_name.removesuffix('.py')}"
    )
    return {str(item) for item in getattr(module, constant)}


# 阴性对照：这两个不是面向模型的拒绝门（一个是 filter，一个由框架内部派生），
# 因此**预期检测不到通道**。它们在这里的唯一作用是证明上面的检测器不是空转 ——
# 早期版本把"任何提到全部成员的长字符串"都算通过，这两个会假阳性。
_NOT_MODEL_FACING_CONTROLS = [
    ("contract_audit.py", "_SEDIMENT_TYPES"),
    ("run_contract.py", "_EXECUTION_MODES"),
]


def test_the_vocabulary_detector_is_not_vacuous():
    for module_name, constant in _NOT_MODEL_FACING_CONTROLS:
        ok, how = _vocabulary_reaches_the_caller(module_name, constant)
        assert not ok, (
            f"阴性对照 {module_name}:{constant} 现在被判为「已送达」（{how}）。"
            "要么它真的变成了面向模型的门（那就移进 _MODEL_FACING_VOCABULARIES），"
            "要么检测器又变松了 —— 两种情况都必须先查清楚，不要直接换个对照了事。"
        )


def test_model_facing_vocabularies_tell_the_caller_the_legal_values():
    unreachable = []
    for module_name, constant in _MODEL_FACING_VOCABULARIES:
        assert _vocabulary_members(module_name, constant), f"{constant} 是空词表"
        ok, how = _vocabulary_reaches_the_caller(module_name, constant)
        if not ok:
            unreachable.append(f"{module_name}:{constant} —— {how}")
    assert not unreachable, (
        "下列面向模型的封闭词表只会拒绝、不会告诉调用方合法取值。"
        "真实 E2E 里这种门的代价是 20+ 轮试错（见本文件上方对照与 "
        "docs/bug-families.md BF-12）：\n  " + "\n  ".join(unreachable)
    )


def test_no_hook_mechanism_depends_on_rewriting_the_llm_response():
    """节点里不得再有靠改写 LLM response 生效的机制。

    框架 v3.1 把 ``on_llm_response`` 从「约定只观察」升级为「机制上只读」：hook 收到
    的是 response 的深拷贝，改它不影响真实流程；只有 framework_exemptions.yaml 里
    登记且未过期的 node_type 仍收原对象。experiment 的那条豁免 deadline 是
    2026-08-01。

    代价已经付过一次：外部作业收尾闸与空停机守卫都靠改写 response 插入工具调用，
    豁免过期后两者只写事件、不产生任何效果，而单元测试直接把可变对象喂给内部函数，
    因此一直全绿。2026-09-08 活体里模型无工具调用停机、闸记录了
    ``insert_closure_reminder``、同一秒 run 就结束了（ROADMAP N-005）。

    机制的存在不等于机制生效。这条断言是那次失效的机械防线：收尾闸已迁到
    ``on_before_finish``（返回消息否决收尾，不碰 response），此后任何人再写
    ``response.tool_calls = ...`` 都会在这里当场变红，而不是等下一次活体才发现。
    """
    import re
    from pathlib import Path

    hooks_path = Path(__file__).resolve().parents[1] / "hooks.py"
    offenders = [
        f"{hooks_path.name}:{i}: {line.strip()}"
        for i, line in enumerate(hooks_path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bresponse\s*\.\s*(tool_calls|content)\s*=(?!=)", line)
    ]
    assert not offenders, (
        "on_llm_response 相位收到的是深拷贝，这些赋值不会生效（豁免已于 2026-08-01 "
        "到期）。要否决收尾请用 on_before_finish 返回消息：\n  "
        + "\n  ".join(offenders)
    )


def test_node_skills_are_in_the_l1_index_but_not_resident():
    """049-0：7 个节点 skill 里 6 个进 harness.yaml 的 skills:（只渲染 L1 索引行），
    gpu-hpc-porting 仍走条件注入。索引模式下不得把正文带进 system_prompt。"""
    from core.skill_registry import render_skills

    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    listed = set(config.get("skills", []))
    assert {"feasibility-ladder", "formal-input-recovery", "hpc-build",
            "primary-scientific-closure", "scheduler-longrun", "scientific-results"} <= listed
    assert "gpu-hpc-porting" not in listed
    bootstrap(force=True)
    rendered = render_skills(config["skills"], node_type="experiment", mode="index")
    for name in ("hpc-build", "scheduler-longrun", "scientific-results"):
        assert f"### {name}" in rendered, name
        assert f"load_skill('{name}')" in rendered, name
    assert "### Skill:" not in rendered            # index lines only, no bodies
    assert "适用：" in rendered                    # applies_when is shown as routing text

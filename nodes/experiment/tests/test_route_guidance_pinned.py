"""031 B-2/B-3：路线文案整句钉死，并和实际判定成对断言。

关键词和黑名单只挡得住旧句原样回来；换个说法把同一句假话加回来，测试照样
可能全绿。这里对注册描述、harness 规则和未分类拒绝文案做整句相等断言；改文案
就必须同时改这里，并重跑下面成对的行为断言。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from core.tool_registry import get_tool
from nodes.experiment.tools import safe_bash
from nodes.experiment.tools.execution_route import execution_route_block


_NODE = Path(__file__).resolve().parents[1]
_HARNESS_RULE_COUNT = 16

_BASH_DESCRIPTION = (
    "执行有界、同步的 shell 诊断或只读探针。timeout 是该诊断调用的显式硬边界，"
    "到期会终止整棵进程树；真正的构建、simulation 与启动器必须从启动时使用 "
    "submit_job(scheduler=local/SLURM/PBS) —— 它们需要活过本次调用的作业身份，"
    "不要把 expected_duration 当作 safe_run_bash 的续时依据。有界的本地动作留在"
    "本工具即可。是否需要路线以本次调用返回的机械判定和 next_action 为准，不要靠"
    "命令名称清单猜测。当前冻结路线已有与本次命令入口对应的就绪步骤时，必须按解析"
    "结果绑定该步骤；解析结果要求显式绑定时传 route_step_id。有界且被机械判定为 "
    "low_risk_effectful 的 run-local 写入通常无需新建路线。"
)

_PYTHON_DESCRIPTION = (
    "在受管隔离沙箱中执行有界、同步的轻量单进程 Python 诊断或机械计算；限制 PID、"
    "内存与时间，日志有界记录。不得安装依赖或派生外部进程。本工具不把任何文件写入"
    "一概升级为新路线步骤；是否需要新路线以本次调用返回的 reason 与提示为准。当前"
    "冻结路线已有与 safe_execute_python 对应的就绪步骤时，真实计算必须传该步骤的 "
    "route_step_id；只读诊断改用 safe_run_bash，不会消耗该步骤。不要把诊断绑定到"
    "该步骤上，否则会把步骤记成已完成。直接写出的 tarfile/zipfile extractall 或 "
    "extract，以及 shutil.unpack_archive，会被识别为 environment_change，必须先"
    "声明对应 Python 路线步骤；动态调用、别名和其他未识别的 Python 效果不因此自动"
    "获得路线保护。真正需要受管生命周期的计算使用 submit_job；需要 shell 效果分析"
    "的动作使用 safe_run_bash，以便获得可审计的机械判定。不得根据这段描述推定未识别"
    "的 Python 效果已经受路线保护。"
)

_ROUTE_RULE = (
    "**按机械判定和当前路线执行**：工具返回需要路线时，先用 `declare_execution_route` "
    "冻结一条 v2 路线，使一个 step 的 action 与本次动作对应，再按返回要求传 "
    "route_step_id；不要靠命令名称清单猜测。当前冻结路线已有与本次调用对应的就绪步骤"
    "时，按解析结果执行绑定；safe_run_bash 以本次命令入口匹配步骤。safe_execute_python "
    "的对应就绪步骤只承接真实计算：真实计算必须传该步骤的 route_step_id；只读诊断改用 "
    "`safe_run_bash`，不会消耗该步骤。不要把诊断绑定到步骤上，否则会把步骤记成已完成。"
    "有界且被机械判定为 low_risk_effectful 的 run-local 写入通常无需新建路线。直接写出"
    "的 `tarfile`/`zipfile` `extractall` 或 `extract`，以及 `shutil.unpack_archive` "
    "会被识别为 `environment_change`，必须先声明对应 Python 路线步骤；动态调用、别名"
    "和其他 Python 未识别效果不因此自动获得路线保护，应改用 `safe_run_bash` 取得可"
    "审计的机械效果判定。route_step_id 本身不授权，构建先 hpc-build/preflight。另："
    "`declared_route`"
    "（构建证据，非 canonical 路线）四字段必填，缺任一被拒 —— `route_type`、"
    "`activities`、`env_domains`（每域至少一个客观探针）、`expected_artifacts`。"
)

_UNCLASSIFIED_BASE = (
    "所有真实执行动作必须先调用 classify_experiment_scope，确定本 run "
    "是 operation 还是 scientific；本次动作未物化目录、未执行。"
    "该分类会决定 prereg、科学门和最终闭环，不能在执行后补填。"
)


def _unclassified(tool: str) -> dict[str, object]:
    return {
        "decision": "route_not_required",
        "tool": tool,
        "policy": "low_risk_effectful",
        "read_only": False,
        "dry_run": False,
        "observed_effects": ["workspace_write"],
        "effective_effects": ["workspace_write"],
        "scope_required": True,
        "scope_status": "absent",
        "scope_mode": None,
    }


def test_registered_route_descriptions_are_pinned_verbatim():
    assert get_tool("safe_run_bash").description == _BASH_DESCRIPTION
    assert get_tool("safe_execute_python").description == _PYTHON_DESCRIPTION


def test_harness_route_rule_is_pinned_verbatim():
    rules = yaml.safe_load((_NODE / "harness.yaml").read_text(encoding="utf-8"))["rules"]
    assert len(rules) == _HARNESS_RULE_COUNT
    assert rules.count(_ROUTE_RULE) == 1


def test_unclassified_scope_messages_are_pinned_verbatim():
    python_block = execution_route_block(
        _unclassified("safe_execute_python"),
        phase="pre_materialization",
    )
    bash_block = execution_route_block(
        _unclassified("safe_run_bash"),
        phase="pre_materialization",
    )
    assert python_block["error"] == (
        _UNCLASSIFIED_BASE + "如果只是只读诊断，请改用 safe_run_bash。"
    )
    assert bash_block["error"] == _UNCLASSIFIED_BASE


def test_pinned_claims_match_actual_decisions(tmp_path, monkeypatch):
    """整句所含断言分别由真实路线判定支撑。"""
    from nodes.experiment.tests.test_route_shadow_wiring import _state

    async def fake_exec(*_args, **_kwargs):
        return {
            "status": "success",
            "returncode": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)

    classified = _state(tmp_path / "classified")
    runtime = classified.root / "outputs" / "experiment" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    write = asyncio.run(
        safe_bash._safe_run_bash(
            classified,
            "echo x > out.txt",
            cwd=str(runtime),
        )
    )
    assert write["status"] == "success", write
    python_write = asyncio.run(
        safe_bash._safe_execute_python(
            classified,
            "open('out.txt','w').write('x')",
            cwd=str(runtime),
        )
    )
    assert python_write["status"] == "success", python_write

    unclassified = _state(tmp_path / "unclassified", bind_scope=False)
    python_read = asyncio.run(
        safe_bash._safe_execute_python(unclassified, "print('diag')")
    )
    assert python_read["reason"] == "experiment_scope_classification_required"
    bash_read = asyncio.run(safe_bash._safe_run_bash(unclassified, "ls"))
    assert bash_read["status"] == "success", bash_read

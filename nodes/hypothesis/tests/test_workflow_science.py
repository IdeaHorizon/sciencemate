"""workflow_science 通用一致性规则测试。"""
from __future__ import annotations

from nodes.hypothesis.tools.workflow_science import (
    parse_model_scope,
    parse_task_rows_for_science,
    validate_workflow_science,
)


def _plan_table(*rows: str, mermaid: str | None = None) -> str:
    header = (
        "| Step ID | 任务类型 | 前置步骤 | 软件/泛函 | 超胞 | 关键参数 | 产出 | 对应 falsifier |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    mermaid_block = mermaid or "flowchart TD\n  S1[geometry_relax 原胞] --> S2[phonon 2x2x2]\n"
    return (
        "Plan sections: experimental_design · computational_workflow · "
        "baselines · resource_estimates · risk_analysis\n\n"
        "## computational_workflow\n\n"
        f"```mermaid\n{mermaid_block}```\n\n"
        + header
        + "\n".join(rows)
        + "\n"
    )


def test_parse_model_scope_primitive() -> None:
    assert parse_model_scope("原胞") == (1, "primitive")
    assert parse_model_scope("1x1x1") == (1, "primitive")
    assert parse_model_scope("2x2x2") == (8, "2x2x2")


def test_flags_undocumented_nondefault_model() -> None:
    content = _plan_table(
        "| S1 | geometry_relax | - | VASP | 1x1x3 | ISIF=3 | CONTCAR | - |",
        "| S2 | phonon | S1 | VASP | 2x2x2 | DFPT 依据：有限尺寸虚频检查 | 谱 | H1 |",
    )
    report = validate_workflow_science(content)
    assert not report.ok
    assert any(i.rule_id == "nondefault_model_undocumented" for i in report.errors)


def test_accepts_documented_model_choices() -> None:
    content = _plan_table(
        "| S1 | geometry_relax | - | VASP | 原胞 | 依据：bulk 平衡结构；ISIF=3 | CONTCAR | H1 |",
        "| S2 | phonon | S1 | VASP | 2x2x2 | 依据：消除有限尺寸虚频；DFPT | 谱 | H1 |",
        "| S3 | aimd | S1 | VASP | 1x1x2 | 依据：有限温度最小镜像；300K 8ps | 轨迹 | H2 |",
    )
    report = validate_workflow_science(content)
    assert report.ok


def test_flags_model_change_without_bridge_or_note() -> None:
    content = _plan_table(
        "| S1 | geometry_relax | - | VASP | 1x1x3 | 依据：稀释掺杂前体超胞构建 | CONTCAR | - |",
        "| S4 | band_dos | S1 | VASP | 原胞 | HSE06 | 能带 | H1 |",
    )
    report = validate_workflow_science(content)
    assert not report.ok
    assert any(i.rule_id == "model_scope_change_unexplained" for i in report.errors)


def test_allows_model_change_with_postprocess_bridge() -> None:
    content = _plan_table(
        "| S1 | geometry_relax | - | VASP | 1x1x3 | 依据：稀释掺杂前体超胞构建 | CONTCAR | - |",
        "| S9 | postprocess | S1 | - | - | 提取原胞供下游静态计算 | 原胞结构 | - |",
        "| S4 | band_dos | S9 | VASP | 原胞 | HSE06 在原胞上计算 | 能带 | H1 |",
        mermaid=("flowchart TD\n  S1[geometry_relax 1x1x3] --> S9[postprocess]\n"
                 "  S9 --> S4[band_dos 原胞]\n"),
    )
    report = validate_workflow_science(content)
    assert report.ok


def test_flags_missing_step_traceability() -> None:
    content = _plan_table(
        "| S1 | geometry_relax | - | VASP | 原胞 | - | - | - |",
    )
    report = validate_workflow_science(content)
    assert not report.ok
    assert any(i.rule_id == "step_not_traceable" for i in report.errors)


# ══ #157：资源估算表被误当 workflow 表 → 无法通过的重复 traceability 错误 ══
# 现场：research plan 同时含 4 张 workflow 表 + 1 张资源估算表（也用 Step ID +
# 任务类型 索引）。资源表没有 trace 列 → 每行都空 → 每个 step 报 false
# step_not_traceable。agent 反复重写**本来就合格**的 workflow 表，19→18→18 不降，
# 31 turns 烧 1.77M token 才被人工 /stop。

_WORKFLOW_HEADER = (
    "| Step ID | 任务类型 | 前置步骤 | 软件/泛函 | 超胞 | 关键参数 | 产出 | 对应 falsifier |\n"
    "|---|---|---|---|---|---|---|---|\n"
)
_RESOURCE_TABLE = (
    "| Step ID | 任务类型 | 预计耗时 | 内存需求 | 磁盘需求 | 说明 |\n"
    "|---|---|---|---|---|---|\n"
    "| S1 | postprocess | 1 min | 1 GiB | 1 GiB | resource estimate |\n"
)


def test_resource_table_ignored_no_false_traceability_error():
    """A：workflow 表 + 同 Step ID 的资源估算表 → 只审 workflow 那行，无 false error。"""
    content = (
        _WORKFLOW_HEADER
        + "| S1 | postprocess | — | Python | 164 nodes | Aggregate within condition "
          "| paired table | neutral estimand |\n\n"
        + _RESOURCE_TABLE
    )
    stats = {}
    rows = parse_task_rows_for_science(content, stats=stats)
    assert [r.step_id for r in rows] == ["S1"]          # 资源表那行被忽略
    assert stats["workflow_tables_detected"] == 1
    assert stats["non_workflow_tables_ignored"] == 1
    assert stats["task_rows_audited"] == 1
    report = validate_workflow_science(content)
    assert not [i for i in report.errors if i.rule_id == "step_not_traceable"]


def test_real_traceability_failure_still_caught():
    """B：workflow 表头齐全但 trace 单元格全空 → 仍必须报 step_not_traceable。
    （不能靠"忽略空内容"绕过科学门槛。）"""
    content = _WORKFLOW_HEADER + "| S1 | postprocess | — | Python | 164 nodes |  |  |  |\n"
    report = validate_workflow_science(content)
    assert [i.rule_id for i in report.errors if i.step_id == "S1"] == ["step_not_traceable"]


def test_multiple_workflow_tables_merge():
    """C：多张完整 workflow 表、不同 Step ID → 全部合并审计。"""
    content = (
        _WORKFLOW_HEADER
        + "| S1 | postprocess | — | Python | 164 nodes | agg | table | est |\n\n"
        + _WORKFLOW_HEADER
        + "| S2 | postprocess | S1 | Python | 164 nodes | fit | model | ci |\n"
    )
    stats = {}
    rows = parse_task_rows_for_science(content, stats=stats)
    assert [r.step_id for r in rows] == ["S1", "S2"]
    assert stats["workflow_tables_detected"] == 2
    assert stats["duplicate_step_ids"] == []


def test_duplicate_identical_step_id_deduped_silently():
    """C：同 Step ID 且内容一致 → 去重只审一次，不报错。"""
    row = "| S1 | postprocess | — | Python | 164 nodes | agg | table | est |\n"
    content = _WORKFLOW_HEADER + row + "\n" + _WORKFLOW_HEADER + row
    stats = {}
    rows = parse_task_rows_for_science(content, stats=stats)
    assert [r.step_id for r in rows] == ["S1"]
    assert stats["duplicate_step_ids"] == []
    report = validate_workflow_science(content)
    assert not [i for i in report.errors if i.rule_id == "duplicate_step_conflict"]


def test_duplicate_conflicting_step_id_reports_conflict():
    """C：同 Step ID 但内容冲突 → duplicate_step_conflict，而非误导性 traceability error。"""
    content = (
        _WORKFLOW_HEADER
        + "| S1 | postprocess | — | Python | 164 nodes | agg | table | est |\n\n"
        + _WORKFLOW_HEADER
        + "| S1 | simulation | — | LAMMPS | 8x8x8 | 完全不同的参数 | other | other |\n"
    )
    stats = {}
    rows = parse_task_rows_for_science(content, stats=stats)
    assert [r.step_id for r in rows] == ["S1"]          # 只保留第一条，不追加第二套
    assert stats["duplicate_step_ids"] == ["S1"]
    report = validate_workflow_science(content)
    assert "duplicate_step_conflict" in [i.rule_id for i in report.errors]


def test_resource_table_alone_fails_no_workflow_rows():
    """#160：只有资源表（无 workflow 表）→ 0 rows，必须 error，不得假通过。"""
    stats = {}
    rows = parse_task_rows_for_science(_RESOURCE_TABLE, stats=stats)
    assert rows == []
    assert stats["workflow_tables_detected"] == 0
    assert stats["non_workflow_tables_ignored"] == 1
    report = validate_workflow_science(_RESOURCE_TABLE)
    assert not report.ok
    assert any(i.rule_id == "no_workflow_task_rows" for i in report.errors)

"""workflow_graph — fork / join / gate DAG 解析与校验测试。"""
from __future__ import annotations

from nodes.hypothesis.tools.workflow_graph import (
    normalize_mermaid_syntax,
    parse_mermaid_flowchart,
    parse_task_tables,
    validate_mermaid_render_safe,
    validate_mermaid_syntax,
    validate_workflow_dag,
)


def _plan(mermaid: str, table: str, *, gates: str = "") -> str:
    return (
        "Plan sections: experimental_workflow · computational_workflow · "
        "baselines · resource_estimates · risk_analysis\n\n"
        "## Computational Workflow\n\n"
        f"```mermaid\n{mermaid}\n```\n\n"
        f"{table}\n"
        f"{gates}\n"
    )


def test_parse_fork_and_join() -> None:
    mermaid = (
        "flowchart TD\n"
        "  S1[relax] --> S2[phonon]\n"
        "  S1 --> S3[aimd]\n"
        "  S2 --> S4[merge]\n"
        "  S3 --> S4\n"
    )
    graph = parse_mermaid_flowchart(mermaid)
    assert graph.outgoing["S1"] == ["S2", "S3"]
    assert set(graph.incoming["S4"]) == {"S2", "S3"}


def test_parse_multi_predecessor_column() -> None:
    table = (
        "| Step ID | 任务类型 | 前置步骤 | 产出 |\n"
        "|---------|---------|---------|------|\n"
        "| S4 | postprocess | S2, S3 | out |\n"
        "| S5 | neb | S2 ∧ S4 | out2 |\n"
    )
    steps = parse_task_tables(table)
    assert steps["S4"].predecessors == ["S2", "S3"]
    assert steps["S5"].predecessors == ["S2", "S4"]


def test_validate_linear_workflow_passes() -> None:
    content = _plan(
        "flowchart TD\n  S1[relax] --> S2[phonon] --> S3[aimd]",
        (
            "| Step ID | 任务类型 | 前置步骤 | 产出 |\n"
            "|---------|---------|---------|------|\n"
            "| S1 | geometry_relax | - | a |\n"
            "| S2 | phonon | S1 | b |\n"
            "| S3 | aimd | S2 | c |\n"
        ),
    )
    ok, issues, _ = validate_workflow_dag(content)
    assert ok is True
    assert issues == []


def test_validate_fork_join_consistent() -> None:
    content = _plan(
        "flowchart TD\n"
        "  S1[relax] --> S2[phonon]\n"
        "  S1 --> S3[aimd]\n"
        "  S2 --> S4[merge]\n"
        "  S3 --> S4\n",
        (
            "| Step ID | 任务类型 | 前置步骤 | 产出 |\n"
            "|---------|---------|---------|------|\n"
            "| S1 | geometry_relax | - | a |\n"
            "| S2 | phonon | S1 | b |\n"
            "| S3 | aimd | S1 | c |\n"
            "| S4 | postprocess | S2, S3 | d |\n"
        ),
        gates="### 步骤依赖与门控\n- S1 完成后 S2/S3 可并行\n- S4 需 S2 与 S3 汇合\n",
    )
    ok, issues, meta = validate_workflow_dag(content)
    assert ok is True
    assert len(meta["fork_nodes"]) == 1
    assert len(meta["join_nodes"]) == 1


def test_validate_fails_when_join_missing_predecessor() -> None:
    content = _plan(
        "flowchart TD\n"
        "  S2 --> S4\n"
        "  S3 --> S4\n",
        (
            "| Step ID | 任务类型 | 前置步骤 | 产出 |\n"
            "|---------|---------|---------|------|\n"
            "| S4 | postprocess | S2 | d |\n"
        ),
    )
    ok, issues, _ = validate_workflow_dag(content)
    assert ok is False
    assert any("汇合节点 S4" in i for i in issues)


def test_validate_fails_when_fork_not_in_table() -> None:
    content = _plan(
        "flowchart TD\n"
        "  S1 --> S2\n"
        "  S1 --> S3\n",
        (
            "| Step ID | 任务类型 | 前置步骤 | 产出 |\n"
            "|---------|---------|---------|------|\n"
            "| S2 | phonon | S1 | b |\n"
            "| S3 | aimd | - | c |\n"
        ),
        gates="### 步骤依赖与门控\n- 并行\n",
    )
    ok, issues, _ = validate_workflow_dag(content)
    assert ok is False
    assert any("S3" in i and "S1" in i for i in issues)


def test_validate_fails_on_cyclic_gate_rollback() -> None:
    content = _plan(
        "flowchart TD\n"
        "  S8 --> S9\n"
        "  S9 --> S10\n"
        "  S10 --> S8b\n"
        "  S8b --> S9\n",
        (
            "| Step ID | 任务类型 | 前置步骤 | 产出 |\n"
            "|---------|---------|---------|------|\n"
            "| S8 | strain | S1 | a |\n"
            "| S9 | relax | S8, S8b | b |\n"
            "| S10 | scf | S9 | c |\n"
            "| S8b | strain | S10 | d |\n"
        ),
        gates="### 步骤依赖与门控\n- 门控回退\n",
    )
    ok, issues, meta = validate_workflow_dag(content)
    assert ok is False
    assert meta.get("cycles") or meta.get("mermaid_cycles")
    assert any("循环" in i for i in issues)


def test_validate_acyclic_gate_retry_branch_passes() -> None:
    content = _plan(
        "flowchart TD\n"
        "  S1 --> S8 --> S9 --> S10 --> S11\n"
        "  S8 --> S8b --> S9b --> S10b\n",
        (
            "| Step ID | 任务类型 | 前置步骤 | 产出 |\n"
            "|---------|---------|---------|------|\n"
            "| S1 | structure_import | - | z |\n"
            "| S8 | strain | S1 | a |\n"
            "| S9 | relax | S8 | b |\n"
            "| S10 | scf | S9 | c |\n"
            "| S8b | strain | S8 | d |\n"
            "| S9b | relax | S8b | e |\n"
            "| S10b | scf | S9b | f |\n"
            "| S11 | neb | S10 | g |\n"
        ),
        gates=(
            "### 步骤依赖与门控\n"
            "- S10 未达标则走 S8b→S9b→S10b 无环支链（S8b 前置 S8，禁止 S8b 前置 S10）\n"
            "- 并行: 主链 S8→S9→S10 与重试支链 S8b 二选一\n"
        ),
    )
    ok, issues, _ = validate_workflow_dag(content)
    assert ok is True, issues


def test_validate_gate_requires_gate_section() -> None:
    content = _plan(
        "flowchart TD\n"
        '  S1 --> S2{"虚频?"}\n'
        "  S2 -->|pass| S3\n",
        (
            "| Step ID | 任务类型 | 前置步骤 | 产出 |\n"
            "|---------|---------|---------|------|\n"
            "| S1 | geometry_relax | - | a |\n"
            "| S3 | phonon | S1 | b |\n"
        ),
    )
    ok, issues, meta = validate_workflow_dag(content)
    assert ok is False
    assert "S2" in meta["gate_nodes"]
    assert any("门控" in i for i in issues)


def test_mermaid_syntax_rejects_unquoted_colon_label() -> None:
    raw = "flowchart TD\n  S1[geometry_relax: PBE] --> S2[phonon]\n"
    ok, issues = validate_mermaid_syntax(raw)
    assert ok is False
    assert any("geometry_relax: PBE" in i for i in issues)


def test_normalize_mermaid_quotes_risky_labels() -> None:
    raw = "flowchart TD\n  S1[geometry_relax: PBE-D2] --> S2{虚频?}\n"
    fixed = normalize_mermaid_syntax(raw)
    ok, issues = validate_mermaid_syntax(fixed)
    assert ok is True
    assert issues == []
    assert 'S1["geometry_relax: PBE-D2"]' in fixed
    assert 'S2{"虚频?"}' in fixed


def test_validate_mermaid_render_safe_passes_after_normalize() -> None:
    content = _plan(
        "flowchart TD\n  S1[relax: PBE] --> S2[phonon]\n",
        "| Step ID | 任务类型 | 前置步骤 | 产出 |\n"
        "| S1 | geometry_relax | - | a |\n"
        "| S2 | phonon | S1 | b |\n",
    )
    ok, issues, normalized = validate_mermaid_render_safe(content)
    assert ok is True
    assert issues == []
    assert normalized is not None

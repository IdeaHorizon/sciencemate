"""Domain-agnostic coherence checks for computational_workflow task tables.

Does NOT encode discipline-specific physics (e.g. "bulk relax must use primitive cell").
Instead enforces traceability: modeling choices are documented, cross-step changes are
explicit, and diagram ↔ table representations align.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .workflow_graph import extract_mermaid_block, parse_task_tables

_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_SUPERCELL_RE = re.compile(
    r"(\d+)\s*[x×]\s*(\d+)\s*[x×]\s*(\d+)",
    flags=re.IGNORECASE,
)
_MERMAID_NODE_RE = re.compile(
    r"([A-Za-z][\w]*)\s*(?:\[[^\]]*\]|\{[^}]*\})",
)
_BARE_DIMENSIONS_RE = re.compile(
    r"^\s*(\d+\s*[x×]\s*\d+\s*[x×]\s*\d+)\s*$",
    flags=re.IGNORECASE,
)
_BRIDGE_TASK_RE = re.compile(
    r"(post|preprocess|process|transform|convert|merge|extract|import|build|"
    r"construct|map|reduce|expand|average|avg|restruct|remap|interpolat)",
    flags=re.IGNORECASE,
)

_PRIMITIVE_LABELS = frozenset({
    "原胞", "primitive", "单胞", "conventional", "unit cell", "unitcell",
    "1x1x1", "1×1×1", "minimal", "default",
})
_MIN_RATIONALE_LEN = 12
_MIN_STEP_DETAIL_LEN = 6


@dataclass
class ScienceIssue:
    severity: str  # error | warning
    step_id: str
    rule_id: str
    message: str
    suggestion: str = ""


@dataclass
class TaskRowScience:
    step_id: str
    task_type: str
    predecessors: list[str]
    model_scope_raw: str
    key_params: str
    output_col: str
    falsifier_col: str
    model_product: int
    model_label: str


@dataclass
class ScienceReport:
    ok: bool
    issues: list[ScienceIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ScienceIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ScienceIssue]:
        return [i for i in self.issues if i.severity == "warning"]


def parse_model_scope(text: str) -> tuple[int, str]:
    """Normalize a model-scope / supercell / system-size cell to (product, label)."""
    raw = (text or "").strip()
    lower = raw.lower()
    if not raw or lower in {"-", "—", "–", "n/a", "na", "none", "null", "same", "inherit", "继承"}:
        return 1, "unspecified"
    if any(label in lower for label in _PRIMITIVE_LABELS):
        return 1, "primitive"
    match = _SUPERCELL_RE.search(raw)
    if match:
        a, b, c = (int(match.group(i)) for i in range(1, 4))
        return a * b * c, f"{a}x{b}x{c}"
    return 1, raw


def model_signature(row: TaskRowScience) -> str:
    if row.model_label not in {"unspecified", "primitive"}:
        return f"p{row.model_product}:{row.model_label.lower()}"
    return f"p{row.model_product}"


def _find_column(cells: list[str], keywords: tuple[str, ...]) -> int | None:
    for idx, cell in enumerate(cells):
        lower = cell.strip().lower()
        if any(k in lower or k in cell for k in keywords):
            return idx
    return None


def _is_step_table_header(cells: list[str]) -> bool:
    joined = " ".join(cells).lower()
    return "step id" in joined or "step_id" in joined or "步骤" in joined


def _is_bridge_task(task_type: str) -> bool:
    return bool(_BRIDGE_TASK_RE.search(task_type or ""))


def _has_substantive_text(text: str, *, min_len: int = _MIN_RATIONALE_LEN) -> bool:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    return len(cleaned) >= min_len


def _model_scope_has_inline_rationale(raw: str) -> bool:
    text = (raw or "").strip()
    if not text:
        return False
    if _BARE_DIMENSIONS_RE.match(text):
        return False
    if _SUPERCELL_RE.search(text):
        remainder = _SUPERCELL_RE.sub("", text).strip(" ,;:-–—|/")
        return _has_substantive_text(remainder, min_len=8)
    return _has_substantive_text(text, min_len=8)


def _step_has_traceability(row: TaskRowScience) -> bool:
    return (
        _has_substantive_text(row.key_params, min_len=_MIN_STEP_DETAIL_LEN)
        or _has_substantive_text(row.output_col, min_len=_MIN_STEP_DETAIL_LEN)
        or _has_substantive_text(row.falsifier_col, min_len=_MIN_STEP_DETAIL_LEN)
    )


def _combined_rationale(row: TaskRowScience) -> str:
    return f"{row.model_scope_raw} {row.key_params}".strip()


def _has_model_choice_rationale(row: TaskRowScience) -> bool:
    if row.model_product <= 1 and row.model_label in {"unspecified", "primitive"}:
        return True
    if _model_scope_has_inline_rationale(row.model_scope_raw):
        return True
    return _has_substantive_text(row.key_params)


def parse_task_rows_for_science(content: str, stats: dict | None = None,
                                 ) -> list[TaskRowScience]:
    """Parse task-table rows for generic coherence checks.

    #157：只有**真正的 computational workflow table** 才产生 TaskRowScience。
    research plan 里常见的"资源估算 / 预算 / 进度 / 责任"表同样用 `Step ID` +
    `任务类型` 索引，但不承担 scientific traceability 职责 —— 以前它们也被当
    workflow 表解析，三个 trace 列全 None → 每行都空 → validator 对每个 step 报
    false `step_not_traceable`。agent 于是反复重写**本来就合格**的 task table，
    错误数永远不降（实测 19→18→18，31 turns 烧 1.77M token 才被人工 /stop）。
    判据：除 Step ID + 任务类型 外，至少要有一个可追溯列（关键参数/产出/falsifier）。

    stats（可选）：回填 workflow_tables_detected / non_workflow_tables_ignored /
    task_rows_audited / duplicate_step_ids，供 audit 输出诊断（qinp 建议 3）。
    """
    steps_map = parse_task_tables(content)
    rows: list[TaskRowScience] = []
    n_workflow_tables = 0
    n_ignored_tables = 0
    lines = content.splitlines()
    idx = 0
    while idx < len(lines):
        row_match = _TABLE_ROW_RE.match(lines[idx])
        if not row_match:
            idx += 1
            continue
        header_cells = [c.strip() for c in row_match.group(1).split("|")]
        if not _is_step_table_header(header_cells):
            idx += 1
            continue

        type_col = _find_column(header_cells, ("任务类型", "task type", "task_type", "类型"))
        scope_col = _find_column(header_cells, (
            "超胞", "supercell", "cell", "模型", "model", "system", "离散",
        ))
        param_col = _find_column(header_cells, ("关键参数", "key param", "parameters", "参数"))
        output_col = _find_column(header_cells, ("产出", "output", "deliverable"))
        falsifier_col = _find_column(header_cells, ("falsifier", "证伪", "对应 falsifier"))
        if type_col is None:
            idx += 1
            continue
        # #157：没有任何可追溯列 → 资源/预算/进度表，不是 workflow 表，整张忽略。
        # 注意"声明了 trace 列但单元格为空"仍会往下走 → step_not_traceable 照常
        # 失败（不能靠忽略空内容绕过科学门槛）。
        if param_col is None and output_col is None and falsifier_col is None:
            n_ignored_tables += 1
            idx += 1
            continue
        n_workflow_tables += 1

        idx += 1
        if idx < len(lines) and _TABLE_ROW_RE.match(lines[idx]):
            sep_cells = [c.strip() for c in _TABLE_ROW_RE.match(lines[idx]).group(1).split("|")]
            if all(set(c) <= {"-", ":", " "} for c in sep_cells if c):
                idx += 1

        while idx < len(lines):
            row_match = _TABLE_ROW_RE.match(lines[idx])
            if not row_match:
                break
            cells = [c.strip() for c in row_match.group(1).split("|")]
            if len(cells) <= type_col:
                break
            step_id = cells[0].strip().strip("`")
            if not step_id or step_id.lower() in {"step id", "step_id", "步骤"}:
                idx += 1
                continue
            task_type = cells[type_col].strip().lower().replace(" ", "_")
            def _cell(col: int | None) -> str:
                return (cells[col].strip()
                        if col is not None and col < len(cells) else "")

            scope_raw = _cell(scope_col)
            key_params = _cell(param_col)
            output_val = _cell(output_col)
            falsifier_val = (
                cells[falsifier_col].strip()
                if falsifier_col is not None and falsifier_col < len(cells)
                else ""
            )
            product, label = parse_model_scope(scope_raw)
            pred = steps_map.get(step_id)
            rows.append(TaskRowScience(
                step_id=step_id,
                task_type=task_type,
                predecessors=list(pred.predecessors) if pred else [],
                model_scope_raw=scope_raw,
                key_params=key_params,
                output_col=output_val,
                falsifier_col=falsifier_val,
                model_product=product,
                model_label=label,
            ))
            idx += 1

    # #157：跨多张 workflow 表的重复 Step ID 要有确定性语义 —— 内容一致就去重
    # （只审一次），内容冲突交给 validator 报 duplicate_step_conflict。绝不能
    # 静默追加第二套 rows 再报一组误导性的内容错误。
    deduped, conflicts = _dedupe_rows_by_step_id(rows)
    if stats is not None:
        stats.update({
            "workflow_tables_detected": n_workflow_tables,
            "non_workflow_tables_ignored": n_ignored_tables,
            "task_rows_audited": len(deduped),
            "duplicate_step_ids": sorted(conflicts),
        })
    return deduped


def _row_trace_signature(row: TaskRowScience) -> tuple:
    """判定两行"同一 step 的重复描述"是否内容一致（只比科学相关字段）。"""
    return (row.task_type, row.model_scope_raw.strip(),
            row.key_params.strip(), row.output_col.strip(),
            row.falsifier_col.strip())


def _dedupe_rows_by_step_id(
    rows: list[TaskRowScience],
) -> tuple[list[TaskRowScience], set[str]]:
    """同 step_id：内容一致 → 保留第一条；内容冲突 → 保留第一条并记进 conflicts
    （validator 据此报 duplicate_step_conflict）。返回 (deduped, conflict_ids)。"""
    seen: dict[str, TaskRowScience] = {}
    order: list[str] = []
    conflicts: set[str] = set()
    for row in rows:
        prev = seen.get(row.step_id)
        if prev is None:
            seen[row.step_id] = row
            order.append(row.step_id)
            continue
        if _row_trace_signature(prev) != _row_trace_signature(row):
            conflicts.add(row.step_id)
    return [seen[sid] for sid in order], conflicts


def _extract_mermaid_step_labels(content: str) -> dict[str, str]:
    mermaid = extract_mermaid_block(content) or ""
    labels: dict[str, str] = {}
    for line in mermaid.splitlines():
        for match in re.finditer(
            r'([A-Za-z][\w]*)\s*(?:\[(.*?)\]|\{(.*?)\})',
            line,
        ):
            step_id = match.group(1)
            label = (match.group(2) or match.group(3) or "").strip()
            if step_id.lower() in {"flowchart", "graph", "subgraph", "end", "direction"}:
                continue
            labels[step_id] = label
    return labels


def _cell_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _SUPERCELL_RE.finditer(text or ""):
        tokens.add(f"{match.group(1)}x{match.group(2)}x{match.group(3)}".lower())
    lower = (text or "").lower()
    for label in _PRIMITIVE_LABELS:
        if label in lower:
            tokens.add("primitive")
    return tokens


def _check_diagram_table_alignment(
    rows: list[TaskRowScience],
    content: str,
) -> list[ScienceIssue]:
    issues: list[ScienceIssue] = []
    mermaid_labels = _extract_mermaid_step_labels(content)
    by_id = {r.step_id: r for r in rows}

    for step_id, label in mermaid_labels.items():
        row = by_id.get(step_id)
        if row is None:
            continue
        table_blob = f"{row.model_scope_raw} {row.task_type}"
        mermaid_tokens = _cell_tokens(label)
        table_tokens = _cell_tokens(table_blob)
        if not mermaid_tokens or not table_tokens:
            continue
        if mermaid_tokens.isdisjoint(table_tokens):
            issues.append(ScienceIssue(
                severity="warning",
                step_id=step_id,
                rule_id="diagram_table_model_mismatch",
                message=(
                    f"{step_id} 的 mermaid 标签与任务表「模型/超胞」列对同一建模尺度描述不一致。"
                ),
                suggestion="统一 mermaid 节点标签与任务表中的模型尺度表述。",
            ))
    return issues


def _path_includes_bridge(pred_id: str, step_id: str, by_id: dict[str, TaskRowScience]) -> bool:
    """Walk predecessors from step_id; True if a bridge task appears before reaching pred_id."""
    queue = list(by_id.get(step_id, TaskRowScience("", "", [], "", "", "", "", 1, "")).predecessors)
    visited: set[str] = set()
    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        row = by_id.get(cur)
        if row is None:
            continue
        if _is_bridge_task(row.task_type):
            return True
        if cur == pred_id:
            continue
        queue.extend(row.predecessors)
    return False


def validate_workflow_science(content: str, stats: dict | None = None) -> ScienceReport:
    """Run domain-agnostic workflow coherence checks."""
    issues: list[ScienceIssue] = []
    _stats: dict = {}
    rows = parse_task_rows_for_science(content, stats=_stats)
    by_id = {r.step_id: r for r in rows}
    if stats is not None:
        stats.update(_stats)

    # #160：0-row 不得假通过。#157 会忽略资源/预算表；若最终没有任何
    # workflow 任务行，以前 issues=[] → ok=True → audit 自动 save。这里显式失败。
    if not rows:
        ignored = int(_stats.get("non_workflow_tables_ignored") or 0)
        issues.append(ScienceIssue(
            severity="error",
            step_id="-",
            rule_id="no_workflow_task_rows",
            message=(
                "未解析到可审计的 computational_workflow 任务行"
                + (f"（已忽略 {ignored} 张非 workflow 表）" if ignored else "")
                + "。仅有资源估算/进度表不算 workflow。"
            ),
            suggestion=(
                "补一张含 Step ID + 任务类型 +（关键参数/产出/falsifier 至少一列）"
                "的 workflow 任务表，再 audit。"
            ),
        ))
        return ScienceReport(ok=False, issues=issues)

    # #157：同一 Step ID 在多张 workflow 表里内容冲突 → 明确报冲突（附 step id），
    # 而不是生成第二套 rows 再给一组误导性的 traceability 错误。
    for dup_id in _stats.get("duplicate_step_ids", []):
        issues.append(ScienceIssue(
            severity="error",
            step_id=dup_id,
            rule_id="duplicate_step_conflict",
            message=(
                f"{dup_id} 在多张 workflow 表里重复出现，且内容不一致"
                f"（任务类型/模型尺度/关键参数/产出/falsifier 至少一项冲突）。"
            ),
            suggestion=(
                "同一 Step ID 只应有一份权威定义：合并成一行，或给不同步骤换 ID。"
                "（内容完全一致的重复会被自动去重，不报错。）"
            ),
        ))

    for row in rows:
        if not _step_has_traceability(row):
            issues.append(ScienceIssue(
                severity="error",
                step_id=row.step_id,
                rule_id="step_not_traceable",
                message=(
                    f"{row.step_id} ({row.task_type}) 缺少可追溯说明："
                    f"「关键参数」「产出」「对应 falsifier」至少一项应有实质内容。"
                ),
                suggestion="补充本步要回答的问题、主要设置或预期产出。",
            ))

        if row.model_product > 1 and not _has_model_choice_rationale(row):
            issues.append(ScienceIssue(
                severity="error",
                step_id=row.step_id,
                rule_id="nondefault_model_undocumented",
                message=(
                    f"{row.step_id} 使用非默认模型尺度 ({row.model_label})，"
                    f"但未在「模型/超胞」或「关键参数」中说明选择依据。"
                ),
                suggestion=(
                    "在关键参数中写清为何需要此模型尺度（与上一步的差异、要消除的误差来源等）。"
                ),
            ))

        if _is_bridge_task(row.task_type):
            continue

        for pred_id in row.predecessors:
            pred = by_id.get(pred_id)
            if pred is None:
                continue
            if model_signature(pred) == model_signature(row):
                continue
            if _path_includes_bridge(pred_id, row.step_id, by_id):
                continue
            if _has_substantive_text(_combined_rationale(row)):
                continue
            issues.append(ScienceIssue(
                severity="error",
                step_id=row.step_id,
                rule_id="model_scope_change_unexplained",
                message=(
                    f"{row.step_id} 相对前置 {pred_id} 变更了模型尺度 "
                    f"({pred.model_label} → {row.model_label})，"
                    f"但缺少 bridge 步骤（postprocess/transform 等）或文字说明。"
                ),
                suggestion=(
                    "插入显式模型转换步骤，或在关键参数中说明为何以及如何变更模型尺度。"
                ),
            ))

    issues.extend(_check_diagram_table_alignment(rows, content))

    return ScienceReport(
        ok=not any(i.severity == "error" for i in issues),
        issues=issues,
    )


def format_science_report(report: ScienceReport) -> str:
    if not report.issues:
        return "computational_workflow 一致性检查通过（建模选择可追溯）"
    lines = []
    for issue in report.issues:
        mark = "✗" if issue.severity == "error" else "⚠"
        lines.append(f"{mark} [{issue.rule_id}] {issue.step_id}: {issue.message}")
        if issue.suggestion:
            lines.append(f"  → 建议: {issue.suggestion}")
    if report.ok:
        lines.insert(0, f"通过（{len(report.warnings)} 条 warning，需人工确认）")
    else:
        lines.insert(0, f"未通过（{len(report.errors)} error, {len(report.warnings)} warning）")
    return "\n".join(lines)

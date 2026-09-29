"""Parse and validate computational_workflow DAG (fork / join / gate).

Supports:
- **Fork**: one step → multiple child steps (parallel branches)
- **Join**: one step with multiple predecessors (AND semantics)
- **Gate**: mermaid diamond `{condition?}` nodes with conditional edges
- **Mermaid syntax**: validate + normalize labels so renderers don't error
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


_EDGE_RE = re.compile(
    r"^\s*([A-Za-z][\w]*)\s*-->\s*(?:\|[^|]*\|\s*)?([A-Za-z][\w]*)\s*(?:%%.*)?$",
)
_INLINE_EDGE_RE = re.compile(
    r"([A-Za-z][\w]*)\s*(?:\[[^\]]*\]|\{[^}]*\})?\s*-->\s*(?:\|[^|]*\|\s*)?"
    r"([A-Za-z][\w]*)\s*(?:\[[^\]]*\]|\{[^}]*\})?",
)
_NODE_DEF_RE = re.compile(r"^\s*([A-Za-z][\w]*)\s*[\[\{]")
_GATE_NODE_RE = re.compile(r"([A-Za-z][\w]*)\s*\{")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_PREDECESSOR_SPLIT_RE = re.compile(r"\s*(?:,|\+|∧|/|\band\b|\&)\s*", flags=re.IGNORECASE)
_EMPTY_PREDECESSOR = frozenset({"", "-", "—", "–", "n/a", "na", "none", "null", "无", "无前置"})

# Mermaid syntax helpers
_FLOWCHART_HEAD_RE = re.compile(r"^\s*(flowchart|graph)\s+(TD|TB|BT|RL|LR)\s*$", re.IGNORECASE)
_NODE_ID_RE = re.compile(r"^[A-Za-z][\w]*$")
_SQUARE_LABEL_RE = re.compile(r'([A-Za-z][\w]*)\[(?!["\'])([^\]"\']+)\]')
_DIAMOND_LABEL_RE = re.compile(r'([A-Za-z][\w]*)\{(?!["\'])([^}"\']+)\}')
_SUBGRAPH_OPEN_RE = re.compile(r"^\s*subgraph\b", re.IGNORECASE)
_RISKY_LABEL_CHARS = frozenset(":/@+&#<>()?\\|[]{}")


@dataclass
class WorkflowGraph:
    nodes: set[str] = field(default_factory=set)
    edges: list[tuple[str, str]] = field(default_factory=list)
    outgoing: dict[str, list[str]] = field(default_factory=dict)
    incoming: dict[str, list[str]] = field(default_factory=dict)
    gate_nodes: set[str] = field(default_factory=set)


@dataclass
class TaskStep:
    step_id: str
    predecessors: list[str]
    raw_predecessors: str = ""


def extract_mermaid_block(content: str) -> str | None:
    match = re.search(r"```mermaid\s*(.*?)\s*```", content, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _escape_mermaid_quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _label_needs_quotes(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if any(c in stripped for c in _RISKY_LABEL_CHARS):
        return True
    if any(ord(c) > 127 for c in stripped):
        return True
    if stripped[0].isdigit():
        return True
    return False


def _line_without_label_text(line: str) -> str:
    """Remove label payloads so structural checks don't scan inside labels."""
    cleaned = re.sub(r'\["[^"]*"\]', "[]", line)
    cleaned = re.sub(r"\[[^\]]*\]", "[]", cleaned)
    cleaned = re.sub(r'\{"[^"]*"\}', "{}", cleaned)
    cleaned = re.sub(r"\{[^}]*\}", "{}", cleaned)
    return cleaned


def normalize_mermaid_syntax(mermaid: str) -> str:
    """Quote risky node labels so Mermaid renderers don't throw Syntax Error."""
    lines: list[str] = []
    for raw_line in mermaid.splitlines():
        line = raw_line
        line = re.sub(r"(?<![\-<>])-(?![\->])>", "-->", line)
        line = re.sub(r"<(?![\-=])-(?![>])", "<--", line)

        def _quote_square(match: re.Match[str]) -> str:
            node_id, label = match.group(1), match.group(2).strip()
            if not _label_needs_quotes(label):
                return match.group(0)
            return f'{node_id}["{_escape_mermaid_quote(label)}"]'

        def _quote_diamond(match: re.Match[str]) -> str:
            node_id, label = match.group(1), match.group(2).strip()
            if not _label_needs_quotes(label):
                return match.group(0)
            return f'{node_id}{{"{_escape_mermaid_quote(label)}"}}'

        line = _SQUARE_LABEL_RE.sub(_quote_square, line)
        line = _DIAMOND_LABEL_RE.sub(_quote_diamond, line)
        lines.append(line)
    return "\n".join(lines).strip()


def validate_mermaid_syntax(mermaid: str) -> tuple[bool, list[str]]:
    """Static checks for common Mermaid Syntax Error causes."""
    issues: list[str] = []
    if not mermaid.strip():
        return False, ["mermaid 代码块为空"]

    lines = [ln.rstrip() for ln in mermaid.splitlines()]
    non_empty = [ln for ln in lines if ln.strip() and not ln.strip().startswith("%%")]
    if not non_empty:
        return False, ["mermaid 代码块为空"]

    if not _FLOWCHART_HEAD_RE.match(non_empty[0]):
        issues.append("首行必须是 flowchart TD/TB/LR 或 graph TD/TB/LR")

    subgraph_depth = 0
    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("%%"):
            continue

        if _SUBGRAPH_OPEN_RE.match(line):
            subgraph_depth += 1
            # subgraph id with slash breaks some renderers: subgraph H1/H2
            bad_id = re.match(r"subgraph\s+([^\s\[]+/[^\s\[]+)", line, flags=re.IGNORECASE)
            if bad_id:
                issues.append(
                    f"第 {lineno} 行 subgraph ID 含 '/'（{bad_id.group(1)}），"
                    "请改为 subgraph H1_H2 [\"H1/H2\"]"
                )
        if line.lower() == "end":
            subgraph_depth = max(0, subgraph_depth - 1)

        if re.search(r"(?<![\-<>])-(?![\->])>", line):
            issues.append(f"第 {lineno} 行使用了 `-` 单箭头，应写 `-->`")

        for bracket, close in (("[", "]"), ("{", "}")):
            if line.count(bracket) != line.count(close):
                issues.append(f"第 {lineno} 行 {bracket}{close} 括号未闭合")

        structural = _line_without_label_text(line)

        for node_id in re.findall(r"\b(\d+[A-Za-z]\w*)\b", structural):
            issues.append(f"第 {lineno} 行节点 ID `{node_id}` 不能以数字开头")

        for node_id in re.findall(r"([A-Za-z][\w]*)\s*[\[\{]", structural):
            if not _NODE_ID_RE.match(node_id):
                issues.append(f"第 {lineno} 行节点 ID `{node_id}` 不合法")

        for match in _SQUARE_LABEL_RE.finditer(line):
            label = match.group(2)
            if _label_needs_quotes(label):
                issues.append(
                    f"第 {lineno} 行节点 {match.group(1)} 标签 `{label}` "
                    "含特殊字符/中文，需用双引号包裹，如 "
                    f'{match.group(1)}["{label}"]'
                )
        for match in _DIAMOND_LABEL_RE.finditer(line):
            label = match.group(2)
            if _label_needs_quotes(label):
                issues.append(
                    f"第 {lineno} 行门控 {match.group(1)} 标签 `{label}` "
                    f'需加引号: {match.group(1)}{{"{label}"}}'
                )

        # duplicate shape redefinition on same line is ok; across file we warn lightly
        pipe_count = line.count("|")
        if "-->" in line and pipe_count % 2 == 1 and re.search(r"-->\s*\|[^|]*$", line):
            issues.append(f"第 {lineno} 行边标签 `|…|` 可能未闭合")

    if subgraph_depth > 0:
        issues.append("subgraph 未闭合：缺少 end")

    # dedupe while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for item in issues:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return len(deduped) == 0, deduped


def validate_mermaid_render_safe(content: str) -> tuple[bool, list[str], str | None]:
    """Validate mermaid syntax; return normalized block if fixable."""
    mermaid = extract_mermaid_block(content)
    if not mermaid:
        return False, ["缺少 mermaid 流程图"], None
    normalized = normalize_mermaid_syntax(mermaid)
    ok, issues = validate_mermaid_syntax(normalized)
    return ok, issues, normalized if normalized != mermaid else None


def _add_edge(graph: WorkflowGraph, src: str, dst: str) -> None:
    graph.nodes.add(src)
    graph.nodes.add(dst)
    graph.edges.append((src, dst))
    graph.outgoing.setdefault(src, [])
    if dst not in graph.outgoing[src]:
        graph.outgoing[src].append(dst)
    graph.incoming.setdefault(dst, [])
    if src not in graph.incoming[dst]:
        graph.incoming[dst].append(src)


def parse_mermaid_flowchart(mermaid: str) -> WorkflowGraph:
    graph = WorkflowGraph()
    for raw_line in mermaid.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("flowchart", "graph", "subgraph", "end", "style", "class", "linkStyle")):
            continue
        if line.startswith("direction "):
            continue

        for gate_id in _GATE_NODE_RE.findall(line):
            graph.gate_nodes.add(gate_id)
            graph.nodes.add(gate_id)

        node_match = _NODE_DEF_RE.match(line)
        if node_match:
            graph.nodes.add(node_match.group(1))

        edges_found = False
        if "-->" in line:
            for src, dst in _INLINE_EDGE_RE.findall(line):
                _add_edge(graph, src, dst)
                edges_found = True

        if not edges_found:
            edge_match = _EDGE_RE.match(line)
            if edge_match:
                _add_edge(graph, edge_match.group(1), edge_match.group(2))
    return graph


def _normalize_step_id(raw: str) -> str:
    return raw.strip().strip("`").strip()


def _parse_predecessors(raw: str) -> list[str]:
    text = (raw or "").strip()
    if text.lower() in _EMPTY_PREDECESSOR:
        return []
    parts = [p.strip() for p in _PREDECESSOR_SPLIT_RE.split(text) if p.strip()]
    cleaned: list[str] = []
    for part in parts:
        token = _normalize_step_id(part)
        if token.lower() in _EMPTY_PREDECESSOR:
            continue
        cleaned.append(token)
    return cleaned


def _find_predecessor_column(cells: list[str]) -> int | None:
    for idx, cell in enumerate(cells):
        lower = cell.strip().lower()
        if "前置" in cell or "predecessor" in lower or "depends" in lower or "dependency" in lower:
            return idx
    return None


def _is_step_table_header(cells: list[str]) -> bool:
    joined = " ".join(cells).lower()
    return "step id" in joined or "step_id" in joined or "步骤" in joined


def parse_task_tables(content: str) -> dict[str, TaskStep]:
    steps: dict[str, TaskStep] = {}
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

        pred_col = _find_predecessor_column(header_cells)
        if pred_col is None:
            idx += 1
            continue

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
            if len(cells) <= pred_col:
                break
            step_id = _normalize_step_id(cells[0])
            if not step_id or step_id.lower() in {"step id", "step_id", "步骤"}:
                idx += 1
                continue
            raw_pred = cells[pred_col]
            predecessors = _parse_predecessors(raw_pred)
            if step_id in steps:
                merged = list(dict.fromkeys(steps[step_id].predecessors + predecessors))
                steps[step_id] = TaskStep(
                    step_id=step_id,
                    predecessors=merged,
                    raw_predecessors=steps[step_id].raw_predecessors or raw_pred,
                )
            else:
                steps[step_id] = TaskStep(
                    step_id=step_id,
                    predecessors=predecessors,
                    raw_predecessors=raw_pred,
                )
            idx += 1
    return steps


def _compute_steps(steps: dict[str, TaskStep], graph: WorkflowGraph) -> set[str]:
    """Executable steps = table rows excluding pure gate/decision nodes."""
    gate_only = graph.gate_nodes - set(steps)
    return {sid for sid in steps if sid not in gate_only}


def _dependency_edges_from_steps(steps: dict[str, TaskStep]) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for step_id, step in steps.items():
        for pred in step.predecessors:
            if pred in steps:
                edges.append((pred, step_id))
    return edges


def find_cycles(nodes: set[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Return dependency cycles as node lists (pred → step direction)."""
    graph: dict[str, list[str]] = {n: [] for n in nodes}
    for src, dst in edges:
        graph.setdefault(src, []).append(dst)
        graph.setdefault(dst, graph.get(dst, []))

    color: dict[str, int] = {n: 0 for n in nodes}  # 0=unseen 1=stack 2=done
    stack: list[str] = []
    cycles: list[list[str]] = []

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in color:
                color[nxt] = 0
            state = color.get(nxt, 0)
            if state == 1:
                start = stack.index(nxt)
                cycle = stack[start:] + [nxt]
                if cycle not in cycles:
                    cycles.append(cycle)
            elif state == 0:
                dfs(nxt)
        stack.pop()
        color[node] = 2

    for node in nodes:
        if color.get(node, 0) == 0:
            dfs(node)
    return cycles


def _format_cycle(cycle: list[str]) -> str:
    if len(cycle) <= 1:
        return " → ".join(cycle)
    body = " → ".join(cycle[:-1])
    if cycle[0] == cycle[-1]:
        return f"{body} → {cycle[0]} (环)"
    return " → ".join(cycle)


def validate_workflow_dag(content: str) -> tuple[bool, list[str], dict]:
    """Validate fork/join/gate consistency between mermaid and task tables."""
    issues: list[str] = []
    meta: dict = {
        "fork_nodes": [],
        "join_nodes": [],
        "gate_nodes": [],
        "n_steps": 0,
        "n_edges": 0,
    }

    mermaid = extract_mermaid_block(content)
    if not mermaid:
        return False, ["缺少 mermaid 流程图"], meta

    syntax_ok, syntax_issues, _ = validate_mermaid_render_safe(content)
    if not syntax_ok:
        return False, [f"mermaid 语法: {i}" for i in syntax_issues[:5]], meta

    graph = parse_mermaid_flowchart(normalize_mermaid_syntax(mermaid))
    steps = parse_task_tables(content)
    compute_steps = _compute_steps(steps, graph)

    meta["gate_nodes"] = sorted(graph.gate_nodes)
    meta["n_steps"] = len(steps)
    meta["n_edges"] = len(graph.edges)

    if not steps:
        return False, ["缺少逐步计算任务表 (| Step ID | ... | 前置步骤 |)"], meta

    # Unique step IDs across tables
    if len(steps) != len({s.step_id for s in steps.values()}):
        issues.append("任务表 Step ID 重复")

    all_step_ids = set(steps)
    for step in steps.values():
        for pred in step.predecessors:
            if pred not in all_step_ids and pred not in graph.gate_nodes:
                issues.append(
                    f"{step.step_id} 的前置步骤 {pred} 不存在于任务表"
                )

    table_cycles = find_cycles(all_step_ids, _dependency_edges_from_steps(steps))
    if table_cycles:
        meta["cycles"] = table_cycles
        for cycle in table_cycles[:3]:
            issues.append(
                f"任务表存在循环依赖: {_format_cycle(cycle)}；"
                "门控回退请用 S8b→S9b 独立链，勿把下游 Step 列为回退 Step 的前置"
            )
    else:
        meta["cycles"] = []

    mermaid_exec_nodes = set(graph.nodes) - graph.gate_nodes
    mermaid_cycles = find_cycles(mermaid_exec_nodes, graph.edges)
    if mermaid_cycles:
        meta["mermaid_cycles"] = mermaid_cycles
        for cycle in mermaid_cycles[:3]:
            issues.append(
                f"mermaid 存在循环边: {_format_cycle(cycle)}；"
                "回退请画成 S8b→S9b→S10b 无环支链，不要 S8b→S9→…→S8b"
            )
    else:
        meta["mermaid_cycles"] = []

    # Fork: out-degree >= 2 to non-gate children (gate 菱形只做条件路由，不要求写入任务表)
    for node, children in graph.outgoing.items():
        if node in graph.gate_nodes:
            continue
        non_gate_children = [c for c in children if c not in graph.gate_nodes]
        if len(non_gate_children) < 2:
            continue
        meta["fork_nodes"].append({
            "node": node,
            "children": non_gate_children,
        })
        for child in non_gate_children:
            if child not in steps:
                issues.append(
                    f"mermaid 分叉 {node} → {child}，但任务表缺少 Step {child}"
                )
                continue
            if node not in steps[child].predecessors:
                issues.append(
                    f"分叉节点 {node} 衍生 {child}，但 {child} 的前置步骤未包含 {node} "
                    f"(当前: {steps[child].raw_predecessors or '空'})"
                )

    # Join: in-degree >= 2 from non-gate parents
    for node, parents in graph.incoming.items():
        if node in graph.gate_nodes:
            continue
        non_gate_parents = [p for p in parents if p not in graph.gate_nodes]
        if len(non_gate_parents) < 2:
            continue
        meta["join_nodes"].append({
            "node": node,
            "parents": non_gate_parents,
        })
        if node not in steps:
            issues.append(
                f"mermaid 汇合 {', '.join(non_gate_parents)} → {node}，但任务表缺少 Step {node}"
            )
            continue
        missing = [p for p in non_gate_parents if p not in steps[node].predecessors]
        if missing:
            issues.append(
                f"汇合节点 {node} 需要多前置 {non_gate_parents}，"
                f"任务表缺少: {', '.join(missing)} "
                f"(当前: {steps[node].raw_predecessors or '空'})"
            )

    # Table-declared joins must match mermaid incoming edges
    join_seen: set[str] = {j["node"] for j in meta["join_nodes"]}
    for step_id, step in steps.items():
        if len(step.predecessors) < 2:
            continue
        if step_id not in join_seen:
            meta["join_nodes"].append({
                "node": step_id,
                "parents": step.predecessors,
                "source": "task_table",
            })
            join_seen.add(step_id)
        mermaid_parents = [
            p for p in graph.incoming.get(step_id, [])
            if p not in graph.gate_nodes
        ]
        for pred in step.predecessors:
            if pred in graph.gate_nodes:
                continue
            if pred not in mermaid_parents and pred in graph.nodes:
                issues.append(
                    f"{step_id} 任务表声明前置 {pred}，但 mermaid 缺少边 {pred} --> {step_id}"
                )
        for pred in mermaid_parents:
            if pred not in step.predecessors:
                issues.append(
                    f"mermaid 边 {pred} --> {step_id} 未写入 {step_id} 的前置步骤列"
                )

    # Mermaid edges → table predecessors (skip gate sources/targets)
    for src, dst in graph.edges:
        if dst in graph.gate_nodes or src in graph.gate_nodes:
            continue
        if dst not in steps:
            continue
        if src not in steps[dst].predecessors and src in all_step_ids:
            issues.append(
                f"mermaid 边 {src} --> {dst} 未反映在 {dst} 的前置步骤 "
                f"(当前: {steps[dst].raw_predecessors or '空'})"
            )

    # Gate nodes should be documented when present
    if graph.gate_nodes:
        lower = content.lower()
        has_gate_doc = any(
            kw in lower
            for kw in (
                "gate g",
                "gate check",
                "门控",
                "步骤依赖",
                "dependencies & gates",
                "dependency & gate",
            )
        )
        if not has_gate_doc:
            issues.append(
                "mermaid 含门控节点 {…}，但未找到「步骤依赖与门控」说明"
            )

    # Parallel / fork should mention parallelization when forks exist
    if meta["fork_nodes"]:
        lower = content.lower()
        if not any(kw in lower for kw in ("并行", "parallel", "embarrassingly parallel", "可并行")):
            issues.append(
                "存在一对多分叉，但未说明哪些分支可并行执行"
            )

    passed = len(issues) == 0
    return passed, issues, meta

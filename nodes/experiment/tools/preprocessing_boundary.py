"""前处理边界的机械观察层。

按文件名和生成器命令记录本次运行的线索，不证明实际生成了什么，更不证明越权。
即使未与 Data 回执对账，生成也可能是任务明确授权的科学工作。
这些线索不是质量判决、完成门禁或长期错误经验；权限与正式输入核验由原契约负责。

## 只观察，不拦截

这里刻意不做执行前拦截。前处理边界的误判面很宽（见下面的白名单），而框架已经
有过一次自伤记录：`tools/path_roles.py` 里那条过严的 role 契约把整个 run 变成
不可申诉的死锁。在误判率有真实数据之前，硬门禁的期望损失高于收益 —— 机器负责
"看见"，判决交给 reviewer 和人。

## 两类诊断线索

1. **生成线索，不是生成事实**。匹配生成器命令，或疑似正式输入文件的写入语法
   （重定向 / tee / heredoc / write_file / python 写文件）。
   读取、`cp` / `mv` / `ln` 一概不计 —— harness 把"按 prereg 把已有输入复制进
   workdir"和"CONTCAR→POSCAR 续跑"明文列为本节点可做的运行化轻调，靠语义把它们
   排除在外，比事后拿特例白名单去捞回来更不容易漏也更不容易误伤。
   唯一的逐条白名单是**只探测不生成**（`gmsh --version` / `which packmol`），
   且必须是整条命令就是个探测，`-v` 这种会跟 verbosity 撞车的短选项不算。
   白名单按 **shell 段**判而不是整条命令：`gmsh --version && gmsh -3 a.geo -o a.msh`
   若按整条判就会被"前半句是探测"带着整条放行 —— 那等于给隐藏生成留了后门。

2. **run 级背景**：保留 verified Data / fallback 交付及 operation scope 的已有
   对账信息，便于本次诊断。未对账不证明违规，已对账也不证明某个文件已获授权。
   blocker 文本本身不授权生成；权限、正式输入来源及冻结要求仍由原契约核验。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


BASH_TOOL_NAMES = frozenset({"run_bash", "safe_run_bash"})
PY_TOOL_NAMES = frozenset({"execute_python", "safe_execute_python"})
WRITE_TOOL_NAMES = frozenset({"write_file", "safe_write_file"})


# 生成器命令线索：命中不证明实际生成，更不证明越权。
_GENERATOR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("mesh_generator", r"\b(gmsh|snappyHexMesh|blockMesh|cfMesh|cartesianMesh|"
                       r"tetgen|netgen|triangle|cubit|coreform|salome|"
                       r"pointwise|icemcfd)\b"),
    ("wps_preprocessing", r"\b(geogrid|ungrib|metgrid|real)\.exe\b"),
    ("structure_builder", r"\b(atomsk|packmol|moltemplate|vaspkit|"
                          r"ase\.build|aseconvert|cif2cell|supercell|"
                          r"phonopy\s+(?:-d|--dim)|sumo-kgen)\b"),
    # 刻意只认**写/构造**调用，不认 `import ase` / `pymatgen` 这类裸引用：
    # experiment 本职就包含解析实验输出，拿 ase.io.read 读 OUTCAR、拿 pymatgen
    # 读 CONTCAR 做分析是它该做的事，把裸 import 当成前处理就是在误报。
    ("structure_api", r"\base\.io\.write\b|\bPoscar\s*\(|\bwrite_vasp\b|"
                      r"\bmake_supercell\b|\bStructure\.to\s*\(|"
                      r"\bwrite\(\s*['\"](?:POSCAR|KPOINTS|INCAR)"),
    ("dataset_split", r"\btrain_test_split\b|\bStratifiedKFold\b|"
                      r"\bsklearn\.model_selection\b"),
)

# 疑似正式输入文件名：仅作诊断线索。后缀类放宽到 basename 匹配。
_INPUT_FILE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("vasp_input", r"^(POSCAR|KPOINTS|INCAR|POTCAR)(\.[\w.-]+)?$"),
    # `data.<system>` 是 LAMMPS 的写法，但 `data.csv` / `results.data` 这类分析
    # 输出撞得太狠 —— 只留碰撞面小的 `in.<case>` 和 `.lmp`。
    ("lammps_input", r"^in\.[\w-]+$|\.lmp$"),
    ("wps_input", r"^namelist\.wps$|^(geo_em|met_em|FILE)[\w.:-]*$"),
    ("mesh_file", r"\.(msh|mesh|cgns|unv|stl|neu|cas|grid|geo)$"),
    ("structure_file", r"\.(cif|xyz|vasp|pdb|gro|lmpdat|xsf|poscar)$"),
    ("qe_input", r"\.(pwi|scf\.in|nscf\.in)$"),
)

# 只探测不生成 —— 命令里出现生成器名字，但显然没在造东西。
# 刻意不收 `-v` / `-h`：`gmsh -3 -v 5 model.geo` 里的 `-v` 是 verbosity，
# 收进来就会把一次真实的网格生成当成探测放掉。
_PROBE_ONLY = re.compile(
    r"^\s*(which|type|command\s+-v|whereis)\b"
    r"|(?:--|-)version\b|--help\b")

# shell 段分隔：白名单必须按段判，否则一句 `--version` 能把同一行后面的真生成
# 一起放行（自审实测）。
_SHELL_SEGMENT = re.compile(r"&&|\|\||[;|&\n]")

_COMPILED_GENERATORS = tuple((label, re.compile(pattern, re.I))
                             for label, pattern in _GENERATOR_PATTERNS)
_COMPILED_INPUT_FILES = tuple((label, re.compile(pattern, re.I))
                              for label, pattern in _INPUT_FILE_PATTERNS)

# python 里"写文件"的形态：open(path, 'w'/'a'/'x') 与常见 write/dump 调用。
_PY_WRITE = re.compile(
    r"open\(\s*['\"]([^'\"]{1,120})['\"]\s*,\s*['\"][wax]"
    r"|(?:write_text|write_bytes|savetxt|to_csv|to_file|writelines|write)"
    r"\(\s*['\"]([^'\"]{1,120})['\"]")

# shell 里"写文件"的形态：重定向、tee、heredoc。单纯读 POSCAR 不算生成。
_SHELL_WRITE = re.compile(
    r">{1,2}\s*(?P<redirect>[^\s;&|<>]+)"
    r"|\btee\b\s+(?:-a\s+)?(?P<tee>[^\s;&|<>]+)"
    r"|<<-?\s*['\"]?\w+['\"]?\s*>\s*(?P<heredoc>[^\s;&|<>]+)")


def _iter_events(state: Any) -> list[dict[str, Any]]:
    path = getattr(state, "transcript_path", None)
    if not path or not Path(path).exists():
        return []
    events: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _match_input_file(name: str) -> str | None:
    base = Path(str(name).strip().strip("'\"")).name
    if not base:
        return None
    for label, pattern in _COMPILED_INPUT_FILES:
        if pattern.search(base):
            return label
    return None


def _shell_write_targets(cmd: str) -> list[str]:
    targets: list[str] = []
    for match in _SHELL_WRITE.finditer(cmd):
        for value in match.groupdict().values():
            if value:
                targets.append(value)
    return targets


def _python_write_targets(code: str) -> list[str]:
    """python 代码里真正被**写**的文件名（读不算）。"""
    targets: list[str] = []
    for match in _PY_WRITE.finditer(code):
        for value in match.groups():
            if value:
                targets.append(value)
    return targets


def _run_accounting(state: Any) -> list[str]:
    """本 run 级别的对账理由（口径见模块 docstring 第 2 层）。"""
    reasons: list[str] = []
    try:
        try:
            from .input_delivery import active_input_delivery_entries
        except ImportError:
            from tools.input_delivery import active_input_delivery_entries
        deliveries = {
            spec_id: entry.get("delivery") or {}
            for spec_id, entry in active_input_delivery_entries(state).items()
            if isinstance(entry, dict)
        }
    except Exception:
        # 观察层不能因账本不可读而崩溃，也不能把不可读误算成已有正规交付。
        deliveries = {}
    for item in deliveries.values():
        if not isinstance(item, dict):
            continue
        if item.get("verified"):
            if item.get("provider") == "experiment_fallback":
                if item.get("fallback_authorized") is True:
                    reasons.append("verified_experiment_fallback")
            else:
                reasons.append("verified_data_delivery")
    try:
        try:
            from .run_contract import load_execution_mode_view
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.run_contract import load_execution_mode_view
        mode = load_execution_mode_view(state).get("mode")
    except Exception:
        mode = None
    if mode == "operational":
        reasons.append("operation_scope")
    return sorted(set(reasons))


def _log_declares_preprocessing_needed(state: Any) -> bool:
    try:
        summaries = state.list_artifacts("experiment_log") or []
    except Exception:
        return False
    for summary in reversed(summaries):
        try:
            record = state.read_artifact(str(summary.get("id") or ""))
        except Exception:
            continue
        content = (record or {}).get("content") if isinstance(record, dict) else None
        if isinstance(content, str) and re.search(
                r"^#{1,6}\s*Preprocessing\s+Needed\b", content, re.I | re.M):
            return True
    return False


def scan_preprocessing_boundary(state: Any) -> dict[str, Any]:
    """机械扫描本 run 的前处理产物生成行为，返回可直接落 transcript 的结论。"""
    events = _iter_events(state)
    n_scanned = 0
    hits: list[dict[str, Any]] = []
    whitelisted: list[dict[str, Any]] = []

    for event in events:
        if event.get("event") != "tool_call":
            continue
        name = event.get("name")
        args = event.get("args") or {}
        if not isinstance(args, dict):
            continue
        if name in WRITE_TOOL_NAMES:
            n_scanned += 1
            label = _match_input_file(args.get("path") or "")
            if label:
                _classify(hits, whitelisted, tool=str(name), label=label,
                          evidence=str(args.get("path"))[:200], command="")
            continue
        if name not in (BASH_TOOL_NAMES | PY_TOOL_NAMES):
            continue
        text = args.get("command") or args.get("code") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        n_scanned += 1
        # 按段扫，且**每个**命中的段都要各自判定 —— 只取第一个匹配段就会被
        # `gmsh --version && gmsh -3 …` 的前半句带偏：探测段先命中、直接 break，
        # 后面那句真生成就再也看不到了（自审实测）。一条命令里既探测又生成，
        # 本来就是两件事，分别记账才对。
        segments = [seg for seg in _SHELL_SEGMENT.split(text) if seg.strip()] or [text]
        for label, pattern in _COMPILED_GENERATORS:
            matching = [seg for seg in segments if pattern.search(seg)]
            if not matching:
                continue
            for segment in matching:
                _classify(hits, whitelisted, tool=str(name), label=label,
                          evidence=segment.strip()[:200], command=segment)
            break
        # 命令/代码自己写正式输入文件。两边都只认**写**：shell 看重定向 / tee /
        # heredoc，python 看 open(..., 'w') 和各种 write/savetxt 调用。
        # 之前 python 侧是"扫出所有引号字符串"，于是 `open('POSCAR')` 这种纯读
        # 也被记成写（自审实测）—— 跟本模块"只认生成不认取得"的口径自相矛盾。
        candidates = (_shell_write_targets(text) if name in BASH_TOOL_NAMES
                      else _python_write_targets(text))
        for candidate in candidates:
            label = _match_input_file(candidate)
            if label:
                _classify(hits, whitelisted, tool=str(name),
                          label=f"{label}_write", evidence=candidate[:200],
                          command=text)
                break

    accounting = _run_accounting(state)
    observations = (
        ["preprocessing_needed_declared"]
        if _log_declares_preprocessing_needed(state) else []
    )
    n_unaccounted = 0 if accounting else len(hits)
    return {
        "verdict": "OBSERVATION",
        "n_scanned": n_scanned,
        "n_hits": len(hits),
        "n_whitelisted": len(whitelisted),
        "n_unaccounted": n_unaccounted,
        "accounting_reasons": accounting,
        "observations": observations,
        "hits": hits[:20],
        "whitelisted": whitelisted[:20],
    }


def _classify(hits: list[dict[str, Any]], whitelisted: list[dict[str, Any]], *,
              tool: str, label: str, evidence: str, command: str) -> None:
    reason = _whitelist_reason(command=command)
    entry = {"tool": tool, "label": label, "evidence": evidence}
    if reason:
        entry["whitelist_reason"] = reason
        whitelisted.append(entry)
    else:
        hits.append(entry)


def _whitelist_reason(*, command: str) -> str | None:
    if command and _PROBE_ONLY.search(command):
        return "probe_only"
    return None

"""构建前源码侦察（第2步主任务，唯一新文件）。

通用、确定性、有限扫描——扫源码树识别官方构建系统/文档/CI/容器/externals/配方线索，
生成【粗粒度路线候选 + 证据】，不自行决定最终路线、不联网、不执行构建、不改源码。

设计边界（来自用户规格）：
- 不为单个应用写专属规则；只识别跨项目可复用的构建、依赖和配方证据。
- 只产候选与证据，不生成"必须用某路线"的强结论（兼容性判断留给第3步 gate / LLM）。
- 有限扫描：默认最大深度/文件数，跳过 .git/build/install/third_party 等，只读文本前部，
  超预算 scan_incomplete=true。

用法（execute_python 调）：
    from tools.source_recon import scan_source, build_route_decision_input
    recon = scan_source("~/project-source")
    rdi = build_route_decision_input(platform_profile, recon)
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"

# 扫描时跳过的目录（大目录/产物/第三方/版本控制内部）
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "build", "_build", "install", "third_party",
    "third-party", "node_modules", ".cache", "__pycache__", ".worktrees",
    "externals_clone", "cime_build", "bld", "CMakeFiles",
}
_DEFAULT_MAX_DEPTH = 4
_DEFAULT_MAX_FILES = 3000
_TEXT_HEAD_BYTES = 8192        # 只读文本前 8KB
_MAX_BIG_FILE = 2_000_000      # 跳过 >2MB 文件

# 构建系统标志文件 → 系统名
_BUILD_SYSTEM_MARKERS = {
    "CMakeLists.txt": "cmake",
    "configure": "autotools",
    "configure.ac": "autotools",
    "Makefile": "make",
    "makefile": "make",
    "GNUmakefile": "make",
    "meson.build": "meson",
    "pyproject.toml": "python",
    "setup.py": "python",
    "SConstruct": "scons",
}
# 项目自带依赖管理 / externals 标志
_PROJECT_MGR_MARKERS = {
    "manage_externals": "external_manager",
    "Externals.cfg": "external_manager",
    ".gitmodules": "git_submodules",
}
_PACKAGE_RECIPE_MARKERS = {
    "package.py": "spack",
    "meta.yaml": "conda",
    "environment.yml": "conda_env",
}
_DOC_PAT = re.compile(r"^(README|INSTALL|BUILD|CONTRIBUTING)", re.IGNORECASE)
_DOC_BUILD_PAT = re.compile(r"(build|install|compile)", re.IGNORECASE)
_ORCHESTRATOR_DIR_RE = re.compile(
    r"(?:^|[_-])(?:config|configs|workflow|workflows|orchestrat(?:or|ion)|case|cases)$",
    re.IGNORECASE,
)
_ORCHESTRATOR_SCRIPT_RE = re.compile(
    r"^(?:create|setup|configure|bootstrap|manage|build)[_-].*\.(?:py|sh)$",
    re.IGNORECASE,
)


def _rel(base: Path, p: Path) -> str:
    try:
        return str(p.relative_to(base))
    except ValueError:
        return str(p)


def _read_head(p: Path) -> str:
    try:
        if p.stat().st_size > _MAX_BIG_FILE:
            return ""
        with open(p, "rb") as f:
            return f.read(_TEXT_HEAD_BYTES).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _git_info(base: Path) -> dict:
    import subprocess
    def g(args):
        try:
            return subprocess.run(["git", "-C", str(base)] + args, capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    commit = g(["rev-parse", "HEAD"])
    if not commit:
        return {}
    return {"commit": commit[:40], "branch": g(["rev-parse", "--abbrev-ref", "HEAD"]),
            "dirty": bool(g(["status", "--porcelain"]))}


def _classify_ci(content: str) -> str:
    """CI workflow 分类（不因出现 make/cmake 就当完整构建）。"""
    c = content.lower()
    has_build = bool(re.search(r"\b(cmake|make|configure|meson|ninja)\b", c))
    if re.search(r"(clang-format|black|flake8|lint|markdownlint|gh-pages|"
                 r"docs|doxygen|spellcheck|pre-commit)", c) and not has_build:
        return "lint_format_docs"
    if re.search(r"(docker|singularity|apptainer|podman)\s", c):
        return "container_delegated"
    if re.search(r"(ctest|pytest|\btest\b|check)", c) and not re.search(r"(cmake\s|make\s+-|configure)", c):
        return "test_only"
    if re.search(r"(bash\s+\S+\.sh|\./\S+\.sh|source\s+\S+)", c) and not has_build:
        return "external_script_delegated"
    if has_build:
        # 完整 vs 单组件：含组件/子目录限定词 → component
        if re.search(r"(component|subdir|--target\s+\w|only)", c):
            return "component_build"
        return "full_build"
    return "unknown"


def scan_source(source_path: str, application_name: str | None = None,
                max_depth: int = _DEFAULT_MAX_DEPTH,
                max_files: int = _DEFAULT_MAX_FILES) -> dict[str, Any]:
    """扫源码树，返回结构化侦察 artifact（见模块 docstring 的 YAML 结构）。"""
    base = Path(os.path.expanduser(source_path)).resolve()
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_path": str(base),
        "application_name": application_name,
        "git": {}, "build_systems": [], "official_docs": [],
        "official_build_entrypoints": [], "ci": [], "containers": [],
        "externals": [], "submodules": [], "package_recipes": [],
        "build_orchestrator_hints": [],
        "route_candidates": [], "route_evidence": [], "unresolved_questions": [],
        "scan_limits": {"max_depth": max_depth, "max_files": max_files,
                        "scan_incomplete": False, "files_seen": 0, "dirs_skipped": []},
    }
    if not base.exists():
        out["unresolved_questions"].append(f"source_path 不存在: {base}")
        return out
    out["git"] = _git_info(base)

    build_systems: set[str] = set()
    files_seen = 0
    incomplete = False
    for root, dirs, files in os.walk(base):
        rootp = Path(root)
        depth = len(rootp.relative_to(base).parts)
        if depth >= max_depth:
            dirs[:] = []
        # 跳过大目录/产物
        skipped = [d for d in dirs if d in _SKIP_DIRS]
        for d in skipped:
            out["scan_limits"]["dirs_skipped"].append(_rel(base, rootp / d))
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]

        # 目录级 externals 标志。只在浅层（depth<=2）查；os.walk top-down 保证顶层先扫到，
        # 即使后续超文件预算也已识别。
        if depth <= 2:
            for d in dirs:
                if d == "externals":
                    out["externals"].append({"file": _rel(base, rootp / d), "kind": "externals_dir"})
                elif _ORCHESTRATOR_DIR_RE.search(d):
                    out["build_orchestrator_hints"].append({
                        "file": _rel(base, rootp / d), "kind": "configuration_dir",
                    })

        for fn in files:
            files_seen += 1
            if files_seen > max_files:
                incomplete = True
                break
            fp = rootp / fn
            rel = _rel(base, fp)
            # 构建系统标志
            if fn in _BUILD_SYSTEM_MARKERS:
                build_systems.add(_BUILD_SYSTEM_MARKERS[fn])
                if depth == 0:   # 仅项目根的构建入口算 official
                    out["official_build_entrypoints"].append(
                        {"file": rel, "system": _BUILD_SYSTEM_MARKERS[fn]})
            # 项目管理 / externals
            if fn in _PROJECT_MGR_MARKERS:
                kind = _PROJECT_MGR_MARKERS[fn]
                if kind == "git_submodules":
                    out["submodules"].append(rel)
                elif kind == "external_manager":
                    out["externals"].append({"file": rel, "kind": "external_manager"})
                else:
                    out["externals"].append({"file": rel, "kind": kind})
            if depth <= 2 and _ORCHESTRATOR_SCRIPT_RE.match(fn):
                out["build_orchestrator_hints"].append({
                    "file": rel, "kind": "orchestration_script",
                })
            # 官方文档
            if _DOC_PAT.match(fn) or (_DOC_BUILD_PAT.search(fn) and fn.lower().endswith((".md", ".rst", ".txt"))):
                out["official_docs"].append(rel)
            # 容器
            if fn.startswith("Dockerfile") or fn.endswith((".def",)) or "singularity" in fn.lower():
                out["containers"].append(rel)
            # 配方
            if fn in _PACKAGE_RECIPE_MARKERS:
                out["package_recipes"].append({"file": rel, "kind": _PACKAGE_RECIPE_MARKERS[fn]})
            elif fn.endswith(".eb"):
                out["package_recipes"].append({"file": rel, "kind": "easybuild"})
            elif re.match(r"requirements.*\.txt", fn):
                out["package_recipes"].append({"file": rel, "kind": "pip_requirements"})
            # CI
            if "/.github/workflows/" in ("/" + rel.replace(os.sep, "/")) and fn.endswith((".yml", ".yaml")):
                out["ci"].append({"file": rel, "type": _classify_ci(_read_head(fp))})
        # .gitlab-ci.yml / Jenkinsfile（按文件名，本层目录）
        for special in (".gitlab-ci.yml", "Jenkinsfile"):
            sp = rootp / special
            if sp.exists():
                out["ci"].append({"file": _rel(base, sp), "type": _classify_ci(_read_head(sp))})
        if incomplete:
            break

    out["build_systems"] = sorted(build_systems)
    out["scan_limits"]["files_seen"] = files_seen
    out["scan_limits"]["scan_incomplete"] = incomplete
    _build_route_candidates(out)
    return out


def _build_route_candidates(out: dict) -> None:
    """基于已发现证据生成粗粒度路线候选（只产候选+证据，不下强结论）。"""
    cands = []
    ev = out["route_evidence"]
    # official_build_system：有项目自带构建编排器或 external_manager
    orch = [e for e in out["externals"] if e.get("kind") in ("project_build_orchestrator", "external_manager")]
    if orch:
        ev.append(f"发现项目自带构建管理/外部依赖系统: {[e['file'] for e in orch]}")
        cands.append({"route": "official_build_system", "confidence": "medium",
                      "evidence": [e["file"] for e in orch],
                      "entrypoints": [e["file"] for e in out["official_build_entrypoints"]],
                      "limitations": "需确认其支持的 compiler/MPI 与平台匹配",
                      "required_tools": ["python", "git"]})
    hints = out.get("build_orchestrator_hints") or []
    hint_kinds = {item.get("kind") for item in hints}
    if {"configuration_dir", "orchestration_script"} <= hint_kinds:
        evidence = [item["file"] for item in hints]
        ev.append(f"发现项目配置目录与编排脚本: {evidence}")
        cands.append({
            "route": "official_build_system",
            "confidence": "low",
            "evidence": evidence,
            "entrypoints": [
                item["file"] for item in hints
                if item.get("kind") == "orchestration_script"
            ],
            "limitations": "这是通用启发式；必须阅读项目官方构建文档确认入口与参数",
            "required_tools": ["project_documentation"],
        })
    # project_native_manual：顶层有 cmake/autotools/make 入口
    if out["official_build_entrypoints"]:
        systems = sorted({e["system"] for e in out["official_build_entrypoints"]})
        ev.append(f"顶层构建入口: {systems}")
        cands.append({"route": "project_native_manual", "confidence": "medium",
                      "evidence": [e["file"] for e in out["official_build_entrypoints"]],
                      "entrypoints": [e["file"] for e in out["official_build_entrypoints"]],
                      "limitations": "手动构建需自行管理依赖顺序",
                      "required_tools": systems})
    # package_manager
    if out["package_recipes"]:
        kinds = sorted({r["kind"] for r in out["package_recipes"]})
        ev.append(f"发现配方: {kinds}")
        cands.append({"route": "package_manager", "confidence": "low",
                      "evidence": [r["file"] for r in out["package_recipes"]],
                      "entrypoints": [r["file"] for r in out["package_recipes"]],
                      "limitations": "配方对当前平台/变体是否适用需核验",
                      "required_tools": kinds})
    # container
    if out["containers"]:
        ev.append(f"发现容器定义: {out['containers']}")
        cands.append({"route": "container", "confidence": "low",
                      "evidence": out["containers"], "entrypoints": out["containers"],
                      "limitations": "容器路线是否被允许需用户确认",
                      "required_tools": ["docker_or_singularity"]})
    if not cands:
        cands.append({"route": "unknown", "confidence": "low", "evidence": [],
                      "entrypoints": [], "limitations": "未发现明确构建入口，需读文档",
                      "required_tools": []})
    out["route_candidates"] = cands


def build_route_decision_input(platform_profile: dict, source_recon: dict) -> dict[str, Any]:
    """合并 platform_profile + source_recon → 轻量路线决策输入（先列问题，不打分）。"""
    pf = platform_profile or {}
    sr = source_recon or {}
    questions: list[str] = []
    blocking: list[str] = []

    # 官方路线要求的 MPI/compiler 与当前是否匹配（只提问，不判定）
    if any(c["route"] in ("official_build_system", "project_native_manual")
           for c in sr.get("route_candidates", [])):
        questions.append("官方路线要求的 MPI 与当前 wrapper 是否同族？(见 platform_facts.mpi_wrappers)")
        questions.append("官方路线是否支持选定 compiler？")
    # 并行 IO
    if pf.get("netcdf"):
        questions.append("目标是否需要并行 IO（parallel HDF5 / PnetCDF）？若需，"
                         "确认 nc/nf/HDF5 并行能力一致")
    # 容器
    if any(c["route"] == "container" for c in sr.get("route_candidates", [])):
        questions.append("容器路线是否被允许？")
    # externals/submodules 完整性
    if sr.get("externals") or sr.get("submodules"):
        questions.append("submodule/externals 是否已完整拉取？")
        if sr.get("git", {}).get("dirty"):
            blocking.append("源码树 dirty，可能含未提交改动，影响复现")
    if sr.get("scan_limits", {}).get("scan_incomplete"):
        blocking.append("源码侦察未扫完（超文件预算），路线证据可能不全")

    return {
        "platform_facts": {
            "mpi_wrappers": pf.get("mpi_wrappers"), "netcdf": pf.get("netcdf"),
            "hdf5": pf.get("hdf5"), "toolchain": list((pf.get("toolchain") or {}).keys()),
            "risk_signals": pf.get("unresolved_risks"),
        },
        "source_facts": {
            "build_systems": sr.get("build_systems"), "externals": sr.get("externals"),
            "official_build_entrypoints": sr.get("official_build_entrypoints"),
            "ci_types": sorted({c["type"] for c in sr.get("ci", [])}),
        },
        "route_candidates": sr.get("route_candidates"),
        "compatibility_questions": questions,   # 先列问题，不过早打分
        "blocking_unknowns": blocking,
    }

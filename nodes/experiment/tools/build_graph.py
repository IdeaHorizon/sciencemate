"""Generic build graph extraction for HPC experiments.

The graph has two sources:
* build-system target dependencies (CMake graphviz, generated Makefile/mkmf);
* low-confidence orchestration edges from scripts/CI/recipes/docs.

No application-specific rules live here.  Link-failure backfill uses generic
missing-module/library symptoms to promote an observed dependency edge.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

try:
    from .path_roles import sandbox_mount_roots
except ImportError:
    from tools.path_roles import sandbox_mount_roots

SCHEMA_VERSION = "1.0"


def _q(path: str | Path) -> str:
    return shlex.quote(str(path))


def _run(cmd: str, *, state: Any, cwd: str, timeout: int = 20) -> tuple[int | None, str]:
    env_path = os.environ.get("EXPERIMENT_ACTIVE_BUILD_ENV")
    if env_path:
        try:
            from tools.env_provision import wrap_command
        except Exception:
            try:
                from .env_provision import wrap_command
            except Exception:
                wrap_command = None
        if wrap_command is not None:
            cmd = wrap_command(cmd, env_path)
    from core.sandbox import SandboxLimits, prepare_attempt_command

    launch = None
    try:
        writable, readonly = sandbox_mount_roots(state)
        command_cwd = Path(cwd).resolve(strict=True)
        if not any(command_cwd == root or command_cwd.is_relative_to(root)
                   for root in [*writable, *readonly]):
            readonly = [*readonly, command_cwd]
        from shared.lib.shell import bash_shell

        launch = prepare_attempt_command(
            [bash_shell(), "-o", "pipefail", "-c", cmd],
            state=state,
            cwd=command_cwd,
            writable_roots=writable,
            readonly_roots=readonly,
            limits=SandboxLimits(
                memory_bytes=2 * 1024**3,
                cpus=1,
                pids=64,
                walltime_seconds=timeout,
                storage_bytes=2 * 1024**3,
                output_bytes=8 * 1024**2,
            ),
        )
        # env=launch.env 不是可选项：这里跑的是 `make -pn` / `cmake --graphviz`，
        # 它们会真的求值项目自带的 Makefile 与 CMakeLists（`$(shell ...)`、
        # execute_process），也就是**外部源码**。后端交出的 launch.env 是宿主环境
        # 减去凭据（core.isolation._native.payload_environment，与 core.secrets
        # 同源判据）；不传它，子进程拿到的就是 harness 的原始环境。
        # 2026-09-08 实测：不传 env 时 payload 打印出 ANTHROPIC_API_KEY 的真值。
        # cwd 不必传 —— core.sandbox._native_launch 已把 `cd` 编进 argv。
        r = subprocess.run(
            launch.argv,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            stdin=subprocess.DEVNULL,
            env=getattr(launch, "env", None),
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        if launch is not None:
            launch.terminate()
        return None, "timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        if launch is not None:
            launch.cleanup()


def _clean_node(name: str) -> str:
    n = re.sub(r"\s+", "_", (name or "").strip().strip("\"'"))
    n = re.sub(r"[^A-Za-z0-9_+.\-]", "_", n)
    return n[:120] or "unknown"


def _empty(status: str, source_root: str | None, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "actionable": False,
        "actionability_reason": reason,
        "source_root": source_root,
        "nodes": {},
        "edges": [],
        "diagnostics": [reason],
    }


def _detect_paths(source_root: str, build_root: str | None = None) -> tuple[Path, Path]:
    src = Path(os.path.expanduser(source_root)).resolve()
    bld = Path(os.path.expanduser(build_root)).resolve() if build_root else src
    return src, bld


def _parse_makefile_text(text: str, base: Path | None = None) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for raw in text.splitlines():
        if not raw or raw.startswith(("\t", "#", " ", ".")):
            continue
        if ":=" in raw or "+=" in raw or "?=" in raw:
            continue
        if ":" not in raw:
            continue
        lhs, rhs = raw.split(":", 1)
        if not lhs or re.search(r"[%$(){}]", lhs):
            continue
        targets = [x for x in re.split(r"\s+", lhs.strip()) if x]
        prereqs = [x for x in re.split(r"\s+", rhs.split("#", 1)[0].strip()) if x]
        if not targets:
            continue
        for target in targets[:4]:
            if len(target) > 160 or target.startswith("-"):
                continue
            tid = _clean_node(target)
            deps = [_clean_node(x) for x in prereqs[:80]
                    if x and not x.startswith(("-", "|")) and "$" not in x]
            outputs = []
            if base and (target.endswith((".a", ".so", ".mod")) or "/" in target):
                p = Path(target)
                outputs.append(str((base / p).resolve() if not p.is_absolute() else p))
            nodes.setdefault(tid, {"id": tid, "outputs": outputs, "deps": [], "source": "makefile"})
            for dep in deps:
                if dep != tid and dep not in nodes[tid]["deps"]:
                    nodes[tid]["deps"].append(dep)
                    nodes.setdefault(dep, {"id": dep, "outputs": [], "deps": [], "source": "makefile"})
    return nodes


_UTILITY_TARGET_RE = re.compile(
    r"^(?:"
    r"all|default|force|phony|"
    r"clean(?:[._-].*)?|distclean|realclean|clobber|"
    r"run(?:[._-].*)?|test(?:[._-].*)?|check(?:[._-].*)?|"
    r"install(?:[._-].*)?|uninstall|help|print.*|list.*"
    r")$",
    re.I,
)


def _make_graph_has_actionable_targets(nodes: dict[str, dict[str, Any]]) -> bool:
    """Return true only if a Make graph looks like a real build DAG.

    Recursive HPC build systems often expose only a top-level shell of phony
    targets (`all`, `run`, `test`, `clean`, `FORCE`). Treating that as an
    extracted DAG is worse than having no DAG: the gate believes dependency
    ordering is known, while component edges such as library -> executable are
    absent. This check stays generic and only asks whether the graph contains
    material target/output signals.
    """
    for nid, node in nodes.items():
        raw = str(node.get("id") or nid)
        low = raw.lower()
        if _UTILITY_TARGET_RE.match(low):
            continue
        if node.get("outputs"):
            return True
        if re.search(r"\.(?:a|so|dylib|dll|exe|o|obj|mod|bin)$", low):
            return True
        if "/" in raw and not low.endswith((".mk", ".inc", ".h", ".hpp")):
            return True
        deps = [str(d).lower() for d in node.get("deps") or []]
        if any(re.search(r"\.(?:a|so|dylib|dll|o|obj|mod|f90|f|c|cc|cpp)$", d) for d in deps):
            return True
    return False


def _actionability(nodes: dict[str, dict[str, Any]],
                   edges: list[dict[str, Any]],
                   source: str) -> tuple[bool, str]:
    """Whether this DAG is safe to use for hard prerequisite gating.

    "Extracted" is not enough: recursive Make/mkmf projects often expose only
    phony top-level targets. Hard gating is only enabled for target graphs with
    real dependency edges and material target/output signals, or for CMake's
    configured target graph where target edges are first-class build-system
    facts. Low-confidence orchestration hints stay advisory.
    """
    if not edges:
        return False, "no dependency edges"
    target_edges = [e for e in edges if e.get("kind") == "target" and e.get("confidence") == "high"]
    if not target_edges:
        return False, "no high-confidence build-system target edges"
    if source == "cmake_graphviz":
        return True, "configured CMake graphviz target graph"
    if source == "makefile":
        if _make_graph_has_actionable_targets(nodes):
            return True, "Makefile graph has material targets/outputs"
        return False, "Makefile graph lacks material targets/outputs"
    if source == "build_system_plus_orchestration":
        if _make_graph_has_actionable_targets(nodes):
            return True, "merged graph has material build-system targets"
        return False, "merged graph only has advisory orchestration/phony targets"
    return False, f"unsupported graph source for hard gating: {source}"


def _extract_make_graph(source_root: str, *, state: Any,
                        build_root: str | None = None) -> dict[str, Any]:
    src, bld = _detect_paths(source_root, build_root)
    candidates = [bld / "Makefile", bld / "makefile", bld / "GNUmakefile",
                  src / "Makefile", src / "makefile", src / "GNUmakefile"]
    nodes: dict[str, dict[str, Any]] = {}
    source_files: list[str] = []
    for mf in candidates:
        if not mf.is_file():
            continue
        try:
            text = mf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        source_files.append(str(mf))
        nodes.update(_parse_makefile_text(text, mf.parent))
    if not nodes and any(p.is_file() for p in candidates):
        rc, out = _run("make -pn", state=state, cwd=str(bld), timeout=30)
        if rc == 0 and out:
            nodes.update(_parse_makefile_text(out, bld))
            source_files.append("make -pn")
    if not nodes:
        return _empty("unknown", str(src), "no Makefile graph extracted")
    if not _make_graph_has_actionable_targets(nodes):
        return _empty(
            "unknown",
            str(src),
            "Makefile graph contains only utility/phony targets; component DAG not materialized",
        )
    return _graph_from_nodes(nodes, str(src), "makefile", source_files)


def _parse_cmake_dot(dot_text: str) -> dict[str, dict[str, Any]]:
    labels: dict[str, str] = {}
    nodes: dict[str, dict[str, Any]] = {}
    for m in re.finditer(r'"([^"]+)"\s+\[.*?label\s*=\s*"([^"]+)"', dot_text):
        labels[m.group(1)] = _clean_node(m.group(2).split("\\n", 1)[0])
    for m in re.finditer(r'"([^"]+)"\s*->\s*"([^"]+)"', dot_text):
        a = labels.get(m.group(1), _clean_node(m.group(1)))
        b = labels.get(m.group(2), _clean_node(m.group(2)))
        nodes.setdefault(a, {"id": a, "outputs": [], "deps": [], "source": "cmake_graphviz"})
        nodes.setdefault(b, {"id": b, "outputs": [], "deps": [], "source": "cmake_graphviz"})
        if b not in nodes[a]["deps"]:
            nodes[a]["deps"].append(b)
    return nodes


def _extract_cmake_graph(source_root: str, *, state: Any,
                         build_root: str | None = None) -> dict[str, Any]:
    src, bld = _detect_paths(source_root, build_root)
    if not (bld / "CMakeCache.txt").is_file():
        return _empty("partial", str(src), "cmake graph needs an already configured build directory")
    dot_dir = Path(state.root) / ".harness" / "build_graph"
    dot_dir.mkdir(parents=True, exist_ok=True)
    dot = dot_dir / "targets.dot"
    dot.unlink(missing_ok=True)
    rc, out = _run(
        f"cmake --graphviz={_q(dot)} .", state=state, cwd=str(bld), timeout=30)
    if rc != 0 or not dot.is_file():
        return _empty("partial", str(src), f"cmake graphviz failed: {out[:300]}")
    try:
        nodes = _parse_cmake_dot(dot.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return _empty("partial", str(src), f"cmake dot parse failed: {type(e).__name__}: {e}")
    finally:
        dot.unlink(missing_ok=True)
    if not nodes:
        return _empty("partial", str(src), "cmake graphviz produced no target edges")
    return _graph_from_nodes(nodes, str(src), "cmake_graphviz", [str(dot)])


def _component_mentions(text: str) -> list[str]:
    names: list[str] = []
    for token in re.findall(r"\b[A-Za-z][A-Za-z0-9_+\-.]{1,40}\b", text or ""):
        low = token.lower()
        if low in {
            "make", "cmake", "ninja", "build", "install", "test", "source", "module",
            "python", "bash", "sh", "cd", "true", "false", "echo", "then", "else",
        }:
            continue
        if any(ch.isdigit() for ch in token) and len(token) <= 3:
            continue
        names.append(_clean_node(token))
    out: list[str] = []
    for n in names:
        if n not in out:
            out.append(n)
    return out


def _orchestration_edges_from_file(path: Path, rel: str) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:12000]
    except Exception:
        return []
    lines = text.splitlines()
    build_lines: list[tuple[int, str, list[str]]] = []
    for i, line in enumerate(lines, 1):
        if not re.search(r"\b(make|cmake|ninja|mkmf|build|compile)\b", line, re.I):
            continue
        names = _component_mentions(line)
        if names:
            build_lines.append((i, line.strip()[:240], names[:6]))
    edges: list[dict[str, Any]] = []
    for prev, cur in zip(build_lines, build_lines[1:]):
        for dep in prev[2]:
            for dst in cur[2]:
                if dep == dst:
                    continue
                edges.append({
                    "from": dep,
                    "to": dst,
                    "kind": "orchestration",
                    "confidence": "low",
                    "evidence": f"{rel}:{prev[0]} -> {cur[0]}",
                    "detail": f"{prev[1]} THEN {cur[1]}",
                })
    return edges[:80]


def orchestration_edges(source_root: str, source_recon: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    src = Path(os.path.expanduser(source_root)).resolve()
    if not src.is_dir():
        return []
    rels: list[str] = []
    sr = source_recon or {}
    for bucket in ("ci", "package_recipes"):
        for item in sr.get(bucket) or []:
            if isinstance(item, dict) and item.get("file"):
                rels.append(str(item["file"]))
    for rel in sr.get("official_docs") or []:
        rels.append(str(rel))
    # Common build scripts near the root, generic names only.
    for pat in ("build*.sh", "compile*.sh", "install*.sh", "*.mk"):
        for p in src.glob(pat):
            rels.append(str(p.relative_to(src)))
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for rel in rels[:80]:
        p = (src / rel).resolve()
        if not p.is_file() or src not in p.parents:
            continue
        for edge in _orchestration_edges_from_file(p, rel):
            key = (edge["from"], edge["to"], edge["evidence"])
            if key not in seen:
                seen.add(key)
                edges.append(edge)
    return edges[:200]


def confirmed_edges_from_failure(log_text: str) -> list[dict[str, Any]]:
    """Infer confirmed dependency edges from generic missing module/library errors."""
    text = log_text or ""
    edges: list[dict[str, Any]] = []
    patterns = [
        (r"cannot\s+(?:open|find)\s+module\s+file\s+'?([A-Za-z][\w+.\-]*)\.mod", "module"),
        (r"fatal\s+error:\s+([A-Za-z][\w+.\-]*)\.mod:\s+No such file", "module"),
        (r"cannot\s+find\s+-l([A-Za-z][\w+.\-]*)", "library"),
        (r"cannot\s+find\s+(?:lib)?([A-Za-z][\w+.\-]*)\.(?:a|so)", "library"),
        (r"undefined reference to [`']([A-Za-z][\w+.\-]*)", "symbol"),
    ]
    consumer = None
    m_cons = re.search(r"(?:linking|building|target)\s+([A-Za-z][\w+.\-]*)", text, re.I)
    if m_cons:
        consumer = _clean_node(m_cons.group(1))
    for pat, kind in patterns:
        for m in re.finditer(pat, text, re.I):
            dep = _clean_node(m.group(1))
            to = consumer or "current_target"
            if dep == to:
                continue
            edges.append({
                "from": dep,
                "to": to,
                "kind": f"confirmed_missing_{kind}",
                "confidence": "confirmed",
                "evidence": m.group(0)[:240],
            })
    return edges[:40]


def _graph_from_nodes(nodes: dict[str, dict[str, Any]], source_root: str,
                      source: str, source_files: list[str]) -> dict[str, Any]:
    edges = []
    for nid, node in nodes.items():
        for dep in node.get("deps") or []:
            edges.append({"from": dep, "to": nid, "kind": "target", "confidence": "high"})
    actionable, actionability_reason = _actionability(nodes, edges, source)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "extracted" if edges else "partial",
        "actionable": actionable,
        "actionability_reason": actionability_reason,
        "source": source,
        "source_root": source_root,
        "source_files": source_files,
        "nodes": nodes,
        "edges": edges,
        "diagnostics": [],
    }
    payload["dag_id"] = hashlib.sha256(
        json.dumps({"nodes": nodes, "edges": edges}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return payload


def extract_build_dag(source_root: str, *, state: Any,
                      build_root: str | None = None,
                      source_recon: dict[str, Any] | None = None) -> dict[str, Any]:
    src = Path(os.path.expanduser(source_root)).resolve()
    if not src.is_dir():
        return _empty("unknown", source_root, "source_root does not exist")
    graphs: list[dict[str, Any]] = []
    if (src / "CMakeLists.txt").is_file() or (build_root and (Path(build_root) / "CMakeCache.txt").is_file()):
        graphs.append(_extract_cmake_graph(str(src), state=state, build_root=build_root))
    graphs.append(_extract_make_graph(str(src), state=state, build_root=build_root))

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    source_files: list[str] = []
    status = "unknown"
    child_actionable = False
    child_actionability_reasons: list[str] = []
    for g in graphs:
        diagnostics.extend(g.get("diagnostics") or [])
        source_files.extend(g.get("source_files") or [])
        if g.get("nodes"):
            status = "extracted" if g.get("status") == "extracted" else "partial"
            if g.get("actionable"):
                child_actionable = True
                if g.get("actionability_reason"):
                    child_actionability_reasons.append(str(g.get("actionability_reason")))
            nodes.update(g.get("nodes") or {})
            edges.extend(g.get("edges") or [])

    orch = orchestration_edges(str(src), source_recon)
    for e in orch:
        nodes.setdefault(e["from"], {"id": e["from"], "outputs": [], "deps": [], "source": "orchestration"})
        nodes.setdefault(e["to"], {"id": e["to"], "outputs": [], "deps": [], "source": "orchestration"})
        if e["from"] not in nodes[e["to"]].setdefault("deps", []):
            nodes[e["to"]]["deps"].append(e["from"])
        edges.append(e)
    if orch and status == "unknown":
        status = "partial"
    if status == "unknown":
        return _empty("unknown", str(src), "; ".join(diagnostics[-4:]) or "no DAG sources available")
    actionable, actionability_reason = _actionability(nodes, edges, "build_system_plus_orchestration")
    if child_actionable:
        actionable = True
        actionability_reason = "; ".join(child_actionability_reasons[:3]) or actionability_reason
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "actionable": actionable,
        "actionability_reason": actionability_reason,
        "source": "build_system_plus_orchestration",
        "source_root": str(src),
        "build_root": str(Path(build_root).resolve()) if build_root else None,
        "source_files": source_files,
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
    }
    payload["dag_id"] = hashlib.sha256(
        json.dumps({"nodes": nodes, "edges": edges}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return payload

"""窄单 B #1217 判据 5：机械探针、修复提案表、权限快照只在 App Server（`platform/backend/app/services`）。

探针会执行 icacls / getfacl、修复会以用户身份改权限 —— 这是宿主的事，不是科研 loop 的事。
框架（`core/`）与节点（`nodes/`）里 import 它们任何一个，就是让 agent 那一侧够得着改宿主权限的路，红。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST_ONLY = ("environment_facts", "session_environment", "permissions_snapshot")
SCANNED = ("core", "nodes")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names += [base] + [f"{base}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
            "import_module", "__import__"
        ) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            names.append(node.args[0].value)
    return names


def _offenders(roots: list[Path]) -> list[str]:
    bad = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            for name in _imports(path):
                if any(part in HOST_ONLY for part in name.split(".")):
                    bad.append(f"{path.relative_to(ROOT)}: {name}")
    return bad


def test_core_and_nodes_never_reach_the_host_probes() -> None:
    roots = [ROOT / d for d in SCANNED]
    scanned = sum(1 for r in roots for _ in r.rglob("*.py"))
    assert scanned > 50, f"只扫到 {scanned} 个文件 —— 目录挪了，这道闸在空扫"
    assert _offenders(roots) == []


def test_the_gate_catches_an_import(tmp_path) -> None:
    (tmp_path / "nodes").mkdir()
    (tmp_path / "nodes" / "x.py").write_text("from app.services import environment_facts\n", encoding="utf-8")
    (tmp_path / "nodes" / "y.py").write_text("import importlib\nimportlib.import_module('app.services.session_environment')\n", encoding="utf-8")
    global ROOT
    saved, ROOT = ROOT, tmp_path
    try:
        assert len(_offenders([tmp_path / "nodes"])) == 2
    finally:
        ROOT = saved

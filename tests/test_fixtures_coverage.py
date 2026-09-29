"""每个节点都必须有可加载的 fixture（防漏）。

如果将来加新节点，没补 fixture 就会被这测试挡住。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
NODES_DIR = ROOT / "nodes"


def _node_dirs() -> list[Path]:
    out = []
    for p in sorted(NODES_DIR.iterdir()):
        if not p.is_dir() or p.name.startswith("__"):
            continue
        if (p / "harness.yaml").exists():
            out.append(p)
    return out


@pytest.mark.parametrize("node_dir", _node_dirs(), ids=lambda p: p.name)
def test_node_has_minimal_fixture(node_dir):
    fx = node_dir / "fixtures" / "minimal.yaml"
    assert fx.exists(), (
        f"节点 {node_dir.name} 缺 fixtures/minimal.yaml；"
        f"参考 nodes/literature/fixtures/minimal.yaml 写一份，或用 "
        f"`python scripts/fixture_from_run.py <run_id> -o {fx}` 从历史 run 生成。"
    )


@pytest.mark.parametrize("node_dir", _node_dirs(), ids=lambda p: p.name)
def test_fixture_parses_as_yaml(node_dir):
    fx = node_dir / "fixtures" / "minimal.yaml"
    if not fx.exists():
        pytest.skip("no fixture (caught by previous test)")
    data = yaml.safe_load(fx.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{fx} 解析后不是 dict"


@pytest.mark.parametrize("node_dir", _node_dirs(), ids=lambda p: p.name)
def test_fixture_has_required_input_artifacts(node_dir):
    """如果 harness 声明了 required_input_artifact_types，fixture 必须含至少一份对应 type。"""
    harness = yaml.safe_load((node_dir / "harness.yaml").read_text(encoding="utf-8"))
    required = harness.get("required_input_artifact_types") or []
    if not required:
        return  # no requirement → trivially pass
    fx = node_dir / "fixtures" / "minimal.yaml"
    if not fx.exists():
        pytest.skip("no fixture")
    data = yaml.safe_load(fx.read_text(encoding="utf-8")) or {}
    types_in_fixture = {a.get("type") for a in (data.get("upstream_artifacts") or [])}
    missing = set(required) - types_in_fixture
    assert not missing, (
        f"{node_dir.name} fixture 缺 required_input_artifact_types: {missing}"
    )

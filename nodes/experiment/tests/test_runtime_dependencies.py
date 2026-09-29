"""Keep experiment's in-process parsers in the base runtime contract."""
from __future__ import annotations

import tomllib
from pathlib import Path


def test_bash_semantic_parser_is_a_base_runtime_dependency():
    project_root = Path(__file__).resolve().parents[3]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())

    assert {
        "tree-sitter>=0.25,<0.26",
        "tree-sitter-bash>=0.25,<0.26",
    } <= set(pyproject["project"]["dependencies"])

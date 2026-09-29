"""experiment 节点本地测试配置。

与根 tests/conftest.py 相同的两件事（该 conftest 不覆盖本目录，需自带）：
  1. repo 根进 sys.path → `import core.*` / `import shared.*` 可用
  2. HARNESS_FRAMEWORK_HOME 隔离到 tmp，防止测试写开发者真 KB
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# repo 根 = nodes/experiment/tests/../../..
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

# 注：历史上这里曾模块级 monkeypatch `core.tool_registry.register_tool` 放宽
# safe_bash 同名覆盖的 ValueError。PR #112 把 safe_bash 改名成 safe_run_bash /
# safe_write_file / safe_execute_python 后已无同名冲突（去掉该 patch 本目录
# 24/24 仍全绿），故删除——那个 patch 是模块级、永不还原的全局污染，会泄漏到
# tests/test_tool_registry_spec.py 等后续测试，让"注册应报错"的用例集体
# DID NOT RAISE。


@pytest.fixture(autouse=True)
def _isolate_harness_home(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="hf-exp-test-home-") as tmp:
        monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", tmp)
        yield

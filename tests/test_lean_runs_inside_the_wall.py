"""模型写的 Lean 代码只能在墙内跑（#793 的最后一条 model_command 债）。

## 为什么 check_lean 是「模型代码」而不是「固定程序」

argv 长得像固定程序加一个文件（`lean Goal.lean`），但那个 `.lean` 文件的内容**是模型
写的**，而 Lean 4 的 `#eval` 可以执行任意 IO。也就是说这条路上跑的是模型的程序。

它此前用 `create_subprocess_exec` 直接在宿主上起，绕开唯一咽喉：没有写边界、没有断网、
没有 kill_event、没有整组清扫、也不进 isolation 记账。

`framework_exemptions.yaml` 把它登记为全仓**唯一**一条 `model_command`，owner=wangd，
deadline 2026-10-15，migrate_to 写着「像 latex.py 一样交给 spawn_and_wait」。这条测试
是那次迁移的判据；登记随之删除，全仓 model_command 债归零。

## A/B（真机，2026-09-10，本机装着 Lean 4）

同一段 payload：`#eval IO.FS.writeFile "<声明根之外>/PWNED.txt" "escaped"`

    修复前   ESCAPED: True   exit 0   ← 文件真的写出去了，而且 Lean **退 0**，静默
    修复后   ESCAPED: False  exit 1   ← "operation not permitted"

正例（`1 + 1 = 2` by rfl）两边都照常 exit 0 并盖章 —— 收进墙内没有削弱这个工具。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from core import isolation
from core.bootstrap import bootstrap
from core.isolation import Invariant
from core.state import State
from core.tool_registry import execute
from shared.tools.library.derivation_check import _lean_binary


def _wall_is_up() -> bool:
    try:
        return Invariant.WRITE_BOUNDARY in isolation.select_backend().capabilities()
    except isolation.IsolationContractError:
        return False


pytestmark = [
    pytest.mark.skipif(_lean_binary() is None, reason="这台机器没有 Lean 工具链"),
    pytest.mark.skipif(not _wall_is_up(), reason="这台机器上写边界起不来，没有墙可验"),
]


@pytest.fixture()
def workspace(tmp_path: Path):
    bootstrap(force=True)
    return State.new(node_type="derivation", base_dir=tmp_path / "runs")


def test_the_models_lean_code_cannot_write_outside_its_root(workspace, tmp_path):
    """`#eval` 的 IO 出不了声明的可写根。

    判据落在**宿主上那个文件到底存不存在**，不落在返回的文案上
    （[[feedback_asserting_the_label_asserts_nothing]]）。
    """
    victim = Path(tempfile.mkdtemp(prefix="hf-lean-victim-")) / "PWNED.txt"
    result = asyncio.run(execute(
        "check_lean", workspace,
        statement="1 + 1 = 2", proof="by rfl",
        preamble=f'#eval IO.FS.writeFile "{victim}" "escaped"',
    ))
    assert result["status"] == "success"
    assert not victim.exists(), (
        "模型写的 Lean 代码在宿主上写出了声明根之外的文件 —— check_lean 又绕开咽喉了"
    )
    assert result["exit_code"] != 0, "写被拒了，Lean 却退 0 —— 那是静默逃逸的形状"


def test_a_real_proof_still_gets_its_seal(workspace):
    """收进墙内不能把工具削弱：正例照常 exit 0 并盖章。

    没有这一条，上一条可以靠"把 Lean 整个禁掉"作弊通过。
    """
    result = asyncio.run(execute(
        "check_lean", workspace, statement="1 + 1 = 2", proof="by rfl"))
    assert result["status"] == "success"
    assert result["exit_code"] == 0, result.get("lean_output")
    assert "verification" in result, "真证明没盖章 —— 收进墙内把工具弄坏了"


def test_a_sorry_is_still_not_a_proof(workspace):
    """`sorry` 让 Lean 退 0，但那是没证。这条语义不因换执行路径而变。"""
    result = asyncio.run(execute(
        "check_lean", workspace, statement="1 + 1 = 2", proof="by sorry"))
    assert result["verified"] is False
    assert "verification" not in result

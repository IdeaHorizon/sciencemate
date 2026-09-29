"""v3.2 越界写入防护回归测试。

设计（照 Claude Code "gate on effect, not tool identity"）：
  - 环境安全类高危（rm -rf/sudo）= ask 确认，可被 /bypass 豁免
  - 越界写框架状态（shell/python 直写 artifacts/KB/memory）= **硬拒**，
    不问人、不受 /bypass 影响（契约完整性规则，不是权限规则）
这里测 pattern 拦截这一道。（曾有第二道 finalize provenance 审计，靠信封目录
与 `artifacts_ledger.jsonl` 比对；记录改为原生文件 + 账本后随之删除 —— 记录只经
`State.save_artifact` 落账本，绕开它写的文件不在账本上、也就不是记录。）
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shared.lib import dangerous_commands as dc
from core.state import State


# ── 第一层：pattern 分类正确 ────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,expected", [
    ("echo '{}' > artifacts/manuscript__x.json", True),
    ("echo '{}' > \"/abs/path/artifacts/manuscript__x.json\"", True),   # 绝对路径
    ("cp /tmp/x ~/.harness-framework/projects/p/kb_claims.jsonl", True),
    ("tee artifacts/out.json", True),
    ("sed -i s/a/b/ memory/directives.md", True),
    ("rm artifacts/foo.json", True),
    ("cat artifacts/survey.json", False),         # 纯读
    ("ls -la artifacts/", False),
    ("grep foo transcript.jsonl", False),
    ("sysctl -n hw.ncpu", False),                 # 查硬件
    ("rm -rf /tmp/build", False),                 # 高危但非越界（走另一条）
])
def test_boundary_shell_classification(cmd, expected):
    assert (dc.match_boundary_violation(cmd, mode="shell") is not None) == expected


@pytest.mark.parametrize("code,expected", [
    ("open('artifacts/x.json','w').write(s)", True),
    ("Path('artifacts/x.json').write_text(s)", True),
    ("shutil.copy('a', 'artifacts/y.json')", True),
    ("os.remove('artifacts/old.json')", True),
    ("import pandas as pd; pd.read_csv('data.csv')", False),
    ("x = sum(range(10)); print(x)", False),
])
def test_boundary_python_classification(code, expected):
    assert (dc.match_boundary_violation(code, mode="python") is not None) == expected


def test_boundary_not_confused_with_highrisk():
    """rm -rf 是高危（可 bypass），不是越界；echo>artifacts 是越界，不是高危。"""
    assert dc.match_high_risk("rm -rf /tmp/x", mode="shell")
    assert dc.match_boundary_violation("rm -rf /tmp/x", mode="shell") is None
    assert dc.match_boundary_violation("echo x > artifacts/a.json", mode="shell")
    assert dc.match_high_risk("echo x > artifacts/a.json", mode="shell") is None


# ── 第一层端到端：框架 run_bash / execute_python 硬拒 ───────────────────────

def test_framework_run_bash_denies_boundary_write(tmp_path):
    import shared.tools.builtin as b
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p1")
    fake = st.records_dir / "manuscript__sneaky.tex"
    res = asyncio.run(b._run_bash(st, cmd=f'echo x > "{fake}"'))
    assert res["status"] == "error"
    assert "越界" in res["error"]
    assert not fake.exists()   # 命令根本没执行


def test_framework_run_bash_allows_readonly(tmp_path):
    import shared.tools.builtin as b
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p2")
    res = asyncio.run(b._run_bash(st, cmd="echo hello"))
    assert res["status"] == "success"


def test_boundary_deny_ignores_bypass(tmp_path, monkeypatch):
    """越界拦截不受 /bypass 影响 —— bypass 只豁免高危确认。"""
    import shared.tools.builtin as b
    monkeypatch.setattr(dc, "BYPASS_ENABLED", True)
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p3")
    fake = st.records_dir / "manuscript__x.tex"
    res = asyncio.run(b._run_bash(st, cmd=f'echo x > "{fake}"'))
    assert res["status"] == "error" and "越界" in res["error"]
    assert not fake.exists()


def test_framework_python_denies_boundary_write(tmp_path):
    from shared.tools.library import python_exec as pe
    st = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p4")
    res = asyncio.run(pe._execute_python(
        st, code="open('artifacts/manuscript__x.json','w').write('x')"))
    assert res["status"] == "error"
    assert "越界" in res["error"]

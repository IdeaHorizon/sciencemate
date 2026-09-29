"""平台该知道自己有什么，不该让 agent 猜二进制名。

## 现场（2026-08-10 E2E v25）

experiment 要跑 LAMMPS，探测写的是：

    which lammps 2>/dev/null || which lmp 2>/dev/null || echo "LAMMPS not in PATH"

二进制叫 `lmp_serial`，**两个名字都没中** → 判定"没装" → 去
`conda install -y -c conda-forge lammps`（改用户的 conda 环境，被审批门拦住）。
而 LAMMPS 一直在 `/opt/homebrew/bin/lmp_serial`，**上一轮会话还用它跑成功过**。

追下去发现的不是探测 bug：

    project_resources 表        0 行，且它管的是项目绑定的外部资源+凭据
    discover_resources 工具     只探调度器和硬件，不含软件
    platform_context_snapshot   项目 id / 版本 / 模型后端 —— 不提算力软件
    全仓                        不存在"主机装了什么"这个概念

**平台不知道自己有什么**，于是每轮会话靠模型猜名字，而猜是名单式的 ——
"护栏要扫盘不要写名单"在**发现**这一侧的同款形态。
"""
from __future__ import annotations

from pathlib import Path

from core.host_capabilities import _resolvable, load, render_section


def _write(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "caps.yaml"
    f.write_text(body, encoding="utf-8")
    return f


def test_empty_registry_still_says_something(tmp_path: Path) -> None:
    """沉默会被读成"这里什么都没有"，然后模型就去装软件。

    所以空的时候必须明说是"平台没登记"，并给出**扫盘式**的探测姿势。
    """
    text = render_section(tmp_path / "does-not-exist.yaml")
    assert "没有登记任何算力软件" in text
    assert "这不等于机器上没有" in text
    assert "扫一遍再下结论" in text


def test_declared_software_is_rendered_with_its_invocation(tmp_path: Path) -> None:
    f = _write(tmp_path, """
software:
  - name: LAMMPS
    invoke: /bin/sh
    version: "stable"
    notes: "生产模拟走 submit_job"
""")
    text = render_section(f)
    assert "LAMMPS" in text and "/bin/sh" in text
    assert "stable" in text and "submit_job" in text
    assert "不代表全集" in text, "登记表是提示，不能被读成'只有这些'"


def test_a_stale_entry_is_loud_not_silent(tmp_path: Path) -> None:
    """登记的路径不存在时必须吵。

    一份说"LAMMPS 在 X"而 X 不存在的登记表，比没有登记表更糟 —— 它让模型
    信一个假事实，然后在错误的前提上一路推下去。
    """
    f = _write(tmp_path, """
software:
  - name: GhostSolver
    invoke: /definitely/not/here/ghost
""")
    entries = load(f)
    assert entries[0].resolvable is False
    assert "⚠️" in render_section(f)


def test_uncheckable_invocation_says_unknown_not_broken(tmp_path: Path) -> None:
    """组合命令（module load && …）判断不了 → None，不是 False。

    把"我判断不了"渲染成"它坏了"，会让人对真正的红色警告脱敏。
    """
    f = _write(tmp_path, """
software:
  - name: VASP
    invoke: "module load vasp/6.4 && vasp_std"
""")
    entries = load(f)
    assert entries[0].resolvable is None
    assert "⚠️" not in render_section(f)


def test_resolvable_handles_bare_commands_and_absolute_paths() -> None:
    assert _resolvable(__import__("sys").executable) is True
    assert _resolvable("/definitely/not/here") is False
    assert _resolvable("sh") is True
    assert _resolvable("definitely-not-a-real-command-xyz") is False
    assert _resolvable("a && b") is None
    assert _resolvable("") is None


def test_reading_a_broken_registry_never_raises(tmp_path: Path) -> None:
    """读一份**可选**的部署声明失败，绝不能改变 run 的形状。

    这条学费在 `project_autonomy_policy` 上付过一次：可选查询放进包住主流程的 try
    里，它一抛就顶掉了真正的 failure，`failure["code"]` 直接消失。
    """
    assert load(_write(tmp_path, "software: [ this is not: valid: yaml")) == []
    assert load(_write(tmp_path, "software: 42")) == []
    assert load(_write(tmp_path, "")) == []
    assert load(None) == []


def test_orientation_carries_the_section() -> None:
    """接到路径上 —— 造好了没人看见等于没造。"""
    import inspect

    from core import loop_hooks_builtin

    source = inspect.getsource(loop_hooks_builtin.build_orientation_snapshot)
    assert "host_capabilities" in source and "render_section" in source

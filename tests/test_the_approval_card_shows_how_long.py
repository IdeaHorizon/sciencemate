"""批准一个真实作业时，人得看得见它最多能跑多久。

## 病例（2026-09-07 真机第五轮）

高危闸把提交内容摆给人看 —— scheduler / job_name / workdir / output_dir /
stage_in / **memory_contract** / command 全在，**唯独没有时间**：

    工具：submit_job
    命中类别：真实外部作业提交
    完整内容（519 字符）：
    scheduler=local
    job_name=ising_mc_L64
    ...
    memory_contract={"memory_gb": 4.0, ...}
    command=python3 run_sweep_L64.py

于是那一轮里，人（我）批准之后只能从提交记录里翻 `walltime_seconds` 才知道
这个作业会占这台机器四个小时。而 PR #839 的说明里我写的是「要跑得更久就把这个
值写出来，**人在批准卡上看得见它**」—— 当时那句话是假的，这条 PR 把它变成真的。

「它最多能跑多久」是批准这件事时第二重要的数（第一是命令本身）：它决定这台机器
要被占多久，也决定作业会不会在跑到一半时被杀。内存契约都摆出来了，时间没道理
不摆。

## 三种局面要分清

对人的含义完全不同：显式给了 / 本地没给（平台会杀，把数和来历一起说）/
调度器没给（站点默认，我们确实不知道 —— 别编一个）。
"""
from __future__ import annotations

import pytest

from nodes.experiment.tools.resource_manager import (
    _LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES,
    _walltime_for_a_human,
)


def test_an_explicit_walltime_is_shown_as_given() -> None:
    for scheduler in ("local", "slurm"):
        line = _walltime_for_a_human(scheduler, 240)
        assert "240" in line and "显式" in line


def test_a_local_job_without_one_says_the_cap_and_what_it_does() -> None:
    """本地没给 —— 那个数、它的来历、以及到点会发生什么，三件都要说。

    只说数字不说"会杀进程"，人读到的是"预计"，而它其实是"上限"。
    """
    line = _walltime_for_a_human("local", None)
    assert str(_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES) in line
    assert "杀" in line, "没说到点会杀进程 —— 人会把它读成预计时长"
    assert "平台" in line or "安全上限" in line, "没说这个数是平台加的，不是作业要的"


def test_a_scheduler_job_without_one_admits_it_does_not_know() -> None:
    """调度器那边的站点默认，我们确实不知道 —— 说不知道，别编一个数。"""
    line = _walltime_for_a_human("slurm", None)
    assert str(_LOCAL_WALLTIME_SAFETY_DEFAULT_MINUTES) not in line, (
        "把本地的安全上限当成了调度器的站点默认 —— 那是编的"
    )
    assert "不知道" in line or "未指定" in line


def test_the_card_actually_carries_it() -> None:
    """判据落在**卡片正文**上，不是"有这么个函数"。

    只加函数不接到 preview 上，是这个仓库里反复出现的那种"修复落在没人走的
    路上"。这里按 AST 查 preview 那段 f-string 里确实引用了它。
    """
    import ast
    import pathlib

    import nodes.experiment.tools.resource_manager as rm

    tree = ast.parse(pathlib.Path(rm.__file__).read_text(encoding="utf-8"))
    called_in_a_preview = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "preview":
            continue
        for inner in ast.walk(node.value):
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                    and inner.func.id == "_walltime_for_a_human"):
                called_in_a_preview = True
    assert called_in_a_preview, (
        "`_walltime_for_a_human` 没有出现在任何一张卡片的 preview 里 —— "
        "函数写了，人还是看不见"
    )

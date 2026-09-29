"""027 v3 审查：共享精确探查判定与重定向剥离的反方向钉子。

现有用例只钉住「带非选项操作数不豁免」「单独一个非探查 flag 不豁免」；
「探查 flag + 非探查选项」与「重定向后面还跟着真实操作数」两类没有钉住，
放宽成 any(探查 flag) 或让重定向吞掉后续 token，定向测试全绿。
"""
from __future__ import annotations

import pytest

from nodes.experiment.tools import safe_bash as sb


# ── 共享判定：只有探查 flag 才算探查 ─────────────────────────────────────────

@pytest.mark.parametrize(
    ("args", "flags", "prefixes"),
    [
        (("--version", "--debug"), frozenset({"--version"}), ()),
        (
            ("-v", "-xc", "-"),
            sb._COMPILER_CAPABILITY_PROBE_FLAGS,
            sb._COMPILER_CAPABILITY_PROBE_PREFIXES,
        ),
    ],
)
def test_probe_flag_mixed_with_non_probe_option_is_not_exact(args, flags, prefixes):
    assert sb._is_exact_capability_probe(args, flags=flags, flag_prefixes=prefixes) is False


def test_compiler_verbose_with_stdin_source_stays_build_and_write():
    # gcc 13 实测：`echo 'int x;' | gcc -v -c -xc -` 写出 `-.o`；`gcc -v -xc -` 写出 a.out
    assert sb._is_major_build("gcc -v -c -xc -") is True
    assert sb._compiler_write_targets(["gcc", "-v", "-xc", "-"], "/run") != []


# ── 重定向剥离：重定向后面的真实操作数不能被吞掉或截断 ─────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "make --version 2>/dev/null install",  # 截断式剥离会把它判成探查
        "make 2>&1 -C -n all",                  # 吞 token 式剥离会把它判成 dry-run
    ],
)
def test_redirection_stripping_keeps_following_build_operands(command):
    assert sb._is_major_build(command) is True


# ── 顺手加固（027 之前就有的缺口，base 上同样通过）──────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "srun 2>/dev/null ./app --help",
        "mpirun 2>&1 ./solver --version",
        "srun --version && srun -n 2 hostname",
        "mpirun --version; mpirun -np 4 ./solver",
        "mpirun --version 2>&1 | head -1 && mpirun -np 2 ./solver",
    ],
)
def test_launcher_probe_does_not_exempt_the_real_launch(command):
    assert sb._is_major_run(command) is True

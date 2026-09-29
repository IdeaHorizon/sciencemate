"""047：命令位之前的环境赋值不得让"只读"判定失真。

只读判定看得见 argv，看不见环境变量能把"执行哪段代码"换掉。判据因此是白名单
（`_read_only_env_prefix_allowed`）而不是"坏变量名单"——坏变量追不完：LD_PRELOAD
有等效的 LD_AUDIT，`git -c core.fsmonitor=` 有等效的 GIT_CONFIG_COUNT/KEY_n/VALUE_n。

本机实测过两件事，是这组判据的依据：
1. `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=<脚本> git status`
   会真的执行那个脚本；
2. `LD_AUDIT=<不存在的.so> ls --version` 时 ld.so 在 ls 打印版本号**之前**就去加载它
   —— 所以"这是一条能力探查"不构成任何豁免理由。
"""
from __future__ import annotations

import pytest

from nodes.experiment.tools import safe_bash as sb


def _read_only(cmd: str) -> bool:
    return sb._is_read_only_shell_command(cmd)


# ── (b) 值的形状：判据只看名字，不看值长什么样 ────────────────────────────────
@pytest.mark.parametrize(
    "value",
    [
        "/tmp/evil.so",          # 路径
        "evil.so",               # 相对名
        "id",                    # 裸命令名（按值的形状放行会从这里漏）
        "",                      # 空值
        "/tmp/a b.so",           # 含空格
        "$(id)",                 # 命令替换的字面文本
    ],
)
def test_unlisted_env_name_is_never_read_only_whatever_its_value(value: str):
    assert _read_only(f'LD_AUDIT="{value}" ls -l') is False


@pytest.mark.parametrize(
    "value", ["C", "C.UTF-8", "en_US.UTF-8", "", "/usr/share/locale"],
)
def test_listed_env_name_stays_read_only_whatever_its_value(value: str):
    assert _read_only(f'LC_ALL="{value}" ls -l') is True


def test_value_conditional_entry_only_passes_its_exact_value():
    """PAGER 本身是"执行哪个程序"，只有恒等分页器 cat 例外。"""
    assert _read_only("PAGER=cat git log -1") is True
    assert _read_only("PAGER=/tmp/evil.sh git log -1") is False
    assert _read_only("PAGER=less git log -1") is False


# ── (c) 包裹：换一层壳不换判据 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "cmd",
    [
        "LD_AUDIT=/tmp/e.so ls -l",
        "env LD_AUDIT=/tmp/e.so ls -l",
        "command LD_AUDIT=/tmp/e.so ls -l",
        "bash -c 'LD_AUDIT=/tmp/e.so ls -l'",
        "sh -c 'LD_AUDIT=/tmp/e.so ls -l'",
        "(LD_AUDIT=/tmp/e.so ls -l)",
        "export LD_AUDIT=/tmp/e.so; ls -l",
        "ls -l | LD_AUDIT=/tmp/e.so grep x",
    ],
)
def test_no_wrapping_makes_an_unlisted_prefix_read_only(cmd: str):
    assert _read_only(cmd) is False


# ── 判据本身：白名单外一律不算无害 ────────────────────────────────────────────
@pytest.mark.parametrize(
    "name",
    [
        # 黑名单时代漏掉的等效机制
        "LD_AUDIT", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_GLOBAL", "GIT_SSH_COMMAND", "HOME", "NODE_OPTIONS",
        "PERL5OPT", "RUBYOPT", "PYTHONSTARTUP", "LESSOPEN", "TERMINFO",
        "MANPAGER", "EDITOR", "VISUAL", "XDG_CONFIG_HOME", "GLIBC_TUNABLES",
        # 黑名单时代已经在挡的，必须继续挡
        "LD_PRELOAD", "LD_LIBRARY_PATH", "PATH", "BASH_ENV", "PYTHONPATH",
        "TAR_OPTIONS", "GIT_EXTERNAL_DIFF",
        # 从没有人想到过的名字：默认落在"算覆盖"那一侧才叫加严
        "SOME_FUTURE_LOADER_HOOK",
    ],
)
def test_unlisted_names_are_identity_overrides(name: str):
    assert sb._read_only_env_prefix_allowed(name, "/tmp/x") is False
    assert _read_only(f"{name}=/tmp/x ls -l") is False


@pytest.mark.parametrize(
    "name",
    ["LANG", "LANGUAGE", "TZ", "TERM", "COLUMNS", "LINES", "NO_COLOR",
     "GREP_COLORS", "LC_ALL", "LC_CTYPE", "LC_TIME", "CLICOLOR", "CLICOLOR_FORCE"],
)
def test_listed_names_are_allowed(name: str):
    assert sb._read_only_env_prefix_allowed(name, "C") is True


def test_every_allowed_name_carries_a_reason():
    """白名单每条都要能回答"它为什么改变不了被执行的代码"。"""
    for name, reason in sb._READ_ONLY_ENV_PREFIX_EXACT.items():
        assert reason and reason.strip(), name


def test_the_two_known_equivalents_of_blocked_argv_forms_are_blocked():
    """argv 形已挡的能力，环境形必须同样挡 —— 这是 047 的起因。"""
    assert _read_only("git -c core.fsmonitor=/tmp/e.sh status --short") is False
    assert _read_only(
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor "
        "GIT_CONFIG_VALUE_0=/tmp/e.sh git status --short") is False
    assert _read_only("LD_PRELOAD=/tmp/e.so ls -l") is False
    assert _read_only("LD_AUDIT=/tmp/e.so ls -l") is False


# ── export 语句形式：_is_read_only_shell_command 在更早一步就拒了整段 export，
# 所以这条分支只被入口信任投影（_read_only_entry_is_trusted /
# _route_event_is_known）用到。直接钉判据本身，否则改坏了没有任何测试会红。
@pytest.mark.parametrize(
    "segment, overridden",
    [
        ("export LD_AUDIT=/tmp/e.so", True),
        ("export LD_PRELOAD=/tmp/e.so", True),
        ("export GIT_CONFIG_COUNT=1", True),
        ("export SOME_FUTURE_LOADER_HOOK=/tmp/e.so", True),
        ("export PAGER=/tmp/evil.sh", True),
        ("export LC_ALL=C", False),
        ("export LANG=C.UTF-8 TZ=UTC", False),
        ("export PAGER=cat", False),
        ("export LC_ALL=C LD_AUDIT=/tmp/e.so", True),
    ],
)
def test_export_statement_uses_the_same_whitelist(segment: str, overridden: bool):
    assert sb._segment_has_identity_override(segment) is overridden

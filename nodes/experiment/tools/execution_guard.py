"""Shared Experiment-only risk classification for execution adapters.

``safe_bash`` and ``submit_job`` execute different transports, but they must
classify the same payload identically.  Framework-wide patterns remain the
base policy; this module holds only Experiment's stricter additions and
exposes one combined classifier for shell and Python payloads.
"""
from __future__ import annotations

import re


# Additions not covered by shared.lib.dangerous_commands.  Keep labels stable:
# they are shown in the single structured confirmation and in audit events.
SHELL_EXTRA_HIGH_RISK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|[\s;&|(])sudo(?:\s|$)"), "提权: sudo"),
    (re.compile(r"(?:^|[\s;&|(])su(?:\s|$)"), "提权: su"),
    (re.compile(r"(?:^|[\s;&|(])pkexec(?:\s|$)"), "提权: pkexec"),
    (re.compile(r"(?:^|[\s;&|(])rm\s+(?:-[a-zA-Z]*r[a-zA-Z]*|--recursive)\b"), "递归删除: rm -r/-rf"),
    (re.compile(r"(?:^|[\s;&|(])dd\b[^\n]*\bof=/dev/"), "磁盘覆写: dd of=/dev/"),
    (re.compile(r"(?:^|[\s;&|(])mkfs\b"), "格式化: mkfs"),
    (re.compile(r"(?:^|[\s;&|(])(?:fdisk|parted|gdisk)\b"), "分区表: fdisk/parted/gdisk"),
    (re.compile(r"(?:^|[\s;&|(])(?:shutdown|reboot|halt|poweroff)\b"), "系统控制: shutdown/reboot/halt"),
    (re.compile(r"(?:^|[\s;&|(])killall\b"), "进程杀除: killall"),
    (re.compile(r"(?:^|[\s;&|(])kill\s+-(?:9|KILL)\b"), "进程杀除: kill -9"),
    (re.compile(r"git\s+push\s+(?:--force\b|-f\b|--force-with-lease\b)"), "强推: git push --force"),
    (re.compile(r"git\s+reset\s+--hard\b"), "硬重置: git reset --hard"),
    (re.compile(r"git\s+clean\s+-[a-zA-Z]*f"), "强清: git clean -fd"),
)

_SHELL_OUT = r"(?:os\.system|os\.popen|subprocess\.\w+|pty\.spawn|commands\.\w+)"
_RISK_WORDS = (
    r"sudo|pkexec|rm\s+-[a-zA-Z]*r|--recursive|mkfs|dd\s+[^\n]*of=/dev/|"
    r"fdisk|parted|gdisk|shutdown|reboot|halt|poweroff|killall|kill\s+-(?:9|KILL)|"
    r"git\s+push\s+(?:--force|-f)|git\s+reset\s+--hard|git\s+clean\s+-[a-zA-Z]*f"
)
PYTHON_EXTRA_HIGH_RISK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(_SHELL_OUT + r"[^\n]{0,200}?(?:" + _RISK_WORDS + r")"),
     "shell-out 调高危命令 (os.system/subprocess + sudo/rm -rf/...)"),
    (re.compile(r"""['\"]rm['\"]\s*,\s*['\"]-[a-zA-Z]*r"""), "递归删除(argv): [rm, -rf]"),
    (re.compile(r"shutil\.rmtree\s*\("), "递归删除: shutil.rmtree"),
    (re.compile(r"os\.removedirs\s*\("), "递归删除: os.removedirs"),
    (re.compile(r"""open\s*\(\s*['\"]/dev/(?:sd|nvme|vd|mmcblk)"""), "写块设备: open('/dev/sdX')"),
)


def match_extra_high_risk(text: str, *, mode: str = "shell") -> str | None:
    patterns = (PYTHON_EXTRA_HIGH_RISK_PATTERNS if mode == "python"
                else SHELL_EXTRA_HIGH_RISK_PATTERNS)
    for pattern, label in patterns:
        if pattern.search(text or ""):
            return label
    return None


def classify_high_risk(text: str, *, mode: str = "shell") -> str | None:
    """Return the one authoritative Experiment classification for a payload."""
    from shared.lib import dangerous_commands as danger

    return danger.match_high_risk(text, mode=mode) or match_extra_high_risk(text, mode=mode)


def all_shell_risk_labels(text: str) -> set[str]:
    """Return every matching shell label for containment decisions."""
    from shared.lib import dangerous_commands as danger

    labels = {label for pattern, label in SHELL_EXTRA_HIGH_RISK_PATTERNS if pattern.search(text or "")}
    labels.update(label for pattern, label in danger._SHELL_PATTERNS if pattern.search(text or ""))
    return labels

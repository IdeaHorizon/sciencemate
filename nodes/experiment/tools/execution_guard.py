"""Shared Experiment-only risk classification for execution adapters.

``safe_bash`` and ``submit_job`` execute different transports, but they must
classify the same payload identically.  Framework-wide patterns remain the
base policy; this module holds only Experiment's stricter additions and
exposes one combined classifier for shell and Python payloads.
"""
from __future__ import annotations

import ast
import re

from .bash_semantics import analyze_bash


# Additions not covered by shared.lib.dangerous_commands.  Keep labels stable:
# they are shown in the single structured confirmation and in audit events.
SHELL_EXTRA_HIGH_RISK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|[\s;&|(])sudo(?:\s|$)"), "提权: sudo"),
    (re.compile(r"(?:^|[\s;&|(])su(?:\s|$)"), "提权: su"),
    (re.compile(r"(?:^|[\s;&|(])pkexec(?:\s|$)"), "提权: pkexec"),
    (re.compile(r"(?:^|[\s;&|(])rm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b"), "递归删除: rm -r/-rf"),
    (re.compile(r"(?:^|[\s;&|(])dd\b[^\n]*\bof=/dev/"), "磁盘覆写: dd of=/dev/"),
    (re.compile(r"(?:^|[\s;&|(])mkfs\b"), "格式化: mkfs"),
    (re.compile(r"(?:^|[\s;&|(])(?:fdisk|parted|gdisk)\b"), "分区表: fdisk/parted/gdisk"),
    (re.compile(r"(?:^|[\s;&|(])(?:shutdown|reboot|halt|poweroff)\b"), "系统控制: shutdown/reboot/halt"),
    (re.compile(r"(?:^|[\s;&|(])killall\b"), "进程杀除: killall"),
    (re.compile(r"(?:^|[\s;&|(])kill\s+-(?:9|KILL)\b"), "进程杀除: kill -9"),
    (re.compile(r"git\s+push\b[^\n;|&]*\s(?:--force\b|-f\b|--force-with-lease\b)"), "强推: git push --force"),
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


from shared.lib.shell_risk import UNPARSED as _UNPARSED, python_report_data


def _python_tree(source, syntax_tree=_UNPARSED):
    if syntax_tree is not _UNPARSED:
        return syntax_tree
    try:
        return ast.parse(source)
    except (ValueError, SyntaxError, RecursionError):
        return None


def _report_python(source: str, *, syntax_tree=_UNPARSED) -> bool:
    """Recognize data construction/report emission, not all 'benign Python'.

    Anything outside this grammar retains the conservative legacy scan. In
    particular a value later used as a program is not proven report data.
    """
    return python_report_data(source, syntax_tree=_python_tree(source, syntax_tree))


def _shell_labels(text: str) -> set[str]:
    from shared.lib.shell_risk import execution_fragments

    fragments = execution_fragments(text)
    labels = {label for pattern, label in SHELL_EXTRA_HIGH_RISK_PATTERNS
              if any(pattern.search(fragment) for fragment in fragments)}
    # Preserve Python-specific additions when a literal shell command invokes
    # Python. Both shell consumers use the shared executable-position projection.
    plan = analyze_bash(text)
    for event in plan.static_path_events:
        if (event.kind == "command"
                and re.fullmatch(r"python(?:[23](?:\.\d+)?)?", event.head or "")
                and len(event.args) == 2 and event.args[0] == "-c"
                and not event.dynamic_args):
            label = match_extra_high_risk(event.args[1], mode="python")
            if label:
                labels.add(label)
    return labels


def match_extra_high_risk(text: str, *, mode: str = "shell", syntax_tree=_UNPARSED) -> str | None:
    if mode == "python":
        # Actual device opens and dangerous calls still match even with other
        # report statements present. Quoted code is data in known reports.
        tree = _python_tree(text or "", syntax_tree)
        if _report_python(text or "", syntax_tree=tree):
            for call in (x for x in ast.walk(tree) if isinstance(x, ast.Call)):
                if isinstance(call.func, ast.Name) and call.func.id == "open" and call.args:
                    path = call.args[0]
                    if isinstance(path, ast.Constant) and isinstance(path.value, str) and re.match(r"/dev/(sd|nvme|vd|mmcblk)", path.value):
                        return PYTHON_EXTRA_HIGH_RISK_PATTERNS[-1][1]
            return None
        return next((label for pat, label in PYTHON_EXTRA_HIGH_RISK_PATTERNS if pat.search(text or "")), None)
    labels = _shell_labels(text or "")
    return next((label for _, label in (*SHELL_EXTRA_HIGH_RISK_PATTERNS, *PYTHON_EXTRA_HIGH_RISK_PATTERNS)
                 if label in labels), None)


def classify_high_risk(text: str, *, mode: str = "shell", syntax_tree=_UNPARSED) -> str | None:
    """Return the one authoritative Experiment classification for a payload."""
    from shared.lib import dangerous_commands as danger

    return danger.match_high_risk(text, mode=mode, syntax_tree=syntax_tree) or match_extra_high_risk(
        text, mode=mode, syntax_tree=syntax_tree)


def all_shell_risk_labels(text: str) -> set[str]:
    """Return every matching label using the same shared shell projection."""
    from shared.lib import dangerous_commands as danger

    return _shell_labels(text or "") | danger.shell_risk_labels(text)

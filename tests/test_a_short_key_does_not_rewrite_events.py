"""#1228：一个太短的「key」不许把事件里的路径与 code 抹花。

PC 真机：api_key 配成 ``"k"`` → 事件流里每个 ``k`` 都成了 ``[REDACTED]``，私有 state 目录名被改，后端判
「转录逃出 state 根」，用户看到 ``harness_worker_unreachable``。规则只有一份（``shared.lib.secret_values``）：
短于 8 个字符的值不按原值抹；真 key 照抹；长成 key 样子的另有按形状的规则。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

from platform_runtime import SecretFilter
from shared.lib.secret_values import MIN_SECRET_CHARS, redact_values, secret_values, split_secret_values

ROOT = Path(__file__).resolve().parents[1]
EVENT = {
    "type": "ready",
    "transcript_path": "/tmp/hf-state-6k8tx30o/transcript.jsonl",
    "warnings": [{"code": "home_marker_unreadable", "path": r"C:\Users\k\afs\harness-state"}],
}


def test_a_one_letter_key_leaves_the_event_alone(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="platform_runtime"):
        clean = SecretFilter(["k", "sk-", "  "]).value(EVENT)
    assert clean == EVENT
    assert "not redacted by value" in caplog.text, "放过太短的值要说一声，不然没人知道 key 填错了"


def test_a_real_key_is_still_redacted_and_surrounding_space_does_not_hide_it() -> None:
    key = "Zq7vR2mXp9LwT4cN"
    clean = SecretFilter([f"  {key} "]).text(f"calling https://api.example/v1?k={key}&x=1")
    assert key not in clean and "[REDACTED]" in clean


def test_longer_secrets_are_replaced_first() -> None:
    long, prefix = "abcdefgh12345678", "abcdefgh"
    assert redact_values(f"x {long} y", secret_values([prefix, long])) == "x [REDACTED] y"


def test_the_threshold_is_counted_after_stripping() -> None:
    kept, too_short = split_secret_values(["a" * (MIN_SECRET_CHARS - 1) + "   ", "b" * MIN_SECRET_CHARS, None, 3])
    assert kept == ["b" * MIN_SECRET_CHARS] and too_short == 1


def _value_substring_redactions(path: Path) -> list[int]:
    """`<x>.replace(<变量>, "[REDACTED]" / REDACTED)`：按原值做子串替换。规则只许在一处。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "replace"
                and len(node.args) == 2 and not isinstance(node.args[0], ast.Constant)):
            second = node.args[1]
            if (isinstance(second, ast.Constant) and isinstance(second.value, (str, bytes))
                    and "REDACTED" in str(second.value)) or (isinstance(second, ast.Name) and "redact" in second.id.lower()):
                lines.append(node.lineno)
    return lines


def test_redacting_by_value_lives_in_one_place() -> None:
    home = ROOT / "shared" / "lib" / "secret_values.py"
    found = {}
    scanned = 0
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if any(part in rel.split("/") for part in (".venv", "node_modules", ".wt", "tests")) or rel.startswith("docs"):
            continue
        scanned += 1
        hits = _value_substring_redactions(path)
        if hits and path != home:
            found[rel] = hits
    assert scanned > 200, f"只扫到 {scanned} 个文件 —— 这道闸在空扫"
    assert _value_substring_redactions(home), "规则那一处自己都认不出来 —— 判据写坏了"
    assert found == {}, f"按原值抹 key 要走 shared.lib.secret_values（#1228），这些地方自己写了一份：{found}"

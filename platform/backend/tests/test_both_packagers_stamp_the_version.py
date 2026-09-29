"""两个平台的包都得知道自己是哪一版。

没有 `PAYLOAD_VERSION` 标记不是「版本未知」，是**任何更新都比它新**
（`is_newer(candidate, None)` 恒真）：界面永远挂着「有新版本」，点了更新、重启、
还是有 —— 因为新装的那份同样没有标记。用户看到的是一个消不掉的横幅。

2026-09-08 实测：Windows 打包器从来没写过这个标记，doctor 早就把
`version: (no PAYLOAD_VERSION marker …)` 印在自检输出里，只是没有任何判据读它。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "package"
PACKAGERS = ("build_mac_app.py", "build_windows_app.py")


def _calls(tree: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


@pytest.mark.parametrize("packager", PACKAGERS)
def test_the_packager_stamps_the_version(packager) -> None:
    """判据落在**调用**上，不在字符串出现上：撤掉调用留个注释照样能 grep 到。"""
    calls = _calls(ast.parse((PACKAGE / packager).read_text(encoding="utf-8")))
    assert calls & {"write_the_version_marker", "build_the_payload"}, (
        f"{packager} 没写版本标记 —— 装出来的包会永远提示有新版本"
    )


@pytest.mark.parametrize("packager", PACKAGERS)
def test_the_self_check_refuses_a_package_without_a_version(packager) -> None:
    """光写还不够：自检得在它没写成时拦住，否则下次改坏了照样发得出去。"""
    text = (PACKAGE / packager).read_text(encoding="utf-8")
    assert "version" in text and ("PAYLOAD_VERSION" in text or "the_version()" in text), packager

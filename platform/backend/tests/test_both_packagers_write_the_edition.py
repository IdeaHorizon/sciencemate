"""两个平台的包都得知道自己是哪种发行 —— 而且是同一种写法。

`edition.json` 住在 bundle 里、不在载荷里（EXEC_PLAN_TWO_EDITIONS §1；理由见
`app/edition.py`）。打包器不写它，装出来的专业版就是个人版：入口不画、更新指向
公开仓库，且没有任何东西报错。判据落在**调用**上，不在字符串出现上。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "package"
PACKAGERS = ("build_mac_app.py", "build_windows_app.py")


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


@pytest.mark.parametrize("packager", PACKAGERS)
def test_the_packager_writes_the_edition_through_the_shared_writer(packager) -> None:
    """两个打包器都经 `payload_release.write_the_edition` 写 —— 格式只在 app/edition.py 定一次。"""
    tree = ast.parse((PACKAGE / packager).read_text(encoding="utf-8"))
    writes = [c for c in _calls(tree) if _name(c) == "write_the_edition"]
    assert writes, f"{packager} 没写 edition.json —— 装出来的专业版会是个人版"
    for call in writes:
        assert isinstance(call.func, ast.Attribute) and getattr(call.func.value, "id", "") == "payload_release", (
            f"{packager} 自己写 edition.json，而不是走 payload_release.write_the_edition —— 格式会分叉")


@pytest.mark.parametrize("packager", PACKAGERS)
def test_the_self_check_asks_the_package_which_edition_it_is(packager) -> None:
    """光写还不够：写错一层就静默变成个人版，自检得真起一次、问 /api/v1/capabilities。"""
    text = (PACKAGE / packager).read_text(encoding="utf-8")
    assert "/api/v1/capabilities" in text and 'get("edition")' in text, (
        f"{packager} 的装完自检没核对包自报的发行")


def test_the_manifest_carries_the_edition() -> None:
    """发布件自己说清楚是哪种发行 —— publish_release 据此拒绝把专业版发进公开仓库。"""
    tree = ast.parse((PACKAGE / "build_mac_app.py").read_text(encoding="utf-8"))
    payload_calls = [c for c in _calls(tree) if _name(c) == "build_the_payload"]
    assert payload_calls, "Mac 打包器不再打载荷了？"
    for call in payload_calls:
        assert any(k.arg == "edition" for k in call.keywords), (
            "build_the_payload 没带 edition —— manifest 里不会有它，发布闸就没有依据")

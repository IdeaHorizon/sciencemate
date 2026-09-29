"""个人版只从导出的公开树打 —— 包里的字节等于开源树里的字节。

2026-09-29 挂载刚打好的个人版 dmg：edition.json 写着 personal，site-packages 里却有完整的
`app/pro/`（明文），静态界面里编进了组织页；自更新的 extras 归档同样带着。分野（#1169–#1172）
守住了源码树与开源导出，没守住装出来的包。

这里守三件事：入口那道拒绝（含专业版的树 + 个人版 → 拒，且报错指名导出那条路）、翻包那道
拒绝（包里有 app/pro 或组织页 → 拒）、两个打包器都真的调了这两道（AST 查 Call，不查字符串）。
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = REPO / "scripts" / "package"
PACKAGERS = ("build_mac_app.py", "build_windows_app.py")


def _payload_release():
    spec = importlib.util.spec_from_file_location("payload_release_under_test", PACKAGE / "payload_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _a_tree(tmp_path: pathlib.Path, *, with_pro: bool) -> pathlib.Path:
    repo = tmp_path / ("internal" if with_pro else "public")
    (repo / "platform" / "backend" / "app").mkdir(parents=True)
    (repo / "platform" / "frontend" / "src").mkdir(parents=True)
    if with_pro:
        (repo / "platform" / "backend" / "app" / "pro").mkdir()
        (repo / "platform" / "frontend" / "src" / "pro").mkdir()
    return repo


def test_the_personal_edition_refuses_a_tree_that_still_holds_the_pro_edition(tmp_path) -> None:
    pr = _payload_release()
    with pytest.raises(SystemExit) as caught:
        pr.the_personal_edition_is_built_from_the_public_tree(_a_tree(tmp_path, with_pro=True), "personal")
    said = str(caught.value)
    assert "platform/backend/app/pro" in said and "platform/frontend/src/pro" in said
    assert "export_public_tree.py" in said, "拒绝了却没指名合法的那条路（导出）"


def test_the_public_tree_and_the_pro_edition_pass(tmp_path) -> None:
    pr = _payload_release()
    assert pr.the_personal_edition_is_built_from_the_public_tree(_a_tree(tmp_path, with_pro=False), "personal") is None
    assert pr.the_personal_edition_is_built_from_the_public_tree(_a_tree(tmp_path, with_pro=True), "pro") is None


def test_a_personal_package_with_pro_traces_is_refused(tmp_path) -> None:
    pr = _payload_release()
    package = tmp_path / "ScienceMate.app"
    site = package / "Contents" / "Resources" / "python" / "lib" / "python3.14" / "site-packages" / "app"
    (site / "pro").mkdir(parents=True)
    (site / "pro" / "__init__.py").write_text("")
    (site / "static_ui").mkdir()
    (site / "static_ui" / "organisation.html").write_text("<html>")
    with pytest.raises(SystemExit, match="app/pro/__init__.py"):
        pr.the_package_carries_no_pro_edition(package, "personal")
    # 专业版包里当然有 —— 不是它的事。
    assert pr.the_package_carries_no_pro_edition(package, "pro") is None
    # 干净的个人版包放行。
    clean = tmp_path / "Clean.app"
    (clean / "site-packages" / "app" / "static_ui").mkdir(parents=True)
    (clean / "site-packages" / "app" / "static_ui" / "index.html").write_text("<html>")
    assert pr.the_package_carries_no_pro_edition(clean, "personal") is None


def _calls_in(tree: ast.AST, function: str) -> list[ast.Call]:
    body = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
    return [n for n in ast.walk(body) if isinstance(n, ast.Call)]


def _is(call: ast.Call, module: str, name: str) -> bool:
    f = call.func
    return isinstance(f, ast.Attribute) and f.attr == name and getattr(f.value, "id", "") == module


@pytest.mark.parametrize("packager", PACKAGERS)
def test_the_packager_asks_before_it_starts_and_again_when_it_is_done(packager) -> None:
    """判据落在调用上：main 里问「从哪棵树打」，装完自检里问「包里有没有」。"""
    tree = ast.parse((PACKAGE / packager).read_text(encoding="utf-8"))
    assert any(_is(c, "payload_release", "the_personal_edition_is_built_from_the_public_tree")
               for c in _calls_in(tree, "main")), f"{packager} 动手前没问「个人版是不是从公开树打」"
    assert any(_is(c, "payload_release", "the_package_carries_no_pro_edition")
               for c in _calls_in(tree, "prove_the_package_works")), f"{packager} 装完自检没翻包找专业版的痕迹"

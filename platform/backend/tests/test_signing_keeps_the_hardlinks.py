"""签名不许把硬链接签散 —— 一个 inode 只签一次。

git 的 `libexec/git-core/` 里 167 个命令是同一个 inode 的硬链接（上游 git 就这么
装）。`codesign --force` 的语义是"重写这个文件"，逐个签就逐个断链，167 个命令各自
变成一份 4.2 MB 的实体。2026-09-16 打 0.5.0 时实测：`Resources/git` 21 MB → 599 MB，
dmg 245 MB → 530 MB。**没有任何东西会报错** —— 包照样能跑、签名照样有效，只是同事的
下载量翻了一倍。

判据落在打包器的**做法**上（AST：按 inode 分组 + 重新 link），不落在"产物有多大"
—— 后者要打一次 994 MB 的包才问得出来，而这道闸要能在单测里转红。
"""
from __future__ import annotations

import ast
import pathlib

PACKAGER = (pathlib.Path(__file__).resolve().parents[3]
            / "scripts" / "package" / "build_mac_app.py")


def _sign_function() -> ast.FunctionDef:
    tree = ast.parse(PACKAGER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "sign":
            return node
    raise AssertionError("build_mac_app.py 里没有 sign() 了")


def _names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            out.add(child.attr)
        elif isinstance(child, ast.Name):
            out.add(child.id)
    return out


def test_signing_groups_by_inode() -> None:
    names = _names(_sign_function())
    assert "st_ino" in names, (
        "sign() 没有按 inode 分组 —— 硬链接会被逐个重写签散，包和 dmg 体积翻倍，"
        "而没有任何东西会报错"
    )


def test_signing_puts_the_hardlinks_back() -> None:
    names = _names(_sign_function())
    assert "link" in names, (
        "sign() 分了组却没把同伴重新 link 回去 —— 少签的那些会变成没签名的实体副本"
    )

"""框架里说给人听的异常，两种语言都写。

`core.materials.MaterialError` 的话由平台原样给上传文件的人看 —— 从前只有中文，英文界面上
就是一句中文（2026-10-19）。现在它和平台的 `SaidToAPerson` 一个形状：`en=` 必填、只收关键字，
`said_in(lang)` 按那一屏说。框架不 import 平台，所以这一条在框架这边自己守：

`core/` 里定义了 `said_in` 的异常类（及其子类），每一处构造都得带 `en=`，英文里不许有汉字。
按"这个类说给人听"扫，不列类名。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1] / "core"
HAN = re.compile(r"[一-鿿]")


def _speaking_classes(trees) -> set[str]:
    classes = [n for t in trees.values() for n in ast.walk(t) if isinstance(n, ast.ClassDef)]
    speaking = {c.name for c in classes
                if any(isinstance(f, ast.FunctionDef) and f.name == "said_in" for f in c.body)}
    grew = True
    while grew:
        before = len(speaking)
        speaking |= {c.name for c in classes
                     if any(getattr(b, "id", getattr(b, "attr", None)) in speaking for b in c.bases)}
        grew = len(speaking) != before
    return speaking


def test_every_raise_brings_its_english() -> None:
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in CORE.rglob("*.py")}
    speaking = _speaking_classes(trees)
    assert {"MaterialError", "MaterialTooLargeError", "MaterialNameConflictError"} <= speaking, speaking
    missing, chinese = [], []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", None)) in speaking):
                continue
            english = [k.value for k in node.keywords if k.arg == "en"]
            if not english:
                missing.append(f"{path.relative_to(CORE.parent)}:{node.lineno}")
            for value in english:
                for c in ast.walk(value):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str) and HAN.search(c.value):
                        chinese.append(f"{path.relative_to(CORE.parent)}:{c.lineno}")
    assert not missing, "这些地方只写了中文：\n" + "\n".join(missing)
    assert not chinese, "英文那一侧写着中文：\n" + "\n".join(chinese)


def test_a_material_error_cannot_be_raised_in_one_language() -> None:
    from core import materials

    with pytest.raises(TypeError):
        materials.MaterialError("只有中文")  # type: ignore[call-arg]
    refused = materials.MaterialError("材料需要一个非空文件名", en="A material needs a non-empty file name")
    assert refused.said_in("en") == "A material needs a non-empty file name"
    assert refused.said_in("zh") == str(refused)

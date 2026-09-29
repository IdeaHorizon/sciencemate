"""扫盘闸：默认数据根「在哪」只许算一次。

`Path.home() / ".harness-framework"`（以及 Windows 上的 `%LOCALAPPDATA%\\afs`）是**默认
根**的算法。它散在各处抄的时候，POSIX 上碰巧都一致所以看不出问题，Windows 上加了分支
就会分叉，而分叉是 08-21 丢 43 个会话的形状。收口之后，这个默认算法只能出现在两处：
`core/paths.py`（harness 侧唯一答案）与 `platform/backend/app/config.py`（后端在导入期就
要它、那时 core 不一定在 sys.path 上，逐字一致由 test_data_root_agreement 钉住）。

用 AST 找「`Path.home()` 与字符串 `.harness-framework` 相除」这个**代码表达式**，不扫
文档串里对 `~/.harness-framework` 的提及。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN = [ROOT / "core", ROOT / "shared", ROOT / "nodes",
        ROOT / "platform_runtime.py", ROOT / "chat.py", ROOT / "platform" / "backend" / "app"]
AUTHORIZED = {
    ROOT / "core" / "paths.py",
    # 后端那一处从 config.py 搬到了零依赖的 data_root_default.py：launcher 在导入
    # config 之前就要这个答案（自更新的载荷指针住在数据根里）。config._default_data_root
    # 现在只是调它的薄壳，不再含算法 —— 还是两处，不是三处。
    ROOT / "platform" / "backend" / "app" / "data_root_default.py",
}


def _computes_the_default(node: ast.AST) -> bool:
    # 匹配 `<...Path.home()...> / ".harness-framework"`
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
        return False
    right = node.right
    if not (isinstance(right, ast.Constant) and right.value == ".harness-framework"):
        return False
    # 左边这一路里出现 Path.home() 调用即可（`Path.home()` 或 `str(Path.home() / x)` 都算）
    for sub in ast.walk(node.left):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "home"):
            return True
    return False


def _hits(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [f"{path.relative_to(ROOT)}:{n.lineno}"
            for n in ast.walk(tree) if _computes_the_default(n)]


def _runtime_files():
    for entry in SCAN:
        if entry.is_file():
            yield entry
        else:
            for p in entry.rglob("*.py"):
                if "tests" not in p.parts:
                    yield p


def test_the_scanner_sees_the_authorized_default():
    assert _hits(ROOT / "core" / "paths.py"), "扫描器认不出 core/paths.py 里的默认算法，守卫会静默归零"


def test_only_two_places_compute_the_default_data_root():
    offenders = [h for f in _runtime_files() if f not in AUTHORIZED for h in _hits(f)]
    assert not offenders, (
        "默认数据根的算法（Path.home() / '.harness-framework'）只能在 core/paths.py 与 "
        "app/data_root_default.py 里 —— 别再抄一份（Windows 上会分叉）。改调 core.paths.home() / "
        "default_home()：\n  " + "\n  ".join(offenders)
    )

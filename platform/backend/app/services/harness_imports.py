"""让后端进程能 import harness 侧的模块（`core.*` / `shared.*` / `nodes.*`）。

后端和 harness 是同一个运行时里的两半：打包后装进同一个 site-packages，开发时
是同一个源码 checkout。后端**不复制** harness 的机制（文件锁、路径布局、文献
归档……），要用就 import 真的那份 —— 前提是它在 sys.path 上。这里是唯一回答
"harness 根在哪、怎么让它可导入"的地方：

* 运行时：`HARNESS_ROOT`（launcher 的 `prepare_the_environment()` 起服务前一定
  设好；打包布局由 `find_the_harness()` 算出后也经它发布）；
* 测试 / 从源码跑：本文件相对仓库根的位置。

**追加、不插前**：后端自己的 `tests` 是正规包、标准库 `platform` 是正规模块，
harness 根下同名的两个目录都没有 `__init__.py`，抢不过它们。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["HarnessNotImportable", "harness_root", "ensure_harness_importable", "harness_module"]


class HarnessNotImportable(RuntimeError):
    """找不到一个像 harness checkout 的目录（`core/` 与 `shared/` 都在）。"""


def _looks_like_the_harness(root: Path) -> bool:
    return (root / "core").is_dir() and (root / "shared").is_dir()


def harness_root() -> Path | None:
    configured = os.environ.get("HARNESS_ROOT", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser().resolve())
    # platform/backend/app/services/harness_imports.py → 仓库根
    candidates.append(Path(__file__).resolve().parents[4])
    for root in candidates:
        if _looks_like_the_harness(root):
            return root
    return None


def ensure_harness_importable() -> Path:
    root = harness_root()
    if root is None:
        raise HarnessNotImportable(
            "harness checkout not found: set HARNESS_ROOT to the directory that "
            "contains core/ and shared/"
        )
    text = str(root)
    if text not in sys.path:
        sys.path.append(text)
    return root


def harness_module(name: str):
    """import 一个 harness 侧模块（``shared.lib.process_control`` 之类）。

    后端模块在**用到的那一刻**调它，而不是顶层 import：后端的测试进程里 harness 根
    不在 sys.path 上，顶层 import 会让整个后端在测试里起不来（2026-08-13 同款）。
    """
    ensure_harness_importable()
    import importlib

    return importlib.import_module(name)

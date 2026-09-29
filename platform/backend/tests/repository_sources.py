"""「哪些文件是这个仓库自己的源码」—— 扫盘闸共用的那一个答案。**问 git。**

## 为什么是 git

这个模块此前用一组**形状启发式**回答这个问题：跳点开头的目录、跳 `__pycache__` /
`node_modules`、认「装好的解释器」（有 `pyvenv.cfg`，或同时有 `bin/python*` 与
`lib/python*`）、认 `.app` 目录、再 `exec` 一遍打包脚本去问它把产物写在哪。

每一条都是在**重新推导**一件 git 已经确切知道的事。代价一路付出来：

* 2026-09-07 往 `.app` 里塞了一份随包 git，扫盘闸读到 CPython 标准库里那些**故意
  畸形编码**的测试文件，`test_a_stream_cannot_outlive_the_server` 当场
  `UnicodeDecodeError` —— 与它要测的东西毫无关系。
* 2026-09-09（#878）：为了排除 `dist/`，`_where_the_packager_writes()` 会 `exec`
  整个打包器。打包器顶层某次多了一句 `sys.path.insert` + `from scripts.package
  import ...`，而 pytest 收集阶段已经把**后端的** `scripts` 包放进了 `sys.modules`
  —— 打包器 import 炸 → `source_files()` 炸 → **每一条扫盘闸一起报错**，而报错落在
  离病因很远的地方（谁会因为 `test_a_stream_cannot_outlive_the_server` 红了就去看
  打包器的 import？）。

启发式还漏过东西：判据本身也知道这一点 —— 它当时配了一条
`test_the_walk_sees_exactly_what_git_tracks` 去和 `git ls-files` **交叉核对**，
docstring 明说「git 在这里是**对照**，不是机制」。

一个判据需要另一个真相源来核对自己有没有算对，说明**那个真相源才该是机制**。
所以这里改成直接问 git，那条交叉核对随之删除（它变成了 git 跟自己比）。

## 口径：tracked ∪ (untracked − ignored)

`git ls-files --cached --others --exclude-standard`。

* **tracked** —— 已经在库里的源码；
* **untracked 但没被 ignore** —— 刚写出来还没 `git add` 的文件。它们也是我们的源码，
  扫盘闸必须看得见；只认 tracked 的话，一个新文件里的违规能一路绿到 add 为止。
* **ignored 一律不在内** —— `dist/`、`.venv/`、`node_modules/`、随包解释器、`.app`
  产物全都写在 `.gitignore` 里。**排除规则不再有第二份**，它就是 `.gitignore`。

git 用不了就**抛**，不退回遍历文件树：扫盘闸扫到空集是**全绿着关掉**，那正是
「没执行 ≈ 执行了没效果」最贵的那种形态。
"""
from __future__ import annotations

import subprocess
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

#: 仓库根（platform/backend/tests → 上溯三级）。
REPO_ROOT = Path(__file__).resolve().parents[3]


class RepositorySourcesUnavailable(RuntimeError):
    """问不到 git —— 扫盘闸没有语料可扫，必须响，不能静静地全绿。"""


@lru_cache(maxsize=None)
def _ours(pattern: str) -> tuple[Path, ...]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", pattern],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositorySourcesUnavailable(
            f"扫盘闸问不到 git（{exc}）——没有语料就没有判据，不许静静通过"
        ) from exc
    if out.returncode != 0:
        raise RepositorySourcesUnavailable(
            f"扫盘闸问不到 git：{(out.stderr or '').strip()[:200]}"
        )
    return tuple(Path(name) for name in out.stdout.split("\0") if name)


def source_files(pattern: str) -> Iterator[tuple[Path, str]]:
    """仓库自己的源码，按 glob 取。产出 `(相对路径, 文本)`。

    读文件一律 `errors="replace"`：一个非 UTF-8 的字节不该让一道与编码无关的闸崩掉。
    列表里的文件可能刚被删掉（`--others` 与真实删除之间有窗口），跳过即可。
    """
    for relative in _ours(pattern):
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        yield relative, path.read_text(encoding="utf-8", errors="replace")

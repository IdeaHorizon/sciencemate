"""扫盘闸的语料 = git 说的那些文件。不是遍历文件树猜出来的（#878）。

## 病例

`source_files()` 曾经走 `REPO_ROOT.rglob(pattern)`，靠一组形状启发式排除非源码：
点开头的目录、`__pycache__`/`node_modules`、「装好的解释器」、`.app` 目录、以及
**`exec` 一遍打包脚本**去问它把产物写在哪。

两次事故都由这条路来的：

* 打完包的工作树里跑后端全量 → **13 红**，把 `dist/`（1.4 GB，含后端源码完整副本）
  挪出去再跑 → 5 红。「只许一处」类的闸看到了第二份、第三份。
* 打包器顶层多了一句 `sys.path.insert` + `from scripts.package import ...`，而 pytest
  收集阶段已经把**后端的** `scripts` 包放进 `sys.modules` → 打包器 import 炸 →
  `source_files()` 炸 → **每一条扫盘闸一起报错**，报错落在离病因很远的地方。

## 判据

排除规则只有一份，就是 `.gitignore`。所以这里只验两件事：

1. 被 git ignore 的东西**不在语料里**（无论它长什么样、放在哪）；
2. 没被 ignore 的源码**在语料里**，哪怕它还没 `git add`（新写的文件里的违规不该等到
   add 之后才被闸看见）。

第三条是 fail-closed：问不到 git 要**抛**，不能返回空集 —— 扫到空集是「全绿着把闸
关掉」，比红更贵。
"""
from __future__ import annotations

import subprocess

import pytest

from tests.repository_sources import REPO_ROOT, RepositorySourcesUnavailable, source_files


def _ignored(relative: str) -> bool:
    return subprocess.run(
        ["git", "check-ignore", "-q", relative],
        cwd=REPO_ROOT, capture_output=True, check=False,
    ).returncode == 0


def test_nothing_git_ignores_is_in_the_corpus():
    """`dist/`、`.venv/`、随包解释器、`.app` 产物 —— 一条规则全覆盖。"""
    offenders = [str(rel) for rel, _text in source_files("*.py") if _ignored(str(rel))]
    assert not offenders, (
        "语料里混进了被 git ignore 的文件：" + ", ".join(offenders[:5])
        + "\n排除规则只该有一份（.gitignore）。"
    )


def test_a_brand_new_file_is_already_in_the_corpus():
    """还没 `git add` 的源码也算我们的 —— 否则新文件里的违规能一路绿到 add 为止。"""
    from tests.repository_sources import _ours

    probe = REPO_ROOT / "platform" / "backend" / "app" / "_scan_corpus_probe.py"
    probe.write_text("# transient probe\n", encoding="utf-8")
    _ours.cache_clear()
    try:
        seen = {str(rel) for rel, _text in source_files("*.py")}
        assert str(probe.relative_to(REPO_ROOT)) in seen, (
            "新写的、未 add 的源码不在语料里 —— 闸看不见它"
        )
    finally:
        probe.unlink(missing_ok=True)
        _ours.cache_clear()


def test_the_corpus_is_not_empty():
    """非空自检：扫到空集时所有闸都会**全绿着**空转。"""
    count = sum(1 for _ in source_files("*.py"))
    assert count > 500, f"只扫到 {count} 个源文件 —— 判据多半在空转"


def test_git_being_unavailable_is_loud(monkeypatch):
    """问不到 git 要抛，不许悄悄返回空集。"""
    from tests import repository_sources

    repository_sources._ours.cache_clear()

    def boom(*_args, **_kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(repository_sources.subprocess, "run", boom)
    with pytest.raises(RepositorySourcesUnavailable):
        list(repository_sources.source_files("*.py"))
    repository_sources._ours.cache_clear()


def test_the_helper_does_not_execute_the_packager():
    """扫盘 helper 不许再 `exec` 打包器。

    那条路让**打包器任何导入期副作用**连坐全部扫盘闸，而且报错指不回病因。
    现在 `dist/` 由 `.gitignore` 排除，问打包器"你写在哪"这件事整个不需要了。
    """
    source = (REPO_ROOT / "platform" / "backend" / "tests" / "repository_sources.py").read_text(
        encoding="utf-8")
    assert "exec_module" not in source
    assert "build_mac_app" not in source

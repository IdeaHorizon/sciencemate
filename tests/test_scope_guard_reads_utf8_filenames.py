"""scope 检查器必须逐字读到文件名，中文名不得被判成越界（#954）。

现场：jicq 在自己目录 `nodes/literature/` 下加了中文名的资料文件，push 成功，
但 PR 一建就被 `forgejo_merge_pr.sh` 自动关掉，理由是"越界"。

真身是 `git log --name-only` 默认按 `core.quotepath=true` 吐路径：

    "nodes/literature/\\344\\272\\214\\347\\272\\247\\345\\255\\246\\347\\247\\221.tsv"

这个串匹配不上 `nodes/literature/**`，于是自己的文件被判成别人的地盘。
表现像"权限没生效"，极难自诊。

判据落在**真跑一次检查器**上：造一个带中文名文件的真仓库，用真 argv 调
`get_changed_files()`，断言拿回来的就是原始文件名、并且 scope 判定为零越界。
不 monkeypatch git —— 替身会把这个 bug 整个藏起来（bug 就在 git 的输出格式里）。
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_pr_scope  # noqa: E402


#: 现场那个名字的形状：纯中文 + 扩展名。
_CN_NAME = "二级学科映射.tsv"


def _repo_with_a_chinese_filename(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "nodes" / "literature").mkdir(parents=True)
    g = ["git", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(g + ["config", "user.email", "t@t"], check=True)
    subprocess.run(g + ["config", "user.name", "t"], check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "base"], check=True)
    subprocess.run(g + ["checkout", "-qb", "feat"], check=True)
    (repo / "nodes" / "literature" / _CN_NAME).write_text("a\tb\n", encoding="utf-8")
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "add chinese-named file"], check=True)
    return repo


def test_a_chinese_filename_comes_back_verbatim(tmp_path, monkeypatch):
    repo = _repo_with_a_chinese_filename(tmp_path)
    monkeypatch.chdir(repo)

    files = check_pr_scope.get_changed_files("main", "feat")

    assert f"nodes/literature/{_CN_NAME}" in files, (
        "中文文件名没有逐字回来 —— 它会匹配不上任何 glob，被判成越界。"
        f" 实际拿到：{files}"
    )
    assert not any(f.startswith('"') for f in files), (
        f"路径还带着 git 的转义引号：{files}")


def test_the_owner_is_not_told_they_went_out_of_scope(tmp_path, monkeypatch):
    """把真实判定跑完一遍 —— 这条才是用户撞到的那个后果。"""
    repo = _repo_with_a_chinese_filename(tmp_path)
    monkeypatch.chdir(repo)

    files = check_pr_scope.get_changed_files("main", "feat")
    allowed = ["nodes/literature/**"]
    violations = [
        f for f in files
        if not any(fnmatch.fnmatch(f, pat) for pat in allowed)
    ]

    assert violations == [], (
        f"owner 自己目录里的文件被判成越界 —— PR 会被自动关掉。越界清单：{violations}")


def test_a_still_quoted_path_stops_instead_of_guessing(capsys):
    """万一引号没去掉（名字里带双引号时 git 照样会引），必须吵出来而不是照字面判。"""
    with pytest.raises(SystemExit) as e:
        check_pr_scope._unquoted_or_die('"nodes/literature/\\344\\272\\214.tsv"',
                                        "git log --name-only")
    assert e.value.code == 2
    assert "误判成越界" in capsys.readouterr().err

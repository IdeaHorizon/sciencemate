"""用户交来的文件走**真入口**：加进会话、发布、开新会话，全程读得到。

这些测试刻意不直接调 `core.materials.materialize` —— 它们走
`GitProjectRepository` 的生产路径。只测模块函数的话，"materialize 的调用点被
删掉"这种缺陷会原样活着而 CI 全绿（`feedback_fix_landed_on_a_path_nobody_runs`）。

钉住的是 2026-09-04 那个洞：会话附件落 gitignored 的 runtime 目录（publish
带不走），项目材料直接 commit canonical main（**已存在的会话**读不到）。用户
在会话里传的文件正好落进两者中间，谁都读不到。
"""
from __future__ import annotations

import hashlib
import io
import subprocess
from pathlib import Path

import pytest

from app.services.harness_contract import materials_module
from app.services.project_repository import (
    GitProjectRepository,
    ProjectRepositoryError,
)

# ⚠️ 模块**在测试里取**，不写 `from core import materials`。
#
# `harness_contract._contract_module` 会把在册的 `core` 逐出再按 HARNESS_ROOT
# 重装（那是它存在的理由：`sys.modules` 是进程全局的，来路不明的同名 `core`
# 会静默赢）。于是"直接 import 的 core.materials"和"后端加载的那个"是两个
# **不同的模块对象**，`MaterialTooLargeError` 也就是两个不同的类，
# `pytest.raises` 抓不到。这条注释比那次红便宜。


def _materials():
    return materials_module()


def _repository(tmp_path: Path) -> GitProjectRepository:
    return GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")


def _project(repository: GitProjectRepository, project_id: str = "p1"):
    return repository.initialize_project(
        project_id=project_id,
        name="User files",
        description="Whether a file the user hands over is actually readable.",
        research_domain="materials science",
        owner_id="owner",
    )


def _add(repository, project_id, session_id, name, payload, **kwargs):
    return repository.add_session_material(
        project_id,
        session_id,
        filename=name,
        stream=io.BytesIO(payload),
        uploaded_by="owner",
        **kwargs,
    )


def test_a_file_added_to_a_session_is_readable_in_that_session(tmp_path: Path) -> None:
    """老实现在这里就断了：材料 commit 到 main，这个会话的分支早分出去了。"""
    repository = _repository(tmp_path)
    _project(repository)
    workspace = repository.ensure_session_workspace(
        project_id="p1", session_id="s1", base_commit=None,
        title="First", created_by="owner",
    )
    payload = b"podsys log lines\n" * 1000
    reference, commit = _add(repository, "p1", "s1", "podsys_asc_log.tar.gz", payload)

    assert commit and commit != workspace.head_commit, "指针要真的提交进会话分支"
    entity = Path(workspace.path) / reference.path
    assert entity.read_bytes() == payload, (
        "文件必须在**这个**会话的工作区里就能读到 —— agent 下一轮用的就是这条路径"
    )
    assert repository.session_status("p1", "s1").clean is True, (
        "加一份文件不该让会话工作树变脏，否则平台自己的产物写入闸会被自己顶住"
    )


def test_the_file_shows_up_in_the_file_tree_as_one_row(tmp_path: Path) -> None:
    """界面上一份文件占一行 —— 它自己那行，真名真大小，不是 `.ref` 双胞胎。"""
    repository = _repository(tmp_path)
    _project(repository)
    repository.ensure_session_workspace(
        project_id="p1", session_id="s1", base_commit=None,
        title="First", created_by="owner",
    )
    payload = b"a,b\n" * 500
    reference, _ = _add(repository, "p1", "s1", "table.csv", payload, note="第三方给的表")

    listing = repository.file_tree(
        "p1", session_id="s1", path=_materials().MATERIALS_RELATIVE,
    )
    material_rows = [
        row for row in listing["entries"]
        if str(row["path"]).startswith(f"{_materials().MATERIALS_RELATIVE}/")
    ]
    assert [row["path"] for row in material_rows] == [reference.path], (
        f"材料目录下应当只端出材料本身那一行，实际：{[r['path'] for r in material_rows]}"
    )
    [row] = material_rows
    assert row["sizeBytes"] == len(payload), "大小要是文件的大小，不是指针的大小"
    assert row["status"] == "committed"
    assert row["note"] == "第三方给的表"
    assert row["missing"] is False
    assert row["sha256"] == reference.sha256


def test_publishing_carries_the_file_to_a_brand_new_session(tmp_path: Path) -> None:
    """跨会话复用走 publish —— 和其它任何改动同一条路，没有第二套账。"""
    repository = _repository(tmp_path)
    project = _project(repository)
    repository.ensure_session_workspace(
        project_id="p1", session_id="s1", base_commit=None,
        title="First", created_by="owner",
    )
    payload = b"corpus" * 5_000
    reference, _ = _add(repository, "p1", "s1", "corpus.tar.gz", payload)

    published = repository.publish_linear(
        project_id="p1", session_id="s1",
        expected_main_commit=project.head_commit,
        paths=[reference.ref_path, f"{_materials().MATERIALS_RELATIVE}/.gitignore"],
        message="Publish the corpus the user handed over",
        change_set_id="change-1", actor_id="owner",
    )
    canonical = Path(repository.status("p1").path)
    assert (canonical / reference.path).read_bytes() == payload, (
        "canonical 主干也要拿到字节 —— 项目还没有会话时界面就是从这儿读的"
    )

    later = repository.ensure_session_workspace(
        project_id="p1", session_id="s2", base_commit=published,
        title="Second", created_by="owner",
    )
    entity = Path(later.path) / reference.path
    assert entity.is_file(), (
        "新会话必须拿得到字节。`.ref` 随分支过来，字节在池里 —— "
        "`ensure_session_workspace` 里那一步 materialize 就是把它接回来的"
    )
    assert hashlib.sha256(entity.read_bytes()).hexdigest() == reference.sha256
    assert repository.session_status("p1", "s2").clean is True


def test_one_copy_of_the_bytes_serves_every_session(tmp_path: Path) -> None:
    """一份 800MB 的材料不该在每个会话里各占一份盘。"""
    repository = _repository(tmp_path)
    project = _project(repository)
    repository.ensure_session_workspace(
        project_id="p1", session_id="s1", base_commit=None,
        title="First", created_by="owner",
    )
    reference, _ = _add(repository, "p1", "s1", "shared.bin", b"shared" * 2_000)
    published = repository.publish_linear(
        project_id="p1", session_id="s1",
        expected_main_commit=project.head_commit,
        paths=[reference.ref_path, f"{_materials().MATERIALS_RELATIVE}/.gitignore"],
        message="Publish", change_set_id="c1", actor_id="owner",
    )
    second = repository.ensure_session_workspace(
        project_id="p1", session_id="s2", base_commit=published,
        title="Second", created_by="owner",
    )
    first_inode = (Path(repository.session_path("p1", "s1")) / reference.path).stat().st_ino
    second_inode = (Path(second.path) / reference.path).stat().st_ino
    assert first_inode == second_inode, "两个会话看到的应当是同一个 inode"


def test_the_commit_is_recognised_as_platform_made(tmp_path: Path) -> None:
    """没有 `Session-ID` 与平台身份，下一次 checkpoint 会把它判成越权。

    2026-08-13 实测过这个形状：平台自己的提交被下一次 checkpoint 判成外人，
    会话当场失败，而且再也自己好不了。
    """
    repository = _repository(tmp_path)
    _project(repository)
    workspace = repository.ensure_session_workspace(
        project_id="p1", session_id="s1", base_commit=None,
        title="First", created_by="owner",
    )
    reference, commit = _add(repository, "p1", "s1", "x.bin", b"x" * 16)
    assert repository._commits_not_made_by_platform(
        Path(workspace.path),
        expected=workspace.head_commit,
        current=commit,
        session_id="s1",
    ) == [], "这条提交必须被认成平台自己做的"

    body = subprocess.run(
        ["git", "-C", str(workspace.path), "log", "-1", "--format=%B"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Session-ID: s1" in body
    assert f"Material-SHA256: {reference.sha256}" in body


def test_only_the_material_paths_are_committed(tmp_path: Path) -> None:
    """pathspec 限定：别人留在工作树里的未提交改动一个都不许被顺手带走。"""
    repository = _repository(tmp_path)
    _project(repository)
    workspace = repository.ensure_session_workspace(
        project_id="p1", session_id="s1", base_commit=None,
        title="First", created_by="owner",
    )
    stray = Path(workspace.path) / "experiments" / "half-written.md"
    stray.write_text("# 正在写，还没提交\n", encoding="utf-8")

    _add(repository, "p1", "s1", "note.txt", b"note")

    status = subprocess.run(
        ["git", "-C", str(workspace.path), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "experiments/half-written.md" in status, "别人的未提交改动应当**原样**还在那儿"


def test_the_size_cap_is_enforced_on_the_stream(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _project(repository)
    repository.ensure_session_workspace(
        project_id="p1", session_id="s1", base_commit=None,
        title="First", created_by="owner",
    )
    with pytest.raises(_materials().MaterialTooLargeError) as raised:
        _add(repository, "p1", "s1", "huge.bin", b"x" * 4_000, max_bytes=1_000)
    assert raised.value.max_bytes == 1_000
    rows = [
        row
        for row in repository.file_tree("p1", session_id="s1")["entries"]
        if str(row["path"]).startswith(f"{_materials().MATERIALS_RELATIVE}/")
    ]
    assert rows == [], "被拒的文件不该在树里留下任何一行"


def test_a_missing_session_is_a_clean_error(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _project(repository)
    with pytest.raises(ProjectRepositoryError):
        _add(repository, "p1", "nope", "x.bin", b"x")


@pytest.fixture(autouse=True)
def _harness_checkout(monkeypatch):
    """把 `HARNESS_ROOT` 指向本仓库自己的 harness checkout。

    材料这一层归 harness（`core/materials`），平台经 `harness_contract` 调它。
    测试进程默认没有这个变量 —— 与 `harness_domain_registry` 同一个理由和同一
    种做法：**给真路径、走真加载**，不 monkeypatch 掉那一层。替身遮住的正是
    "平台读得到 harness 的那份实现吗"这个接缝，而那是这条契约唯一值得验的东西。
    """
    from app.config import settings
    from app.services import harness_contract

    repository_root = Path(__file__).resolve().parents[3]
    assert (repository_root / "core" / "materials.py").is_file(), repository_root
    monkeypatch.setattr(settings, "harness_root", str(repository_root))
    harness_contract._harness_root.cache_clear()
    yield
    harness_contract._harness_root.cache_clear()

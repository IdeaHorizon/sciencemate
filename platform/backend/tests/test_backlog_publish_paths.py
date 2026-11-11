"""Regression coverage for committed paths at checkpoint/publish boundaries."""

from pathlib import Path

import pytest

from app.services.project_repository import GitProjectRepository, ProjectRepositoryError


def _session(tmp_path):
    repo = GitProjectRepository(tmp_path / "repos", tmp_path / "worktrees")
    project = repo.initialize_project(
        project_id="unicode",
        name="Unicode",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    session = repo.ensure_session_workspace(
        project_id="unicode",
        session_id="session",
        base_commit=project.head_commit,
        title="Write",
        created_by="owner",
    )
    return repo, project, Path(session.path)


def _checkpoint(repo, paths):
    return repo.checkpoint_session_workspace(
        project_id="unicode",
        session_id="session",
        node_type="experiment",
        run_id="run",
        run_status="completed",
        workspace_prefix="experiments",
        paths=paths,
        expected_head_commit=repo.session_status("unicode", "session").head_commit,
    )


def _publish(repo, project, paths):
    return repo.publish_linear(
        project_id="unicode",
        session_id="session",
        expected_main_commit=project.head_commit,
        paths=paths,
        message="Publish",
        change_set_id="publish",
        actor_id="owner",
    )


def test_unicode_and_space_paths_survive_checkpoint_publish_and_reset(tmp_path):
    repo, project, root = _session(tmp_path)
    contents = {
        "experiments/ascii.txt": b"control\n",
        "experiments/工作区 空格/结果 A/summary.json": "结果\n".encode(),
        "experiments/工作区 空格/输入.csv": b"id,value\n001,1.250\n",
    }
    for name, data in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    checkpoint = _checkpoint(repo, list(contents))
    paths = repo.changed_paths("unicode", "session")
    assert paths == sorted(contents)
    published = _publish(repo, project, paths)
    assert repo.session_status("unicode", "session").head_commit == published
    for name, data in contents.items():
        assert (root / name).read_bytes() == data
        assert (Path(project.path) / name).read_bytes() == data
    assert checkpoint.commit_sha in repo._git(
        Path(project.path),
        "show-ref",
        "--verify",
        f"refs/audit/sessions/session/{checkpoint.commit_sha}",
    )


def test_publish_refuses_missing_file_that_is_still_in_session_commit(tmp_path):
    repo, project, root = _session(tmp_path)
    name = "experiments/结果.txt"
    (root / name).write_text("evidence", encoding="utf-8")
    checkpoint = _checkpoint(repo, [name])
    (root / name).unlink()
    with pytest.raises(ProjectRepositoryError, match="missing|Missing"):
        _publish(repo, project, repo.changed_paths("unicode", "session"))
    assert repo.session_status("unicode", "session").head_commit == checkpoint.commit_sha
    assert repo.status("unicode").head_commit == project.head_commit


@pytest.mark.parametrize(
    "data,secret",
    [
        (b"#define SUBLANG_SLOVAK_SLOVAKIA 0x01", False),
        (b'const char *name = "AKIA";', False),
        (b"access_key=AKIA0123456789ABCDEF", True),
        (b"access_key=ASIA0123456789ABCDEF", True),
        (b"prefixAKIA0123456789ABCDEFsuffix", False),
        (b"-----BEGIN OPENSSH PRIVATE KEY-----", True),
    ],
)
def test_secret_scanner_checks_credential_shape(data, secret):
    assert GitProjectRepository._secret_like(data) is secret

"""A chosen folder is the project's address, across creation, restart and Git I/O."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.services.project_locations import ProjectLocationError, list_project_folders
from app.services.project_repository import (
    GitProjectRepository,
    ProjectRepositoryError,
    get_project_repository,
)
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401


def _repository(tmp_path):
    return GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")


def _create(repository, *, project_id="project-1", root_folder=None):
    return repository.initialize_project(
        project_id=project_id,
        name="Research",
        description=None,
        research_domain=None,
        owner_id="owner",
        root_folder=root_folder,
    )


def test_default_and_existing_projects_keep_their_addresses(tmp_path):
    repository = _repository(tmp_path)
    assert repository.project_path("legacy") == tmp_path / "repositories/legacy"
    project = _create(repository)
    assert Path(project.path) == tmp_path / "repositories/project-1"
    assert (
        repository.session_path("project-1", "session-1")
        == tmp_path / "worktrees/project-1/session-1"
    )


@pytest.mark.parametrize("existing_empty", [False, True])
def test_chosen_root_survives_a_fresh_repository_instance(tmp_path, existing_empty):
    root = tmp_path / "另一个卷" / "Research with spaces"
    if existing_empty:
        root.mkdir(parents=True)
    first = _create(_repository(tmp_path), root_folder=str(root))
    restarted = _repository(tmp_path)
    assert restarted.project_path("project-1") == root
    assert restarted.status("project-1").head_commit == first.head_commit
    assert (root / "PROJECT.md").is_file()
    assert not (tmp_path / "repositories/project-1").exists()
    assert _create(restarted, root_folder=str(root)).head_commit == first.head_commit
    assert _create(restarted).head_commit == first.head_commit


def test_sessions_lanes_and_published_files_use_the_chosen_root(tmp_path):
    root = tmp_path / "chosen"
    project = _create(_repository(tmp_path), root_folder=str(root))
    repository = _repository(tmp_path)
    workspace = repository.ensure_session_workspace(
        project_id="project-1",
        session_id="session-1",
        base_commit=project.head_commit,
        title="Experiment",
        created_by="owner",
    )
    assert Path(workspace.path).is_relative_to(root)
    lane = repository.open_child_lane(
        project_id="project-1",
        session_id="session-1",
        lane_id="lane-1",
        node_type="experiment",
    )
    assert Path(lane.path).is_relative_to(root)
    assert not Path(lane.path).is_relative_to(Path(workspace.path))
    output = Path(workspace.path) / "experiments/result.txt"
    output.write_text("a measured result", encoding="utf-8")
    repository.checkpoint_session_workspace(
        project_id="project-1",
        session_id="session-1",
        node_type="experiment",
        run_id="run-1",
        run_status="completed",
        workspace_prefix="experiments",
        paths=["experiments/result.txt"],
        expected_head_commit=workspace.head_commit,
    )
    repository.publish_linear(
        project_id="project-1",
        session_id="session-1",
        expected_main_commit=project.head_commit,
        paths=["experiments/result.txt"],
        message="Publish measurement",
        change_set_id="change-1",
        actor_id="owner",
    )
    assert (root / "experiments/result.txt").read_text() == "a measured result"
    assert repository.status("project-1").clean
    assert not (tmp_path / "worktrees").exists()


def test_nonempty_folder_is_rejected_without_touching_its_files(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    original = root / "data.csv"
    original.write_bytes(b"valuable data\x00\xff")
    with pytest.raises(ProjectLocationError):
        _create(_repository(tmp_path), root_folder=str(root))
    assert original.read_bytes() == b"valuable data\x00\xff"
    assert list(root.iterdir()) == [original]


def test_another_project_cannot_adopt_an_existing_repository(tmp_path):
    repository = _repository(tmp_path)
    root = tmp_path / "chosen"
    first = _create(repository, root_folder=str(root))
    with pytest.raises(ProjectLocationError):
        _create(repository, project_id="project-2", root_folder=str(root))
    assert repository.status("project-1").head_commit == first.head_commit


def test_location_index_failure_leaves_the_selected_empty_folder_untouched(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    root = tmp_path / "chosen"
    root.mkdir()

    def cannot_register(*_args):
        raise PermissionError("index is not writable")

    monkeypatch.setattr(repository.locations, "register", cannot_register)
    with pytest.raises(PermissionError):
        _create(repository, root_folder=str(root))
    assert root.is_dir() and list(root.iterdir()) == []
    assert not list(tmp_path.glob(".project-1.init-*"))


def test_install_failure_does_not_leave_a_location_reservation(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    root = tmp_path / "chosen"
    original_replace = Path.replace

    def cannot_install(source, destination):
        if source.name.startswith(".project-1.init-"):
            raise PermissionError("destination became unavailable")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", cannot_install)
    with pytest.raises(PermissionError):
        _create(repository, root_folder=str(root))
    assert _repository(tmp_path).locations.custom_root("project-1") is None
    assert not root.exists()
    assert not list(tmp_path.glob(".project-1.init-*"))


def test_relative_file_and_nested_git_paths_are_rejected(tmp_path):
    repository = _repository(tmp_path)
    with pytest.raises(ProjectLocationError):
        _create(repository, root_folder="relative/folder")
    file = tmp_path / "not-a-directory"
    file.write_text("keep")
    with pytest.raises(ProjectLocationError):
        _create(repository, root_folder=str(file))
    root = Path(_create(repository).path)
    with pytest.raises(ProjectLocationError):
        _create(repository, project_id="project-2", root_folder=str(root / "nested"))
    assert not (root / "nested").exists()


def test_an_unavailable_custom_root_never_falls_back_to_the_default(tmp_path):
    repository = _repository(tmp_path)
    root = tmp_path / "volume" / "chosen"
    _create(repository, root_folder=str(root))
    root.rename(root.with_name("unmounted"))
    restarted = _repository(tmp_path)
    assert restarted.project_path("project-1") == root
    with pytest.raises(ProjectRepositoryError):
        restarted.status("project-1")
    with pytest.raises(ProjectLocationError):
        _create(restarted)
    assert not root.exists()
    assert not (tmp_path / "repositories/project-1").exists()


def test_folder_navigation_lists_directories_and_counts_hidden_files(tmp_path):
    (tmp_path / "a-folder").mkdir()
    (tmp_path / "a-file").write_text("file")
    listing = list_project_folders(tmp_path / "defaults", str(tmp_path))
    assert listing["directories"] == [{"name": "a-folder", "path": str(tmp_path / "a-folder")}]
    assert not listing["empty"]
    assert list_project_folders(tmp_path, str(tmp_path / "a-folder"))["empty"]
    (tmp_path / "a-folder/.hidden").touch()
    assert not list_project_folders(tmp_path, str(tmp_path / "a-folder"))["empty"]


def _relocation_script():
    path = Path(__file__).resolve().parents[1] / "scripts/relocate_project_data.py"
    spec = importlib.util.spec_from_file_location("relocate_project_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_moving_app_data_moves_default_projects_and_preserves_chosen_roots(tmp_path, monkeypatch):
    repository = get_project_repository()
    root = tmp_path / "chosen"
    custom = _create(repository, root_folder=str(root))
    default = _create(repository, project_id="project-2")
    recorded = []
    for project_id, session_id, head in (
        ("project-1", "session-1", custom.head_commit),
        ("project-2", "session-2", default.head_commit),
    ):
        workspace = repository.ensure_session_workspace(
            project_id=project_id,
            session_id=session_id,
            base_commit=head,
            title="Research",
            created_by="owner",
        )
        recorded.append((session_id, project_id, workspace.path))
    script = _relocation_script()
    mapping = {}

    async def recorded_paths():
        return recorded

    async def rewrite(paths):
        mapping.update(paths)
        return len(paths)

    target = tmp_path / "moved-app-data"
    monkeypatch.setattr(script, "_recorded_paths", recorded_paths)
    monkeypatch.setattr(script, "_rewrite", rewrite)
    monkeypatch.setattr(sys, "argv", ["relocate", "--to", str(target), "--apply"])
    asyncio.run(script.main())

    relocated = GitProjectRepository(target / "project-repositories", target / "project-worktrees")
    assert relocated.project_path("project-1") == root
    assert mapping["session-1"] == recorded[0][2]
    assert mapping["session-2"] == str(relocated.session_path("project-2", "session-2"))
    assert relocated.session_status("project-1", "session-1").head_commit
    assert relocated.session_status("project-2", "session-2").head_commit
    assert (root / ".git").is_dir()
    assert not Path(default.path).exists()


@pytest.mark.parametrize("apply", [False, True])
def test_moving_app_data_keeps_addresses_even_without_active_sessions(tmp_path, monkeypatch, apply):
    repository = get_project_repository()
    root = tmp_path / "chosen"
    _create(repository, root_folder=str(root))
    script = _relocation_script()

    async def no_sessions():
        return []

    target = tmp_path / "moved-app-data"
    monkeypatch.setattr(script, "_recorded_paths", no_sessions)
    monkeypatch.setattr(
        sys, "argv", ["relocate", "--to", str(target)] + (["--apply"] if apply else [])
    )
    asyncio.run(script.main())
    relocated = GitProjectRepository(target / "project-repositories", target / "project-worktrees")
    assert relocated.locations.custom_root("project-1") == (root if apply else None)
    assert (root / ".git").is_dir()
    if not apply:
        assert not target.exists()


@pytest.mark.asyncio
async def test_creation_detail_and_list_report_the_same_root(
    runtime_client,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "profile", "personal")
    client, _ = runtime_client
    headers = _headers(await _token(client, "researcher@atrium.local"))
    root = tmp_path / "user-selected"
    created = await client.post(
        "/api/v1/projects/",
        headers=headers,
        json={
            "name": "Chosen location",
            "root_folder": str(root),
        },
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["root_folder"] == str(root)
    detail = await client.get(f"/api/v1/projects/{project['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["root_folder"] == str(root)
    listing = await client.get("/api/v1/projects/", headers=headers)
    assert next(p for p in listing.json() if p["id"] == project["id"])["root_folder"] == str(root)
    assert (root / ".git").is_dir()


@pytest.mark.asyncio
async def test_invalid_root_returns_actionable_error_and_no_project(
    runtime_client,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "profile", "personal")
    client, _ = runtime_client
    headers = {
        **_headers(await _token(client, "researcher@atrium.local")),
        "X-Interface-Language": "en",
    }
    root = tmp_path / "nonempty"
    root.mkdir()
    (root / "data.txt").write_text("keep")
    response = await client.post(
        "/api/v1/projects/",
        headers=headers,
        json={
            "name": "Must not exist",
            "root_folder": str(root),
        },
    )
    assert response.status_code == 422
    assert "empty folder" in response.json()["detail"]
    projects = (await client.get("/api/v1/projects/", headers=headers)).json()
    assert all(p["name"] != "Must not exist" for p in projects)


@pytest.mark.asyncio
async def test_org_storage_cannot_be_browsed_or_overridden(
    runtime_client,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "profile", "org")
    client, _ = runtime_client
    headers = _headers(await _token(client, "researcher@atrium.local"))
    assert (await client.get("/api/v1/projects/folders", headers=headers)).json() == {
        "available": False
    }
    response = await client.post(
        "/api/v1/projects/",
        headers=headers,
        json={
            "name": "Server project",
            "root_folder": str(tmp_path / "server"),
        },
    )
    assert response.status_code == 422
    assert not (tmp_path / "server").exists()

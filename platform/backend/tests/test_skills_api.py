"""Real personal-edition API → immutable library → project settings → harness."""

from __future__ import annotations

import io
import stat
import zipfile

import pytest

from app import assembly
from app.config import settings, the_projects_home
from app.main import app
from app.services import skills
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401

SKILL = (
    b"---\nname: external-method\ndescription: A test method\n"
    b"tools_used: [read_file]\n---\n\nFollow the external method.\n"
)


async def _project(client, headers):
    result = await client.get("/api/v1/projects/", headers=headers)
    return result.json()[0]["id"]


async def _upload(client, headers=None, body=SKILL):
    response = await client.post(
        "/api/v1/settings/skills/import",
        headers=headers,
        files=[("files", ("SKILL.md", body, "text/markdown"))],
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_personal_edition_needs_no_account_and_runs_the_saved_assignment(
    runtime_client,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    client, _ = runtime_client
    monkeypatch.setattr(settings, "profile", "personal")
    assembly.install(app)
    try:
        record = await _upload(client)
        preview = await client.get("/api/v1/settings/skills/external-method")
        assert "external method" in preview.json()["content"]
        project = await client.post("/api/v1/projects/", json={"name": "Personal skills"})
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        path = f"/api/v1/projects/{project_id}/skills"
        before = (await client.get(path)).json()
        assert before["bindings"] == []
        assert "experiment" in {n["id"] for n in before["nodes"]}
        binding = {k: record[k] for k in ("owner_id", "name", "revision")}
        binding.update(nodes=["experiment"], enabled=True)
        saved = await client.put(path, json={"revision": before["revision"], "bindings": [binding]})
        assert saved.status_code == 200, saved.text
        assert saved.json()["bindings"] == [binding]
        # The worker receives projects_home explicitly; use that exact boundary.
        monkeypatch.setenv("HARNESS_FRAMEWORK_PROJECTS_HOME", str(the_projects_home()))
        from core.skill_library import snapshot_skills
        from core.state import State

        node = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id=project_id)
        assert "external method" in snapshot_skills(node, "experiment")[0].body_markdown
        conflict = await client.put(path, json={"revision": before["revision"], "bindings": []})
        assert conflict.status_code == 409
        disabled = await client.patch(
            "/api/v1/settings/skills/external-method", json={"enabled": False}
        )
        assert disabled.status_code == 200
        fresh = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id=project_id)
        assert snapshot_skills(fresh, "experiment") == []
        assert snapshot_skills(node, "experiment")
    finally:
        assembly.uninstall(app)


@pytest.mark.asyncio
async def test_libraries_and_project_writes_are_isolated_between_users(runtime_client):  # noqa: F811
    client, _ = runtime_client
    alice = _headers(await _token(client, "researcher@atrium.local"))
    bob = _headers(await _token(client, "colleague@atrium.local"))
    record = await _upload(client, alice)
    assert (await client.get("/api/v1/settings/skills", headers=bob)).json()["skills"] == []
    assert (
        await client.get("/api/v1/settings/skills/external-method", headers=bob)
    ).status_code == 404
    alice_project = await _project(client, alice)
    denied = await client.put(
        f"/api/v1/projects/{alice_project}/skills",
        headers=bob,
        json={"revision": "0" * 64, "bindings": []},
    )
    assert denied.status_code == 403, denied.text
    bob_project = await _project(client, bob)
    path = f"/api/v1/projects/{bob_project}/skills"
    config = (await client.get(path, headers=bob)).json()
    binding = {k: record[k] for k in ("owner_id", "name", "revision")}
    binding.update(nodes=["experiment"], enabled=True)
    guessed = await client.put(
        path, headers=bob, json={"revision": config["revision"], "bindings": [binding]}
    )
    assert guessed.status_code == 404


@pytest.mark.asyncio
async def test_zip_and_folder_import_keep_all_assets_and_old_revisions(runtime_client):  # noqa: F811
    client, _ = runtime_client
    headers = _headers(await _token(client, "researcher@atrium.local"))
    first = await _upload(client, headers)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("my-skill/SKILL.md", SKILL.replace(b"external method.", b"updated method."))
        z.writestr("my-skill/scripts/check.py", "print('ok')\n")
        z.writestr("my-skill/assets/example.bin", b"\x00\xff")
    imported = await client.post(
        "/api/v1/settings/skills/import",
        headers=headers,
        files=[("files", ("bundle.zip", archive.getvalue(), "application/zip"))],
    )
    assert imported.status_code == 201, imported.text
    second = imported.json()
    assert set(second["revisions"]) == {first["revision"], second["revision"]}
    asset = await client.get(
        "/api/v1/settings/skills/external-method",
        headers=headers,
        params={"asset": "scripts/check.py"},
    )
    assert asset.json()["content"] == "print('ok')\n"
    old = await client.get(
        "/api/v1/settings/skills/external-method",
        headers=headers,
        params={"revision": first["revision"]},
    )
    assert "external method" in old.json()["content"]
    folder = await client.post(
        "/api/v1/settings/skills/import",
        headers=headers,
        files=[
            ("files", ("folder/SKILL.md", SKILL, "text/markdown")),
            ("files", ("folder/references/guide.md", b"Reference", "text/markdown")),
        ],
    )
    assert folder.status_code == 201, folder.text
    assert "references/guide.md" in folder.json()["files"]


@pytest.mark.parametrize(
    "entry,symlink",
    [("../escaped", False), ("/absolute", False), ("C:/drive", False), ("scripts/link", True)],
)
def test_archive_cannot_write_outside_the_skill(entry, symlink):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("SKILL.md", SKILL)
        info = zipfile.ZipInfo(entry)
        if symlink:
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, b"../../outside")
    with pytest.raises(skills.library().SkillLibraryError, match="unsafe_path"):
        skills.unpack_zip(archive.getvalue())


def test_preview_limits_do_not_mistake_a_split_utf8_character_for_a_binary_file():
    content = "数据验证" * 15000
    record = skills.import_files(
        "preview-user", {"SKILL.md": SKILL, "references/large.md": content.encode()}
    )
    result = skills.preview("preview-user", record["name"], asset="references/large.md")
    assert not result["binary"]
    assert result["truncated"]
    assert content.startswith(result["content"])


@pytest.mark.asyncio
async def test_malformed_skill_has_a_localized_error_and_does_not_enter_library(runtime_client):  # noqa: F811
    client, _ = runtime_client
    headers = {
        **_headers(await _token(client, "researcher@atrium.local")),
        "X-Interface-Language": "en",
    }
    response = await client.post(
        "/api/v1/settings/skills/import",
        headers=headers,
        files=[("files", ("SKILL.md", b"---\nname: [invalid]\n---\nbody", "text/markdown"))],
    )
    assert response.status_code == 400
    assert response.json()["detail"]["message"].isascii()
    assert (await client.get("/api/v1/settings/skills", headers=headers)).json()["skills"] == []


@pytest.mark.asyncio
async def test_project_members_receive_only_the_shared_revision(runtime_client):  # noqa: F811
    from app.models.project import ProjectMembership

    client, factory = runtime_client
    alice = _headers(await _token(client, "researcher@atrium.local"))
    bob = _headers(await _token(client, "colleague@atrium.local"))
    first = await _upload(client, alice)
    project_id = await _project(client, alice)
    path = f"/api/v1/projects/{project_id}/skills"
    config = (await client.get(path, headers=alice)).json()
    binding = {k: first[k] for k in ("owner_id", "name", "revision")}
    binding.update(nodes=["experiment"], enabled=True)
    saved = await client.put(
        path, headers=alice, json={"revision": config["revision"], "bindings": [binding]}
    )
    assert saved.status_code == 200, saved.text
    bob_id = (await client.get("/api/v1/auth/me", headers=bob)).json()["id"]
    async with factory() as db:
        db.add(
            ProjectMembership(
                project_id=project_id,
                user_id=bob_id,
                role="viewer",
                created_by_user_id=first["owner_id"],
                updated_by_user_id=first["owner_id"],
            )
        )
        await db.commit()
    second = await _upload(client, alice, SKILL.replace(b"A test method", b"Private future method"))
    shared = (await client.get(path, headers=bob)).json()
    assert shared["skills"][0]["revisions"] == [first["revision"]]
    preview = await client.get(
        f"{path}/preview",
        headers=bob,
        params={
            "owner_id": first["owner_id"],
            "name": first["name"],
            "revision": first["revision"],
        },
    )
    assert preview.status_code == 200, preview.text
    assert second["revision"] not in preview.json()["revision_details"]
    denied = await client.get(
        f"{path}/preview",
        headers=bob,
        params={
            "owner_id": first["owner_id"],
            "name": first["name"],
            "revision": second["revision"],
        },
    )
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_unavailable_nodes_and_archived_projects_cannot_receive_assignments(runtime_client):  # noqa: F811
    client, _ = runtime_client
    headers = _headers(await _token(client, "researcher@atrium.local"))
    record = await _upload(client, headers)
    project_id = await _project(client, headers)
    path = f"/api/v1/projects/{project_id}/skills"
    config = (await client.get(path, headers=headers)).json()
    binding = {k: record[k] for k in ("owner_id", "name", "revision")}
    binding.update(nodes=["missing-node"], enabled=True)
    result = await client.put(
        path, headers=headers, json={"revision": config["revision"], "bindings": [binding]}
    )
    assert result.status_code == 400
    assert (await client.get(path, headers=headers)).json()["bindings"] == []
    assert (
        await client.post(f"/api/v1/projects/{project_id}/archive", headers=headers)
    ).status_code == 200
    binding["nodes"] = ["experiment"]
    result = await client.put(
        path, headers=headers, json={"revision": config["revision"], "bindings": [binding]}
    )
    assert result.status_code == 403

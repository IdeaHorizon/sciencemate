"""A UI-imported skill reaches only its assigned project/node and pinned run."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import skill_library as library
from core.skill_registry import get_skill, render_skills, visible_skills_for


def bundle(body="Use the first method.", *, name="imported-method"):
    return {
        "SKILL.md": f"---\nname: {name}\ndescription: A reusable method\n---\n\n{body}\n".encode(),
        "references/guide.md": b"version one reference",
        "scripts/check.py": b"print('check')\n",
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_FRAMEWORK_PROJECTS_HOME", str(tmp_path / "projects"))
    first = library.import_bundle("alice", bundle())
    return tmp_path, first


def assign(project, record, *, nodes=("experiment",), enabled=True):
    return library.write_bindings(
        project,
        [
            {
                "owner_id": record["owner_id"],
                "name": record["name"],
                "revision": record["revision"],
                "nodes": list(nodes),
                "enabled": enabled,
            }
        ],
        library.read_bindings(project)["revision"],
    )


def state(root: Path, project="project-a", node="experiment", run="run-a"):
    path = root / run
    path.mkdir(exist_ok=True)
    return SimpleNamespace(
        root=path,
        project_id=project,
        node_type=node,
        hook_state={},
        append_transcript=lambda *a, **kw: None,
    )


def test_import_only_enters_the_library(setup):
    root, first = setup
    assert library.list_library("alice") == [first]
    assert library.list_library("bob") == []
    assert library.snapshot_skills(state(root), "experiment") == []
    assert get_skill(first["name"]) is None, "Import must not mutate the global registry"


def test_prompt_listing_body_assets_and_node_override_share_the_assignment(setup):
    from shared.tools.library.skill_tools import _list_skills, _load_skill

    root, first = setup
    assign("project-a", first)
    bound = state(root)
    assert "load_skill('imported-method')" in render_skills([], node_type="experiment", state=bound)
    listed = asyncio.run(_list_skills(bound))
    assert any(s["name"] == first["name"] for s in listed["skills"])
    assert "first method" in asyncio.run(_load_skill(bound, first["name"]))["body_markdown"]
    assert (
        "print('check')"
        in asyncio.run(_load_skill(bound, first["name"], asset="scripts/check.py"))["content"]
    )
    wrong_node = state(root, node="writing", run="writing")
    assert all(s.name != first["name"] for s in visible_skills_for("writing", state=wrong_node))
    assert (
        asyncio.run(_load_skill(wrong_node, first["name"], for_node="experiment"))["status"]
        == "error"
    )
    assert not (wrong_node.root / "skill_assets" / first["name"]).exists()
    other_project = state(root, project="project-b", run="other-project")
    assert asyncio.run(_load_skill(other_project, first["name"]))["status"] == "error"
    assert asyncio.run(_load_skill(bound, first["name"], asset="../../secret"))["status"] == "error"


def test_revision_update_disable_and_restart_do_not_change_a_live_run(setup):
    from shared.tools.library.skill_tools import _load_skill

    root, first = setup
    assign("project-a", first)
    running = state(root)
    assert "first method" in asyncio.run(_load_skill(running, first["name"]))["body_markdown"]
    changed = bundle("Use the second method.")
    changed["references/guide.md"] = b"version two reference"
    second = library.import_bundle("alice", changed)
    # Importing a newer revision does not upgrade pinned projects.
    still_pinned = state(root, run="still-pinned")
    assert "first method" in asyncio.run(_load_skill(still_pinned, first["name"]))["body_markdown"]
    assign("project-a", second)
    fresh = state(root, run="fresh")
    assert "second method" in asyncio.run(_load_skill(fresh, first["name"]))["body_markdown"]
    library.set_library_enabled("alice", first["name"], False)
    disabled = state(root, run="disabled")
    assert asyncio.run(_load_skill(disabled, first["name"]))["status"] == "error"
    reopened = state(root)  # same run directory, empty process-local cache
    assert "first method" in asyncio.run(_load_skill(reopened, first["name"]))["body_markdown"]
    assert (
        asyncio.run(_load_skill(reopened, first["name"], asset="references/guide.md"))["content"]
        == "version one reference"
    )


def test_two_projects_can_use_different_revisions_without_registry_pollution(setup):
    root, first = setup
    second = library.import_bundle("alice", bundle("Use an independent method."))
    assign("project-a", first)
    assign("project-b", second)
    a, b = state(root), state(root, project="project-b", run="b")
    assert "first method" in library.snapshot_skills(a, "experiment")[0].body_markdown
    assert "independent" in library.snapshot_skills(b, "experiment")[0].body_markdown
    assert "first method" in library.snapshot_skills(a, "experiment")[0].body_markdown
    assert get_skill(first["name"]) is None


def test_project_saves_detect_concurrent_edits(setup):
    _, first = setup
    before = library.read_bindings("project-a")["revision"]
    assign("project-a", first)
    with pytest.raises(library.SkillLibraryError, match="configuration_changed"):
        library.write_bindings("project-a", [], before)


@pytest.mark.parametrize("path", ["../outside", "/outside", "C:/outside", "a\\b", "a/../b", ".env"])
def test_import_rejects_unsafe_paths(setup, path):
    with pytest.raises(library.SkillLibraryError, match="unsafe_path"):
        library.import_bundle("alice", {**bundle(), path: b"unsafe"})


def test_tampered_revisions_are_detected_before_a_new_run(setup):
    root, first = setup
    assign("project-a", first)
    (
        library.bundle_path("alice", first["name"], first["revision"]) / "scripts/check.py"
    ).write_text("changed")
    with pytest.raises(library.SkillLibraryError, match="revision_mismatch"):
        library.snapshot_skills(state(root), "experiment")


def test_a_restored_run_uses_its_own_assets_after_the_data_directory_moves(setup):
    root, first = setup
    assign("project-a", first)
    original = state(root)
    library.snapshot_skills(original, "experiment")
    shutil.copytree(original.root, root / "restored")
    library.set_library_enabled("alice", first["name"], False)
    restored = state(root, run="restored")
    skill = library.snapshot_skills(restored, "experiment")[0]
    assert Path(skill.source_dir).is_relative_to(restored.root)
    assert (Path(skill.source_dir) / "scripts/check.py").read_bytes() == bundle()[
        "scripts/check.py"
    ]


@pytest.mark.parametrize(
    "text",
    [
        b"\xef\xbb\xbf" + bundle()["SKILL.md"],
        bundle()["SKILL.md"].replace(b"\n", b"\r\n"),
    ],
)
def test_skill_text_from_windows_and_bom_editors_loads_correctly(setup, text):
    root, _ = setup
    record = library.import_bundle("alice", {"SKILL.md": text})
    assign("project-a", record)
    skill = library.snapshot_skills(state(root), "experiment")[0]
    assert skill.description == "A reusable method"
    assert skill.body_markdown == "Use the first method."

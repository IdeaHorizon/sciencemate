from __future__ import annotations

from core import artifact_provenance as provenance
from core.executor import _materialize_forwarded_file
from core.state import State
from shared.tools.run_node import _resolve_forward_artifacts


def _state(tmp_path, node_type: str) -> State:
    return State.new(node_type=node_type, base_dir=tmp_path / node_type)


def test_run_node_forwarding_preserves_top_level_provenance(tmp_path) -> None:
    parent = _state(tmp_path, "writing")
    saved = parent.save_artifact(
        "scientific_image",
        "external-image",
        "",
        provenance={
            "kind": "imported",
            "by_node_type": "_artifact_intake",
            "by_run_id": parent.run_id,
            "origin_kind": "imported",
        },
    )

    forwarded, missing = _resolve_forward_artifacts(parent, [saved["id"]])

    assert missing == []
    assert provenance.is_imported({"provenance": forwarded[0]["provenance"]})


def test_file_backed_artifact_is_copied_into_child_run(tmp_path) -> None:
    parent = _state(tmp_path, "writing")
    source = parent.root / "inputs" / "microscopy.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"scientific-image-bytes")
    child = _state(tmp_path, "postprocess")

    metadata = _materialize_forwarded_file(
        child_state=child,
        parent_state=parent,
        artifact={"type": "scientific_image", "name": "microscopy"},
        metadata={"file_path": "inputs/microscopy.png", "pixel_spacing": "0.2 um"},
    )

    copied = child.root / metadata["file_path"]
    assert copied.exists()
    assert copied.read_bytes() == source.read_bytes()
    assert child.root.resolve() in copied.resolve().parents
    assert metadata["forwarded_source_file_path"] == str(source.resolve())


def test_outside_parent_path_is_not_implicitly_copied(tmp_path) -> None:
    parent = _state(tmp_path, "writing")
    outside = tmp_path / "outside.pdb"
    outside.write_text("ATOM\n")
    child = _state(tmp_path, "postprocess")

    metadata = _materialize_forwarded_file(
        child_state=child,
        parent_state=parent,
        artifact={"type": "molecular_structure", "name": "outside"},
        metadata={"file_path": str(outside)},
    )

    assert metadata["file_path"] == str(outside)
    assert not (child.root / "forwarded_inputs").exists()

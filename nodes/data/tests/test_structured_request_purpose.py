"""Structured caller contracts retain their declared purpose (#1189)."""
from __future__ import annotations

from copy import deepcopy

import pytest

from nodes.data.planning.request_contract import normalize_preprocessing_request


@pytest.mark.parametrize("kind", ["preprocessing_service_request", "formal_input_preparation"])
def test_structured_caller_purpose_is_not_replaced_by_the_rendered_spec(kind):
    caller = {
        "request_kind": kind,
        "purpose": "Prepare the exact requested solver mesh",
        "spec": "A human-readable request explanation, not the purpose field",
        "required_assets": [{"name": "mesh.msh", "format": "gmsh", "purpose": "solver input"}],
        **({"source_prereg_artifact_id": "pre_registration__exact"}
           if kind == "formal_input_preparation" else {}),
    }
    original = deepcopy(caller)
    request = normalize_preprocessing_request({
        "request_source": "structured_data_request", "node_inputs": caller,
        "user_request": caller["spec"],
    })
    assert request["purpose"] == caller["purpose"]
    assert caller == original
    assert [item["filename"] for item in request["requested_assets"]] == ["mesh.msh"]


@pytest.mark.parametrize("source", ["structured_data_request", "bound_data_request"])
def test_explicit_contract_purpose_survives_its_different_authority_excerpt(source):
    purpose = "Convert the selected observation table to the declared schema"
    request = normalize_preprocessing_request({
        "request_source": source,
        "user_request": "An explanatory excerpt from the original caller",
        "preprocessing_request": {
            "purpose": purpose,
            "authority": {"kind": "experiment_request"},
            "authority_snapshot": {"request": "An earlier authority excerpt"},
            "requested_assets": [{"filename": "table.csv", "format": "csv"}],
        },
    })
    assert request["purpose"] == purpose
    assert request["authority_snapshot"] == {"request": "An earlier authority excerpt"}


def test_legacy_dispatcher_hints_do_not_override_the_bound_user_request():
    user_request = "Generate the user's cylinder, retaining its specified dimensions"
    normalized = normalize_preprocessing_request({
        "request_source": "caller_or_experiment_node", "user_request": user_request,
        "node_inputs": {"spec": "A dispatcher-proposed cube", "purpose": "Another task"},
    })
    assert normalized["purpose"] == user_request


def test_asset_acquisition_reference_is_preserved_without_aliasing_and_binds_identity():
    reference = {
        "source_locator": "https://example.test/data.csv",
        "expected_revision": "r1", "expected_filename": "data.csv",
        "expected_sha256": "a" * 64, "license": "CC BY 4.0",
    }
    caller = {
        "request_kind": "preprocessing_service_request", "purpose": "Prepare the declared input",
        "required_assets": [{
            "name": "data.csv", "format": "csv", "purpose": "solver input",
            "acquisition_reference": reference,
        }],
    }
    context = {"request_source": "structured_data_request", "node_inputs": caller}
    normalized = normalize_preprocessing_request(context)
    saved = normalized["requested_assets"][0]["acquisition_reference"]
    assert saved == reference
    saved["expected_revision"] = "r2"
    assert reference["expected_revision"] == "r1"
    changed = deepcopy(context)
    changed_reference = changed["node_inputs"]["required_assets"][0]["acquisition_reference"]
    changed_reference["expected_revision"] = "r2"
    changed_request = normalize_preprocessing_request(changed)
    assert changed_request["request_spec_hash"] != normalized["request_spec_hash"]

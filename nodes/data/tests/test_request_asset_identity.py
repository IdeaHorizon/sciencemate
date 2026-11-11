from copy import deepcopy

import pytest

from nodes.data.planning.schemas import validate_plan
from nodes.data.tools.preprocessing_planner import _repair_designer_plan_contract


@pytest.mark.parametrize("asset_id", ["converted.tsv", "Converted-Result.tsv", "O1"])
def test_request_work_order_and_repaired_plan_keep_one_asset_identity(asset_id):
    analysis = {"review_profile": "request_bound", "required_files": [{
        "id": asset_id, "name": asset_id, "format": "tsv",
        "reason": f"User requested {asset_id}", "source_strategy": "local_generation",
        "asset_role": "data_file", "generation_recipe": "Convert CSV to TSV without changing fields",
    }]}
    draft = {"required_deliverables": [{"id": asset_id, "name": asset_id}],
             "generation_steps": [{"id": "convert",
                                   "tool_name": "generate_preprocessing_artifact",
                                   "outputs": [asset_id], "tool_arguments": {}}]}
    plan = _repair_designer_plan_contract(draft, analysis)
    for _ in range(2):
        assert [item["id"] for item in plan["required_deliverables"]] == [asset_id]
        units = plan["preprocessing_work_order"]["work_units"]
        assert units[0]["requested_asset_ids"] == [asset_id]
        step = plan["generation_steps"][0]
        assert step["outputs"] == [asset_id]
        assert step["work_unit_id"] == units[0]["work_unit_id"]
        expected_path = "o1.tsv" if asset_id == "O1" else asset_id
        assert plan["required_deliverables"][0]["declared_output_path"] == expected_path
        assert not any("assets missing" in error for error in validate_plan(plan)["errors"])
        plan = _repair_designer_plan_contract(plan, analysis)
    missing = deepcopy(plan)
    missing["required_deliverables"] = []
    assert any("assets missing" in error and asset_id in error
               for error in validate_plan(missing)["errors"])

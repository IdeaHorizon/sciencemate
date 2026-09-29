"""判决拆除·第三波 B 组：零散的降格 / 契约归 schema，各一条「墙加回去就转红」。

覆盖（对应 plan_B）：
  · builtin:1637 edit_file「先 read」删；1646 old==new 成 no-op success；1648 old 空归 schema
  · run_node:3667/3694/3700 request_upstream_rework 空串归 schema，假文案「≥15 字符」删
  · web:298/301 url 非空 + http(s) 归 schema pattern；web:185 / papers:200 query 归 schema
  · run_node:1354 user_note 归 schema；1470 review 别名并进 harness 不存在的报错
  · builtin:167/169/208/263/269 save_artifact 契约合一
  · kb_schema 文案与判据同源（452 / 744 不再许诺字数闸）；enum 只声明一次
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute, get_tool


@pytest.fixture(autouse=True)
def _boot():
    bootstrap()


def _st(tmp_path, node_type="experiment") -> State:
    return State.new(node_type=node_type, base_dir=tmp_path / "runs", project_id="pw")


# ── edit_file ────────────────────────────────────────────────────────────────

def test_edit_file_needs_no_read_ritual(tmp_path):
    st = _st(tmp_path)
    f = Path(st.root) / "notes.md"
    f.write_text("alpha beta\n", encoding="utf-8")
    assert str(f) not in st.files_read
    res = asyncio.run(execute("edit_file", st, path=str(f), old_string="beta", new_string="gamma"))
    assert res["status"] == "success", res
    assert f.read_text(encoding="utf-8") == "alpha gamma\n"


def test_edit_file_identical_strings_is_a_noop_success(tmp_path):
    st = _st(tmp_path)
    f = Path(st.root) / "notes.md"
    f.write_text("alpha\n", encoding="utf-8")
    res = asyncio.run(execute("edit_file", st, path=str(f), old_string="alpha", new_string="alpha"))
    assert res["status"] == "success" and res["replacements_made"] == 0, res


def test_edit_file_empty_old_string_is_refused_by_the_schema(tmp_path):
    st = _st(tmp_path)
    f = Path(st.root) / "notes.md"
    f.write_text("alpha\n", encoding="utf-8")
    res = asyncio.run(execute("edit_file", st, path=str(f), old_string="", new_string="x"))
    assert res["status"] == "error" and res.get("parameter_violations"), res
    assert "调用前**必须**先 read_file" not in get_tool("edit_file").description


# ── request_upstream_rework ─────────────────────────────────────────────────

def test_upstream_rework_blank_fields_are_schema_violations_with_honest_wording(tmp_path):
    st = _st(tmp_path, "writing")
    res = asyncio.run(execute("request_upstream_rework", st, upstream_node="literature",
                              missing="   ", acceptance="ok"))
    assert res["status"] == "error" and res.get("parameter_violations"), res
    assert "15 字符" not in res["error"]
    schema = get_tool("request_upstream_rework").parameters_schema["properties"]
    for key in ("upstream_node", "missing", "acceptance"):
        assert schema[key]["minLength"] == 1
        assert "15 字符" not in schema[key].get("description", "")
    ok = asyncio.run(execute("request_upstream_rework", st, upstream_node="literature",
                             missing="缺 3 篇被引论文的 KB chunk", acceptance="全部 cite key 落到 KB"))
    assert ok["status"] == "success", ok


# ── web / papers ────────────────────────────────────────────────────────────

def test_web_fetch_scheme_and_emptiness_are_schema(tmp_path):
    st = _st(tmp_path)
    for bad in ("", "ftp://example.org/x"):
        res = asyncio.run(execute("web_fetch", st, url=bad))
        assert res["status"] == "error" and res.get("parameter_violations"), (bad, res)
    props = get_tool("web_fetch").parameters_schema["properties"]["url"]
    assert props["pattern"] == r"^https?://" and props["minLength"] == 1
    assert get_tool("web_search").parameters_schema["properties"]["query"]["minLength"] == 1
    assert get_tool("arxiv_search").parameters_schema["properties"]["query"]["minLength"] == 1


# ── run_node ────────────────────────────────────────────────────────────────

def test_run_node_blank_user_note_is_a_schema_violation(tmp_path):
    st = _st(tmp_path, "_orchestrator")
    st.hook_state["_callable_nodes"] = ["*"]
    res = asyncio.run(execute("run_node", st, node_type="literature", user_note="  "))
    assert res["status"] == "error" and "user_note" in res["error"], res
    assert res.get("parameter_violations")


def test_review_alias_is_answered_where_the_harness_is_looked_up(tmp_path):
    st = _st(tmp_path, "_orchestrator")
    st.hook_state["_callable_nodes"] = ["*"]
    res = asyncio.run(execute("run_node", st, node_type="review", user_note="审稿"))
    assert res["status"] == "error"
    assert "_reviewer" in res["error"] and "已废弃" in res["error"]


def test_run_node_schema_declares_the_deliverable_shape():
    schema = get_tool("run_node").parameters_schema["properties"]
    assert schema["deliverable"]["required"] == ["artifact_type", "path"]
    assert schema["user_note"]["minLength"] == 1
    jobs = get_tool("run_nodes_parallel").parameters_schema["properties"]["jobs"]
    assert jobs["minItems"] == 1 and jobs["items"]["required"] == ["node_type", "user_note"]


# ── save_artifact ───────────────────────────────────────────────────────────

def test_save_artifact_contract_is_declared_once(tmp_path):
    st = _st(tmp_path, "writing")
    props = get_tool("save_artifact").parameters_schema["properties"]
    assert props["artifact_type"]["minLength"] == 1 and props["name"]["minLength"] == 1
    res = asyncio.run(execute("save_artifact", st, artifact_type="", name="n", content="c"))
    assert res["status"] == "error" and res.get("parameter_violations")
    both = asyncio.run(execute("save_artifact", st, artifact_type="scratchpad", name="n",
                               content="c", content_from_file="x.md"))
    assert both["status"] == "error" and "只能给一个" in both["error"]
    bad_md = asyncio.run(execute("save_artifact", st, artifact_type="scratchpad", name="n",
                                 content="c", metadata="[1, 2]"))
    assert bad_md["status"] == "error" and "object" in bad_md["error"]


# ── kb_schema：文案与判据同源，enum 一份声明 ────────────────────────────────

def test_kb_schema_wording_matches_the_criterion():
    from shared.lib import kb_schema as k

    with pytest.raises(k.SchemaValidationError) as exc:
        k.validate_record("claims", {
            "claim_text": "x", "claim_type": "dead_end", "confidence": 0.5,
            "concept_ids": ["concept_a"], "sources": [], "orphan_reason": "x",
        })
    assert "dont_repeat_reason" in str(exc.value) and "10 字符" not in str(exc.value)
    assert k._ENTITY_SCHEMAS["claims"]["properties"]["claim_type"]["enum"] is k.CLAIM_TYPES
    assert k._ENTITY_SCHEMAS["concepts"]["properties"]["concept_type"]["enum"] is k.CONCEPT_TYPES
    with pytest.raises(KeyError):
        k.validate_record("nope", {})

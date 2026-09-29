"""判决词表（status / quality_mode / verdict）不许回到图记录 —— 钉在**铸造侧**。

判决拆除第三波（publication_figures:100 X 删）：消费端曾在运行时「metadata 出现
status/quality_mode/verdict 键即拒整张图」。那是防复发闸放错了层：没有机器消费者
读这些键，放行账不假；而 metadata 恰好有个 `status` 键的合法记录会被误伤。
防复发的正确位置是铸造侧 + 这条测试：

  · nodes/postprocess/tools/figure.py 的铸造函数在 save_artifact 之前调
    reject_verdict_fields（AST 锚，不是 grep 字符串）；
  · shared/lib/publication_figures.py 不再有运行时的判决词表拒绝。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_MINT = ROOT / "nodes" / "postprocess" / "tools" / "figure.py"
_CONSUME = ROOT / "shared" / "lib" / "publication_figures.py"


def _calls(fn: ast.AST) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
            out.append((name, node.lineno))
    return out


def test_minting_rejects_verdict_fields_before_it_saves():
    tree = ast.parse(_MINT.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "_render_figure")
    calls = _calls(fn)
    rejects = [ln for name, ln in calls if name == "reject_verdict_fields"]
    saves = [ln for name, ln in calls if name == "save_artifact"]
    assert rejects, "铸造侧不再调 reject_verdict_fields —— 判决词表复活无人拦"
    assert saves and min(rejects) < min(saves), "reject_verdict_fields 必须在 save_artifact 之前"


def test_the_consumer_has_no_runtime_verdict_field_wall():
    src = _CONSUME.read_text(encoding="utf-8")
    assert "_FORBIDDEN_VERDICT_FIELDS" not in src
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "validate_figure_record")
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "verdict fields" not in node.value


def _minted_metadata() -> dict:
    from shared.lib.publication_figures import figure_binding_hash

    h = "sha256:" + "a" * 64
    md = {
        "record": "rendered_figure",
        "source_artifact_ids": ["dataset__x"],
        "source_hashes": [h],
        "render_code": {"path": "render.py", "content_hash": h},
        "files": [{"path": "fig.png", "format": "png", "content_hash": h}],
        "caption": "c", "alt_text": "a",
        "audit": {"ran": True}, "findings": [], "vlm_review": {"completed": True},
    }
    md["figure_hash"] = figure_binding_hash(md)
    return md


def _record(md: dict) -> dict:
    return {"type": "figure", "produced_by_node_type": "postprocess", "metadata": md}


def test_a_record_with_a_stray_status_key_is_consumed_not_refused():
    """从前：metadata 里有 `status` 键 → PublicationFigureError。现在照收（账不假：
    指纹与出处绑定照验）。墙加回去这条转红。"""
    from shared.lib.publication_figures import validate_figure_record

    md = _minted_metadata()
    md["status"] = "ok"
    payload = validate_figure_record(_record(md))
    assert payload["figure_hash"] == md["figure_hash"]
    # 这条要说的是「没有**别的**事出错」，不是「一条充分性事实都没有」。
    # #439 之后，没声明 annotated_values 的图会如实披露「图文对账未运行」——
    # 那是应有的缺席记录，不是这条测试关心的 status 键出了问题。
    others = [f for f in payload["_adequacy_findings"] if "图文数值对账" not in f]
    assert others == [], others


def test_the_shape_contract_is_still_one_schema():
    """形状契约（C）收成一份 schema：缺文件 / 坏 hash / 长度不等各自被指名。"""
    from shared.lib.publication_figures import (
        PublicationFigureError, _FIGURE_METADATA_SCHEMA, validate_figure_record,
    )

    assert set(_FIGURE_METADATA_SCHEMA["required"]) == {
        "source_artifact_ids", "source_hashes", "render_code", "files"}
    for mutate, needle in (
        (lambda m: m.update(files=[]), "files"),
        (lambda m: m["render_code"].update(content_hash="sha256:zz"), "content_hash"),
        (lambda m: m.update(source_hashes=[]), "长度不等"),
        (lambda m: m["files"][0].update(path=""), r"files\[0\]\.path 不能为空"),
    ):
        md = _minted_metadata()
        mutate(md)
        with pytest.raises(PublicationFigureError, match=needle):
            validate_figure_record(_record(md))

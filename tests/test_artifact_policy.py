"""测 artifact policy 表 / freeze_artifact 端到端（冻结 = 账本一行，钉死 path@sha256；
没有 deliverables/ 与 org/_archive/ 抄件 —— 目录页、收尾清单、发布链全读账本）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.lib.artifact_policy import (  # noqa: E402
    get_policy, is_permanent, is_chunk_then_drop, is_transient, ttl_days,
)


# HARNESS_FRAMEWORK_HOME 隔离由 tests/conftest.py 的全局 autouse fixture 处理
# 本文件 test 仍用名为 _isolate_home 的参数，从 conftest.isolate_harness_home alias 拿到 tmp_path


@pytest.fixture
def _isolate_home(isolate_harness_home):
    """alias 旧 fixture 名（避免大改 test 签名）。"""
    return str(isolate_harness_home)


def _build_state(home: str, project_id: str = "proj1"):
    """没绑 worktree 的 run 本地 State：记录落 `<run>/artifacts/`，账本 `<run>/records.jsonl`。"""
    from core.state import State
    root = Path(home) / "runs" / "r1"
    root.mkdir(parents=True)
    s = State(run_id="r1", node_type="test", root=root, project_id=project_id)
    s.project_root = Path(home) / "projects" / project_id
    s.project_root.mkdir(parents=True, exist_ok=True)
    return s


# ─── policy table tests ──────────────────────────────────────────────────

def test_policy_table_covers_all_main_types():
    for k in ("survey_report", "research_plan", "pre_registration",
              "manuscript", "accepted_paper", "analysis_report"):
        p = get_policy(k)
        assert p["retention"] == "permanent", f"{k} must be permanent"
        assert is_permanent(k)


def test_unknown_type_defaults_transient():
    p = get_policy("random_unknown_type_xyz")
    assert p["retention"] == "transient"
    assert is_transient("random_unknown_type_xyz")
    assert ttl_days("random_unknown_type_xyz") == 30


def test_experiment_log_is_chunk_then_drop():
    assert is_chunk_then_drop("experiment_log")
    assert not is_permanent("experiment_log")


# ─── freeze_artifact end-to-end ─────────────────────────────────────────

def test_freeze_prereg_pins_a_tamper_evident_row_in_the_ledger(_isolate_home):
    """冻结 prereg = 账本一条 freeze 行钉死 path@sha256（曾是 deliverables/prereg/
    `_register.jsonl` 的 tamper-evident 条目）；文件一个字节不动，文件哈希 = 账本哈希。"""
    import shared.tools.library.artifacts_extra as ax
    s = _build_state(_isolate_home)
    s.save_artifact("pre_registration", "H1",
                     """## Research Questions

### Q1: max_dev 是否超过 5%？
- output_kind: 对一条命题的裁决
- proposition: max_dev 超过 5%
```yaml
- metric: max_dev
  comparison: ">"
  threshold: 5
```
""")
    before = s.find_artifact_path("pre_registration__H1").read_bytes()
    result = asyncio.run(ax._freeze_artifact(
        state=s, artifact_id="pre_registration__H1",
        # 冻结 prereg 必须声明这批实验算不算正式证据（见
        # tests/test_prereg_run_role_declaration.py）
        run_role="primary", analysis_eligible=True,
        expected_params={"n_particles": 4000},
    ))
    assert result["status"] == "success"
    head = s.artifact_head("pre_registration__H1")
    assert head.frozen and head.frozen_version == head.version == 1
    assert len(head.frozen_sha256) == 64
    path = s.find_artifact_path("pre_registration__H1")
    assert path.read_bytes() == before, "冻结不改文件"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == head.frozen_sha256, \
        "账本钉死的哈希 = 文件哈希"
    rows = [json.loads(line) for line in
            (s.root / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    freezes = [r for r in rows if r.get("event") == "freeze" and r.get("id") == "pre_registration__H1"]
    assert len(freezes) == 1
    assert freezes[0]["sha256"] == head.frozen_sha256 and freezes[0]["path"] == head.path
    assert freezes[0]["by_node"] == "test" and freezes[0]["by_run"] == "r1", "谁冻的要在行上"
    meta = s.read_artifact("pre_registration__H1")["metadata"]
    assert meta["frozen"] is True and meta["run_role"] == "primary"


def test_freeze_experiment_log_no_promotion(_isolate_home):
    import shared.tools.library.artifacts_extra as ax
    s = _build_state(_isolate_home)
    s.save_artifact("experiment_log", "Run42", "raw log dump...")
    result = asyncio.run(ax._freeze_artifact(
        state=s, artifact_id="experiment_log__Run42",
    ))
    assert result["status"] == "success"
    assert result.get("promotion") in (None, {})


def test_already_frozen_is_noop(_isolate_home):
    import shared.tools.library.artifacts_extra as ax
    s = _build_state(_isolate_home)
    s.save_artifact("survey_report", "X", "content")
    r1 = asyncio.run(ax._freeze_artifact(state=s, artifact_id="survey_report__X"))
    r2 = asyncio.run(ax._freeze_artifact(state=s, artifact_id="survey_report__X"))
    assert r1["status"] == "success"
    assert r2.get("already_frozen") is True


def test_freeze_while_matching_formal_review_flow_is_pending_records_it(_isolate_home):
    """A reviewer REVISE cannot be laundered into a *clean* frozen manuscript.

    判决拆除·第三波：顺序闸降格——冻结照做，但冻结件机械写入
    metadata.review_state='open' + 待闭合的 flow（含 recommended_action=revise），
    账是真的。墙若被加回来（拒绝冻结）这条转红。
    （manuscript 自己的类型冻结门 review_binding / validation_binding 是另一堵
    保留的墙，这里用没有类型门的 survey_report 单测顺序闸本身。）
    """
    import shared.tools.library.artifacts_extra as ax
    s = _build_state(_isolate_home)
    s.save_artifact("survey_report", "Draft", "survey body")
    s.hook_state["pending_post_node_flow"] = [{
        "producing_node": "literature",
        "producing_run_id": "r-lit",
        "artifact_ids": ["survey_report__Draft"],
        "review_state": "done",
        "curator_state": "done",
        "decision_state": "awaiting_human",
        "decision_recommended_action": "revise",
    }]

    result = asyncio.run(ax._freeze_artifact(
        state=s, artifact_id="survey_report__Draft",
    ))

    assert result["status"] == "success", result
    assert result["review_state"] == "open"
    assert result["pending_flows"][0]["recommended_action"] == "revise"
    meta = s.read_artifact("survey_report__Draft")["metadata"]
    assert meta["frozen"] is True
    assert meta["review_state"] == "open"
    assert meta["pending_review_flows"][0]["recommended_action"] == "revise"


# ─── KB user_id stamping ──────────────────────────────────────────────

def test_kb_write_stamps_created_by_user_id(_isolate_home):
    s = _build_state(_isolate_home)
    from core.paths import user_root
    user_root().mkdir(parents=True, exist_ok=True)
    (user_root() / "identity.json").write_text(
        '{"user_id":"alice","display_name":"Alice","email":"alice@x"}',
        encoding="utf-8",
    )
    rec, _ = s.write_kb("concepts", {
        "canonical_name": "X", "concept_type": "method", "description": "y",
    })
    assert rec["created_by_user_id"] == "alice"


def test_update_lifecycle_stamps_user_id_in_review_history(_isolate_home):
    s = _build_state(_isolate_home)
    from core.paths import user_root
    user_root().mkdir(parents=True, exist_ok=True)
    (user_root() / "identity.json").write_text(
        '{"user_id":"bob","display_name":"Bob","email":"bob@x"}',
        encoding="utf-8",
    )
    rec, _ = s.write_kb("claims", {
        "claim_text": "z is q", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": ["chunk_xxxxxxxxx0001"],
    })
    updated = s.update_lifecycle(
        "claims", rec["id"],
        status_change={"to_status": "validated"},
        reasoning="strong evidence supports",
    )
    hist = updated["review_history"]
    assert hist[-1]["by_user_id"] == "bob"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

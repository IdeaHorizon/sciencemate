"""组织服务器经桥问 harness：这个组织的待审是什么、采纳、退回。

`RFC_ORGANISATION_PAGE_20260923` C 批。一台组织服务器上有好几个组织，每个一份 org 层
（`<数据根>/organisations/<id>`）。所以桥上的每一问都**必须说是哪个组织**：没说就退回
`home_dir/org` 的话，管理员会在一个谁也不读的私有目录里「采纳」—— 而且不报错。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import kb_promotion as kp
from core.state import State
from tests.test_kb_promotion import _good_card, _make_terminal, _seed_claim, _seed_evidence

REPO = Path(__file__).resolve().parents[1]
PROJECT = "p_demo"


def _ask(**fields) -> dict:
    request = {"op": "kb_promotion", "request_id": "t1", **fields}
    proc = subprocess.run(
        [sys.executable, "-m", "platform_runtime"],
        input=json.dumps(request) + "\n", capture_output=True, text=True,
        cwd=str(REPO), timeout=60,
    )
    for line in proc.stdout.splitlines():
        event = json.loads(line)
        if event.get("type") in ("kb_promotion_result", "error"):
            return event
    raise AssertionError(f"桥没有回结果：{proc.stdout!r} {proc.stderr[-400:]!r}")


@pytest.fixture()
def two_organisations(tmp_path, monkeypatch):
    """一个成员的 home、两个组织；成员的项目做完了，交给了 A。"""
    home, org_a, org_b = tmp_path / "users" / "u1", tmp_path / "orgs" / "A", tmp_path / "orgs" / "B"
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(home))
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", str(org_a))
    st = State.new(node_type="_orchestrator", base_dir=home / "projects" / PROJECT / "runs",
                   project_id=PROJECT)
    _make_terminal(st)
    finding = _seed_claim(st, _seed_evidence(st))
    out = kp.offer_to_the_organisation(st, project_id=PROJECT, at="2026-09-24T00:00:00Z",
                                       drafts={finding: _good_card()})
    assert out["queued"], out
    return home, org_a, org_b, finding


def _org_claims(org: Path) -> list[dict]:
    path = org / "kb_claims.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_each_organisation_sees_only_its_own_queue(two_organisations):
    home, org_a, org_b, finding = two_organisations

    mine = _ask(action="list", home_dir=str(home), org_home=str(org_a))
    theirs = _ask(action="list", home_dir=str(home), org_home=str(org_b))

    assert [p["source_id"] for p in mine["proposals"]] == [finding]
    assert theirs["proposals"] == [], "另一个组织的待审里出现了这个组织项目提上来的东西"


def test_adopting_lands_in_that_organisation_only(two_organisations):
    home, org_a, org_b, finding = two_organisations
    [waiting] = _ask(action="list", home_dir=str(home), org_home=str(org_a))["proposals"]

    done = _ask(action="adopt", home_dir=str(home), org_home=str(org_a), project_id=PROJECT,
                proposal_id=waiting["id"], by="admin@lab.test")

    assert done["type"] == "kb_promotion_result", done
    assert done["proposal"]["status"] == "adopted"
    assert [c["claim_text"] for c in _org_claims(org_a)] == [_good_card()["statement"]]
    assert _org_claims(org_b) == []


def test_declining_says_why(two_organisations):
    home, org_a, _org_b, _finding = two_organisations
    [waiting] = _ask(action="list", home_dir=str(home), org_home=str(org_a))["proposals"]

    silent = _ask(action="decline", home_dir=str(home), org_home=str(org_a),
                  proposal_id=waiting["id"], by="admin@lab.test", reason="")
    said = _ask(action="decline", home_dir=str(home), org_home=str(org_a),
                proposal_id=waiting["id"], by="admin@lab.test", reason="适用条件写得太宽")

    assert silent["type"] == "error" and silent["code"] == "reason_required"
    assert said["proposal"]["status"] == "declined" and said["proposal"]["reason"] == "适用条件写得太宽"


def test_an_unnamed_organisation_is_refused(two_organisations):
    home, _org_a, _org_b, _finding = two_organisations

    got = _ask(action="list", home_dir=str(home))

    assert got["type"] == "error" and got["code"] == "invalid_org_home"

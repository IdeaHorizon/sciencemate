"""组织服务器经桥改正组织自己的一条知识：推翻 / 取代 / 撤回、成员提更正、管理员认可更正。

和采纳同一条桥（`op=kb_promotion`），同一个规矩：**必须说是哪个组织**。改组织自己那一条
不读任何项目 —— 认可一条成员提的更正时，请求里没有项目，也不该需要。
"""
from __future__ import annotations

from tests.test_the_organisations_review_queue_over_the_bridge import (  # noqa: F401
    PROJECT, _ask, _org_claims, two_organisations,
)

ADMIN = "admin@lab.test"


def _an_entry_in_a(two_organisations) -> tuple:  # noqa: F811
    home, org_a, org_b, _finding = two_organisations
    [waiting] = _ask(action="list", home_dir=str(home), org_home=str(org_a))["proposals"]
    adopted = _ask(action="adopt", home_dir=str(home), org_home=str(org_a), project_id=PROJECT,
                   proposal_id=waiting["id"], by=ADMIN)
    return home, org_a, org_b, adopted["proposal"]["org_id"]


def _in(org, org_id: str) -> dict:
    return next(c for c in _org_claims(org) if c["id"] == org_id)


def test_the_admin_refutes_an_entry_in_that_organisation(two_organisations):  # noqa: F811
    home, org_a, org_b, org_id = _an_entry_in_a(two_organisations)

    done = _ask(action="retire", home_dir=str(home), org_home=str(org_a), org_id=org_id,
                verdict="refuted", reason="复算发现参考能量取错了", by=ADMIN)
    elsewhere = _ask(action="retire", home_dir=str(home), org_home=str(org_b), org_id=org_id,
                     verdict="refuted", reason="隔壁组织想推翻它", by=ADMIN)

    assert done["type"] == "kb_promotion_result", done
    assert done["entry"]["org_standing"]["verdict"] == "refuted"
    assert _in(org_a, org_id)["org_standing"]["by"] == ADMIN, "裁定没落进那个组织的账本"
    assert elsewhere["type"] == "error" and elsewhere["code"] == "not_found", (
        "另一个组织推翻得了这个组织的知识")


def test_a_verdict_is_withdrawn_over_the_bridge(two_organisations):  # noqa: F811
    home, org_a, _org_b, org_id = _an_entry_in_a(two_organisations)
    _ask(action="retire", home_dir=str(home), org_home=str(org_a), org_id=org_id,
         verdict="refuted", reason="以为错了", by=ADMIN)

    done = _ask(action="reinstate", home_dir=str(home), org_home=str(org_a), org_id=org_id,
                reason="复核没错", by=ADMIN)

    assert done["type"] == "kb_promotion_result", done
    assert _in(org_a, org_id).get("org_standing") is None


def test_a_members_correction_is_adopted_without_any_project(two_organisations):  # noqa: F811
    home, org_a, _org_b, org_id = _an_entry_in_a(two_organisations)

    filed = _ask(action="propose_correction", home_dir=str(home), org_home=str(org_a),
                 org_id=org_id, verdict="refuted", reason="我们组复现不出来", by="zhang@lab.test")
    waiting = filed["proposal"]
    adopted = _ask(action="adopt", home_dir=str(home), org_home=str(org_a),
                   proposal_id=waiting["id"], by=ADMIN)

    assert filed["type"] == "kb_promotion_result", filed
    assert waiting["type"] == "correction" and waiting["origin"] == "member"
    assert waiting["proposed_by"] == "zhang@lab.test"
    assert adopted["type"] == "kb_promotion_result", adopted
    assert adopted["proposal"]["status"] == "adopted"
    assert _in(org_a, org_id)["org_standing"]["proposal_id"] == waiting["id"]


def test_a_correction_needs_the_organisation_named(two_organisations):  # noqa: F811
    home, _org_a, _org_b, org_id = _an_entry_in_a(two_organisations)

    got = _ask(action="retire", home_dir=str(home), org_id=org_id, verdict="refuted",
               reason="x", by=ADMIN)

    assert got["type"] == "error" and got["code"] == "invalid_org_home"

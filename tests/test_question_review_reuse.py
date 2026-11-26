"""Question-level review references use the real typed artifact/context boundary."""

import copy
import json

import pytest

from core.bootstrap import bootstrap
from core.context_engine import build_messages
from core.loader import load_harness
from core.state import State
from core.tool_registry import execute as execute_tool
from nodes._reviewer.tools.critique_builder import _compose_review_critique as compose
from nodes.analysis.tools.research_state import _update_research_state

PROOF = """For a finite simple undirected graph G with n >= 3 vertices and m edges,
let t be its number of triangles and d(v) its vertex degrees. The number I3 of
independent vertex triples is C(n,3)-m(n-2)+sum_v C(d(v),2)-t.
1. Start from all C(n,3) three-vertex subsets.
2. For each edge e, let A_e contain the triples including both endpoints of e.
   Then |A_e|=n-2, so the sum of single-edge event sizes is m(n-2).
3. The independent triples are exactly the complement of the union of A_e.
4. Two distinct edge events intersect in a three-vertex set only if the edges
   share a vertex. Each adjacent edge pair belongs to exactly one such triple.
5. The number of adjacent edge pairs is sum_v C(d(v),2); inclusion-exclusion
   adds this sum back after subtracting the single-edge events.
6. Three distinct edge events intersect exactly when the three edges form a
   triangle. Each triangle contributes one triple; subtract t. Four distinct
   edge events cannot intersect on three vertices in a simple graph.
7. Inclusion-exclusion therefore gives the stated identity, with no asymptotic
   approximation or unstated restriction on vertex degrees.
Checks at n=3: the empty graph gives I3=1; the two-edge path gives I3=0;
the triangle gives I3=0. The identity holds for all graphs in the stated domain.
"""


def reviewer(tmp_path, worktree, subject):
    state = State.new("_reviewer", tmp_path / "runs", project_worktree=worktree)
    messages = build_messages(
        load_harness("_reviewer"),
        state,
        {
            "source_node_type": "analysis",
            "artifact_id": subject,
        },
    )
    return state, "\n".join(m.content or "" for m in messages)


async def fixture(tmp_path, worktree):
    bootstrap()
    author = State.new("analysis", tmp_path / "runs", project_worktree=worktree)
    evidence = author.save_artifact(
        "analysis_evidence",
        "Q2",
        json.dumps(
            {
                "samples": [
                    {"vertices": 3, "edges": [], "independent_triples": 1},
                    {"vertices": 3, "edges": [[0, 1], [1, 2]], "independent_triples": 0},
                    {"vertices": 3, "edges": [[0, 1], [1, 2], [0, 2]], "independent_triples": 0},
                ]
            }
        ),
        metadata={
            "definitions": "simple undirected graphs",
            "criteria": "all seven steps valid",
            "use": "finite mathematical theorem, not an asymptotic claim",
        },
    )
    rows = [
        {
            "id": "Q2",
            "status": "supported",
            "note": PROOF,
            "evidence": [evidence["id"]],
        },
        {"id": "Q5", "status": "inconclusive", "note": "finite experiments only"},
    ]
    result = await _update_research_state(author, verdict="continue", hypotheses=rows)
    subject = result["artifact_id"]
    dependencies = [
        {"role": "proof", "artifact_id": subject, "question_id": "Q2", "pointer": "/note"},
        {"role": "data", "artifact_id": evidence["id"]},
        *[
            {"role": role, "artifact_id": evidence["id"], "pointer": "/metadata/" + key}
            for role, key in (
                ("definitions", "definitions"),
                ("criteria", "criteria"),
                ("use", "use"),
            )
        ],
    ]
    return author, subject, evidence["id"], rows, dependencies


async def record(state, subject, dependencies, **overrides):
    args = dict(
        artifact_under_review=subject,
        question_id="Q2",
        question_status="verified",
        question_finding="Seven steps and equality cases checked against small graphs.",
        question_dependencies=dependencies,
        dependency_completeness="complete",
        intended_use="finite theorem and equality conditions",
    )
    args.update(overrides)
    return await execute_tool("compose_review_critique", state, action="record_question", **args)


async def finish(state, subject, name="analysis_revision_review"):
    await compose(state, "set_verdict", verdict="major_concerns", summary="Q5 still needs revision")
    await compose(
        state,
        "set_recommended_action",
        recommended_action="revise",
        feedback_to_next_run="Retain Q2; revise only the finite/asymptotic wording in Q5.",
    )
    result = await compose(
        state, "finalize", name=name, artifact_under_review=subject, source_node_type="analysis"
    )
    assert result["status"] == "success", result
    saved = {**state.read_artifact(result["artifact_id"]), "id": result["artifact_id"]}
    return saved, json.loads(saved["content"])


@pytest.mark.asyncio
async def test_independent_question_change_carries_exact_finding_after_restart(
    tmp_path, mem_worktree
):
    author, subject, _, rows, deps = await fixture(tmp_path, mem_worktree)
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    result = await record(first, subject, deps)
    assert result["status"] == "success", result
    prior, _ = await finish(first, subject)
    rows[1]["note"] = "Q5 now separates finite and asymptotic statements"
    await _update_research_state(
        author, verdict="continue", hypotheses=rows, change_reason="Q5 only"
    )
    current, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "eligible"' in prompt
    assert prior["id"] in prompt and "Seven steps" in prompt
    result = await record(
        current,
        subject,
        deps,
        question_status="carried_forward",
        previous_review_id=prior["id"],
        previous_review_version=prior["version"],
    )
    assert result["status"] == "success", result
    # A worker restart reads persisted references, not a process-local cache.
    from core.agent_loop import _persist_messages_checkpoint, load_messages_checkpoint

    _persist_messages_checkpoint(current, [], turn=3)
    current = State.reopen(
        "_reviewer", current.root.parent, current.run_id, project_worktree=mem_worktree
    )
    assert load_messages_checkpoint(current) is not None
    _, payload = await finish(current, subject, "next_revision")
    assert payload["verdict"] == "major_concerns"
    assert payload["recommended_action"]["action"] == "revise"
    finding = payload["question_findings"][0]
    assert finding["status"] == "carried_forward"
    assert finding["previous_review"]["artifact_id"] == prior["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["proof", "data", "definitions", "criteria", "use"])
async def test_changed_question_or_dependency_reopens_with_reason(tmp_path, mem_worktree, changed):
    author, subject, evidence_id, rows, deps = await fixture(tmp_path, mem_worktree)
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(first, subject, deps))["status"] == "success"
    prior, _ = await finish(first, subject)
    if changed == "proof":
        rows[0]["note"] += "; changed step 4"
    else:
        evidence = author.read_artifact(evidence_id)
        metadata = dict(evidence["metadata"])
        content = evidence["content"]
        if changed == "data":
            content += "; additional counterexample"
        else:
            metadata[changed] += "; changed"
        author.save_artifact("analysis_evidence", "Q2", content, metadata=metadata)
    await _update_research_state(author, verdict="continue", hypotheses=rows, change_reason=changed)
    current, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "reopened"' in prompt
    rejected = await record(
        current,
        subject,
        deps,
        question_status="carried_forward",
        previous_review_id=prior["id"],
        previous_review_version=prior["version"],
    )
    assert rejected["status"] == "error"
    assert "changed" in rejected["error"].lower()
    assert (
        await record(
            current,
            subject,
            deps,
            question_status="reopened",
            question_reason=changed + " changed; reassess the affected claim",
        )
    )["status"] == "success"
    _, payload = await finish(current, subject, "reopened_review")
    assert payload["question_findings"][0]["reason"]


@pytest.mark.asyncio
async def test_reorder_uses_question_identity_and_forged_receipts_do_not_pass(
    tmp_path, mem_worktree
):
    author, subject, _, rows, deps = await fixture(tmp_path, mem_worktree)
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(first, subject, deps))["status"] == "success"
    prior, _ = await finish(first, subject)
    await _update_research_state(
        author, verdict="continue", hypotheses=rows[::-1], change_reason="order only"
    )
    current, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "eligible"' in prompt
    for overrides in (
        {"previous_review_version": 99},
        {"question_id": "Q5"},
        {"intended_use": "population-wide asymptotic theorem"},
    ):
        args = dict(
            question_status="carried_forward",
            previous_review_id=prior["id"],
            previous_review_version=prior["version"],
        )
        args.update(overrides)
        assert (await record(current, subject, deps, **args))["status"] == "error"
    # The same verification is not silently repeated instead of carrying forward.
    assert (await record(current, subject, deps))["status"] == "error"
    assert (
        await record(
            current, subject, deps, question_reason="Independent spot check of the equality case"
        )
    )["status"] == "success"
    # Reviewer may request an independent spot check, but must say why.
    assert (
        await record(
            current,
            subject,
            deps,
            question_status="reopened",
            question_reason="Independent spot check of the equality case",
        )
    )["status"] == "success"


@pytest.mark.asyncio
async def test_missing_bindings_and_nonexistent_pointer_are_not_verified(tmp_path, mem_worktree):
    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    state, _ = reviewer(tmp_path, mem_worktree, subject)
    for dependencies in ([], deps[:-1], [{**r, "pointer": "/missing"} for r in deps]):
        result = await record(state, subject, dependencies)
        assert result["status"] == "error", result
    # Limitations are retained, never transformed into a verified credential.
    result = await record(
        state,
        subject,
        [],
        question_status="unverified",
        dependency_completeness="limited",
        dependency_limitations="Data not available",
        question_reason="Missing data binding",
    )
    assert result["status"] == "success", result
    _, payload = await finish(state, subject)
    assert payload["question_findings"][0]["status"] == "unverified"


@pytest.mark.asyncio
async def test_dependency_missing_or_legacy_review_never_becomes_a_pass(tmp_path, mem_worktree):
    author, subject, evidence_id, rows, deps = await fixture(tmp_path, mem_worktree)
    legacy, _ = reviewer(tmp_path, mem_worktree, subject)
    await compose(legacy, "add_strength", text="Q2 proof was checked")
    await finish(legacy, subject, "legacy_review")
    state, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "reference_only"' in prompt
    assert '"status": "eligible"' not in prompt
    assert (await record(state, subject, deps))["status"] == "success"
    await finish(state, subject)
    author.find_artifact_path(evidence_id).unlink()
    await _update_research_state(
        author, verdict="continue", hypotheses=rows, change_reason="Q5 wording"
    )
    _, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "reopened"' in prompt and "missing" in prompt.lower()


@pytest.mark.asyncio
async def test_stale_finalization_and_later_reopening_cannot_reuse_older_pass(
    tmp_path, mem_worktree
):
    author, subject, _, rows, deps = await fixture(tmp_path, mem_worktree)
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(first, subject, deps))["status"] == "success"
    prior, _ = await finish(first, subject)
    second, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (
        await record(
            second,
            subject,
            deps,
            question_status="reopened",
            question_reason="New contradiction in the equality argument",
        )
    )["status"] == "success"
    await finish(second, subject, "new_concern")
    third, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "eligible"' not in prompt
    assert (
        await record(
            third,
            subject,
            deps,
            question_status="carried_forward",
            previous_review_id=prior["id"],
            previous_review_version=prior["version"],
        )
    )["status"] == "error"
    assert (await record(third, subject, deps))["status"] == "success"
    rows[0]["note"] += "; changed after reviewer checked it"
    await _update_research_state(
        author, verdict="continue", hypotheses=rows, change_reason="proof changed"
    )
    result = await compose(
        third, "finalize", name="stale", artifact_under_review=subject, source_node_type="analysis"
    )
    assert result["status"] == "error", result


@pytest.mark.asyncio
async def test_same_title_or_cross_project_reference_does_not_match(tmp_path, mem_worktree):
    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(first, subject, deps))["status"] == "success"
    prior, _ = await finish(first, subject)
    # A forwarded credential retains its original project binding.
    other = tmp_path / "other-project"
    other.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", str(other)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(other),
            "-c",
            "user.name=test",
            "-c",
            "user.email=t@t",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
    )
    _, other_subject, _, _, other_deps = await fixture(tmp_path, other)
    receiver, _ = reviewer(tmp_path, other, other_subject)
    from core.artifact_provenance import forwarded

    receiver.save_artifact(
        "review_critique",
        "foreign",
        prior["content"],
        metadata=copy.deepcopy(prior["metadata"]),
        provenance=forwarded(
            prior["provenance"], via_node_type="_reviewer", via_run_id=receiver.run_id
        ),
    )
    _, prompt = reviewer(tmp_path, other, other_subject)
    assert '"status": "eligible"' not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["policy", "owner_spec"])
async def test_review_standard_change_invalidates_reuse(
    tmp_path, mem_worktree, monkeypatch, changed
):
    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(first, subject, deps))["status"] == "success"
    prior, _ = await finish(first, subject)
    current, _ = reviewer(tmp_path, mem_worktree, subject)
    if changed == "policy":
        import core.review_policy

        monkeypatch.setattr(core.review_policy, "REVIEW_POLICY_SHA256", "new-policy")
    else:
        # Change the actual currently delivered spec bytes, without touching
        # the repository's owner file or claiming a new standard in tool input.
        delivery = current.hook_state["_owner_review_spec_delivery"]
        path = tmp_path / "changed-owner-spec.md"
        path.write_text("A new scientific criterion")
        delivery["owner_spec_path"] = str(path)
    result = await record(
        current,
        subject,
        deps,
        question_status="carried_forward",
        previous_review_id=prior["id"],
        previous_review_version=prior["version"],
    )
    assert result["status"] == "error", result


@pytest.mark.asyncio
async def test_delivered_standard_matches_the_actual_owner_with_legacy_alias(
    tmp_path, mem_worktree
):
    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    current, _ = reviewer(tmp_path, mem_worktree, subject)
    build_messages(
        load_harness("_reviewer"),
        current,
        {"source_node_type": "literature", "artifact_id": subject},
    )
    result = await record(current, subject, deps)
    assert result["status"] == "error" and "owner" in result["error"]
    build_messages(
        load_harness("_reviewer"),
        current,
        {"source_node_type": "hypothesis", "artifact_id": subject},
    )
    assert (await record(current, subject, deps))["status"] == "success"


@pytest.mark.asyncio
async def test_context_eligibility_is_not_trusted_after_evidence_changes(tmp_path, mem_worktree):
    author, subject, evidence_id, _, deps = await fixture(tmp_path, mem_worktree)
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(first, subject, deps))["status"] == "success"
    prior, _ = await finish(first, subject)
    current, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "eligible"' in prompt
    evidence = author.read_artifact(evidence_id)
    author.save_artifact(
        "analysis_evidence", "Q2", "Counterexample found", metadata=evidence["metadata"]
    )
    current.hook_state["question_eligibility"] = {"Q2": "eligible"}
    result = await record(
        current,
        subject,
        deps,
        question_status="carried_forward",
        previous_review_id=prior["id"],
        previous_review_version=prior["version"],
        snapshot={"fingerprint": "caller-forged"},
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_partial_hook_preserves_findings_without_reusable_approval(tmp_path, mem_worktree):
    from types import SimpleNamespace

    from nodes._reviewer.hooks import _land_partial_critique

    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    state, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(state, subject, deps))["status"] == "success"
    # Only the question finding exists: no verdict/strength/concern is needed
    # for this recovery path to retain useful work.
    _land_partial_critique(SimpleNamespace(state=state, turn=4), None)
    artifacts = state.list_artifacts("review_critique", own_only=True)
    assert len(artifacts) == 1
    partial = state.read_artifact(artifacts[0]["id"])
    payload = json.loads(partial["content"])
    assert payload["question_findings"][0]["question_id"] == "Q2"
    assert payload["review_incomplete"]
    _, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "eligible"' not in prompt
    assert '"status": "reopened"' in prompt


@pytest.mark.asyncio
async def test_partial_reopening_after_an_earlier_finalize_is_not_lost(tmp_path, mem_worktree):
    from types import SimpleNamespace

    from nodes._reviewer.hooks import _land_partial_critique

    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    state, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(state, subject, deps))["status"] == "success"
    await finish(state, subject)
    assert (
        await record(
            state,
            subject,
            deps,
            question_status="reopened",
            question_reason="New contradiction after initial review",
        )
    )["status"] == "success"
    _land_partial_critique(SimpleNamespace(state=state, turn=8), None)
    assert len(state.list_artifacts("review_critique", own_only=True)) == 2
    _, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "eligible"' not in prompt
    assert '"status": "reopened"' in prompt


@pytest.mark.asyncio
async def test_positional_pointer_binds_identity_even_when_leaf_text_is_equal(
    tmp_path, mem_worktree
):
    author, subject, evidence_id, _, deps = await fixture(tmp_path, mem_worktree)
    evidence = author.read_artifact(evidence_id)
    metadata = evidence["metadata"]
    metadata["definitions"] = [{"id": "D1", "text": "same"}, {"id": "D2", "text": "same"}]
    author.save_artifact("analysis_evidence", "Q2", evidence["content"], metadata=metadata)
    deps[2]["pointer"] = "/metadata/definitions/0/text"
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(first, subject, deps))["status"] == "success"
    await finish(first, subject)
    metadata["definitions"].reverse()
    author.save_artifact("analysis_evidence", "Q2", evidence["content"], metadata=metadata)
    _, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert "definitions dependency changed" in prompt


@pytest.mark.asyncio
async def test_project_file_dependency_rechecks_at_finalize_and_rejects_escape(
    tmp_path, mem_worktree
):
    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    proof = mem_worktree / "proof.txt"
    proof.write_text("seven verified steps")
    deps[0] = {"role": "proof", "path": str(proof)}
    current, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(current, subject, deps))["status"] == "success"
    proof.write_text("step 4 modified after check")
    result = await compose(
        current,
        "finalize",
        name="changed_file",
        artifact_under_review=subject,
        source_node_type="analysis",
    )
    assert result["status"] == "error"
    outside = tmp_path / "outside.txt"
    outside.write_text("unrelated project")
    deps[0] = {"role": "proof", "path": str(outside)}
    assert (await record(current, subject, deps))["status"] == "error"


@pytest.mark.asyncio
async def test_prior_review_version_and_fresh_process_are_isolated(tmp_path, mem_worktree):
    import os
    import subprocess
    import sys
    from pathlib import Path

    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(first, subject, deps))["status"] == "success"
    prior, _ = await finish(first, subject)
    code = """
import json, sys
from pathlib import Path
from core.bootstrap import bootstrap
from core.context_engine import build_messages
from core.loader import load_harness
from core.state import State
bootstrap()
state=State.new('_reviewer', Path(sys.argv[1]), project_worktree=Path(sys.argv[2]))
messages=build_messages(load_harness('_reviewer'),state,
 {'source_node_type':'analysis','artifact_id':sys.argv[3]})
text='\\n'.join(m.content or '' for m in messages)
assert '\"status\": \"eligible\"' in text
print('persisted Q2 reference recovered')
"""
    check = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "runs"), str(mem_worktree), subject],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        timeout=20,
    )
    assert check.returncode == 0, check.stderr
    second, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (
        await record(
            second,
            subject,
            deps,
            question_status="reopened",
            question_reason="Independent check found a contradiction",
        )
    )["status"] == "success"
    newer, _ = await finish(second, subject)  # same artifact identity, next version
    assert newer["version"] == prior["version"] + 1
    current, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (
        await record(
            current,
            subject,
            deps,
            question_status="carried_forward",
            previous_review_id=prior["id"],
            previous_review_version=prior["version"],
        )
    )["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash_mismatch", "missing", "invalid_utf8", "raises_oserror"])
async def test_unreadable_latest_reopening_never_resurrects_an_old_pass(
    tmp_path,
    mem_worktree,
    damage,
    monkeypatch,
):
    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    first, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (await record(first, subject, deps))["status"] == "success"
    old, _ = await finish(first, subject, "old_verified")
    newer, _ = reviewer(tmp_path, mem_worktree, subject)
    assert (
        await record(
            newer,
            subject,
            deps,
            question_status="reopened",
            question_reason="A new concrete contradiction",
        )
    )["status"] == "success"
    latest, _ = await finish(newer, subject, "latest_reopening")
    path = newer.find_artifact_path(latest["id"])
    if damage == "missing":
        path.unlink()
    elif damage == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif damage == "raises_oserror":
        read = State.read_artifact

        def unavailable(state, artifact_id):
            if artifact_id == latest["id"]:
                raise OSError("review backing store became unavailable")
            return read(state, artifact_id)

        monkeypatch.setattr(State, "read_artifact", unavailable)
    else:
        path.write_text(latest["content"] + "\n")
    current, prompt = reviewer(tmp_path, mem_worktree, subject)
    assert '"status": "eligible"' not in prompt
    assert latest["id"] in prompt
    result = await record(
        current,
        subject,
        deps,
        question_status="carried_forward",
        previous_review_id=old["id"],
        previous_review_version=old["version"],
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["resolve", "before_open", "after_open"])
async def test_file_binding_rejects_path_replacement_without_reading_outside(
    tmp_path,
    mem_worktree,
    monkeypatch,
    phase,
):
    import os

    from core import project_workspace

    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    state, _ = reviewer(tmp_path, mem_worktree, subject)
    proof = mem_worktree / "proof.txt"
    proof.write_text(PROOF)
    outside = tmp_path / "outside-proof.txt"
    outside.write_text("OUTSIDE-READ-BOUNDARY")
    deps[0] = {"role": "proof", "path": str(proof)}
    real_resolve = project_workspace.resolve_tool_path
    real_open = os.open

    def replace():
        proof.unlink()
        proof.symlink_to(outside)

    def resolve_then_replace(*args, **kwargs):
        path = real_resolve(*args, **kwargs)
        if path == proof:
            replace()
        return path

    def open_then_replace(path, *args, **kwargs):
        is_proof = str(path) == str(proof) or (str(path) == proof.name and "dir_fd" in kwargs)
        if is_proof and phase == "before_open":
            replace()
        fd = real_open(path, *args, **kwargs)
        if is_proof and phase == "after_open":
            replace()
        return fd

    if phase == "resolve":
        monkeypatch.setattr(project_workspace, "resolve_tool_path", resolve_then_replace)
    else:
        monkeypatch.setattr(os, "open", open_then_replace)
    result = await record(state, subject, deps)
    assert result["status"] == "error", result


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_directory_open", "after_directory_open"])
async def test_file_binding_opens_each_parent_without_following_symlinks(
    tmp_path,
    mem_worktree,
    monkeypatch,
    phase,
):
    import os

    from core import review_reuse

    if review_reuse._WINDOWS:
        pytest.skip("POSIX directory-descriptor regression; Windows verifies the opened handle")
    _, subject, _, _, deps = await fixture(tmp_path, mem_worktree)
    state, _ = reviewer(tmp_path, mem_worktree, subject)
    parent = mem_worktree / "proofs"
    parent.mkdir()
    proof = parent / "proof.txt"
    proof.write_text(PROOF)
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    (outside / "proof.txt").write_text("OUTSIDE-READ-BOUNDARY")
    deps[0] = {"role": "proof", "path": str(proof)}
    real_open = os.open

    def replace():
        parent.rename(mem_worktree / "original-proofs")
        parent.symlink_to(outside, target_is_directory=True)

    def raced_open(path, *args, **kwargs):
        is_parent = str(path) == parent.name and "dir_fd" in kwargs
        if is_parent and phase == "before_directory_open":
            replace()
        fd = real_open(path, *args, **kwargs)
        if is_parent and phase == "after_directory_open":
            replace()
        return fd

    monkeypatch.setattr(os, "open", raced_open)
    result = await record(state, subject, deps)
    assert result["status"] == "error", result


def test_windows_open_checks_the_handle_target_before_any_content_read(tmp_path, monkeypatch):
    import hashlib
    import os

    from core import review_reuse

    proof = (tmp_path / "proof.txt").resolve()
    proof.write_text(PROOF)
    monkeypatch.setattr(review_reuse, "_WINDOWS", True)
    monkeypatch.setattr(os, "O_BINARY", getattr(os, "O_BINARY", 0), raising=False)
    monkeypatch.setattr(review_reuse, "_windows_fd_path", lambda fd: str(proof))
    assert review_reuse._file_hash(proof) == hashlib.sha256(PROOF.encode()).hexdigest()
    monkeypatch.setattr(review_reuse, "_windows_fd_path", lambda fd: str(tmp_path / "outside.txt"))

    def unexpected_read_hash(*args):
        pytest.fail("must reject the outside handle before hashing any content")

    monkeypatch.setattr(review_reuse.hashlib, "sha256", unexpected_read_hash)
    with pytest.raises(ValueError, match="opened handle escaped"):
        review_reuse._file_hash(proof)


def test_windows_final_path_query_resizes_and_normalizes_dos_and_unc(tmp_path, monkeypatch):
    import ctypes
    import sys
    from types import SimpleNamespace

    from core import review_reuse

    actual = "\\\\?\\C:\\" + "a" * 700 + "\\proof.txt"
    calls = []

    def query(handle, buffer, capacity, flags):
        assert handle == 42 and flags == 0
        calls.append(capacity)
        if capacity <= len(actual):
            return len(actual) + 1
        buffer.value = actual
        return len(actual)

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 42))
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *a, **kw: SimpleNamespace(GetFinalPathNameByHandleW=query),
        raising=False,
    )
    assert review_reuse._windows_fd_path(7) == actual
    assert len(calls) == 2
    assert review_reuse._windows_path_key("\\\\?\\C:\\Proof.txt") == "c:\\proof.txt"
    assert (
        review_reuse._windows_path_key("\\\\?\\UNC\\server\\share\\Proof.txt")
        == "\\\\server\\share\\proof.txt"
    )

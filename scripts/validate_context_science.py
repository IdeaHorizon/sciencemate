"""Real-model scientific pilots; existing source projects are read-only.

Run with the project's Python environment, --data-root pointing to the directory
containing deliverables/ and reports/. Credentials use CONTEXT_VALIDATION_API_KEY
or DEEPSEEK_API_KEY. Output includes raw model answers and explicit checks, not
an LLM self-assessment. This is a controlled pilot, not a production E2E claim.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import context_view, jobs, memory, memory_delivery, summarizer as sm
from core.conversation_store import (
    load_conversation, render_context_change, save_conversation, unresolved_tool_calls,
)
from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMClient, LLMMessage, framework_notice
from core.state import State
from core.artifact_provenance import imported
from nodes.analysis.tools.research_state import _read_research_state, _update_research_state
from shared.lib import process_control as pc
from shared.tools.library.kb import write_claim, _search_kb


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def read(path):
    return json.loads(path.read_text())


def read_all(view, ref):
    parts, offset = [], 0
    while True:
        page = view.read_result(ref, offset=offset)
        parts.append(page["content"])
        if not page["truncated"]:
            return "".join(parts)
        offset = page["next_offset"]


def source(data_root, relative, hashes):
    path = data_root / relative
    raw = path.read_bytes()
    hashes[relative] = hashlib.sha256(raw).hexdigest()
    d = json.loads(raw)
    return d.get("content", json.dumps(d, ensure_ascii=False))


def append_report(messages, text, prefix):
    # Replay the actual shape of scientific reports saved through save_artifact.
    # These are derived test exchanges, not a claim to be the original transcript.
    for i, start in enumerate(range(0, len(text), 5000)):
        call_id = f"{prefix}-{i}"
        messages.extend([
            LLMMessage(role="assistant", tool_calls=[{
                "id": call_id, "type": "function", "function": {
                    "name": "save_artifact", "arguments": json.dumps({
                        "artifact_type": "analysis_report", "name": prefix,
                        "content": text[start:start + 5000]}, ensure_ascii=False)}}]),
            LLMMessage(role="tool", name="save_artifact", tool_call_id=call_id,
                       content=json.dumps({"status": "success", "report": prefix})),
        ])


async def compact_round(state, messages, llm, turn, output):
    h = NodeHarness(node_type="analysis", max_context_tokens=18_000, max_output_tokens=4096,
                    summarizer=SummarizerConfig(strategy="llm", target_tokens=2400,
                                                keep_last_n_turns=6))
    expected = [(m.content, m.context_slot) for m in messages if m.context_slot == "research_state"]
    candidate = await sm.run_summarizer(h, state, messages, llm, turn, sm.estimate_tokens(messages))
    outcome = dict(state.hook_state["_context_compaction"])
    if not outcome["target_met"]:
        raise AssertionError(outcome)
    assert [(m.content, m.context_slot) for m in candidate if m.context_slot == "research_state"] == expected
    assert not unresolved_tool_calls(candidate)
    render_context_change(state, messages, candidate, turn=turn, source="scientific_pilot")
    ledger = next((m.content for m in messages if m.context_slot == sm.CONTINUATION_SLOT), "")
    assert ledger, "A real successful ledger must survive archive fallback"
    (output / f"ledger-{turn}.txt").write_text(ledger)
    save_conversation(state, messages)
    reopened = State.reopen("analysis", state.root.parent, state.run_id)
    reopened.project_root, reopened.project_worktree = state.project_root, state.project_worktree
    restored = load_conversation(reopened)
    assert restored is not None
    view = context_view.get(reopened)
    archives = json.loads(read_all(view, view.history_index_ref()))
    for item in archives:
        assert json.loads(read_all(view, item["result_ref"]))
    outcome["archives_directly_readable"] = len(archives)
    return reopened, restored, outcome


async def scientific_state(state, question, findings, reason):
    result = await _update_research_state(
        state, verdict="continue", research_question=question, hypotheses=[],
        change_reason=reason, completed_experiments=[{"evidence": findings}],
        gaps=["Controlled context pilot; no new scientific verdict is issued."],
        next_steps=["Read original evidence before revising the conclusion."],
    )
    assert result["status"] == "success", result
    snap = framework_notice(json.dumps(await _read_research_state(state), ensure_ascii=False))
    snap.context_slot = "research_state"
    return snap


async def answer(llm, messages, prompt, output):
    # Keep transport output so format/sanitizer failures cannot masquerade as
    # a model recall failure. No request headers or credentials are recorded.
    originals = {}
    for name in ("_stream_with_retry", "_post_with_retry"):
        originals[name] = getattr(llm, name)
        async def capture(payload, _original=originals[name], **kwargs):
            data = await _original(payload, **kwargs)
            dump(output / "provider-raw.json", data)
            return data
        setattr(llm, name, capture)
    try:
        response = await llm.chat([*messages, LLMMessage(role="user", content=prompt)],
                                  max_tokens=6000, temperature=0.2)
    finally:
        for name, original in originals.items(): setattr(llm, name, original)
    (output / "continuation.txt").write_text(response.content or "")
    dump(output / "continuation-usage.json", {"finish_reason": response.finish_reason, "usage": response.usage})
    assert response.finish_reason != "length" and response.content
    return response.content


async def verify_future_memory(llm, old_state, output):
    """A fresh task gets no conversation summary, only existing memory/KB paths."""
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import _memory_onboarding_on_turn_start

    fresh = State.new("analysis", old_state.root.parent)
    fresh.project_root = old_state.project_root
    fresh.project_worktree = old_state.project_worktree
    h = NodeHarness(node_type="analysis", tools=[])
    notices = _memory_onboarding_on_turn_start(HookContext(h, fresh, [], 1))
    assert notices and "measurement interval" in notices[0].content
    kb = await _search_kb(fresh, entity_type="claims", query="runtime", scope="project", projection="full")
    claim = kb["records"][0]
    chunk = fresh.get_kb_record("chunks", claim["sources"][0])
    assert chunk and chunk["origin_content_hash"] == hashlib.sha256(chunk["text"].encode()).hexdigest()
    stored = framework_notice(json.dumps(kb, ensure_ascii=False))
    stored.context_slot = "kb_recall"
    output.mkdir(exist_ok=True)
    text = await answer(llm, [LLMMessage(role="system", content="Use retrieved evidence and preserve its experimental scope."),
                             *notices, stored],
        "A new task proposes changing the Ising measurement interval from 2 to 1 and asks whether the old result guarantees a two-hour run. Return only JSON with original_meas_interval (integer), original_n_seeds, original_n_meas, guarantee_two_hours (boolean), and explanation. You have no old conversation; use the retrieved KB conditions.", output)
    parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
    checks = {"new_task_automatic_memory_hook": bool(notices), "evidence_hash_matches": True,
              "original_interval": parsed.get("original_meas_interval") == 2,
              "original_seed_count": parsed.get("original_n_seeds") == 5,
              "original_measurement_count": parsed.get("original_n_meas") == 100000,
              "no_unjustified_runtime_guarantee": parsed.get("guarantee_two_hours") is False}
    dump(output / "checks.json", {"checks": checks, "answer": parsed,
                                 "retrieved_claim": claim["id"], "retrieved_chunk": chunk["id"],
                                 "old_conversation_supplied": False})
    assert all(checks.values()), checks
    return checks


async def run(args):
    if args.direct:
        for key in list(os.environ):
            if key.lower().endswith("_proxy"):
                os.environ.pop(key)
    llm = LLMClient(api_key=os.getenv("CONTEXT_VALIDATION_API_KEY") or os.environ["DEEPSEEK_API_KEY"],
                    model=args.model, base_url=args.base_url, timeout=180, max_retries=0)
    args.output.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="context-science-pilot-"))
    hashes = read(args.output / "source-hashes.json") if (args.output / "source-hashes.json").exists() else {}
    results = read(args.output / "results.json") if (args.output / "results.json").exists() else {}
    dump(args.output / ("run-" + "-".join(args.cases) + ".json"), {"model": llm.model, "endpoint": llm.base_url,
         "runtime": str(runtime), "scope": "archived-data replay + real-model continuation + bounded local job"})

    if "ising" in args.cases:
        # Case 1: raw Binder measurements and the archived log are the authority.
        base = "deliverables/e2e_v26_ising_tc_20260813/"
        raw = json.loads(source(args.data_root, base + "raw_results.json", hashes))
        clean = json.loads(source(args.data_root, base + "clean_results__2D_Ising_Tc_CleanResults.json", hashes))
        log = source(args.data_root, base + "experiment_log__2D_Ising_Tc_ExperimentLog.json", hashes)
        wall = raw["metadata"]["wall_time_s"]
        binder_error = max(abs((1 - r["avg_m4"] / (3 * r["avg_m2"] ** 2)) - r["U_L"])
                           for r in raw["results"])
        assert binder_error < 1e-12
        facts = {"raw_rows": len(raw["results"]), "binder_max_error": binder_error,
                 "wall_hours": wall / 3600, "budget_hours": 2, "runtime_ratio": wall / 7200,
                 "fss": clean["fss_extrapolation"], "verdict": clean["verdict"]}
        state = State.new("analysis", runtime / "ising")
        output = args.output / "ising"; output.mkdir(exist_ok=True)
        state.project_worktree = runtime / "ising-project"
        state.project_worktree.mkdir()
        state.project_root = state.project_worktree
        # Use existing memory and KB. A scientific assertion goes to KB; the
        # associated reusable workflow lesson goes to MEMORY.md with applicability.
        memory.append_manual(state, text="For Ising runtime estimates, retain lattice size, measurement interval and measured wall time; an accuracy pass does not establish a runtime pass.",
                             section=memory.SECTION_METHOD, nodes=["analysis"], tools=[], run_id=state.run_id)
        evidence = state.save_artifact("analysis_report", "Ising_pinned_evidence", log,
            provenance=imported(source_path=base + "experiment_log__2D_Ising_Tc_ExperimentLog.json",
                                sha256=hashes[base + "experiment_log__2D_Ising_Tc_ExperimentLog.json"],
                                size_bytes=len(log.encode()), by_run_id=state.run_id,
                                note="Read-only archived evidence for context validation"))
        kb = await write_claim(state, claim_type="empirical", scope="project",
            claim_text="The two-hour Ising runtime claim failed on the measured Wolff configuration: 31868.398079 seconds, L=16..256, five seeds, 100000 measurements per seed. Accuracy under the unweighted crossing extrapolation does not establish runtime feasibility. Effective sample size was not measured from these aggregates.",
            scope_dimensions={"algorithm": "Wolff", "L": [16,32,64,128,256], "n_seeds": 5,
                              "n_meas": 100000, "meas_interval": 2}, sources=[evidence["id"]],
            orphan_reason="Isolated replay of an existing experiment; no concept catalogue imported.")
        assert kb["status"] == "success", kb
        claim_id = kb["id"]
        question = "Can the Ising result support both accuracy and the two-hour runtime claim?"
        snap = await scientific_state(state, question, facts, "Recompute from pinned raw measurements.")
        messages = [LLMMessage(role="system", content="You are a research analyst. Preserve conditional conclusions and evidence scope."),
                    LLMMessage(role="user", content=question), snap]
        rounds = []
        for turn in (21, 42, 63):
            append_report(messages, json.dumps(raw) + json.dumps(clean) + log + "\nRecomputed measurements:\n" + json.dumps(facts), f"ising-{turn}")
            state, messages, outcome = await compact_round(state, messages, llm, turn, output)
            rounds.append(outcome)
            print("ising", turn, outcome["strategy"], flush=True)
        # The next question now makes the old runtime condition relevant. Retrieve
        # from the original stores after all compactions and restart cycles.
        recalled = await _search_kb(state, entity_type="claims", query="runtime", scope="project", projection="full")
        assert claim_id in json.dumps(recalled)
        full_claim = state.get_kb_record("claims", claim_id)
        assert full_claim and full_claim["scope_dimensions"]["n_seeds"] == 5
        assert full_claim["sources"] and all(state.get_kb_record("chunks", x) for x in full_claim["sources"])
        delivery = memory_delivery.onboarding_slice(state, "analysis")
        assert delivery and "measurement interval" in delivery
        dump(output / "memory-kb-recall.json", {"manual_delivery": delivery, "search": recalled, "full_claim": full_claim,
             "semantic_retrieval": "not exercised; explicit keyword lookup and automatic applicability delivery"})
        await verify_future_memory(llm, state, output / "future-memory")
        text = await answer(llm, messages, "Write a brief English scientific conclusion. State which claim failed, the measured runtime versus budget, the Binder convention and its conversion, and distinguish measurement count from effective sample size. Cite the source records; do not invent missing diagnostics.", output)
        checks = {"runtime_failure": bool(re.search(r"8\.85|31,?868", text)),
                  "two_hour_budget": bool(re.search(r"2[ -]hour|two.hour|7200|7,200", text, re.I)),
                  "binder_conversion": "1.5" in text,
                  "no_measured_neff": bool(re.search(r"cannot|not (?:directly )?(?:measured|available|comput)|unavailable|not identifiable", text, re.I)),
                  "memory_delivered_after_restart": bool(delivery), "kb_conditions_and_evidence_restored": bool(full_claim)}
        results["ising"] = {"checks": checks, "rounds": rounds, "recomputed": facts}
        dump(args.output / "results.json", results)

    if "job" in args.cases:
        # Case 2: real local computation, existing process-control and job ledger.
        state = State.new("analysis", runtime / "job")
        state.project_root = runtime / "job-project"; state.project_root.mkdir()
        work = state.project_root
        worker = work / "integrate.py"
        worker.write_text(textwrap.dedent('''
    import json, math, time
    from pathlib import Path
    with Path("launches.txt").open("a") as f: f.write("launch\\n")
    for level in range(8, 15):
        n = 2**level
        value = sum(math.exp(-((i+.5)/n)**2) for i in range(n))/n
        Path("progress.json").write_text(json.dumps({"n":n,"integral":value}))
    Path("samples.json").write_text(json.dumps([math.exp(-((i+.5)/2048)**2) for i in range(2048)]))
    deadline = time.monotonic()+600
    while not Path("release").exists():
        if time.monotonic()>deadline: raise TimeoutError("pilot deadline")
        time.sleep(.1)
    Path("result.json").write_text(Path("progress.json").read_text())
    '''))
        # A controlled local worker, deliberately not a replay of the original HPC job.
        proc = subprocess.Popen([sys.executable, str(worker)], cwd=work,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **pc.group_spawn_kwargs())
        group = pc.Group.of(proc.pid)
        output = args.output / "job"; output.mkdir(exist_ok=True)
        try:
            job = jobs.declare(state, purpose="Numerical quadrature convergence; context pilot",
                               pid=proc.pid, resources="1 CPU", progress_probe=str(work / "progress.json"))
            for _ in range(100):
                if (work / "samples.json").exists(): break
                await asyncio.sleep(.05)
            assert (work / "progress.json").exists() and proc.poll() is None
            messages = [LLMMessage(role="system", content="Continue existing jobs by inspecting their handles; never relaunch an already-running job."),
                        LLMMessage(role="user", content="Track the numerical integral and report measured output only.")]
            rounds = []
            for turn in (21, 42, 63):
                receipt = jobs.render_for_orchestrator(state) + "\n" + json.dumps([j.as_dict() for j in jobs.load(state, only_open=True)])
                snap = framework_notice(receipt); snap.context_slot = "jobs"
                messages = [m for m in messages if m.context_slot != "jobs"] + [snap]
                append_report(messages, (work / "samples.json").read_text() + "\nJob observation: " + receipt, f"job-{turn}")
                state, messages, outcome = await compact_round(state, messages, llm, turn, output)
                current = jobs.load(state, only_open=True)
                assert len(current) == 1 and current[0].job_id == job.job_id and current[0].pid == proc.pid
                assert pc.alive(proc.pid) and (work / "launches.txt").read_text().splitlines() == ["launch"]
                rounds.append(outcome)
                print("job", turn, outcome["strategy"], flush=True)
            text = await answer(llm, messages, "The registered integral job is still running. Return JSON with job_id, pid, next_action and may_resubmit (boolean). Continue from the exact observed handle. Do not issue a scientific result for a job whose completion you have not observed.", output)
            try:
                parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
            except ValueError:
                parsed = {}  # Record failure and still evaluate the remaining case.
            (work / "release").touch()
            await asyncio.to_thread(proc.wait, timeout=15)
            measured = read(work / "result.json")
            error = abs(measured["integral"] - math.sqrt(math.pi) / 2 * math.erf(1))
            assert proc.returncode == 0 and error < 1e-8
            jobs.update(state, job.job_id, status="completed", last_progress=json.dumps(measured))
            results["job"] = {"checks": {"exact_handle": parsed.get("job_id") == job.job_id and parsed.get("pid") == proc.pid,
                "no_resubmit": parsed.get("may_resubmit") is False,
                "single_launch": (work / "launches.txt").read_text().splitlines() == ["launch"],
                "measured_convergence": error < 1e-8}, "rounds": rounds, "measured": measured,
                "absolute_error": error, "job_id": job.job_id, "pid": proc.pid}
        finally:
            if proc.poll() is None:
                group.kill(); proc.wait(timeout=10)
                jobs.update(state, job.job_id, status="cancelled", note="Controlled pilot failed; worker cleaned up.")
            group.close()
        dump(args.output / "results.json", results)

    if "rsi" in args.cases:
        # Case 3: revised data supersedes an old draft; no new experimental jobs.
        base = "reports/rsi-continuation-20261113/"
        correction = source(args.data_root, base + "RSI_v2_verified_evidence_corrections.json", hashes)
        audit = json.loads(source(args.data_root, base + "q2_budget_audit.json", hashes))
        star = json.loads(source(args.data_root, base + "e_star.json", hashes))
        spend = {arm: sum(audit["spend_by_arm_seed"][arm][str(i)] for i in range(2, 6))
                 for arm in audit["search_arms"]}
        ratio = max(spend.values()) / min(spend.values())
        assert math.isclose(ratio, audit["ratio_search_v14_seeds"], rel_tol=1e-10)
        max_overrun = max(audit["over_budget_cells"].values())
        facts = {"search_ratio_seeds_2_to_5": ratio, "max_overrun_tokens": max_overrun,
                 "max_overrun_percent": 100 * max_overrun / audit["budget_per_arm_per_seed"],
                 "selfmod_episodes_at_E_star": [star["arms"][f"A_selfmod__seed{i}"]["episodes_at_g_star"] for i in range(1, 6)]}
        state = State.new("analysis", runtime / "rsi")
        output = args.output / "rsi"; output.mkdir(exist_ok=True)
        question = "Revise the RSI results paragraph after verification, without launching new experiments."
        messages = [LLMMessage(role="system", content="You are revising a scientific manuscript. New verified evidence supersedes older drafts. Do not generalize accounting scope."),
                    LLMMessage(role="user", content=question)]
        old_draft = "Old draft pending verification: search ratio 1.97; max overrun 86,257 (1.38%); every cell exactly 480 episodes; random E* at generation 1."
        append_report(messages, old_draft, "rsi-old-draft")
        rounds = []
        for turn in (21, 42, 63):
            snap = await scientific_state(state, question, facts, "Pinned original audit overrides unverified older draft.")
            messages = [m for m in messages if m.context_slot != "research_state"] + [snap]
            original = (source(args.data_root, base + "q1_trajectory_dev.json", hashes)
                        + source(args.data_root, base + "RSI_v2_verified_trajectories.json", hashes))
            append_report(messages, original + json.dumps(audit) + json.dumps(star) + correction + "\nRecomputed from original JSON:\n" + json.dumps(facts), f"rsi-{turn}")
            state, messages, outcome = await compact_round(state, messages, llm, turn, output)
            rounds.append(outcome)
            print("rsi", turn, outcome["strategy"], flush=True)
        text = await answer(llm, messages, "Write the corrected English results paragraph only, without quoting obsolete claims. Report the audited search ratio and its seed/stage scope, maximum overrun tokens and percent, and the selfmod prefix episode counts. Distinguish search accounting from all-stage accounting. Do not run new experiments.", output)
        checks = {"ratio_correct": "1.8297" in text or "1.83" in text,
                  "overrun_correct": bool(re.search(r"76,?257", text)) and ("1.22" in text),
                  "variable_prefix": all(str(x) in text for x in (480, 504, 492)),
                  "no_obsolete_numbers": not any(x in text for x in ("1.97", "86,257", "86257", "1.38%")),
                  "scope_separated": "search" in text.lower() and bool(re.search(r"all.stage|all stage", text, re.I))}
        results["rsi"] = {"checks": checks, "rounds": rounds, "recomputed": facts}
    dump(args.output / "source-hashes.json", hashes)
    dump(args.output / "results.json", results)
    assert all(all(case["checks"].values()) for case in results.values()), results
    print("All mechanical pilot checks passed; inspect the saved answers for unsupported scientific claims.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--direct", action="store_true", help="Ignore proxy environment for this validation process only")
    parser.add_argument("--cases", nargs="+", choices=["ising", "job", "rsi"], default=["ising", "job", "rsi"])
    asyncio.run(run(parser.parse_args()))

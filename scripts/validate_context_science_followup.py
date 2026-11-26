"""Finish the live-job and corrected-data pilots, retaining raw failed attempts.

Uses the same compaction/restart driver as validate_context_science.py. Every
reconstructed save exchange below performs the real write in an isolated run.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.validate_context_science import dump, read, read_all, source
from core import context_view, jobs, research_state_reader, summarizer as sm
from core.artifact_provenance import imported
from core.conversation_store import load_conversation, msg_to_jsonable, render_context_change, save_conversation, unresolved_tool_calls
from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMClient, LLMMessage, framework_notice, framework_notice_body
from core.state import State
from nodes.analysis.tools.research_state import _read_research_state, _update_research_state
from shared.lib import process_control as pc


def append_persisted(state, messages, text, prefix):
    for index, start in enumerate(range(0, len(text), 5000)):
        content = text[start:start+5000]
        args = {"artifact_type": "analysis_report", "name": f"{prefix}-part-{index}", "content": content}
        receipt = state.save_artifact(**args, provenance=imported(
            source_path=f"controlled-pilot:{prefix}:{start}",
            sha256=hashlib.sha256(content.encode()).hexdigest(), size_bytes=len(content.encode()),
            by_run_id=state.run_id, note="Copied original or recomputed pilot evidence; no new experimental verdict."))
        call_id = f"{prefix}-{index}"
        messages += [LLMMessage(role="assistant", tool_calls=[{"id":call_id,"type":"function",
            "function":{"name":"save_artifact","arguments":json.dumps(args)}}]),
            LLMMessage(role="tool", name="save_artifact", tool_call_id=call_id,
                       content=json.dumps(receipt))]


async def scientific_state(state, question, findings, reason):
    # Use the existing completed_experiments contract. The old pilot put a
    # dict in an unrecognized `evidence` field, rendering the row as `?`.
    pinned = state.save_artifact("analysis_report", "Verified_continuation_facts",
                                 json.dumps(findings, ensure_ascii=False, indent=2))
    result = await _update_research_state(
        state, verdict="continue", research_question=question, hypotheses=[], change_reason=reason,
        completed_experiments=[{"artifact_id":pinned["id"],"credibility":"controlled_recomputation",
                                "note":json.dumps(findings, ensure_ascii=False)}],
        gaps=["Controlled archived-data pilot; no new scientific verdict is authorized."],
        next_steps=["Use the pinned facts and verify cited evidence; preserve unresolved source discrepancies."])
    assert result["status"] == "success", result
    notice = framework_notice(json.dumps(await _read_research_state(state), ensure_ascii=False))
    notice.context_slot = "research_state"
    return notice


async def compact_round(state, messages, llm, turn, output):
    """Test the production owner binding, without requiring a pilot-only slot."""
    harness = NodeHarness(node_type="analysis",max_context_tokens=18_000,max_output_tokens=4096,
                           summarizer=SummarizerConfig(strategy="llm",target_tokens=2400,keep_last_n_turns=6))
    source = [msg_to_jsonable(message) for message in messages]
    science_before = research_state_reader.read_for_state(state)
    pending = unresolved_tool_calls(messages)
    candidate = await sm.run_summarizer(harness,state,messages,llm,turn,sm.estimate_tokens(messages))
    outcome = dict(state.hook_state["_context_compaction"])
    assert outcome["target_met"], outcome
    assert json.loads(read_all(context_view.get(state),outcome["history_ref"])) == source
    assert unresolved_tool_calls(candidate) == pending
    assert research_state_reader.read_for_state(state) == science_before
    if science_before.get("exists"):
        snapshots = [message for message in candidate if message.context_slot=="research_state"]
        assert len(snapshots)==1
        projected = json.loads(framework_notice_body(snapshots[0]))
        if projected.get("mode")=="reference":
            assert projected["version"]==science_before["version"]
            assert projected["content_sha256"]==hashlib.sha256(science_before["content"].encode()).hexdigest()
        else:
            assert projected == science_before
    render_context_change(state,messages,candidate,turn=turn,source="scientific_owner_pilot")
    ledger = next((message.content for message in messages if message.context_slot==sm.CONTINUATION_SLOT),"")
    if ledger:
        (output/f"ledger-{turn}.txt").write_text(ledger)
    save_conversation(state,messages)
    reopened = State.reopen("analysis",state.root.parent,state.run_id)
    reopened.project_root,reopened.project_worktree = state.project_root,state.project_worktree
    restored = load_conversation(reopened)
    assert restored is not None and research_state_reader.read_for_state(reopened)==science_before
    view = context_view.get(reopened)
    archives = json.loads(read_all(view,view.history_index_ref()))
    assert all(json.loads(read_all(view,item["result_ref"])) for item in archives)
    outcome.update({"archives_directly_readable":len(archives),"archive_exact":True,
                    "scientific_owner_unchanged":True,"continuation_ledger_present":bool(ledger)})
    return reopened,restored,outcome


class RecordedClient(LLMClient):
    """Record visible provider output before the normal firewall, without reasoning."""
    def capture(self, data):
        message = data["choices"][0]["message"]
        self.raw.append({"content":message.get("content"), "finish_reason":data["choices"][0].get("finish_reason"),
                         "tool_calls":message.get("tool_calls"), "usage":data.get("usage")})
        return data

    async def _post_with_retry(self, *args, **kwargs):
        return self.capture(await super()._post_with_retry(*args, **kwargs))

    async def _stream_with_retry(self, *args, **kwargs):
        return self.capture(await super()._stream_with_retry(*args, **kwargs))


async def structured_answer(llm, messages, prompt, output, *, state=None):
    from core.tool_registry import execute, get_tool, to_openai_schema

    allowed = {"read_artifact", "read_research_state", "read_tool_result"} if (
        state is not None and getattr(llm,"evidence_tools",False)) else set()
    schemas = [to_openai_schema(get_tool(name)) for name in sorted(allowed)] or None
    attempts = []
    for attempt in range(2):
        llm.raw = []
        work = [*messages, LLMMessage(role="user", content=prompt + (
            "\nReturn only a complete JSON object, beginning with { and ending with }." if attempt else ""))]
        steps = []
        for tool_turn in range(6):
            response = await llm.chat(work, tools=schemas, max_tokens=6000, temperature=.2)
            if not response.tool_calls:
                break
            work.append(LLMMessage(role="assistant",content=response.content,tool_calls=response.tool_calls,
                                   reasoning_content=response.reasoning_content))
            for call in response.tool_calls:
                function = call["function"]
                name = function["name"]
                if name not in allowed:
                    receipt = {"status":"error","error":"Tool not offered in this read-only validation"}
                else:
                    try:
                        receipt = await execute(name,state,**json.loads(function["arguments"]))
                    except (ValueError,TypeError) as error:
                        receipt = {"status":"error","error":str(error)}
                steps.append({"name":name,"arguments":function["arguments"],"receipt":receipt})
                work.append(LLMMessage(role="tool",name=name,tool_call_id=call["id"],content=json.dumps(receipt,ensure_ascii=False)))
        text = (response.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n",1)[1].rsplit("```",1)[0].strip()
        record = {"raw_provider":llm.raw, "content":response.content, "finish_reason":response.finish_reason,
                  "tool_calls":response.tool_calls, "tool_steps":steps, "protocol_leak":response.protocol_leak,
                  "provider_recovery":response.provider_recovery,"usage":response.usage}
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict) or response.finish_reason == "length" or response.tool_calls or response.protocol_leak:
                raise ValueError("Incomplete or non-object answer")
            record["valid_object"] = True
            attempts.append(record)
            dump(output / "answer-attempts.json", attempts)
            return parsed, len(attempts)
        except ValueError:
            record["valid_object"] = False
            attempts.append(record)
            dump(output / "answer-attempts.json", attempts)
    return {}, len(attempts)


async def run_ising(args,llm,runtime,hashes,results):
    # Repeat Ising with real imported records too, so citations can be checked
    # against the existing artifact owner rather than only replayed message names.
    base = "deliverables/e2e_v26_ising_tc_20260813/"
    raw = json.loads(source(args.data_root,base+"raw_results.json",hashes))
    clean = json.loads(source(args.data_root,base+"clean_results__2D_Ising_Tc_CleanResults.json",hashes))
    log = source(args.data_root,base+"experiment_log__2D_Ising_Tc_ExperimentLog.json",hashes)
    pairs = list(clean["crossings"].values())
    xs,ys = [1/p["L1"] for p in pairs],[p["T_cross"] for p in pairs]
    slope = sum((x-statistics.mean(xs))*(y-statistics.mean(ys)) for x,y in zip(xs,ys))/sum((x-statistics.mean(xs))**2 for x in xs)
    tc = statistics.mean(ys)-slope*statistics.mean(xs)
    facts = {"tc":tc,"normalized_deviation":abs(tc-2/math.log(1+math.sqrt(2)))/clean["fss_extrapolation"]["sigma_bootstrap"],
             "reference_tc":2/math.log(1+math.sqrt(2)),"sigma_bootstrap":clean["fss_extrapolation"]["sigma_bootstrap"],
             "recomputed_method":"ordinary least squares, unweighted",
             "source_discrepancy":"Original log calls the fit weighted. This recomputation is unweighted; do not silently equate them or infer a new sigma from rounded prose.",
             "wall_hours":raw["metadata"]["wall_time_s"]/3600,"time_budget_pass":False,
             "binder_converted":1.5*clean["verdict"]["H1"]["U_star_our_def"],
             "effective_sample_size_measured":False,"execution_n_indep":raw["metadata"]["n_indep"],
             "max_raw_binder_error":max(abs(p["U_L"]-(1-p["avg_m4"]/(3*p["avg_m2"]**2))) for p in raw["results"])}
    assert abs(tc-clean["fss_extrapolation"]["T_c_est"])<1e-12 and facts["max_raw_binder_error"]<1e-12
    state = State.new("analysis",runtime / "ising")
    output = args.output / "ising"
    output.mkdir()
    evidence = state.save_artifact("analysis_report","Ising_original_log",log,
        provenance=imported(source_path=base+"experiment_log__2D_Ising_Tc_ExperimentLog.json",
                            sha256=hashes[base+"experiment_log__2D_Ising_Tc_ExperimentLog.json"],
                            size_bytes=(args.data_root/base/"experiment_log__2D_Ising_Tc_ExperimentLog.json").stat().st_size,
                            by_run_id=state.run_id))
    facts["evidence_artifact_id"] = evidence["id"]
    messages = [LLMMessage(role="system",content="You are analyzing scientific evidence. Preserve failure conditions, uncertainties, versions and measured versus estimated quantities. Current verified state takes precedence over old summaries."),
                LLMMessage(role="user",content="Assess Ising accuracy separately from the two-hour cost budget using the current recomputation and imported evidence.")]
    rounds = []
    for turn in [21,42,63]:
        snapshot = await scientific_state(state,"Assess conditional Ising claims",facts,"Independent raw-data and crossing-fit recomputation; preserve source discrepancies.")
        messages = [m for m in messages if m.context_slot!="research_state"]+([] if args.owner_context else [snapshot])
        append_persisted(state,messages,json.dumps(raw)+json.dumps(clean)+log+json.dumps(facts),f"ising-evidence-{turn}")
        state,messages,outcome = await compact_round(state,messages,llm,turn,output)
        rounds.append(outcome)
        print("ising",turn,outcome["strategy"],flush=True)
    answer,attempts = await structured_answer(llm,messages,
        "Return JSON: tc, normalized_deviation, wall_hours, time_budget_pass, binder_converted, effective_sample_size_measured, execution_n_indep, evidence_artifact_id, results_paragraph (at most 150 English words). Copy current pinned numerical values, retaining at least eight decimal places for normalized_deviation; do not recompute from rounded source prose. Copy evidence_artifact_id exactly from the current binding. State uncertainty and unresolved source discrepancies. No new scientific verdict or experiment. If evidence tools are available, first verify the primary imported record using read_artifact.",output,state=state)
    results["ising"] = {"rounds":rounds,"answer":answer,"answer_attempts":attempts,"facts":facts,"checks":{
        "crossing_fit":abs(answer.get("tc",0)-facts["tc"])<1e-6,
        "exact_reference_deviation":abs(answer.get("normalized_deviation",0)-facts["normalized_deviation"])<1e-8,
        "cost_failure":answer.get("time_budget_pass") is False and abs(answer.get("wall_hours",0)-facts["wall_hours"])<.02,
        "binder_convention":abs(answer.get("binder_converted",0)-facts["binder_converted"])<1e-4,
        "no_measured_neff":answer.get("effective_sample_size_measured") is False,
        "execution_version":answer.get("execution_n_indep")==5,
        "existing_evidence_record":answer.get("evidence_artifact_id")==evidence["id"] and state.read_artifact(evidence["id"]) is not None}}
    dump(args.output / "source-hashes.json",hashes)
    dump(args.output / "results.json",results)


async def main(args):
    for key in list(os.environ):
        if args.direct and key.lower().endswith("_proxy"):
            os.environ.pop(key)
    args.output.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="context-science-followup-"))
    llm = RecordedClient(api_key=os.environ["DEEPSEEK_API_KEY"], model=args.model,
                         base_url=args.base_url, timeout=180, max_retries=0)
    llm.evidence_tools = args.evidence_tools
    results, hashes = {}, {}
    dump(args.output / "run.json", {"model":llm.model,"runtime":str(runtime),
         "mode":"controlled local job and archived-data replay; not production E2E", "evidence_tools":args.evidence_tools,
         "owner_context":args.owner_context,
         "implementation_hashes":{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in
             ["core/context_lifecycle.py","core/context_view.py","core/summarizer.py","core/llm.py","core/jobs.py","core/tool_call_recovery.py","core/research_state_reader.py","scripts/validate_context_science_followup.py"]}})

    if args.only_ising:
        await run_ising(args,llm,runtime,hashes,results)
        print(json.dumps(results["ising"]["checks"]),flush=True)
        if not all(results["ising"]["checks"].values()): raise SystemExit(1)
        return

    if args.only_rsi:
        await run_rsi(args,llm,runtime,hashes,results)
        print(json.dumps(results["rsi"]["checks"]),flush=True)
        if not all(results["rsi"]["checks"].values()): raise SystemExit(1)
        return

    state = State.new("analysis", runtime / "job")
    state.project_root = runtime / "job-project"
    state.project_root.mkdir()
    work = state.project_root
    worker = work / "integrate.py"
    worker.write_text('''import json, math, time
from pathlib import Path
with Path("launches.txt").open("a") as f: f.write("launch\\n")
for level in range(8,15):
 n=2**level
 value=sum(math.exp(-((i+.5)/n)**2) for i in range(n))/n
 Path("progress.json").write_text(json.dumps({"n":n,"integral":value}))
Path("samples.json").write_text(json.dumps([math.exp(-((i+.5)/2048)**2) for i in range(2048)]))
deadline=time.monotonic()+600
while not Path("release").exists():
 if time.monotonic()>deadline:raise TimeoutError("pilot deadline")
 time.sleep(.1)
Path("result.json").write_text(Path("progress.json").read_text())
''')
    proc = subprocess.Popen([sys.executable,str(worker)], cwd=work,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**pc.group_spawn_kwargs())
    group = pc.Group.of(proc.pid)
    output = args.output / "job"
    output.mkdir()
    try:
        job = jobs.declare(state,purpose="Quadrature convergence context followup",pid=proc.pid,
                           resources="1 CPU",progress_probe=str(work / "progress.json"))
        for _ in range(100):
            if (work / "samples.json").exists(): break
            await asyncio.sleep(.05)
        assert (work / "samples.json").exists()
        messages = [LLMMessage(role="system",content="You are tracking an existing numerical job. Continue from its observed identity; do not relaunch it."),
                    LLMMessage(role="user",content="Inspect the registered job and report completion only after observing its receipt.")]
        rounds = []
        for turn in [21,42,63]:
            snapshot = framework_notice(jobs.render_for_orchestrator(state))
            snapshot.context_slot = "jobs"
            messages = [m for m in messages if m.context_slot != "jobs"] + [snapshot]
            append_persisted(state,messages,(work / "samples.json").read_text()+"\nJob observation: "+snapshot.content,f"quadrature-{turn}")
            state,messages,outcome = await compact_round(state,messages,llm,turn,output)
            current = jobs.load(state,only_open=True)
            assert len(current)==1 and current[0].job_id==job.job_id and current[0].pid==proc.pid
            assert pc.alive(proc.pid) and (work / "launches.txt").read_text().splitlines()==["launch"]
            rounds.append(outcome)
            print("job",turn,outcome["strategy"],flush=True)
        answer,attempts = await structured_answer(llm,messages,
            'The registered job is STILL RUNNING. Return JSON with job_id (string), pid (integer), next_action ("inspect_existing"), may_resubmit (boolean), measured_final_result (null). Use the current jobs snapshot. Do not execute anything.', output,state=state)
        (work / "release").touch()
        await asyncio.to_thread(proc.wait,timeout=15)
        measured = read(work / "result.json")
        error = abs(measured["integral"]-math.sqrt(math.pi)/2*math.erf(1))
        jobs.update(state,job.job_id,status="completed",last_progress=json.dumps(measured))
        results["job"] = {"rounds":rounds,"answer":answer,"answer_attempts":attempts,"measured":measured,
            "absolute_error":error,"checks":{
                "exact_handle":answer.get("job_id")==job.job_id and answer.get("pid")==proc.pid,
                "safe_next_action":answer.get("next_action")=="inspect_existing" and answer.get("may_resubmit") is False,
                "no_early_result":answer.get("measured_final_result","missing") is None,
                "one_launch":(work / "launches.txt").read_text().splitlines()==["launch"],
                "convergence":proc.returncode==0 and error<1e-8}}
        dump(args.output / "results.json",results)
    finally:
        if proc.poll() is None:
            group.kill()
            proc.wait(timeout=10)
        group.close()

    if args.only_job:
        print(json.dumps(results["job"]["checks"]),flush=True)
        if not all(results["job"]["checks"].values()): raise SystemExit(1)
        return
    await run_rsi(args,llm,runtime,hashes,results)
    await run_ising(args,llm,runtime,hashes,results)
    print(json.dumps({k:v["checks"] for k,v in results.items()}),flush=True)
    if not all(all(case["checks"].values()) for case in results.values()):raise SystemExit(1)


async def run_rsi(args,llm,runtime,hashes,results):
    base = "reports/rsi-continuation-20261113/"
    audit = json.loads(source(args.data_root,base+"q2_budget_audit.json",hashes))
    star = json.loads(source(args.data_root,base+"e_star.json",hashes))
    correction = source(args.data_root,base+"RSI_v2_verified_evidence_corrections.json",hashes)
    spend = {arm:sum(audit["spend_by_arm_seed"][arm][str(i)] for i in range(2,6)) for arm in audit["search_arms"]}
    facts = {"search_ratio":max(spend.values())/min(spend.values()),"accounting_stage":"search","seeds":[2,3,4,5],
             "all_stage_audit_verified":False,"all_stage_scope":"No independent all-stage audit has been supplied or established.",
             "max_overrun_tokens":max(audit["over_budget_cells"].values()),
             "max_overrun_percent":100*max(audit["over_budget_cells"].values())/audit["budget_per_arm_per_seed"],
             "random_g_star":[star["arms"][f"A_random__seed{i}"]["g_star"] for i in range(1,6)],
             "selfmod_episodes":[star["arms"][f"A_selfmod__seed{i}"]["episodes_at_g_star"] for i in range(1,6)]}
    assert abs(facts["search_ratio"]-audit["ratio_search_v14_seeds"])<1e-12
    state = State.new("analysis",runtime / "rsi")
    output = args.output / "rsi"
    output.mkdir()
    messages = [LLMMessage(role="system",content="Revise a scientific manuscript. Current verified data supersedes older drafts. Preserve accounting scope and version references."),
                LLMMessage(role="user",content="Correct RSI results without running new experiments.")]
    append_persisted(state,messages,"Unverified old draft: search ratio 1.97; max overrun 86257 (1.38%); all cells exactly 480 episodes; random E* at generation 1.","obsolete-draft")
    rounds = []
    for turn in [21,42,63]:
        snapshot = await scientific_state(state,"Correct RSI results",facts,"Pinned evidence overrides the obsolete draft.")
        messages = [m for m in messages if m.context_slot != "research_state"]+([] if args.owner_context else [snapshot])
        text = source(args.data_root,base+"q1_trajectory_dev.json",hashes)+json.dumps(audit)+json.dumps(star)+correction+json.dumps(facts)
        # Controlled repeated evidence pressure, not a claim of new science.
        append_persisted(state,messages,text*2,f"rsi-verified-{turn}")
        state,messages,outcome = await compact_round(state,messages,llm,turn,output)
        rounds.append(outcome)
        print("rsi",turn,outcome["strategy"],flush=True)
    answer,attempts = await structured_answer(llm,messages,
        "Return JSON with search_ratio, accounting_stage, seeds, max_overrun_tokens, max_overrun_percent, random_g_star, selfmod_episodes, all_stage_audit_verified, and results_paragraph (English, at most 150 words). Use the corrected pinned current evidence, without recalculation from rounded text. Do not quote obsolete values or assert unprovided analyses. Explicitly distinguish search accounting from all-stage accounting.",output,state=state)
    paragraph = answer.get("results_paragraph","")
    results["rsi"] = {"rounds":rounds,"answer":answer,"answer_attempts":attempts,"facts":facts,"checks":{
        "ratio":abs(answer.get("search_ratio",0)-facts["search_ratio"])<1e-5,
        "scope":answer.get("accounting_stage")=="search" and answer.get("seeds")==[2,3,4,5],
        "overrun":answer.get("max_overrun_tokens")==76257 and abs(answer.get("max_overrun_percent",0)-facts["max_overrun_percent"])<.001,
        "correct_checkpoints":answer.get("random_g_star")==facts["random_g_star"],
        "episode_overshoot":answer.get("selfmod_episodes")==facts["selfmod_episodes"],
        "no_asserted_all_stage_audit":answer.get("all_stage_audit_verified") is False,
        "no_obsolete_values":not any(x in paragraph for x in ["1.97","86257","86,257","1.38%"]),
        "prose_separates_scope":"search" in paragraph.lower() and ("all-stage" in paragraph.lower() or "all stage" in paragraph.lower())}}
    dump(args.output / "source-hashes.json",hashes)
    dump(args.output / "results.json",results)



if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--model",default="deepseek-flash")
    p.add_argument("--base-url",default="https://api.deepseek.com")
    p.add_argument("--direct",action="store_true")
    p.add_argument("--only-ising",action="store_true",help="Independently repeat Ising with real persisted evidence")
    p.add_argument("--only-job",action="store_true")
    p.add_argument("--only-rsi",action="store_true")
    p.add_argument("--evidence-tools",action="store_true",help="Allow the existing read-only evidence tools during continuation")
    p.add_argument("--owner-context",action="store_true",help="Read current science through production compaction; do not inject a pilot snapshot")
    asyncio.run(main(p.parse_args()))

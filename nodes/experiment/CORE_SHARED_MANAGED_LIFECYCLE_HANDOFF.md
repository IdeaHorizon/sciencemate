# Core/shared handoff — C39 managed-lifecycle gaps

> Owner: Core/shared.  This document is written by the Experiment builder as a
> handoff only; it does not authorize a node-side workaround or a Core/shared
> patch.  It records the issues that cannot be made correct solely under
> `nodes/experiment/`.
>
> Evidence base: `fdf07b22` / current branch `b2fbffec`, the independent
> review dated 2026-08-28/29, and this builder's C39 runs.  E3-R5 completed and
> removed its exact Docker container; E4-R1 completed the business workflow but
> was incorrectly blocked by Experiment issue E-2 (handled separately in the
> Experiment scope).

## Boundary and desired invariants

Experiment owns route facts, execution evidence, external-job identities and
closure artifacts.  Core/shared own RunAttempt lifecycle, Docker authority,
global admission, tool dispatch, child-run concurrency and top-level CLI
behavior.

The cross-layer invariant is:

1. A cancellation or terminal boundary either drains the exact owned sandbox
   payload and releases its reservation, or leaves a durable, explicit unknown
   state; it must never silently leak or stop an unrelated job.
2. A resource admission rejection is structured, truthful and retryable when
   appropriate; it must not create an irrecoverable route failure before a
   payload or submission identity exists.
3. Docker probe failure is distinct from a container that is truly absent.
4. Every tool dispatch and run-resume path has one authoritative authorization,
   audit and concurrency boundary.

## CS-1 — terminal exit/cancellation can leave a RunAttempt allocated

**Severity:** P1.  **Owner:** Core executor + shared subprocess + sandbox.

### Evidence and call chain

The E3-R2 run was interrupted after a route preflight failure.  The managed
RunAttempt remained running until a manual exact
`evict_attempt(local:local:merge-e3-toolchain-r2:local:1787937109-362c19)`
drain.

E4-R3 supplies the complementary normal-completion reproduction: run
`1787976595-537061` returned CLI rc=0 and `status=completed`; its exact local
job container was removed and its workflow lifecycle was finalized.  Yet
`list_attempt_instances()` still returned the run-owned, running Attempt
`local:local:merge-e4-scientific-r3:local:1787976595-537061` with a 4 GiB
reservation.  A manual exact `evict_attempt(...)` then removed it and restored
the Attempt list to empty.  Therefore this is not only an external-SIGINT
problem: normal terminal completion also fails to reclaim an idle Attempt.

H2 supplies two independent `chat.py` reproductions on 2026-08-29.  Both
`chat.py /exit` calls returned rc=0, but the exact top-level Attempts remained
running with 4 GiB reservations until an official exact eviction:

```text
local:local:merge-h2-lammps-r1:local:orchestrator__merge-h2-lammps-r1
  container=hf-harness-attempt-0c1070c3ee0d2d5d-g1
local:local:merge-h2-lammps-r2:local:orchestrator__merge-h2-lammps-r2
  container=hf-harness-attempt-270e538011817fe8-g1
```

For each, `evict_attempt(exact_attempt_id)` returned `True`; subsequent
inspection reported `exists=false` and `list_attempt_instances()==[]`.  The
R2 chat had no Experiment child/job/artifact at all, so normal REPL/session
teardown alone is sufficient to reproduce the leak.  Evidence is retained at
`/tmp/hf-evidence-managed-lifecycle-merge/h2-lammps-r1-LFFioe` and
`/tmp/hf-evidence-managed-lifecycle-merge/h2-lammps-r2-YsSdrl`.


H2-R4 is a third, corrected H2 reproduction. Its Experiment child
1788006176-46ca24 executed the Docker-only command-v preflight, persisted an
operation receipt with outcome=blocked, and the top-level chat exited normally.
Immediately afterward, list_attempt_instances filtered to this run still
reported exists=true and running=true for
local:local:merge-h2-lammps-r4-20260829:local:1788006176-46ca24. Its container
name/id fields were already absent, so the framework must treat this as an
idle exact Attempt record rather than infer that it was cleaned. An official
evict_attempt on that exact id returned True; the post-check reported matching
attempts=[] and matching reservations={}. The evidence root is
/tmp/hf-evidence-managed-lifecycle-merge/h2-lammps-r4-j2u9yX. This confirms
that a successful safe_run_bash plus ordinary operation closure does not by
itself guarantee terminal Attempt reclamation.



H8-R1 and H9-R1 reproduce the same defect on two further successful
Docker-only capability preflights. Each child completed a structured blocked
operation receipt after one command-v query, each top-level chat exited
normally, and each exact child Attempt was still reported exists=true,
running=true with no container id/name. Exact evictions returned True for
local:local:merge-h8-openfoam-r1-20260829:local:1788008668-3e726e and
local:local:merge-h9-meep-r1-20260829:local:1788008975-bed15a; post-checks
reported matching attempts=[] and reservations={}. These are independent of
scientific payload, scheduler submission and cancellation: ordinary blocked
terminal boundaries are enough to leak an idle Attempt.


The relevant ownership chain is:

```text
run_node / process SIGINT
  -> core.executor.execute_node()
     -> shared.lib.cancellable_subprocess.spawn_and_wait()
        -> core.sandbox RunAttempt / Docker command
```

Normal `finalize_run()` cleanup is not a sufficient guarantee when the
process receives cancellation before it reaches that boundary.

### Required Core design

- On cancellation, send a command-scoped terminate to the exact sandbox
  command, then drain/wait for the supervisor's terminal acknowledgement.
- At every terminal executor boundary (completed, failed, blocked, cancelled, paused
  and process interruption), evict only the state-bound, idle exact Attempt.
  Do not derive a new manifest merely to clean up and do not stop a busy/shared
  Attempt.
- Preserve the original cancellation after bounded cleanup; a second SIGINT
  must not orphan the payload or convert it into a successful completion.
- Do not use Experiment-local cleanup as a substitute for this shared
  lifecycle guarantee.

A local, uncommitted exploratory change previously touched
`core/executor.py` and `shared/lib/cancellable_subprocess.py`.  It is not
an Experiment-side solution and must be independently reviewed or removed
from this builder worktree before submitting an Experiment-only change.

### Acceptance tests

1. Start a real Docker-backed payload that sleeps; send SIGINT to the parent
   process; assert the exact payload/container terminates, Attempt list and
   reservations are empty, and no unrelated container is touched.
2. Repeat for a second cancellation while drain is in flight.
3. Verify normal completed/error/blocked/paused boundaries release idle Attempts but
   preserve an explicitly active/shared one.
4. Check no asyncio transport/task leak remains after the cancellation tests.

## CS-2 — fixed 4 GiB standard Attempt is a global capacity-policy decision

**Severity:** P2; P1 only where the product promises concurrent/small-host
operation.  **Owner:** Core sandbox admission.

### Evidence

`core/sandbox.py::SandboxLimits.memory_bytes` defines a 4 GiB default.
The global admission pool uses a 0.70 host-memory fraction.  On a roughly
15 GiB host, only two standard requests fit; retained Attempts made this worse
before CS-1 cleanup.

This is not a request to silently reduce the default.  The Experiment contract
deliberately gives safe Python a 4 GiB limit plus 25% transient headroom
(about 5 GiB), and fail-closed rejection on an undersized host is safer than
running outside that contract.

### Required Core decision

Choose and document one or more of:

- explicit capacity profiles with declared lower limits;
- a queue/wait policy for standard Attempts;
- a minimum-host requirement and a structured, retryable admission response;
- an explicit per-run resource plan that maps to sandbox limits without
  weakening isolation.

The error must report requested capacity, safe pool, deficit and whether
waiting/retrying can change the result.  It must not degrade silently to a
smaller container limit.

### Acceptance tests

- 4 GiB, 15 GiB and larger-host matrices for one and concurrent standard
  Attempts;
- no reservation leak after terminal cleanup;
- a structured admission blocker for an impossible request;
- no oversubscription and no infinite retry loop.

## CS-3 — Docker inspect has an unsafe absent/error ambiguity

**Severity:** P1.  **Owner:** Core sandbox.

### Evidence

`core/sandbox.py::inspect_container()` returns `{"exists": false}` when
`docker inspect` exits nonzero.  A daemon/permission/transient failure is
therefore indistinguishable from a truly missing container.  Consumers can
treat a live job as terminal or release a reservation after a probe failure.

### Required Core design

Make inspection tri-state:

```json
{"exists": true,  "...": "..."}
{"exists": false, "reason": "not_found"}
{"exists": null,  "error": "daemon_unavailable|permission_denied|timeout"}
```

All status, cancel, stop and eviction consumers must fail closed on the third
state.  Only an authoritative not-found result may be treated as absent.

### Acceptance tests

Mock/induce daemon unavailable, permission denied, timeout and true not-found;
verify status becomes unknown/error, cancellation is not reported successful,
and a reservation is not released on probe failure.

## CS-4 — dynamic admission failure can still occur after route binding

**Severity:** P1.  **Owner:** Core sandbox contract with Experiment integration.

### Evidence

The Experiment patch now preflights deterministic local path/mount failures
before route binding.  However dynamic Docker preparation/admission happens
later, after a route attempt can be bound.  A capacity, image or launch
failure in that interval may write an irreversible `failed` route outcome
despite no intent, scheduler identity, payload or container existing.

### Required Core/Experiment contract

Provide either:

1. a side-effect-free, atomic-enough Core admission preflight/reservation that
   Experiment can call before binding; or
2. a structured launch result that marks post-bind infrastructure rejection as
   recoverable/blocked, never as an execution failure, with no submission
   identity implied.

The invariant is that a no-payload rejection cannot poison a route step; a
TOCTOU failure must preserve a safe, diagnosable retry/amend path.

### Acceptance tests

Inject path, capacity, image and Docker-unavailable failures both before and
after preflight.  Assert no intent/job/container for pre-bind rejection, and
a recoverable route state for post-bind infrastructure failure.  A later
successful retry must not require a fictitious execution amendment.

## CS-5 — legacy systemd resource health is not the Docker execution truth

**Severity:** P2.  **Owner:** Core/Experiment architecture.

### Evidence

The `build_resource_guard` systemd/cgroup supervisor writes resource health
for its own legacy path, while local managed jobs run through Docker
`prepare_launch`.  The latter does not produce the same status evidence, so
the Experiment joint job/resource decision can fall back to
`resource_health=not_applicable`.

### Required decision

Either connect Docker/cgroup telemetry to the shared health contract, or
retire the dead systemd-health promise and its misleading decision paths.
Do not claim a `stalled + pressure -> managed_cancel_recommended` guarantee
until the live Docker path produces authoritative pressure evidence.

### Acceptance tests

A real Docker job with controlled pressure/stall evidence must produce the
documented joint decision; alternatively, after retirement no consumer may
advertise that unavailable decision.

## CS-6 — tool dispatch, audit and run-resume concurrency lack one authority

**Severity:** P0/P1.  **Owner:** Core.

### Evidence

Normal authorization/audit is split between `core/agent_loop.py` and
`core/tool_registry.py`; the chat standby path can invoke
`execute_tool(...)` without the same revalidation/audit boundary.  Resume
selection also lacks an in-flight ownership lock, and canonical route writes
have no compare-and-swap generation.

### Required Core design

Move authorization and audit to the single dispatch primitive, or make all
callers use a compulsory wrapper.  Bind run ownership with a durable
in-flight lease/lock and give canonical route updates a version/CAS contract.
A failed lease/CAS must return a structured conflict, not silently run a
second loop.

### Acceptance tests

- standby dispatch rejects a tool outside its declared readonly/runtime set
  and records the same audit event as normal dispatch;
- concurrent resumes of one run permit only one active executor;
- competing route updates cannot oscillate or overwrite a newer generation.

## CS-7 — safe Python capacity failure needs a shared structured response

**Severity:** P2.  **Owner:** Experiment + Core admission contract.

Experiment's approximately 5 GiB safe-Python request is intentional and must
not be silently shrunk.  What is missing is a structured Core admission
result that lets Experiment record a truthful resource blocker with
requested/safe/deficit/retryability fields.  This can be delivered with
CS-2; it is not a reason to weaken the Python safety envelope.

## CS-8 — missing reasoning model leaves the top-level REPL waiting

**Severity:** P3 (P2 in continuous mode).  **Owner:** Core/chat.

`ModelRoleUnavailable` is caught by the generic top-level turn-error path;
the CLI prints guidance and then waits for stdin.  A deterministic missing
configuration should either be preflighted before entering the REPL or
classified as a terminal configuration error with a nonzero exit.  Preserve
interactive recovery only if there is an explicit command that can change the
configuration in-process.

## CS-9 — generic structured human choices are displayed as numbered but not mechanically settled

**Severity:** P1.  **Owner:** Core pause/resume + shared human-input contract.

### Evidence and root cause

H2-R1 showed a three-option `request_human_input` prompt in `chat.py`; the
CLI rendered the choices as `[1]`, `[2]`, `[3]`.  The tester typed `2`, but
the resumed agent immediately stated that choice 3 (“Python 为主 + LAMMPS
对照”) had been selected.  The run was stopped before any substitute payload
or job started, so the observation is uncontaminated by scientific execution.

This is not an off-by-one parser: the parser already exists but is skipped on
the generic path.  `shared/tools/builtin.py::_request_human_input` creates an
Offer for `structured_question` with stable choice IDs; `chat.py` renders the
same `PauseEvent` one-indexed and forwards raw `"2"`; `core/pause_driver.py`
only settles `decision_package`, so `core/agent_loop.py::resume_loop` writes
only `{response: "2"}` back to the LLM.  The LLM then makes a semantic choice
from an ambiguous naked number.  `core/decision_offer.py::resolve_answer()`
would have mapped `"2"` to the second choice from the same Offer, but it is
never called for `structured_question`.  The background `/answer` path in
`shared/tools/run_node.py` also sends raw replies to `resume_loop`, so a
chat-only patch would leave a second bypass.

### Required Core/shared design

- Treat every pause carrying an Offer, including `structured_question`, as a
  framework-settled choice: reconstruct the current Offer and call
  `resolve_answer()` while the framework still has both the Offer snapshot and
  raw reply.
- On success, inject authoritative `choice_id` and label into the resume
  envelope; retain raw text only for audit.  On stale/invalid replies, do not
  resume the agent; re-present the current Offer with a structured rejection.
- Make chat, background `/answer` and auto-approve use this one common
  settlement path.  Keep CLI number rendering and the resolver derived from
  the same ordered Offer; frontends should prefer `{offer_id, choice_id}`.

### Acceptance tests

1. A CLI/queue `structured_question` with choices A/B/C renders `[1]/[2]/[3]`;
   input `2` resumes exactly B, with authoritative identity visible to the
   LLM.
2. An invalid or stale `offer_id`/`choice_id` never resumes the run and is
   re-presented with legal choices.
3. Auto-approve and background `/answer` use the same settled authoritative
   choice, rather than returning a naked number for the LLM to reinterpret.

## CS-10 — H1 multi-source literature retrieval repeats known failing sources and delays cancellation

**Severity:** P2 (P1 if it causes user-requested cancellation to retain a live job).
**Owner:** Literature retrieval + Core child-cancellation/status projection.

### Evidence

H1-R1 (`merge-h1-airsea-r1`) correctly entered `hypothesis→literature` and did
not invent citations.  However, five expanded queries repeatedly waited on
OpenAlex/arXiv/S2 failures and then serial Crossref fallbacks (about 27–148 s
per query).  There was no source-level circuit breaker, progress checkpoint or
partial evidence delivery; no artifact, claim or experiment was produced.
After the tester sent `/stop`, the UI still had to wait for the active tool
call before closure (about 120–150 s), and the child summaries were
`cancelled` while the top-level summary recorded `stop_reason=finished`.
Evidence: `/tmp/hf-merge-h1-airsea.Cy9mHM/.harness-framework/projects/merge-h1-airsea-r1`.

### Required cross-owner design

- Record source health per run/query batch.  Once a source reaches its bounded
  failure budget, open a circuit for the remaining batch rather than retrying
  the same failed source for every expanded query.
- Set a batch deadline and deliver an auditable partial-result/source-failure
  artifact before returning; never manufacture claims from absent results.
- Propagate `/stop` through the active retrieval request with bounded drain,
  and preserve `cancelled` in the top-level status rather than rewriting it as
  `finished`.

### Acceptance tests

1. Mock repeated OpenAlex/arXiv failures across five queries; verify each
   source is attempted only within its configured budget, Crossref partial
   evidence is retained, and no unsupported claim is emitted.
2. Stop during an active delayed source request; assert bounded cancellation,
   no live child/Attempt, and a top-level `cancelled` status.
3. A mixed-success batch yields an explicit incomplete-evidence artifact and
   useful progress update rather than serial silent waiting.

## CS-11 — Slurm/PBS real submission is a required product capability, not a permanent dry-run mode

**Severity:** P0 capability gap.  **Owner:** coordinated Experiment + Core/
deployment + scheduler platform.

### Requirement and current evidence

The product requirement is explicit: an agent must be able to submit, monitor,
cancel and reconcile real Slurm/PBS jobs.  This is not satisfied by a tool that
only renders an `sbatch`/`qsub` script.

`nodes/experiment/tools/resource_manager.py::_submit_job` currently rejects
every non-local `dry_run=false` request before materialization
(`remote_sandbox_contract_unavailable`, around lines 3643--3651).  The lower
adapter is nevertheless useful implementation substrate: `_script_for`,
`_submit_sync`, `_submit_command`, identity parsing, status/cancel, intent
recovery and external-job handoff already model Slurm/PBS.  Removing only the
early rejection would submit the raw model-selected shell body on scheduler
nodes, with no remote image identity, mount, egress, process-supervision or
node-side ABI contract.  That would add a nominal feature by deleting the
security boundary and is not an acceptable implementation.

There is also a second future defect behind that gate: the generic real-job
approval later calls local-Docker `trusted_image_id()` and records
`network=none` without a scheduler-specific identity.  It is unreachable today,
but must be replaced by the remote profile receipt before Slurm/PBS is enabled.
Likewise, the current ABI preflight is only on the local `safe_run_bash` path;
it is not a `submit_job` target-node check.

### Required delivery shape

1. **Platform-owned scheduler-native executor.**  Each enabled scheduler
   profile supplies an attested runner (for example a site-approved
   Apptainer/Enroot wrapper; Docker is not assumed on compute nodes) that runs
   a deployment-pinned immutable image.  It enforces read-only base/root,
   explicit input/output binds, default-deny egress, scheduler cgroup/time/
   memory allocation, bounded temporary/ephemeral storage, PID-1/child
   reaping, and teardown.  The profile must state its exact runtime, version,
   image digest, supported queues/nodes and mount mapping.
   It also binds endpoint/service identity and immutable cluster identity; a
   submit-host CLI or an SSH `sinfo` probe alone is not that capability.
2. **Experiment-owned remote execution envelope.**  Before confirmation,
   freeze a versioned `remote_execution_contract` containing scheduler/profile
   identity, immutable image digest, wrapper entrypoint, input/output mapping,
   network mode, requested resources, toolchain/source locks, target ABI
   fingerprint and payload digest.  Render Slurm/PBS scripts that invoke only
   that wrapper; never embed a bare payload as the scheduler job body.
3. **One exact approval and durable evidence.**  The high-risk confirmation
   includes the complete frozen envelope.  Persist submission intent before
   `sbatch`/`qsub`, parse an authoritative job id, then retain remote runtime
   receipt, scheduler/accounting snapshots, image/toolchain/ABI evidence and
   terminal exit evidence.  Unknown acceptance remains `do_not_resubmit` and
   enters the existing reconciliation path.
4. **Remote lifecycle parity.**  `job_status`, `cancel_job`, recovery and
   output-conflict protection operate on the remote job id plus profile/
   cluster identity.  Target-node ABI preflight runs inside the remote runner;
   the present local-Docker `ldd` probe is not evidence for a Slurm/PBS node.

### Ownership split

- **Experiment builder:** contract schema and validation; wrapper-only script
  rendering; route/approval/persistence wiring; Slurm/PBS adapter tests and
  truthful structured blockers when no approved profile exists.
- **Core/deployment:** trusted-profile registry and signature/digest
  verification; canonical image/toolchain identity; shared authorization and
  lifecycle/recovery primitives.
- **Scheduler platform:** installs/configures the runner on compute nodes,
  exposes only approved scheduler profiles, grants scoped submission identity,
  enforces filesystem/egress/quota policy, and provides a real integration
  target.  This cannot be emulated by an Experiment-only Python change.

### Acceptance tests

1. Unit/fake-client tests for Slurm and PBS: absent, stale, unsigned or
   digest-mismatched profile is blocked before script/intent; a valid profile
   renders wrapper-only jobs and preserves the frozen envelope in approval and
   receipt.
2. Submission/recovery tests: `sbatch --parsable` and `qsub` identity success,
   timeout/unknown acceptance, explicit rejection, status, cancel and output
   conflict all retain the exact scheduler/profile identity and never permit
   duplicate submission.
3. One real Slurm and one real PBS platform integration run: prove image
   digest, no unapproved host write or egress, resource limits, target-node ABI
   evidence, normal completion, cancellation/reaping and artifact retrieval.
4. A raw native command or caller-selected image bypass is rejected even when
   Slurm/PBS itself is available; a valid remote submission does not consult
   local-Docker `trusted_image_id()`.


## CS-12 — local RunAttempt manifest is incorrectly inherited across State boundaries

**Severity:** P1 correctness and availability defect. **Owner:** Core executor and sandbox.

### Evidence and exact failure mode

On 2026-08-29, H2-LAMMPS-R3 first let the parent orchestrator perform three
raw run_bash inspections. That path legitimately created a local Docker
SandboxManifest for the parent State and froze only the parent run roots.
core/executor.py then copied sandbox_manifest, sandbox_manifest_hash and
platform_attempt_id into a newly dispatched Experiment child without checking
whether the manifest was platform-issued or belonged to the child run.

The first child safe_run_bash correctly computed its own run/workspace roots.
safe_bash.py then called sandbox.manifest_for, which correctly rejected the
missing child root before any payload was spawned:

    writable root was not frozen into this RunAttempt:
    .../runs/1788004378-a79f71

The rejection is the right fail-closed behavior. The defect is the Core
cross-State inheritance of a local attempt capability. It affects a normal
workflow: an orchestrator may perform a safe parent inspection and then
delegate execution to an Experiment child. The child must not lose the ability
to use its own approved roots merely because the parent has already performed
a model tool call.

The later local submit_job in that invalid H2 run is not counter-evidence:
it uses detached prepare_launch rather than safe_run_bash persistent
prepare_attempt_command, created a job despite the H2 preflight constraint,
and the operation was stopped before closure. It must not count as H2 evidence.

### 2026-08-31 East-Asia confirmation

The RCM/WRF child `1788154494-2934c0` reproduced the same defect through
a different parent/child pair. Its immutable attempt reported the orchestrator
run id instead of the child run id and omitted `workspace_root` and `run_root`;
both `safe_run_bash` and `safe_execute_python` were rejected before payload
launch. The receipt explicitly requested
`core_reissue_child_attempt_with_bound_workspace_and_run_root`. This confirms
CS-12 is not LAMMPS-specific and blocks WRF source build even when the host
toolchain and local Docker route are available.

### Required Core design

1. A local or CLI-derived manifest is scoped to exactly one State.run_id and
   must never be copied into a child with a different run id.
2. A newly created child without an authoritative platform_attempt_id starts
   with no inherited local manifest; its first model-tool payload obtains a
   new least-privilege manifest for the child roots.
3. A platform-issued manifest may be inherited only through an explicit
   platform capability path. Core must verify provenance and ensure that the
   signed scope already covers the child exact roots; a node must not expand
   it after dispatch.
4. Keep the existing fail-closed root check. Clearing, regenerating or
   widening a child manifest in Experiment would bypass the frozen-capability
   boundary and is not an acceptable node-local workaround.
5. Diagnostics may state that an inherited local manifest is incompatible
   with the child run, but must not disclose unneeded host paths.

### Acceptance tests

1. Pure Core test: create a parent State with a valid local manifest covering
   only parent.root, dispatch a child, and assert the child does not inherit
   manifest/hash/platform_attempt_id. A first manifest_for call for
   child.root then succeeds and has child.run_id.
2. Platform-capability regression: a parent with an authoritative,
   platform_attempt_id-backed manifest that was issued for the child scope
   retains that capability path; Core must not discard a valid platform
   authority merely because it is not local.
3. Integration regression: parent performs a read-only model tool call, then
   dispatches Experiment; child safe_run_bash can use only its approved roots,
   while an unlisted root remains rejected before spawn.
4. Verify no change permits parent roots, arbitrary host paths, or a stale
   child manifest to be used after delegation.

## CS-13 — cancelling a one-shot network acquisition leaks its exact Docker container

**Severity:** P1 lifecycle/resource leak. **Owner:** shared subprocess + Core sandbox.

### Evidence and exact failure mode

On 2026-08-29, the locally isolated H2 controlled-fetch runs were interrupted
only after measuring a transfer rate that could not finish before their declared
bounded-acquisition timeout (900 s for initial attempts; 3600 s for STATIC-R2). In H2-LAMMPS-WHEEL-R1, the parent received SIGINT
while `fetch_resource` was awaiting a networked one-shot acquisition. The parent
exited with `asyncio.CancelledError`, but exact managed acquisition container
`hf-harness-147203e37fc442e4ad2c` remained running. It was verified by exact
name and then removed manually with an exact Docker stop; no broad prune was
used. Evidence root:
`/tmp/hf-evidence-managed-lifecycle-merge/h2-lammps-wheel-r1-XBRHI6`.

H2-LAMMPS-STATIC-R2 reproduced the same ownership failure under a larger,
explicit one-hour acquisition budget: after about six minutes and only
2,699,264 downloaded bytes, its parent was interrupted because the observed
rate projected roughly two hours for the approximately 58.5 MB official asset.
The parent exited with `CancelledError`, but exact container
`hf-harness-828d976ea6a347efa8c5` remained running until it too was stopped by
exact name. Evidence root:
`/tmp/hf-evidence-managed-lifecycle-merge/h2-lammps-static-r2-6SL8cK`.

The cause is visible in shared `spawn_and_wait`: its ordinary timeout/kill-event
branch calls `sandbox_launch.terminate()`, but task cancellation injected by the
parent jumps directly to the `finally`. That `finally` calls only
`sandbox_launch.cleanup()`, which removes the control directory and releases the
reservation without terminating the still-running one-shot acquisition container.
This is distinct from CS-1's persistent RunAttempt leak: acquisition containers
are intentionally data-empty, networked, one-shot Docker launches.

### Required shared/Core design

1. On `CancelledError` after a `SandboxLaunch` has been prepared/spawned,
   terminate and remove only that exact managed container before releasing its
   reservation/control directory. Preserve the original cancellation afterwards.
2. Make cleanup idempotent and fail closed: if exact identity cannot be inspected
   or disappearance cannot be verified, preserve a durable unknown/cleanup-needed
   state rather than silently releasing capacity.
3. Do not broaden Experiment's fetch adapter or add a node-local Docker cleanup
   workaround; the same shared wrapper owns every one-shot acquisition adapter.

### Acceptance tests

1. Start a controlled `network_access=True` acquisition, cancel its awaiting
   task, and assert the exact acquisition container and reservation disappear.
2. Repeat with cancellation racing normal completion and with a second
   cancellation; no unrelated managed or user container may be touched.
3. Assert the original `CancelledError` still reaches the caller and that
   temporary staging cleanup remains bounded.

### Experiment PR-1 delivered on `fix-experiment-managed-lifecycle`

This branch now contains the Experiment-owned, compatibility-preserving first
slice. It does **not** claim that remote execution is enabled.

- `tools/execution_envelope.py` creates a content-addressed, frozen
  `execution_envelope` artifact. Its returned reference is exact:
  `artifact:<id>@v<version>#sha256=<content-hash>`.
- The envelope is not a state machine. It is attached only to the existing
  `declared_route.steps[].action.evidence_refs`; the existing route content
  hash and `step_execution_contract_hash` therefore invalidate a changed
  reference naturally. Existing route attempts and external-job receipts
  remain the sole execution lifecycle.
- v1 records only an opaque content-addressed target-profile reference, the
  one implemented generic storage binding (`shared_filesystem` mapped to
  existing path roles), frozen environment-lock artifact triples, optional
  frozen resource-plan triples, and—only for `evidence_bearing`—the exact
  prereg version/hash already selected by `load_run_contract`. It does not
  duplicate source/toolchain/prereg content or announce staged-transfer,
  object-store, scratch, Apptainer, Slurm, PBS or target-canary support.
- Route declaration validates any opt-in envelope reference; before real
  materialization the existing route resolver re-resolves its exact frozen
  version and every supporting artifact. A malformed, unfrozen, wrong-step
  or drifted reference returns the existing route blocker path before a
  directory, process or scheduler intent is created. Legacy routes without
  an envelope remain compatible during the migration.
- `resource_manager` now has a deliberately narrow private local-Docker
  **submission-launch adapter**: `prepare -> durable submission intent ->
  launch`, with `abandon` only for proven pre-submit failure. It reuses the
  existing intent/recovery artifacts, `_job_key_for_record` ten-field
  identity, status/cancel/finalize machinery and Docker cleanup. It is not a
  second executor or a registry. Unknown launch outcomes and malformed
  runtime IDs still retain their prepared identity and return
  `do_not_resubmit`.
- `verify_external_job_execution` is a small generic Experiment transition for
  a terminal, successful exact managed job: it reuses the existing authoritative
  health verification and route execution receipt projection, then unlocks a
  dependent declared `submit_job` step. It writes neither a closure artifact nor a
  second lifecycle; closure/finalize remain whole-route terminal operations.

The remaining work is intentionally owned outside this Experiment-only
change: Core/deployment must provide a trusted target-profile registry and a
scheduler-native runner; the scheduler platform must prove target-side
identity, cgroup/proctrack/reaping, approved storage visibility and ABI/canary
facts. Only then should a `slurm` launch adapter be registered, first pass the
same fake conformance contract, and immediately be checked against a real
Slurm target. The current `scheduler != local` real-submit rejection remains
the correct safety boundary until that PR exists.

### Regression evidence for the delivered slice

- `nodes/experiment/tests/test_execution_envelope.py`: content-addressing,
  generic-save rejection, evidence-bearing prereg+lock binding, route-step
  binding, exact reference resolution and unsupported-storage/lifecycle-field
  rejection.
- `nodes/experiment/tests/test_submission_launch_adapter.py`: protocol shape,
  prepare -> durable intent -> launch order, intent-failure abandon, and
  unknown immutable runtime identity retaining reconciliation state.
- Existing `test_local_docker_runtime_not_found_is_sealed_before_submit`
  continues to cover the actual Core Docker primitive through the new seam.

## Priority and submission split

| Priority | Item | Required submit scope |
|---|---|---|
| P0/P1 | CS-6 dispatch/audit/concurrency | Core-only |
| P1 | CS-1 cancellation/reclaim | Core/shared-only |
| P1 | CS-3 inspect tri-state | Core-only |
| P1 | CS-4 dynamic admission/route lifecycle | coordinated Core + Experiment |
| P2 | CS-2 capacity policy | Core-only design/contract |
| P2 | CS-5 Docker resource health | coordinated architecture |
| P2 | CS-7 structured safe-Python capacity blocker | coordinated follow-up |
| P3 | CS-8 missing-model CLI exit | Core/chat-only |
| P1 | CS-9 generic structured-choice settlement | Core + shared-only |
| P2/P1 | CS-10 H1 retrieval circuit/cancellation projection | Literature + Core-only |
| P0 | CS-11 real Slurm/PBS scheduler-native execution | coordinated Experiment + Core/deployment + scheduler platform |
| P1 | CS-12 cross-State local manifest inheritance | Core executor + sandbox only |
| P1 | CS-13 one-shot acquisition cancellation/reclaim | shared subprocess + Core sandbox |
| P2 | CS-14 shared prompt-budget invariant | Core context engine + shared prompt policy |
| P2 | CS-15 durable child-input digest | Core executor + shared run_node |

## Experiment-owned work deliberately excluded

- E-2 dry-run receipt validation is in `nodes/experiment/hooks.py` and will
  be fixed with node-local regression tests.  It is the direct current reason
  E4-R1 ended `blocked`.
- E-1 read-only conda/spack probe portability and E-6 route-attempt liveness
  each need their own Experiment or coordinated design review; neither should
  be hidden by a Core workaround.
- E-5 generic multi-job route support is now delivered on the Experiment side:
  `verify_external_job_execution` reuses exact managed-job verification and
  the existing route receipt to unlock a dependent local job without creating
  a closure or second lifecycle. It does not resolve CS-13's shared
  cancellation/reclaim obligation.


## CS-14 — Experiment first-prompt token-budget test is already broken by shared injection

**Severity:** P2. **Owner:** Core context engine / shared prompt policy.

### Evidence

`nodes/experiment/tests/test_context_slimming.py` enforces an initial-system
budget of 1400 tokens. On the fetched base commit, reconstructing the same
Experiment harness and State yields 3103 tokens; after this Experiment change
compacts its own prompt and rules it currently yields 2787. The base therefore
already exceeds the asserted budget. The current full Experiment suite reaches 462 passes before this sole failure.

The excess is not solely node-owned text: Core injects platform capabilities,
workspace/provenance instructions, a generic blocker section, the shared
claim-evidence skill, expected-output prose and the global KB heuristic. Those
shared sections alone exceed the test budget in this environment. Experiment
can compact its own prompt (done here), but cannot make the stated global
invariant true without deleting required shared instructions or changing Core.

### Required Core decision

Either enforce a real per-section prompt budget in `context_engine` (with
stable required sections and explicit truncation/indexing), or change the test
to measure the node-owned core prompt rather than all shared injections. Do not
raise the threshold silently without recording which sections are intentionally
always resident.

### Acceptance tests

- Construct an Experiment State with representative platform capability and
  workspace injections; assert the chosen global budget contract.
- Separately assert that Experiment-owned system prompt + rules fit their
  agreed node budget.
- Verify the claim-evidence skill is available by `load_skill` when it is
  indexed rather than silently dropped.


## CS-15 — Child run does not persist the canonical dispatched-input digest

**Severity:** P2. **Owner:** Core executor / shared `run_node` contract.

### Evidence

Experiment now records the exact `request_sha256` and `payload_sha256` sent by
`dispatch_data_request`, binds one direct Data child by `run_start.parent_run_id`
and node type, and accepts only terminal receipt members whose framework
producer identity is that child. This closes artifact selection, project-worktree
shadowing, and resumed-child provenance on the Experiment side.

However, Core writes child `run_start` with node type and parent run id but no
versioned canonical digest of the actual normalized `node_inputs`. Therefore a
later audit can prove that the parent called the managed wrapper and that the
artifact came from its direct Data child, but cannot independently prove from
the child transcript that the child received exactly the payload whose hash is
in the parent receipt. For formal input preparation, that payload contains the
frozen preregistration identity/version/content-hash triple.

### Required Core change

At the child execution boundary, after `run_node` has normalized the actual
`node_inputs` and before the child starts, compute a versioned canonical JSON
SHA-256 and persist only that digest plus its schema/version in `run_start`.
Do not persist arbitrary payload text there: Data requests may carry sensitive
locators or credentials. Return or otherwise expose the same immutable receipt
field to the parent. Experiment can then calculate the same digest over the
already-existing `dispatch_node_inputs` and require equality for every managed
Data receipt, including pause reconciliation.

This is a Core/shared provenance receipt, not a new lifecycle or a Data-specific
execution path. Legacy child transcripts without the field remain readable, but
must not newly authorize a formal Experiment fallback.

### Acceptance tests

- A `run_node(data, node_inputs=...)` child has a `run_start` input-digest
  receipt matching the exact normalized call arguments.
- Two inputs that differ in a frozen prereg version/hash have different
  receipts; key ordering does not alter a receipt.
- A managed Experiment Data dispatch rejects a direct child whose durable
  input digest differs or is absent when formal fallback authority is requested.
- The persisted receipt contains no raw request payload or credential value.

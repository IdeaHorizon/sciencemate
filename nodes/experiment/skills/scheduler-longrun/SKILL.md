---
name: scheduler-longrun
description: |
  Submit, monitor, recover, and finalize managed scheduler or long-running
  Experiment work. Use before scheduler submission, for known long work, or
  immediately after a synchronous timeout.
applies_when:
  - A scheduler, expected_duration_s >= 600, check_after_seconds, or prior timeout is present
  - An external job must be observed, cancelled, handed off, or finalized
tools_used:
  - discover_resources
  - recommend_resources
  - submit_job
  - check_external_job_health
  - wait_for_external_job
  - finish
  - cancel_job
expected_outcome: A managed job with declared resources, health checks, terminal evidence, and closure
status: validated
---

# Managed Scheduler and Long Run

1. Use current resource discovery/recommendation when choosing how to submit;
   refresh relevant facts when the environment or requirements change. Keep cwd
   and outputs within the actual authorized boundaries, following the existing
   tool path coordinates. Reuse framework-assigned paths; this skill neither
   requires declaring internal path roles nor expands write permissions.
   For managed work, record stage, expected duration, resource request,
   output paths, and a shell-free health check: progress paths, completion paths,
   error patterns, polling interval, and stall limit.
2. Submit only through `submit_job`. Do not use `nohup`, `setsid`, shell `&`,
   `disown`, bare scheduler commands, manual `ps` loops, or bare `kill` for a
   managed job. A synchronous timeout is an observation, not an instruction to
   submit again: distinguish long work from blocked input/I/O or a failed step.
   Inspect the existing execution and termination facts first; if effects are
   still open or termination is uncertain, use the supported observation,
   cancellation or handoff path instead of starting a duplicate. Independent
   read-only diagnosis remains available. When a retry is safe, choose bounded
   synchronous execution or managed work according to
   the actual need and supported limits, not merely a larger timeout. If the
   needed managed entry is unavailable, report the capability gap; do not
   bypass it with bare shell submission.
3. After submission, verify job identity and output location. While the chat is
   active, call `wait_for_external_job`; if it returns healthy elapsed waiting,
   call it again. Use `check_external_job_health` for a read-only diagnosis, not
   as a substitute for managed waiting.
4. At termination, inspect exit evidence and outputs. The framework records the
   scheduler's terminal facts; `finish` refuses success while a job is still open. Cancel a replacement
   through `cancel_job` before submitting another one. If the session must end,
   preserve the managed handoff instead of claiming success.
5. Read the scheduler's terminal-state facts before interpreting `State` /
   `ExitCode` (Slurm) or `job_state` / `Exit_status` (PBS Pro, Torque). They are
   assets of this skill, each fact with its official source and the cases in
   which it lies: `references/slurm-terminal-facts.md` and
   `references/pbs-torque-terminal-facts.md` (fetch with
   `load_skill(name='scheduler-longrun', asset='<path>')`). The runtime points
   at the right file once per job when a managed job reaches `terminal`. A zero
   low byte in the exit code never proves success on its own; the tool's
   `terminal_evidence` is the verdict.

This skill governs execution supervision only. It does not relax source-build,
input provenance, preregistration, freeze, or scientific closure gates.

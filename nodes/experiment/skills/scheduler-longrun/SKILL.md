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
  - finalize_external_job
  - cancel_job
expected_outcome: A managed job with declared resources, health checks, terminal evidence, and closure
status: validated
---

# Managed Scheduler and Long Run

1. Discover/recommend resources before submission and keep the job in declared
   `build_root` or `run_root`. Record stage, expected duration, resource request,
   output paths, and a shell-free health check: progress paths, completion paths,
   error patterns, polling interval, and stall limit.
2. Submit only through `submit_job`. Do not use `nohup`, `setsid`, shell `&`,
   `disown`, bare scheduler commands, manual `ps` loops, or bare `kill` for a
   managed job. A synchronous timeout is evidence to submit, not a reason to
   rerun with a larger foreground timeout.
3. After submission, verify job identity and output location. While the chat is
   active, call `wait_for_external_job`; if it returns healthy elapsed waiting,
   call it again. Use `check_external_job_health` for a read-only diagnosis, not
   as a substitute for managed waiting.
4. At termination, inspect exit evidence and outputs. Freeze scientific results
   and the required log before `finalize_external_job`. Cancel a replacement
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

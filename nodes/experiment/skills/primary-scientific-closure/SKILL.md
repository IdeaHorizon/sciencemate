---
name: primary-scientific-closure
description: |
  Complete the execution-layer closure for a primary preregistered scientific
  simulation. Use only when scope=scientific, run_role=primary, and
  stage=simulation, after result evidence is ready.
applies_when:
  - A primary scientific simulation needs verdict, credibility, sediment, or final experiment-log closure
tools_used:
  - resolve_prereg_questions
  - resolve_prereg_hypotheses
  - assess_sediment_candidate
  - declare_no_sediment
  - preview_experiment_contract
  - freeze_artifact
expected_outcome: A mechanically auditable primary execution record ready for Analysis, or an explicit blocker
status: validated
---

# Primary Scientific Closure

1. Start from the frozen preregistration and the frozen clean-results evidence.
   Resolve every frozen research question; resolve hypotheses only for questions
   that have propositions. Do not use free-text KB search to guess the contract.
2. Write a separate Verdict section with machine-readable `verdict:`. A
   provisional conclusion must identify measured metrics, closure discharges,
   threshold comparisons, and evidence IDs/run IDs. An inconclusive conclusion
   must state the unmet condition and the next required evidence.
3. Write a separate Credibility section with exactly one supported value:
   `credibility: reliable`, `credibility: questionable`, or
   `credibility: invalid`. Base it on recorded tool/output evidence; uncertainty
   is `questionable`, not optimism.
4. Before freezing, assess a methodological/dead-end sediment candidate, or
   call `declare_no_sediment` with an auditable reason. Do not invent empirical
   findings and do not perform this step for operation runs.
5. Run `preview_experiment_contract`, address every blocker in the same draft
   log and freeze it with `freeze_artifact`. Only when this execution itself merits durable KB indexing, optionally register it with `create_experiment`; absence of that index does not reopen the frozen evidence closure.

This is execution-layer closure only. Analysis owns final per-hypothesis
validated/refuted status; this skill cannot amend preregistration or weaken
verdict, provenance, uniqueness, citation, sediment, or QC gates.

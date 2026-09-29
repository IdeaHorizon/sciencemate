---
name: scientific-results
description: |
  Parse, validate, preserve, and freeze results from any scientific Experiment
  run. Use after a scientific simulation or analysis-producing execution,
  including secondary and diagnostic scientific runs.
applies_when:
  - scope is scientific and raw outputs need parsing, validation, aggregation, or freezing
  - A scientific result has anomalies, unit/schema uncertainty, or replay/provenance needs
tools_used:
  - freeze_artifact
  - save_artifact
  - read_artifact
expected_outcome: Traceable raw and clean results with honest quality annotations and a frozen execution record
status: validated
---

# Scientific Results

1. Preserve raw output before interpretation. Record command, input identifiers,
   output paths, exit evidence, hashes where applicable, and the mapping from
   raw files to derived values. Do not delete an input named by frozen result
   provenance.
2. Parse with declared units, schema, parameter values, and run role. Validate
   completeness, numeric sanity, convergence/completion signals, duplicates,
   missing values, and anomalies. Keep uncertain values as uncertain; do not
   turn a successful process exit into a scientific claim.
3. Save an explicit raw-results manifest, then create clean results whose
   metadata points back to exact raw evidence. Aggregate or normalize only with
   the stated method and preserve exclusions, transformations, and limits.
4. Save each evidence artifact with its declared type, then freeze in order by
   calling `freeze_artifact` on the `raw_results` artifact ID, the
   `clean_results` artifact ID, and finally the one canonical `experiment_log`
   artifact ID. Do not create a
   `_v2` or replacement log to evade an incomplete record.
5. Leave result eligibility alone: there is no eligibility bit to set. Say what
   the run is (run_role, execution_mode) and let the contract derive the rest.
   If a completed measurement genuinely cannot be replayed, declare
   `not_replayable: true` with a `reason`. Secondary, pilot, or
   diagnostic output may be useful evidence but is not a primary preregistered
   result. Report failures and blockers honestly rather than silently filling
   gaps.

6. A scientific run without a frozen prereg is exploratory. Say so in the
   `experiment_log` ("exploratory, no prereg"), list the parameter points
   actually used and the observables measured, and leave what the result means
   to downstream judgement. There is no extra schema field for this.

For a primary simulation, load `primary-scientific-closure` before writing
verdict, sediment, or credibility closure. This skill never changes a
hypothesis status and never substitutes for service-side freeze/QC checks.

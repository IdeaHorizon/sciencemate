# Data-node preprocessing planning

This package implements the pre-execution Designer/Critic loop for the `data` node.

## Runtime flow

1. `analyze_preprocessing_requirements` normalizes every input as a versioned `PreprocessingRequest`.
   `plan_bound` preserves declared stages; `request_bound` creates asset-owned work units and never invents
   a research stage or research plan.
2. Insufficient evidence returns `needs_reference_search` with focused official-documentation queries.
3. Search results are passed back as `reference_evidence`; no application-specific skill is created.
   Missing assets are resolved through the approved plan's selected registered tools. Incomplete evidence
   produces one recoverable preprocessing blocked report rather than guessed data or repeated HITL.
4. The Designer converts the accepted requirement analysis into a versioned execution plan.
5. The Critic scores seven dimensions. Deterministic schema and DAG checks are hard gates.
6. A failed candidate is revised for at most five iterations. The loop stops early on approval or convergence.
7. Approval binds to the exact plan SHA-256. A newer or modified plan invalidates the approval.
8. Registered generation entry points reject execution until the current plan is approved.

The caller has already fixed the scientific scope. The requirement layer does not select, rank, compare,
merge, or defer hypotheses.

Run-local records are written under `<state.root>/planning/`:

- `plan_XX.json`
- `critique_XX.json`
- `approved_plan.json`

## Module map

- `request_contract.py`: request identity, authority lock, review profile, and work-order compilation.
- `schemas.py`: plan normalization and deterministic structural gates.
- `store.py`: versioned plans, critiques, hashes, approvals, and execution authorization.
- `../tools/preprocessing_planner.py`: Requirement Analyst, Designer, Critic, scoring policy, and the bounded planning loop.

## Environment boundary and execution

Experiment owns Python packages, CLI executables, compilers, modules, licensed runtimes, and all installation
or source-build work. Data executes the approved DAG using that supplied environment. Missing runtime
capabilities become an auditable `externally_blocked` contract naming the affected step and requirement;
independent preprocessing assets remain deliverable.

Python imports in a generated script are statically recorded as
`artifact_spec.runtime_dependencies`, per-file provenance, and package-manifest
`runtime_dependencies`; generation never installs those packages. Experiment consumes this metadata when
preparing the downstream execution environment.

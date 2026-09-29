# RFC: Project v2 Git repository and research workspace

Status: implemented on the architecture branch; production migration pending

Schema version: `2`

## Decision

Every Project is one portable Git repository and one shared research workspace. Git is the durable authority for Project files and revision history. PostgreSQL indexes those revisions and owns execution/query state, but must never advertise a Project revision that has no Git commit.

Nodes exchange research material by reading the Project worktree. They no longer copy files through a framework artifact bus or prove freshness through file modification times. `save_artifact` remains a compatible semantic tool, but it is not the Git boundary and it is not required for scripts, notes, configs, manuscripts, figures or other ordinary Project files.

The Platform is the only Git writer. An agent can edit files through its tools; after every tool call the Harness checks its directory boundary, and at the end of every node outcome the Platform creates one checkpoint commit. Agents do not stage, commit, merge or reset.

## Fixed root

```text
project.yaml                         machine-readable identity and publish mode
PROJECT.md                          objective, scope and acceptance criteria
MEMORY.md                           canonical, bounded Project research memory
access/
  members.yaml                      Project membership projection
  roles.yaml                        user role capabilities
  nodes.yaml                        node → writable root ownership
  artifact-types.yaml               optional semantic release/receipt policy
resources/
  registry.yaml                     datasets, compute, storage and services
  environments.lock.yaml            reproducible environment references
  secrets.refs.yaml                 secret-manager references, never values
literature/                         owned by Literature
hypothesis/                         owned by Hypothesis
data/                               owned by Data
experiment/                         owned by Experiment
postprocess/                        owned by Postprocess
writing/                            owned by Writing
reviews/                            owned by Reviewer
runs/                               registered external-job/run manifests
.research/
  repository.yaml                   authority and lifecycle policy
  schema-version                    exact schema marker
  orchestration/                    Platform/Orchestrator state and Session manifests
  releases/                         publication records
  migrations/                       idempotent migration records
  legacy/                           preserved v1 material
```

Each scientific node owns the contents of its top-level directory. The layout below that directory is deliberately not prescribed by the central architecture: its maintainer and agent may organize scripts, notes, inputs and results for that discipline. A README may describe the directory to later readers.

`MEMORY.md` is one file rather than a directory. Research Projects are expected to have a few hundred durable facts at most. Its fixed headings are Goals, Decisions, Findings, Constraints and Open questions. The curator compresses it when it grows; a future scale problem can introduce an index without changing today’s authority.

Large HPC outputs, model weights and datasets do not enter Git. `runs/` or the owning node directory stores a small manifest containing URI, content hash, producer, parameters and retrieval instructions. Git rejects secrets, symlinks and oversized blobs at checkpoint time.

## Authority and permissions

| Actor | May read | May edit | May commit/publish |
| --- | --- | --- | --- |
| scientific node | whole Project | its owned top-level directory | never |
| Reviewer | whole Project | `reviews/` | never |
| Curator | whole Project | `MEMORY.md` | never |
| Orchestrator | whole Project | `.research/orchestration/` | never |
| Platform | whole Project | policy-controlled checkpoint/publish paths | only authority |

The boundary is enforced centrally, not by six copies of node code. Shell and Python working directories are constrained to the owned root. After every generic tool call the Harness scans the complete worktree; unauthorized edits and agent-created Git commits are reverted and recorded. The Platform validates the same ownership policy again before checkpointing, so bypassing either layer is insufficient.

Unknown extension nodes are isolated under `runs/extensions/<node>/<run>` and do not gain a top-level root by guessing a name.

## Session and publication lifecycle

```text
create Project
  → initialize v2 tree and main commit
create Session
  → session/<session_id> branch + isolated worktree
node edits files
  → UI polls current Git status/tree/diff; no commit is needed to display changes
node ends as completed / incomplete / blocked / failed
  → Platform validates ownership and creates one outcome checkpoint commit
interactive Project
  → user reviews the Session diff and publishes approved changes
continuous Project
  → a completed top-level run attempts the same CAS-guarded publish automatically
publish
  → replay approved Session paths as one linear main commit
  → advance PostgreSQL ProjectRevision only after Git succeeds
```

Checkpointing and publishing are separate. Incomplete or blocked work remains on the Session branch as an ordinary, inspectable commit; it is not deleted and is not silently mixed into Project `main`. Review is a research/governance decision, not a prerequisite for Git history. Continuous mode can skip human Git review, while policy-sensitive scientific release decisions remain separate.

A Session worktree has one mutation lane. Project-bound `run_node(background=true)` is rejected because a background child and its parent could otherwise race Platform checkpoints in the same checkout. Work that genuinely must run concurrently uses another Session/worktree; long external HPC jobs remain asynchronous through their registered job handle and output manifest.

Every mutating operation compares the expected and actual Git head. Direct agent commits, concurrent head movement and stale database projections fail loudly. Publishing is idempotent and uses database CAS plus the real Git head; it never falls back to a database-only revision.

## Runtime collaboration

The Project worktree is the upstream/downstream interface:

- a downstream node reads earlier nodes’ directories directly;
- it does not require a fixed list of transported artifact types;
- a node may use ordinary file, shell or Python tools to create any small file in its own directory;
- `save_artifact` may add semantic metadata but has no special version-control power;
- file evidence for Judge comes from the current Project tree, not only an artifact ledger.

When required material or a machine capability is missing, the node calls `report_blocker` with a category, evidence, requested outcome, suggested owner and whether retrying unchanged conditions can help. The framework does not encode solutions such as “install TinyTeX” or “move to node20”. The Orchestrator receives the blocker and uses its normal agent reasoning/tools to repair, redirect, ask a responsible node, wait for an external job, or escalate to a person.

The hard repeated-failure dispatch cap is disabled in Project v2. It previously turned a recoverable coordination failure into a dead end. Retry policy should be based on the blocker and changed conditions, not a global count detached from the cause.

## Judge and provenance

Judge has four mechanically distinct outcomes:

- `pass`: the evaluated scientific/mechanical condition passed;
- `fail`: the condition was evaluated and did not pass;
- `error`: Judge/provider/parser infrastructure failed, so no scientific verdict exists;
- `not_run`: the legacy check is not applicable or required evidence is absent.

Only `fail` is a negative scientific verdict. `error` is routed as infrastructure work and `not_run` cannot be converted into failure by reading a falsey field.

The old Project-mode modification-time provenance gate is removed. It tried to infer whether data belonged to the current run from timestamps and could reject valid files or encourage copying data merely to refresh a time. Project v2 instead records durable source facts in `resources/registry.yaml`, Git history and external-output manifests. This preserves scientific provenance without using provenance as a brittle file-transport admission system.

## Memory authority

In a Project-bound run, `MEMORY.md` is the only Project memory authority. Goals, decisions, findings, constraints and questions are versioned alongside the research. Node memory candidates are returned to the Orchestrator/Curator; only the Curator may edit the file. Hidden legacy MemoryV2 directories remain a CLI compatibility path for runs that are not bound to a Project and are never a second authority inside Project v2.

`PROJECT.md` is likewise the Project instruction authority for a bound Session. Organization and user profile layers may still be projected from the Platform, but a stale database Project snapshot cannot override the checked-out Project file.

## Legacy migration

Opening a v1 repository performs one clean-tree, idempotent migration commit:

1. preserve old `PROJECT.yaml`, `README.md`, `memory/`, `artifacts/`, `documents/`, `receipts/`, `sessions/`, `workflows/` and legacy policies below `.research/legacy/v1/`;
2. consolidate legacy memory text into canonical `MEMORY.md`;
3. create the v2 root, ownership files and schema marker;
4. record `.research/migrations/project-v2.json` with the source commit;
5. let the next ProjectRevision point to the migrated Git head.

Legacy database-only artifacts are preserved below `.research/legacy/database/`; they are not promoted back into a new artifact bus.

Migration refuses a dirty canonical checkout so it cannot overwrite uncommitted human work.

## Acceptance criteria

- A clean clone reconstructs every non-secret Project fact and the Project memory.
- Every visible mutation appears in the UI before commit and in a node-outcome commit afterward.
- Nodes read upstream work directly and can retain their own small scripts without node-specific Git code.
- Unauthorized cross-node edits, agent Git mutation, secrets, symlinks and oversized files are reverted or rejected.
- Blocked/incomplete work is preserved on the Session branch and never mistaken for successful publication.
- Judge infrastructure faults never become scientific failures.
- Project mode contains no modification-time provenance gate or required artifact-transport gate.
- Interactive and Continuous publication use the same Git/CAS path; only the human approval step differs.
- The architecture changes require no edits inside the six scientific node packages.

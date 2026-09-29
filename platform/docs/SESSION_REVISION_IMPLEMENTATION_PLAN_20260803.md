# Session and Project Revision implementation contract

Status: implementation baseline for the local multi-user slice.

## Product invariants

- A Project is the long-lived collaborative research object and owns the published head.
- A Session is one durable research objective and the graphical equivalent of one `chat.py` thread.
- A Session contains many Runs; a Run contains one or more Attempts.
- Chat messages, events, decisions, and Runs use the same `session_id`.
- A Session belongs to a Project, not to one user's private conversation list.
- Project members may view authorized Sessions. One primary driver at a time may append a main chat command.
- Session outputs are staged candidates. They do not overwrite the Project head until published.
- Published and frozen versions are immutable. Rollback creates a new Revision; it never deletes history.
- KB claims stay project-level append-only proposals with provenance; they are not text-merged through a ChangeSet.
- There is one chat product. `/chat` is a Project launcher; the working route is a Project Session.

## Canonical relationship

```text
Project 1 -> N Session 1 -> N Run 1 -> N Attempt
Project 1 -> N ProjectRevision
Session 1 -> N ChangeSet 1 -> N ChangeItem
Artifact 1 -> N ArtifactVersion
ProjectRevision -> manifest of selected ArtifactVersion / Project document / config versions
```

## Minimum data contract

Extend the existing `sessions` table into the canonical Session record. Do not create a second Session truth.

```text
sessions
  tenant_id, workspace_id, project_id, session_id
  title, summary
  created_by_user_id, primary_driver_user_id, driver_lease_until
  lifecycle_status        active | completed | archived
  base_revision_id
  policy_snapshot_id, model_backend_id, knowledge_read_watermark
  next_message_sequence, next_event_sequence
  created_at, updated_at, archived_at

session_messages
  id, session_id, sequence, actor_user_id
  role                    user | assistant | system
  content, command_id, created_at

project_revisions
  id, project_id, revision_no, parent_revision_ids
  source_session_id, source_run_id, created_by_user_id
  message, manifest, manifest_hash, created_at

change_sets
  id, session_id, base_revision_id
  status                  open | publishing | published | conflicted | abandoned
  published_revision_id, created_at, updated_at

change_items
  id, change_set_id
  resource_type           artifact | project_doc | project_config
  resource_key, operation
  base_version_id, proposed_version_id

merge_conflicts
  id, change_set_id, resource_type, resource_key
  base_version_id, project_version_id, proposed_version_id
  status, resolution, resolved_by_user_id, resolved_at
```

The legacy `conversations` rows are migrated into `sessions` and `session_messages`. Existing execution-only Session rows are preserved as recovered/archived Sessions rather than deleted.

## Runtime and concurrency contract

- Harness runtime identity is `(tenant_id, project_id, session_id)`, never Project-only.
- Runtime state lives under a Session-scoped directory. Run-local output remains further isolated by Run.
- Different Sessions in the same Project may run concurrently within configured quotas.
- One Session has one mutation lane. The driver lease is short, renewable, and explicitly transferable.
- Viewing, comments, event streaming, and authorized Decision responses do not require the driver lease.
- Waiting-human and turn boundaries are safe release/checkpoint boundaries; in-flight tools are not blindly migrated.

## Publish protocol

1. A Session opens a ChangeSet against base Revision `B`.
2. Runs append immutable candidate Artifact Versions or Project document/config proposals.
3. Publish reads current Project head `H`.
4. If `H == B`, create a new immutable Revision and update `projects.current_revision_id` with CAS.
5. If `H != B`, compare logical resource keys changed by `B -> H` and `B -> ChangeSet`.
6. Disjoint changes are automatically rebased and published.
7. The same resource key creates a fail-loud MergeConflict; neither side is overwritten.
8. After publication, the Session may continue with a fresh ChangeSet based on the new head.

## UI contract

- `/projects/:projectId/research` is the complete Session index.
- `/projects/:projectId/sessions/:sessionId` is the only working conversation/execution surface.
- `/projects/:projectId/chat` redirects to the remembered/recent Session or creates an empty Session.
- The stable Project navigation contains Research, Artifacts, Compute, and a restrained More/Settings affordance.
- The Session index groups Running, Needs attention, Unpublished, Recent, and Archived.
- A Session row shows title, driver/owner, execution state, base Revision, unpublished change count, usage, and last activity.
- The Session header shows `main · rN`, current driver, execution state, and quiet publish/conflict actions.
- Do not render nested card stacks. Assistant narration is open document text; tools are compact rows with active-only expansion.
- Use human language: Publish changes, Review updates, Resolve conflict, Restore this version. Avoid exposing Git terminology by default.

## Five required dogfood iterations

1. Researcher creates, renames, continues, completes, and archives Sessions; refresh/restart preserves identity.
2. Group admin and researcher verify project-visible Sessions, driver enforcement, and Decision authority.
3. Two Sessions create different Artifact candidates and publish non-conflicting Changes into consecutive Revisions.
4. Two Sessions modify the same logical Artifact; the second publish produces a visible conflict with no data loss.
5. Visual and failure audit at desktop and narrow widths: navigation density, empty/loading/error states, stale Session, cost/retry visibility, keyboard flow, and final artifact discoverability.

## Explicit non-goals for this slice

- Character-level collaborative editing (CRDT/OT).
- Copying the Project KB or vector index per Session.
- Treating membership, usage counters, or audit records as Project Revision content.
- Automatically merging preregistration commitments or contradictory scientific claims.

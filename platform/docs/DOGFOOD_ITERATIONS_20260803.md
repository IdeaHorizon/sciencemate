# Product dogfood log · Session and collaboration slice

This log records implementation-backed product iterations. An iteration counts only when code is integrated, the local app is exercised through a real user path, and the next changes are derived from observed behavior.

## Baseline audit (before iteration 1)

Tested route: `/projects/c3695bec-5e9a-441e-a167-712c807d00c9/chat`

Observed:

- Three simultaneous columns compete for attention: Project navigation, Conversation rail, and chat canvas.
- Project navigation exposes six destinations and four Recent Run rows at once.
- Header repeats three forms of the same context: App Server, Project research, Research chat.
- A visually dominant `Start` control is detached from the user's conversation intent.
- Assistant prose is enclosed in a large bordered card, contrary to the open-document execution language.
- Conversation rows are private-user records while execution Sessions are project-scoped; the two counts already diverge.
- The test Project contains 3 visible conversations, 7 Session projections, 8 Runs, 6 v1 Artifact rows, and 0 ArtifactVersion rows.
- The live Harness registry is keyed by Project, so different Sessions in one Project cannot actually run concurrently.
- The global rail repeats identity at both top and bottom, and duplicates `New research` with `Research`.
- Global Recent currently exposes an invalid recovered `global-*` execution as a Project Session link.
- The Projects page renders a small isolated card in a large empty canvas instead of a scannable project row/list with collaboration state.
- Researcher Settings exposes the full provider plumbing, two duplicate demo choices, and an unsupported backend; the effective model choice is not separated from administrator-only connection management.

Required correction:

- One canonical project-owned Session identity and append-only message stream.
- One stable, restrained Project rail; Session discovery moves to a dedicated Research index.
- One Session canvas with open assistant narration and compact activity rows.
- Session-scoped Harness runtime and staging.
- ArtifactVersion, ChangeSet, ProjectRevision, and publish/conflict semantics.

## Integration checkpoint (not counted as an iteration)

The first integrated build was exercised after applying migration `011` to the existing
local database. A 25 MB PostgreSQL custom-format backup was created before the upgrade.

Observed and corrected during integration:

- Membership-aware Run visibility initially compared the durable string `Run.project_id`
  with the UUID `Project.id`, producing a PostgreSQL `varchar = uuid` 500. The query now
  preserves membership visibility and casts only at the projection compatibility boundary.
- Readiness still expected migration `010` after the database reached `011`; the expected
  schema version now follows the canonical Session migration.
- A clean frontend restart was required to load the newly imported Session stylesheet;
  this exposed the intended open assistant canvas and compact composer rather than the
  stale three-column styling.
- The compatibility Session surface still reports `revision unavailable`, zero active
  Sessions, and unavailable member counts until the canonical HTTP endpoints replace the
  explicit legacy fallback. This checkpoint therefore does not qualify as Iteration 1.
- The simplified Projects row is substantially quieter, but browser review found duplicate
  user identity in the rail and three non-actionable, identically titled completed Recent
  items. These were returned to the frontend iteration queue.

## Iteration 1 · canonical Session lifecycle

Tested as the Researcher demo identity against the real PostgreSQL database, canonical
Session endpoints, DeepSeek model backend, and the formal Harness bridge.

Path exercised:

1. Opened the Project Research index with `includeArchived=true`.
2. Created Session `420bb6bc-5237-41d7-9649-865a6fc550d4`.
3. Renamed it to `Iteration 1 · Session lifecycle` and observed the rail update.
4. Submitted the first command with Enter; the real agent replied and persisted a
   collapsed `Completed 10 execution events` activity line.
5. Reloaded the browser and confirmed the title plus user/assistant messages survived.
6. Submitted a second command after reload; the Harness correctly recalled the first
   answer and returned `连续性验证通过`.
7. Archived the Session, confirmed the composer became read-only, returned to Research,
   and confirmed the collapsed Archived group increased from 5 to 6.

Corrections integrated before and during this iteration:

- Canonical project Session/message/member/driver endpoints replaced the explicit legacy
  fallback, so Revision `r0`, effective model, driver, token usage, and capabilities are real.
- Project chat streaming now appends `SessionMessage` records and does not grow the legacy
  Conversation table; non-streaming/global write bypasses fail with HTTP 410.
- The Research index explicitly includes archived Sessions while recent redirect excludes them.
- Empty open ChangeSets no longer masquerade as unpublished results.

Observed follow-up:

- Archiving clears the lease but the header rendered `Unassigned · driver`; Archived now
  suppresses that label and active unassigned Sessions use an honest no-driver label.
- The live collapsed execution-event line is not reconstructed after reload even though the
  Run/Event record is durable. This is scheduled for the final execution-visibility pass via
  `SessionMessage.commandId → Run/Event` rather than by inventing another transcript summary.

## Iteration 2 · shared Session authority

Tested with the Researcher, Research group administrator, and Institution administrator
demo identities against one canonical Project Session.

Path exercised:

1. Mia, the Project Lead, added Prof. Chen as a Project Researcher.
2. Mia created Session `4627d249-5d52-4527-aea1-99580e3dba70`, renamed it to
   `Iteration 2 · Shared driver`, and held the initial driver lease.
3. Prof. Chen could view the shared Session but could not take control while Mia's lease
   was live; the API returned HTTP 409.
4. Dr. Lin, an Institution administrator without Project membership, could inspect and
   govern membership but remained unable to create or drive a Session.
5. Mia released control. Prof. Chen then acquired the same Session, submitted a real
   command, and produced one shared user/assistant message pair, one Run, three Events,
   and an Artifact.
6. The canonical message API identified Prof. Chen as the actor and used the same
   `run_ae745070980a4bce868f0c811babe03f` command id for both messages.
7. While Prof. Chen held the live lease, Mia attempted to demote him from Researcher to
   Viewer. The backend rejected it with HTTP 409, the UI restored the previous role, and
   displayed `Transfer or release the active driver first`.

Corrections integrated before and during this iteration:

- A user holding a live driver lease can no longer be removed or demoted to Reviewer or
  Viewer. This is enforced in the backend, not inferred by the client.
- The Session header no longer offers a misleading `Take control` action while another
  valid lease exists; it states who holds control and remains view-only.
- Governance scope is not confused with execution authority: administrators can govern,
  but only explicit Project members with a driving role can append commands.
- Native `window.confirm` was replaced with an inline, keyboard-accessible Release/Cancel
  confirmation. Escape and blur cancel it; request errors remain next to the action.

Observed follow-up:

- The deterministic demo backend is still selected for this Project because an earlier
  smoke test changed Mia's effective preference. The final pass must select and verify a
  configured formal provider rather than silently retaining the demo executor.
- The live `Completed 3 execution events` row still disappears after reload; the durable
  activity reconstruction remains scheduled for Iteration 5.

## Iteration 3 · candidate staging and Project Revision publish

Tested as Mia against the canonical ChangeSet API and real PostgreSQL data.

Path exercised:

1. Created Session `aeaa82f2-09ce-4a91-a206-40f10db96ccb` at Project revision `r0`
   and renamed it `Iteration 3 · Publish candidate`.
2. Submitted a real local execution command. The resulting memo was written as a hidden
   candidate ArtifactVersion and ChangeItem, not exposed in the Project Artifact list.
3. Reloaded the Session and reviewed the `1 unpublished change` state, then published it.
   The Project head advanced atomically from `r0` to `r1`, the candidate became published,
   and the Artifact became visible.
4. Submitted a second command in the same Session. The stable logical resource key produced
   an `update` ChangeItem based on `r1`, with an auditable Markdown preview.
5. Expanded the quiet change summary, reviewed the candidate, and published again. The
   Project advanced to `r2`; parent, source Session, source Run, manifest, and hash are durable.

Corrections integrated before and during this iteration:

- Actual demo and formal Harness artifact output now flows through the same candidate
  ArtifactVersion/ChangeItem staging service; the compatibility Artifact row remains hidden
  until publish.
- Publish uses `Session.projectHeadRevisionId` as the optimistic CAS expectation while
  preserving `ChangeSet.baseRevisionId` as the three-way merge base.
- Knowledge Base claims are explicitly excluded from Project Revision manifests.
- The Session surface presents one restrained unpublished-change line, expandable previews,
  honest server truncation, and a single Publish action rather than a version-management page.

Observed follow-up:

- Immediately after the streaming turn finishes, the candidate exists but the Session
  change panel does not appear until reload. Query invalidation must refresh the canonical
  Session/ChangeSet at terminal completion without inventing optimistic changes.
- The Project Artifacts page still carries a legacy `V2`/lock description and does not expose
  the published Revision/version context. This is queued for the final visual/product audit.

## Iteration 4 · parallel Sessions, auto-rebase, and conflict resolution

Tested with two Project Sessions created at the same `r2` head:

- `b11b8be5-880c-471b-b8f2-93bbcc445e85` · `Iteration 4 · Parallel A`
- `ed62588f-99cd-4a81-b48e-8fd0bdb822b3` · `Iteration 4 · Parallel B`

Path exercised:

1. Staged `project_doc/parallel-a.md` in A and `project_doc/parallel-b.md` in B.
2. Published A to `r3`. B remained honestly based on `r2` and showed that the Project had
   advanced to `r3`.
3. Published B with the refreshed `r3` head expectation. Because the resource keys were
   disjoint, the backend mechanically rebased B and advanced the Project to `r4` without
   user conflict work. Both resources survived in the manifest.
4. Staged different candidate versions of `project_doc/shared-protocol.md` in both Sessions,
   now based on `r4`.
5. Published A to `r5`. Publishing B detected that the same logical resource changed and
   returned HTTP 409 without overwriting either version.
6. The Session UI opened one fail-loud conflict view showing common base, Project/theirs,
   and Session/ours. Chose `Use Session version`, then published the resolved ChangeSet to
   `r6`.
7. Queried the immutable Revision chain and final ArtifactVersion. `r5` points to A's
   candidate; `r6` points to B's chosen candidate, and both remain addressable.

Corrections integrated before and during this iteration:

- The client preserves Project head Revision ID separately from Session base Revision ID,
  so optimistic CAS and three-way merge use the correct references.
- Disjoint stale changes auto-rebase; same-resource changes always create durable conflict
  rows and require an explicit authorized resolution.
- Candidate previews are bounded and mark server truncation; Knowledge Base remains outside
  this merge model.

Observed follow-up:

- After resolving the final conflict, the prior publish error remained inline until the next
  publish, and the conflicted resource was duplicated below the comparison as a normal
  ChangeItem. The frontend follow-up clears stale mutation errors and suppresses duplicate
  rows while a resource has an open conflict.
- Conflict resolution updated the ChangeSet merge base but not the Session base projection,
  briefly rendering `Based on r4 · Project advanced to r5` after the conflict had already
  been rebased. The backend follow-up updates both projections atomically.

## Iteration 5 · formal Harness truth, failure recovery, and final visual audit

Tested as Mia with the real formal Harness bridge, DeepSeek and Kimi provider bindings,
the canonical Run/Event APIs, a 390 × 844 narrow viewport, and a restarted App Server.

Path exercised:

1. Ran a DeepSeek-bound Session through a real provider HTTP 402. The first build kept only
   transient SSE output, so refresh erased both the command and failure explanation.
2. Re-ran the same failure after the backend correction. Refresh retained one user message,
   one platform `system` failure, one failed Run, one failed Command, and four canonical
   Events. Secrets and local user paths were absent from SSE and persisted records; the
   Session remained retryable.
3. Ran a new Kimi Session and discovered that its unique conversation state still received
   prior Session commands through the Project-level `research_intake.json`. The Harness now
   uses a Session-owned intake root only for PlatformSession while preserving Project-shared
   artifacts, KB, and memory and preserving the existing Project-level CLI behavior.
4. Restarted the formal bridge and created Session
   `f942c698-c1b8-4a39-960b-49fbcfc144cb`. Its intake and serialized system prompt contained
   only its own commands. Two real turns produced two distinct Runs and Commands with
   operation-local token counts; refresh reconstructed two independent, collapsed
   `Completed 8 execution events` rows from canonical Run detail.
5. Created the clean continuation Session
   `a1fc1256-1f31-4170-b911-20def3e96b12` using the working Kimi backend and confirmed a
   normal Chinese research-assistant response, durable reload, keyboard Enter submission,
   Shift+Enter non-submission, driver state, and canonical usage visibility.
6. Reviewed Projects, Research sessions, the Session canvas, Artifacts index/detail,
   Compute, Project settings, model settings, and the three-role login surface at desktop
   width and 390 × 844. Delete and driver release use inline confirmation; no native browser
   confirmation remains on the reviewed paths.

Corrections integrated before and during this iteration:

- Formal execution failure reconstruction is atomic and fail-loud: no fabricated assistant
  answer, no partial duplicate Run/Command, and no secret-bearing raw provider error.
- Every persisted `SessionMessage` keeps `commandId` and `runId` as separate identities.
  Each distinct Run renders its own collapsed canonical activity line after reload; failed
  Runs open by default with status, event count, partial usage, retry count, and Run ID.
- Durable `system` failures render as restrained inline alerts instead of disappearing or
  masquerading as assistant prose. Legacy messages without `runId` never invent activity.
- Project artifacts are now an open, divider-based document index with real Artifact version
  and published Revision context. Detail is an open document surface with bounded metadata
  and inline deletion confirmation. Compute reports only scheduler/worker facts and does not
  estimate capacity.
- The global rail has one New research entry, Projects and Inbox as primary navigation,
  secondary destinations under More, and governance/settings at the bottom. The Project rail
  has three primary destinations plus at most three Recent Sessions. The `/chat` route is a
  Project-aware launcher/redirect, not a second workspace conversation product.
- Empty Session summaries no longer repeat `No summary yet`; finished tool activity folds into
  quiet one-line records while failures and Decisions stay prominent.
- Noisy connectivity, contamination, and failure fixtures were archived after the audit,
  leaving the clean formal Session and the Revision/concurrency demonstrations discoverable.

Observed follow-up and disposition:

- DeepSeek is configured but the current external account has insufficient balance. The
  platform surfaces this honestly and Kimi remains the verified working default; provider
  billing is external deployment state, not hidden by a demo fallback.
- A literal one-line validation prompt made Kimi emit the Harness continuous-status marker.
  A normal research-assistant prompt and a continuity turn both answered correctly. The
  malformed validation Session was archived rather than presented as product content.
- The previous backend full-suite failure expected a removed scheduler-owned direct Graph
  mutation method. The regression was aligned with the canonical
  `GraphRuntime.propose_growth()` path; the backend suite is now 40/40 green without
  restoring a second execution truth.

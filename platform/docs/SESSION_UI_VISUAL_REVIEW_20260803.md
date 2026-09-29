# Session UI visual review — 2026-08-03

Scope: Project Research index and the canonical Session workspace.

The in-app browser runtime reported no available browser during this worktree review. No screenshot result is claimed. Both passes below therefore used rendered DOM structure, route output, responsive CSS inspection, type checks, and the production build. A real-browser pass remains required after integration starts the branch in an available browser.

## Pass 1 — desktop hierarchy

Viewport target: 1440 × 900.

Problems found and corrected:

- The old page had three simultaneous columns: Project navigation, a permanent Conversation rail, and chat. The Conversation rail is removed; Session switching now lives in the Research index.
- Project identity and research context were repeated in the App rail, ProjectShell top bar, and page header. The ProjectShell top bar is removed.
- Research chat, Research navigation, and scheduler Start competed as primary actions. The stable Project navigation now has Research, Artifacts, Compute; Activity and Settings are under More. Scheduler Start is not chat chrome.
- Assistant narration inherited a bordered message card. Session-scoped assistant text now renders directly on the document background; only user input receives a quiet filled surface.
- Empty Session composer inherited the generic centered chat treatment. The Session canvas now reserves the reading area while keeping the composer at the bottom.
- A broad state selector colored the entire status label instead of its dot. State colors are now scoped to the six-pixel indicator.
- Explicit fixture mode exposed publish-like controls that could not mutate canonical state. Mutating controls are hidden in fixture mode.

Attention audit after corrections:

- One main task: continue this Session.
- One stable Project rail plus one document canvas.
- At most one primary header action is normally visible; conflict, publish, and driver actions are capability and state gated.
- Execution history is a secondary action on the same Session route, not a second Session product.

## Pass 2 — narrow hierarchy

Viewport targets: 760 × 900 and 390 × 844.

Problems found and corrected:

- Session metadata was hidden with positional `nth-child` rules; adding an advanced-revision notice could hide the wrong field. Metadata now uses semantic classes.
- At narrow width, detailed base/head divergence, driver attribution, unpublished count, and Review updates competed with the Session title. Narrow layout retains current head plus execution state; detail remains available at wider widths and in the index.
- Session rows attempted to retain three metadata columns on phone widths. Below 620 px, each row becomes state dot + title/summary + affordance.
- Composer context could crowd the input. The model chip is removed at phone width; Project and access remain.
- Text header actions collapse to compact icons or move into the Session action menu.

## Integration browser follow-up

- Verify desktop and 390 px screenshots after the integration worktree has an available browser runtime.
- Verify keyboard focus order: back, title rename, state action, message composer, send.
- Verify long Project, Session, and member names in both index rows and the header.
- Verify canonical empty, loading, error, archived, stale, conflict, and base-behind-head states against the backend fixtures.

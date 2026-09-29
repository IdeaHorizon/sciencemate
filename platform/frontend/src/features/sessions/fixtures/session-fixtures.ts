import type { CurrentUser } from "@/lib/api";
import type { ProjectSessionCapability, ResearchSession, SessionMessage } from "../types";
import type { ContextWindowState } from "../lib/context-window";

const usage = (tokens: number, cost: number | null, coverage: "complete" | "partial" = "complete") => ({
  promptTokens: Math.round(tokens * 0.72),
  completionTokens: Math.round(tokens * 0.28),
  totalTokens: tokens,
  cost,
  currency: cost === null ? null : "USD",
  coverage,
});

/** 演示态的窗口占用：形状与后端 `contextWindow` 一致，数字只为让 chip 有东西可画。 */
const contextWindow = (effective: number, at: string): ContextWindowState => ({
  at,
  runId: "run-fixture",
  turn: 12,
  promptTokens: Math.round(effective / 1.3),
  estimatedTokens: Math.round(effective / 1.3),
  effectiveTokens: effective,
  window: 256_000,
  configuredWindow: 256_000,
  compressAt: 0.7,
  emergencyAt: 0.9,
  breakdown: {
    system: Math.round(effective * 0.06),
    tools: Math.round(effective * 0.08),
    toolResults: Math.round(effective * 0.52),
    summary: Math.round(effective * 0.07),
    framework: Math.round(effective * 0.03),
    conversation: Math.round(effective * 0.24),
  },
  messageCount: 88,
  lastCompaction: { turn: 7, tokensBefore: 210_000, tokensAfter: 96_000 },
});

/** 演示态那张待答卡片。真实态由后端随局面一起下发（execution.answer.pause）。 */
const FIXTURE_PENDING_APPROVAL = {
  kind: "human_input" as const,
  runId: "run-fixture",
  reason: "waiting_human",
  prompt: "Which evidence tier should the survey lead with?",
  context: null,
  options: ["Randomised trials only", "Include high-quality cohorts"],
  optionDetails: [],
  recommendedOptionIndex: null,
  offer: null,
  askingNodeType: "literature",
  pauseKind: "structured_question",
  askedAt: "2026-08-03T12:00:00Z",
};

export function sessionFixtures(projectId: string, user?: CurrentUser | null): ResearchSession[] {
  const driverId = user?.id ?? "demo-researcher";
  const driverLabel = user?.display_name ?? "Dr. Lin";
  const base = {
    baseCommitSha: "1".repeat(40),
    headCommitSha: "1".repeat(40),
    aheadBy: 0,
    behindBy: 0,
    projectId,
    createdByUserId: driverId,
    primaryDriverUserId: driverId,
    lifecycleStatus: "active" as const,
    modelBackendId: "deepseek-v4-local",
    archivedAt: null,
    driverLabel,
    creatorLabel: driverLabel,
    source: "fixture" as const,
    gitBranch: "session/demo",
    gitBaseCommitSha: "719ef5c7c8b10f4f7c3abc029bc97f5af40f5544",
    gitHeadCommitSha: "9a4f482816a0ad32b11b1c19c69cc3a2e95014ae",
    modelBackendName: "Project model",
    capabilities: ["view", "drive", "publish", "resolve", "manage_members"] as ProjectSessionCapability[],
    contextWindow: null as ContextWindowState | null,
  };
  return [
    {
      ...base,
      id: "session-catalyst-survey",
      title: "Map the current evidence landscape",
      summary: "Comparing methods, limitations, and evidence quality across 18 selected papers.",
      createdAt: "2026-08-03T08:30:00Z",
      updatedAt: "2026-08-03T12:18:00Z",
      runCount: 4,
      unpublishedChangeCount: 3,
      conflictCount: 0,
      execution: { phase: "alive", waitingOn: null, outcome: null, error: null, canStop: true, answer: { via: "composer" }, label: "Running", runId: "run-fixture", since: "2026-08-03T12:00:00Z" },
      contextWindow: contextWindow(133_000, "2026-08-03T12:17:40Z"),
      usage: usage(42840, 1.82),
      retryCount: 1,
    },
    {
      ...base,
      id: "session-baseline-review",
      title: "Review baseline assumptions",
      summary: "The evidence gate needs a decision on two contradictory benchmark claims.",
      createdAt: "2026-08-02T10:20:00Z",
      updatedAt: "2026-08-03T11:42:00Z",
      runCount: 3,
      unpublishedChangeCount: 2,
      conflictCount: 1,
      execution: { phase: "alive", waitingOn: { kind: "human" }, outcome: null, error: null, canStop: true, answer: { via: "pause", pause: FIXTURE_PENDING_APPROVAL }, label: "Needs your answer", runId: "run-fixture", since: "2026-08-03T12:00:00Z" },
      usage: usage(19420, 0.74),
      retryCount: 0,
    },
    {
      ...base,
      id: "session-method-note",
      title: "Draft the methods note",
      summary: "Candidate method text and one figure are ready to review before publication.",
      createdAt: "2026-08-01T09:10:00Z",
      updatedAt: "2026-08-03T10:05:00Z",
      runCount: 2,
      unpublishedChangeCount: 4,
      conflictCount: 0,
      execution: { phase: "ended", waitingOn: null, outcome: "ok", error: null, canStop: false, answer: { via: "composer" }, label: "Completed", runId: "run-fixture", since: "2026-08-03T12:00:00Z" },
      usage: usage(12330, 0.51),
      retryCount: 0,
    },
    {
      ...base,
      id: "session-dataset-audit",
      title: "Audit dataset provenance",
      summary: "Source licenses and duplicate structures verified; no project changes remain.",
      primaryDriverUserId: null,
      driverLabel: "Unassigned",
      lifecycleStatus: "completed",
      createdAt: "2026-07-30T14:00:00Z",
      updatedAt: "2026-08-02T16:34:00Z",
      runCount: 1,
      unpublishedChangeCount: 0,
      conflictCount: 0,
      execution: { phase: "ended", waitingOn: null, outcome: "ok", error: null, canStop: false, answer: { via: "composer" }, label: "Completed", runId: "run-fixture", since: "2026-08-03T12:00:00Z" },
      usage: usage(8290, null, "partial"),
      retryCount: 0,
    },
    {
      ...base,
      id: "session-archived-scope",
      title: "Initial scope exploration",
      summary: "Superseded scoping pass retained for provenance.",
      primaryDriverUserId: null,
      driverLabel: "Unassigned",
      lifecycleStatus: "archived",
      createdAt: "2026-07-24T09:00:00Z",
      updatedAt: "2026-07-25T17:20:00Z",
      archivedAt: "2026-07-25T17:20:00Z",
      runCount: 2,
      unpublishedChangeCount: 0,
      conflictCount: 0,
      execution: { phase: "ended", waitingOn: null, outcome: "cancelled", error: null, canStop: false, answer: { via: "composer" }, label: "Cancelled", runId: "run-fixture", since: "2026-08-03T12:00:00Z" },
      usage: usage(5710, 0.22),
      retryCount: 2,
    },
  ];
}

export function sessionMessageFixtures(sessionId: string): SessionMessage[] {
  return [
    {
      id: `${sessionId}:1`, sessionId, sequence: 1, actorUserId: "demo-researcher",
      role: "user", content: "Survey the selected papers and identify the most credible open questions and methodological limitations.",
      commandId: "command-1", runId: null, offerId: null, createdAt: "2026-08-03T11:51:00Z",
    },
    {
      id: `${sessionId}:2`, sessionId, sequence: 2, actorUserId: null,
      role: "assistant", content: "I’ll compare the selected evidence, preserve conflicting claims, and keep source quality visible in the synthesis.",
      commandId: null, runId: null, offerId: null, createdAt: "2026-08-03T11:51:08Z",
    },
  ];
}

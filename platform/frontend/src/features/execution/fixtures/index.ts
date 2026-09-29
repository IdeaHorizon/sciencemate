import activeRun from "./active-run.json";
import completedRun from "./completed-run.json";
import decisionRun from "./decision-run.json";
import { parseExecutionEvents, type ExecutionEvent } from "../lib/execution-event";

export type FixtureId = "active" | "decision" | "completed";

export type ExecutionFixture = {
  id: FixtureId;
  label: string;
  description: string;
  provenance: "ui_demo";
  events: ExecutionEvent[];
};

export const EXECUTION_FIXTURES: Record<FixtureId, ExecutionFixture> = {
  active: {
    id: "active",
    label: "Active run",
    description: "UI demo: current activity stays open while completed siblings fold.",
    provenance: "ui_demo",
    events: parseExecutionEvents(activeRun),
  },
  decision: {
    id: "decision",
    label: "Needs decision",
    description: "UI demo: a blocked tool and its research decision remain expanded.",
    provenance: "ui_demo",
    events: parseExecutionEvents(decisionRun),
  },
  completed: {
    id: "completed",
    label: "Completed run",
    description: "UI demo: execution history folds while the final result stays prominent.",
    provenance: "ui_demo",
    events: parseExecutionEvents(completedRun),
  },
};

import activeRun from "./active-run.json";
import completedRun from "./completed-run.json";
import decisionRun from "./decision-run.json";
import { parseExecutionEvents, type ExecutionEvent } from "../lib/execution-event";
import type { Phrase } from "@/shared/i18n";

export type FixtureId = "active" | "decision" | "completed";

export type ExecutionFixture = {
  id: FixtureId;
  label: Phrase;
  description: Phrase;
  provenance: "ui_demo";
  events: ExecutionEvent[];
};

export const EXECUTION_FIXTURES: Record<FixtureId, ExecutionFixture> = {
  active: {
    id: "active",
    label: { zh: "执行中", en: "Active run" },
    description: { zh: "演示：当前操作保持展开，已完成的同级操作收起。", en: "UI demo: current activity stays open while completed siblings fold." },
    provenance: "ui_demo",
    events: parseExecutionEvents(activeRun),
  },
  decision: {
    id: "decision",
    label: { zh: "需要做决定", en: "Needs decision" },
    description: { zh: "演示：受阻的工具和对应的研究决策保持展开。", en: "UI demo: a blocked tool and its research decision remain expanded." },
    provenance: "ui_demo",
    events: parseExecutionEvents(decisionRun),
  },
  completed: {
    id: "completed",
    label: { zh: "已完成", en: "Completed run" },
    description: { zh: "演示：执行过程收起，最终结果突出显示。", en: "UI demo: execution history folds while the final result stays prominent." },
    provenance: "ui_demo",
    events: parseExecutionEvents(completedRun),
  },
};

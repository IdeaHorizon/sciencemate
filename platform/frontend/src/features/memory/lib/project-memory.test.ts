import assert from "node:assert/strict";
import test from "node:test";

import { entryId, parseProjectMemory } from "./project-memory.ts";

const SAMPLE = `# Project Memory

## Current Focus

- 当前阶段：预注册已冻结，等实验结果
- 下一步：跑三档冷却速率

## Decisions

- [D-001][2026-08-07][active] 用每个 Project 独立的 Git workspace。
- [D-002][2026-08-07][active] literature 定向模式只交 evidence package。

## Established Findings

- [F-001][supported] 更快冷却 → 更高最终势能。

## Open Questions
`;

test("MEMORY.md 按章节解析成记忆分类", () => {
  const sections = parseProjectMemory(SAMPLE);
  assert.deepEqual(
    sections.map((s) => s.heading),
    ["Current Focus", "Decisions", "Established Findings"],
  );
  assert.equal(sections[1].entries.length, 2);
  assert.match(sections[1].entries[0], /Git workspace/);
  // 空章节不占位（Open Questions 下面没有条目）
  assert.equal(sections.some((s) => s.heading === "Open Questions"), false);
});

test("稳定 ID 从条目开头解析，没有就是 null", () => {
  assert.equal(entryId("[D-001][2026-08-07][active] 用 Git workspace。"), "D-001");
  assert.equal(entryId("[F-002][refuted] H2 没有优于 baseline。"), "F-002");
  assert.equal(entryId("当前阶段：预注册已冻结"), null);
});

test("空输入不炸", () => {
  assert.deepEqual(parseProjectMemory(""), []);
  assert.deepEqual(parseProjectMemory("没有任何章节的正文"), []);
});

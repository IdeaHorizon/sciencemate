import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseExecutionEvents, type ExecutionEvent, type ExecutionEventKind } from "./execution-event.ts";
import { projectRunActivity as projectRunActivityRaw } from "./run-activity-detail.ts";

// 下面的判据断言的是**英文**那一份文案，所以这里显式说英文 —— 默认语言已经
// 是中文了。换成默认值就等于把「这些句子长什么样」这件事交给默认语言，
// 判据会跟着默认值一起漂。
const projectRunActivity = (events: readonly ExecutionEvent[], parentInterruptedHint?: boolean) =>
  projectRunActivityRaw(events, parentInterruptedHint, "en");

function event(
  sequence: number,
  kind: ExecutionEventKind,
  payload: Record<string, unknown>,
): ExecutionEvent {
  return {
    schemaVersion: 1,
    id: `event-${sequence}`,
    sequence,
    at: "2026-08-04T00:00:00Z",
    workspaceId: "workspace-a",
    projectId: "project-a",
    sessionId: "session-a",
    runId: "run-a",
    origin: "app_command",
    source: {},
    kind,
    visibility: "standard",
    payload,
  };
}

test("completed backend fixture exposes recorded step, tool input, and output", () => {
  const fixture = readFileSync(
    new URL("../../../../../contracts/fixtures/completed-run.jsonl", import.meta.url),
    "utf8",
  );
  const detail = projectRunActivity(parseExecutionEvents(fixture));
  const tool = detail.steps[0].tools[0];

  assert.equal(detail.directModelResponse, false);
  assert.equal(detail.steps[0].title, "Survey reproducible sampling methods");
  assert.equal(tool.technicalName, "search_literature");
  assert.equal(tool.title, "Search literature — reproducible environmental field sampling methods");
  assert.match(tool.input ?? "", /Query: reproducible environmental field sampling methods/);
  assert.equal(tool.output, "Found 12 relevant sources after deterministic de-duplication.");
  assert.equal(tool.status, "completed");
});

test("a simple assistant answer is labeled as a direct response without tool calls", () => {
  const detail = projectRunActivity([
    event(1, "run.started", {}),
    event(2, "session.message", { role: "assistant", content: "A direct answer." }),
    event(3, "run.completed", {}),
  ]);
  assert.equal(detail.directModelResponse, true);
  assert.equal(detail.toolCount, 0);
  assert.deepEqual(detail.steps, []);
});

test("tool errors, retries, redaction, and truncation stay explicit", () => {
  const detail = projectRunActivity([
    event(1, "step.started", { stepId: "step-a", title: "Check source" }),
    event(2, "tool.started", {
      stepId: "step-a",
      toolCallId: "tool-a",
      toolName: "read_source",
      arguments: { token: "[REDACTED]", excerpt: "partial…[TRUNCATED]" },
    }),
    event(3, "redaction.warning", {
      sourceEventId: "event-2",
      codes: ["sensitive_field", "string_truncated"],
    }),
    event(4, "tool.retrying", {
      stepId: "step-a",
      toolCallId: "tool-a",
      toolName: "read_source",
    }),
    event(5, "tool.failed", {
      stepId: "step-a",
      toolCallId: "tool-a",
      toolName: "read_source",
      errorCode: "source_unavailable",
      errorMessage: "The source could not be read.",
      retryable: false,
    }),
  ]);
  const tool = detail.steps[0].tools[0];

  assert.equal(tool.status, "failed");
  assert.equal(tool.retryCount, 1);
  assert.equal(tool.error?.title, "Read source material did not complete");
  assert.equal(tool.error?.message, "The source could not be read.");
  assert.deepEqual(tool.notices, [
    "Source payload truncated during ingest",
    "Sensitive values redacted",
  ]);
});

test("an unassociated group reports completed only when all recorded tools completed", () => {
  const detail = projectRunActivity([
    event(1, "tool.started", { toolCallId: "tool-a", toolName: "search_sources", arguments: {} }),
    event(2, "tool.completed", { toolCallId: "tool-a", toolName: "search_sources", resultSummary: "Done" }),
  ]);
  assert.equal(detail.steps[0].title, undefined);
  assert.equal(detail.steps[0].status, "completed");
});

test("completed internal preparation and scratchpad activity leave no transcript row", () => {
  const detail = projectRunActivity([
    event(1, "run.started", {}),
    event(2, "step.started", { stepId: "root", title: "Orchestrator activity" }),
    event(3, "tool.started", { stepId: "root", toolCallId: "scratch", toolName: "write_scratchpad", arguments: { content: "private note" } }),
    event(4, "tool.completed", { stepId: "root", toolCallId: "scratch", toolName: "write_scratchpad", resultSummary: "saved" }),
    event(5, "step.completed", { stepId: "root" }),
    event(6, "run.completed", {}),
  ]);

  assert.equal(detail.toolCount, 0);
  assert.deepEqual(detail.steps, []);
  assert.equal(detail.currentStatus, undefined);
  assert.deepEqual(detail.artifacts, []);
});

test("synthetic research-context resolution stays out of conversation activity", () => {
  const detail = projectRunActivity([
    event(1, "run.started", {}),
    event(2, "tool.started", {
      toolCallId: "context",
      toolName: "resolve_research_context",
      arguments: { project_id: "project-a" },
    }),
    event(3, "tool.completed", {
      toolCallId: "context",
      toolName: "resolve_research_context",
      resultSummary: "Context resolved",
    }),
    event(4, "session.message", { role: "assistant", content: "A plain answer." }),
    event(5, "run.completed", {}),
  ]);

  assert.equal(detail.toolCount, 0);
  assert.deepEqual(detail.steps, []);
  assert.equal(detail.directModelResponse, true);
});

test("a producing child remains as one human step while generic root preparation stays hidden", () => {
  const start = event(2, "step.started", { stepId: "child-literature", title: "Literature survey" });
  start.source.rawEvent = "subagent_call_start";
  const detail = projectRunActivity([
    event(1, "step.started", { stepId: "root", title: "Orchestrator activity" }),
    start,
    event(3, "step.completed", { stepId: "child-literature" }),
    event(4, "step.completed", { stepId: "root" }),
    event(5, "run.completed", {}),
  ]);

  assert.equal(detail.steps.length, 1);
  assert.equal(detail.steps[0].kind, "child");
  assert.equal(detail.steps[0].title, "Literature survey");
  assert.equal(detail.steps[0].status, "completed");
});

test("running projection exposes one human status and retains retry history", () => {
  const detail = projectRunActivity([
    event(1, "run.started", {}),
    event(2, "run.retrying", { reason: "provider_timeout" }),
    event(3, "tool.started", {
      stepId: "root",
      toolCallId: "search-one",
      toolName: "arxiv_search",
      arguments: { query: "scientific foundation models" },
    }),
  ]);

  assert.equal(detail.currentStatus, "Searching literature — scientific foundation models");
  assert.equal(detail.retryCount, 1);
  assert.equal(detail.steps[0].tools[0].title, "Search literature — scientific foundation models");
});

test("a rolled-back empty response names the failure and exact retry", () => {
  const detail = projectRunActivity([
    event(1, "run.started", { modelBackendName: "DeepSeek" }),
    event(2, "run.recovering", {
      reason: "void_turn",
      attempt: 2,
      maxAttempts: 5,
      promptTokens: 24_277,
      completionTokens: 1,
    }),
  ]);

  assert.equal(detail.currentStatus, "The model returned no usable response · retry 2/5");
  assert.equal(detail.retryCount, 1);
});

test("raw classification JSON stays out of the standard tool preview and artifact output is explicit", () => {
  const detail = projectRunActivity([
    event(1, "tool.started", {
      toolCallId: "archive",
      toolName: "archive_papers",
      arguments: {
        name: "literature_index",
        papers_json: "[{\"title\":\"bulk\"}]",
        classification_json: "{\"clusters\":[]}",
      },
    }),
    event(2, "tool.failed", {
      toolCallId: "archive",
      toolName: "archive_papers",
      errorCode: "tool_errorclassification_json",
      errorMessage: "classification_json parse failed: Extra data at line 1",
    }),
    event(3, "artifact.created", {
      artifactId: "report-a",
      name: "Survey report",
      artifactType: "survey_report",
      version: 1,
    }),
  ]);

  assert.equal(detail.steps[0].tools[0].input, "Name: literature_index");
  assert.equal(detail.steps[0].tools[0].input?.includes("classification_json"), false);
  assert.equal(detail.steps[0].tools[0].error?.title, "Paper classification was not saved");
  // 大块 payload 不进预览（上面两条断言），但**错误正文照原样给**——
  // 出问题的字段叫什么名字正是人和模型下一步要用的东西。
  assert.equal(
    detail.steps[0].tools[0].error?.message,
    "classification_json parse failed: Extra data at line 1",
  );
  assert.deepEqual(detail.artifacts, [{ id: "report-a", linkable: true, name: "Survey report", mediaType: "survey_report", version: 1 }]);
});

test("literature work stays one human child step with two durable artifacts", () => {
  const child = event(1, "step.started", { stepId: "literature", title: "Review heterogeneous MoE research" });
  child.source.rawEvent = "subagent_call_start";
  const detail = projectRunActivity([
    child,
    event(2, "tool.started", { stepId: "literature", toolCallId: "search", toolName: "search_papers", arguments: { query: "heterogeneous mixture of experts" } }),
    event(3, "tool.completed", { stepId: "literature", toolCallId: "search", toolName: "search_papers", resultSummary: "20 papers retained" }),
    event(4, "step.completed", { stepId: "literature", resultSummary: "Survey complete" }),
    event(5, "artifact.created", { artifactId: "index", name: "Literature index", artifactType: "literature_index", version: 1 }),
    event(6, "artifact.created", { artifactId: "report", name: "Survey report", artifactType: "survey_report", version: 1 }),
    event(7, "run.completed", {}),
  ]);

  assert.equal(detail.steps.length, 1);
  assert.equal(detail.steps[0].kind, "child");
  assert.equal(detail.steps[0].title, "Review heterogeneous MoE research");
  assert.deepEqual(detail.artifacts.map((artifact) => artifact.name), ["Literature index", "Survey report"]);
});

test("a calculation remains one folded-capable compute record alongside the answer", () => {
  const detail = projectRunActivity([
    event(1, "tool.started", { toolCallId: "calc", toolName: "execute_python", arguments: { name: "bootstrap confidence interval", code: "bulk code hidden" } }),
    event(2, "tool.completed", { toolCallId: "calc", toolName: "execute_python", resultSummary: "95% CI: 0.42–0.58" }),
    event(3, "session.message", { role: "assistant", content: "The interval is 0.42–0.58." }),
    event(4, "run.completed", {}),
  ]);

  assert.equal(detail.steps.length, 1);
  assert.equal(detail.steps[0].tools.length, 1);
  assert.equal(detail.steps[0].tools[0].title, "Run computation — bootstrap confidence interval");
  assert.equal(detail.steps[0].tools[0].input, "Name: bootstrap confidence interval");
  assert.equal(detail.directModelResponse, false);
});

test("structured computation output becomes readable terminal text without its JSON shell", () => {
  const detail = projectRunActivity([
    event(1, "tool.started", {
      toolCallId: "calc",
      toolName: "execute_python",
      arguments: {
        timeout: 30,
        code: "values = [1, 2, 3]\nprint(sum(values))",
      },
    }),
    event(2, "tool.completed", {
      toolCallId: "calc",
      toolName: "execute_python",
      resultSummary: JSON.stringify({
        status: "success",
        stdout: "6\nconfidence interval: [1.2, 2.8]\n",
        stderr: "",
        return_code: 0,
        output_files: [],
      }),
    }),
  ]);
  const tool = detail.steps[0].tools[0];

  assert.equal(tool.input, "Timeout: 30");
  assert.equal(tool.code, "values = [1, 2, 3]\nprint(sum(values))");
  assert.equal(tool.outputFormat, "terminal");
  assert.equal(tool.output, "stdout\n6\nconfidence interval: [1.2, 2.8]\n\nExit code 0");
  assert.equal(tool.output?.includes("stdout\""), false);
  assert.equal(tool.output?.includes("output_files"), false);
});

test("canonical computation tail fixture renders stdout and exit code without JSON", () => {
  const detail = projectRunActivity([
    event(1, "tool.started", {
      toolCallId: "calc-tail",
      toolName: "execute_python",
      arguments: { code: "print(42)", timeout: 30 },
    }),
    event(2, "tool.completed", {
      toolCallId: "calc-tail",
      toolName: "execute_python",
      resultSummary: JSON.stringify({
        status: "success",
        returncode: 0,
        stdout_tail: "mean = 2.0\n95% CI = [1.2, 2.8]\n",
        stderr_tail: "",
      }),
    }),
  ]);
  const tool = detail.steps[0].tools[0];

  assert.equal(tool.outputFormat, "terminal");
  assert.equal(tool.output, "stdout\nmean = 2.0\n95% CI = [1.2, 2.8]\n\nExit code 0");
  assert.equal(tool.output?.includes("stdout_tail"), false);
  assert.equal(tool.output?.includes("returncode"), false);
});

test("unknown structured tool payload stays in Trace instead of the standard output", () => {
  const detail = projectRunActivity([
    event(1, "tool.started", { toolCallId: "calc", toolName: "execute_python", arguments: { timeout: 30 } }),
    event(2, "tool.completed", {
      toolCallId: "calc",
      toolName: "execute_python",
      resultSummary: JSON.stringify({ internal_payload: { raw: true } }),
    }),
  ]);

  assert.equal(detail.steps[0].tools[0].code, undefined);
  assert.equal(detail.steps[0].tools[0].output, undefined);
});

test("terminal parent fixture interrupts unfinished children without leaving a live status", () => {
  // 调用方现在传的是「父 run 断了没有」这个布尔，而不是一个状态字符串 ——
  // 哪些状态算"被打断"由后端的 view 回答，消费方不再各写一份名单。
  for (const parentStatus of [true] as const) {
    const childStart = event(1, "step.started", {
      stepId: "literature-child",
      title: "Literature search",
    });
    childStart.source.rawEvent = "subagent_call_start";
    const detail = projectRunActivity([
      childStart,
      event(2, "tool.started", {
        stepId: "literature-child",
        toolCallId: "search",
        toolName: "semantic_scholar_search",
        arguments: { query: "GShard scaling laws" },
      }),
      event(3, "session.message", { role: "assistant", content: "The run was interrupted." }),
    ], parentStatus);

    assert.equal(detail.steps[0].status, "interrupted", String(parentStatus));
    assert.equal(detail.steps[0].tools[0].status, "interrupted", String(parentStatus));
    assert.equal(detail.currentStatus, undefined, String(parentStatus));
  }
});

test("recorded terminal events also interrupt an unfinished tool without parent metadata", () => {
  const detail = projectRunActivity([
    event(1, "tool.started", {
      toolCallId: "search",
      toolName: "semantic_scholar_search",
      arguments: { query: "GShard scaling laws" },
    }),
    event(2, "run.cancelled", {}),
  ]);

  assert.equal(detail.steps[0].tools[0].status, "interrupted");
  assert.equal(detail.currentStatus, undefined);
});

test("reported blockers surface as a readable activity line", () => {
  // v2.1：report_blocker 是新架构的一等机制（节点只报事实+证据+需求）。
  // 此前平台既不投影事件、UI 也没有展示 —— 节点报了阻塞，用户只看到 run
  // 莫名 incomplete，毫无线索（2026-08-07 E2E 实测：postprocess 报了
  // missing_input 且说得很具体，调用方没消费、用户也看不见）。
  const detail = projectRunActivity([
    { kind: "run.started", payload: { nodeType: "project_chat" }, source: {}, sequence: 1 },
    {
      kind: "run.blocked",
      payload: {
        reportingNode: "postprocess",
        category: "missing_input",
        summary: "节点输入缺少 visual_requests 字段",
      },
      source: {},
      sequence: 2,
    },
  ] as never);
  const text = JSON.stringify(detail);
  assert.match(text, /postprocess is blocked/);
  assert.match(text, /visual_requests/);
});

test("a workspace change carries its diff so the inline card can open", () => {
  const patch = [
    "diff --git a/writing/report.tex b/writing/report.tex",
    "new file mode 100644",
    "--- /dev/null",
    "+++ b/writing/report.tex",
    "@@ -0,0 +1,2 @@",
    "+\\documentclass{article}",
    "+\\begin{document}",
  ].join("\n");
  const detail = projectRunActivity([
    event(1, "workspace.changed", {
      tool: "write_file",
      filesChanged: 1,
      additions: 2,
      deletions: 0,
      files: [{ path: "paper/report.tex", additions: 2, deletions: 0, status: "added" }],
      patch,
      patchTruncated: false,
    }),
  ]);

  const change = detail.workspaceChanges[0];
  assert.equal(change.patch, patch);
  assert.equal(change.patchTruncated, false);
  assert.equal(change.files[0].status, "added");
});

test("a pre-fix workspace event reports no diff instead of an empty one", () => {
  // 修复前落库的事件没有 patch。卡片据此决定"不画那个展开箭头" —— 画了
  // 点开是空的，比不给展开更糟。
  const detail = projectRunActivity([
    event(1, "workspace.changed", {
      tool: "write_file",
      filesChanged: 1,
      additions: 5,
      deletions: 0,
      files: [{ path: "paper/draft.md" }],
    }),
  ]);

  const change = detail.workspaceChanges[0];
  assert.equal(change.patch, undefined);
  assert.equal(change.patchTruncated, false);
  assert.equal(change.files[0].status, undefined);
});

test("a blank patch string counts as no diff, not as an openable card", () => {
  const detail = projectRunActivity([
    event(1, "workspace.changed", {
      tool: "write_file",
      filesChanged: 1,
      additions: 0,
      deletions: 0,
      files: [{ path: "paper/draft.md" }],
      patch: "   \n  ",
      patchTruncated: true,
    }),
  ]);

  assert.equal(detail.workspaceChanges[0].patch, undefined);
});

test("停靠中说清楚还要等多久、以及一句话能叫醒它", () => {
  // 8-21 现场：unattended 停靠在 4 小时的复查点上，用户问「怎么样了？」，
  // 读到「平台在记录这次运行时撞上了内部错误」—— 不在记录阶段、不是内部
  // 错误、也不是重发能解决的。真实局面 worker 全都自报了（RFC D10）。
  const until = Math.round(Date.now() / 1000) + 3 * 3600;
  const parked = [
    event(1, "run.started", { nodeType: "orchestrator" }),
    event(2, "run.parked", { untilEpoch: until, delaySeconds: 10800, why: "unattended_recheck_backoff" }),
  ];
  const line = projectRunActivityRaw(parked, undefined, "zh").currentStatus ?? "";
  assert.match(line, /停靠中/);
  assert.match(line, /约 3 小时后复查/, "不给时间尺度的「稍等」比不说更糟");
  assert.match(line, /发一句话可立即唤醒/, "D12 已经能叫醒它了，用户该知道有这个选项");
  // 同一个局面在英文界面里也要说全这三件事 —— 只有一种语言写全的话，
  // 另一种语言的用户读到的是残缺的局面。
  const english = projectRunActivityRaw(parked, undefined, "en").currentStatus ?? "";
  assert.match(english, /Parked/);
  assert.match(english, /about 3h/);
  assert.match(english, /wake it now/);
});

test("平台的回执留在事实流里，锚回它回应的那条消息", () => {
  // 断流即失的 progress 帧换成持久事件（RFC P1）：刷新之后，「人说了什么」
  // 和「平台答了什么」都还在，而不是只剩一句没有回音的话。
  // 这条断言的是**中文**那一份文案，所以显式说中文（本文件默认绑了英文）。
  const detail = projectRunActivityRaw([
    event(1, "run.started", { nodeType: "orchestrator" }),
    event(2, "interject.queued", {
      messageId: "msg-7", delivery: "queued_for_resume", occupancy: "occupied",
    }),
  ], undefined, "zh");
  const receipt = detail.said.find((s) => s.repliesToMessageId === "msg-7");
  assert.ok(receipt, "回执没锚回那条消息 —— 事后答不出它在回应谁");
  assert.match(receipt!.text, /已收下/);
  assert.match(receipt!.text, /上一轮还占着/, "占用状态要如实说，别只说「已收下」");
});

test("「收下了」和「被读到了」是两件事，不许合并", () => {
  // 合成一个的话，"入队黑洞"（收下了但永远没人取）就再也看不出来 ——
  // 而那正是 D10 可寻址那一维要人看见的东西。
  const detail = projectRunActivityRaw([
    event(1, "run.started", { nodeType: "orchestrator" }),
    event(2, "interject.queued", { messageId: "m", delivery: "queued_for_resume" }),
    event(3, "interrupt.acknowledged", { repliesToMessageId: "m", deferred: false }),
  ], undefined, "zh");
  const both = detail.said.filter((s) => s.repliesToMessageId === "m");
  assert.equal(both.length, 2, "两件事被合成了一条");
  assert.ok(both.some((s) => s.receipt !== undefined), "worker 那条回执丢了");
  assert.ok(both.some((s) => /已收下/.test(s.text)), "平台那条回执丢了");
});

test("没有专用锚字段的事件：提交号留在 submissionId，不冒充消息锚", () => {
  // 曾经把 submissionId 退回成 repliesToMessageId —— 「这一趟」被当成「回答谁」。
  // 2026-09-02 实测：一轮末尾的派发线只带提交号，被当回复回声滤出 run 窗口，
  // 折在它尾巴上的子节点卡随之消失（见 a-dispatch-inside-a-turn-keeps-its-child）。
  // 现在两件事各归各名；"让位给消息槽位"由 withoutMessageAnchoredSaids 按语义判。
  const detail = projectRunActivity([
    event(1, "run.started", { nodeType: "orchestrator" }),
    event(2, "orchestrator.said", { text: "我先看一下预注册", submissionId: "turn-9" }),
  ]);
  const said = detail.said.find((s) => s.text.includes("预注册"));
  assert.equal(said?.submissionId, "turn-9", "这一趟的归属要留着");
  assert.equal(said?.repliesToMessageId ?? "", "", "提交号不是消息锚");
});

test("专用锚字段优先于 submissionId —— 它更精确", () => {
  const detail = projectRunActivity([
    event(1, "run.started", { nodeType: "orchestrator" }),
    event(2, "orchestrator.said", {
      text: "答复", repliesToMessageId: "msg-1", submissionId: "turn-9",
    }),
  ]);
  assert.equal(detail.said.find((s) => s.text === "答复")?.repliesToMessageId, "msg-1");
});

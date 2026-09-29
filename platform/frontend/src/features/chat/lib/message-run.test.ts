import test from "node:test";
import assert from "node:assert/strict";
import { latestMessageRun, messageRunOwners } from "./message-run.ts";

test("latest Run ownership comes from the latest explicit message.runId", () => {
  assert.deepEqual(latestMessageRun([
    { id: "assistant-old", role: "assistant", text: "Earlier result", runId: "run-old" },
    { id: "user-new", role: "user", text: "Try again", runId: "run-new" },
  ]), { messageId: "user-new", runId: "run-new" });
});

test("a durable system failure can own the canonical Run row", () => {
  assert.deepEqual(latestMessageRun([
    { id: "user", role: "user", text: "Run it", runId: "run-failed" },
    { id: "failure", role: "system", text: "Execution failed", runId: "run-failed" },
  ]), { messageId: "failure", runId: "run-failed" });
});

test("legacy messages without runId produce no invented execution row", () => {
  assert.equal(latestMessageRun([
    { id: "user", role: "user", text: "Legacy request" },
    { id: "assistant", role: "assistant", text: "Legacy answer", runId: null },
  ]), null);
});

test("every distinct Run is owned by its final attributable message", () => {
  assert.deepEqual(messageRunOwners([
    { id: "run-a-user", role: "user", text: "First", runId: "run-a" },
    { id: "run-a-assistant", role: "assistant", text: "Done", runId: "run-a" },
    { id: "run-b-user", role: "user", text: "Second", runId: "run-b" },
    { id: "run-b-failure", role: "system", text: "Failed", runId: "run-b" },
    { id: "legacy", role: "assistant", text: "No owner", runId: null },
  ]), [
    { messageId: "run-a-assistant", runId: "run-a" },
    { messageId: "run-b-failure", runId: "run-b" },
  ]);
});

// ── messageRunSegments：时间穿插归属（2026-08-17）─────────────────────────────

import { messageRunSegments } from "./message-run.ts";

test("已经发生的活动排在插话上面，之后发生的排在下面", () => {
  // wangd 2026-08-18：「我现在输入之后，当前正在运行的这个内容还是会出现
  // 在我输入信息的直接的下方。按理说它应该是出现在上面才对。」
  //
  // 当时的修法是"挂载点只认开启这一轮的那条消息" —— 因为那会儿**窗口本身没有
  // 意义**：消息号和事件号来自两个各自从 0 开始的计数器，切出来的区间是垃圾。
  //
  // 序号统一之后（后端 sessions.next_sequence），窗口重新可信，而它本来就答得
  // 了这个诉求，答得还更准：sequence 小于插话的活动落在更早那段窗口（渲染在
  // 插话上面），之后发生的落在后一段（渲染在下面 —— 它确实是后来才发生的）。
  const segments = messageRunSegments([
    { id: "u1", role: "user", text: "继续吧", sequence: 10 },
    { id: "a1", role: "assistant", text: "第 1 轮", runId: "run-a", sequence: 20 },
    { id: "u2", role: "user", text: "现在跑的怎么样了?", runId: "run-a", sequence: 80 },
  ]);
  const inWindow = (sequence: number, w: { start: number; end: number | null }) =>
    sequence > w.start && (w.end === null || sequence < w.end);
  const slotOf = (sequence: number) =>
    segments.find((segment) => inWindow(sequence, segment.window))?.messageId;

  // 插话之前发生的那条动作：渲染在插话上面
  assert.equal(slotOf(50), "a1");
  // 插话之后发生的：渲染在插话下面，因为它确实发生在之后
  assert.equal(slotOf(90), "u2");
  // run 在第一句话之前干的活落在前导 user 消息名下
  assert.equal(slotOf(15), "u1");
  // 每条消息各自有家，没有谁吞掉别人的窗口
  assert.deepEqual(segments.map((segment) => segment.messageId), ["u1", "a1", "u2"]);
});

test("真数据形状：一条 run 名下多条消息，活动落在正确的两条之间", () => {
  // 取自会话 e46448f0（迁移回填之后的真实序号）。同一条 run 名下四条消息，
  // 中间夹着几百条活动 —— 这正是"显示的顺序不是按时间来的"那个现场：
  // 只认第一条挂载点时，13:32 的动作会画在 13:17 的对话上面。
  const segments = messageRunSegments([
    { id: "m2", role: "user", text: "你帮我研究一下…", runId: "run-cedea504", sequence: 2 },
    { id: "m66", role: "assistant", text: "我发现 KB 里…", runId: "run-cedea504", sequence: 66 },
    { id: "m68", role: "user", text: "从头完整重做", runId: "run-cedea504", sequence: 68 },
    { id: "m430", role: "assistant", text: "Post-node decision…", runId: "run-cedea504", sequence: 430 },
    { id: "m433", role: "user", text: "PROCEED to next stage", runId: "run-378b00c9", sequence: 433 },
  ]);
  const inWindow = (sequence: number, w: { start: number; end: number | null }) =>
    sequence > w.start && (w.end === null || sequence < w.end);
  const slotOf = (sequence: number, runId: string) =>
    segments.find(
      (segment) => segment.runId === runId && inWindow(sequence, segment.window),
    )?.messageId;

  // hypothesis 那一整段（事件 110–319）发生在"从头完整重做"之后、决策卡之前
  assert.equal(slotOf(110, "run-cedea504"), "m68");
  assert.equal(slotOf(319, "run-cedea504"), "m68");
  // 开题那句话（事件 8）在第一条用户消息与第一句回复之间
  assert.equal(slotOf(8, "run-cedea504"), "m2");
  // 下一轮的活动归下一条 run，且在它自己的消息之后
  assert.equal(slotOf(500, "run-378b00c9"), "m433");
});

test("two runs interleave without stealing each other's windows", () => {
  const segments = messageRunSegments([
    { id: "u1", role: "user", text: "First", sequence: 10 },
    { id: "a1", role: "assistant", text: "Done", runId: "run-a", sequence: 30 },
    { id: "u2", role: "user", text: "Second", sequence: 100 },
    { id: "a2", role: "assistant", text: "Done", runId: "run-b", sequence: 130 },
  ]);
  const byRun = Object.groupBy(segments, (segment) => segment.runId);
  assert.deepEqual(byRun["run-a"], [
    { messageId: "u1", runId: "run-a", window: { start: 10, end: 30 }, interjection: false },
    { messageId: "a1", runId: "run-a", window: { start: 30, end: null }, interjection: false },
  ]);
  assert.deepEqual(byRun["run-b"], [
    { messageId: "u2", runId: "run-b", window: { start: 100, end: 130 }, interjection: false },
    { messageId: "a2", runId: "run-b", window: { start: 130, end: null }, interjection: false },
  ]);
});

test("optimistic messages without sequence anchor to the durable tail", () => {
  const segments = messageRunSegments([
    { id: "a0", role: "assistant", text: "Old", runId: "run-old", sequence: 40 },
    { id: "u-opt", role: "user", text: "New question" },
    { id: "a-opt", role: "assistant", text: "", runId: "run-live" },
  ]);
  const live = segments.filter((segment) => segment.runId === "run-live");
  // 只有一个尾窗实例：从最后一条持久消息之后开始，开放到 ∞。
  assert.deepEqual(live, [
    { messageId: "a-opt", runId: "run-live", window: { start: 40, end: null }, interjection: false },
  ]);
});

test("a leading window is not invented when the preceding message owns a run", () => {
  const segments = messageRunSegments([
    { id: "a1", role: "assistant", text: "Done", runId: "run-a", sequence: 30 },
    { id: "a2", role: "assistant", text: "Next", runId: "run-b", sequence: 50 },
  ]);
  const runB = segments.filter((segment) => segment.runId === "run-b");
  assert.deepEqual(runB, [
    { messageId: "a2", runId: "run-b", window: { start: 50, end: null }, interjection: false },
  ]);
});

test("插话消息是挂载点，且被标成 interjection；开启 run 的那条不是", () => {
  const messages = [
    { id: "m-start", role: "user", content: "开题", sequence: 1400, runId: "run-a" },
    { id: "m-reply", role: "assistant", content: "好", sequence: 1401 },
    { id: "m-interject", role: "user", content: "插一句", sequence: 1691, runId: "run-a" },
  ] as unknown as Parameters<typeof messageRunSegments>[0];
  const segments = messageRunSegments(messages);
  const byMessage = new Map(segments.map((segment) => [segment.messageId, segment]));
  assert.equal(byMessage.get("m-start")?.interjection, false, "开启 run 的消息不是插话");
  assert.equal(byMessage.get("m-interject")?.interjection, true, "插话仍是挂载点，且要被标出来");
  // 挂载点身份没被拿掉：插话之后的活动落在它自己的窗口里
  assert.equal(byMessage.get("m-interject")?.window.start, 1691);
  assert.equal(byMessage.get("m-interject")?.window.end, null);
});

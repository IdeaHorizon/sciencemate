import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { projectRunActivity as projectRunActivityRaw } from "./run-activity-detail.ts";

// 下面的判据断言的是**英文**那一份文案，所以这里显式说英文 —— 默认语言已经
// 是中文了。换成默认值就等于把「这些句子长什么样」这件事交给默认语言，
// 判据会跟着默认值一起漂。
const projectRunActivity = (events: readonly ExecutionEvent[], parentInterruptedHint?: boolean) =>
  projectRunActivityRaw(events, parentInterruptedHint, "en");
import {
  buildRunTimeline,
  conversationOnly,
  saidsAnchoredTo,
  withoutMessageAnchoredSaids,
  withoutReplyEcho,
} from "./run-activity-timeline.ts";
import type { ExecutionEvent } from "./execution-event.ts";

/**
 * 插话的答复必须恰好有一个家（#766）。
 *
 * 2026-09-02 实拍（课题二，会话 c7168ec6）：用户插话（seq 1691），调度器的答复
 * （orchestrator.said 1694，真锚 repliesToMessageId=09fdb0a5-…）和回执
 * （interrupt.acknowledged 1693）主聊天区哪里都不渲染，只有右栏历史能翻到。
 *
 * 机制：插话消息带 runId → 它是挂载点（messageRunSegments 有意如此，之后的
 * 活动才渲染在它下面）→ 走窗口路径；窗口路径里锚到消息的 said 由
 * withoutMessageAnchoredSaids 让位，假定"那条消息的槽位会画它"；而那个槽位
 * 只对**没有 segment** 的用户消息生效 —— 带 runId 的消息必然有 segment，
 * 槽位永远走不到。让位做了、接住没做。
 *
 * 修法不是取消挂载（那会把 08-23 修好的"活动画在插话上面"请回来），是让插话
 * 的窗口实例自己接住锚到它的答复。fixture 是那一页真实事件（PR #764 的那份）。
 */

const ROOT = "run_3f0d5a99baa9461dbcd71b7d88a565b7";
const INTERJECT_MESSAGE = "09fdb0a5-cc93-447c-8f61-8dd45330ccd6";

function realEvents(): ExecutionEvent[] {
  const url = new URL("./fixtures/dispatch-line-inside-a-turn.json", import.meta.url);
  return JSON.parse(readFileSync(url, "utf8")) as ExecutionEvent[];
}

test("真数据：锚到插话的答复被窗口让位，且被槽位接住 —— 同一条规则的两面", () => {
  const activity = projectRunActivity(realEvents(), undefined);
  const window = withoutMessageAnchoredSaids(
    conversationOnly(withoutReplyEcho(buildRunTimeline(activity, ROOT), undefined)),
  );
  // 让位：窗口里没有它
  assert.equal(
    window.some((item) => item.kind === "said" && item.said.repliesToMessageId === INTERJECT_MESSAGE),
    false,
    "锚到插话的答复不该留在 run 窗口里（会画在提问上面）",
  );
  // 接住：槽位里有它，而且是那句真答复
  const { replies, receipt } = saidsAnchoredTo(activity.said, INTERJECT_MESSAGE);
  // 真数据里锚到这条插话的非回执 said 有两条：1692 平台的"已收下"旁注、1694 答复
  assert.deepEqual(replies.map((item) => item.sequence), [1692, 1694]);
  assert.ok(replies.every((item) => item.repliesToMessageId === INTERJECT_MESSAGE));
  assert.match(replies.at(-1)!.text, /收到，这条插得对/);
  // 已经有人开口 → 回执（1693）让位，不再显示"正在处理"
  assert.equal(receipt, undefined);
});

test("还没人答复时，回执是占位；答复一到就让位", () => {
  // 拿掉两条非回执的 said（1692 旁注、1694 答复），只剩 1693 那条回执
  const activity = projectRunActivity(
    realEvents().filter((event) => event.sequence !== 1694 && event.sequence !== 1692),
    undefined,
  );
  const { replies, receipt } = saidsAnchoredTo(activity.said, INTERJECT_MESSAGE);
  assert.equal(replies.length, 0);
  assert.ok(receipt?.receipt, "1693 的送达回执必须被选中当占位");
  assert.equal(receipt?.sequence, 1693);
});

test("不相干的消息 id 一无所获 —— 判据是精确匹配，不是猜", () => {
  const activity = projectRunActivity(realEvents(), undefined);
  const { replies, receipt } = saidsAnchoredTo(activity.said, "turn-does-not-exist");
  assert.deepEqual(replies, []);
  assert.equal(receipt, undefined);
});

test("接线：插话的窗口实例带 interjectionMessageId，旧的死槽位分支已删", () => {
  const workspace = readFileSync(
    new URL("../../sessions/components/SessionWorkspace.tsx", import.meta.url), "utf8");
  assert.match(
    workspace,
    /interjectionMessageId=\{segment\.interjection \? message\.id : undefined\}/,
    "窗口实例没把 interjectionMessageId 传下去",
  );
  assert.doesNotMatch(
    workspace,
    /插话消息不是 run 的挂载点/,
    "那条永远走不到的槽位分支还在（它的前提与 messageRunSegments 矛盾）",
  );
  const component = readFileSync(
    new URL("../../chat/components/CanonicalRunActivity.tsx", import.meta.url), "utf8");
  assert.match(component, /saidsAnchoredTo\(/, "组件没接上 saidsAnchoredTo");
  // 窗口路径的主返回：<div className="chat-transcript-run"> 换行后第一项就是答复块。
  // 独立槽位那行是 `>{anchoredBlock}</div>`（不换行），不会误匹配 —— 变异测试
  // 第一版正是被它蒙混过关的。
  assert.match(
    component,
    /<div className="chat-transcript-run">\s*\n\s*\{anchoredBlock\}/,
    "窗口路径没有把锚到消息的答复块画在活动前面",
  );
});

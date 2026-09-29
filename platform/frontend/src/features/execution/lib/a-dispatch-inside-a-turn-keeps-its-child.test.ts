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
  withoutMessageAnchoredSaids,
  withoutReplyEcho,
} from "./run-activity-timeline.ts";
import type { ExecutionEvent } from "./execution-event.ts";

/**
 * 一轮之内派出去的子节点，主聊天区必须看得见。
 *
 * 2026-09-02 实拍（课题二，会话 c7168ec6，父 run run_3f0d…）：调度器在一轮末尾
 * 说「启动实验阶段第一个 run：Q1-POWER 功效标定…」并派出 experiment；
 * 这句派发线带 submissionId=turn-…（它是该轮的最后一个动作），`repliesToMessageId`
 * 退回提交号后被当成"回复回声"从 run 窗口滤掉，而 experiment 子节点卡已经
 * 折在它尾巴上 —— 一起消失。结果：一个跑了一小时、七十多个动作的子节点，
 * 主聊天区一张卡都没有；右栏「研究进程」却好好的（同一份事件、同一个投影）。
 *
 * fixture 是那一页真实事件（seq 1421–1470 + 插话回执/答复 1691–1694），
 * 不是手造样本：第一次派发（literature，不带 submissionId）能渲染、这一次
 * 不能，差别就藏在真数据的字段里。
 */

const ROOT = "run_3f0d5a99baa9461dbcd71b7d88a565b7";
const EXPERIMENT = `${ROOT}::_orchestrator->experiment@d1`;
const INTERJECT_MESSAGE = "09fdb0a5-cc93-447c-8f61-8dd45330ccd6";

function realEvents(): ExecutionEvent[] {
  const url = new URL("./fixtures/dispatch-line-inside-a-turn.json", import.meta.url);
  return JSON.parse(readFileSync(url, "utf8")) as ExecutionEvent[];
}

function timelineAsTheChatSeesIt(events: ExecutionEvent[]) {
  const activity = projectRunActivity(events, undefined);
  // 与 CanonicalRunActivity 的链一致（窗口是尾窗，不过滤 sequence）。
  return {
    activity,
    beforeAnchorFilter: conversationOnly(withoutReplyEcho(buildRunTimeline(activity, ROOT), undefined)),
    shown: withoutMessageAnchoredSaids(
      conversationOnly(withoutReplyEcho(buildRunTimeline(activity, ROOT), undefined)),
    ),
  };
}

test("提交号不是消息锚：只带 submissionId 的派发线没有 repliesToMessageId", () => {
  const { activity } = timelineAsTheChatSeesIt(realEvents());
  const dispatch = activity.said?.find((said) => said.sequence === 1440);
  assert.ok(dispatch, "真事件页里 seq 1440 就是那句派发线");
  assert.equal(dispatch.aboutNodeType, "experiment");
  assert.equal(dispatch.submissionId, "turn-c2d87403ab7544849f50c6301d7b3ba7", "这一趟的归属要留着");
  assert.equal(dispatch.repliesToMessageId ?? "", "", "提交号被当成了消息锚 —— 这句话会被当回声滤掉");
});

test("一轮之内派出去的子节点，主聊天区仍然看得见（折在派发线尾巴上）", () => {
  const { shown } = timelineAsTheChatSeesIt(realEvents());
  const dispatch = shown.find((item) => item.kind === "said" && item.said.aboutNodeType === "experiment");
  assert.ok(dispatch && dispatch.kind === "said", "派发线从 run 窗口消失了");
  assert.equal(dispatch.child?.runId, EXPERIMENT, "子节点卡没折在派发线上");
  assert.equal(dispatch.child?.status, "running");
  assert.ok((dispatch.child?.tools.length ?? 0) > 0, "卡上该有它已经做过的动作计数");
});

test("真正锚到插话消息的答复/回执仍然让位给那条消息的槽位（不在 run 窗口里画第二遍）", () => {
  const { beforeAnchorFilter, shown } = timelineAsTheChatSeesIt(realEvents());
  const anchored = beforeAnchorFilter.filter(
    (item) => item.kind === "said" && item.said.repliesToMessageId === INTERJECT_MESSAGE,
  );
  assert.ok(anchored.length >= 2, "fixture 里有真锚的回执 + 答复（seq 1693/1694）");
  for (const item of anchored) {
    assert.ok(!shown.includes(item), "真锚的 said 不该留在 run 窗口");
  }
});

test("插话的「已收下」与送达回执都用消息 id 锚定，不靠提交号退回", () => {
  const { activity } = timelineAsTheChatSeesIt(realEvents());
  // seq 1692 interject.queued：只带 messageId（没有 submissionId）—— 去掉退回后它必须仍有锚
  const queued = activity.said?.find((said) => said.sequence === 1692);
  assert.ok(queued, "seq 1692 是 interject.queued 的「已收下」");
  assert.equal(queued.repliesToMessageId, INTERJECT_MESSAGE);
  assert.ok(queued.text.length > 0);
  // seq 1693 interrupt.acknowledged：带真 repliesToMessageId 的送达回执
  const receipt = activity.said?.find((said) => said.sequence === 1693);
  assert.ok(receipt?.receipt, "seq 1693 是送达回执（receipt）");
  assert.equal(receipt.repliesToMessageId, INTERJECT_MESSAGE);
});

test("组件真的走这条纯函数（弱守卫：源码里必须调用它 —— 真证据是浏览器里那张卡）", () => {
  const url = new URL("../../chat/components/CanonicalRunActivity.tsx", import.meta.url);
  const source = readFileSync(url, "utf8");
  assert.match(source, /withoutMessageAnchoredSaids\(/, "组件没接上 withoutMessageAnchoredSaids");
  assert.doesNotMatch(source, /\.filter\(\(item\) => item\.kind !== "said" \|\| !item\.said\.repliesToMessageId\)/,
    "组件里不该再留一份内联判据（两份就会各自演化）");
});

test("只带提交号的纯答复文字仍然让位给消息侧（它就是这一趟的回复正文）", () => {
  // 老数据没有专用锚字段：这条让位规则不能因为派发线被救回来而一起失效。
  const plainReply = {
    kind: "said" as const,
    sequence: 5,
    said: { id: "s5", sequence: 5, text: "答复正文", aboutNodeType: "", submissionId: "turn-9" },
  };
  const dispatch = {
    kind: "said" as const,
    sequence: 6,
    said: { id: "s6", sequence: 6, text: "我派 experiment 去跑", aboutNodeType: "experiment", submissionId: "turn-9" },
  };
  const kept = withoutMessageAnchoredSaids([plainReply, dispatch]);
  assert.deepEqual(kept.map((item) => item.sequence), [6], "纯答复让位、派发线留下");
});

import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  answerBubbleText,
  buildChatRequest,
  composeAnswer,
  composeText,
  type Answer,
} from "./answer.ts";

/**
 * 前端唯一的答复构造函数，与后端钉在同一批 fixture 上。
 *
 * `accepted/*.json` 里每一份请求体都由这里用**生产同一条路**
 * （composeAnswer / composeText → buildChatRequest）造出来并断言逐字段相等；
 * 后端 `tests/test_chat_request_fixtures.py` 把同一批文件喂进真实 Pydantic 模型。
 * 任何一边改了形状而另一边没跟上，两个测试里必有一个红。
 */
const FIXTURES = new URL("../../../../../contracts/fixtures/chat-request/", import.meta.url).pathname;

function fixture(name: string) {
  return JSON.parse(readFileSync(join(FIXTURES, "accepted", name), "utf8"));
}

const OFFER = "1788332859-ecd718:pecb8eac6:o3b1cc632";
const SESSION = "de4632cc-8a94-4c02-abb3-6a51ca48c155";
const REVISE = { id: "revise", choiceId: "revise", label: "REVISE (re-run source_node with reviewer feedback)", value: "REVISE (re-run source_node with reviewer feedback)" };

test("cuib 09-03 那一次：只点选项不写附言 —— 是一次完整的提交", () => {
  const answer = composeAnswer({ selected: REVISE, note: "", offerId: OFFER });
  assert.ok(answer, "选了带身份的选项就有东西可发，附言不是必填");
  assert.deepEqual(
    buildChatRequest(answer, { history: [], conversationId: SESSION }),
    fixture("choice-without-note.json"),
  );
});

test("选项 + 附言：身份走 choice_id，理由走 note", () => {
  const answer = composeAnswer({
    selected: REVISE,
    note: "  请补一段对小网格分辨率的敏感性分析，再回头修假设。 ",
    offerId: OFFER,
  });
  assert.ok(answer);
  assert.deepEqual(
    buildChatRequest(answer, { history: [], conversationId: SESSION }),
    fixture("choice-with-note.json"),
  );
});

test("呈递没有 offer_id 时如实发 null，不编", () => {
  const answer = composeAnswer({
    selected: { id: "proceed", choiceId: "proceed", label: "PROCEED", value: "PROCEED" },
    note: "",
    offerId: undefined,
  });
  assert.ok(answer);
  assert.deepEqual(
    buildChatRequest(answer, { history: [], conversationId: SESSION }),
    fixture("choice-without-offer-id.json"),
  );
});

test("输入框那条路：一句话开新会话", () => {
  const answer = composeText("请帮我进行一个气象模拟领域最新方向的简单研究，模拟对象选择一个体量较小的网格即可");
  assert.ok(answer);
  assert.deepEqual(
    buildChatRequest(answer, { history: [], conversationId: null }),
    fixture("text-new-session.json"),
  );
});

test("带历史的插话", () => {
  const answer = composeText("跑的怎么样了？ ");
  assert.ok(answer);
  assert.deepEqual(
    buildChatRequest(answer, {
      history: [
        { role: "user", content: "开始研究" },
        { role: "assistant", content: "已开始。" },
      ],
      conversationId: SESSION,
    }),
    fixture("text-with-history.json"),
  );
});

test("没有东西可发 = 构造失败（null），不是一个会走到 send 的答复", () => {
  assert.equal(composeText(""), null);
  assert.equal(composeText("   \n\t"), null);
  assert.equal(composeAnswer({ selected: null, note: "   ", offerId: OFFER }), null);
});

test("没有身份的老式选项：文案就是答复本体，附言让位", () => {
  const legacy = { id: "option-1", label: "RETRY REVIEWER (…)", value: "RETRY REVIEWER (…)" };
  const answer = composeAnswer({ selected: legacy, note: "ignored", offerId: undefined });
  assert.deepEqual(answer, { kind: "text", text: "RETRY REVIEWER (…)" });
});

test("每一份 accepted fixture 都能由本文件的构造函数产出（没有前端造不出的合法形状）", () => {
  const produced = new Set<string>();
  const record = (answer: Answer, conversationId: string | null, history: { role: "user" | "assistant"; content: string }[] = []) =>
    produced.add(JSON.stringify(buildChatRequest(answer, { history, conversationId })));
  record(composeAnswer({ selected: REVISE, note: "", offerId: OFFER })!, SESSION);
  record(composeAnswer({ selected: REVISE, note: "请补一段对小网格分辨率的敏感性分析，再回头修假设。", offerId: OFFER })!, SESSION);
  record(composeAnswer({ selected: { id: "proceed", choiceId: "proceed", label: "PROCEED", value: "PROCEED" }, note: "", offerId: null })!, SESSION);
  record(composeText("请帮我进行一个气象模拟领域最新方向的简单研究，模拟对象选择一个体量较小的网格即可")!, null);
  record(composeText("跑的怎么样了？")!, SESSION, [
    { role: "user", content: "开始研究" },
    { role: "assistant", content: "已开始。" },
  ]);
  for (const name of readdirSync(join(FIXTURES, "accepted"))) {
    const body = JSON.stringify(fixture(name));
    assert.ok(produced.has(body), `${name} 不是前端构造函数能产出的形状 —— 要么补构造路径，要么这份 fixture 是手编的`);
  }
});

test("气泡文字：选项答复显示人看到的那行字，不显示裸 id", () => {
  const answer = composeAnswer({ selected: REVISE, note: "理由", offerId: OFFER })!;
  assert.equal(answerBubbleText(answer), `${REVISE.label}\n理由`);
  assert.equal(answerBubbleText(composeText("你好")!), "你好");
});

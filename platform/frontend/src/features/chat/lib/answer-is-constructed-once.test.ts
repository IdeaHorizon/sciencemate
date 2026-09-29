import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

/**
 * 扫盘闸：「这次提交是什么」整个前端只在 answer.ts 里回答一次。
 *
 * ## 这道闸防的是哪一族事故
 *
 * 2026-09-03（cuib，会话 de4632cc）：只点选项不写附言，点 Submit 没反应。
 * 从卡片到 HTTP 请求经过五层交接，每一层都拿自己看得见的碎片再判一遍
 * "有没有东西可发"：卡片看 choice+附言、workspace 看 `(!text && !choice)`、
 * hook 只看 `text.trim()`、后端 schema 只看 `message` 的 min_length。8-31 改了
 * 前两道，后两道原样 —— hook 里一句 `return;` 把这次点击静默吞掉。同形事故
 * 此前已发生三次（8-19 选项身份 / 8-21 占用 / 9-01 答复入口）。
 *
 * 修法不是把四道闸改成一致，是让**没有第二道闸可写**：
 *   1. 值以 `Answer` 和类型过界，`text` 分支是品牌类型 NonEmptyText；
 *   2. 线格式只在 answer.ts 组装，目标类型从后端模型生成；
 *   3. 命令处理函数返回类型非 void，裸 `return;` 是编译错误。
 *
 * 类型系统管住了 1 和 3 的大半；这里扫的是类型管不到的那几处：
 * 有没有人在别处又开始判空、又开始手写请求体、又开始手写线类型。
 */
const SRC = new URL("../../../", import.meta.url).pathname;

function read(rel: string) {
  return readFileSync(join(SRC, rel), "utf8");
}

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) yield full;
  }
}

const ANSWER_MODULE = "features/chat/lib/answer.ts";
const SEND_PATH = [
  "features/chat/components/HumanInputPrompt.tsx",
  "features/chat/hooks/useChat.ts",
  "features/sessions/components/SessionWorkspace.tsx",
];

/** 去掉注释：注释里当然可以提事故里的那些名字，扫的是代码。 */
function code(src: string) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

/**
 * 提交路径上允许存在的 `.trim()` —— **默认拒绝，例外逐条登记**。
 *
 * 每一条都不在"要发出去的那段文字"上：标题改名、消息列表过滤、事件标签
 * 探测。要加新例外就得来这里写一行并说清它不是在判提交内容 —— 这正是
 * 名单式护栏与默认拒绝的区别：新写的 `.trim()` 默认是红的。
 */
const TRIM_EXCEPTIONS: Record<string, string[]> = {
  "features/chat/hooks/useChat.ts": [
    'typeof value === "string" && value.trim()',   // activityFromEvent：事件标签探测
  ],
  "features/sessions/components/SessionWorkspace.tsx": [
    "const next = title.trim();",                   // 会话改名
    "message.runId?.trim() && message.text.trim()", // 回复文本索引：过滤消息列表
  ],
  "features/chat/components/HumanInputPrompt.tsx": [],
};

test("提交路径上的三层不再自己判「空不空」", () => {
  for (const rel of SEND_PATH) {
    let src = code(read(rel));
    for (const allowed of TRIM_EXCEPTIONS[rel] ?? []) {
      assert.ok(src.includes(allowed), `${rel} 的例外「${allowed}」已经不存在了，把它从名单里删掉`);
      src = src.split(allowed).join("");
    }
    // 剩下的任何 .trim()，以及对提交文本本身的 .length 判空 / `!x` 守卫，
    // 都是"我在重新判断有没有内容"。（options.length 之类不是提交文本，不管。）
    const hit = src.match(
      /\.trim\(\)|\b(text|draft|answer|note|response)\.length\b|if \(!(text|draft|answer|note|response)\b/,
    );
    assert.equal(hit, null, `${rel} 里又在判空：${hit?.[0]} —— 判空只许在 ${ANSWER_MODULE}`);
    assert.ok(!/answeringRef/.test(src), `${rel} 里又长出了第二个"在飞"标志`);
  }
  assert.match(code(read(ANSWER_MODULE)), /\.trim\(\)/, "构造函数自己当然要 trim —— 这是唯一的一处");
});

test("请求体只在 answer.ts 组装，只由 useChat 发出", () => {
  const builders: string[] = [];
  const senders: string[] = [];
  for (const file of walk(SRC)) {
    const src = readFileSync(file, "utf8");
    const rel = file.slice(SRC.length);
    if (/buildChatRequest\(/.test(src) && rel !== ANSWER_MODULE) builders.push(rel);
    if (/api\.stream(Project|Global)Chat\(/.test(src)) senders.push(rel);
    // 手写线字段 = 又一份契约抄件。
    if (rel !== ANSWER_MODULE && rel !== "lib/generated/chat-request.ts") {
      assert.ok(!/conversation_id:\s*[^,}]+,\s*\n?\s*(answer|message):/.test(src)
        && !/\banswer:\s*\{\s*kind:/.test(src),
        `${rel} 在手写 ChatRequest 的字段 —— 只许调 buildChatRequest`);
    }
  }
  assert.deepEqual(builders, ["features/chat/hooks/useChat.ts"]);
  assert.deepEqual(senders, ["features/chat/hooks/useChat.ts"]);
});

test("线类型是生成的，api.ts 不许再手写一份", () => {
  const api = read("lib/api.ts");
  assert.ok(!/export interface ChatRequest/.test(api), "api.ts 又长出了手写 ChatRequest");
  assert.match(api, /from "\.\/generated\/chat-request"/);
  const generated = read("lib/generated/chat-request.ts");
  assert.match(generated, /^\/\/ GENERATED FILE/, "生成文件必须自带身份标记");
  assert.match(generated, /export type Answer = ChoiceAnswer \| TextAnswer;/);
});

test("命令处理函数的返回类型非 void：沉默返回是编译错误，不是写法选项", () => {
  assert.match(read("features/chat/hooks/useChat.ts"), /\(answer: Answer\): SendDispatch =>/);
  assert.match(read("features/sessions/components/SessionWorkspace.tsx"),
    /const dispatch = async \(answer: Answer\): Promise<SubmitOutcome> =>/);
  assert.match(read("features/chat/components/HumanInputPrompt.tsx"),
    /onAnswer: \(answer: Answer\) => void;/);
  // 卡片把"能不能发"和"发什么"绑在同一个构造结果上：按钮亮着就一定有东西可发。
  assert.match(read("features/chat/components/HumanInputPrompt.tsx"),
    /const canContinue = interactive && answer !== null;/);
});

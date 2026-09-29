/**
 * **前端不许有自己的 run 状态词表，答复卡片不许有第二个渲染点。**
 *
 * ## 为什么是扫盘，不是名单
 *
 * 2026-08-27 把真相源换成了后端现算的 view，删掉了前端那 9 套手写状态集合。
 * 但 `ChatRunActivity` 的 prop 还叫 `status?: string | null`，判据还是
 * `status === "waiting_human" || …` —— 于是调用点开始各自把 view **翻译**
 * 成状态串。两个调用点、两份翻译，其中一份把 `phase`（值 `"alive"`）传了
 * 进去。`string` 是开放类型，编译器一声不吭；名单不匹配，整张待答卡片消失。
 *
 * 光把那一行改对，下一个人还会再写一次。所以判据是：**这些词在前端源码里
 * 就不该出现**。新增一个 run 状态、或者谁又想手写一套分类，落地即死，
 * 而不是等一次真实事故来照。
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const SRC = new URL("../../../", import.meta.url).pathname;

/**
 * run 状态**独有**的那几个词（后端 `models/execution.py`）。
 *
 * 只扫独有的：`running` / `queued` / `retrying` 这些在工具态、步骤态、算力
 * 作业态里也合法，扫它们会把一堆正当代码报成违规，而"噪音大的护栏会被关掉"
 * 本身就是一种失效。这几个词只可能出自 run 状态词表 —— 它们一旦出现在前端，
 * 就一定有人在手写一套本该由 view 回答的分类。
 *
 * 反过来，`view.phase === "alive"` / `view.outcome === "failed"` 这类**是**
 * 对的：那是 view 自己的封闭词表，TS 字面量联合钉着，加取值编译即红。
 */
const RUN_STATUS_ONLY_WORDS = [
  "waiting_human", "waiting_permission", "waiting_compute",
  "stale_unknown", "completed_with_warning", "dispatching",
];

/** 只留代码行。注释里出现这些词是**在解释为什么不再这么干**，那是资产不是违规。 */
function codeOnly(text: string): string {
  return text
    .split("\n")
    .filter((line) => {
      const trimmed = line.trimStart();
      return !trimmed.startsWith("*") && !trimmed.startsWith("//") && !trimmed.startsWith("/*");
    })
    .join("\n");
}

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules" || entry.startsWith(".")) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) sourceFiles(full, out);
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

test("前端源码里没有 run 状态词表", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(SRC)) {
    // fixtures 是"假装后端"的那一层，它按契约造后端会发的东西 —— 那是数据，
    // 不是判据。判据是"有没有人拿这些词做分支"。
    if (file.includes("/fixtures/")) continue;
    // **唯一的隔离区**：SSE 线协议上确实有一个 run 状态枚举（终帧的 `status`），
    // 总得有一个地方认得它。允许恰好一个文件知道，其余一律违规 —— 这是
    // 「一个问题一个真相源」，不是「零」。它只做协议层的终止判定，不做任何
    // 界面分类；要判界面，读 view。
    if (file.endsWith("features/execution/lib/run-event-stream.ts")) continue;
    const text = readFileSync(file, "utf8");
    for (const [n, line] of text.split("\n").entries()) {
      if (line.trimStart().startsWith("*") || line.trimStart().startsWith("//")) continue;
      for (const word of RUN_STATUS_ONLY_WORDS) {
        if (line.includes(`"${word}"`) || line.includes(`'${word}'`)) {
          offenders.push(`${file.slice(SRC.length)}:${n + 1}  ${line.trim().slice(0, 90)}`);
        }
      }
    }
  }
  assert.deepEqual(offenders, [], [
    "前端又长出 run 状态词表了。局面读 execution view 的 phase / waitingOn.kind /",
    "outcome —— 那是后端唯一那份分区里的取值，加新取值前端编译即红；",
    "手写字符串名单不会红，只会在某个真实会话上悄悄漏掉一支。",
  ].join("\n"));
});

test("能点的那张待答卡片全前端只构造一次", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(SRC)) {
    const text = readFileSync(file, "utf8");
    // 组件定义处不算，只数**用**它的地方。
    if (file.endsWith("HumanInputPrompt.tsx")) continue;
    if (file.endsWith("index.ts") && text.includes("export { HumanInputPrompt }")) continue;
    // 只数代码：注释里提到它是**在解释为什么这里不再渲染它**，那是资产。
    const uses = codeOnly(text).split("<HumanInputPrompt").length - 1;
    if (uses > 0) offenders.push(`${file.slice(SRC.length)} × ${uses}`);
  }
  assert.equal(offenders.length, 1, [
    `能点的那张卡出现在 ${offenders.length} 个文件里：${offenders.join(", ")}`,
    "两个渲染点就要有一个静音开关（当年那个叫 hidePausePrompt），",
    "而静音开关一旦判错，人就会看见「请回答」却没有可答的东西。",
  ].join("\n"));
  assert.match(offenders[0] ?? "", /SessionWorkspace\.tsx × 1$/);
});

test("只读的那张记录卡不接答复回调", () => {
  const record = codeOnly(
    readFileSync(join(SRC, "features/chat/components/PausedRecord.tsx"), "utf8"),
  );
  assert.doesNotMatch(record, /onAnswer/, [
    "记录卡有了 onAnswer 就又能表达「看起来能点其实点不了」——",
    "而那要靠一个 resumable 之类的布尔来区分，判错就是一次静默死锁。",
  ].join("\n"));
});

test("局面组件不收 `status: string` —— 开放类型是那次事故的载体", () => {
  const activity = codeOnly(
    readFileSync(join(SRC, "features/chat/components/ChatRunActivity.tsx"), "utf8"),
  );
  assert.doesNotMatch(activity, /status\??:\s*string/, [
    "`status?: string | null` 让 `\"alive\"` 落进一个期望 `\"waiting_human\"` 的名单，",
    "而编译器一声不吭。局面组件只收 view —— 取值由字面量联合钉住。",
  ].join("\n"));
});

test("只有答复入口能宣布「下面有东西等你点」", () => {
  // 第四个各自独立的答案（2026-09-02 部署后当场照出来）：运行记录那条路
  // 从**这条 run 的** waitingOn 断言"Choose a response below"，而下面有没有
  // 东西由**会话**的 answer 说了算。只读的人于是看见「请回答」+ 空白 +
  // 「你只能看」—— 和 09-01 锁死同一个形状。
  //
  // 判据扫**承诺**，不扫某一行：任何在会话 answer 之外许诺"下面有可点的东西"
  // 的文案都算违规。上一版的扫盘闸盖不住它（它既不是 run 状态字面量，也不是
  // 第二个 <HumanInputPrompt>）—— 护栏也会有视野盲区，补的方式是补判据。
  const offenders: string[] = [];
  for (const file of sourceFiles(SRC)) {
    if (file.endsWith("features/sessions/lib/answer-affordance.ts")) continue;
    if (file.endsWith("features/chat/components/HumanInputPrompt.tsx")) continue;
    const text = codeOnly(readFileSync(file, "utf8"));
    if (/Choose a response below|response below to continue/i.test(text)) {
      offenders.push(file.slice(SRC.length));
    }
  }
  assert.deepEqual(offenders, [], [
    "这些地方在会话 answer 之外许诺「下面有可点的东西」：" + offenders.join(", "),
    "有没有那个东西只有 execution.answer 知道；一条 run 的 view 里没有 answer 这个键。",
  ].join("\n"));
});

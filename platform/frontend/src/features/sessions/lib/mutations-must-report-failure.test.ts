import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

/**
 * 每个写操作失败时都得出声。
 *
 * ## 缺口（2026-09-04）
 *
 * 同事从界面传了一个 800MB 的日志包。后端 413 拒了它 —— 而那个 mutation 只写了
 * `onSuccess`，没写 `onError`。界面一声不响，于是他以为传上去了，跟 agent 来回
 * 找了半天文件。真相是文件根本没上去。
 *
 * 缺失的错误处理和"上传成功了"在屏幕上**长得一模一样**，这正是它能潜伏下来的
 * 原因。所以判据不能靠人记得写：扫盘，一律要求 `onError`，例外逐条登记。
 *
 * 判据形状照 `feedback_guardrails_must_scan_not_list`：默认拒绝 + 具名例外。
 * 写"允许名单"的话，下一个新增的 mutation 默认漏过，而且 CI 全绿没人会知道。
 */

const ROOT = join(import.meta.dirname, "../../..");

/**
 * 登记过的例外。每条都要说明为什么这次失败**不需要**告诉用户。
 *
 * key = `<相对路径>:<mutationFn 里被调的函数名>`
 */
const EXEMPT = new Map<string, string>([
  [
    "features/feed/hooks/useFeed.ts:api.recordFeedEngagement",
    "结果就在屏幕上：没有乐观更新，成功才会重取，所以失败 = 星标/隐藏根本没变。" +
      "而且 action=\"open\" 是用户点开文章的副作用，不是他发起的动作 —— " +
      "为一条没记上的阅读回执弹红条，是在用户没做过的事上制造噪音。",
  ],
  [
    "features/feed/hooks/useFeed.ts:api.undoFeedEngagement",
    "同上，撤销失败等于星标没取消，屏幕上直接看得出来。",
  ],
]);

function walk(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory)) {
    const full = join(directory, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "node_modules" || entry === "fixtures") continue;
      found.push(...walk(full));
    } else if (
      (entry.endsWith(".ts") || entry.endsWith(".tsx")) &&
      !entry.endsWith(".test.ts") &&
      !entry.endsWith(".test.tsx")
    ) {
      found.push(full);
    }
  }
  return found;
}

/** 找出每个 `useMutation({...})` 块，返回 [文件相对路径, 块正文]。 */
function mutationBlocks(source: string): string[] {
  const blocks: string[] = [];
  let index = source.indexOf("useMutation(");
  while (index !== -1) {
    let depth = 0;
    let cursor = index + "useMutation(".length - 1;
    const start = cursor;
    do {
      const character = source[cursor];
      if (character === "(" || character === "{") depth += 1;
      else if (character === ")" || character === "}") depth -= 1;
      cursor += 1;
    } while (depth > 0 && cursor < source.length);
    blocks.push(source.slice(start, cursor));
    index = source.indexOf("useMutation(", cursor);
  }
  return blocks;
}

/** mutationFn 里调的那个函数名 —— 用来在例外表里指名道姓。 */
function calledFunction(block: string): string {
  // 带上成员名：`api.recordFeedEngagement` 和 `api.shareFeedLink` 是两件事，
  // 只取 `api` 会让同一个文件里五条 mutation 共用一个 key，例外表一登记就
  // 把另外四条一起放过 —— 那正是"名单式护栏"的失效方式。
  const identifier = "[A-Za-z_$][\\w$]*(?:\\.[A-Za-z_$][\\w$]*)*";
  const match =
    block.match(new RegExp(`mutationFn:\\s*\\([^)]*\\)\\s*=>\\s*(${identifier})`)) ??
    block.match(new RegExp(`mutationFn:\\s*(${identifier})`)) ??
    block.match(
      new RegExp(`mutationFn:\\s*async\\s*\\([^)]*\\)\\s*=>\\s*[\\s\\S]*?(${identifier})\\(`),
    );
  return match ? match[1] : "<unnamed>";
}

test("每个 useMutation 都要么处理失败、要么在例外表里登记", () => {
  const offenders: string[] = [];
  for (const file of walk(join(ROOT, "features"))) {
    const source = readFileSync(file, "utf8");
    if (!source.includes("useMutation(")) continue;
    const relative = file.slice(ROOT.length + 1);
    for (const block of mutationBlocks(source)) {
      if (/\bonError\b/.test(block)) continue;
      const key = `${relative}:${calledFunction(block)}`;
      if (EXEMPT.has(key)) continue;
      offenders.push(key);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "这些写操作失败时界面不会出声。要么加 onError，要么在 EXEMPT 里写清为什么" +
      `不需要告诉用户：\n  ${offenders.join("\n  ")}`,
  );
});

test("例外表里不许留下已经不存在的条目", () => {
  const seen = new Set<string>();
  for (const file of walk(join(ROOT, "features"))) {
    const source = readFileSync(file, "utf8");
    if (!source.includes("useMutation(")) continue;
    const relative = file.slice(ROOT.length + 1);
    for (const block of mutationBlocks(source)) {
      seen.add(`${relative}:${calledFunction(block)}`);
    }
  }
  const stale = [...EXEMPT.keys()].filter((key) => !seen.has(key));
  assert.deepEqual(
    stale,
    [],
    `例外表指着不存在的 mutation —— 判决过期了就该作废：\n  ${stale.join("\n  ")}`,
  );
});

test("交文件那条路自己必须报错，且用后端给的那句话", () => {
  const workspace = readFileSync(
    join(ROOT, "features/sessions/components/SessionWorkspace.tsx"),
    "utf8",
  );
  const block = mutationBlocks(workspace).find((item) => item.includes("addFileToSession"));
  assert.ok(block, "找不到交文件的 mutation");
  assert.match(block!, /onError/, "2026-09-04 的静默就出在这里，别再让它消失");

  const repository = readFileSync(
    join(ROOT, "features/sessions/api/session-repository.ts"),
    "utf8",
  );
  assert.match(
    repository,
    /detail\?\.message/,
    "后端把「上限多少、超了走哪条路」放在 detail.message 里；" +
      "整个对象丢给 String() 会渲染成 [object Object]，那句唯一有用的话就没了",
  );
});

test("上传上限不许在前端写死", () => {
  const composer = readFileSync(
    join(ROOT, "features/sessions/components/SessionComposerBar.tsx"),
    "utf8",
  );
  assert.match(composer, /file\.size > maxFileBytes/, "选文件时就要判上限");
  const hardcoded = composer.match(/\b\d{7,}\b|\b\d+\s*\*\s*1024\s*\*\s*1024\b/g);
  assert.equal(
    hardcoded,
    null,
    `上限只能来自后端（session.materialMaxBytes）。前端写常量的话，调上限那天` +
      `它会继续按老数字放行，然后用户传完才收到 413：${hardcoded?.join(", ")}`,
  );
});

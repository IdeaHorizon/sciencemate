import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { qk } from "../../../lib/query/keys.ts";

/**
 * 手写的 queryKey 就是一份会各自演化的抄件。
 *
 * 2026-08-20 现场：换模型之后 chip 纹丝不动，非得再发一条消息才跟上。原因是
 * 失效用的键是手写的 `["session", projectId, sessionId]`（单数），而查询用的
 * 是 `qk.session(...)` = `["sessions", projectId, sessionId, mode]`（复数）——
 * **两边都不报错**，invalidate 只是什么都没匹配上。
 *
 * 同一处还有第二份：composer 的模型列表查询写成 `["model-backends"]`，而设置
 * 页用的是 `qk.modelBackends()` = `["settings","model-backends"]`。于是在设置
 * 页加完一条连接，会话里的下拉永远看不到它。
 *
 * 判据是扫盘不是名单：这一层里**任何**手写的 queryKey 字面量都算违规，
 * 键必须从 `qk` 来。
 */
function tsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = `${dir}/${entry}`;
    if (statSync(path).isDirectory()) out.push(...tsFiles(path));
    else if ((path.endsWith(".ts") || path.endsWith(".tsx")) && !path.endsWith(".test.ts")) out.push(path);
  }
  return out;
}

test("features/sessions 里没有手写的 queryKey 字面量", () => {
  const root = new URL("../", import.meta.url).pathname;
  const offenders: string[] = [];
  for (const file of tsFiles(root)) {
    const source = readFileSync(file, "utf8");
    for (const match of source.matchAll(/queryKey:\s*\[/g)) {
      const line = source.slice(0, match.index).split("\n").length;
      offenders.push(`${file.slice(root.length)}:${line}`);
    }
  }
  assert.deepEqual(offenders, [], `这些地方手写了 queryKey，改成用 qk.*：\n${offenders.join("\n")}`);
});

test("会话查询键和它的前缀真的能互相匹配", () => {
  // invalidate 用前缀、查询用全键 —— 前缀必须是全键的真前缀，否则失效不了。
  const full = qk.session("p1", "s1", "api");
  const prefix = qk.sessionsPrefix("p1");
  assert.deepEqual(full.slice(0, prefix.length), [...prefix]);
});

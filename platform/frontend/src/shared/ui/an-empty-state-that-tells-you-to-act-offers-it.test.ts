/**
 * 空状态里写了祈使句，就得把那个按钮放在那句话旁边。
 *
 * ## 这道闸是怎么来的
 *
 * 2026-09-17 走查：用户盯着项目页正中那块空状态问「还是没有看到创建项目的啊？」。
 * 他没找错地方 —— 那儿写着「**建一个项目**，研究会话就有了共享的上下文…」，却
 * 没有任何能点的东西；真正的「新建项目」是右上角一个细边框的小按钮，离他在看的
 * 地方十万八千里。
 *
 * 空状态是第一次来的人唯一会盯着看的东西。它说「去做 X」而不给做 X 的路，和
 * 「文案许诺了一个 API 给不出的能力」是同一件事：界面把人指向一堵墙。
 *
 * ## 判据
 *
 * 扫所有 `<Empty ...>`：hint 里出现祈使句（建一个…/ 先… / Create a… 之类）的，
 * 必须带 `action=`。**不写名单** —— 新加的空状态自动进这道闸。
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";

function sources(dir: string, found: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) sources(path, found);
    else if (/\.tsx$/.test(entry)) found.push(path);
  }
  return found;
}

/** 叫人动手的说法。中英各几个，覆盖现有文案的写法。 */
const IMPERATIVE = /建一个|新建一个|先选|先配|去建|Create a |Start by |Add your first/;

test("空状态叫你做什么，就得给你做那件事的东西", () => {
  const offenders: string[] = [];
  for (const file of sources("src")) {
    const source = readFileSync(file, "utf8");
    // 每一处 `<Empty` 到它自己那个 `/>` 为止。
    for (const match of source.matchAll(/<Empty\b/g)) {
      const start = match.index!;
      const end = source.indexOf("/>", start);
      if (end === -1) continue;
      const block = source.slice(start, end);
      if (IMPERATIVE.test(block) && !/\baction=/.test(block)) {
        offenders.push(`${file.replace(/^src\//, "")} → ${(block.match(IMPERATIVE) ?? [""])[0]}`);
      }
    }
  }
  assert.deepEqual(offenders, [],
    "这些空状态叫人动手，却没给可点的东西 —— 第一次来的人只会盯着它，然后问「在哪」：\n" +
    offenders.join("\n"));
});

test("项目那一处确实带上了按钮（这道闸的由来）", () => {
  const source = readFileSync("src/features/projects/components/ProjectsList.tsx", "utf8");
  assert.match(source, /action=\{onCreate/, "项目空状态又没有按钮了");
  assert.match(source, /variant="primary"/, "按钮不是主按钮 —— 它是这一屏唯一该点的东西");
});

import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";

/**
 * 成功提示要能通过一道问题：**屏幕上看不出来吗？**
 *
 * wangd 2026-08-20：「这种弹窗一点点用都没有…还有个什么 you are now session
 * owner，这简直太蠢了…啥是 session owner 啊？」当时 21 条成功提示里 13 条在
 * 复述同一屏上已经发生的事。
 *
 * 这条测试不去数总量（数量会变），它钉住**被点名删掉的那些不许回来**，以及
 * 判据本身写在 `stores/notification.ts` 里。判据一旦消失，下一个人会照着
 * 「别的地方都弹了」把它们一条条加回来。
 */
const BANNED = [
  "You are now the session driver",   // 打字时自动发生的副作用，而且是内部词
  "Session control released",
  "Model connection saved",
  "Model connection removed",
  "Default model updated for new Sessions",
  "Profile updated",
  "Notification preference saved",
  "Project profile saved",
  "Artifact deleted",
  "Memory proposal approved",
  "Memory proposal rejected",
  "Project created",
  "Project deleted",
  "Resource registered",
  "Research session created",
  "Session renamed",
  "Run stopped",
  "停止信号已投递",
  "Instruction draft saved",
];

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = `${dir}/${entry}`;
    if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
    else if ((path.endsWith(".ts") || path.endsWith(".tsx")) && !path.endsWith(".test.ts")) out.push(path);
  }
  return out;
}

test("复述屏幕的成功提示不许回来", () => {
  const root = new URL("../../../", import.meta.url).pathname;
  const offenders: string[] = [];
  for (const file of sourceFiles(root)) {
    const source = readFileSync(file, "utf8");
    for (const banned of BANNED) {
      if (!source.includes(banned)) continue;
      // 只关心它是不是又被塞进 toast 了；出现在别处（按钮文案等）不算。
      const inToast = new RegExp(`push(Success|Info)\\([^)]*${banned.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&")}`).test(source);
      if (inToast) offenders.push(`${file.slice(root.length)} → ${banned}`);
    }
  }
  assert.deepEqual(offenders, [], `这些成功提示复述了屏幕上已有的事实：\n${offenders.join("\n")}`);
});

test("判据本身留在代码里", () => {
  const store = readFileSync(new URL("../../../stores/notification.ts", import.meta.url), "utf8");
  assert.match(store, /屏幕上看不出来吗/);
  assert.match(store, /结果在同一屏上立刻可见/);
});

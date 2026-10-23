/** 更新进行中说什么：按行为测（喂阶段与秒数，看它说什么）。 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { sayItWasUpdated, sayWhatIsHappening } from "./say-what-is-happening.ts";

const zh = (p: { zh: string }) => p.zh;
const en = (p: { en: string }) => p.en;

test("installing names the version and, after a few seconds, how long it has been", () => {
  assert.equal(sayWhatIsHappening("installing", "0.5.7", 0, zh), "正在下载并校验 0.5.7…");
  assert.equal(sayWhatIsHappening("installing", "0.5.7", 8, zh), "正在下载并校验 0.5.7…（已 8 秒）");
  assert.equal(sayWhatIsHappening("installing", "0.5.7", 8, en), "Downloading and verifying 0.5.7… (8s so far)");
});

test("restarting says the window comes back", () => {
  assert.match(sayWhatIsHappening("restarting", "0.5.7", 0, zh) ?? "", /0\.5\.7 已就绪.*窗口会自动回来/);
});

test("nothing to say when nothing is happening", () => {
  for (const phase of ["idle", "checking", "failed"] as const) {
    assert.equal(sayWhatIsHappening(phase, "0.5.7", 5, zh), null);
  }
});

test("after the update it says from what to what", () => {
  assert.equal(sayItWasUpdated("0.5.6", "0.5.7", zh), "已更新到 0.5.7（原 0.5.6）。");
  assert.equal(sayItWasUpdated("0.5.6", "0.5.7", en), "Updated to 0.5.7 (was 0.5.6).");
});

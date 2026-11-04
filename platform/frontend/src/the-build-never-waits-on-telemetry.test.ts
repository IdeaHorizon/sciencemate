import test from "node:test";
import assert from "node:assert/strict";

// `next build` 收尾时的使用统计上报，在外网「连得上、不回话」时会让进程停在路由表之后
// 不退出（CI public-tree 挂过四次，见 next.config.ts 顶部）。判据落在「加载这份配置之后
// 统计是关的」：CI、打包、本地构建都经过这份配置，关在这里就一处都关了。
test("loading the Next config turns build telemetry off", async () => {
  delete process.env.NEXT_TELEMETRY_DISABLED;
  await import("../next.config.ts");
  assert.equal(process.env.NEXT_TELEMETRY_DISABLED, "1");
});

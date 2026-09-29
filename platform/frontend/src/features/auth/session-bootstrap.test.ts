import test from "node:test";
import assert from "node:assert/strict";
import { bootstrapSession } from "./session-bootstrap.ts";
import type { CurrentUser } from "@/lib/api";

const LOCAL_USER = {
  id: "local", email: "", display_name: "Dev Researcher", role: "researcher",
  permissions: ["projects.create"],
} as unknown as CurrentUser;

test("没有票也要问一句我是谁 —— 个人档就是这么拿到身份的", async () => {
  let asked = 0;
  const user = await bootstrapSession({
    storedToken: null,
    adoptToken: () => assert.fail("没有票就没有票可装"),
    askWhoIAm: async () => { asked += 1; return LOCAL_USER; },
    discardTheToken: () => assert.fail("本来就没票，没有任何东西该被清掉"),
  });
  assert.equal(asked, 1, "问都没问 —— 界面于是永远不知道自己能做什么");
  assert.equal(user?.permissions?.includes("projects.create"), true);
});

test("没有票且服务器要登录：答案是没有身份，但不清任何东西", async () => {
  let discarded = 0;
  const user = await bootstrapSession({
    storedToken: null,
    adoptToken: () => {},
    askWhoIAm: async () => { throw new Error("401"); },
    discardTheToken: () => { discarded += 1; },
  });
  assert.equal(user, null);
  assert.equal(discarded, 0, "本来就没票，清掉等于把别处的缓存一并倒掉");
});

test("有票就带上票问", async () => {
  const adopted: string[] = [];
  const user = await bootstrapSession({
    storedToken: "a-real-token",
    adoptToken: (value) => adopted.push(value),
    askWhoIAm: async () => LOCAL_USER,
    discardTheToken: () => assert.fail("票是好的"),
  });
  assert.deepEqual(adopted, ["a-real-token"]);
  assert.equal(user?.id, "local");
});

test("拿着坏票被拒：清掉它", async () => {
  let discarded = 0;
  const user = await bootstrapSession({
    storedToken: "an-expired-token",
    adoptToken: () => {},
    askWhoIAm: async () => { throw new Error("401"); },
    discardTheToken: () => { discarded += 1; },
  });
  assert.equal(user, null);
  assert.equal(discarded, 1, "坏票留着，之后每个请求都带着它");
});

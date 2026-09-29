/**
 * 每一条出门的请求都要说清「这是谁的事」—— 否则它会去问错的机器。
 *
 * ## 这道闸挡的是什么
 *
 * 项目住在哪，它的记录、会话、产出就在哪。本机后端按请求**点名**了什么来决定由谁
 * 回答：地址里的项目 id、查询串里的 `project_id`，或者兜底的 `X-Project` /
 * `X-Organisation` 两个头。那两个头由 `ApiClient.fetchWithAuth` 统一加上。
 *
 * 于是有一类请求会静默走错：**绕过 `fetchWithAuth` 自己 fetch 的那些**。它们既没有
 * 凭据也没有点名，落到本机后端手里，本机诚实地答「没有这个东西」—— 而用户看到的
 * 是"这一块数据莫名其妙是空的"，没有任何一层报错。
 *
 * 会话事件流（SSE）正是这样一条：它的地址是 `/sessions/{id}/events/stream`，
 * **不在** `/projects/` 底下，也不带 `project_id` —— 全靠那个头。它还接受一个
 * `fetchImpl` 参数，默认是裸 fetch。生产调用方今天都传了 `api.fetchWithAuth`；
 * 少传一个的那天，症状是"组织项目的会话界面一直空着"。
 *
 * 判据把**合法的那一条路命名出来**，其余一律违规：只要谁在生产代码里传 `fetchImpl`，
 * 它就必须是 `api.fetchWithAuth`（`feedback_guardrails_must_scan_not_list`）。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { ApiClient } from "./api.ts";

function everySourceFile(root: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(root)) {
    const path = join(root, entry);
    if (statSync(path).isDirectory()) { found.push(...everySourceFile(path)); continue; }
    if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) found.push(path);
  }
  return found;
}

test("凡是传 fetchImpl 的地方，传的都是 api.fetchWithAuth", () => {
  const offenders: string[] = [];
  for (const file of everySourceFile("src")) {
    // 这两个是**定义**那一侧（形参和它的默认值），不是调用方。
    if (file.endsWith("src/lib/api.ts")) continue;
    const source = readFileSync(file, "utf8");
    // 取到**行尾**，不是到第一个逗号：传进去的通常是
    // `(input, init) => api.fetchWithAuth(input, init)`，里面本来就有逗号。
    // 第一版在逗号处截断，于是每一行都"不含 api.fetchWithAuth"，四条全被误判 ——
    // 一道判据自己看错了它要看的东西。
    for (const [, supplied] of source.matchAll(/fetchImpl:\s*(.+)$/gm)) {
      if (!supplied.includes("api.fetchWithAuth")) {
        offenders.push(`${file}: fetchImpl: ${supplied.trim()}`);
      }
    }
  }
  assert.deepEqual(offenders, [],
    "有请求绕过了 api.fetchWithAuth —— 它既没带凭据，也没说清自己在问谁的项目，"
    + "于是会落到本机后端手里并交出一片空白，而且不报错");
});

test("客户端真的把「在问谁」加在每条请求上", () => {
  const api = readFileSync("src/lib/api.ts", "utf8");
  // 两条出门的路：`fetchWithAuth`（绝大多数）和 `upload`（交文件那条，走裸 fetch）。
  // 少一条，那条路上的请求就会去问本机。
  const leaving = api.slice(api.indexOf("async fetchWithAuth"));
  assert.match(leaving.slice(0, 500), /whoThisIsAbout\(\)/,
    "fetchWithAuth 没带上「在问谁」");
  const uploading = api.slice(api.indexOf("private async upload<T>"));
  assert.match(uploading.slice(0, 400), /whoThisIsAbout\(\)/,
    "上传那条路没带上「在问谁」—— 交给组织项目的文件会落到本机");
});

/**
 * 反过来的那一半：这台机器自己的事，开着哪个项目都**不**带那个头。
 *
 * 2026-09-24 两次真跑（组织服务器 + 两个专业版桌面）：成员在本机早关掉了开场，一进
 * 组织项目它又弹出来。页面是在项目里整页载入的，`/settings/interface` 带着
 * `X-Project` 出门，被转去组织服务器 —— 服务器上那份"他"从没人碰过，
 * `onboarding_done: false`；主题、语言、项目那一圈教没教过，也都换成了那一份。
 *
 * 判据走真的客户端、真的出门那条路，只换掉 fetch：同一个开着组织项目的客户端，
 * 界面偏好不带头、项目级的一问照旧带（后一半防的是"把头整个拿掉"也能让前一半变绿）。
 */
test("界面偏好问的是这台机器 —— 开着组织项目也不说「这是那个项目的事」", async () => {
  const sent: { url: string; project: string | null }[] = [];
  const client = new ApiClient((async (input: string | URL | Request, init?: RequestInit) => {
    sent.push({ url: String(input), project: new Headers(init?.headers).get("X-Project") });
    return new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch);
  client.askingAbout({ project: "an-org-project" });

  await client.getInterfaceSettings();
  await client.saveInterfaceSettings({} as never);
  await client.listModelBackends();

  const said = (path: string) => sent.filter((one) => one.url.endsWith(path)).map((one) => one.project);
  assert.deepEqual(said("/settings/interface"), [null, null],
    "界面偏好带着 X-Project 出门了 —— 组织项目里它会被转去组织服务器，开场、主题、语言都换成服务器上那份");
  assert.deepEqual(said("/settings/model-backends"), ["an-org-project"],
    "项目页上的一问不再带兜底的头了 —— 组织项目的会话会去问本机，然后一片空白");
});

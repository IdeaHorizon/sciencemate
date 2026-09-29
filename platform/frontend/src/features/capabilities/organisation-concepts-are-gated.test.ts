/**
 * 个人档的界面上不出现组织概念（RFC_RESEARCH_BUDDY §3 R1）。
 *
 * 领导的要求是「首先个人用得爽，不教育用户」。落到界面上最硬的一条：一台装在
 * 自己电脑上的软件里，"登录"、"机构范围"、"成员角色" 这些词一个都不该出现 ——
 * 它们要求用户先理解一套他不属于的组织结构。
 *
 * 判据是机械的：凡是画组织概念的组件，都必须以服务器报出的**能力**为条件，
 * 不能只看有没有 token、更不能无条件画。默认拒绝 + 显式例外。
 */
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

/** 每一处「组织专属的界面」和它必须问的那项能力。 */
const GATED = [
  { file: "src/features/auth/AuthGate.tsx", capability: "auth" },
  { file: "src/app/login/page.tsx", capability: "auth" },
  // 注册页和登录页一样：没有登录这回事的安装上它不存在。
  { file: "src/app/register/page.tsx", capability: "auth" },
  { file: "src/shared/layout/AppShell.tsx", capability: "governance" },
  { file: "src/features/settings/components/PreferenceLanding.tsx", capability: "auth" },
  // 「组织」那一页：桌面上它是"你握着哪些组织"，组织服务器上它是"管这个组织"。
  // 两种局面都只画给有能力的那一端 —— 个人版连这个 Tab 都没有（AppShell 的
  // GatedNavLink），直接敲地址进来也只看到一句"这个版本只用本机"。
  { file: "src/app/(workspace)/organisation/page.tsx", capability: "connections" },
];

/**
 * 开场里那一问（「有组织服务器吗」）2026-09-22 删了，所以 `FirstRun.tsx` 不再
 * 在这张表上。
 *
 * 组织在**建项目那一刻**挑，不在开机第一眼问：那一问既问不出结果（一条连接要有
 * 凭据才叫连接，而凭据只能登录拿到），又把一个刚打开软件的人拦在一张表单前面。
 * 专业版第一次打开现在和个人版一模一样 —— 这正是 R1 想要的样子，只是比从前更彻底。
 *
 * 建项目里那一行「放在哪」不靠能力守，靠**数据**守：一条连接都没有时它整个不
 * 渲染（`WhereItLives`）。个人版上 `/connections` 永远是空清单，所以那一行永远
 * 不出现 —— 比能力判据更强，因为它连"有能力但还没连过"的人也不打扰。
 */

/**
 * 侧栏底部那块「本人 + 登出」也归这道闸。
 *
 * 它此前的判据是"有没有 user 对象"，而个人档**永远有** user（隐式本机用户）——
 * 于是一台没有登录这回事的机器上，侧栏底部一直挂着 "Dev Researcher · Researcher"
 * 和一个点了什么都不会发生的登出按钮（2026-09-16 新用户走查时看见的）。
 * 这正是这道闸开头那句话的意思：「有没有 token / 有没有 user」回答不了
 * 「这台机器上有没有登录这回事」。
 */
test("侧栏底部的『本人 + 登出』问服务器有没有 auth 这项能力", () => {
  const source = readFileSync("src/shared/layout/AppShell.tsx", "utf8");
  const from = source.indexOf("function UserContext()");
  const next = source.indexOf("\nfunction ", from + 1);
  const block = source.slice(from, next === -1 ? undefined : next);
  assert.match(
    block,
    /useHasCapability\(\s*["']auth["']\s*\)/,
    "UserContext 没问服务器有没有 auth —— 个人档里它会照画登出按钮",
  );
});

for (const { file, capability } of GATED) {
  test(`${file} asks the server whether "${capability}" exists here`, (t) => {
    // 登录页、注册页、「组织」Tab 是专业版的（公开树里删掉了）：不在就没有可守的。
    if (!existsSync(file)) return t.skip("专业版的页面，这棵树里没有");
    const source = readFileSync(file, "utf8");
    assert.match(
      source,
      new RegExp(`useHasCapability\\(\\s*["']${capability}["']\\s*\\)`),
      `${file} 没有问服务器有没有 ${capability} 这项能力。个人档里这段界面` +
        "不该存在，而「有没有 token」回答不了这个问题 —— 个人档里永远没有 token。",
    );
  });
}

test("the gate fails safe: an unreachable server is treated as one that needs sign-in", () => {
  const source = readFileSync("src/features/capabilities/ServerCapabilities.tsx", "utf8");
  // 问不到能力时必须往"要登录"的方向倒。反过来会在一台真有别人的服务器上
  // 放行匿名访问 —— 那是把一次网络抖动变成一次越权。
  assert.match(source, /\.catch\([\s\S]*?features:\s*\["auth"\]/);
});

test("nothing decides what to draw from the profile name", () => {
  // 名字会变（产品还没定名），能力不会。profile 只用来显示"本机 / 组织服务器"。
  for (const { file } of GATED) {
    if (!existsSync(file)) continue; // 专业版的页面，公开树里没有
    const source = readFileSync(file, "utf8");
    assert.doesNotMatch(
      source,
      /profile\s*===\s*["'](personal|org)["']/,
      `${file} 按档位名字判断该画什么 —— 判据要落在能力上`,
    );
  }
});

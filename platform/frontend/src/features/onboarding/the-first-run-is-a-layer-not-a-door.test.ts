/**
 * 开场是**叠在工作区上的一层**，不是一道必须先过的门。
 *
 * ## 这道闸接的是哪一条
 *
 * 它接替 `src/app/setup/the-first-run-asks-two-things.test.ts`（那一页 09-17 退役）。
 * 老判据里对的那几条一条没丢：组织那一问只问一次且能跳、人人都走的那段不教组织
 * 概念、答了要存得下、要不要问由真实状态定。新加的两条是这次改动本身的要害：
 * 落地不再被劫持，以及**每一步都能跳过、而跳过的代价必须说出口**。
 */
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { coversTheMainRole, stepsToShow } from "./lib/steps.ts";

/** 只看画出来的东西。注释里解释"为什么这里没有登录"必然写到"登录"两个字。 */
const visible = (path: string) => readFileSync(path, "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^\s*\/\/.*$/gm, "");

const FIRST_RUN = visible("src/features/onboarding/FirstRun.tsx");
const STEPS = visible("src/features/onboarding/lib/steps.ts");
const BANNER = visible("src/features/onboarding/MissingModelBanner.tsx");
const LANDING = visible("src/features/settings/components/PreferenceLanding.tsx");
const SHELL = visible("src/shared/layout/AppShell.tsx");

test("开场不再把人劫持到一个独占的页面", () => {
  assert.doesNotMatch(LANDING, /replace\(\s*["']\/setup["']\s*\)/,
    "落地又被劫持了 —— 用户还没看见产品就被要求先做决定，这正是要改掉的那件事");
  assert.match(SHELL, /<FirstRun\s*\/>/, "开场那一层没挂在工作区上");
});

test("每一步都能跳过", () => {
  // 每一步各一处「跳过」；最后一步是偏好，给的是"用默认的"。
  const skips = FIRST_RUN.match(/onboarding-skip/g) ?? [];
  assert.ok(skips.length >= 3, `能跳过的步骤只有 ${skips.length} 处 —— 说好每一步都不强制`);
  assert.match(FIRST_RUN, /onClick=\{remember\}/, "关不掉：没有「以后再说」这条路");
});

test("跳过模型的代价说出了口，而且盯的是真实状态", () => {
  assert.match(FIRST_RUN, /没有模型开不了会话|without a model you cannot start a session/,
    "「先跳过」旁边没说清代价 —— 用户会在一个跑不动的工作区里点来点去");
  assert.match(SHELL, /<MissingModelBanner\s*\/>/, "缺口提示没挂上");
  assert.doesNotMatch(BANNER, /onboarding_done/,
    "缺口提示看了那个标记 —— 那样跳过一次之后，这台机器就再也不说自己开不了工了");
});

test("「有没有主模型」只有一个判据，开场和缺口提示问的是同一个", () => {
  // 两处各问各的，就有两个会分叉的答案：曾经两处都数"有没有一条 ready 的连接"，
  // 而一条只被授权审图的连接也是 ready。判据是后端按运行时同一个函数算出的角色绑定。
  for (const [name, source] of [["开场", FIRST_RUN], ["缺口提示", BANNER]] as const) {
    assert.match(source, /coversTheMainRole\(/, `${name}没问 coversTheMainRole —— 又自己判了一遍`);
    assert.match(source, /listModelRoles\(\)/, `${name}的事实不是从角色绑定来的`);
    assert.doesNotMatch(source, /status === "ready"/, `${name}又在数 ready 的连接了`);
  }
});

test("主模型由组织提供，就不再问「用哪个模型」", () => {
  // 形状照 `/settings/model-roles` 在成员桌面上的真实回答（2026-09-24 真跑抄的）：
  // 组织的模型被对进本机列表、成员挑它作主模型之后，reasoning 那一行绑着它。
  const orgMember = [
    { id: "reasoning", required: true, available: true, bound_backend_id: "8defd0a2", bound_display_name: "组里的网关" },
    { id: "visual_review", required: false, available: false, bound_backend_id: null, bound_display_name: null },
  ];
  assert.equal(coversTheMainRole(orgMember), true);
  assert.deepEqual(stepsToShow({ hasMainModel: coversTheMainRole(orgMember), hasInterests: true }), []);
  assert.ok(!stepsToShow({ hasMainModel: coversTheMainRole(orgMember), hasInterests: false }).includes("model"));

  // 反过来：只有一个可选角色有人担着 —— 开不了会话，这一步得问。
  const onlyReviewer = [
    { id: "reasoning", required: true, available: false },
    { id: "visual_review", required: false, available: true },
  ];
  assert.equal(coversTheMainRole(onlyReviewer), false);
  assert.equal(stepsToShow({ hasMainModel: coversTheMainRole(onlyReviewer), hasInterests: true })[0], "model");
  assert.equal(coversTheMainRole([]), false, "角色目录读不到时装作有主模型");
});

test("开场只浮在工作区上 —— 项目页上不问也不开", () => {
  // 项目页上每一问都带着"这是那个项目的事"的兜底头（`api.askingAbout`），组织项目里
  // 就被转去组织服务器。开场问的、存的全是这台机器的事，它在项目页上一开，答话的就是
  // 服务器上那份从没配过模型、没选过方向的"他"（2026-09-24 真跑撞上的）。
  assert.match(FIRST_RUN, /const onAProject = useProjectId\(\) !== ""/, "开场不知道自己是不是在项目页上");
  assert.match(FIRST_RUN, /if \(canConnect === null \|\| onAProject \|\| facts !== null\) return;/,
    "项目页上也去取事实了 —— 组织项目里那几问由组织服务器回答");
  const running = FIRST_RUN.match(/const running = ([^;]+);/)?.[1] ?? "";
  assert.match(running, /!onAProject/, "项目页上开场照样开 —— 它的每一次保存都会落到组织服务器上");
});

test("那个标记只管「要不要再教一遍」，不管「配好了没有」", () => {
  // 同样只看代码：那个文件的注释里正解释着这个标记该管什么。
  const steps = visible("src/features/onboarding/lib/steps.ts");
  assert.doesNotMatch(steps, /onboarding_done/,
    "算步骤的时候看了那个标记 —— 删掉模型之后开场会以为一切就绪");
  assert.match(FIRST_RUN, /onboarding_done: true/, "走完之后没记下来，下次打开又教一遍");
});

test("开场一个组织概念都不提 —— 专业版第一次打开和个人版一模一样", () => {
  // 这里曾经有一步「有组织服务器吗」。2026-09-22 删了：**组织在建项目那一刻挑，
  // 不在开机第一眼问**。那一问既问不出结果（一条连接要有凭据才叫连接，而凭据只能
  // 登录拿到），又把一个刚打开软件的人拦在一张表单前面 —— wangd 那句「pro 版就
  // 不洗得登录，必须得搞一个组织是嘛」说的就是它。
  //
  // 判据钉在**开场会走哪几步**上，不是"有没有某个词"：步骤表是那条路的唯一出处，
  // 它产不出组织那一步，开场里就不可能冒出来。
  assert.doesNotMatch(STEPS, /"organisation"/,
    "开场的步骤表里又有组织那一步了 —— 第一次打开就要求先回答「你属于哪个组织」");
  assert.doesNotMatch(FIRST_RUN, /features\/organisation/,
    "开场又去引组织那一块了");
});

test("加组织那条路在人真的需要它的两个地方", () => {
  // 向导里没有它了，所以它必须在别处真的够得着 —— 否则一个专业版用户永远连不上
  // 自己的课题组。两处都在他**正想用它**的那一刻：
  // 「组织」Tab 那一处是专业版的，判据在 src/pro/features/organisation/the-pro-edition-adds-one-entry.test.ts。
  // 建项目时的「放在哪」—— 这是最自然的那一刻："我要开个课题，它归谁"。核心留的是槽
  // （`otherHomes.Adder`），专业版装配时把「加一个组织」填进去。
  const creating = visible("src/app/(workspace)/projects/page.tsx");
  assert.match(creating, /<WhereItLives[\s/>]/, "建项目时没得挑家");
  assert.match(creating, /<AddAHome[\s/>]/, "「放在哪」里加不了一个新家");
  assert.match(creating, /otherHomes\.Adder/, "加一个家的对话框不是从槽里拿的");
});

test("默认是本机 —— 不说话就不属于任何组织", () => {
  const creating = visible("src/app/(workspace)/projects/page.tsx");
  // 「我开一个 project 就想本地搞不属于任何组织不行吗」——默认值就是这句话的答案。
  assert.match(creating, /home:\s*""/,
    "建项目的默认值不是本机 —— 只想自己弄的人被迫先挑一个组织");
});

test("可选模型按服务器的角色目录扫，不写死名单", () => {
  assert.match(FIRST_RUN, /listModelRoles\(\)/);
  assert.match(FIRST_RUN, /roles\.filter\(\(role\) => !role\.required\)/,
    "可选角色不是从目录里筛出来的 —— 写死名单意味着 harness 新加的角色默认漏过");
  for (const ghost of ["text_to_image", "文生图", "image_generation"]) {
    assert.ok(!FIRST_RUN.includes(ghost),
      `开场里出现了「${ghost}」——角色目录里没有它，消费方也没有。设置页上填完永远不会被调用的槽，就是幽灵能力`);
  }
});

test("谁都能给自己加一条模型连接 —— 这里不问权限", () => {
  // 第一版这里问的是 canManageModels()：能管共享模型才给表单。跑起来当场露馅——
  // 个人版那个隐式本机用户是 researcher，只有 model_backends.select，于是一台
  // 自己电脑上的软件告诉自己的主人"去找管理员"。
  //
  // 真身是问错了问题：后端 create_model_backend 一个权限都不查，managed_scope()
  // 对研究员返回 ("personal", user.id)。model-roles.ts 里记着同一个坑的实测：
  // 一位研究员因为界面把他指向一堵墙，认定平台不支持配审图模型。
  assert.doesNotMatch(FIRST_RUN, /canManageModels\(/,
    "又按「能不能管共享模型」决定给不给表单了 —— 那会让个人版叫自己去找管理员");
  assert.doesNotMatch(FIRST_RUN, /找发你邀请的那位管理员|tell whoever invited you/,
    "又把人指向一堵墙了");
});

const TOUR = visible("src/features/onboarding/GuidedTour.tsx");
const STOPS = visible("src/features/onboarding/lib/tour-stops.ts");
const PROJECT_GUIDE = visible("src/features/onboarding/ProjectGuide.tsx");

test("「这里干嘛的」贴在真东西旁边，锚点不在就不画", () => {
  assert.match(TOUR, /querySelector\(stop\.anchor\)/, "气泡没按站点给的锚点去找");
  assert.match(SHELL, /data-tour=\{item\.href\.split/, "导航项上没有锚点，气泡会找不到东西可贴");
  assert.match(TOUR, /if \(!anchor\)/, "锚点不在时没有跳过 —— 会画出一个悬在半空的气泡");
  // 锚点键不许带斜杠：带了就会被 navigation-state 那道闸当成一个不存在的页面
  // 地址（它判得没错），而且项目里的 href 带项目 id，整条写死只能贴一个项目。
  assert.doesNotMatch(STOPS, /data-tour[$^*]?="\//, "锚点键又长得像页面地址了");
});

test("项目里那一圈单独记，且不跟开场抢", () => {
  assert.match(SHELL, /\{projectId && <ProjectGuide \/>\}/, "项目里没挂那一圈气泡");
  assert.match(PROJECT_GUIDE, /project_guide_done/);
  assert.match(PROJECT_GUIDE, /if \(!preference\.settings\.onboarding_done\) return null/,
    "开场还没走完就插话 —— 一次只教一件事");
});

test("开场能再走一遍", () => {
  const general = visible("src/app/(workspace)/settings/page.tsx");
  assert.match(general, /onboarding_done: false/,
    "走完之后就再也看不到了 —— 得有一条路能把它再打开");
});

test("气泡说的是这块地方替你做什么，不是它叫什么", () => {
  // 侧栏上已经写着名字了，再念一遍等于什么都没说。
  for (const stop of ["每天按你选的方向", "研究在这里发生", "会话能用到的机器"]) {
    assert.ok(STOPS.includes(stop), `工作区那一圈少了一站：${stop}`);
  }
  for (const stop of ["干活的地方", "你交给它的数据", "做出来的东西在这儿", "这个课题现在推到哪了"]) {
    assert.ok(STOPS.includes(stop), `项目那一圈少了一站：${stop}`);
  }
});

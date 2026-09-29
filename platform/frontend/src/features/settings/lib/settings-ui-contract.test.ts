import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("Project settings edit only persisted metadata behind manage_settings", () => {
  const page = source("../../../app/(workspace)/projects/[id]/settings/page.tsx");
  assert.match(page, /canManageProjectSettings\(project\)/);
  assert.match(page, /api\.updateProject\(projectId, data\)/);
  assert.match(page, /disabled=\{!canManageSettings \|\| update\.isPending\}/);
  assert.match(page, /只有项目负责人或管理员能改这些设置/);
  assert.match(page, /ProjectMembersPanel/);
  // 「资源」「算力」两行链到的那两页在 2026-09-12 删了（登记簿 harness 零读取），
  // 所以这里反过来钉住：它们不许再回来。
  for (const unsupported of ["operation_mode", "reporting_level", "tool_whitelist", "research_intent", "preferred_model"]) {
    assert.equal(page.includes(unsupported), false, unsupported);
  }
});

test("personal research settings explain scope, audience, and activation time", () => {
  const page = source("../../../app/(workspace)/settings/research/page.tsx");
  assert.match(page, /Personal/);
  assert.match(page, /只影响新会话/);
  assert.match(page, /已有会话不变/);
  assert.match(page, /会话建立时冻结/);
  assert.match(page, /在别处强制执行/);
  assert.match(page, /把个人指令应用到新会话/);
  assert.match(page, /不会改动项目知识库，也不影响已有会话/);
  assert.match(page, /memory_enabled: settings\.memory_enabled/);
  assert.match(page, /memory_enabled: event\.target\.checked/);
  assert.match(page, /不改变数据访问权限/);
  assert.match(page, /not available in the current settings contract/);
});

test("settings center has one searchable shell and dedicated personal, research, and governance routes", () => {
  const shell = source("../components/SettingsShell.tsx");
  const appShell = source("../../../shared/layout/AppShell.tsx");
  assert.match(shell, /回到工作区/);
  assert.match(shell, /搜索设置/);
  // `/settings/governance` 09-17 没了：管一个组织不是偏好设置，整段搬进了侧栏的
  // 「组织」Tab（features/organisation）。`/settings/organisation-server` 09-22 也
  // 没了 —— 那一页是「这个应用下次打开时连哪」，而应用现在永远开在本机。
  // 设置里剩下的就是**这台机器**的偏好，一项不多。
  for (const route of ["/settings/profile", "/settings/appearance", "/settings/research", "/settings/models", "/settings/notifications", "/settings/usage"]) {
    assert.match(shell, new RegExp(route));
  }
  assert.match(appShell, /pathname\.startsWith\("\/settings"\)/);
});

test("general settings are real startup and Session usage controls", () => {
  const page = source("../../../app/(workspace)/settings/page.tsx");
  const landing = source("./default-landing.ts");
  const sessionIndex = source("../../sessions/components/SessionIndex.tsx");
  const execution = source("../../execution/components/SessionExecutionView.tsx");
  const canonicalActivity = source("../../chat/components/CanonicalRunActivity.tsx");
  const chatMessages = source("../../chat/components/ChatMessages.tsx");
  assert.match(page, /default_landing/);
  assert.match(page, /show_run_usage/);
  assert.match(landing, /latestRun/);
  assert.match(landing, /return "\/projects"/);
  assert.match(sessionIndex, /interfaceSettings\.show_run_usage/);
  for (const field of ["execution_detail", "auto_collapse_completed_tools", "auto_collapse_completed_steps"]) {
    assert.match(execution, new RegExp(`interfaceSettings\\.${field}`));
    assert.match(canonicalActivity, new RegExp(`settings\\.${field}`));
  }
  assert.match(chatMessages, /interfaceSettings\.follow_active_run/);
});

test("notification preferences use the canonical API and only expose real live-Run consumers", () => {
  const page = source("../../../app/(workspace)/settings/notifications/page.tsx");
  const chat = source("../../chat/hooks/useChat.ts");
  assert.match(page, /api\.saveNotificationSettings/);
  for (const preference of ["decision_required", "run_failed", "run_completed"]) {
    assert.match(page, new RegExp(preference));
    assert.match(chat, new RegExp(preference));
  }
  assert.match(page, /预算告警/);
  assert.match(page, /没有可用的送达方式/);
  assert.match(page, /永远可见/);
});

test("the account page and the organisation page do not dump the access machinery", () => {
  // 「实际范围 / 实际权限 / 策略来源 / 组织归属」曾经摆在这两页上：给机器看的东西
  // （权限字符串后端从不检查、策略来源是两行写死的字面量、rbac-v1 不是任何东西的版本），
  // 没有一个科研人会问（RFC_ORGANISATION_PAGE_20260923 §1.1）。它们回来 = 这一页又在
  // 画机制而不是名词。
  // 只看画出来的东西：注释里解释"为什么这一页不再画它"必然要写到它的名字。
  const shown = (path: string) => source(path).replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  const profile = shown("../../../app/(workspace)/settings/profile/page.tsx");
  assert.match(profile, /账户与安全/);
  assert.doesNotMatch(profile, /实际权限|组织归属|实际治理范围|governance_scope|permissions\.map/);
  // 组织页那三块是专业版的：src/pro/features/organisation/the-organisation-page-draws-nouns.test.ts。
});

test("instructions are files: one editable text per layer, read live by the session", () => {
  // 三张表、发布流程、版本历史 2026-09-05 删（RFC X2）；那份抄进数据库的快照
  // 2026-09-14 删（RFC X3）。指令是文件、版本是 git、冻结是会话自己的 git 分支。
  // 这条测试问的是"这三件事还成立吗"：文件能编辑、路径要说出来（别的编辑器改它
  // 也算数）、会话面板显示的是**这个会话当下读到的**那几层。
  const editor = source("../../instructions/components/InstructionFileEditor.tsx");
  const session = source("../../instructions/components/SessionAboutDrawer.tsx");
  const personal = source("../../../app/(workspace)/settings/research/page.tsx");
  const project = source("../../../app/(workspace)/projects/[id]/settings/page.tsx");
  const workspace = source("../../sessions/components/SessionWorkspace.tsx");

  assert.match(editor, /PROFILE\.md/);
  assert.match(editor, /PROJECT\.md/);
  assert.match(editor, /file\.data\.path/, "文件路径要显示出来");
  assert.match(editor, /readOnly=\{!editable\}/, "没有管理权限时只读，不是藏起来");
  assert.match(personal, /PersonalInstructionsEditor/);
  assert.match(project, /ProjectInstructionsEditor/);
  // 抽屉显示的是这个会话**当下**读到的那几层：项目层来自它自己的 worktree 分支
  //（git 就是冻结），个人层来自当下的文件。
  assert.match(session, /readSessionInstructions/);
  assert.match(session, /sha256/);
  assert.match(workspace, /SessionAboutDrawer/);
});

// 登录页的两条判据（没有演示凭据、?next= 只走 localReturnPath）是专业版的：
// src/pro/features/auth/the-login-page-is-production-grade.test.ts。

test("每个模型角色都在页面上有名有姓，缺的那个也在", () => {
  // 从前这一页只有一行「Default model」—— 于是节点内的辅助模型（审图 VLM）
  // 在界面上完全不存在：看不见是谁、换不了、缺了也不知道。主模型只是
  // role="reasoning"，它不该有自己的一套渲染和自己的一个端点。
  // 表单、角色、连接列表住在本机页和组织页共用的 `ModelConnections` 里；本机页只给它一份
  // 「问本机后端」的 client（组织页给的是「问那个组织的服务器」）。
  const shell = source("../../../app/(workspace)/settings/models/page.tsx");
  const page = source("../components/ModelConnections.tsx");
  assert.match(shell, /<ModelConnections/, "本机的模型页没用那一块共用的界面");
  assert.match(page, /模型角色/);
  assert.match(page, /改动只对新会话生效/);
  // 角色目录来自 API。前端**不枚举角色** —— provider 词表当年在两侧各活
  // 一份、靠一条测试钉着相等，那条路不走第二遍。
  assert.match(page, /client\.roles\(\)/);
  assert.match(shell, /roles: \(\) => api\.listModelRoles\(\)/);
  assert.equal(
    /"visual_review"|'visual_review'/.test(page),
    false,
    "角色名不该硬编码在前端；目录是 API 给的",
  );
  // 缺角色时要显示**出路**，不是一个空槽 —— 而且要显示这个读者走得通的那条
  // （见 model-roles.test.ts：局面由 situation 决定，不由权限单独决定）。
  assert.match(page, /roleAssignmentHint\(role, bound, situation, lang\)/);
  assert.match(page, /canAuthorizeExisting: hasEditableBackend\(visibleBackends\)/);
  assert.match(page, /这个角色还没有可用的连接/);
  // 空槽旁边就得有补它的那个动作，且**预勾这个角色** —— 不预勾等于把人送回
  // 同一个坑：他在对话框里还得自己想起来去勾一个他压根没听说过的复选框。
  assert.match(page, /给这个角色配一个模型/);
  assert.match(page, /setEditing\(\{ \.\.\.EMPTY_FORM, roles: \[role\.id\] \}\)/);
  // absence_note 是注入给消费方节点的（"你什么都不用做"）。设置页的读者是
  // 唯一能把槽填上的人，读的必须是 absence_impact。
  assert.equal(page.includes("absence_note"), false);
  // 指派走同一条路：主模型没有自己的 mutation。
  assert.match(page, /client\.setRoleDefault\(backend\.id, role\)/);
  assert.match(shell, /setRoleDefault: \(id, role\) => api\.setRoleDefaultBackend\(id, role\)/);
  assert.equal(
    (page + shell).includes("api.setDefaultModelBackend"),
    false,
    "「设默认」不该有两份实现；主模型走 role=reasoning",
  );
  assert.match(page, /\{n\} 条由管理员维护/);
  assert.match(page, /role="dialog"/);
  assert.match(page, /deduplicateModelBackends/);
  assert.equal(page.includes("model-backend-form"), false);
});

test("一条连接能被授权给哪些角色，是可以在界面上勾的", () => {
  // 没有这一段，一条连接永远只能当主模型 —— 审图 VLM 注册不进来。
  const page = source("../components/ModelConnections.tsx");
  assert.match(page, /这条连接可以承担哪些角色/);
  assert.match(page, /type="checkbox"/);
  assert.match(page, /roleCatalog\.map/);
});

test("每个角色都够得着「加一个模型」，且这一页不假装能建会话", () => {
  // 加连接的入口曾经挂在 `model_backends.manage` 上 —— 研究员拿不到这个权限，
  // 于是「增加模型」这件后端一直支持的事，在界面上整个不存在。
  const shell = source("../../../app/(workspace)/settings/models/page.tsx");
  const page = source("../components/ModelConnections.tsx");
  assert.match(page, /添加模型/);
  // 本机上谁都能加（研究员加进自己的 scope）；「只有管理员能加」只属于组织那一份。
  assert.match(shell, /\n\s*canAdd\n/, "本机的模型页把「添加模型」关掉了");
  assert.equal(page.includes("canManage"), false, "整段连接区不该再被角色权限一刀切");
  assert.match(page, /actions\.canEdit && <button/);
  // provider 的合法取值必须在控件里给出，不能等存完之后靠一个状态词去发现。
  assert.match(page, /providerOptions\(form\.provider\)/);
  // 能建就得能删：一条填错的连接不该永远留在列表里。
  assert.match(page, /client\.remove\(backend\.id\)/);
  assert.match(shell, /remove: \(id\) => api\.deleteModelBackend\(id\)/);
  assert.match(page, /globalThis\.confirm\(t\(\{ zh: `确定删除模型连接/);
  assert.equal(/<h2>New Session<\/h2>/.test(page + shell), false, "会话在 Project 里建，设置页不建会话");
  assert.match(page, /这一页不会新建会话/);
});

test("应用外壳定高：左栏不被右栏的内容推走", () => {
  // Projects 列表把 grid 撑到 3107px 时，侧栏底部的 Settings / 账号跟着沉到
  // 3107px 处 —— 首屏看不见。滚动条属于右栏。
  const shell = source("../../../shared/styles/shell.css");
  assert.match(shell, /\.app-shell \{[^}]*height: 100dvh/);
  assert.match(shell, /\.app-shell \{[^}]*overflow: hidden/);
  assert.match(shell, /\.app-main \{[^}]*overflow: auto/);
  assert.match(shell, /\.app-sidebar \{[^}]*overflow-y: auto/);
  assert.equal(/\.app-shell \{[^}]*min-height: 100vh/.test(shell), false);
  assert.equal(/\.app-sidebar \{[^}]*min-height: 100vh/.test(shell), false);
});

test("appearance persistence applies explicit theme, legacy colors, density, scale, and reduced motion", () => {
  const provider = source("../InterfaceSettingsProvider.tsx");
  const tokens = source("../../../shared/styles/tokens.css");
  const globals = source("../../../app/globals.css");
  assert.match(provider, /root\.dataset\.theme = settings\.theme/);
  assert.match(provider, /root\.dataset\.density = settings\.density/);
  assert.match(provider, /--interface-font-scale/);
  assert.match(tokens, /data-theme="light"/);
  assert.match(tokens, /data-theme="dark"/);
  assert.match(tokens, /data-reduce-motion="true"/);
  assert.match(globals, /--bg: var\(--color-bg\)/);
  assert.match(globals, /--text: var\(--color-fg\)/);
});

test("Session composer can actually switch the model, and still links to the single Settings entry", () => {
  // 2026-08-10：模型从"一行截断文字 + 链接"改成控制条上的 chip。
  // 2026-08-13：**取消会话级冻结**。冻结挡不住配错的后端，只挡住修正 ——
  // 机构默认后端配错那次，会话里两次 run 全是 401、零产出，却换不掉模型。
  // 守的意图：这个 chip 必须是**真开关**（点开有候选、选了会发 PATCH），
  // 不许退回成"点开只解释为什么不能换"的假开关。
  const workspace = source("../../sessions/components/SessionWorkspace.tsx");
  const composer = source("../../sessions/components/SessionComposerBar.tsx");
  const styles = source("../../../shared/styles/sessions.css");
  assert.match(workspace, /modelLabel=\{session\.modelBackendName \?\? null\}/);
  assert.match(workspace, /model_backend_id: backendId/);
  assert.match(composer, /href="\/settings\/models"/);
  assert.match(composer, /onModelChange\?\.\(item\.id\)/);
  assert.equal(composer.includes("会话中途不允许换模型"), false);
  assert.match(styles, /\.composer-chip[^}]*cursor: pointer/);
  assert.equal(composer.includes("cursor: help"), false);
  // 「为什么不能发」由后端随局面一起下发（`answer.reason`），前端只渲染。
  // 此前这句是前端自己拼的文案，而它与后端的 `canSend` 各算各的 ——
  // 2026-09-01 两者分叉，用户被锁在"请回答"和一个灰输入框之间 6 小时。
  assert.match(workspace, /composerDisabledReason=\{[\s\S]{0,200}?inputPlan\.reason/);
});

test("Settings navigation is one-line and keeps one global entry outside Project settings", () => {
  const shell = source("../components/SettingsShell.tsx");
  const appShell = source("../../../shared/layout/AppShell.tsx");
  assert.equal(shell.includes("<small>{item.description}</small>"), false);
  assert.match(appShell, /label: \{ zh: "项目设置"/);
  assert.equal((appShell.match(/GLOBAL_NAVIGATION\.settings\.href/g) ?? []).length, 1);
  assert.equal(appShell.includes("Research settings"), false);
  assert.equal(appShell.includes("settings/governance"), false);
});

test("算力只剩一页，探针收在一个默认关着的技术小节里", () => {
  /**
   * 项目级那一页 2026-09-12 删了：它读的 `project_resources` 登记簿 harness
   * **零读取**（core/host_capabilities.py 自己写着那张表 0 行、不是它读的），
   * 登记了 agent 也不知道；而它还内嵌一份全局算力页，于是两处可能不一致。
   * 剩下的这一页只答一个问题：这台机器能提供什么。
   */
  const inventory = source("../../compute/components/ComputeInventoryView.tsx");
  const page = source("../../../app/(workspace)/compute/page.tsx");
  assert.match(page, /ComputeInventoryView/);
  for (const label of ["CPU", "内存", "存储", "GPU", "正在跑的", "计算节点", "调度器"]) {
    assert.match(inventory, new RegExp(label));
  }
  assert.equal((inventory.match(/<details className="compute-technical">/g) ?? []).length, 1);
  assert.equal(inventory.includes("<details className=\"compute-technical\" open"), false);
  assert.ok(inventory.indexOf("技术细节") < inventory.indexOf("健康探针"));
  assert.equal(inventory.includes("Not supported"), false);
  assert.equal(inventory.includes(">Unknown<"), false);
});

test("member controls remain visible and disabled without manage_members", () => {
  const members = source("../../projects/components/ProjectMembersPanel.tsx");
  assert.match(members, /disabled=\{!canManage \|\| lastLead \|\| members\.changeRole\.isPending\}/);
  assert.match(members, /只有项目负责人或管理员能改成员角色/);
  assert.match(members, /disabled=\{!canManage\}/);
});

test("设置页写的 settings-* 类名都必须真有样式 —— 幽灵类名不许存在", () => {
  // 2026-09-15 现场（yuankk 的截图）：模型设置页里角色勾选那一段排版是散的，
  // 复选框和它的标签文字各占一行。查下来 `settings-role-fieldset` /
  // `settings-role-checkbox` **在任何 CSS 里都不存在** —— 只写在 JSX 上。
  // 于是它们落进通用规则 `.settings-dialog-fields label { display: grid }`，
  // 每个 label 变成单列网格，复选框被挤到标签文字的上一行。
  //
  // 判据不写名单（写名单 = 下一个幽灵类名默认漏过）：把页面里出现的
  // `settings-*` 类名全扫出来，逐个到样式表里找。
  const page = source("../../../app/(workspace)/settings/models/page.tsx")
    + source("../components/ModelConnections.tsx");
  const styles = [
    source("../../../shared/styles/settings.css"),
    source("../../../shared/styles/shell.css"),
  ].join("\n");

  const used = new Set<string>();
  for (const [, value] of page.matchAll(/className="([^"{}]+)"/g)) {
    for (const token of value.split(/\s+/)) {
      if (token.startsWith("settings-")) used.add(token);
    }
  }
  assert.ok(used.size > 5, `没扫到类名，正则失效了（扫到 ${used.size} 个）`);

  const ghosts = [...used].filter((name) => !styles.includes(`.${name}`));
  assert.deepEqual(ghosts, [], `这些类名写在 JSX 上但没有任何样式规则：${ghosts.join(", ")}`);
});

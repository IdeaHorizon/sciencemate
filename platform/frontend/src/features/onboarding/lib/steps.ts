/**
 * 开场要问哪几步 —— 从**真实状态**推，不从"我见过向导了"推。
 *
 * ## 为什么这是一个纯函数
 *
 * 开场这件事最容易出的错是「判据和事实分叉」：用一个标记记住"教过了"，然后
 * 用户把模型删了、或者换了台机器，界面仍然以为一切就绪。所以每一步能不能跳过
 * 由它自己的事实决定（有没有可用模型 / 有没有连上组织服务器 / 有没有选过方向），
 * 而那个标记只管一件事：**要不要再教一遍**（见 InterfaceSettings.onboarding_done）。
 *
 * 纯函数是为了能把上面这句话钉成判据，不用起浏览器。
 */

import type { ModelRole } from "@/lib/api";

export type OnboardingStep = "model" | "interests" | "landing";

export type OnboardingFacts = {
  /** 主模型那个角色现在有没有人担着（`coversTheMainRole`）。没有 = 开不了会话。 */
  hasMainModel: boolean;
  /** 资讯的方向选过没有。 */
  hasInterests: boolean;
};

/**
 * 「能不能开工」：主模型那个角色（目录里 `required` 的那一个）现在有没有一条真会被
 * 用上的连接。开场的第一步和工作区顶上那条缺口提示问的都是它。
 *
 * 答案取自 `/settings/model-roles` —— 后端用运行时真去取模型的同一个函数
 * （`select_effective_backend`）算出每个角色此刻落在哪条连接上。组织提供的模型在
 * 那一问之前就被对进了这台机器的列表，所以成员挑中「组里的网关」作主模型，这里就是真。
 *
 * 曾经问的是"列表里有没有一条 ready 的连接"。那是另一个问题：一条只被授权审图的连接
 * 也是 ready，却开不了会话；而两处各问各的，就有两个会分叉的答案。
 */
export function coversTheMainRole(roles: readonly Pick<ModelRole, "required" | "available">[]): boolean {
  return roles.some((role) => role.required && role.available);
}

/**
 * 按顺序给出这次开场要走的步骤；空数组 = 不用弹。
 *
 * 「打开时先看什么」(`landing`) 只是偏好，没有"没配好"这回事，所以它**不单独
 * 构成一次开场** —— 只有前面确实有话要问的时候才把它捎上。否则一台早就配好的
 * 机器升级上来会平白弹一次窗，教它一件它早就会的事。
 */
export function stepsToShow(facts: OnboardingFacts): OnboardingStep[] {
  const steps: OnboardingStep[] = [];
  if (!facts.hasMainModel) steps.push("model");
  // 这里曾经有一步「有组织服务器吗」（专业版才问）。2026-09-22 删了：**组织在建
  // 项目那一刻挑，不在开机第一眼问**。
  //
  // 那一问在旧模型里也只能记下一个还没连上的地址；新模型里一条连接要有凭据才叫
  // 连接，而凭据只能登录拿到 —— 于是那一问要么问不出结果，要么把一个刚打开软件
  // 的人拦在一张登录表单前面。专业版第一次打开现在和个人版一模一样：填模型、
  // 建项目、开跑。入口在「建项目 → 放在哪 → ＋加一个组织」和设置里的「组织」页，
  // 两处都在他真的需要它的那一刻。
  if (!facts.hasInterests) steps.push("interests");
  if (steps.length > 0) steps.push("landing");
  return steps;
}

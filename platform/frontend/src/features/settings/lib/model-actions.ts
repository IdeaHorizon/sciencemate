import type { CurrentUser, ModelBackend } from "@/lib/api";
import { canManageModels, canSelectModels } from "../../auth/access.ts";
import { say, type Language } from "../../../shared/i18n/language.ts";

/**
 * 一条连接能不能改，**后端已经答过了** —— `editable` 就是后端的
 * `can_edit_backend()`：这条连接的 scope 是不是你管得着的那个 scope。
 *
 * 这里原本是 `canManageModels(user) && backend.editable`。两个判据串起来的
 * 结果是：研究员建在自己名下的连接，后端认（PUT 会通过），前端不认（Edit
 * 按钮不出现）。而 `model_backends.manage` 这个权限研究员一辈子也拿不到 ——
 * 于是"个人 scope 的连接"这条后端一直支持的路径，在界面上整个不存在。
 * 判据只留后端那一份。
 */
export function modelBackendActions(
  user: CurrentUser | null,
  backend: Pick<ModelBackend, "editable" | "is_default" | "status">,
) {
  const canSelect = canSelectModels(user);
  return {
    canEdit: backend.editable,
    canSetDefault: canSelect && !backend.is_default && backend.status === "ready",
  };
}

export function modelAccessDescription(user: CurrentUser | null, lang: Language = "zh") {
  if (canManageModels(user)) return say({ zh: "可以管理模型连接和默认模型", en: "Model backend connections and defaults can be managed" }, lang);
  if (canSelectModels(user)) return say({ zh: "可以添加和修改自己的连接；共享的连接由管理员维护", en: "Your own connections can be added and edited; shared connections are managed by an administrator" }, lang);
  return say({ zh: "模型配置只读", en: "Read-only model configuration" }, lang);
}

export function modelBackendScopeKind(scope: ModelBackend["scope"]) {
  return typeof scope === "string" ? scope : scope.kind;
}

/**
 * 新建的连接会落在哪个 scope。
 *
 * 后端 `managed_scope()` 按角色定（管理员→institution，其余→personal；组那一档
 * 随 group_admin 一起退役了），而它和 `/auth/me` 里那份治理 scope 是同一套角色映射 ——
 * 所以这里读后端已经给过的事实，不按角色再算一遍。两边的名字差一个词：
 * 治理层叫 individual，连接的 scope 叫 personal。
 */
export function managedScopeLabel(user: CurrentUser | null, lang: Language = "zh") {
  const kind = user?.governance_scope?.kind;
  if (kind === "institution") return modelBackendScopeLabel("institution", lang);
  return modelBackendScopeLabel("personal", lang);
}

export function modelBackendScopeLabel(scope: ModelBackend["scope"], lang: Language = "zh") {
  const kind = modelBackendScopeKind(scope);
  // institution scope 就是「组织」（一台组织服务器上的一个组织）；"机构"是 node20 时代的叫法。
  if (kind === "institution") return say({ zh: "组织的连接", en: "Organisation connection" }, lang);
  if (kind === "personal") return say({ zh: "个人的连接", en: "Personal connection" }, lang);
  return say({ zh: "平台的连接", en: "Platform connection" }, lang);
}

export function modelDefaultImpact(
  user: CurrentUser | null,
  backend: Pick<ModelBackend, "editable" | "scope">,
  lang: Language = "zh",
) {
  const scope = modelBackendScopeKind(backend.scope);
  if (canManageModels(user) && backend.editable && scope === "institution") {
    return say({ zh: "设为组织默认，同时作为你新建会话时用的模型。", en: "Sets the Organisation default and your model for newly created Sessions." }, lang);
  }
  return say({ zh: "只改你新建会话时用的模型；已有会话保持原来的模型。", en: "Changes only your model for newly created Sessions; existing Sessions keep their model." }, lang);
}

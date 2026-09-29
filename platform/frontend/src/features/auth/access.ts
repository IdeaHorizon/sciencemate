import type { CurrentUser, UserRole } from "@/lib/api";
import { say, type Language, type Phrase } from "../../shared/i18n/language.ts";

//: 组织里只有两档身份（`RFC_ORGANISATION_PAGE_20260923` §3.1）。存储值沿用
//: `institution_admin` / `researcher`，给人看的是它们的意思。
const ROLE_LABELS: Record<UserRole, Phrase> = {
  institution_admin: { zh: "管理员", en: "Administrator" },
  researcher: { zh: "成员", en: "Member" },
};

export function roleLabel(role: UserRole | undefined, lang: Language = "zh") {
  return say(ROLE_LABELS[role ?? "researcher"] ?? ROLE_LABELS.researcher, lang);
}

export function hasAnyPermission(user: CurrentUser | null, permissions: string[]) {
  if (!user) return false;
  return permissions.some((permission) => user.permissions?.includes(permission));
}

//: 下面只认服务器**真的会发**的权限字符串（后端 `policies.ROLE_PERMISSIONS`）。曾经每个
//: 函数还各自认几个后端从不发的名字（`sessions.create`、`decisions.respond`…），五个函数
//: 整个就只认那种名字、而且没有一个调用方 —— 一道永远答「没有」的门，没人发现是因为没人走。
export function canManageModels(user: CurrentUser | null) {
  return hasAnyPermission(user, ["model_backends.manage"]);
}

export function canSelectModels(user: CurrentUser | null) {
  return canManageModels(user) || hasAnyPermission(user, ["model_backends.select"]);
}

export function canCreateSessions(user: CurrentUser | null) {
  return hasAnyPermission(user, ["projects.create"]);
}

export function governanceKindLabel(kind?: string, lang: Language = "zh") {
  if (kind === "institution") return say({ zh: "机构", en: "Institution" }, lang);
  return say({ zh: "个人", en: "Individual" }, lang);
}

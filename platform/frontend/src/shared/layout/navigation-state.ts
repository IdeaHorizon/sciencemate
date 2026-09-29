import type { LucideIcon } from "lucide-react";
import type { GovernanceScope } from "@/lib/api";
import type { Phrase } from "../i18n/language.ts";
import { say, type Language } from "../i18n/language.ts";

/**
 * 全局层（不在任何 Project 里）只暴露 Project 级的东西。
 *
 * Session 是 Project 内的概念：它的上下文、执行、产物全归属于某个 Project。
 * 所以这里既没有「新建 Session」的入口，也没有跨 Project 的活跃 Session 列表
 * —— 那些只在进入 Project 之后才出现（wangd 2026-08-21）。
 */
/**
 * 全局层只放**还回答得出问题**的入口。
 *
 * 2026-09-12 逐项核查，砍掉的三个各有各的死法，都不是"用得少"：
 *
 * - **Inbox**：读 `kb_proposals.jsonl`，而唯一的写入方 `curator_audit.curator_run`
 *   零调用方（记忆系统重建时删了候选队列）→ 它**永远是空的**。副标题还提着
 *   早就删掉的 Skills，而且它其实是项目级的（进去先让你挑项目）却挂在全局。
 * - **学术搜索**：一座孤岛。那套 `literature_search` 端点只有它在用，研究节点
 *   走的是自己的 `systematic_literature_search`。找文献是对话里的一句话。
 * - **Artifacts（全局）**：读 `artifacts` 表，而表里只有 publish 过的行，前端
 *   零 publish 调用。它和项目里的「研究产出」问同一个问题却读不同的东西，
 *   同一篇论文可能这边有那边没有，且分叉不报错（项目级那一份 09-09 已因此
 *   换成 `/outputs`）。
 *
 * 资讯留着，仍然排第一、仍是默认落地页：打开平台先看今天这个领域发生了什么
 * （wangd 2026-08-22、09-14 重申）。
 */
export const GLOBAL_NAVIGATION = {
  primary: [
    { href: "/feed", label: { zh: "资讯", en: "Feed" } as Phrase },
    { href: "/projects", label: { zh: "项目", en: "Projects" } as Phrase },
  ],
  more: [
    { href: "/compute", label: { zh: "算力", en: "Compute" } as Phrase },
    // 专业版相对个人版多出来的**唯一**一个侧栏入口「组织」不在这张表上：它由专业版
    // 在装配时登记（`registerNavigation`，见 `src/pro/wire.ts`）。公开树里没有专业版，
    // 这张表就是全部 —— 个人版一个组织概念都不画。
  ],
  settings: { href: "/settings", label: { zh: "设置", en: "Settings" } as Phrase },
} as const;

export function workspaceDisplayName(scope?: GovernanceScope, lang: Language = "zh") {
  if (!scope || scope.kind === "personal" || (scope.kind as string) === "individual") {
    return say({ zh: "个人工作区", en: "Personal workspace" }, lang);
  }
  // 机构和课题组的名字是后端给的真名 —— 那是数据，不翻译。
  return scope.name;
}


// ── 发行登记的导航入口 ────────────────────────────────────────────────────

export type NavigationEntry = {
  href: string;
  label: Phrase;
  /** 这几项能力**任一**具备时才画（专业版/组织服务器多出来的那一项）。 */
  needsAny?: readonly string[];
  icon?: LucideIcon;
  match?: (path: string) => boolean;
};

const EXTRA_NAVIGATION: NavigationEntry[] = [];

/** 发行往侧栏「更多」那一栏加一项。装配时调一次；同一个 href 只登记一次。 */
export function registerNavigation(entry: NavigationEntry): void {
  if (!EXTRA_NAVIGATION.some((one) => one.href === entry.href)) EXTRA_NAVIGATION.push(entry);
}

export function extraNavigation(): readonly NavigationEntry[] {
  return EXTRA_NAVIGATION;
}

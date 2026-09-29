"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ArrowLeft,
  BarChart3,
  Bell,
  Bot,
  Info,
  Monitor,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  UserRound,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/shared/ui";
import { useT, type Phrase } from "@/shared/i18n";

type SettingsNavigationItem = {
  href: string;
  label: Phrase;
  description: Phrase;
  keywords: string;
  Icon: LucideIcon;
  exact?: boolean;
};

const PERSONAL_ITEMS: SettingsNavigationItem[] = [
  { href: "/settings", label: { zh: "通用", en: "General" }, description: { zh: "打开时落在哪、记什么", en: "Where you land and what is recorded" }, keywords: "landing session token cost 落地 启动", Icon: Wrench, exact: true },
  { href: "/settings/profile", label: { zh: "账户与安全", en: "Account & security" }, description: { zh: "名字与密码", en: "Name and password" }, keywords: "name email account security password 账号 密码", Icon: UserRound },
  { href: "/settings/appearance", label: { zh: "外观", en: "Appearance" }, description: { zh: "主题、密度与字号", en: "Theme, density and type size" }, keywords: "light dark system font motion 主题 深色", Icon: Monitor },
  { href: "/settings/about", label: { zh: "关于", en: "About" }, description: { zh: "版本与更新", en: "Version and updates" }, keywords: "version update release about 版本 更新 升级 关于", Icon: Info },
];

const RESEARCH_ITEMS: SettingsNavigationItem[] = [
  { href: "/settings/research", label: { zh: "个人指令", en: "Personal instructions" }, description: { zh: "证据要求与回答偏好", en: "Evidence standard and response preferences" }, keywords: "agent citation language instructions reproducibility 指令 偏好", Icon: SlidersHorizontal },
  { href: "/settings/models", label: { zh: "模型与密钥", en: "Models & credentials" }, description: { zh: "默认模型、地址与密钥", en: "Default model, endpoint and keys" }, keywords: "llm api key provider gateway 模型 密钥", Icon: Bot },
  { href: "/settings/notifications", label: { zh: "通知", en: "Notifications" }, description: { zh: "什么时候提醒你、怎么送达", en: "When you are alerted and how it reaches you" }, keywords: "decision approval email desktop failure 通知 提醒", Icon: Bell },
  { href: "/settings/usage", label: { zh: "用量", en: "Usage" }, description: { zh: "跑过什么、花了多少", en: "What ran and what it cost" }, keywords: "tokens runs retries heatmap billing 用量 花费", Icon: BarChart3 },
];

//: 「组织与权限」09-17 搬进了侧栏的「组织」Tab —— 管一个组织不是偏好设置。
//: 这一组现在空着；留着这个名字是因为下面的分组标题还按它判断要不要出现，
//: 而组织服务器上迟早会有真正属于"设置"的治理项（比如组织级指令）。
//:
//: 这里也曾有过一组「连接」，里面唯一一项是「这个应用下次打开时连哪」。2026-09-22
//: 连同 `server.json` 整个删了：应用永远开在本机，组织是项目的家，入口在侧栏的
//: 「组织」Tab 和建项目时的「放在哪」—— 两处都在人真的需要它的那一刻。
const GOVERNANCE_ITEMS: SettingsNavigationItem[] = [];

function SettingsNavigationLink({ item, pathname }: { item: SettingsNavigationItem; pathname: string }) {
  const t = useT();
  const active = item.exact ? pathname === item.href : pathname.startsWith(item.href);
  return (
    <Link className={cn("settings-nav-link", active && "is-active")} href={item.href}>
      <item.Icon size={15} aria-hidden="true" />
      <span><strong>{t(item.label)}</strong></span>
    </Link>
  );
}

export function SettingsShell({ children }: { children: React.ReactNode }) {
  const t = useT();
  const pathname = usePathname() ?? "/settings";
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLowerCase();
  const filter = (items: SettingsNavigationItem[]) => !normalizedQuery
    ? items
    : items.filter((item) => `${item.label.zh} ${item.label.en} ${item.description.zh} ${item.description.en} ${item.keywords}`.toLowerCase().includes(normalizedQuery));
  const personal = filter(PERSONAL_ITEMS);
  const research = filter(RESEARCH_ITEMS);
  const governance = filter(GOVERNANCE_ITEMS);

  return (
    <main className="settings-center-shell">
      <aside className="settings-center-sidebar">
        <Link className="settings-back-link" href="/projects"><ArrowLeft size={14} />{t({ zh: "回到工作区", en: "Back to app" })}</Link>
        <div className="settings-center-title"><strong>{t({ zh: "设置", en: "Settings" })}</strong><small>{t({ zh: "这台平台的偏好", en: "Workspace preferences" })}</small></div>
        <label className="settings-search">
          <Search size={14} aria-hidden="true" />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t({ zh: "搜索设置", en: "Search settings" })} aria-label={t({ zh: "搜索设置", en: "Search settings" })} />
        </label>
        <nav aria-label={t({ zh: "设置导航", en: "Settings navigation" })}>
          {personal.length > 0 && <span className="settings-nav-heading">{t({ zh: "个人", en: "Individual" })}</span>}
          {personal.map((item) => <SettingsNavigationLink key={item.href} item={item} pathname={pathname} />)}
          {research.length > 0 && <span className="settings-nav-heading settings-section-nav-heading">{t({ zh: "研究工作区", en: "Research workspace" })}</span>}
          {research.map((item) => <SettingsNavigationLink key={item.href} item={item} pathname={pathname} />)}
          {governance.length > 0 && <span className="settings-nav-heading settings-governance-heading">{t({ zh: "组织治理", en: "Governance" })}</span>}
          {governance.map((item) => <SettingsNavigationLink key={item.href} item={item} pathname={pathname} />)}
          {personal.length === 0 && research.length === 0 && governance.length === 0 && <p className="settings-search-empty">{t({ zh: "没有匹配的设置项。", en: "No matching settings." })}</p>}
        </nav>
      </aside>
      <section className="settings-center-main">{children}</section>
    </main>
  );
}

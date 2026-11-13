"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Boxes,
  Cpu,
  FlaskConical,
  FolderKanban,
  LogOut,
  Newspaper,
  PanelLeft,
  Settings,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/shared/ui";
import { ProjectTree } from "./ProjectTree";
import { useProjectId } from "@/shared/routing/route-params";
import { governanceKindLabel, roleLabel, useAuth } from "@/features/auth";
import { useHasCapability } from "@/features/capabilities";
import { FirstRun, MissingModelBanner, ProjectGuide } from "@/features/onboarding";
import { AppVersion } from "@/features/update/AppVersion";
import { LanguageSwitch } from "@/features/settings/components/LanguageSwitch";
import { UpdateBanner } from "@/features/update/UpdateBanner";
import { GLOBAL_NAVIGATION, extraNavigation, workspaceDisplayName, type NavigationEntry } from "./navigation-state";
import { PRODUCT_MARK, PRODUCT_NAME } from "@/shared/brand";
import { useLanguage, useT, type Phrase } from "@/shared/i18n";

type NavItem = {
  href: string;
  label: Phrase;
  icon: LucideIcon;
  match: (path: string) => boolean;
  meta?: string;
  /** 这几项能力**任一**具备时才画（专业版/组织服务器多出来的那一项）。 */
  needsAny?: readonly string[];
};

/**
 * 每个全局导航目的地的图标与高亮判据，**按 href 索引**。
 *
 * 从前这两张表是按下标展开 GLOBAL_NAVIGATION 的（`primary[0]` / `primary[1]`）。
 * 那意味着往导航表里加一项，这里不改就会：新项不显示，或者更糟 —— 下标错位，
 * 图标和标签配错人。两种都不报错。
 *
 * 现在按 href 查表，缺映射的目的地照样显示（退回默认图标），由
 * `navigation-state.test.ts` 机械核对每个 href 都在这里有一份。
 */
const NAV_PRESENTATION: Record<string, { icon: LucideIcon; match: (path: string) => boolean }> = {
  "/feed": { icon: Newspaper, match: (path) => path.startsWith("/feed") },
  "/projects": { icon: FolderKanban, match: (path) => path === "/projects" },
  "/compute": { icon: Cpu, match: (path) => path.startsWith("/compute") },
};

function decorate(entries: readonly NavigationEntry[]): NavItem[] {
  return entries.map((entry) => {
    const presentation = NAV_PRESENTATION[entry.href];
    return {
      ...entry,
      // 发行登记的入口自带图标与高亮判据（它的表不在核心）；核心的查 NAV_PRESENTATION。
      icon: entry.icon ?? presentation?.icon ?? Boxes,
      match: entry.match ?? presentation?.match ?? ((path: string) => path.startsWith(entry.href)),
    };
  });
}

const GLOBAL_PRIMARY_NAV: NavItem[] = decorate(GLOBAL_NAVIGATION.primary);
/** 「更多」那一栏在渲染时算：发行登记的入口（`registerNavigation`）要等装配跑过才在。 */
function secondaryNav(): NavItem[] {
  return decorate([...GLOBAL_NAVIGATION.more, ...extraNavigation()]);
}

function NavLink({ item, pathname }: { item: NavItem; pathname: string }) {
  const t = useT();
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      className={cn("app-nav-item", item.match(pathname) && "app-nav-item-active")}
      title={t(item.label)}
      aria-label={t(item.label)}
      // 引导气泡的锚点（features/onboarding）。用**最后一段**而不是整个
      // href：项目里的地址带着项目 id，写整条就只能贴在某一个项目上。而且
      // 一个不带斜杠的键不会被当成页面地址 —— navigation-state 那道闸扫的是
      // 「像地址的字符串」，`[data-tour$="/research"]` 会被它判成一个不存在
      // 的页面，判得没错：那本来就不该长得像地址。
      data-tour={item.href.split("/").filter(Boolean).pop()}
    >
      <Icon size={16} />
      <span>{t(item.label)}</span>
      {item.meta && <small>{item.meta}</small>}
    </Link>
  );
}

function UserContext() {
  const t = useT();
  const lang = useLanguage();
  const { user, logout } = useAuth();
  // 个人版没有登录，所以也不该有「本人 + 登出」这一块 —— 它此前照画不误：
  // 隐式本机用户有个 display_name、有个 role，于是侧栏底部挂着一个
  // "Dev Researcher · Researcher" 和一个点了什么都不会发生的登出按钮。
  // 判据落在服务器报出的能力上（`auth`），不落在"有没有 user 对象"——
  // 个人档永远有 user，永远没有登录。
  const needsAuthentication = useHasCapability("auth");
  if (needsAuthentication !== true) return null;
  if (!user) return null;
  return (
    <div className="app-user-context">
      <div className="app-user-avatar">{user.display_name.slice(0, 1).toUpperCase()}</div>
      <span>
        <strong>{user.display_name}</strong>
        <small>{roleLabel(user.role, lang)}</small>
      </span>
      <button type="button" onClick={() => void logout()} aria-label={t({ zh: "退出登录", en: "Sign out" })} title={t({ zh: "退出登录", en: "Sign out" })}>
        <LogOut size={14} />
      </button>
    </div>
  );
}

function WorkspaceScope() {
  const t = useT();
  const lang = useLanguage();
  const { user } = useAuth();
  // 「机构范围 / 课题组范围」是组织才有的概念。个人档里它只会让人纳闷自己
  // 属于哪个机构 —— 而答案是：不属于任何机构，这台软件装在你自己的电脑上。
  const hasGovernance = useHasCapability("governance");
  const scope = user?.governance_scope;
  if (hasGovernance !== true) return null;
  const name = workspaceDisplayName(scope, lang);
  return (
    <div className="app-workspace-switcher" title={name}>
      <span>
        <strong>{name}</strong>
        <small>{t({ zh: "{kind}范围", en: "{kind} scope" }, { kind: governanceKindLabel(scope?.kind, lang) })}</small>
      </span>
    </div>
  );
}

/**
 * 带能力闸的导航项：`needs` 说明它要服务器报出哪项能力。
 *
 * 组织服务器多出来的入口（组织知识）在个人版上一个字都不该出现 —— 判据落在
 * `capabilities()` 上，不落在档位名字上（同 features/capabilities 那道闸）。
 * 还没问到能力（null）时不画：宁可晚半拍，也不要在个人版上闪一下。
 */
function GatedNavLink({ item, pathname }: { item: NavItem; pathname: string }) {
  // hook 数量不能随 item 变 —— 两条能力各问一次，固定两次。
  const first = useHasCapability(item.needsAny?.[0] ?? "");
  const second = useHasCapability(item.needsAny?.[1] ?? "");
  if (!item.needsAny?.length) return <NavLink item={item} pathname={pathname} />;
  const allowed = item.needsAny.some((_, at) => (at === 0 ? first : second) === true);
  // 还没问到能力时不画：宁可晚半拍，也不要在个人版上闪一下组织入口。
  if (!allowed) return null;
  return <NavLink item={item} pathname={pathname} />;
}

function GlobalNavigation({ pathname }: { pathname: string }) {
  const t = useT();
  return (
    <>
      <div className="app-brand" data-mark={PRODUCT_MARK}>
        <FlaskConical size={19} />
        <div><strong>{PRODUCT_NAME}</strong></div>
      </div>
      <WorkspaceScope />
      <nav className="app-nav" aria-label={t({ zh: "工作区导航", en: "Workspace navigation" })}>
        {GLOBAL_PRIMARY_NAV.filter((item) => item.href !== "/projects").map((item) => <NavLink key={item.href} item={item} pathname={pathname} />)}
      </nav>
      <ProjectTree />
      <div className="app-sidebar-bottom">
        {secondaryNav().map((item) => <GatedNavLink key={item.href} item={item} pathname={pathname} />)}
        {/* 用目标语言写着的那个钮：不懂当前语言的人，也认得出自己那种语言的名字。 */}
        <LanguageSwitch className="app-nav-item" />
        <Link href={GLOBAL_NAVIGATION.settings.href} aria-label={t(GLOBAL_NAVIGATION.settings.label)} title={t(GLOBAL_NAVIGATION.settings.label)} className={cn("app-nav-item", pathname.startsWith("/settings") && "app-nav-item-active")}>
          <Settings size={16}/><span>{t(GLOBAL_NAVIGATION.settings.label)}</span>
        </Link>
        <UserContext />
      </div>
    </>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  // 兜底用 /projects：/home 那条路由删掉了（它没有任何入口，内容是设置页的
  // 一块），拿一个不存在的地址当默认值会让高亮判据对着空气算。
  const pathname = usePathname() ?? "/projects";
  const t = useT();
  const projectId = useProjectId();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  useEffect(() => { setSidebarOpen(false); }, [pathname]);
  useEffect(() => {
    if (!sidebarOpen) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setSidebarOpen(false); };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [sidebarOpen]);
  if (pathname.startsWith("/settings")) return <>{children}</>;

  return (
    <main className="app-shell">
      <button type="button" className="workspace-sidebar-toggle" aria-label={t({ zh: "项目和会话", en: "Projects and sessions" })} aria-expanded={sidebarOpen} aria-controls="workspace-sidebar" onClick={() => setSidebarOpen(!sidebarOpen)}><PanelLeft size={16} /></button>
      {sidebarOpen && <button type="button" className="workspace-sidebar-scrim" aria-label={t({ zh: "关闭导航", en: "Close navigation" })} onClick={() => setSidebarOpen(false)} />}
      <aside id="workspace-sidebar" className={cn("app-sidebar", sidebarOpen && "is-open")}>
        <GlobalNavigation pathname={pathname} />
        <AppVersion />
      </aside>
      <section className="app-main"><UpdateBanner /><MissingModelBanner />{children}</section>
      {/* 开场浮在整个工作区上 —— 它是一层，不是一个页面。 */}
      <FirstRun />
      {/* 项目引导贴在统一侧栏与工作区工具栏上。 */}
      {projectId && <ProjectGuide />}
    </main>
  );
}

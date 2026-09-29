"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Archive,
  BookMarked,
  Boxes,
  ChevronLeft,
  Cpu,
  FlaskConical,
  FolderKanban,
  LogOut,
  Newspaper,
  Settings,
  Target,
  type LucideIcon,
  FolderTree,
} from "lucide-react";
import { cn } from "@/shared/ui";
import { ContextMenu, useContextMenu } from "@/shared/ui/ContextMenu";
import { governanceKindLabel, roleLabel, useAuth } from "@/features/auth";
import { useHasCapability } from "@/features/capabilities";
import { FirstRun, MissingModelBanner, ProjectGuide } from "@/features/onboarding";
import { UpdateBanner } from "@/features/update/UpdateBanner";
import { useProject } from "@/features/projects/hooks/useProjects";
import { projectIdentity } from "@/features/projects/lib/project-identity";
import { useProjectSessions, useSessionMutations } from "@/features/sessions";
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

/**
 * 侧栏「一行待办」的行内容 —— 项目内 Recent sessions 用。
 *
 * 标题必须包在 span 里才截得住：裸文本节点在 grid 里是匿名盒，`text-overflow`
 * 落不到它身上（早先全局那份抄件就是漏了这层，在 199px 宽的侧栏里换行溢出）。
 */
function RecentRowContent({ status, title, note }: {
  status: string;
  title: string;
  /** 状态那类附注。给了就换行显示 —— 侧栏放不下"标题 + 一句状态"同一行。 */
  note?: string;
}) {
  return (
    <>
      <i className={`run-status-${status}`} />
      <span className="app-recent-title">{title}</span>
      {note && <small className="app-recent-note">{note}</small>}
    </>
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
      <button type="button" onClick={() => void logout()} aria-label={t({ zh: "登出", en: "Sign out" })} title={t({ zh: "登出", en: "Sign out" })}>
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
 * 全局侧栏 —— 这里**没有** Session 级的东西：没有「新建 Session」的入口，
 * 也没有跨 Project 的活跃 Session 列表。见 navigation-state 里的那条判据。
 */
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
        {GLOBAL_PRIMARY_NAV.map((item) => <NavLink key={item.href} item={item} pathname={pathname} />)}
        {secondaryNav().map((item) => <GatedNavLink key={item.href} item={item} pathname={pathname} />)}
      </nav>
      <div className="app-sidebar-bottom">
        <Link href={GLOBAL_NAVIGATION.settings.href} aria-label={t(GLOBAL_NAVIGATION.settings.label)} title={t(GLOBAL_NAVIGATION.settings.label)} className={cn("app-nav-item", pathname.startsWith("/settings") && "app-nav-item-active")}>
          <Settings size={16}/><span>{t(GLOBAL_NAVIGATION.settings.label)}</span>
        </Link>
        <UserContext />
      </div>
    </>
  );
}

function ProjectNavigation({ pathname, projectId }: { pathname: string; projectId: string }) {
  const t = useT();
  const project = useProject(projectId);
  const identity = projectIdentity(project.data, {
    loading: project.isLoading,
    error: project.isError,
  }, useLanguage());
  const sessions = useProjectSessions(projectId);
  const recentSessions = (sessions.data ?? [])
    .filter((session) => session.lifecycleStatus !== "archived")
    .slice(0, 3);
  const root = `/projects/${projectId}`;
  const researchHref = `${root}/research`;
  /**
   * 项目内的入口。2026-09-12 核查后去掉三项：
   *
   * - **Resources**：`project_resources` 登记簿，而 harness **零读取**
   *   （`core/host_capabilities.py` 自己写着那张表 0 行、不是它读的）——
   *   登记了 agent 也不知道，是一份没人看的账。
   * - **Compute（项目）**：同一张登记簿筛 `type=compute`，再内嵌一份全局算力页。
   *   两个都在，答案却可能不一样。
   * - **Activity**：runs 列表。而「这个项目在跑什么」已经由 Research 那一页和
   *   侧栏的 Recent sessions 各答一遍 —— 三处同答一个问题，分叉时都不报错。
   */
  const items: NavItem[] = [
    { href: researchHref, label: { zh: "研究", en: "Research" }, icon: FlaskConical, match: (path) => path.startsWith(`${root}/research`) || path.startsWith(`${root}/sessions/`) },
    // 「研究产出」问的是"这个项目做出了什么"，一个问题一个入口。Artifacts 面
    // 读的是后端 artifacts 表（只有 publish 过的才有行），和它各说各话 ——
    // 同一篇论文可能这边有那边没有，而且分叉不报错。
    { href: `${root}/outputs`, label: { zh: "研究产出", en: "Research outputs" }, icon: Archive, match: (path) => path.startsWith(`${root}/outputs`) },
    { href: `${root}/files`, label: { zh: "项目文件", en: "Project files" }, icon: FolderTree, match: (path) => path.startsWith(`${root}/files`) },
    { href: `${root}/research-state`, label: { zh: "研究进展", en: "Research progress" }, icon: Target, match: (path) => path.startsWith(`${root}/research-state`) },
    { href: `${root}/memory`, label: { zh: "项目记忆", en: "Project memory" }, icon: BookMarked, match: (path) => path.startsWith(`${root}/memory`) },
    { href: `${root}/settings`, label: { zh: "项目设置", en: "Project settings" }, icon: Settings, match: (path) => path.startsWith(`${root}/settings`) },
  ];

  return (
    <>
      <Link href="/projects" className="app-project-back"><ChevronLeft size={14} />{t({ zh: "所有项目", en: "All projects" })}</Link>
      <div className="app-project-identity">
        <strong title={identity.name}>{identity.name}</strong>
        <span title={identity.meta} className={`project-identity-${identity.state}`}><i /> {identity.meta}</span>
      </div>
      <nav className="app-nav" aria-label={t({ zh: "项目导航", en: "Project navigation" })}>
        {items.map((item) => <NavLink key={item.href} item={item} pathname={pathname} />)}
      </nav>
      <div className="app-nav-section app-project-recents">
        <span>{t({ zh: "最近会话", en: "Recent sessions" })}</span>
        {sessions.isLoading && !sessions.data && <small className="app-recent-empty">{t({ zh: "载入中…", en: "Loading…" })}</small>}
        {sessions.isError && <small className="app-recent-empty">{t({ zh: "读不到会话", en: "Sessions unavailable" })}</small>}
        {sessions.data && recentSessions.length === 0 && <small className="app-recent-empty">{t({ zh: "还没有会话", en: "No sessions yet" })}</small>}
        {recentSessions.map((session) => (
          <SessionRecentItem
            key={session.id}
            session={session}
            root={root}
            projectId={projectId}
          />
        ))}
      </div>
      <div className="app-sidebar-bottom">
        <Link href="/projects" className="app-nav-item">
          <FolderKanban size={16} /><span>{t({ zh: "切换项目", en: "Switch project" })}</span>
        </Link>
        <UserContext />
      </div>
    </>
  );
}

/**
 * 侧栏里的一条会话 —— 左键进去，**右键才有删除**。
 *
 * 删除不常驻：它在列表里每一行都摆一个按钮的话，误点的代价是别人的研究。
 * 右键是"我确实在找这个操作"的信号（wangd 2026-08-19：「可以加一个右键菜单」）。
 */
function SessionRecentItem({ session, root, projectId }: {
  session: { id: string; title: string; execution: { phase: string } };
  root: string;
  projectId: string;
}) {
  const t = useT();
  const menu = useContextMenu();
  const mutations = useSessionMutations(projectId);
  return (
    <>
      <Link
        href={`${root}/sessions/${encodeURIComponent(session.id)}`}
        className="app-recent-item"
        title={session.title}
        onContextMenu={menu.onContextMenu}
      >
        {/* 真正的修法在后端 —— 标题现在是生成的短名 —— 这里是兜底：
            人手改的长名字、以及命名还没回来的那一小会儿。 */}
        <RecentRowContent status={session.execution.phase} title={session.title} />
      </Link>
      <ContextMenu
        at={menu.at}
        onClose={menu.close}
        items={[{
          // 后端一个端点两种结果：空会话真删、有内容的归档。文案照实说，
          // 别承诺一件它不做的事。
          label: t({ zh: "归档 / 删除会话", en: "Archive or delete session" }),
          danger: true,
          confirm: t({ zh: `确定归档或删除「${session.title}」吗？空会话会被直接删除，有内容的会归档。`, en: `Archive or delete "${session.title}"? An empty session is deleted outright; one with content is archived.` }),
          onSelect: () => mutations.archive.mutate(session.id),
        }]}
      />
    </>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  // 兜底用 /projects：/home 那条路由删掉了（它没有任何入口，内容是设置页的
  // 一块），拿一个不存在的地址当默认值会让高亮判据对着空气算。
  const pathname = usePathname() ?? "/projects";
  if (pathname.startsWith("/settings")) return <>{children}</>;
  const projectMatch = pathname.match(/^\/projects\/([^/]+)/);
  const projectId = projectMatch?.[1];

  return (
    <main className="app-shell">
      <aside className="app-sidebar">
        {projectId
          ? <ProjectNavigation pathname={pathname} projectId={projectId} />
          : <GlobalNavigation pathname={pathname} />}
      </aside>
      <section className="app-main"><UpdateBanner /><MissingModelBanner />{children}</section>
      {/* 开场浮在整个工作区上 —— 它是一层，不是一个页面。 */}
      <FirstRun />
      {/* 第一次进项目时那一圈：贴在项目侧栏那几项上。 */}
      {projectId && <ProjectGuide />}
    </main>
  );
}

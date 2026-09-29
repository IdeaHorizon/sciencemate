"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { useRouter } from "next/navigation";
import { AlertCircle, Archive, ArrowRight, Loader2, Plus } from "lucide-react";
import { canCreateSessions, useAuth } from "@/features/auth";
import { formatRelativeTime } from "@/shared/format/dates";
import { Skeleton } from "@/shared/ui";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { groupSessions } from "../lib/session-adapter";
import { sessionDriverLabel, sessionRevisionLabel } from "../lib/session-presentation";
import { useProjectSessions, useSessionMutations } from "../hooks/useSessions";
import type { ResearchSession, SessionDataMode, SessionGroup } from "..";
import { useT, useLanguage, type Phrase } from "@/shared/i18n";

const GROUP_LABEL: Record<SessionGroup, Phrase> = {
  running: { zh: "正在跑", en: "Running" },
  needs_attention: { zh: "需要你", en: "Needs attention" },
  unpublished: { zh: "未发布", en: "Unpublished" },
  recent: { zh: "最近", en: "Recent" },
  archived: { zh: "已归档", en: "Archived" },
};

const GROUP_ORDER: SessionGroup[] = [
  "running",
  "needs_attention",
  "unpublished",
  "recent",
  "archived",
];

// 纯函数收 t，不自己够 hook —— 见 shared/i18n/useT.ts 的「纯函数不许用它」。
function changesLabel(session: ResearchSession, t: (phrase: Phrase) => string) {
  if (session.conflictCount > 0) return t({ zh: `${session.conflictCount} 处冲突`, en: `${session.conflictCount} conflicts` });
  if (session.unpublishedChangeCount > 0) {
    return t({ zh: `${session.unpublishedChangeCount} 处待审`, en: `${session.unpublishedChangeCount} ready to review` });
  }
  return t({ zh: "没有待发布的改动", en: "No changes to publish" });
}

function usageLabel(session: ResearchSession, t: (phrase: Phrase) => string) {
  if (session.usage.totalTokens === 0 && session.usage.cost === null) return t({ zh: "没有花费记录", en: "Usage unavailable" });
  const parts = [`${session.usage.totalTokens.toLocaleString()} tokens`];
  if (session.usage.cost !== null && session.usage.currency) {
    try {
      parts.push(new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: session.usage.currency,
        maximumFractionDigits: 2,
      }).format(session.usage.cost));
    } catch {
      parts.push(`${session.usage.cost.toFixed(2)} ${session.usage.currency}`);
    }
  } else {
    parts.push(t({ zh: "费用未知", en: "cost unavailable" }));
  }
  if (session.retryCount) parts.push(t({ zh: `重试 ${session.retryCount} 次`, en: `${session.retryCount} retries` }));
  return parts.join(" · ");
}

function SessionRow({
  session, mode, showRunUsage, onArchive, archiving,
}: {
  session: ResearchSession;
  mode: SessionDataMode;
  showRunUsage: boolean;
  onArchive?: (session: ResearchSession) => void;
  archiving: boolean;
}) {
  const t = useT();
  const href = `/projects/${encodeURIComponent(session.projectId)}/sessions/${encodeURIComponent(session.id)}${mode === "fixture" ? "?mode=demo" : ""}`;
  const lang = useLanguage();
  const driver = sessionDriverLabel(session, lang);
  return (
    <div className="session-index-row-wrap">
      <Link className="session-index-row" href={href}>
        <i className={`session-state-dot state-${session.execution.phase}`} />
        <span className="session-index-main">
          <strong>{session.title}</strong>
          {session.summary && <small>{session.summary}</small>}
        </span>
        <span className="session-index-context">
          <strong>{session.execution.label}</strong>
          <small>{driver ? `${driver} · ` : ""}{sessionRevisionLabel(session, lang)}</small>
        </span>
        <span className="session-index-context session-index-usage">
          <strong>{changesLabel(session, t)}</strong>
          <small>{showRunUsage ? `${usageLabel(session, t)} · ` : ""}{formatRelativeTime(session.updatedAt, lang)}</small>
        </span>
        <ArrowRight size={13} aria-hidden="true" />
      </Link>
      {onArchive && (
        // 收起就在列表这一行上 —— 要清理的正是这一列里那些空壳，逼人先点进去
        // 再从 ⋯ 菜单里找，等于没有这个功能。
        <button
          type="button"
          className="session-index-archive"
          disabled={archiving}
          title={t({ zh: "收起这个会话；里面什么都没发生过的会直接删掉", en: "Put this session away; one where nothing ever happened is deleted outright" })}
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            onArchive(session);
          }}
        >
          {archiving ? <Loader2 size={13} className="spin" /> : <Archive size={13} />}
        </button>
      )}
    </div>
  );
}

export function SessionIndex({ projectId, mode = "api" }: { projectId: string; mode?: SessionDataMode }) {
  const t = useT();
  const router = useRouter();
  const { user } = useAuth();
  const { settings: interfaceSettings } = useInterfaceSettings();
  const sessions = useProjectSessions(projectId, mode);
  const mutations = useSessionMutations(projectId);
  const groups = groupSessions(sessions.data ?? []);
  // 开会话、收起会话，服务器要的都是**这个项目**的 drive 能力（`drive_session`）。只看账号角色的话，
  // 只读进来的人（组内可见的别人项目）和归档的项目里都会画一个点了就 403 的按钮。
  const project = useQuery({
    queryKey: qk.project(projectId),
    queryFn: () => api.getProject(projectId),
    enabled: mode === "api" && Boolean(projectId),
  });
  const drivesThisProject = project.data?.capabilities?.includes("drive") ?? false;
  const canCreate = canCreateSessions(user) && mode === "api" && drivesThisProject;
  // 收起只要项目权限，**不要求你此刻持有 driver** —— 后端 archive 端点要的
  // 就是 drive_session 能力。前端原来那道门比后端严，于是"没有活跃 driver"
  // 的会话在界面上完全动不了，看着像功能不存在。
  const canArchive = canCreateSessions(user) && mode === "api" && drivesThisProject;
  const [archivingId, setArchivingId] = useState<string | null>(null);

  const createSession = async () => {
    const created = await mutations.create.mutateAsync(t({ zh: "新的研究", en: "New research" }));
    const id = "id" in created ? created.id : null;
    if (id) router.push(`/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(id)}`);
  };

  return (
    <main className="session-index-page">
      <header className="session-index-header">
        <div>
          <span>{t({ zh: "项目研究", en: "Project research" })}</span>
          <h1>{t({ zh: "研究会话", en: "Research sessions" })}</h1>
          <p>{t({ zh: "一条会话装一个研究目标：它的对话、执行过程，以及还没发布的改动。", en: "Each Session keeps one research objective, its conversation, execution history, and changes awaiting publication." })}</p>
        </div>
        {canCreate && (
          <button type="button" onClick={() => void createSession()} disabled={mutations.create.isPending}>
            <Plus size={13} /> {mutations.create.isPending ? t({ zh: "新建中…", en: "Creating…" }) : t({ zh: "新建会话", en: "New session" })}
          </button>
        )}
      </header>

      {mode === "fixture" && <p className="session-fixture-provenance">{t({ zh: "界面演示数据 · 不是这个项目的真实历史", en: "UI demonstration data · not canonical project history" })}</p>}

      {sessions.isLoading && !sessions.data && (
        <div className="session-index-loading" aria-label={t({ zh: "正在载入会话", en: "Loading sessions" })}>
          {Array.from({ length: 5 }).map((_, index) => <Skeleton key={index} height={65} rounded="sm" />)}
        </div>
      )}

      {sessions.isError && (
        <div className="session-index-message" role="alert">
          <AlertCircle size={16} />
          <span><strong>{t({ zh: "读不到会话列表。", en: "Sessions could not be loaded." })}</strong><small>{sessions.error instanceof Error ? sessions.error.message : t({ zh: "平台没有返回一份能用的会话索引。", en: "The platform did not return a usable session index." })}</small></span>
          <button type="button" onClick={() => void sessions.refetch()}>{t({ zh: "重试", en: "Retries" })}</button>
        </div>
      )}

      {sessions.data && sessions.data.length === 0 && (
        <div className="session-index-empty">
          <span>{t({ zh: "这里还是空的", en: "Nothing here yet" })}</span>
          <h2>{t({ zh: "从一个研究目标开始", en: "Begin with one research objective" })}</h2>
          <p>{t({ zh: "你的第一句话就会成为这个项目下的一条会话。它的产出在你审过之前不会发布。", en: "The first message becomes a durable Project Session. Its outputs remain unpublished until reviewed." })}</p>
          {canCreate && <button type="button" onClick={() => void createSession()}><Plus size={13} />{t({ zh: "新建会话", en: "New session" })}</button>}
        </div>
      )}

      {sessions.data && sessions.data.length > 0 && (
        <div className="session-groups">
          {GROUP_ORDER.map((group) => {
            const items = groups[group];
            if (items.length === 0) return null;
            const content = items.map((session) => (
              <SessionRow
                key={session.id}
                session={session}
                mode={mode}
                showRunUsage={interfaceSettings.show_run_usage}
                archiving={mutations.archive.isPending && archivingId === session.id}
                onArchive={canArchive ? (item) => {
                  setArchivingId(item.id);
                  mutations.archive.mutate(item.id);
                } : undefined}
              />
            ));
            if (group === "archived") {
              return (
                <details className="session-index-group session-archived-group" key={group}>
                  <summary><Archive size={12} />{t({ zh: "已归档", en: "Archived" })}<span>{items.length}</span></summary>
                  <div className="session-index-list">{content}</div>
                </details>
              );
            }
            return (
              <section className="session-index-group" key={group}>
                <div className="session-index-group-heading">
                  <h2>{t(GROUP_LABEL[group])}</h2><span>{items.length}</span>
                </div>
                <div className="session-index-list">{content}</div>
              </section>
            );
          })}
        </div>
      )}
    </main>
  );
}

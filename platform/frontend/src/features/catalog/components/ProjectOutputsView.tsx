"use client";

import { useMemo } from "react";
import { FileText, FlaskConical, Image as ImageIcon, Package, Snowflake } from "lucide-react";

import { WorkspacePanelHost } from "@/features/file-preview/components/WorkspacePanelHost";
import { useProjectSessions } from "@/features/sessions/hooks/useSessions";
import type { CatalogEntry, CatalogTier } from "@/lib/api";
import { useWorkspacePanel } from "@/stores/workspace-panel";

import { useProjectCatalog } from "../hooks/useCatalog";
import { artifactKindLabel } from "../lib/kind-label";
import { TIER_ORDER, groupByTier } from "../lib/group-by-tier";
import { useT, type Phrase, useLanguage } from "@/shared/i18n";

/**
 * 「研究产出」—— 这个项目做出了什么，一页答完。
 *
 * ## 为什么需要它（wangd 2026-09-09）
 *
 * 「这个文件管理系统很混乱，比如每个节点一大堆没用的 log。」
 *
 * 在此之前，"这个项目产出了什么"有六个地方各自回答，同一篇论文可能出现在
 * 三处、各有各的名字，也可能一处都没有。而用户要找的东西——那篇论文——被
 * 埋在几百件框架内务中间：一个真项目上量过，264 件产物里 206 件是
 * `compression_log` / `latex_build_receipt` / `visual_*` 这类收据和快照。
 *
 * ## 单位是**对象**，不是文件
 *
 * 一篇论文 = 一行：有版本、冻没冻、它编出来的 PDF 就挂在那一行上，点开就看。
 * 让用户去目录树里翻、还得知道它叫 `paper/latex_build/…/main_clean.pdf`，
 * 正是这次要修的毛病。文件树仍在（「Project files」），那是调试视图。
 *
 * ## 分层是排序，不是过滤
 *
 * 「工作过程」默认收起但**照样列出来** —— 少列一条，用户就会以为那件东西
 * 不存在。看不见的东西没法被审计。
 */

const TIER_LABEL: Record<CatalogTier, Phrase> = {
  deliverable: { zh: "交付物", en: "Deliverables" },
  output: { zh: "研究产出", en: "Research outputs" },
  working: { zh: "工作过程", en: "Working files" },
};

const TIER_HINT: Record<CatalogTier, Phrase> = {
  deliverable: { zh: "已冻结的永久产出 —— 论文、图、预注册、结果数据。", en: "Frozen, permanent outputs: papers, figures, pre-registrations, result data." },
  output: { zh: "研究产出，但还没冻结，或者是证据而不是交付物。", en: "Research outputs that are not frozen yet, or are evidence rather than deliverables." },
  working: { zh: "收据、快照、压缩日志这类框架内务。列出来是为了可审计，不是给你读的。", en: "Receipts, snapshots and compaction logs — framework bookkeeping. Listed so it can be audited, not so you read it." },
};

function iconFor(entry: CatalogEntry) {
  if (entry.kind === "figure") return ImageIcon;
  if (entry.kind.includes("results") || entry.kind.includes("experiment")) return FlaskConical;
  if (entry.files.some((path) => /\.(zip|tar|gz)$/i.test(path))) return Package;
  return FileText;
}

export function ProjectOutputsView({ projectId }: { projectId: string }) {
  const t = useT();
  const sessions = useProjectSessions(projectId);
  const sessionId = useMemo(() => (sessions.data ?? [])[0]?.id ?? "", [sessions.data]);
  const catalog = useProjectCatalog(projectId, sessionId, !sessions.isLoading);
  const openFile = useWorkspacePanel((state) => state.openFile);

  const grouped = useMemo(
    () => groupByTier(catalog.data?.entries ?? []),
    [catalog.data],
  );

  return (
    <WorkspacePanelHost projectId={projectId} mainClassName="project-outputs-main">
      {catalog.isLoading && !catalog.data ? (
        <p className="muted">{t({ zh: "正在读这个项目的产出…", en: "Reading this project's outputs…" })}</p>
      ) : catalog.error ? (
        <p className="muted">读不到研究产出：{String((catalog.error as Error).message)}</p>
      ) : (catalog.data?.entries.length ?? 0) === 0 ? (
        <p className="muted">{t({ zh: "这个项目还没有产出 —— agent 冻结第一件产物之后会出现在这里。", en: "This project has no outputs yet — they appear once the agent freezes the first one." })}</p>
      ) : (
        <div className="project-outputs">
          {TIER_ORDER.map((tier) => (
            <TierSection
              key={tier}
              tier={tier}
              entries={grouped[tier]}
              onOpenFile={(path) => openFile({ projectId, sessionId, path })}
            />
          ))}
        </div>
      )}
    </WorkspacePanelHost>
  );
}

function TierSection({
  tier,
  entries,
  onOpenFile,
}: {
  tier: CatalogTier;
  entries: CatalogEntry[];
  onOpenFile: (path: string) => void;
}) {
  const t = useT();
  if (entries.length === 0 && tier === "working") return null;
  // 交付物默认展开：那是用户来这一页要看的东西。工作过程默认收起。
  const open = tier !== "working";
  return (
    <details className={`project-outputs-tier is-${tier}`} open={open}>
      <summary>
        <span>{t(TIER_LABEL[tier])}</span>
        <small>{entries.length}</small>
      </summary>
      <p className="project-outputs-hint">{t(TIER_HINT[tier])}</p>
      {entries.length === 0 ? (
        <p className="muted">{t({ zh: "这一档还是空的。", en: "This tier is empty." })}</p>
      ) : (
        <ul className="project-outputs-list">
          {entries.map((entry) => (
            <OutputRow key={entry.recordPath} entry={entry} onOpenFile={onOpenFile} />
          ))}
        </ul>
      )}
    </details>
  );
}

function OutputRow({
  entry,
  onOpenFile,
}: {
  entry: CatalogEntry;
  onOpenFile: (path: string) => void;
}) {
  const t = useT();
  const lang = useLanguage();
  const Icon = iconFor(entry);
  const kind = artifactKindLabel(entry.kind, lang);
  return (
    <li className="project-outputs-row">
      <Icon size={14} />
      <div className="project-outputs-identity">
        <strong>{entry.name}</strong>
        <small>
          {kind} · v{entry.version}
          {entry.ownerNode ? ` · ${entry.ownerNode}` : ""}
          {entry.frozen ? t({ zh: " · 已冻结", en: " · frozen" }) : ""}
        </small>
      </div>
      {entry.frozen && <Snowflake size={12} className="project-outputs-frozen" />}
      <div className="project-outputs-files">
        {/* 点得开的东西排在前面（后端已按可呈现性排过序）。记录本身放最后 ——
            用户要的是论文，不是那份 JSON 信封。 */}
        {entry.files.map((path) => (
          <button key={path} type="button" onClick={() => onOpenFile(path)} title={path}>
            {path.split("/").pop()}
          </button>
        ))}
        <button
          type="button"
          className="is-record"
          onClick={() => onOpenFile(entry.recordPath)}
          title={entry.recordPath}
        >{t({ zh: "记录", en: "Record" })}</button>
      </div>
    </li>
  );
}

"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowUpRight, FileText, Package, RefreshCw, Search } from "lucide-react";
import type { CatalogEntry } from "@/lib/api";
import { useLanguage, useT } from "@/shared/i18n";
import { useProjectCatalog } from "../hooks/useCatalog";
import { artifactKindLabel } from "../lib/kind-label";
import { outputPaths, sessionOutputs } from "../lib/session-outputs";

export function SessionOutputsPanel({ projectId, sessionId, onOpenFile, active = true }: {
  projectId: string; sessionId: string; onOpenFile: (path: string) => void; active?: boolean;
}) {
  const t = useT();
  const catalog = useProjectCatalog(projectId, sessionId, active);
  const [query, setQuery] = useState("");
  const [finalizedOnly, setFinalizedOnly] = useState(false);
  const entries = catalog.data?.entries ?? [];
  const { primary, figures, data, other } = sessionOutputs(finalizedOnly ? entries.filter((entry) => entry.frozen) : entries, query);
  const filtered = Boolean(query.trim()) || finalizedOnly;
  return (
    <section className="session-outputs-panel" aria-label={t({ zh: "会话产物", en: "Session outputs" })}>
      <header className="session-outputs-heading">
        <div><h2>{t({ zh: "产物", en: "Outputs" })}</h2>
          <p>{t({ zh: "本会话可见的成果，包含项目已有产物。", en: "Outputs visible to this session, including existing project results." })}</p></div>
        <button type="button" onClick={() => void catalog.refetch()} disabled={catalog.isFetching}
          aria-label={t({ zh: "刷新产物", en: "Refresh outputs" })}>
          <RefreshCw size={13} className={catalog.isFetching ? "spin" : ""} />
        </button>
      </header>
      <label className="session-outputs-search"><Search size={13} />
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)}
          aria-label={t({ zh: "搜索产物", en: "Search outputs" })}
          placeholder={t({ zh: "搜索名称或文件…", en: "Search names or files…" })} />
      </label>
      <label className="session-outputs-filter"><input type="checkbox" checked={finalizedOnly}
        onChange={(event) => setFinalizedOnly(event.target.checked)} />
        {t({ zh: "仅已定稿", en: "Finalized only" })}</label>
      <div className="session-outputs-list">
        {catalog.isLoading && <p role="status" className="session-outputs-empty">{t({ zh: "正在加载产物…", en: "Loading outputs…" })}</p>}
        {catalog.isError && <div role="alert" className="session-outputs-error">
          <p>{t({ zh: "无法加载产物", en: "Could not load outputs" })}</p>
          <button type="button" onClick={() => void catalog.refetch()}>{t({ zh: "重试", en: "Retry" })}</button>
        </div>}
        {catalog.data && <>
          <section className="session-outputs-group" aria-label={t({ zh: "主要产物", en: "Main outputs" })}>
            <h3>{t({ zh: "主要产物", en: "Main outputs" })}<span>{primary.length}</span></h3>
            {primary.length > 0 ? <ul>{primary.map((entry) => <OutputCard key={entry.artifactId || entry.recordPath} entry={entry} onOpenFile={onOpenFile} />)}</ul>
              : <div className="session-outputs-empty"><Package size={20} /><p>{filtered
                ? t({ zh: "没有匹配的主要产物。", en: "No matching main outputs." })
                : t({ zh: "还没有论文、报告或方案。生成后会显示在这里。", en: "No papers, reports or plans yet. They will appear here when created." })}</p></div>}
          </section>
          {[
            { id: "figures", label: t({ zh: "图表", en: "Figures" }), entries: figures },
            { id: "data", label: t({ zh: "结果数据", en: "Result data" }), entries: data },
            { id: "other", label: t({ zh: "其他成果", en: "Other outputs" }), entries: other },
          ].filter((group) => group.entries.length > 0).map((group) => <details key={group.id}
            className="session-outputs-group session-outputs-other" open={filtered || undefined}>
            <summary>{group.label}<span>{group.entries.length}</span></summary>
            <ul>{group.entries.map((entry) => <OutputCard key={entry.artifactId || entry.recordPath} entry={entry} onOpenFile={onOpenFile} />)}</ul>
          </details>)}
        </>}
      </div>
      <footer><Link href={`/projects/${encodeURIComponent(projectId)}/outputs`}>
        {t({ zh: "查看项目全部成果", en: "View all project outputs" })}<ArrowUpRight size={12} /></Link>
        <small>{t({ zh: "日志、快照等过程文件可在全部成果中查看。", en: "Logs, snapshots and working files are available in all outputs." })}</small>
      </footer>
    </section>
  );
}

function OutputCard({ entry, onOpenFile }: { entry: CatalogEntry; onOpenFile: (path: string) => void }) {
  const t = useT();
  const lang = useLanguage();
  const paths = outputPaths(entry);
  const primary = paths[0];
  const kind = artifactKindLabel(entry.kind, lang);
  const date = new Date(entry.frozen ? entry.frozenAt : entry.createdAt);
  const dateLabel = Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(lang === "zh" ? "zh-CN" : "en-US");
  const fileLabel = (path: string) => path === entry.recordPath && entry.files.length > 0
    ? t({ zh: "产物记录", en: "Output record" }) : path.split("/").pop() || path;
  return <li className="session-output-card">
    <h4>{entry.name || kind}</h4>
    <p className="session-output-meta"><span>{kind} · v{entry.version}</span>
      <span className={`session-output-status${entry.frozen ? " is-finalized" : ""}`}>{entry.frozen
        ? t({ zh: "已定稿", en: "Finalized" }) : t({ zh: "未定稿", en: "Not finalized" })}</span>
      {dateLabel && <time dateTime={date.toISOString()} title={entry.frozen
      ? t({ zh: "定稿时间", en: "Finalized" }) : t({ zh: "创建时间", en: "Created" })}>{dateLabel}</time>}</p>
    {primary ? <button type="button" className="session-output-open" title={primary} onClick={() => onOpenFile(primary)}>
      <FileText size={13} /><span>{fileLabel(primary)}</span><ArrowUpRight size={12} />
    </button> : <p className="session-output-unavailable">{t({ zh: "暂无可打开的文件", en: "No file available to open" })}</p>}
    {paths.length > 1 && <details className="session-output-files"><summary>{t({ zh: "更多文件（{count}）", en: "More files ({count})" }, { count: paths.length - 1 })}</summary>
      {paths.slice(1).map((path) => <button key={path} type="button" onClick={() => onOpenFile(path)} title={path}>
        <FileText size={12} /><span>{fileLabel(path)}</span></button>)}
    </details>}
  </li>;
}

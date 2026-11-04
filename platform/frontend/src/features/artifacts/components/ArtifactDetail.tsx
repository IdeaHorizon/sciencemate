"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, Trash2 } from "lucide-react";
import { Skeleton } from "@/shared/ui";
import { artifactIcon } from "@/shared/icons/artifactIcons";
import { formatRelativeTime } from "@/shared/format/dates";
import { fmtFieldName } from "@/shared/format/text";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { pushSuccess, pushError } from "@/stores/notification";
import type { Artifact } from "@/lib/api";
import { useProject } from "@/features/projects/hooks/useProjects";
import { publishedRevisionId, visibleArtifactMetadata } from "../lib/artifact-presentation";
import { useArtifactContent, useProjectArtifactsList } from "../hooks/useArtifacts";
import { FrozenLockBadge } from "./FrozenLockBadge";
import { artifactKindLabel } from "@/features/catalog/lib/kind-label";
import { useT, useLanguage, say, type Language } from "@/shared/i18n";

export function ArtifactDetail({ projectId, artifactId }: { projectId: string; artifactId: string }) {
  const t = useT();
  const lang = useLanguage();
  const queryClient = useQueryClient();
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const projectQuery = useProject(projectId);
  const canDelete = projectQuery.data?.capabilities?.includes("publish_changes") ?? false;
  const artifactsQuery = useProjectArtifactsList(projectId);
  const { data: artifacts, isLoading: artifactsLoading } = artifactsQuery;
  const artifact: Artifact | undefined = artifacts?.find((item) => item.id === artifactId);
  const contentQuery = useArtifactContent(projectId, artifactId);
  const { data: content, isLoading: contentLoading } = contentQuery;

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteArtifact(projectId, artifactId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.artifacts(projectId) });
      window.history.back();
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "删除失败", en: "Delete failed" })),
  });

  if (!artifact) {
    return (
      <div className="artifact-document-page">
        {artifactsLoading ? <Skeleton height={160} rounded="sm" /> : (
          <div className="artifact-document-unavailable" role="alert">
            <span>{t({ zh: "无法打开这个成果", en: "Artifact unavailable" })}</span>
            <p>{artifactsQuery.error instanceof Error
              ? artifactsQuery.error.message
              : t({ zh: "当前项目中找不到这个成果。", en: "This artifact can't be found in the current Project." })}</p>
            <Link href={`/projects/${encodeURIComponent(projectId)}/outputs`}>{t({ zh: "返回研究成果", en: "Back to research outputs" })}</Link>
          </div>
        )}
      </div>
    );
  }

  const Icon = artifactIcon(artifact.type);
  const metadata = visibleArtifactMetadata(artifact);
  const revisionId = publishedRevisionId(artifact);
  const frozen = artifact.extra_data?.frozen === true;

  return (
    <main className="artifact-document-page">
      {/* 回「研究成果」而不是回一个跨项目的 Artifacts 列表：产出永远是某个
          项目的产出，而那个跨项目列表读的是 artifacts 表（只有 publish 过的行），
          和工作区里真实的产物各说各话。 */}
      <Link href={`/projects/${encodeURIComponent(projectId)}/outputs`} className="artifact-document-back">
        <ChevronLeft size={13} />{t({ zh: "研究成果", en: "Research outputs" })}</Link>

      <header className="artifact-document-header">
        <div className="artifact-document-title">
          <Icon size={18} />
          <div>
            <h1>{artifact.name}</h1>
            <p>
              {/* 类型用成果类型表的显示名；scope 只有 project / library 两种，只在
                  library（外部资料）时值得说一声。 */}
              <span>{artifactKindLabel(artifact.type, lang)}</span>
              {artifact.scope === "library" && <span>{t({ zh: "外部资料", en: "Library" })}</span>}
              <span>{t({ zh: "版本 {n}", en: "version {n}" }, { n: artifact.current_version })}</span>
              {revisionId && <span>{t({ zh: "已发布版本", en: "published revision" })}<code title={revisionId}>{revisionId}</code></span>}
              <span>{t({ zh: "{time}更新", en: "updated {time}" }, { time: formatRelativeTime(artifact.updated_at, lang) })}</span>
              <FrozenLockBadge extraData={artifact.extra_data} />
            </p>
          </div>
        </div>
        {canDelete && !frozen && !confirmingDelete && (
          <button type="button" className="artifact-delete-trigger" onClick={() => setConfirmingDelete(true)}>
            <Trash2 size={12} /> {t({ zh: "删除", en: "Delete" })}
          </button>
        )}
      </header>

      {artifact.description && <p className="artifact-document-description">{artifact.description}</p>}

      {confirmingDelete && (
        <div className="artifact-delete-confirmation" role="alert">
          <span><strong>{t({ zh: "删除这个成果？", en: "Delete this artifact?" })}</strong><small>{t({ zh: "这个成果的所有版本都会被删除。", en: "All versions of this artifact will be deleted." })}</small></span>
          <button type="button" onClick={() => setConfirmingDelete(false)} disabled={deleteMutation.isPending}>{t({ zh: "取消", en: "Cancel" })}</button>
          <button type="button" onClick={() => deleteMutation.mutate()} disabled={deleteMutation.isPending}>
            {deleteMutation.isPending ? t({ zh: "正在删除…", en: "Deleting…" }) : t({ zh: "删除", en: "Delete" })}
          </button>
        </div>
      )}

      {frozen && (
        <p className="artifact-frozen-note">
          {t({ zh: "这个成果已定稿，不能修改或删除。", en: "This artifact is finalized and can't be edited or deleted." })}
        </p>
      )}

      {metadata.length > 0 && (
        <details className="artifact-document-metadata">
          <summary>{t({ zh: "元信息", en: "Metadata" })}<span>{t({ zh: "{count} 项", en: "{count} fields" }, { count: metadata.length })}</span></summary>
          <dl>
            {metadata.slice(0, 12).map(([key, value]) => (
              <div key={key}><dt>{fmtFieldName(key)}</dt><dd>{formatValue(value, lang)}</dd></div>
            ))}
          </dl>
        </details>
      )}

      <article className="artifact-document-content">
        <header>
          <span>{t({ zh: "正文", en: "Document" })}</span>
          {content && <small>{t({ zh: "{n} 字符", en: "{n} characters" }, { n: (content.content ?? "").length.toLocaleString() })}{content.mime_type ? ` · ${content.mime_type}` : ""}</small>}
        </header>
        {contentLoading && !content && <Skeleton height={220} rounded="sm" />}
        {contentQuery.isError && (
          <div className="artifact-content-error" role="alert">
            <strong>{t({ zh: "无法加载正文", en: "Document content unavailable" })}</strong>
            <span>{contentQuery.error instanceof Error ? contentQuery.error.message : t({ zh: "请稍后重试。", en: "Please try again later." })}</span>
          </div>
        )}
        {content && <pre>{content.content || t({ zh: "（内容为空）", en: "(empty)" })}</pre>}
      </article>
    </main>
  );
}

function formatValue(value: unknown, lang: Language): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value.length > 140 ? `${value.slice(0, 139)}…` : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return say({ zh: "{n} 项", en: "{n} items" }, lang, { n: value.length });
  return "{…}";
}

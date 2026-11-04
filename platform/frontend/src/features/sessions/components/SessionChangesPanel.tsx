"use client";

import { useState } from "react";
import { AlertTriangle, ChevronDown, GitBranch, GitCompareArrows, GitPullRequest } from "lucide-react";
import type {
  ResearchSession,
  RevisionOperation,
  RevisionResourceType,
  SessionChangeSet,
  SessionMergeConflict,
} from "../types";
import {
  openSessionConflicts,
  sessionChangeDetailExpanded,
  sessionUnpublishedCount,
  visibleSessionChangePaths,
} from "../lib/session-change-projection";
import { GitDiffViewer } from "@/shared/ui";
import { ContractViolation } from "@/shared/contract-violation";
import { useT, useLanguage, say, type Language, type Phrase } from "@/shared/i18n";

const resourceLabels: Record<RevisionResourceType, Phrase> = {
  artifact: { zh: "成果", en: "Output" },
  project_doc: { zh: "项目文档", en: "Project document" },
  project_config: { zh: "项目设置", en: "Project settings" },
};

// 英文靠复数后缀数数，中文靠量词 —— 两种说法各写一份，不从一份推另一份。
function conflictCountLabel(count: number, lang: Language) {
  return say({ zh: "{count} 处发布冲突", en: "{count} publication conflict{s}" }, lang,
    { count, s: count === 1 ? "" : "s" });
}

function fileCountLabel(count: number, lang: Language) {
  return say({ zh: "{count} 个文件", en: "{count} file{s}" }, lang, { count, s: count === 1 ? "" : "s" });
}

function readyChangeLabel(count: number, lang: Language) {
  return say({ zh: "这个会话修改了 {count} 个文件", en: "{count} file{s} changed in this Session" }, lang,
    { count, s: count === 1 ? "" : "s" });
}

function shortSha(value: string | null | undefined, lang: Language) {
  return value?.slice(0, 8) ?? say({ zh: "未提交", en: "uncommitted" }, lang);
}

function errorMessage(error: unknown, lang: Language) {
  // 对面违约（变更集的形状不对、身份串了）的 message 是写给排查的人的诊断，不照念。
  return error instanceof Error && !(error instanceof ContractViolation)
    ? error.message
    : say({ zh: "无法加载改动，请稍后重试。", en: "Could not load changes. Please try again later." }, lang);
}

function Preview({
  value,
  fallback,
  truncated,
}: {
  value: string | null;
  fallback: string | null;
  truncated: boolean;
}) {
  const t = useT();
  return (
    <div className="session-change-preview">
      {value ? <pre>{value}</pre> : <p>{fallback ?? t({ zh: "预览不可用", en: "Preview unavailable" })}</p>}
      {truncated && <small>{t({ zh: "预览已截断。", en: "Preview truncated." })}</small>}
    </div>
  );
}

function ConflictItem({
  conflict,
  canResolve,
  resolving,
  onResolve,
}: {
  conflict: SessionMergeConflict;
  canResolve: boolean;
  resolving: boolean;
  onResolve: (conflictId: string, choice: "use_project" | "use_proposed") => void;
}) {
  const t = useT();
  return (
    <article className="session-conflict-item">
      <header>
        <span>{t(resourceLabels[conflict.resourceType])}</span>
        <strong>{conflict.resourceKey}</strong>
      </header>

      <details className="session-conflict-base">
        <summary>{t({ zh: "显示修改前的版本", en: "Show the original version" })}</summary>
        <Preview
          value={conflict.basePreview}
          fallback={conflict.baseVersionId}
          truncated={conflict.previewTruncated.base}
        />
      </details>

      <div className="session-conflict-comparison">
        <section>
          <span>{t({ zh: "项目中的版本", en: "Project version" })}</span>
          <Preview
            value={conflict.theirsPreview}
            fallback={conflict.projectVersionId}
            truncated={conflict.previewTruncated.theirs}
          />
          {canResolve && (
            <button
              type="button"
              disabled={resolving}
              onClick={() => onResolve(conflict.id, "use_project")}
            >{t({ zh: "保留项目中的版本", en: "Keep Project version" })}</button>
          )}
        </section>
        <section>
          <span>{t({ zh: "这个会话的版本", en: "Session version" })}</span>
          <Preview
            value={conflict.oursPreview}
            fallback={conflict.proposedVersionId}
            truncated={conflict.previewTruncated.ours}
          />
          {canResolve && (
            <button
              type="button"
              disabled={resolving}
              onClick={() => onResolve(conflict.id, "use_proposed")}
            >{t({ zh: "使用这个会话的版本", en: "Use Session version" })}</button>
          )}
        </section>
      </div>
      {!canResolve && <small className="session-conflict-permission">{t({ zh: "解决这个冲突需要审阅权限。", en: "Reviewer permission is required to resolve this conflict." })}</small>}
    </article>
  );
}

export function SessionChangesPanel({
  session,
  changeSet,
  conflicts,
  loading,
  error,
  canPublish,
  canResolve,
  publishing,
  resolving,
  onPublish,
  onResolve,
  initiallyExpanded = false,
  requested = false,
}: {
  session: ResearchSession;
  changeSet: SessionChangeSet | null | undefined;
  conflicts: SessionMergeConflict[] | undefined;
  loading: boolean;
  error: unknown;
  canPublish: boolean;
  canResolve: boolean;
  publishing: boolean;
  resolving: boolean;
  onPublish: () => void;
  onResolve: (conflictId: string, choice: "use_project" | "use_proposed") => void;
  /** 按需挂载（点 changes chip）时直接展开 —— 用户点它就是要看内容。 */
  initiallyExpanded?: boolean;
  /**
   * 是人**主动点开**的（而不是因为有冲突自动挂上来的）。
   *
   * 空的时候也要给个回答：原来 0 改动 0 冲突直接 `return null`，于是点了
   * 分支 chip 屏幕上什么都不发生（wangd 2026-08-20：「那个 worktree 的符号
   * 现在点了也没反应啊？？」）。问了就得答，"没有未发布的改动"也是答案。
   */
  requested?: boolean;
}) {
  const t = useT();
  const lang = useLanguage();
  const openConflicts = openSessionConflicts(conflicts ?? []);
  const visibleChangePaths = visibleSessionChangePaths(changeSet?.changedPaths ?? [], conflicts ?? []);
  const changeCount = sessionUnpublishedCount(changeSet, session.unpublishedChangeCount);
  const conflictCount = openConflicts.length || session.conflictCount;
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const detailExpanded = sessionChangeDetailExpanded(expanded, conflictCount);

  if (!loading && !error && changeCount === 0 && conflictCount === 0) {
    if (!requested) return null;
    return (
      <section className="session-changes-panel is-empty" aria-label={t({ zh: "这个会话中待发布的改动", en: "Session changes awaiting publication" })}>
        <div className="session-changes-inner">
          <div className="session-changes-summary">
            <span className="session-changes-empty">
              <GitCompareArrows size={13} />
              <span>
                <strong>{t({ zh: "没有未发布的改动", en: "No unpublished changes" })}</strong>
                <small>{t({ zh: "与项目最新版本一致", en: "Matches the latest project version" })}</small>
              </span>
            </span>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className={`session-changes-panel${conflictCount > 0 ? " has-conflicts" : ""}`} aria-label={t({ zh: "这个会话中待发布的改动", en: "Session changes awaiting publication" })}>
      <div className="session-changes-inner">
        <div className="session-changes-summary">
          <button
            type="button"
            className="session-changes-toggle"
            aria-expanded={detailExpanded}
            aria-disabled={conflictCount > 0}
            onClick={() => {
              if (conflictCount === 0) setExpanded((current) => !current);
            }}
          >
            {conflictCount > 0 ? <AlertTriangle size={13} /> : <GitCompareArrows size={13} />}
            <span>
              <strong>{conflictCount > 0 ? conflictCountLabel(conflictCount, lang) : readyChangeLabel(changeCount, lang)}</strong>
              <small>
                {conflictCount > 0
                  ? t({ zh: "项目和这个会话修改了同一项内容", en: "The project and this session changed the same item" })
                  : t({ zh: "实时改动", en: "Live changes" })}
              </small>
            </span>
            <ChevronDown size={13} className={detailExpanded ? "is-expanded" : ""} />
          </button>
          {canPublish && (
            <button type="button" className="session-publish-action" disabled={publishing} onClick={onPublish}>
              <GitPullRequest size={12} /> {publishing ? t({ zh: "正在发布…", en: "Publishing…" }) : t({ zh: "发布改动", en: "Publish changes" })}
            </button>
          )}
        </div>

        {error ? <p className="session-changes-error" role="alert">{errorMessage(error, lang)}</p> : null}

        {detailExpanded && (
          <div className="session-changes-detail">
            {/* 分支全名、提交号、领先 / 落后几个提交：排查用的信息，默认收起。 */}
            {changeSet?.gitBranch && (
              <details className="session-git-details">
                <summary>{t({ zh: "版本详情", en: "Version details" })}</summary>
                <div className="session-git-summary">
                  <span><GitBranch size={12} /><code>{changeSet.gitBranch}</code></span>
                  <span><code>{shortSha(changeSet.gitBaseCommitSha, lang)}</code> → <code>{shortSha(changeSet.gitHeadCommitSha, lang)}</code></span>
                  <span>{t({ zh: "领先 {ahead} · 落后 {behind}", en: "{ahead} ahead · {behind} behind" }, { ahead: changeSet.aheadBy, behind: changeSet.behindBy })}</span>
                  <span className="session-git-stats"><b>+{changeSet.additions}</b><i>−{changeSet.deletions}</i> · {fileCountLabel(changeSet.filesChanged, lang)}</span>
                </div>
              </details>
            )}
            {conflictCount > 0 && (
              <p className="session-conflict-explanation">
                {t({ zh: "两边的内容都还在。发布前，请为每一项选择保留项目中的版本还是这个会话的版本。", en: "Nothing has been overwritten. Before publishing, choose the project version or the session version for each item." })}
              </p>
            )}

            {openConflicts.map((conflict) => (
              <ConflictItem
                key={conflict.id}
                conflict={conflict}
                canResolve={canResolve}
                resolving={resolving}
                onResolve={onResolve}
              />
            ))}

            {changeSet?.patch && <GitDiffViewer patch={changeSet.patch} truncated={changeSet.patchTruncated} />}

            {/* 有补丁就看补丁；没有（太大被截断、或纯二进制）时至少把动过的
                文件列出来。正文不再有第二份预览抄件 —— 内容的真相源是 git。 */}
            {!changeSet?.patch && visibleChangePaths.map((path) => (
              <div className="session-change-item session-change-path" key={path}>
                <GitBranch size={12} />
                <strong>{path}</strong>
              </div>
            ))}

            {loading && !changeSet && openConflicts.length === 0 && <p className="session-changes-loading">{t({ zh: "正在加载改动…", en: "Loading changes…" })}</p>}

          </div>
        )}
      </div>
    </section>
  );
}

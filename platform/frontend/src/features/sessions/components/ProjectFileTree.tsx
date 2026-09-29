"use client";

import { useMemo } from "react";
import { FileText, Folder } from "lucide-react";

import { useSessionProjectTree } from "../hooks/useSessions";
import type { ProjectFileEntry, ProjectFileTree as ProjectFileTreeData } from "../types";
import { useT } from "@/shared/i18n";

/**
 * Project 仓库里有哪些文件 —— **展开一层，取一层**。
 *
 * ## 为什么不再一次拿整棵树（2026-09-09 实测）
 *
 * 原来这里拿的是一个平坦列表，后端按路径字母序切前 5000 条。qinp 的 Podsys
 * 论文项目有 9171 个文件（`.research/` 4542 + `observation/extracted/` 4520），
 * 那两个目录就把配额吃光 —— 字母序排在 `o` 后面的每一个顶层目录整个消失，
 * 包括用户当时正在找的那篇论文所在的 `paper/`。而界面显示的是
 * "5000 Project files"：**"我只拿到这么多"被显示成了"一共就这么多"**。
 *
 * 一层一层取之后，"整个目录凭空消失"在构造上不可能发生：每一层的目录行都在，
 * 展开才要钱。单层仍可能到顶（一个目录直接塞了几千个文件），那时后端会给
 * `truncated` / `totalEntries`，界面**必须**把它说出来 —— 见 `LevelNotice`。
 *
 * ## 为什么数据取在这里而不是由外面传进来
 *
 * 懒加载的单位是"一层"，而"哪一层展开着"是这棵树自己的状态。外面传 `tree`
 * 就等于要求调用方替每一层各建一个 query，那份状态会分裂成两处。
 */

/**
 * 平台自己的记账，不是研究产出。
 *
 * 实测（2026-08-12，真实项目）：73 个文件里 **23 个**是 `.history/`（每次改写
 * 存一份带时间戳的副本）和冻结登记表。它们和 `survey_report__*.md`
 * 平铺在一起，真正的产出被淹掉（2026-09-12 起事实进 `.research/ledger/`，
 * 节点目录里只剩原生文件）。
 *
 * 判决**由后端给**（`_is_bookkeeping`），这里不再拿路径形状猜。原来那条
 * "路径里有点开头的段就是记账"只错了一个地方，而那个地方恰好是用户
 * 2026-09-09 找不到的那篇论文：调度器的工作区当时是 `.research/orchestration`，
 * 它产出的每一样东西整片被折了起来（2026-09-12 起它写 `notes/`）。"哪些目录是
 * 节点工作区"在后端已经有真相源，界面自己再判一次就是第二份会分叉的判据。
 * 根这一层的顺序同理由后端给（`_PIPELINE_ORDER`）—— 这里曾有一份 ROOT_ORDER
 * 名单，漏了后加的两个节点，2026-09-12 删除。
 *
 * 注意这里是**分区**不是**隐藏**：它们照样列出来，只是默认收起。看不见的
 * 东西没法被审计。
 */
function isPlatformBookkeeping(entry: ProjectFileEntry) {
  return entry.bookkeeping;
}

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export function ProjectFileTree({
  projectId,
  sessionId,
  onOpenFile,
}: {
  projectId: string;
  sessionId: string;
  /**
   * 点一个文件 = 在右栏打开它。没传就退回成不可点的纯列表 —— 这棵树不知道
   * 也不该知道"右栏"是什么，它只负责报告"点了哪个路径"。
   */
  onOpenFile?: (path: string) => void;
}) {
  const t = useT();
  // 没有会话时问项目主干 —— 开关是 projectId，不是 sessionId。
  const root = useSessionProjectTree(projectId, sessionId, "", !!projectId);

  if (root.isLoading && !root.data) {
    return <p className="muted">{t({ zh: "正在加载 Git 工作区…", en: "Loading Git worktree…" })}</p>;
  }
  if (root.data && root.data.entries.length === 0) {
    return <p className="muted">{t({ zh: "这个工作区还没有文件 —— agent 产出东西之后会出现在这里。", en: "This workspace has no files yet — they appear once the agent produces something." })}</p>;
  }

  return (
    <section className="project-files-panel" aria-label={t({ zh: "项目 Git 文件", en: "Project Git files" })}>
      <p className="project-files-count">
        {root.data?.totalFiles ?? 0} Project files
      </p>
      <div className="project-files-tree">
        <Level
          projectId={projectId}
          sessionId={sessionId}
          path=""
          listing={root.data}
          loading={root.isLoading}
          onOpenFile={onOpenFile}
        />
      </div>
    </section>
  );
}

/**
 * 一层：目录行（可展开）在前，文件行在后，平台记账单独分区。
 *
 * 根这一层的数据由上面传进来（它已经取过了），更深的层自己取 —— `enabled`
 * 跟着挂载：一个目录没被展开，它那层的请求就不会发出去。
 */
function Level({
  projectId,
  sessionId,
  path,
  listing: given,
  loading: givenLoading,
  onOpenFile,
}: {
  projectId: string;
  sessionId: string;
  path: string;
  listing?: ProjectFileTreeData;
  loading?: boolean;
  onOpenFile?: (path: string) => void;
}) {
  const t = useT();
  const fetched = useSessionProjectTree(projectId, sessionId, path, !given && !!projectId);
  const listing = given ?? fetched.data;
  const loading = given ? givenLoading : fetched.isLoading;

  const [outputs, bookkeeping] = useMemo(() => {
    const all = listing?.entries ?? [];
    return [
      all.filter((entry) => !isPlatformBookkeeping(entry)),
      all.filter((entry) => isPlatformBookkeeping(entry)),
    ];
  }, [listing]);

  if (loading && !listing) {
    return <p className="muted">Loading…</p>;
  }

  // 顺序由后端给（跟着节点表走）—— 前端再排一次就是第二份会分叉的名单，
  // 而那份名单漏了 derivation / observation 两个后加的节点（真机看出来的）。
  return (
    <>
      <EntryList
        entries={outputs}
        projectId={projectId}
        sessionId={sessionId}
        onOpenFile={onOpenFile}
      />
      {bookkeeping.length > 0 && (
        <details className="project-files-bookkeeping">
          <summary>
            <span>{t({ zh: "平台记账", en: "Platform bookkeeping" })}</span>
            <small>{bookkeeping.length} 个（`.history/` 等，不是研究产出）</small>
          </summary>
          <EntryList
            entries={bookkeeping}
            projectId={projectId}
            sessionId={sessionId}
            onOpenFile={onOpenFile}
          />
        </details>
      )}
      {listing && <LevelNotice listing={listing} />}
    </>
  );
}

/**
 * 这一层到顶了就说出来。
 *
 * 这条**不是**装饰：静默截断正是 2026-09-09 那个 bug 的本体 —— 少给了而不说，
 * 用户和 agent 都只能猜界面是不是折叠了。
 */
function LevelNotice({ listing }: { listing: ProjectFileTreeData }) {
  if (!listing.truncated) return null;
  return (
    <p className="project-files-truncated">
      这一层共 {listing.totalEntries} 项，只列出了前 {listing.entries.length} 项。
    </p>
  );
}

function EntryList({
  entries,
  projectId,
  sessionId,
  onOpenFile,
}: {
  entries: ProjectFileEntry[];
  projectId: string;
  sessionId: string;
  onOpenFile?: (path: string) => void;
}) {
  const t = useT();
  return (
    <ul>
      {entries.map((entry) =>
        entry.kind === "directory" ? (
          <li key={entry.path} className={`is-${entry.status}`}>
            <details>
              <summary>
                <Folder size={12} />
                <code>{entry.name}</code>
                <small>
                  {entry.fileCount} 个文件 · {formatBytes(entry.sizeBytes)}
                  {entry.changedCount ? ` · ${entry.changedCount} live` : ""}
                </small>
              </summary>
              {/* 挂载即取数：没展开的目录一个请求都不发。 */}
              <Level
                projectId={projectId}
                sessionId={sessionId}
                path={entry.path}
                onOpenFile={onOpenFile}
              />
            </details>
          </li>
        ) : (
          <li key={entry.path} className={`is-${entry.status}`}>
            <FileText size={12} />
            {/* 能打开的时候是 button 而不是加了 onClick 的 li：键盘要能 Tab 到、
                回车要能触发。可点的东西长得像可点的东西，这一条靠 CSS 保证。 */}
            {onOpenFile ? (
              <button
                type="button"
                className="project-files-open"
                onClick={() => onOpenFile(entry.path)}
                title={t({ zh: `在右栏打开 ${entry.path}`, en: `Open ${entry.path} in the side panel` })}
              >
                <code>{entry.name}</code>
              </button>
            ) : (
              <code>{entry.name}</code>
            )}
            <small>
              {entry.owner} · {entry.status}
              {entry.missing ? t({ zh: " · 字节不在这个工作区", en: " · bytes are not in this workspace" }) : ""}
            </small>
          </li>
        ),
      )}
    </ul>
  );
}

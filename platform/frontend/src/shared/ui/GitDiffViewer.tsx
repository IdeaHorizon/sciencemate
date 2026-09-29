"use client";

import { FileDiff } from "lucide-react";
import { parseGitDiff } from "@/shared/format/git-diff";
import { useT } from "@/shared/i18n";

/** 每文件的权威统计 —— 由产出方给，不从 patch 数。
 *
 *  patch 可能被截断，数出来的 ± 就会比真实值小；而 "Created" 与 "Edited" 是
 *  两句不同的话，`-0` 两边都成立，猜不出来。两个问题两份数据。 */
export type GitDiffStat = {
  additions?: number;
  deletions?: number;
  status?: "added" | "modified" | "deleted" | "renamed" | string;
};

const statusLabels: Record<string, string> = {
  added: "Created",
  deleted: "Deleted",
  renamed: "Renamed",
};

export function GitDiffViewer({
  patch,
  truncated,
  stats,
  compact = false,
}: {
  patch: string;
  truncated: boolean;
  /** path → ±行数/状态。给了就渲染在文件头上。 */
  stats?: Record<string, GitDiffStat>;
  /** 内联在对话流里的紧凑形态：更小的字号，超长自己滚，不把消息撑成一堵墙。 */
  compact?: boolean;
}) {
  const t = useT();
  const files = parseGitDiff(patch);
  if (files.length === 0) return null;

  /* 紧凑形态里把文件头之前的 Git 管道行（index / --- / +++ / new file mode）
     收起来：路径和状态已经在标题栏上了，四行 plumbing 挡在六行正文前面，读者
     要找的东西反而更远。判据用结构而不是名单 —— "第一个 @@ 之前的 meta 行"。
     没有任何 hunk 的文件（二进制、纯改名）那几行就是它**全部**的证据，留着。 */
  const visibleLines = (file: (typeof files)[number]) => {
    const body = file.lines.filter((line) => line.kind !== "header");
    if (!compact) return body;
    const firstHunk = body.findIndex((line) => line.kind === "hunk");
    if (firstHunk < 0) return body;
    return body.filter((line, index) => index > firstHunk || line.kind !== "meta");
  };

  return (
    <div className={`git-diff${compact ? " is-compact" : ""}`} aria-label={t({ zh: "Git 改动", en: "Git changes" })}>
      {files.map((file) => {
        const stat = stats?.[file.displayPath];
        return (
          <section className="git-diff-file" key={`${file.oldPath}:${file.newPath}`}>
            <header>
              <FileDiff size={13} />
              <code>{file.displayPath}</code>
              {stat?.status && statusLabels[stat.status] && (
                <em className="git-diff-file-status">{statusLabels[stat.status]}</em>
              )}
              {typeof stat?.additions === "number" && (
                <em className="git-diff-file-add">+{stat.additions}</em>
              )}
              {typeof stat?.deletions === "number" && (
                <em className="git-diff-file-del">-{stat.deletions}</em>
              )}
            </header>
            <div className="git-diff-lines">
              {visibleLines(file).map((line, index) => (
                <div className={`git-diff-line is-${line.kind}`} key={`${index}:${line.content}`}>
                  <span>{line.oldLine ?? ""}</span>
                  <span>{line.newLine ?? ""}</span>
                  <code>{line.content || " "}</code>
                </div>
              ))}
            </div>
          </section>
        );
      })}
      {truncated && <p className="git-diff-truncated">{t({ zh: "服务端截断了 diff。Git 提交本身是完整的。", en: "Diff truncated by the server. The Git commit remains complete." })}</p>}
    </div>
  );
}

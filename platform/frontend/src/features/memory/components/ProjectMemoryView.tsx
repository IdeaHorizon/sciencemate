"use client";

import { useQuery } from "@tanstack/react-query";

import { getProjectRepositoryFile } from "@/features/sessions/api/session-repository";
import { useProjectTruthSession } from "@/features/research-state/hooks/useProjectTruthSession";
import { qk } from "@/lib/query/keys";
import { Badge, Card, Empty, Skeleton } from "@/shared/ui";

import { entryId, parseProjectMemory } from "../lib/project-memory";
import { useT } from "@/shared/i18n";

/**
 * Project Memory —— 直接读 Git 仓库根的 MEMORY.md（v2.1 起的唯一真相源）。
 *
 * 此前 Memory 页只读 DB 的 memory_entries；而 Project v2 下 harness 真正写的
 * 是别处（收敛前甚至写在用户家目录），Git 那份没人写。收敛之后 curator 写
 * Git、DB 降级为检索投影 —— UI 必须看 Git 那份，否则展示的是另一份数据。
 */
export function ProjectMemoryView({ projectId }: { projectId: string }) {
  const t = useT();
  // MEMORY.md 由 curator 在 session 分支上 checkpoint —— 同 Research state，
  // 不带 sessionId 读 main 会在 publish 前显示"还没有记忆"。
  const truth = useProjectTruthSession(projectId);
  const { data, isLoading, error } = useQuery({
    queryKey: [...qk.projectMemory(projectId), truth.sessionId ?? "main"],
    queryFn: () => getProjectRepositoryFile(projectId, "MEMORY.md", truth.sessionId),
    enabled: !truth.isLoading,
  });

  if ((isLoading || truth.isLoading) && !data) return <Skeleton height={140} />;
  if (error) {
    return (
      <Empty
        title={t({ zh: "读不到项目记忆", en: "Project memory is unavailable" })}
        hint={t({ zh: "读不到项目仓库里的 MEMORY.md。", en: "MEMORY.md could not be read from the Project repository." })}
      />
    );
  }

  const sections = parseProjectMemory(data?.content ?? "");
  if (sections.length === 0) {
    return (
      <Empty
        title={t({ zh: "还没有项目记忆", en: "No project memory yet" })}
        hint={t({ zh: "随着决策和发现积累，curator 会把它们写进 MEMORY.md。", en: "The curator writes MEMORY.md as decisions and findings accumulate." })}
      />
    );
  }

  return (
    <div className="memory-list">
      {sections.map((section) => (
        <Card key={section.heading}>
          <Card.Body>
            <div className="memory-row-head">
              <Badge size="sm" kind="info">{section.heading}</Badge>
              <span className="memory-topic">{t({ zh: "MEMORY.md · 跟着 Git 走", en: "MEMORY.md · tracked in Git" })}</span>
            </div>
            {section.entries.map((entry, index) => {
              const id = entryId(entry);
              return (
                <p className="memory-content" key={`${section.heading}-${index}`}>
                  {id && <Badge size="sm" kind="muted">{id}</Badge>} {entry}
                </p>
              );
            })}
          </Card.Body>
        </Card>
      ))}
    </div>
  );
}

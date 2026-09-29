"use client";

import { useMemo } from "react";

import { WorkspacePanelHost } from "@/features/file-preview/components/WorkspacePanelHost";
import { useWorkspacePanel } from "@/stores/workspace-panel";

import { useProjectSessions } from "../hooks/useSessions";
import { ProjectFileTree } from "./ProjectFileTree";

/**
 * 项目级的「一共有哪些文件」。
 *
 * ## 没有会话时看项目主干（2026-09-10 真机看出来的）
 *
 * 这里原来在没有会话时直接说"这个项目还没有会话"，一个文件都不列 —— 而那个
 * 项目的仓库里躺着 371 个文件，「研究产出」页把它们列得好好的。同一个项目
 * 两页各说各话，用户没有任何办法知道哪个是真的。
 *
 * 后端本来就支持不传 sessionId（回落到项目主干），少的只是前端这一步。
 *
 * 从内联面板挪出来的理由（wangd 2026-08-11 试用）：它和 Artifacts /
 * Research state / Memory 同层级，那几个都在左侧栏，只有它内联在会话里，
 * 展开还把对话挤下去。会话里留的是「本轮改了什么」（SessionChangesPanel）——
 * 那才是打开会话想知道的。
 *
 * 树按**最近一个会话**的工作区取：Project = Git 仓库，会话是它的 worktree，
 * 正在进行的工作只在会话分支上看得到。没有会话时后端回落到项目主干。
 */
export function ProjectFilesView({ projectId }: { projectId: string }) {
  const sessionsQuery = useProjectSessions(projectId);
  const sessionId = useMemo(() => {
    const rows = sessionsQuery.data ?? [];
    return rows[0]?.id ?? "";
  }, [sessionsQuery.data]);

  const openFile = useWorkspacePanel((state) => state.openFile);

  return (
    <WorkspacePanelHost projectId={projectId} mainClassName="project-files-main">
      {sessionsQuery.isLoading ? (
        <p className="muted">Loading…</p>
      ) : (
        <ProjectFileTree
          projectId={projectId}
          sessionId={sessionId}
          // 预览必须和树读同一个 worktree。分别去算"哪个会话"的话，两边排序
          // 规则稍有不同就会出现"树里列着、点开 404"，而且看起来像后端的错。
          onOpenFile={(path) => openFile({ projectId, sessionId, path })}
        />
      )}
    </WorkspacePanelHost>
  );
}

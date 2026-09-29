"use client";

import { useEffect } from "react";

import { api } from "@/lib/api";
import { useProjectId } from "@/shared/routing/route-params";

import { ArchivedBanner } from "./ArchivedBanner";
import { ProjectShell } from "./ProjectShell";

/**
 * 路由那一层的 ProjectShell：项目 id 从**地址栏**读。
 *
 * 静态导出下服务端只渲染一次占位段（`/projects/_/…`），那份 HTML 的 RSC 载荷里
 * 参数就是 `_`；`useParams()` 在水合首帧交出来的正是它。地址栏不会骗人。
 */
export function ProjectShellRoute({ children }: { children: React.ReactNode }) {
  const projectId = useProjectId();

  // 正开着哪个项目 —— 本机后端按它决定由谁回答（项目住在哪，它的东西就在哪）。
  //
  // 大多数请求地址里就带着项目 id，用不着这个。兜底是为了**明天新加的那个端点**：
  // 按路径前缀列名单的话，漏掉的那个会默默去问错的机器，而且不报错，只是那一块
  // 数据莫名其妙是空的（`feedback_guardrails_must_scan_not_list`）。
  useEffect(() => {
    api.askingAbout({ project: projectId || null });
    return () => api.askingAbout({ project: null });
  }, [projectId]);

  return (
    <ProjectShell projectId={projectId}>
      <ArchivedBanner projectId={projectId} />
      {children}
    </ProjectShell>
  );
}

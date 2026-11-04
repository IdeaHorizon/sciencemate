"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useProjectId } from "@/shared/routing/route-params";

/**
 * `/projects/<id>` 不是一页，打开就转到「研究」（`/projects/<id>/research`）。
 *
 * 这里曾经是一页「项目总览」：一个「研究对话」链接加一个执行次数。界面上没有任何
 * 入口链到它 —— 项目卡片进的是「研究」，侧栏也没有这一项 —— 只有手敲地址或旧书签
 * 才会落到这里。项目的第一页就是「研究」，这条地址就送人去那里。
 */
export default function ProjectRootRedirect() {
  const router = useRouter();
  const projectId = useProjectId();
  useEffect(() => {
    router.replace(`/projects/${encodeURIComponent(projectId)}/research`);
  }, [router, projectId]);
  return null;
}

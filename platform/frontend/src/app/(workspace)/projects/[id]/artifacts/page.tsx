"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useProjectId } from "@/shared/routing/route-params";

/**
 * 旧地址：这一页搬到了「研究成果」（`/projects/<id>/outputs`），打开就直接转过去。
 *
 * ## 为什么不是留着两页
 *
 * 它和「研究成果」问的是同一个问题——"这个项目产出了什么"——却读不同的东西：
 * 这里读后端 `artifacts` 表（**只有 publish 过的才有行**），那边读工作区里
 * 真实的产物。于是同一篇论文可能这边没有那边有，而分叉不报错，用户只会发现
 * "这个页面里找不到，换个页面又能找到"（wangd 2026-09-09）。
 *
 * ## 为什么转过去而不是删掉
 *
 * 它在左侧导航里挂了很久，存量书签会落到这条地址上。直接删 = 404。之前这里是一块
 * 「此页面已移至…」的指路牌，等于多让人点一下才到；现在直接 `replace` 过去，返回键
 * 也不会再落回这里。
 *
 * 单件成果的详情页 `/projects/<id>/artifacts/<aId>` 不受影响；跨项目的 `/artifacts`
 * 也保留：那问的是另一个问题（"我这些项目里一共有什么"），目录是按项目算的，答不了它。
 */
export default function ProjectArtifactsRedirect() {
  const router = useRouter();
  const projectId = useProjectId();
  useEffect(() => {
    router.replace(`/projects/${encodeURIComponent(projectId)}/outputs`);
  }, [router, projectId]);
  return null;
}

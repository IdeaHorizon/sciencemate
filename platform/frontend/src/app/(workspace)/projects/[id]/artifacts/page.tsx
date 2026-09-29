"use client";

import Link from "next/link";

import { useProjectId } from "@/shared/routing/route-params";
import { useT } from "@/shared/i18n";

/**
 * 这一页搬到了「研究产出」（`/projects/<id>/outputs`）。
 *
 * ## 为什么不是留着两页
 *
 * 它和「研究产出」问的是同一个问题——"这个项目产出了什么"——却读不同的东西：
 * 这里读后端 `artifacts` 表（**只有 publish 过的才有行**），那边读工作区里
 * 真实的产物。于是同一篇论文可能这边没有那边有，而分叉不报错，用户只会发现
 * "这个页面里找不到，换个页面又能找到"（wangd 2026-09-09）。
 *
 * ## 为什么留一块指路牌而不是直接删掉
 *
 * 它在左侧导航里挂了很久，存量书签会落到这条地址上。直接删 = 404，而 404
 * 说不出"东西搬去哪了"。这块牌子**不回答那个问题**（它一条产物都不列），
 * 所以不构成第二个答案 —— 它只负责把人送到唯一那个答案那里去。
 *
 * 跨项目的 `/artifacts` 保留：那问的是另一个问题（"我这些项目里一共有什么"），
 * 目录是按项目算的，答不了它。
 */
export default function ProjectArtifactsMovedPage() {
  const t = useT();
  const projectId = useProjectId();
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">{t({ zh: "这一页改名叫「研究产出」了", en: "This page is now called Research outputs" })}</h1>
          <p className="page-subtitle">{t({ zh: "交付物、研究产出、工作过程都在那里，论文点一下就能打开。", en: "Deliverables, outputs and working files are all there; a paper opens in one click." })}</p>
        </div>
      </header>
      <p>
        <Link href={`/projects/${encodeURIComponent(projectId)}/outputs`}>{t({ zh: "去「研究产出」→", en: "Go to Research outputs →" })}</Link>
      </p>
    </div>
  );
}

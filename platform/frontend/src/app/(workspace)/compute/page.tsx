"use client";

import { ComputeInventoryView } from "@/features/compute";
import { useT } from "@/shared/i18n";

export default function ComputePage() {
  const t = useT();
  return (
    <main className="compute-index-page">
      <header><h1>{t({ zh: "算力", en: "Compute" })}</h1><p>{t({ zh: "可供会话使用的 CPU、内存、存储和 GPU。", en: "CPU, memory, storage and GPUs available to your sessions." })}</p></header>
      {/* 这一页只答「这台机器上有什么」。组织的机器、谁能用什么在「组织 → 算力」
          （`RFC_ORGANISATION_PAGE_20260923` §6：桌面本机没有「组织里有哪些机器」这个问题）。 */}
      <ComputeInventoryView />
    </main>
  );
}

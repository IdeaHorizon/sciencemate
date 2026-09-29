import type { CatalogEntry } from "@/lib/api";

/**
 * 交付物条上那一枚 chip 点下去开哪个文件。
 *
 * 1. 有伴随文件就开第一个。目录已按**可呈现性**排过序（harness 侧
 *    `closing_manifest._presentability_rank`），所以论文拿到的是 PDF 而不是
 *    某个 .tex 碎片 —— 这里不重排。
 * 2. 没有伴随文件就开**产物自己**：预注册、综述、研究计划没有编译产物，正文
 *    就是那个原生文件（markdown / LaTeX / JSON），右栏按格式画。
 *
 * 2026-09-10 之前只有第 1 条，于是交付面上除了论文全是灰的 —— 而对着一份
 * 综述说「没有可打开的文件」是不对的：文件就在盘上，目录里 recordPath 一直
 * 写着它。
 *
 * 目录只放行"落在工作区里、盘上真的存在"的路径（core/catalog），两条来路
 * 都不需要这里再验。
 */
export function openablePathOf(entry: Pick<CatalogEntry, "files" | "recordPath">): string | null {
  return entry.files[0] ?? (entry.recordPath || null);
}

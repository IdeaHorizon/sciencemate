"use client";

import { FileText } from "lucide-react";

import { useProjectCatalog } from "@/features/catalog/hooks/useCatalog";
import { artifactKindLabel } from "@/features/catalog/lib/kind-label";
import { openablePathOf } from "@/features/catalog/lib/openable-path";
import type { CatalogEntry } from "@/lib/api";
import { useT, useLanguage, type Language } from "@/shared/i18n";

/** 类型名和「研究产出」页、右栏信封视图共用一张表：不会各自演化出两个名字。 */
function label(item: CatalogEntry, lang: Language): string {
  const kind = artifactKindLabel(item.kind, lang);
  return item.name ? `${kind} · ${item.name}` : kind;
}

/**
 * 「交付物」—— 这个项目冻结了什么，一点就在右栏打开。
 *
 * ## 为什么需要它
 *
 * 交付链一直是通的（harness 冻结 → promote → 后端 publish 成 Artifact 行），
 * 2026-08-25 那条 run 三件全交付成功。**而前端一个叫"交付物"的地方都没有**：
 * 论文写完了，用户得自己去 Project files 的目录树里翻，还得知道它叫
 * `paper/latex_build/British_Food_Culture_clean/main_clean.pdf`
 * （wangd：「并没有把论文直接显示出来，还得从 Project files 里面去找」）。
 *
 * ## 数据源与「研究产出」页是同一个（2026-09-09）

 * 这里原来读 `/deliverables` —— 那条路由读的是 `<state>/…/deliverables/` 下
 * 的**复制品**，和目录、和 Artifacts 面各是一份答案。同一篇论文因此可能在
 * 一处有、另一处没有，而分叉不报错。现在两边都读 `/catalog`：交付物是什么
 * 由 `core/catalog.py` 一处说了算。
 *
 * ## 口径：项目级，不是"本次交付"
 *
 * `deliverables/` 按 project 分目录，一份冻结产物属于项目而不是某一轮对话。
 * 所以标题就叫「交付物」——名字必须和它回答的问题是同一个。
 *
 * 打不开的条目**照列**（灰着、不可点）：这里回答"冻结了什么"，不是"哪些
 * 文件我恰好能渲染"。少列一条，用户就会以为那份产物不存在。
 *
 * 「打得开」的口径见 `openablePathOf`：有伴随文件（论文的 PDF）给伴随文件，
 * 没有的给**产物自己**的信封 —— 预注册 / 综述的正文就在那份 JSON 里，右栏会
 * 拆开来画。2026-09-10 之前只认伴随文件，于是除了论文全灰着：一块看得见摸不
 * 着的交付面比没有更让人疑惑（wangd：「里面的东西也点不开」）。
 */
export function SessionDeliverables({
  projectId,
  sessionId,
  enabled,
  onOpenFile,
}: {
  projectId: string;
  sessionId: string;
  enabled: boolean;
  onOpenFile: (path: string) => void;
}) {
  const t = useT();
  const lang = useLanguage();
  const query = useProjectCatalog(projectId, sessionId, enabled && !!projectId);
  const items = (query.data?.entries ?? []).filter((entry) => entry.isDeliverable);
  // 一件都没有 = 这个项目还没冻结过任何东西。空条不占地方。
  if (!items.length) return null;

  return (
    <section className="session-deliverables" aria-label={t({ zh: "交付物", en: "Deliverables" })}>
      <h2>
        <FileText size={12} aria-hidden="true" />{t({ zh: "交付物", en: "Deliverables" })}</h2>
      <ul>
        {items.map((item) => {
          const openable = openablePathOf(item);
          return (
            <li key={item.recordPath}>
              {openable ? (
                <button
                  type="button"
                  className="session-deliverable"
                  onClick={() => onOpenFile(openable)}
                  title={t({ zh: `在右栏打开 ${openable}`, en: `Open ${openable} in the side panel` })}
                >
                  {label(item, lang)}
                </button>
              ) : (
                <span
                  className="session-deliverable is-static"
                  title={t({ zh: "这份产物没有可在右栏打开的文件", en: "This output has no file the side panel can open" })}
                >
                  {label(item, lang)}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

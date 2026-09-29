"use client";

import { useMutation } from "@tanstack/react-query";
import { MoonStar } from "lucide-react";
import { skipProjectDreaming } from "@/features/sessions";
import { pushError, pushSuccess } from "@/stores/notification";
import { useT } from "@/shared/i18n";

/**
 * 「跳过本次 KB 整理」—— 项目级动作，所以在项目设置里。
 *
 * 它此前挂在**会话**的 ⋯ 菜单里，可是它调的是 `skipProjectDreaming(projectId)`：
 * 从任何一个会话点它，影响的是整个项目下所有会话的下一次整理。一个动作的作用
 * 域和它所在的位置对不上，用的人就无从知道自己刚刚影响了多大范围。
 */
export function ProjectDreamingPanel({
  projectId,
  canManage,
}: {
  projectId: string;
  canManage: boolean;
}) {
  const t = useT();
  const skip = useMutation({
    mutationFn: () => skipProjectDreaming(projectId),
    onSuccess: () => undefined,
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "跳过失败", en: "The skip failed" })),
  });

  return (
    <section className="settings-compact-section">
      <div className="settings-compact-heading">
        <div>
          <h2>{t({ zh: "知识库整理", en: "Knowledge base tidy-up" })}</h2>
          <p>{t({ zh: "研究间隙自动跑的沉淀过程 —— 把这个项目的产出整理进知识库。", en: "A between-runs pass that files this project's outputs into the knowledge base." })}</p>
        </div>
      </div>
      <div className="settings-row-group">
        <div className="settings-row">
          <span>
            <strong>{t({ zh: "跳过本次整理", en: "Skip this tidy-up" })}</strong>
            <small>
              {canManage
                ? t({ zh: "只跳过排在最前面的这一次；下次触发条件满足时会重新排上。已经沉淀的内容不受影响。", en: "Skips only the one at the front of the queue; it is scheduled again when the trigger next fires. Nothing already filed is affected." })
                : t({ zh: "只有项目负责人或管理员可以跳过整理。", en: "Only a project lead or administrator can skip the tidy-up." })}
            </small>
          </span>
          <button
            type="button"
            className="settings-primary-action"
            disabled={!canManage || skip.isPending}
            onClick={() => skip.mutate()}
          >
            <MoonStar size={14} /> {skip.isPending ? t({ zh: "正在跳过…", en: "Skipping…" }) : t({ zh: "跳过本次", en: "Skip this one" })}
          </button>
        </div>
      </div>
    </section>
  );
}

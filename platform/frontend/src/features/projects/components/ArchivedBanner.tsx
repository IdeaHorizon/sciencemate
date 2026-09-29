"use client";

/**
 * 归档的项目：一进来就说清楚 —— 只能看；后台也不再替它干活。能归档 / 恢复的人（负责人或同组织
 * 管理员，服务器答的 `can_archive`）在这里恢复。没有这一条，归档的项目看上去和平常一样，只是每个
 * 按钮都点不动。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Button } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { useT } from "@/shared/i18n";

export function ArchivedBanner({ projectId }: { projectId: string }) {
  const t = useT();
  const queryClient = useQueryClient();
  const project = useQuery({
    queryKey: qk.project(projectId),
    queryFn: () => api.getProject(projectId),
    enabled: Boolean(projectId),
  });
  const restore = useMutation({
    mutationFn: () => api.restoreProject(projectId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.project(projectId) });
      void queryClient.invalidateQueries({ queryKey: qk.projects() });
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "没恢复成", en: "Could not restore it" })),
  });
  if (project.data?.status !== "archived") return null;
  return (
    <div className="project-archived-banner" role="status">
      <span>
        <strong>{t({ zh: "这个项目已归档", en: "This project is archived" })}</strong>
        <small>{t({ zh: "只能看：不能开新会话、改设置，后台也不再替它干活。知识、会话和产出都在。",
                    en: "Read-only: no new sessions or settings changes, and nothing runs for it in the background. Its knowledge, sessions and outputs are all kept." })}</small>
      </span>
      {project.data.can_archive && (
        <Button size="sm" iconLeft={<RotateCcw size={13} />} disabled={restore.isPending} onClick={() => restore.mutate()}>
          {t({ zh: "恢复", en: "Restore" })}
        </Button>
      )}
    </div>
  );
}

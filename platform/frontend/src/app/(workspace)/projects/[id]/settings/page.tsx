"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Cpu, Database, Users } from "lucide-react";
import { api, type ProjectDetail, type UpdateProjectRequest } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Button, Skeleton } from "@/shared/ui";
import { ProjectDreamingPanel, ProjectMembersPanel, useProjects } from "@/features/projects";
import { useHasCapability } from "@/features/capabilities";
import { ProjectInstructionsEditor } from "@/features/instructions";
import {
  canManageProjectSettings,
  projectProfileChanged,
  projectProfileDraft,
  projectProfilePatch,
  type ProjectProfileDraft,
} from "@/features/projects/lib/project-profile";
import { pushError, pushSuccess } from "@/stores/notification";
import { useProjectId } from "@/shared/routing/route-params";
import { useT } from "@/shared/i18n";

export default function ProjectSettingsPage() {
  const t = useT();
  const projectId = useProjectId();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ProjectProfileDraft | null>(null);
  const projectQuery = useQuery({
    queryKey: qk.project(projectId),
    queryFn: () => api.getProject(projectId),
  });
  const project = projectQuery.data;
  const canManageSettings = canManageProjectSettings(project);
  // 可见范围只对组织里的项目有意义：组织服务器自己的网页上（governance），或者桌面上这个项目
  // 住在某个组织里（清单上的 home —— 转过来的详情里，服务器说的是「住我这儿」）。
  const servedByAnOrganisation = useHasCapability("governance");
  const listed = useProjects().data?.find((item) => item.id === projectId);
  const inAnOrganisation = servedByAnOrganisation || listed?.home?.kind === "organisation";
  const root = `/projects/${encodeURIComponent(projectId)}`;

  useEffect(() => {
    if (project) setDraft(projectProfileDraft(project));
  }, [project]);

  const update = useMutation({
    mutationFn: (data: UpdateProjectRequest) => api.updateProject(projectId, data),
    onSuccess: async (updated) => {
      queryClient.setQueryData<ProjectDetail>(qk.project(projectId), (current) =>
        current ? { ...current, ...updated } : current);
      await queryClient.invalidateQueries({ queryKey: qk.projects() });
      setDraft(projectProfileDraft(updated));
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "项目资料没能保存", en: "The project profile could not be saved" })),
  });

  if (projectQuery.isLoading && !project) {
    return (
      <div className="project-settings-page project-settings-loading">
        <Skeleton height={72} rounded="sm" />
        <Skeleton height={260} rounded="sm" />
        <Skeleton height={180} rounded="sm" />
      </div>
    );
  }

  if (projectQuery.isError || !project || !draft) {
    return (
      <div className="project-settings-page">
        <header className="settings-page-header"><h1>{t({ zh: "项目设置", en: "Project settings" })}</h1></header>
        <p className="settings-load-error" role="alert">{t({ zh: "读不到项目设置。刷新再试一次。", en: "Project settings could not be loaded. Refresh and try again." })}</p>
      </div>
    );
  }

  const dirty = projectProfileChanged(project, draft);
  const saveDisabled = !canManageSettings || !dirty || !draft.name.trim() || update.isPending;

  return (
    <div className="project-settings-page">
      <header className="settings-page-header">
        <h1>{t({ zh: "项目设置", en: "Project settings" })}</h1>
        <p>{t({ zh: "这个项目的名称、权限与资源。", en: "Shared identity, access, and resources for this Project." })}</p>
      </header>

      <section className="settings-compact-section">
        <div className="settings-compact-heading">
          <div><h2>{t({ zh: "项目资料", en: "Project profile" })}</h2><p>{t({ zh: "所有能进这个项目的人都看得到。", en: "Visible to everyone with access to this Project." })}</p></div>
          <button
            type="button"
            className="settings-primary-action"
            disabled={saveDisabled}
            onClick={() => update.mutate(projectProfilePatch(draft))}
          >
            {update.isPending ? "Saving…" : "Save"}
          </button>
        </div>
        <div className="settings-row-group project-profile-group">
          <label className="settings-row">
            <span><strong>{t({ zh: "名称", en: "Name" })}</strong><small>{t({ zh: "会话和产出里显示的项目名。", en: "The Project name shown across Sessions and outputs." })}</small></span>
            <input
              required
              maxLength={300}
              value={draft.name}
              disabled={!canManageSettings || update.isPending}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </label>
          <label className="settings-row">
            <span><strong>{t({ zh: "研究方向", en: "Research domain" })}</strong><small>{t({ zh: "所属的学科或研究领域。", en: "The scientific field or research area." })}</small></span>
            <input
              value={draft.researchDomain}
              disabled={!canManageSettings || update.isPending}
              onChange={(event) => setDraft({ ...draft, researchDomain: event.target.value })}
              placeholder={t({ zh: "未填写", en: "Not specified" })}
            />
          </label>
          <label className="settings-row settings-row-textarea">
            <span><strong>{t({ zh: "说明", en: "Description" })}</strong><small>{t({ zh: "给协作者看的一段背景。", en: "Brief shared context for collaborators." })}</small></span>
            <textarea
              rows={3}
              value={draft.description}
              disabled={!canManageSettings || update.isPending}
              onChange={(event) => setDraft({ ...draft, description: event.target.value })}
              placeholder={t({ zh: "未填写", en: "Not specified" })}
            />
          </label>
          <div className="settings-row settings-permission-row">
            <span><strong>{t({ zh: "编辑权限", en: "Editing access" })}</strong><small>{canManageSettings ? t({ zh: "你可以修改这个项目的资料。", en: "You can update this project's profile." }) : t({ zh: "只有项目负责人或管理员能改这些设置。", en: "Only a project lead or administrator can change these settings." })}</small></span>
            <span className={canManageSettings ? "settings-access-value" : "settings-access-value is-readonly"}>
              {canManageSettings ? t({ zh: "可编辑", en: "Editable" }) : t({ zh: "只读", en: "Read only" })}
            </span>
          </div>
        </div>
      </section>

      {inAnOrganisation && (
        <section className="settings-compact-section" id="project-visibility">
          <div className="settings-compact-heading">
            <div>
              <h2>{t({ zh: "组织里谁看得见", en: "Who in the organisation can see it" })}</h2>
              <p>{t({ zh: "组内可见：组里的人在「组织 → 项目」里看得到、能只读打开。仅成员：只有项目成员（和组织管理员）。",
                      en: "Open: everyone in the organisation can find it under Organisation → Projects and open it read-only. Members only: just the project's members (and administrators)." })}</p>
            </div>
          </div>
          <div className="settings-row-group" role="radiogroup" aria-label={t({ zh: "可见范围", en: "Visibility" })}>
            {(["organisation", "members"] as const).map((choice) => (
              <label key={choice} className="settings-row">
                <span><strong>{choice === "organisation" ? t({ zh: "组内可见", en: "Open to the organisation" }) : t({ zh: "仅成员", en: "Members only" })}</strong></span>
                <input type="radio" name="project-visibility" value={choice}
                  checked={(project.visibility ?? "organisation") === choice}
                  disabled={!canManageSettings || update.isPending}
                  onChange={() => update.mutate({ visibility: choice })} />
              </label>
            ))}
          </div>
        </section>
      )}

      {project.can_archive && project.status === "active" && <ArchiveThisProject projectId={projectId} />}

      <ProjectInstructionsEditor projectId={projectId} editable={canManageSettings} />

      <ProjectDreamingPanel projectId={projectId} canManage={canManageSettings} />

      <section className="settings-compact-section">
        <div className="settings-compact-heading"><div><h2>{t({ zh: "权限与资源", en: "Access and resources" })}</h2><p>{t({ zh: "打开每一块的权威记录。", en: "Open the authoritative Project records for each area." })}</p></div></div>
        <div className="settings-row-group settings-link-group">
          <a className="settings-row settings-link-row" href="#project-members">
            <span><Users size={15} /><span><strong>{t({ zh: "成员", en: "Members" })}</strong><small>{t({ zh: "项目角色与协作权限。", en: "Project roles and collaboration access." })}</small></span></span>
            <ArrowRight size={14} />
          </a>
        </div>
      </section>

      <div id="project-members" className="project-members-anchor">
        <ProjectMembersPanel project={project} />
      </div>
    </div>
  );
}


/**
 * 归档 = 冻结：只读、不再开会话、后台不再替它干活；知识、会话、产出都在，能恢复（项目页顶上的横条）。
 * 负责人或同组织管理员（服务器答的 `can_archive`）。先说清楚会发生什么，再点第二下。
 */
function ArchiveThisProject({ projectId }: { projectId: string }) {
  const t = useT();
  const queryClient = useQueryClient();
  const [asking, setAsking] = useState(false);
  const archive = useMutation({
    mutationFn: () => api.archiveProject(projectId),
    onSuccess: () => {
      setAsking(false);
      void queryClient.invalidateQueries({ queryKey: qk.project(projectId) });
      void queryClient.invalidateQueries({ queryKey: qk.projects() });
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "没归档成", en: "Could not archive it" })),
  });
  return (
    <section className="settings-compact-section" id="project-archive">
      <div className="settings-compact-heading">
        <div>
          <h2>{t({ zh: "归档", en: "Archive" })}</h2>
          <p>{t({ zh: "不做了就归档：项目冻结成只读，不能再开会话、改设置，后台也不再替它干活。知识、会话和产出都在，随时能恢复。",
                  en: "Archive a project you have stopped working on: it becomes read-only, takes no new sessions or settings changes, and nothing runs for it in the background. Its knowledge, sessions and outputs are kept, and you can restore it any time." })}</p>
        </div>
      </div>
      <div className="settings-form-actions">
        {asking ? (
          <>
            <Button size="sm" onClick={() => setAsking(false)}>{t({ zh: "算了", en: "Cancel" })}</Button>
            <Button size="sm" variant="primary" disabled={archive.isPending} onClick={() => archive.mutate()}>
              {t({ zh: "归档", en: "Archive" })}
            </Button>
          </>
        ) : (
          <Button size="sm" onClick={() => setAsking(true)}>{t({ zh: "归档这个项目…", en: "Archive this project…" })}</Button>
        )}
      </div>
    </section>
  );
}

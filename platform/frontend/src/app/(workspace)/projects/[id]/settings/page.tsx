"use client";

import { useEffect, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ProjectDetail, type UpdateProjectRequest } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Button, Skeleton } from "@/shared/ui";
import { ProjectDreamingPanel, ProjectMembersPanel, useProjects } from "@/features/projects";
import { useHasCapability } from "@/features/capabilities";
import { ProjectInstructionsEditor } from "@/features/instructions";
import { ProjectSkillsPanel } from "@/features/skills/components/ProjectSkillsPanel";
import {
  canManageProjectSettings,
  projectProfileChanged,
  projectProfileDraft,
  projectProfilePatch,
  type ProjectProfileDraft,
} from "@/features/projects/lib/project-profile";
import { pushError } from "@/stores/notification";
import { useProjectId } from "@/shared/routing/route-params";
import { useT } from "@/shared/i18n";
import { ProjectSSHResources } from "@/features/resources/components/ProjectSSHResources";

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
  // 可见范围、成员、编辑权限只对组织里的项目有意义：组织服务器自己的网页上（governance），
  // 或者桌面上这个项目住在某个组织里（清单上的 home —— 转过来的详情里，服务器说的是「住我这儿」）。
  // 本机项目只有你一个人：成员接口在个人版根本不存在（只有专业版后端挂 `/members`），
  // 「编辑权限」也永远是「可编辑」，画出来只是一块灰掉的空壳。
  const servedByAnOrganisation = useHasCapability("governance");
  const listed = useProjects().data?.find((item) => item.id === projectId);
  const inAnOrganisation = servedByAnOrganisation || listed?.home?.kind === "organisation";

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
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "保存失败", en: "Save failed" })),
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
        <p className="settings-load-error" role="alert">{t({ zh: "无法加载项目设置，请稍后重试。", en: "Could not load project settings. Please try again later." })}</p>
      </div>
    );
  }

  const dirty = projectProfileChanged(project, draft);
  const saveDisabled = !canManageSettings || !dirty || !draft.name.trim() || update.isPending;

  return (
    <div className="project-settings-page">
      <header className="settings-page-header">
        <h1>{t({ zh: "项目设置", en: "Project settings" })}</h1>
      </header>

      <section className="settings-compact-section">
        <div className="settings-compact-heading">
          <div><h2>{t({ zh: "项目资料", en: "Project profile" })}</h2><p>{t({ zh: "能打开这个项目的人都能看到这些信息。", en: "Visible to everyone with access to this Project." })}</p></div>
          <button
            type="button"
            className="settings-primary-action"
            disabled={saveDisabled}
            onClick={() => update.mutate(projectProfilePatch(draft))}
          >
            {update.isPending ? t({ zh: "正在保存…", en: "Saving…" }) : t({ zh: "保存", en: "Save" })}
          </button>
        </div>
        <div className="settings-row-group project-profile-group">
          <label className="settings-row">
            <span><strong>{t({ zh: "名称", en: "Name" })}</strong></span>
            <input
              required
              maxLength={300}
              value={draft.name}
              disabled={!canManageSettings || update.isPending}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </label>
          <label className="settings-row">
            <span><strong>{t({ zh: "研究方向", en: "Research domain" })}</strong></span>
            <input
              value={draft.researchDomain}
              disabled={!canManageSettings || update.isPending}
              onChange={(event) => setDraft({ ...draft, researchDomain: event.target.value })}
              placeholder={t({ zh: "未填写", en: "Not specified" })}
            />
          </label>
          <label className="settings-row settings-row-textarea">
            <span><strong>{t({ zh: "说明", en: "Description" })}</strong><small>{t({ zh: "项目的背景和目标。", en: "Background and goals of the project." })}</small></span>
            <textarea
              rows={3}
              value={draft.description}
              disabled={!canManageSettings || update.isPending}
              onChange={(event) => setDraft({ ...draft, description: event.target.value })}
              placeholder={t({ zh: "未填写", en: "Not specified" })}
            />
          </label>
          {inAnOrganisation && (
            <div className="settings-row settings-permission-row">
              <span><strong>{t({ zh: "编辑权限", en: "Editing access" })}</strong><small>{canManageSettings ? t({ zh: "你可以修改这个项目的资料。", en: "You can update this project's profile." }) : t({ zh: "只有项目负责人或管理员可以修改这些设置。", en: "Only a project lead or administrator can change these settings." })}</small></span>
              <span className={canManageSettings ? "settings-access-value" : "settings-access-value is-readonly"}>
                {canManageSettings ? t({ zh: "可编辑", en: "Editable" }) : t({ zh: "只读", en: "Read only" })}
              </span>
            </div>
          )}
        </div>
      </section>

      {project.root_folder && !inAnOrganisation && <section className="settings-compact-section">
        <div className="settings-compact-heading"><div>
          <h2>{t({ zh: "根文件夹", en: "Root folder" })}</h2>
          <p>{t({ zh: "创建项目时确定的文件存储位置。", en: "The file location chosen when this project was created." })}</p>
        </div></div>
        <code className="project-folder-path">{project.root_folder}</code>
      </section>}

      <ProjectSSHResources projectId={projectId} canManage={Boolean(project.capabilities?.includes("manage_resources"))} />

      {inAnOrganisation && (
        <section className="settings-compact-section" id="project-visibility">
          <div className="settings-compact-heading">
            <div>
              <h2>{t({ zh: "可见范围", en: "Visibility" })}</h2>
              <p>{t({ zh: "「组内可见」：组织成员可以在「组织 → 项目」中找到并以只读方式打开。「仅成员」：只有项目成员和组织管理员可以看到。",
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

      <ProjectSkillsPanel key={projectId} projectId={projectId} editable={canManageSettings} />

      <ProjectDreamingPanel projectId={projectId} canManage={canManageSettings} />

      {inAnOrganisation && <ProjectMembersPanel project={project} />}
    </div>
  );
}


/**
 * 归档 = 只读：不再开会话、后台任务停下；知识、会话、成果都在，能恢复（项目页顶上的横条）。
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
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "归档失败", en: "Archive failed" })),
  });
  return (
    <section className="settings-compact-section" id="project-archive">
      <div className="settings-compact-heading">
        <div>
          <h2>{t({ zh: "归档", en: "Archive" })}</h2>
          <p>{t({ zh: "归档后项目变为只读，不能新建会话或修改设置，后台任务也会停止。已有内容全部保留，可以随时恢复。",
                  en: "Archived projects become read-only: no new sessions or settings changes, and background tasks stop. Everything is kept, and you can restore it any time." })}</p>
        </div>
      </div>
      <div className="settings-form-actions">
        {asking ? (
          <>
            <Button size="sm" onClick={() => setAsking(false)}>{t({ zh: "取消", en: "Cancel" })}</Button>
            <Button size="sm" variant="primary" disabled={archive.isPending} onClick={() => archive.mutate()}>
              {t({ zh: "归档", en: "Archive" })}
            </Button>
          </>
        ) : (
          <Button size="sm" onClick={() => setAsking(true)}>{t({ zh: "归档项目…", en: "Archive project…" })}</Button>
        )}
      </div>
    </section>
  );
}

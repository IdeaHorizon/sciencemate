"use client";

import { useState } from "react";
import { Plus, Trash2, X } from "lucide-react";
import type { Project } from "@/lib/api";
import { Skeleton } from "@/shared/ui";
import { useProjectMembers } from "../hooks/useProjectMembers";
import type { ProjectMemberRole } from "../api/project-members";
import { protectsLastLead } from "../lib/member-policy";
import { useT, type Phrase } from "@/shared/i18n";

const ROLES: ProjectMemberRole[] = ["viewer", "researcher", "reviewer", "lead"];

/** 角色的取值是给后端的；给人看的是这几个名字。 */
const ROLE_LABELS: Record<ProjectMemberRole, Phrase> = {
  viewer: { zh: "查看者", en: "Viewer" },
  researcher: { zh: "研究员", en: "Researcher" },
  reviewer: { zh: "评审人", en: "Reviewer" },
  lead: { zh: "负责人", en: "Lead" },
};

export function ProjectMembersPanel({ project }: { project: Project }) {
  const t = useT();
  const members = useProjectMembers(project.id, project);
  const [adding, setAdding] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<ProjectMemberRole>("researcher");
  const result = members.query.data;
  const canManage = result?.canonical && result.capabilities.includes("manage_members");

  // 增删改失败由 useProjectMembers 的 onError 弹一次 toast；面板里不再另写一遍。
  const add = (event: React.FormEvent) => {
    event.preventDefault();
    members.add.mutate({ email: email.trim(), role }, {
      onSuccess: () => {
        setEmail("");
        setAdding(false);
      },
    });
  };

  return (
    <section className="project-members-section">
      <div className="project-members-heading">
        <div><span>{t({ zh: "权限", en: "Access" })}</span><h2>{t({ zh: "成员", en: "Members" })}</h2><p>{t({ zh: "修改角色后立即生效。", en: "Role changes take effect immediately." })}</p></div>
        {!adding && (
          <button
            type="button"
            disabled={!canManage}
            title={canManage
              ? t({ zh: "添加项目成员", en: "Add a Project member" })
              : t({ zh: "只有项目负责人或管理员可以管理成员。", en: "Only a Project Lead or administrator can manage members." })}
            onClick={() => setAdding(true)}
          ><Plus size={12} />{t({ zh: "添加成员", en: "Add member" })}</button>
        )}
      </div>

      {members.query.isLoading && <div className="project-members-loading"><Skeleton height={54} rounded="sm" /><Skeleton height={54} rounded="sm" /></div>}
      {members.query.isError && <p className="project-members-error">{members.query.error instanceof Error ? members.query.error.message : t({ zh: "无法加载成员。", en: "Could not load members." })}</p>}
      {result && !result.canonical && (
        <p className="project-members-note">{t({ zh: "当前不支持成员管理。", en: "Member management is not available here." })}</p>
      )}
      {result?.canonical && !canManage && (
        <p className="project-members-note">{t({ zh: "只有项目负责人或管理员可以修改成员角色。", en: "Only a Project Lead or administrator can modify member roles." })}</p>
      )}

      {adding && canManage && (
        <form className="project-member-add" onSubmit={add}>
          <input required type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@example.edu" aria-label={t({ zh: "成员邮箱", en: "Member email" })} />
          <select value={role} onChange={(event) => setRole(event.target.value as ProjectMemberRole)} aria-label={t({ zh: "项目角色", en: "Project role" })}>
            {ROLES.map((value) => <option value={value} key={value}>{t(ROLE_LABELS[value])}</option>)}
          </select>
          <button type="submit" disabled={members.add.isPending}>{members.add.isPending ? t({ zh: "正在添加…", en: "Adding…" }) : t({ zh: "添加", en: "Add" })}</button>
          <button type="button" onClick={() => setAdding(false)} aria-label={t({ zh: "取消", en: "Cancel" })}><X size={13} /></button>
        </form>
      )}

      {result && (
        <div className="project-member-list">
          {result.items.length === 0 && <p className="project-members-note">{t({ zh: "还没有项目成员。", en: "No project members yet." })}</p>}
          {result.items.map((member) => {
            const lastLead = protectsLastLead(member, result.items);
            return (
              <div className="project-member-row" key={member.userId}>
                <span className="project-member-avatar">{member.displayName.slice(0, 1).toUpperCase()}</span>
                <span><strong>{member.displayName}</strong><small>{member.email ?? t({ zh: "邮箱未知", en: "Email unavailable" })}</small></span>
                <select
                  value={member.role}
                  disabled={!canManage || lastLead || members.changeRole.isPending}
                  title={!canManage ? t({ zh: "只有项目负责人或管理员可以修改角色。", en: "Only a Project Lead or administrator can change roles." }) : lastLead ? t({ zh: "项目至少要保留一位负责人。", en: "A Project must keep at least one Lead." }) : t({ zh: "修改项目角色", en: "Change project role" })}
                  onChange={(event) => members.changeRole.mutate({ userId: member.userId, role: event.target.value as ProjectMemberRole })}
                >
                  {ROLES.map((value) => <option value={value} key={value}>{t(ROLE_LABELS[value])}</option>)}
                </select>
                <button
                  type="button"
                  disabled={!canManage || lastLead || members.remove.isPending}
                  title={!canManage ? t({ zh: "只有项目负责人或管理员可以移出成员。", en: "Only a Project Lead or administrator can remove members." }) : lastLead ? t({ zh: "项目至少要保留一位负责人。", en: "A Project must keep at least one Lead." }) : t({ zh: "移出成员", en: "Remove member" })}
                  aria-label={t({ zh: "移出{name}", en: "Remove {name}" }, { name: member.displayName })}
                  onClick={() => members.remove.mutate(member.userId)}
                ><Trash2 size={12} /></button>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

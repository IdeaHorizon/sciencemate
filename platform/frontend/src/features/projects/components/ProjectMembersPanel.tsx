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

// 纯函数收 t，不自己够 hook —— 见 shared/i18n/useT.ts 的「纯函数不许用它」。
function errorText(error: unknown, t: (phrase: Phrase) => string) {
  return error instanceof Error
    ? error.message
    : t({ zh: "成员的改动没能保存。", en: "The member change could not be saved." });
}

export function ProjectMembersPanel({ project }: { project: Project }) {
  const t = useT();
  const members = useProjectMembers(project.id, project);
  const [adding, setAdding] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<ProjectMemberRole>("researcher");
  const [inlineError, setInlineError] = useState<string | null>(null);
  const result = members.query.data;
  const canManage = result?.canonical && result.capabilities.includes("manage_members");

  const add = async (event: React.FormEvent) => {
    event.preventDefault();
    setInlineError(null);
    try {
      await members.add.mutateAsync({ email: email.trim(), role });
      setEmail("");
      setAdding(false);
    } catch (error) {
      setInlineError(errorText(error, t));
    }
  };

  return (
    <section className="project-members-section">
      <div className="project-members-heading">
        <div><span>{t({ zh: "权限", en: "Access" })}</span><h2>{t({ zh: "成员", en: "Members" })}</h2><p>{t({ zh: "角色立刻对这个项目生效，包括下一个会话、决策、评审或发布动作。", en: "Roles affect this Project immediately, including the next Session, Decision, review, or publication action." })}</p></div>
        {!adding && (
          <button
            type="button"
            disabled={!canManage}
            title={canManage
              ? t({ zh: "给项目加一个成员", en: "Add a Project member" })
              : t({ zh: "只有项目负责人或管理员能管理成员。", en: "Only a Project Lead or administrator can manage members." })}
            onClick={() => setAdding(true)}
          ><Plus size={12} />{t({ zh: "添加成员", en: "Add member" })}</button>
        )}
      </div>

      {members.query.isLoading && <div className="project-members-loading"><Skeleton height={54} rounded="sm" /><Skeleton height={54} rounded="sm" /></div>}
      {members.query.isError && <p className="project-members-error">{errorText(members.query.error, t)}</p>}
      {result && !result.canonical && (
        <p className="project-members-note">{t({ zh: "这个后端只给得出已记录的项目归属。成员管理还没开。", en: "Only recorded Project ownership is available from this backend. Member management is not enabled yet." })}</p>
      )}
      {result?.canonical && !canManage && (
        <p className="project-members-note">{t({ zh: "只有项目负责人或管理员能改成员角色。", en: "Only a Project Lead or administrator can modify member roles." })}</p>
      )}

      {adding && canManage && (
        <form className="project-member-add" onSubmit={(event) => void add(event)}>
          <input required type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@example.edu" aria-label={t({ zh: "成员邮箱", en: "Member email" })} />
          <select value={role} onChange={(event) => setRole(event.target.value as ProjectMemberRole)} aria-label={t({ zh: "项目角色", en: "Project role" })}>
            {ROLES.map((value) => <option value={value} key={value}>{value}</option>)}
          </select>
          <button type="submit" disabled={members.add.isPending}>{members.add.isPending ? "Adding…" : "Add"}</button>
          <button type="button" onClick={() => setAdding(false)} aria-label={t({ zh: "取消", en: "Cancel" })}><X size={13} /></button>
        </form>
      )}

      {inlineError && <p className="project-members-error" role="alert">{inlineError}</p>}

      {result && (
        <div className="project-member-list">
          {result.items.length === 0 && <p className="project-members-note">{t({ zh: "没有返回任何项目成员。", en: "No Project members were returned." })}</p>}
          {result.items.map((member) => {
            const lastLead = protectsLastLead(member, result.items);
            return (
              <div className="project-member-row" key={member.userId}>
                <span className="project-member-avatar">{member.displayName.slice(0, 1).toUpperCase()}</span>
                <span><strong>{member.displayName}</strong><small>{member.email ?? t({ zh: "读不到邮箱", en: "Email unavailable" })}</small></span>
                <select
                  value={member.role}
                  disabled={!canManage || lastLead || members.changeRole.isPending}
                  title={!canManage ? t({ zh: "只有项目负责人或管理员能改角色。", en: "Only a Project Lead or administrator can change roles." }) : lastLead ? t({ zh: "项目至少要留一个负责人。", en: "A Project must keep at least one Lead." }) : t({ zh: "修改项目角色", en: "Change project role" })}
                  onChange={async (event) => {
                    setInlineError(null);
                    try { await members.changeRole.mutateAsync({ userId: member.userId, role: event.target.value as ProjectMemberRole }); }
                    catch (error) { setInlineError(errorText(error, t)); }
                  }}
                >
                  {ROLES.map((value) => <option value={value} key={value}>{value}</option>)}
                </select>
                <button
                  type="button"
                  disabled={!canManage || lastLead || members.remove.isPending}
                  title={!canManage ? t({ zh: "只有项目负责人或管理员能移出成员。", en: "Only a Project Lead or administrator can remove members." }) : lastLead ? "A Project must keep at least one Lead." : t({ zh: "移出成员", en: "Remove member" })}
                  aria-label={`Remove ${member.displayName}`}
                  onClick={async () => {
                    setInlineError(null);
                    try { await members.remove.mutateAsync(member.userId); }
                    catch (error) { setInlineError(errorText(error, t)); }
                  }}
                ><Trash2 size={12} /></button>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

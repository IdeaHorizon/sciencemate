"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Plus, X } from "lucide-react";
import { ProjectsList } from "@/features/projects";
import { WhereItLives } from "@/features/projects/WhereItLives";
import { ProjectRootFolder } from "@/features/projects/ProjectRootFolder";
import { otherHomes } from "@/features/projects/homes";
import { useCreateProject } from "@/features/projects/hooks/useProjects";
import { api } from "@/lib/api";
import { Button } from "@/shared/ui";
import { useT } from "@/shared/i18n";

const INITIAL_FORM = {
  name: "",
  description: "",
  research_domain: "",
  operation_mode: "assisted",
  //: 放在哪：空串 = 本机。**默认必须是空的** —— 一个只想在自己电脑上开个课题的人
  //: 不该先回答"你属于哪个组织"。
  home: "",
  root_folder: null as string | null,
};

export default function ProjectsPage() {
  const t = useT();
  const router = useRouter();
  const createProject = useCreateProject();
  const [open, setOpen] = useState(false);
  const [addingHome, setAddingHome] = useState(false);
  const AddAHome = otherHomes.Adder;
  const [form, setForm] = useState(INITIAL_FORM);

  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("create") === "1") setOpen(true);
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    // UI 三档 ⇄ 存储两态（同 SessionWorkspace）：「连续」= 自主 + 预授权全部高危
    // 类别（"*"）。创建端点只收 assisted/autonomous，所以连续档先按自主建，再补
    // 一刀 config 把授权写全。少了这一步，用户在创建时选不到真正 hands-off 的档。
    const isContinuous = form.operation_mode === "continuous";
    const created = await createProject.mutateAsync({
      ...form,
      root_folder: form.home ? undefined : form.root_folder?.trim() || undefined,
      operation_mode: isContinuous ? "autonomous" : form.operation_mode,
      description: form.description.trim() || undefined,
      research_domain: form.research_domain.trim() || undefined,
    }).catch(() => undefined);
    // The mutation displays the server's error; keep the form and chosen path for retry.
    if (!created) return;
    // 连续档那一刀只补得了**本机**的项目：组织项目的 config 在那台服务器上，而
    // `updateProjectConfig` 走的是带 id 的路径，转交那一层会把它送对地方。
    if (isContinuous) {
      await api.updateProjectConfig(created.id, {
        operation_mode: "autonomous",
        autonomous_authorized_risk_classes: ["*"],
      });
    }
    setForm(INITIAL_FORM);
    setOpen(false);
    router.push(`/projects/${created.id}/chat`);
  };

  return (
    <div className="page projects-page">
      <header className="page-header">
        <div>
          <h1 className="page-title">{t({ zh: "项目", en: "Projects" })}</h1>
          <p className="page-subtitle">{t({ zh: "每个项目是一个独立的研究工作区，包含会话、证据和成果。", en: "Each project is a research workspace that holds its sessions, evidence and outputs." })}</p>
        </div>
        <Button size="sm" iconLeft={<Plus size={13} />} onClick={() => setOpen(true)}>{t({ zh: "新建项目", en: "New project" })}</Button>
      </header>
      <ProjectsList onCreate={() => setOpen(true)} />

      {addingHome && AddAHome && (
        <div className="project-create-backdrop" role="presentation"
             onMouseDown={(event) => { if (event.target === event.currentTarget) setAddingHome(false); }}>
          <section className="project-create-panel org-panel" role="dialog" aria-modal="true">
            <AddAHome
              onCancel={() => setAddingHome(false)}
              onHeld={(held) => { setAddingHome(false); setForm((f) => ({ ...f, home: held.id })); }}
            />
          </section>
        </div>
      )}

      {open && (
        <div className="project-create-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setOpen(false); }}>
          <section className="project-create-panel" role="dialog" aria-modal="true" aria-labelledby="new-project-title">
            <header>
              <div><span>{t({ zh: "研究工作区", en: "Research workspace" })}</span><h2 id="new-project-title">{t({ zh: "新建项目", en: "New project" })}</h2><p>{t({ zh: "填好名称就可以创建，研究方向和背景之后也可以在会话里告诉助手。", en: "A name is all you need. You can tell the assistant about the research direction in a session later." })}</p></div>
              <button type="button" onClick={() => setOpen(false)} aria-label={t({ zh: "关闭", en: "Close" })}><X size={16} /></button>
            </header>
            <form className="project-create-form" onSubmit={submit}>
              <label><span>{t({ zh: "项目名称", en: "Project name" })}</span><input required autoFocus maxLength={300} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={t({ zh: "例如：翼型转捩预测", en: "e.g. Robust interatomic potentials" })} /></label>
              <label><span>{t({ zh: "研究方向", en: "Research domain" })}</span><input value={form.research_domain} onChange={(e) => setForm({ ...form, research_domain: e.target.value })} placeholder={t({ zh: "流体力学", en: "Materials science" })} /></label>
              <label><span>{t({ zh: "研究说明", en: "Research brief" })}</span><textarea rows={4} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder={t({ zh: "想解决什么问题、有什么限制、希望得到什么结果。", en: "Describe the question, constraints and desired result." })} /></label>
              <WhereItLives value={form.home}
                            onChange={(home) => setForm({ ...form, home, root_folder: null })}
                            onAddOne={() => setAddingHome(true)} />
              {!form.home && <ProjectRootFolder value={form.root_folder}
                onChange={(root_folder) => setForm((current) => ({ ...current, root_folder }))}
                disabled={createProject.isPending} />}
              <details className="project-create-advanced">
                <summary>{t({ zh: "高级", en: "Advanced" })}</summary>
                <div className="project-create-grid">
                  <label><span>{t({ zh: "工作模式", en: "Operation mode" })}</span><select value={form.operation_mode} onChange={(e) => setForm({ ...form, operation_mode: e.target.value })}><option value="assisted">{t({ zh: "协作：每一步都先征求你的意见", en: "Assisted: asks you at each step" })}</option><option value="autonomous">{t({ zh: "自主：只在高风险操作前停下来询问", en: "Autonomous: stops only before high-risk actions" })}</option><option value="continuous">{t({ zh: "连续：不再询问，一直执行到完成", en: "Continuous: runs to completion without asking" })}</option></select></label>
                </div>
              </details>
              <div className="project-create-actions"><Button type="button" variant="ghost" size="sm" onClick={() => setOpen(false)}>{t({ zh: "取消", en: "Cancel" })}</Button><Button type="submit" variant="primary" size="sm" loading={createProject.isPending}>{t({ zh: "创建并打开", en: "Create and open" })}</Button></div>
            </form>
          </section>
        </div>
      )}
    </div>
  );
}

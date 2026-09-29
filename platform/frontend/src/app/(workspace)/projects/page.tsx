"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Plus, X } from "lucide-react";
import { ProjectsList } from "@/features/projects";
import { WhereItLives } from "@/features/projects/WhereItLives";
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
  reporting_level: "medium",
  //: 放在哪：空串 = 本机。**默认必须是空的** —— 一个只想在自己电脑上开个课题的人
  //: 不该先回答"你属于哪个组织"。
  home: "",
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
      operation_mode: isContinuous ? "autonomous" : form.operation_mode,
      description: form.description.trim() || undefined,
      research_domain: form.research_domain.trim() || undefined,
    });
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
          <p className="page-subtitle">{t({ zh: "每个项目是一个研究工作区：对话、证据和产出都归它。", en: "Shared research workspaces with governed conversations, evidence and outputs." })}</p>
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
              <div><span>{t({ zh: "研究工作区", en: "Research workspace" })}</span><h2 id="new-project-title">{t({ zh: "新建项目", en: "New project" })}</h2><p>{t({ zh: "先起个名字就行。研究方向和说明可以在对话里慢慢说清楚。", en: "Start with a clean shared context. You can refine the research brief in chat." })}</p></div>
              <button type="button" onClick={() => setOpen(false)} aria-label={t({ zh: "关闭", en: "Close" })}><X size={16} /></button>
            </header>
            <form className="project-create-form" onSubmit={submit}>
              <label><span>{t({ zh: "项目名称", en: "Project name" })}</span><input required autoFocus maxLength={300} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={t({ zh: "例如：翼型转捩预测", en: "e.g. Robust interatomic potentials" })} /></label>
              <label><span>{t({ zh: "研究方向", en: "Research domain" })}</span><input value={form.research_domain} onChange={(e) => setForm({ ...form, research_domain: e.target.value })} placeholder={t({ zh: "流体力学", en: "Materials science" })} /></label>
              <label><span>{t({ zh: "研究说明", en: "Research brief" })}</span><textarea rows={4} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder={t({ zh: "想解决什么问题、有什么限制、希望得到什么结果。", en: "Describe the question, constraints and desired result." })} /></label>
              <WhereItLives value={form.home}
                            onChange={(home) => setForm({ ...form, home })}
                            onAddOne={() => setAddingHome(true)} />
              <details className="project-create-advanced">
                <summary>{t({ zh: "高级", en: "Advanced" })}</summary>
                <div className="project-create-grid">
                  <label><span>{t({ zh: "工作档位", en: "Operation mode" })}</span><select value={form.operation_mode} onChange={(e) => setForm({ ...form, operation_mode: e.target.value })}><option value="assisted">{t({ zh: "协助 —— 每一步先问你", en: "Assisted — asks at each step" })}</option><option value="autonomous">{t({ zh: "自主 —— 只在高风险处停下来问", en: "Autonomous — stops only at high-risk points" })}</option><option value="continuous">{t({ zh: "连续 —— 不问，一直做到交付", en: "Continuous — runs hands-off to completion" })}</option></select></label>
                  <label><span>{t({ zh: "汇报详细度", en: "Reporting detail" })}</span><select value={form.reporting_level} onChange={(e) => setForm({ ...form, reporting_level: e.target.value })}><option value="low">{t({ zh: "简要", en: "Compact" })}</option><option value="medium">{t({ zh: "标准", en: "Standard" })}</option><option value="high">{t({ zh: "详细", en: "Detailed" })}</option></select></label>
                </div>
              </details>
              <div className="project-create-actions"><Button type="button" variant="ghost" size="sm" onClick={() => setOpen(false)}>{t({ zh: "取消", en: "Cancel" })}</Button><Button type="submit" variant="primary" size="sm" loading={createProject.isPending}>{t({ zh: "建好就进去", en: "Create and open" })}</Button></div>
            </form>
          </section>
        </div>
      )}
    </div>
  );
}

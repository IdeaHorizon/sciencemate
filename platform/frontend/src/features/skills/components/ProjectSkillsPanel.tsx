"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLanguage, useT } from "@/shared/i18n";
import { nodeDisplayName } from "@/features/execution/lib/display-names";
import { Skeleton } from "@/shared/ui";
import { skillsClient, skillsKeys } from "../api";
import { bindingFor, bindingKey, missingTools, sameBindings } from "../lib/bindings";
import type { LibrarySkill, SkillBinding } from "../types";
import { SkillImport } from "./SkillImport";
import { SkillPreview } from "./SkillPreview";

export function ProjectSkillsPanel({ projectId, editable }: { projectId: string; editable: boolean }) {
  const t = useT();
  const lang = useLanguage();
  const qc = useQueryClient();
  const config = useQuery({ queryKey: skillsKeys.project(projectId), queryFn: () => skillsClient.project(projectId) });
  const [draft, setDraft] = useState<SkillBinding[] | null>(null);
  const [baseRevision, setBaseRevision] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [search, setSearch] = useState("");
  const [saveError, setSaveError] = useState("");
  const hasDraft = draft !== null;
  useEffect(() => {
    if (config.data && draft === null) {
      const bindings = [...config.data.bindings];
      const wanted = new URLSearchParams(window.location.search).get("skill");
      const skill = config.data.skills.find((s) => s.name === wanted);
      if (editable && skill?.enabled && !bindings.some((b) => b.name === wanted)) bindings.push(bindingFor(skill));
      setDraft(bindings); setBaseRevision(config.data.revision);
    }
  }, [config.data, draft, editable]);
  useEffect(() => {
    if (hasDraft && window.location.hash === "#project-skills") {
      const frame = requestAnimationFrame(() => document.getElementById("project-skills")?.scrollIntoView({ block: "start" }));
      return () => cancelAnimationFrame(frame);
    }
  // Scroll when the initial assignments arrive, while leaving edits in place.
  }, [hasDraft]);
  const save = useMutation({
    mutationFn: () => skillsClient.saveProject(projectId, baseRevision, draft ?? []),
    onMutate: () => setSaveError(""),
    onSuccess: (data) => {
      qc.setQueryData(skillsKeys.project(projectId), data);
      setDraft(data.bindings); setBaseRevision(data.revision); setSaved(true);
    },
    onError: (error) => setSaveError(error instanceof Error ? error.message : t({ zh: "技能配置未能保存。", en: "Skill assignments could not be saved." })),
  });
  const change = (bindings: SkillBinding[]) => { setSaved(false); setDraft(bindings); };
  const patch = (binding: SkillBinding, update: Partial<SkillBinding>) => change((draft ?? []).map((b) => bindingKey(b) === bindingKey(binding) ? { ...b, ...update } : b));
  const add = (skill: LibrarySkill) => {
    if (skill.enabled && !(draft ?? []).some((b) => b.name === skill.name)) change([...(draft ?? []), bindingFor(skill)]);
    setPreview(bindingKey(skill));
  };
  const imported = (skill: LibrarySkill) => {
    void qc.invalidateQueries({ queryKey: skillsKeys.project(projectId) });
    void qc.invalidateQueries({ queryKey: skillsKeys.library });
    add(skill);
  };
  const dirty = draft !== null && config.data !== undefined && !sameBindings(draft, config.data.bindings);
  const incomplete = draft?.some((b) => b.nodes.length === 0) ?? false;
  const busy = !editable || save.isPending;
  const visible = config.data?.skills.filter((s) => `${s.name} ${s.description}`.toLowerCase().includes(search.toLowerCase())) ?? [];
  return (
    <section className="settings-compact-section project-skills" id="project-skills">
      <div className="settings-compact-heading"><div><h2>{t({ zh: "Skills · 项目技能", en: "Project skills" })}</h2><p>{t({ zh: "选择本项目使用的技能与节点。保存后从下一次节点运行开始生效。", en: "Choose skills and nodes for this project. Saved changes apply when a node next starts a run." })}</p></div><button type="button" className="settings-primary-action" disabled={busy || !dirty || incomplete} onClick={() => save.mutate()}>{save.isPending ? t({ zh: "保存中…", en: "Saving…" }) : t({ zh: "保存", en: "Save" })}</button></div>
      {editable && <SkillImport projectId={projectId} onImported={imported} disabled={save.isPending} />}
      {!editable && <p>{t({ zh: "你可以查看技能配置；修改需要项目设置权限。", en: "You can view skill assignments. Editing requires project settings access." })}</p>}
      {config.isLoading && <Skeleton height={150} />}
      {config.isError && <p className="settings-inline-error" role="alert">{t({ zh: "无法读取项目技能。", en: "Could not read project skills." })}<button type="button" onClick={() => void config.refetch()}>{t({ zh: "重试", en: "Retry" })}</button></p>}
      {config.data && <>
        <label className="skill-search"><span>{t({ zh: "搜索可用技能", en: "Search available skills" })}</span><input value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        {visible.length === 0 && <p className="skill-empty">{t({ zh: "还没有可选技能，可以直接导入或在总设置的技能库中添加。", en: "No skills to choose from. Import one here or add it in Settings → Skills." })}</p>}
        <div className="skill-list">{visible.map((skill) => {
          const binding = draft?.find((b) => bindingKey(b) === bindingKey(skill));
          const viewed = binding ? { ...skill, ...skill.revision_details?.[binding.revision], revision: binding.revision } : skill;
          return <article key={bindingKey(skill)} className={`skill-card${binding ? " is-assigned" : ""}`}>
            <div className="skill-card-heading"><label className="skill-choice"><input type="checkbox" checked={Boolean(binding)} disabled={busy || (!binding && (!skill.enabled || (draft ?? []).some((b) => b.name === skill.name)))} onChange={(event) => event.target.checked ? add(skill) : change((draft ?? []).filter((b) => bindingKey(b) !== bindingKey(skill)))} /><strong>{skill.name}</strong></label><button type="button" className="settings-secondary-action" onClick={() => setPreview(preview === bindingKey(skill) ? null : bindingKey(skill))}>{t({ zh: "预览", en: "Preview" })}</button></div>
            <p>{viewed.description}</p>
            {!skill.enabled && <p className="skill-warning">{t({ zh: "技能库中已停用，之后启动的节点不会加载。", en: "Disabled in the library. Nodes starting later will not load it." })}</p>}
            {binding && <div className="skill-assignment">
              <div className="skill-assignment-options"><label className="skill-enabled"><input type="checkbox" checked={binding.enabled} disabled={busy || !skill.enabled} onChange={(event) => patch(binding, { enabled: event.target.checked })} />{t({ zh: "在本项目启用", en: "Enable in this project" })}</label><label className="skill-file-select"><span>{t({ zh: "使用版本", en: "Revision" })}</span><select value={binding.revision} disabled={busy || !skill.enabled} onChange={(event) => patch(binding, { revision: event.target.value })}>{Array.from(new Set([binding.revision, ...skill.revisions])).map((rev) => <option key={rev} value={rev}>{rev.slice(0, 12)}{rev === skill.revision ? t({ zh: "（当前）", en: " (current)" }) : ""}</option>)}</select></label></div>
              <fieldset disabled={busy}><legend>{t({ zh: "使用节点", en: "Assigned nodes" })}</legend><div className="skill-node-choices">{config.data.nodes.map((node) => <label key={node.id}><input type="checkbox" checked={binding.nodes.includes(node.id)} onChange={(event) => patch(binding, { nodes: event.target.checked ? [...binding.nodes, node.id] : binding.nodes.filter((id) => id !== node.id) })} />{nodeDisplayName(node.id, lang)}</label>)}</div></fieldset>
              {binding.nodes.length === 0 && <p className="skill-warning">{t({ zh: "请选择至少一个节点后保存。", en: "Select at least one node before saving." })}</p>}
              {config.data.nodes.filter((node) => binding.nodes.includes(node.id) && missingTools(viewed, node).length > 0).map((node) => <p className="skill-warning" key={node.id}>{t({ zh: "{node} 尚未提供声明的工具：{tools}。", en: "{node} does not provide these declared tools: {tools}." }, { node: nodeDisplayName(node.id, lang), tools: missingTools(viewed, node).join(", ") })}</p>)}
            </div>}
            {preview === bindingKey(skill) && <SkillPreview key={`${bindingKey(skill)}/${viewed.revision}`} skill={viewed} projectId={projectId} />}
          </article>;
        })}</div>
      </>}
      {saveError && <p role="alert" className="settings-inline-error">{saveError}<button type="button" disabled={save.isPending || config.isFetching} onClick={() => { void config.refetch().then(({ data }) => { if (data) { setDraft(data.bindings); setBaseRevision(data.revision); setSaveError(""); save.reset(); } }); }}>{t({ zh: "重新加载配置", en: "Reload assignments" })}</button></p>}
      {saved && <p role="status" className="skill-saved">{t({ zh: "已保存，将在下一次节点运行时生效。", en: "Saved. Changes take effect when a node next starts a run." })}</p>}
    </section>
  );
}

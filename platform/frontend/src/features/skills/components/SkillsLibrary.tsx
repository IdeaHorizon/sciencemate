"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "@/shared/i18n";
import { Skeleton } from "@/shared/ui";
import { useProjects } from "@/features/projects";
import { skillsClient, skillsKeys } from "../api";
import { SkillImport } from "./SkillImport";
import { SkillPreview } from "./SkillPreview";
import type { LibrarySkill } from "../types";

export function SkillsLibrary() {
  const t = useT();
  const qc = useQueryClient();
  const library = useQuery({ queryKey: skillsKeys.library, queryFn: skillsClient.list });
  const projects = useProjects();
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [projectId, setProjectId] = useState("");
  const [revision, setRevision] = useState("");
  const [toggleError, setToggleError] = useState("");
  const toggle = useMutation({
    mutationFn: (skill: LibrarySkill) => skillsClient.setEnabled(skill.name, !skill.enabled),
    onMutate: () => setToggleError(""),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
    onError: (error) => setToggleError(error instanceof Error ? error.message : t({ zh: "无法修改技能状态。", en: "Could not change skill availability." })),
  });
  const imported = (skill: LibrarySkill) => {
    setSelected(skill.name); setRevision("");
    void qc.invalidateQueries({ queryKey: ["skills"] });
  };
  const picked = library.data?.skills.find((skill) => skill.name === selected);
  const viewed = picked ? { ...picked, ...picked.revision_details?.[revision || picked.revision], revision: revision || picked.revision } : null;
  const filtered = library.data?.skills.filter((skill) => `${skill.name} ${skill.description}`.toLowerCase().includes(query.toLowerCase())) ?? [];
  const localProjects = projects.data?.filter((p) => p.status === "active" && p.home?.kind !== "organisation") ?? [];
  const knownTools = new Set(library.data?.nodes.flatMap((node) => node.tools) ?? []);
  const missing = viewed?.tools_used.filter((tool) => !knownTools.has(tool)) ?? [];
  return (
    <div className="settings-page skills-library-page">
      <header className="settings-page-header"><h1>{t({ zh: "技能库", en: "Skills" })}</h1><p>{t({ zh: "导入可复用的研究方法，再到项目设置中选择使用节点。", en: "Import reusable research methods, then assign them to nodes in project settings." })}</p></header>
      <SkillImport onImported={imported} />
      <label className="skill-search"><span>{t({ zh: "搜索技能", en: "Search skills" })}</span><input value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      {library.isLoading && <Skeleton height={200} />}
      {library.isError && <p role="alert" className="settings-inline-error">{t({ zh: "无法读取技能库。", en: "Could not read the skill library." })}<button type="button" onClick={() => void library.refetch()}>{t({ zh: "重试", en: "Retry" })}</button></p>}
      {toggleError && <p role="alert" className="settings-inline-error">{toggleError}</p>}
      {library.data && filtered.length === 0 && <p className="skill-empty">{library.data.skills.length ? t({ zh: "没有匹配的技能。", en: "No matching skills." }) : t({ zh: "还没有导入技能。导入后，在项目设置中分配给需要的节点。", en: "No imported skills yet. After importing, assign them to nodes in project settings." })}</p>}
      <div className="skills-library-grid">
        <div className="skill-list">{filtered.map((skill) => <article key={skill.name} className={`skill-card${selected === skill.name ? " is-selected" : ""}`}>
          <div className="skill-card-heading"><button type="button" onClick={() => { setSelected(skill.name); setRevision(""); }}>{skill.name}</button><label className="skill-enabled"><input type="checkbox" checked={skill.enabled} disabled={toggle.isPending} onChange={() => toggle.mutate(skill)} />{t({ zh: "可用", en: "Available" })}</label></div>
          <p>{skill.description}</p><small>{skill.revision.slice(0, 12)} · {t({ zh: "{count} 个文件", en: "{count} files" }, { count: skill.files.length })}</small>
          {!skill.enabled && <p className="skill-warning">{t({ zh: "已停用，之后启动的节点不会加载它。", en: "Disabled. Nodes starting later will not load this skill." })}</p>}
        </article>)}</div>
        {picked && <div className="skill-detail">
          {picked.revisions.length > 1 && <label className="skill-file-select"><span>{t({ zh: "版本", en: "Revision" })}</span><select value={revision || picked.revision} onChange={(event) => setRevision(event.target.value)}>{picked.revisions.map((rev) => <option key={rev} value={rev}>{rev.slice(0, 12)}{rev === picked.revision ? t({ zh: "（当前）", en: " (current)" }) : ""}</option>)}</select></label>}
          {missing.length > 0 && <p className="skill-warning">{t({ zh: "平台尚未提供声明的工具：{tools}。使用前需要适配。", en: "These declared tools are not available in the platform: {tools}. Adapt the skill before use." }, { tools: missing.join(", ") })}</p>}
          {viewed && <SkillPreview key={`${picked.name}/${revision}`} skill={viewed} />}
          {localProjects.length > 0 && <div className="skill-project-link"><label><span>{t({ zh: "添加到项目", en: "Add to project" })}</span><select value={projectId} onChange={(event) => setProjectId(event.target.value)}><option value="">{t({ zh: "选择项目", en: "Choose a project" })}</option>{localProjects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>{projectId && <Link className="settings-primary-action" href={`/projects/${encodeURIComponent(projectId)}/settings?skill=${encodeURIComponent(picked.name)}#project-skills`}>{t({ zh: "选择使用节点", en: "Choose nodes" })}</Link>}</div>}
        </div>}
      </div>
    </div>
  );
}

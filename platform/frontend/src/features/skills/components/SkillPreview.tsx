"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useT } from "@/shared/i18n";
import { RichText } from "@/features/chat/components/RichText";
import { Skeleton } from "@/shared/ui";
import { skillsClient } from "../api";
import type { LibrarySkill } from "../types";

export function SkillPreview({ skill, projectId }: { skill: LibrarySkill; projectId?: string }) {
  const t = useT();
  const [asset, setAsset] = useState("");
  const preview = useQuery({
    queryKey: ["skills", "preview", projectId ?? "personal", skill.owner_id, skill.name, skill.revision, asset],
    queryFn: () => skillsClient.preview(skill, asset, projectId),
  });
  return (
    <section className="skill-preview" aria-label={t({ zh: "技能预览", en: "Skill preview" })}>
      <header><h3>{skill.name}</h3><code>{skill.revision.slice(0, 12)}</code></header>
      <p>{skill.description}</p>
      <label className="skill-file-select">
        <span>{t({ zh: "查看文件", en: "View file" })}</span>
        <select value={asset} onChange={(event) => setAsset(event.target.value)}>
          <option value="">SKILL.md</option>
          {(preview.data?.files ?? skill.files).filter((file) => file !== "SKILL.md").map((file) => <option key={file} value={file}>{file}</option>)}
        </select>
      </label>
      {skill.tools_used.length > 0 && <p className="skill-tools">{t({ zh: "声明的工具：", en: "Declared tools: " })}{skill.tools_used.join(", ")}</p>}
      {preview.isLoading && <Skeleton height={180} />}
      {preview.isError && <p role="alert" className="settings-inline-error">{preview.error instanceof Error ? preview.error.message : t({ zh: "无法读取技能。", en: "Could not read the skill." })}</p>}
      {preview.data && (preview.data.binary
        ? <p>{t({ zh: "该文件是二进制附件，已随技能保留。", en: "This binary attachment is stored with the skill." })}</p>
        : <div className="skill-preview-content">{asset && !asset.endsWith(".md")
          ? <pre>{preview.data.content}</pre>
          : <RichText text={preview.data.content.replace(/^---\s*\r?\n[\s\S]*?\r?\n---\s*\r?\n/, "")} />}</div>)}
      {preview.data?.truncated && <small>{t({ zh: "预览显示前 128 KB，完整文件已保留。", en: "Preview shows the first 128 KB. The complete file is retained." })}</small>}
    </section>
  );
}

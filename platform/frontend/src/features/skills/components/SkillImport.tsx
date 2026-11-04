"use client";

import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { FolderOpen, Upload } from "lucide-react";
import { useT } from "@/shared/i18n";
import { skillsClient } from "../api";
import type { LibrarySkill } from "../types";

export function SkillImport({ projectId, onImported, disabled = false }: {
  projectId?: string; onImported: (skill: LibrarySkill) => void; disabled?: boolean;
}) {
  const t = useT();
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const [error, setError] = useState("");
  const upload = useMutation({
    mutationFn: (files: File[]) => skillsClient.import(files, projectId),
    onSuccess: (skill) => { setError(""); onImported(skill); },
    onError: (err) => setError(err instanceof Error ? err.message : t({
      zh: "技能导入失败，请重试。", en: "The skill could not be imported. Please try again.",
    })),
  });
  const select = (input: HTMLInputElement) => {
    const files = Array.from(input.files ?? []);
    if (files.length) { setError(""); upload.mutate(files); }
    input.value = "";
  };
  const busy = disabled || upload.isPending;
  return (
    <div className="skill-import">
      <div className="skill-actions">
        <button type="button" className="settings-primary-action" disabled={busy} onClick={() => fileInput.current?.click()}>
          <Upload size={14} />{upload.isPending ? t({ zh: "导入中…", en: "Importing…" }) : t({ zh: "导入 Skill", en: "Import skill" })}
        </button>
        <button type="button" className="settings-secondary-action" disabled={busy} onClick={() => folderInput.current?.click()}>
          <FolderOpen size={14} />{t({ zh: "导入文件夹", en: "Import folder" })}
        </button>
      </div>
      <input ref={fileInput} type="file" accept=".zip,.md" hidden onChange={(event) => select(event.currentTarget)} />
      <input ref={folderInput} type="file" {...{ webkitdirectory: "", directory: "" }} multiple hidden onChange={(event) => select(event.currentTarget)} />
      <small>{t({ zh: "支持 ZIP、SKILL.md 或完整技能文件夹。最多 8 MB。", en: "ZIP, SKILL.md or a complete skill folder. Up to 8 MB." })}</small>
      {error && <p className="settings-inline-error" role="alert">{error}</p>}
    </div>
  );
}

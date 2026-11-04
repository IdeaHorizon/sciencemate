"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { pushError, pushSuccess } from "@/stores/notification";
import { Skeleton } from "@/shared/ui";

import { instructionsClient } from "../api/instructions-client";
import type { InstructionFile } from "../types";
import { useT } from "@/shared/i18n";

/**
 * 一段指令 = 一个文件。
 *
 * 从前这里是「起草 → 发布 → 版本 → 快照」四步，配三张表和一个治理界面。它们
 * 回答的问题只有一个：这个课题的指令是什么。答案是一段文本（RFC X2）。
 *
 * 路径要显示出来：这个文件用别的编辑器改也算数（agent 读的就是它），
 * 藏起来会让人以为只有这个界面能改。
 */
export function InstructionFileEditor({
  title,
  description,
  queryKey,
  read,
  write,
  editable = true,
}: {
  title: string;
  description: string;
  queryKey: string[];
  read: () => Promise<InstructionFile>;
  write: (content: string) => Promise<InstructionFile>;
  editable?: boolean;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const file = useQuery({ queryKey, queryFn: read });
  const [draft, setDraft] = useState<string | null>(null);

  useEffect(() => {
    if (file.data && draft === null) setDraft(file.data.content);
  }, [file.data, draft]);

  const save = useMutation({
    mutationFn: (content: string) => write(content),
    onSuccess: (saved) => {
      setDraft(saved.content);
      void queryClient.invalidateQueries({ queryKey });
      pushSuccess(t({ zh: "从下一个新会话开始生效", en: "Takes effect from the next new session" }), t({ zh: "已保存", en: "Saved" }));
    },
    onError: (error) =>
      pushError(error instanceof Error ? error.message : t({ zh: "请稍后重试。", en: "Please try again later." }), t({ zh: "保存失败", en: "Save failed" })),
  });

  const dirty = draft !== null && file.data !== undefined && draft !== file.data.content;

  return (
    <section className="settings-compact-section instruction-file">
      <div className="settings-compact-heading">
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </div>
      {file.isLoading && <Skeleton height={160} />}
      {file.isError && <p className="instruction-file-error">{t({ zh: "无法读取指令文件。", en: "Could not read the instruction file." })}</p>}
      {file.data && (
        <>
          <textarea
            className="instruction-file-body"
            value={draft ?? ""}
            onChange={(event) => setDraft(event.target.value)}
            rows={14}
            spellCheck={false}
            readOnly={!editable}
            aria-label={title}
          />
          <div className="instruction-file-footer">
            <code title={t({ zh: "也可以直接用其他编辑器修改这个文件。", en: "You can also edit this file in another editor." })}>{file.data.path}</code>
            {editable && (
              <button
                type="button"
                disabled={!dirty || save.isPending}
                onClick={() => draft !== null && save.mutate(draft)}
              >
                {save.isPending ? t({ zh: "保存中…", en: "Saving…" }) : dirty ? t({ zh: "保存", en: "Save" }) : t({ zh: "已保存", en: "Saved" })}
              </button>
            )}
          </div>
        </>
      )}
    </section>
  );
}

export function PersonalInstructionsEditor() {
  const t = useT();
  return (
    <InstructionFileEditor
      title={t({ zh: "长期指令（PROFILE.md）", en: "Standing instructions (PROFILE.md)" })}
      description={t({ zh: "在所有项目里都生效。修改后从下一个新会话开始生效。", en: "Applies across all projects. Changes take effect from the next new session." })}
      queryKey={["instructions", "personal"]}
      read={instructionsClient.readPersonal}
      write={instructionsClient.writePersonal}
    />
  );
}

export function ProjectInstructionsEditor({ projectId, editable = true }: {
  projectId: string;
  editable?: boolean;
}) {
  const t = useT();
  return (
    <InstructionFileEditor
      title={t({ zh: "项目指令（PROJECT.md）", en: "Project instructions (PROJECT.md)" })}
      description={t({ zh: "只在本项目中生效，从下一个新会话开始生效。", en: "Applies in this project only, from the next new session." })}
      queryKey={["instructions", "project", projectId]}
      read={() => instructionsClient.readProject(projectId)}
      write={(content) => instructionsClient.writeProject(projectId, content)}
      editable={editable}
    />
  );
}

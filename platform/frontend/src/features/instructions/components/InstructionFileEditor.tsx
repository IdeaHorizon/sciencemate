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
      pushSuccess(t({ zh: "指令已保存", en: "Instructions saved" }), t({ zh: "下一个新会话会看到它", en: "The next new session will see it" }));
    },
    onError: (error) =>
      pushError(error instanceof Error ? error.message : t({ zh: "没能保存这份指令", en: "This instruction could not be saved" }), t({ zh: "未保存", en: "Unsaved" })),
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
      {file.isError && <p className="instruction-file-error">{t({ zh: "读不到这份指令。", en: "This instruction could not be read." })}</p>}
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
            <code title={t({ zh: "agent 读的就是这个文件；用别的编辑器改它也算数", en: "The agent reads this very file; editing it elsewhere counts too" })}>{file.data.path}</code>
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
      title={t({ zh: "PROFILE.md · 你自己的长期指令", en: "PROFILE.md · your standing instructions" })}
      description={t({ zh: "跨课题都生效的偏好与要求。新会话开跑时冻结一份，之后再改不影响已经在跑的。", en: "Preferences and requirements that apply across projects. A copy is frozen when a new session starts; later edits do not affect sessions already running." })}
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
      title={t({ zh: "PROJECT.md · 这个课题的指令", en: "PROJECT.md · instructions for this project" })}
      description={t({ zh: "只在这个课题里生效。新会话开跑时冻结一份。", en: "Applies inside this project only. A copy is frozen when a new session starts." })}
      queryKey={["instructions", "project", projectId]}
      read={() => instructionsClient.readProject(projectId)}
      write={(content) => instructionsClient.writeProject(projectId, content)}
      editable={editable}
    />
  );
}

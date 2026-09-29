"use client";

import { useQuery } from "@tanstack/react-query";
import { FileCheck2, X } from "lucide-react";

import { instructionsClient } from "../api/instructions-client";
import { useT } from "@/shared/i18n";

const LAYERS = ["project", "personal"] as const;

/**
 * 会话信息抽屉 —— 版本 / 驾驶者 / **这个会话现在读到的指令**。
 *
 * 项目层是它自己 worktree 分支上的 `PROJECT.md`：别人改主干不会从它脚下抽走，
 * 这就是冻结，而且冻结的是 git 不是数据库里的抄件。个人层是当下的文件 ——
 * 用户改了偏好下一轮就生效（RFC X3）。
 */
export function SessionAboutDrawer({
  projectId,
  sessionId,
  enabled,
  open,
  onClose,
  facts,
}: {
  projectId: string;
  sessionId: string;
  enabled: boolean;
  open: boolean;
  onClose: () => void;
  facts: { term: string; value: string }[];
}) {
  const t = useT();
  const effective = useQuery({
    queryKey: ["instructions", "session", projectId, sessionId],
    queryFn: () => instructionsClient.readSessionInstructions(projectId, sessionId),
    enabled: enabled && open,
    retry: false,
  });

  if (!open) return null;

  return (
    <aside className="session-about-drawer" aria-label={t({ zh: "会话信息", en: "Session details" })}>
      <header>
        <h2><FileCheck2 size={15} />{t({ zh: "会话信息", en: "Session details" })}</h2>
        <button type="button" onClick={onClose} aria-label={t({ zh: "关闭", en: "Close" })}><X size={14} /></button>
      </header>
      <dl>
        {facts.map((fact) => (
          <div key={fact.term}><dt>{fact.term}</dt><dd>{fact.value}</dd></div>
        ))}
      </dl>
      <section>
        <h3>{t({ zh: "当前生效的指令", en: "Instructions in effect" })}</h3>
        {effective.isLoading && <p>{t({ zh: "正在读…", en: "Reading…" })}</p>}
        {effective.isError && <p>{t({ zh: "读不到这个会话的指令。", en: "The instructions for this session could not be read." })}</p>}
        {effective.data && LAYERS.map((kind) => {
          const layer = effective.data.layers[kind];
          if (!layer) return null;
          return (
            <details key={kind}>
              <summary>
                {layer.filename}{" "}
                {/* 没内容就没有指纹 —— 那时印一串 undefined 才是骗人。 */}
                {layer.sha256 ? <code>{layer.sha256.slice(0, 12)}</code> : <em>{t({ zh: "（空）", en: "(empty)" })}</em>}
              </summary>
              <pre>{layer.content}</pre>
            </details>
          );
        })}
      </section>
    </aside>
  );
}

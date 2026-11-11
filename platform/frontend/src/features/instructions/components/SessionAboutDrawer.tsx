"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, FileCheck2, X } from "lucide-react";
import { useEffect, useId, useRef } from "react";

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
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const effective = useQuery({
    queryKey: ["instructions", "session", projectId, sessionId],
    queryFn: () => instructionsClient.readSessionInstructions(projectId, sessionId),
    enabled: enabled && open,
    retry: false,
  });

  // 放进浏览器的模态层，避免抽屉占用会话网格的一行；焦点和 Escape 也由 dialog 管理。
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!open || !dialog) return;
    dialog.showModal();
    return () => dialog.close();
  }, [open]);

  const closeDrawer = () => {
    dialogRef.current?.close();
    onClose();
  };

  if (!open) return null;

  return (
    <dialog
      ref={dialogRef}
      className="session-about-drawer"
      aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); closeDrawer(); }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        const bounds = event.currentTarget.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeDrawer();
      }}
    >
      <header>
        <h2 id={titleId}><FileCheck2 size={15} />{t({ zh: "会话信息", en: "Session details" })}</h2>
        <button type="button" onClick={closeDrawer} aria-label={t({ zh: "关闭", en: "Close" })}><X size={14} /></button>
      </header>
      <div className="session-about-content">
        <dl className="session-about-facts">
          {facts.map((fact) => (
            <div key={fact.term}><dt>{fact.term}</dt><dd>{fact.value}</dd></div>
          ))}
        </dl>
        <section>
          <h3 className="session-about-instruction-label">{t({ zh: "当前生效的指令", en: "Instructions in effect" })}</h3>
          {effective.isLoading && <p>{t({ zh: "正在加载…", en: "Loading…" })}</p>}
          {effective.isError && <p>{t({ zh: "无法加载这个会话的指令。", en: "The instructions for this session could not be read." })}</p>}
          <div className="session-about-sources">
            {effective.data && LAYERS.map((kind) => {
              const layer = effective.data.layers[kind];
              if (!layer) return null;
              return (
                <details key={kind}>
                  <summary>
                    <ChevronRight size={13} aria-hidden="true" />
                    <span>{layer.filename}</span>
                    {/* 没内容就没有指纹 —— 那时印一串 undefined 才是骗人。 */}
                    {layer.sha256 ? <code>{layer.sha256.slice(0, 12)}</code> : <em>{t({ zh: "（空）", en: "(empty)" })}</em>}
                  </summary>
                  <pre>{layer.content}</pre>
                </details>
              );
            })}
          </div>
        </section>
      </div>
    </dialog>
  );
}

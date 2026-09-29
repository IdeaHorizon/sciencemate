"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { FolderPlus, Loader2 } from "lucide-react";
import type { FeedItem } from "@/lib/api";
import { useProjects } from "@/features/projects";
import { createProjectSession } from "@/features/sessions/api/session-repository";
import { pushError } from "@/stores/notification";
import { useLanguage } from "@/shared/i18n";
import { fc } from "../lib/feed-copy";
import { handoffDraft, handoffHref, handoffSessionTitle } from "../lib/handoff";

/**
 * 「接入课题」：挑一个课题 → 在它下面新建一个会话 → 带着这条资讯落进输入框。
 *
 * ## 为什么不是一个链接
 *
 * 第一版是 `<a href="/projects?feed_item=…">`。它**看起来**在工作：点了会跳走。
 * 但 Projects 页根本不读那个参数 —— 用户拿到的是一个普通的课题列表，那篇论文
 * 什么都没跟过来。按钮许诺了一个界面给不出的能力，而且没有任何一层会报错：
 * 我甚至写了一条测试断言"接入课题"这几个字存在，它一直是绿的。
 *
 * 真跑一次、点一下，才看得出这条路是空的。
 */
export function HandoffAction({
  item,
  onOpened,
}: {
  item: FeedItem;
  onOpened: () => void;
}) {
  const router = useRouter();
  const lang = useLanguage();
  const projects = useProjects();
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);

  const options = projects.data ?? [];

  const handoff = async (projectId: string) => {
    setBusy(true);
    try {
      const session = await createProjectSession(projectId, handoffSessionTitle(item));
      onOpened();
      router.push(handoffHref(projectId, session.id, handoffDraft(item, lang)));
    } catch (caught) {
      // 建不出会话就说清楚，别把人送到一个不存在的地址。
      pushError(caught instanceof Error ? caught.message : fc("handoff.failed", lang));
      setBusy(false);
      setPicking(false);
    }
  };

  if (!picking) {
    return (
      <button
        type="button"
        className="feed-action"
        title={fc("handoff.action.hint", lang)}
        onClick={() => setPicking(true)}
      >
        <FolderPlus size={15} />
        <span>{fc("handoff.action", lang)}</span>
      </button>
    );
  }

  if (projects.isLoading) {
    return (
      <span className="feed-action feed-action-quiet">
        <Loader2 size={15} className="feed-spin" />
        <span>{fc("handoff.loading_projects", lang)}</span>
      </span>
    );
  }

  if (options.length === 0) {
    // 一个课题都没有时说实话，而不是给一个点了没反应的菜单。
    return (
      <span className="feed-handoff-empty">{fc("handoff.no_projects", lang)}</span>
    );
  }

  return (
    <div className="feed-handoff">
      <span className="feed-handoff-label">{fc("handoff.which_project", lang)}</span>
      {options.slice(0, 6).map((project) => (
        <button
          key={project.id}
          type="button"
          className="feed-chip"
          disabled={busy}
          onClick={() => void handoff(project.id)}
        >
          {project.name}
        </button>
      ))}
      <button type="button" className="feed-action feed-action-quiet" onClick={() => setPicking(false)}>
        {fc("action.cancel", lang)}
      </button>
    </div>
  );
}

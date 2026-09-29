"use client";

import { FeedHome } from "@/features/feed";
import { useT } from "@/shared/i18n";

export default function FeedPage() {
  const t = useT();
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">{t({ zh: "科研资讯", en: "Research feed" })}</h1>
          <p className="page-subtitle">{t({ zh: "今天你这个领域发生了什么 —— 按你在跑的课题和关注方向挑的。", en: "What happened in your field today, picked from your projects and stated interests." })}</p>
        </div>
      </header>
      <FeedHome />
    </div>
  );
}

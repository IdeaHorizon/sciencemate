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
        </div>
      </header>
      <FeedHome />
    </div>
  );
}

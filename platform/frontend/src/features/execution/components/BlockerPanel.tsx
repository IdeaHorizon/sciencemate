"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { AlertTriangle } from "lucide-react";

import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Badge, Card } from "@/shared/ui";

import { adaptBlockers, blockerCategoryLabel, openBlockers } from "../lib/blockers";
import { useT } from "@/shared/i18n";

/**
 * Blocker 面板 —— "现在有什么在等我，它要我做什么"。
 *
 * 节点报阻塞（report_blocker）此前只以事件存在，用户要逐个翻 run 的事件流
 * 才看得到；实际结果是 run 莫名 incomplete，没人知道为什么。这里把它提到
 * 项目层面：没有阻塞时整个面板不渲染，有阻塞时它排在活动列表前面。
 */
export function BlockerPanel({ projectId }: { projectId: string }) {
  const t = useT();
  const { data } = useQuery({
    queryKey: qk.projectBlockers(projectId),
    queryFn: () => api.listProjectBlockers(projectId),
    refetchInterval: 15000,
  });

  const blockers = openBlockers(adaptBlockers(data));
  if (blockers.length === 0) return null;

  return (
    <section className="blocker-panel">
      <div className="platform-section-heading">
        <h2><AlertTriangle size={14} />{t({ zh: "被卡住", en: "Blocked" })}</h2>
        <span>{blockers.length} waiting on a decision or resource</span>
      </div>
      <div className="blocker-list">
        {blockers.map((blocker) => (
          <Card key={blocker.id}>
            <Card.Body>
              <div className="blocker-head">
                <Badge size="sm" kind="warning">{blockerCategoryLabel(blocker.category)}</Badge>
                <code className="blocker-node">{blocker.reportingNode}</code>
                {!blocker.retryableAfterChange && (
                  <Badge size="sm" kind="danger">{t({ zh: "按设计不可重试", en: "Not retryable as designed" })}</Badge>
                )}
                <Link
                  className="blocker-link"
                  href={`/projects/${projectId}/sessions/${encodeURIComponent(blocker.sessionId)}`}
                >{t({ zh: "打开会话", en: "Open session" })}</Link>
              </div>
              <p className="blocker-summary">{blocker.summary}</p>
              {blocker.requestedAction && (
                <p className="blocker-action"><strong>{t({ zh: "还缺：", en: "Needs:" })}</strong> {blocker.requestedAction}</p>
              )}
              {blocker.evidencePaths.length > 0 && (
                <p className="blocker-evidence">
                  {blocker.evidencePaths.map((path) => (
                    <code key={path}>{path}</code>
                  ))}
                </p>
              )}
            </Card.Body>
          </Card>
        ))}
      </div>
    </section>
  );
}

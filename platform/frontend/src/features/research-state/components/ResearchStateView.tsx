"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Badge, Card, Empty, Skeleton } from "@/shared/ui";

import { useProjectTruthSession } from "../hooks/useProjectTruthSession";
import {
  hypothesisStatusLabel,
  parseResearchState,
  researchStateHead,
  statusTone,
  unresolvedHypotheses,
  verdictLabel,
  verdictTone,
} from "../lib/research-state";
import { useLanguage, useT } from "@/shared/i18n";

/**
 * Research State —— Analysis 的裁决总账（v2.1 P3c）。
 *
 * 这是项目"到底走到哪了"唯一说得清的地方：哪些假说还没裁决、哪些被证伪
 * 及依据是什么、计划改过几版、下一步是什么。它决定项目能不能进 writing
 * （verdict=ready_candidate 要求 prereg 假说全部已裁决）。
 *
 * 真相源是 Git 里的记录账本（`/repository/records`），不是 DB —— 与 Memory
 * 视图同规。裁决表这种结构化事实在账本的 metadata 里，正文文件里没有。
 */
export function ResearchStateView({ projectId }: { projectId: string }) {
  const t = useT();
  const lang = useLanguage();
  // checkpoint 在 session 分支上；不带 sessionId 读到的是 main（publish 前是空的）
  const truth = useProjectTruthSession(projectId);
  const records = useQuery({
    queryKey: qk.projectRecords(projectId, truth.sessionId ?? ""),
    queryFn: () => api.getProjectRecords(projectId, truth.sessionId, "research_state"),
    enabled: !truth.isLoading,
  });

  if (truth.isLoading || records.isLoading) return <Skeleton height={180} />;

  if (records.error) {
    return (
      <Empty
        title={t({ zh: "无法加载研究进展", en: "Research state is unavailable" })}
        hint={t({ zh: "请稍后重试。", en: "Please try again later." })}
      />
    );
  }
  const head = researchStateHead(records.data?.records ?? []);
  if (!head) {
    return (
      <Empty
        title={t({ zh: "还没有研究进展", en: "No research state yet" })}
        hint={t({ zh: "预注册定稿后，研究进展会显示在这里。开始一个研究会话即可生成。", en: "Research state appears here once the pre-registration is finalized. Start a research session to create it." })}
      />
    );
  }

  const state = parseResearchState(head.metadata);
  if (!state) {
    return (
      <Empty
        title={t({ zh: "无法读取研究进展", en: "Research state could not be read" })}
        hint={t({ zh: "{path} 中缺少研究进展数据。", en: "{path} is missing research state data." }, { path: head.path })}
      />
    );
  }

  const unresolved = unresolvedHypotheses(state);

  return (
    <div className="stack-md">
      <Card>
        <Card.Body>
          <div className="rs-head">
            <Badge size="md" kind={verdictTone(state.verdict)}>{state.verdict ? verdictLabel(state.verdict, lang) : "—"}</Badge>
            <span className="rs-version">
              v{state.version}
              {state.parentVersion !== null ? ` ← v${state.parentVersion}` : t({ zh: " （首版）", en: " (first version)" })}
            </span>
          </div>
          <p className="rs-summary">
            {unresolved.length === 0
              ? t({ zh: "所有假设都已判定。", en: "All hypotheses are resolved." })
              : t({ zh: "{n} 个假设尚待判定：{ids}", en: "{n} hypotheses are still open: {ids}" }, {
                n: unresolved.length,
                ids: unresolved.map((h) => h.id).join(lang === "zh" ? "、" : ", "),
              })}
            {state.planVersion ? t({ zh: " · 计划版本：{v}", en: " · plan: {v}" }, { v: state.planVersion }) : ""}
          </p>
          {state.changeReason && (
            <p className="rs-change">
              <strong>{t({ zh: "本版改动原因：", en: "Reason for this version:" })}</strong> {state.changeReason}
            </p>
          )}
        </Card.Body>
      </Card>

      <Card>
        <Card.Body>
          <h2 className="rs-section">{t({ zh: "假设", en: "Hypotheses" })}</h2>
          <div className="rs-table-scroll">
            <table className="rs-table">
              <thead>
                <tr><th>ID</th><th>{t({ zh: "状态", en: "Status" })}</th><th>{t({ zh: "证据", en: "Evidence" })}</th><th>{t({ zh: "备注", en: "Note" })}</th></tr>
              </thead>
              <tbody>
                {state.hypotheses.map((row) => (
                  <tr key={row.id}>
                    <td><code>{row.id}</code></td>
                    <td><Badge size="sm" kind={statusTone(row.status)}>{hypothesisStatusLabel(row.status, lang)}</Badge></td>
                    <td>
                      {row.evidence.length === 0
                        ? <span className="rs-dim">—</span>
                        : row.evidence.map((item) => <code key={item} className="rs-ev">{item}</code>)}
                    </td>
                    <td>{row.note ?? <span className="rs-dim">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card.Body>
      </Card>

      {state.completedExperiments.length > 0 && (
        <Card>
          <Card.Body>
            <h2 className="rs-section">{t({ zh: "已完成的实验", en: "Completed experiments" })}</h2>
            <ul className="rs-list">
              {state.completedExperiments.map((row) => (
                <li key={row.ref}>
                  <code>{row.ref}</code>{t({ zh: "：可信度 {v}", en: " — credibility: {v}" }, { v: row.credibility })}
                </li>
              ))}
            </ul>
          </Card.Body>
        </Card>
      )}

      {(state.gaps.length > 0 || state.nextSteps.length > 0) && (
        <Card>
          <Card.Body>
            {state.gaps.length > 0 && (
              <>
                <h2 className="rs-section">{t({ zh: "缺口", en: "Gaps" })}</h2>
                <ul className="rs-list">{state.gaps.map((g) => <li key={g}>{g}</li>)}</ul>
              </>
            )}
            {state.nextSteps.length > 0 && (
              <>
                <h2 className="rs-section">{t({ zh: "下一步", en: "Next steps" })}</h2>
                <ul className="rs-list">{state.nextSteps.map((s) => <li key={s}>{s}</li>)}</ul>
              </>
            )}
          </Card.Body>
        </Card>
      )}
    </div>
  );
}

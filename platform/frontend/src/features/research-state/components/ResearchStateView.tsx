"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Badge, Card, Empty, Skeleton } from "@/shared/ui";

import { useProjectTruthSession } from "../hooks/useProjectTruthSession";
import {
  parseResearchState,
  researchStateHead,
  statusTone,
  unresolvedHypotheses,
  verdictTone,
} from "../lib/research-state";
import { useT } from "@/shared/i18n";

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
        title={t({ zh: "读不到研究进展", en: "Research state is unavailable" })}
        hint={t({ zh: "读不到这个项目的仓库。", en: "The Project repository could not be read." })}
      />
    );
  }
  const head = researchStateHead(records.data?.records ?? []);
  if (!head && records.data?.legacyLayout) {
    // 与后端 research_state_reader 的 UnmigratedWorkspaceError 同一句话：
    // 不读旧布局，但也不能装作没看见。
    return (
      <Empty
        title={t({ zh: "这个项目用的是迁移前的旧布局", en: "Research state uses a pre-migration layout" })}
        hint={t({ zh: "这个项目建于记录改成原生文件加账本之前。这个版本读不了它 —— 新建一个项目继续研究。", en: "This Project was created before records became native files with a ledger. This version does not read it — start a new Project to continue the research." })}
        // 这句话叫人「新建一个项目」，就得给他一条过去的路。扫盘闸
        // （shared/ui/an-empty-state-that-tells-you-to-act-offers-it）在项目页
        // 那处同样的毛病被修掉之后，顺手把这一处也揪了出来。
        action={<Link className="ui-empty-link" href="/projects">{t({ zh: "去新建项目", en: "Go to Projects" })}</Link>}
      />
    );
  }
  if (!head) {
    return (
      <Empty
        title={t({ zh: "还没有研究进展", en: "No research state yet" })}
        hint={t({ zh: "预注册冻结之后，分析会写出 research_state。开一个研究会话来生成它。", en: "Analysis writes research_state once the pre-registration is frozen. Start a research session to create it." })}
      />
    );
  }

  const state = parseResearchState(head.metadata);
  if (!state) {
    return (
      <Empty
        title={t({ zh: "研究进展解析不出来", en: "Research state could not be parsed" })}
        hint={`${head.path} does not carry the expected metadata.`}
      />
    );
  }

  const unresolved = unresolvedHypotheses(state);

  return (
    <div className="stack-md">
      <Card>
        <Card.Body>
          <div className="rs-head">
            <Badge size="md" kind={verdictTone(state.verdict)}>{state.verdict || "—"}</Badge>
            <span className="rs-version">
              v{state.version}
              {state.parentVersion !== null ? ` ← v${state.parentVersion}` : t({ zh: " （首版）", en: " (first version)" })}
            </span>
          </div>
          <p className="rs-summary">
            {unresolved.length === 0
              ? t({ zh: "所有假说均已裁决。", en: "Every hypothesis has been adjudicated." })
              : t({ zh: `${unresolved.length} 条假说尚未裁决：${unresolved.map((h) => h.id).join(", ")}`, en: `${unresolved.length} hypotheses are still open: ${unresolved.map((h) => h.id).join(", ")}` })}
            {state.planVersion ? ` · plan: ${state.planVersion}` : ""}
          </p>
          {state.changeReason && (
            <p className="rs-change">
              <strong>{t({ zh: "相对上一版：", en: "Since the last version:" })}</strong> {state.changeReason}
            </p>
          )}
        </Card.Body>
      </Card>

      <Card>
        <Card.Body>
          <h2 className="rs-section">{t({ zh: "假说", en: "Hypotheses" })}</h2>
          <div className="rs-table-scroll">
            <table className="rs-table">
              <thead>
                <tr><th>ID</th><th>{t({ zh: "状态", en: "Status" })}</th><th>{t({ zh: "证据", en: "Evidence" })}</th><th>{t({ zh: "备注", en: "Note" })}</th></tr>
              </thead>
              <tbody>
                {state.hypotheses.map((row) => (
                  <tr key={row.id}>
                    <td><code>{row.id}</code></td>
                    <td><Badge size="sm" kind={statusTone(row.status)}>{row.status}</Badge></td>
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
                  <code>{row.ref}</code> — credibility: {row.credibility}
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

import {say, type Language, type Phrase} from "../../../shared/i18n/language.ts";
import type {LiteraturePaper, LiteratureQueryGroup, LiteratureSearchResponse} from "@/lib/api";
import type {CnkiCollector, CnkiJob} from "../api/cnki";

export type SearchChannel = {state: string; detail: string; count?: number;
  groups?: {label: string; state: string; count: number; detail: string}[]};

const doiKey = (p: LiteraturePaper) => (p.doi || "").replace(/^https?:\/\/(dx\.)?doi.org\//i, "").trim().toLowerCase();
const titleKey = (p: LiteraturePaper) => p.title.normalize("NFKC").toLowerCase().replace(/[\s\p{P}\p{S}]/gu, "") + ":" + (p.year || "");

/** Preserve source order, interleave catalogues, and deduplicate by DOI OR title/year.
 * CNKI does not supply our quality scores: rank fusion must not invent them. */
export function mergeLiteratureResults(query: string, plan: LiteratureQueryGroup[], results: LiteratureSearchResponse[], limit = 200): LiteratureSearchResponse {
  const papers: LiteraturePaper[] = [], dois = new Map<string, LiteraturePaper>(), titles = new Map<string, LiteraturePaper>();
  const maximum = Math.max(0, ...results.map(r => r.papers.length));
  for (let row = 0; row < maximum; row++) for (const result of results) {
    const p = result.papers[row];
    if (!p) continue;
    const doi = doiKey(p), title = titleKey(p);
    const existing = (doi && dois.get(doi)) || titles.get(title);
    if (existing) {
      for (const key of ["doi", "abstract", "abstract_zh", "title_zh", "venue", "url", "school_fulltext_url"] as const) {
        if (!existing[key] && p[key]) existing[key] = p[key];
      }
      existing.school_holdings = [...new Set([...(existing.school_holdings || []), ...(p.school_holdings || [])])];
      if (doi) dois.set(doi, existing);
      titles.set(title, existing);
    } else {
      const copy = {...p, federated_rank: papers.length};
      papers.push(copy); titles.set(title, copy);
      if (doi) dois.set(doi, copy);
    }
  }
  const uniqueCount = papers.length;
  const shown = papers.slice(0, limit);
  const union = (key: "requested_sources" | "attempted_sources" | "unavailable_sources") => [...new Set(results.flatMap(r => r[key] || []))];
  return {
    query, search_plan: plan, decomposed_queries: plan.flatMap(p => [p.en, p.zh]), papers: shown,
    total: shown.length, candidate_count: Math.max(uniqueCount, ...results.map(r => r.candidate_count || 0)),
    excluded_low_relevance: results[0]?.excluded_low_relevance || 0,
    minimum_relevance: results[0]?.minimum_relevance || 0, required_core_terms: results[0]?.required_core_terms || [],
    source_counts: shown.reduce<Record<string, number>>((counts, p) => { counts[p.source] = (counts[p.source] || 0) + 1; return counts; }, {}),
    requested_sources: union("requested_sources"), attempted_sources: union("attempted_sources"), unavailable_sources: union("unavailable_sources"),
    warnings: results.flatMap(r => r.warnings || []), from_cache: results.length > 0 && results.every(r => r.from_cache), timings: results[0]?.timings || {},
    strategy_diagnostics: results.find(r => r.strategy_diagnostics?.admission)?.strategy_diagnostics,
  };
}

type Collector = {
  status: () => Promise<{collector: CnkiCollector}>;
  create: (query: string, limit: number) => Promise<CnkiJob>;
  read: (id: string) => Promise<CnkiJob>;
  sleep: () => Promise<void>;
  active: () => boolean;
  update: (channel: SearchChannel, results: LiteratureSearchResponse[]) => void;
};

/** One collector per plan. Cancellation gates every callback and new job creation. */
export async function collectChinese(plan: LiteratureQueryGroup[], limit: number, io: Collector, language:Language) {
  const t=(phrase:Phrase,fields?:Record<string,string|number>)=>say(phrase,language,fields);
  let results: LiteratureSearchResponse[] = [];
  const update = (channel: SearchChannel) => { if (io.active()) io.update(channel, results); };
  try {
    const {collector} = await io.status();
    if (!io.active()) return;
    if (!collector.available) { update({state:"unavailable", detail:collector.detail}); return; }
    const queries = [...new Set(plan.map(p => p.zh))];
    const jobs: CnkiJob[] = [];
    for (const query of queries) {
      if (!io.active()) return;
      jobs.push(await io.create(query, Math.max(1, Math.ceil(limit / queries.length))));
    }
    while (io.active()) {
      results = jobs.flatMap(j => j.result ? [j.result] : []);
      const count = mergeLiteratureResults("", [], results, limit).total;
      const completed = jobs.filter(j => j.state === "completed").length;
      const groups = jobs.map((j, index) => ({
        label: t({zh:"第 {index} 组 · {label}",en:"Group {index} \u00b7 {label}"},{index:index+1,label:plan.find(p=>p.zh===j.query)?.label || j.query}),
        state: j.state, count: j.result?.total || 0,
        detail: j.state === "completed" ? t({zh:"检索完成",en:"Search complete"}) : j.detail || t({zh:"正在采集",en:"Collecting"}),
      }));
      if (jobs.every(j => ["completed", "cancelled"].includes(j.state))) {
        const cancelled = jobs.length - completed;
        update({state:"completed", detail:t({zh:"已获取 {count} 篇（去重）；{completed}/{total} 组完成，{cancelled} 组已取消",en:"Collected {count} unique papers; {completed}/{total} groups complete, {cancelled} cancelled"},{count,completed,total:jobs.length,cancelled}), count, groups}); return;
      }
      const stopped = jobs.find(j => ["waiting_for_verification", "paused", "error", "browser_closed"].includes(j.state));
      const state = stopped?.state || "searching";
      const blocked = jobs.filter(j => ["waiting_for_verification", "paused", "error", "browser_closed"].includes(j.state)).length;
      const running = jobs.filter(j => !["completed", "cancelled", "waiting_for_verification", "paused", "error", "browser_closed"].includes(j.state)).length;
      const detail = t({zh:"已获取 {count} 篇（去重）；{completed}/{total} 组完成",en:"Collected {count} unique papers; {completed}/{total} groups complete"},{count,completed,total:jobs.length})
        + (running ? t({zh:"，{count} 组采集中",en:", {count} collecting"},{count:running}) : "")
        + (blocked ? t({zh:"，{count} 组暂未完成（展开查看原因）",en:", {count} pending (expand for details)"},{count:blocked}) : "")
        + (count && blocked ? t({zh:"。已取得的内容可正常查看",en:". Collected results remain available."}) : "");
      update({state, detail, count, groups});
      await io.sleep();
      if (!io.active()) return;
      await Promise.all(jobs.map(async (j, i) => {
        if (!["completed", "cancelled"].includes(j.state)) jobs[i] = await io.read(j.id);
      }));
    }
  } catch (error) {
    update({state:"error", detail:error instanceof Error ? error.message : String(error)});
  }
}

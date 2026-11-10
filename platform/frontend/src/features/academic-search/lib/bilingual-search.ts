import {say, type Language, type Phrase} from "../../../shared/i18n/language.ts";
import type {LiteraturePaper, LiteratureQueryGroup, LiteratureSearchResponse} from "@/lib/api";
import type {CnkiCollector, CnkiJob, CnkiDocumentKind} from "../api/cnki";

type CollectionProgress = {state: string; phase?: "index" | "metadata" | "mixed"};
export type SearchChannel = CollectionProgress & {detail: string; count?: number;
  groups?: (CollectionProgress & {label: string; count: number; detail: string})[]};

// Use the persisted job phase: a batch can temporarily clear the detail message.
function collectionPhase(items:CollectionProgress[]):CollectionProgress["phase"] {
  const pending=items.filter(item=>!["completed","cancelled"].includes(item.state));
  if (pending.length && pending.every(item=>item.phase==="metadata")) return "metadata";
  return pending.some(item=>item.phase==="metadata" || item.phase==="mixed") ? "mixed" : "index";
}

export function cnkiProgressLabel(channel:CollectionProgress|undefined,t:(phrase:Phrase)=>string):string {
  if (channel?.phase==="metadata") return channel.state==="queued"
    ? t({zh:"中文题录已获取，等待读取知网摘要…",en:"Chinese records collected; waiting to read abstracts from CNKI\u2026"})
    : t({zh:"中文题录已获取，正在读取知网摘要…",en:"Chinese records collected; reading abstracts from CNKI\u2026"});
  if (channel?.phase==="mixed") return t({zh:"正在获取中文题录，并读取已获取文献的知网摘要…",en:"Collecting Chinese records and reading available abstracts from CNKI\u2026"});
  return channel?.state==="queued"
    ? t({zh:"知网题录获取排队中，可先查看已有结果…",en:"CNKI record collection is queued. Available results can be viewed meanwhile."})
    : t({zh:"正在获取中文题录…",en:"Collecting Chinese bibliographic records\u2026"});
}

const doiKey = (p: LiteraturePaper) => (p.doi || "").trim().replace(/^https?:\/\/(dx\.)?doi\.org\//i, "").replace(/^doi\s*:\s*/i, "").trim().toLowerCase();
const titleKey = (p: LiteraturePaper) => p.title.normalize("NFKC").toLowerCase().replace(/(?<=\p{L})[-‐‑‒–—](?=\p{L})/gu, " ").trim().replace(/\s+/g, " ") + ":" + (p.year || "");

/** Preserve source order, interleave catalogues, and deduplicate by DOI OR title/year.
 * Retrieval limits belong to the sources; merging must retain every acquired record.
 * CNKI does not supply our quality scores: rank fusion must not invent them. */
export function mergeLiteratureResults(query: string, plan: LiteratureQueryGroup[], results: LiteratureSearchResponse[]): LiteratureSearchResponse {
  const papers: LiteraturePaper[] = [], dois = new Map<string, LiteraturePaper>(), titles = new Map<string, LiteraturePaper[]>();
  const remember = (record: LiteraturePaper, alias = record) => {
    for (const value of [record, alias]) {
      const doi = doiKey(value), title = titleKey(value);
      if (doi) dois.set(doi, record);
      const matches = titles.get(title) || [];
      if (!matches.includes(record)) matches.push(record);
      titles.set(title, matches);
    }
  };
  const maximum = Math.max(0, ...results.map(r => r.papers.length));
  for (let row = 0; row < maximum; row++) for (const result of results) {
    const p = result.papers[row];
    if (!p) continue;
    const doi = doiKey(p), title = titleKey(p);
    const matches = (titles.get(title) || []).filter(p => !doi || !doiKey(p) || doiKey(p) === doi);
    const existing = (doi && dois.get(doi)) || (p.title.trim() && matches.length === 1 ? matches[0] : undefined);
    if (existing) {
      const provenance = {...existing.metadata_provenance};
      for (const key of ["doi", "abstract", "abstract_zh", "title_zh", "venue", "url", "school_fulltext_url"] as const) {
        if (!existing[key] && p[key]) {
          existing[key] = p[key];
          // A copied value carries its own origin; a discarded one cannot replace it.
          delete provenance[key];
          if (p.metadata_provenance?.[key]) provenance[key] = p.metadata_provenance[key];
        } else if (existing[key] && existing[key] === p[key] && !provenance[key] && p.metadata_provenance?.[key]) {
          provenance[key] = p.metadata_provenance[key];
        }
      }
      for (const [key, value] of Object.entries(p.metadata_provenance || {})) {
        if (!(key in existing) && !(key in p)) provenance[key] = value;
      }
      existing.metadata_provenance = provenance;
      existing.school_holdings = [...new Set([...(existing.school_holdings || []), ...(p.school_holdings || [])])];
      remember(existing, p);
    } else {
      const copy = {...p, federated_rank: papers.length};
      papers.push(copy); remember(copy);
    }
  }
  const uniqueCount = papers.length;
  const union = (key: "requested_sources" | "attempted_sources" | "unavailable_sources") => [...new Set(results.flatMap(r => r[key] || []))];
  return {
    query, search_plan: plan, decomposed_queries: plan.flatMap(p => [p.en, p.zh]), papers,
    total: uniqueCount, candidate_count: Math.max(uniqueCount, ...results.map(r => r.candidate_count || 0)),
    excluded_low_relevance: results[0]?.excluded_low_relevance || 0,
    minimum_relevance: results[0]?.minimum_relevance || 0, required_core_terms: results[0]?.required_core_terms || [],
    source_counts: papers.reduce<Record<string, number>>((counts, p) => { counts[p.source] = (counts[p.source] || 0) + 1; return counts; }, {}),
    requested_sources: union("requested_sources"), attempted_sources: union("attempted_sources"), unavailable_sources: union("unavailable_sources"),
    warnings: results.flatMap(r => r.warnings || []), from_cache: results.length > 0 && results.every(r => r.from_cache), timings: results[0]?.timings || {},
    strategy_diagnostics: results.find(r => r.strategy_diagnostics?.admission)?.strategy_diagnostics,
  };
}

type Collector = {
  status: () => Promise<{collector: CnkiCollector}>;
  create: (query: string, limit: number | null, kind?:CnkiDocumentKind) => Promise<CnkiJob>;
  read: (id: string, cursor?: string) => Promise<CnkiJob>;
  sleep: () => Promise<void>;
  active: () => boolean;
  update: (channel: SearchChannel, results: LiteratureSearchResponse[]) => void;
};

/** Each visible category owns its budget; deduplicate before applying it. */
export function limitCategories(result:LiteratureSearchResponse, limit=200):LiteratureSearchResponse {
  const counts:Record<string,number>={};
  const papers=result.papers.filter(p=>{
    const category=p.publication_category || "其他";
    if ((counts[category] || 0)>=limit) return false;
    counts[category]=(counts[category] || 0)+1;return true;
  });
  return {...result,papers,total:papers.length,source_counts:papers.reduce<Record<string,number>>((all,p)=>{
    all[p.source]=(all[p.source] || 0)+1;return all;
  },{})};
}

/** Independent category jobs enter the two-tab worker queue together. Additional
 * query groups run only for categories which still have room after deduplication. */
export async function collectChineseCategories(plan:LiteratureQueryGroup[], io:Collector, language:Language) {
  const t=(phrase:Phrase)=>say(phrase,language);
  const kinds:[CnkiDocumentKind,Phrase][]=[
    ["journal",{zh:"中文期刊",en:"Chinese journals"}],
    ["thesis",{zh:"学位论文",en:"Theses"}],
    ["conference",{zh:"会议文章",en:"Conference papers"}],
    ["book",{zh:"书籍",en:"Books"}],
    ["other",{zh:"其他",en:"Other"}],
  ];
  const channels=new Map<string,SearchChannel>();
  const results=new Map<string,LiteratureSearchResponse>();
  const status=io.status();
  let creation:Promise<unknown>=Promise.resolve();
  const blockedStates=["error","paused","browser_closed","waiting_for_verification","unavailable"];
  const channelDetail=(channel?:SearchChannel)=>channel?.groups?.find(group=>blockedStates.includes(group.state))?.detail || channel?.detail || "";
  const publish=()=>{
    if (!io.active()) return;
    const all=kinds.map(([kind])=>channels.get(kind) || {state:"queued",detail:""});
    const blocked=all.find(c=>blockedStates.includes(c.state));
    const pending=all.filter(c=>c.state!=="completed");
    io.update({state:blocked?.state || (!pending.length?"completed":pending.every(c=>c.state==="queued")?"queued":"searching"),
      phase:collectionPhase(all),
      count:[...results.values()].reduce((sum,result)=>sum+result.total,0),
      detail:channelDetail(blocked) || t({zh:"按类别采集中，每类最多 200 篇",en:"Collecting by category, up to 200 papers each"}),
      groups:kinds.map(([kind,label])=>({label:t(label),state:channels.get(kind)?.state || "queued",phase:channels.get(kind)?.phase,count:results.get(kind)?.total || 0,detail:channelDetail(channels.get(kind))}))},[...results.values()]);
  };
  await Promise.all(kinds.map(async([kind,label])=>{
    let completed:LiteratureSearchResponse|undefined;
    for (const group of plan) {
      if (!io.active() || (completed?.total || 0)>=200) break;
      let current:LiteratureSearchResponse|undefined;
      await collectChinese([group],200,{...io,
        status:()=>status,
        create:(query,limit)=>{
          const next=creation.then(()=>{
            if (!io.active()) throw new Error(t({zh:"已停止检索",en:"Search stopped"}));
            return io.create(query,limit,kind);
          });
          creation=next.catch(()=>undefined);return next;
        },
        update:(channel,batches)=>{
          channels.set(kind,channel);
          const combined=mergeLiteratureResults("",plan,[...(completed?[completed]:[]),...batches]);
          // The API uses Chinese category keys regardless of the display language.
          current=limitCategories({...combined,papers:combined.papers.filter(p=>p.publication_category===label.zh)});
          results.set(kind,current);publish();
        },
      },language);
      if (current) completed=current;
      if (channels.get(kind)?.state!=="completed") return;
    }
    channels.set(kind,{state:"completed",detail:t({zh:"该类别采集完成",en:"Category collection complete"})});publish();
  }));
}

/** One collector per plan. Cancellation gates every callback and new job creation. */
export async function collectChinese(plan: LiteratureQueryGroup[], limit: number | null, io: Collector, language:Language) {
  const t=(phrase:Phrase,fields?:Record<string,string|number>)=>say(phrase,language,fields);
  let results: LiteratureSearchResponse[] = [];
  const update = (channel: SearchChannel) => { if (io.active()) io.update(channel, results); };
  try {
    const {collector} = await io.status();
    if (!io.active()) return;
    if (!collector.available) { update({state:"unavailable", detail:collector.detail}); return; }
    const queries = [...new Set(plan.map(p => p.zh))];
    const jobs: CnkiJob[] = [];
    const records = new Map<string, Map<string, LiteraturePaper>>();
    const accept = (job: CnkiJob): CnkiJob => {
      if (!job.result || !job.paper_ids) return job; // Compatible with older backends.
      const papers = records.get(job.id) || new Map<string, LiteraturePaper>();
      job.result.papers.forEach((paper,index)=>{
        const id=job.paper_ids?.[index];
        if (id) papers.set(id,paper);
      });
      records.set(job.id,papers);
      return {...job,result:{...job.result,papers:[...papers.values()],total:papers.size,source_counts:{cnki:papers.size}}};
    };
    for (const query of queries) {
      if (!io.active()) return;
      jobs.push(accept(await io.create(query, limit === null ? null : Math.max(1, Math.ceil(limit / queries.length)))));
    }
    while (io.active()) {
      results = jobs.flatMap(j => j.result ? [j.result] : []);
      const count = mergeLiteratureResults("", [], results).total;
      const completed = jobs.filter(j => j.state === "completed").length;
      const groups = jobs.map((j, index) => ({
        label: t({zh:"第 {index} 组 · {label}",en:"Group {index} \u00b7 {label}"},{index:index+1,label:plan.find(p=>p.zh===j.query)?.label || j.query}),
        state: j.state, phase:j.phase, count: j.result?.total || 0,
        detail: j.state === "completed" ? t({zh:"检索完成",en:"Search complete"}) : j.detail || cnkiProgressLabel(j,t),
      }));
      if (jobs.every(j => ["completed", "cancelled"].includes(j.state) && !j.has_more)) {
        const cancelled = jobs.length - completed;
        update({state:"completed", detail:t({zh:"已获取 {count} 篇（去重）；{completed}/{total} 组完成，{cancelled} 组已取消",en:"Collected {count} unique papers; {completed}/{total} groups complete, {cancelled} cancelled"},{count,completed,total:jobs.length,cancelled}), count, groups}); return;
      }
      const stopped = jobs.find(j => ["waiting_for_verification", "paused", "error", "browser_closed"].includes(j.state));
      const state = stopped?.state || (jobs.some(j => j.state === "working") ? "searching" : "queued");
      const blocked = jobs.filter(j => ["waiting_for_verification", "paused", "error", "browser_closed"].includes(j.state)).length;
      const queued = jobs.filter(j => j.state === "queued").length;
      const running = jobs.filter(j => j.state === "working").length;
      const detail = t({zh:"已获取 {count} 篇（去重）；{completed}/{total} 组完成",en:"Collected {count} unique papers; {completed}/{total} groups complete"},{count,completed,total:jobs.length})
        + (running ? t({zh:"，{count} 组采集中",en:", {count} collecting"},{count:running}) : "")
        + (queued ? t({zh:"，{count} 组排队中",en:", {count} queued"},{count:queued}) : "")
        + (blocked ? t({zh:"，{count} 组暂未完成（展开查看原因）",en:", {count} pending (expand for details)"},{count:blocked}) : "")
        + (count && blocked ? t({zh:"。已取得的内容可正常查看",en:". Collected results remain available."}) : "");
      update({state, phase:collectionPhase(jobs), detail, count, groups});
      if (!jobs.some(j=>j.has_more)) await io.sleep();
      if (!io.active()) return;
      await Promise.all(jobs.map(async (j, i) => {
        if (j.has_more || !["completed", "cancelled"].includes(j.state)) jobs[i] = accept(await io.read(j.id,j.next_cursor));
      }));
    }
  } catch (error) {
    update({state:"error", detail:error instanceof Error ? error.message : String(error)});
  }
}

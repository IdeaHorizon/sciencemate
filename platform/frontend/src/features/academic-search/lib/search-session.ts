import {say, type Language, type Phrase} from "../../../shared/i18n/language.ts";
import type {api, LiteratureQueryGroup, LiteratureSearchResponse} from "@/lib/api";
import type {CnkiCollector, CnkiJob, CnkiDocumentKind} from "../api/cnki";
import {collectChineseCategories, limitCategories, mergeLiteratureResults, type SearchChannel} from "./bilingual-search.ts";

type Dependencies = {
  stream: typeof api.streamLiteratureSearch;
  plan: (query:string)=>Promise<{search_plan:LiteratureQueryGroup[]}>;
  status: ()=>Promise<{collector:CnkiCollector}>;
  create: (query:string,limit:number|null,kind?:CnkiDocumentKind)=>Promise<CnkiJob>;
  read: (id:string,cursor?:string)=>Promise<CnkiJob>;
  retry: (id:string)=>Promise<unknown>;
  cancelJob?: (id:string)=>Promise<unknown>;
  sleep: ()=>Promise<void>;
};
type Snapshot = {
  query:string;
  data:LiteratureSearchResponse|undefined;
  error:Error|undefined;
  progress:{stage:string;detail:string;percent:number}|undefined;
  plan:LiteratureQueryGroup[];
  channels:Record<string,SearchChannel>;
  isPending:boolean;
  cnkiPending:boolean;
  hasMore:boolean;
};

/** The session owns requests. Views only subscribe: navigation never cancels work.
 * A new search, explicit Stop, or end of authentication invalidates old callbacks. */
export function createAcademicSearchSession(io: Dependencies) {
  const initial:Snapshot = {query:"",data:undefined,error:undefined,progress:undefined,
    plan:[],channels:{},isPending:false,cnkiPending:false,hasMore:false};
  let state = initial;
  let language:Language;
  const t=(phrase:Phrase,fields?:Record<string,string|number>)=>say(phrase,language,fields);
  let cancel:(()=>Promise<void>)|null = null;
  let retry:(()=>Promise<void>)|null = null;
  let more:(()=>void)|null = null;
  const listeners = new Set<()=>void>();
  const notify = () => listeners.forEach(listener=>listener());
  const patch = (value:Partial<Snapshot>) => {state={...state,...value};notify();};
  type Update<T> = T | ((current:T)=>T);
  const setData = (data:Snapshot["data"])=>patch({data});
  const setError = (error:Snapshot["error"])=>patch({error});
  const setPlan = (plan:Snapshot["plan"])=>patch({plan});
  const setPending = (isPending:boolean)=>patch({isPending});
  const setCnkiPending = (cnkiPending:boolean)=>patch({cnkiPending});
  const setProgress = (value:Update<Snapshot["progress"]>)=>patch({progress:typeof value==="function"?value(state.progress):value});
  const setChannels = (value:Update<Snapshot["channels"]>)=>patch({channels:typeof value==="function"?value(state.channels):value});

  const start = (input: { query: string; limit?: number | null; source?: "all" | "cnki"; language:Language }) => {
    void cancel?.().catch(()=>console.warn(t({zh:"上次采集未能停止",en:"Previous collection could not be stopped"})));
    language=input.language;
    const onlyChinese = input.source === "cnki", limit = input.limit ?? null;
    let active = true, chineseStarted = false;
    const transport: {stop?: () => void} = {};
    let searchPlan: LiteratureQueryGroup[] = [], international: LiteratureSearchResponse | undefined;
    let completedInternational: LiteratureSearchResponse | undefined;
    let nextInternational:Record<string,unknown>|undefined;
    let chinese: LiteratureSearchResponse | undefined;
    const jobIds = new Set<string>();
    retry = async () => {
      try { await Promise.all([...jobIds].map(io.retry)); }
      catch (err) { channel("cnki",{state:"error",detail:err instanceof Error ? err.message : String(err)}); }
    };
    cancel = async () => {
      active = false; transport.stop?.();
      if (io.cancelJob) await Promise.all([...jobIds].map(async id=>{await io.cancelJob!(id);jobIds.delete(id);}));
    };
    more = null;
    patch({query:input.query,hasMore:false});
    setPending(true); setCnkiPending(false); setError(undefined); setData(undefined); setPlan([]);
    setChannels(onlyChinese ? {cnki:{state:"planning",detail:t({zh:"正在提取中文检索词",en:"Generating Chinese search terms"})}} : {
      international:{state:"planning",detail:t({zh:"正在提取英文检索词",en:"Generating English search terms"})}, cnki:{state:"planning",detail:t({zh:"等待中英文检索计划",en:"Waiting for a bilingual search plan"})},
    });
    setProgress({stage:"strategy",detail:t({zh:"正在按研究问题生成中英文检索词",en:"Generating bilingual search terms for your question"}),percent:1});
    const channel = (name: string, value: SearchChannel) => {
      if (active) setChannels(current => ({...current, [name]:value}));
    };
    const publish = () => {
      if (active && (international || chinese)) setData(limitCategories(mergeLiteratureResults(input.query, searchPlan,
        [international, chinese].filter((r): r is LiteratureSearchResponse => Boolean(r)))));
    };
    const beginChinese = (groups: LiteratureQueryGroup[]) => {
      if (!active || chineseStarted || !groups.length) return;
      chineseStarted = true; searchPlan = groups; setPlan(groups); setCnkiPending(true);
      void collectChineseCategories(groups, {
        status:()=>io.status(), create:async(query, count, kind)=>{
          const job=await io.create(query,count,kind);
          if (!["completed","cancelled"].includes(job.state)) {
            if (!active && io.cancelJob) await io.cancelJob(job.id);
            else jobIds.add(job.id);
          }
          return job;
        },
        read:async(id,cursor)=>{
          const job=await io.read(id,cursor);
          if (["completed","cancelled"].includes(job.state)) jobIds.delete(id);
          return job;
        }, sleep:io.sleep, active:()=>active,
        update:(state, results)=>{
          channel("cnki",state);
          if (active) setCnkiPending(["queued", "searching", "planning"].includes(state.state)
            || Boolean(state.groups?.some(group => ["queued", "working"].includes(group.state))));
          if (results.length) {chinese = mergeLiteratureResults(input.query, searchPlan, results); publish();}
        },
      }, language).finally(()=>{if (active) setCnkiPending(false);});
    };
    if (onlyChinese) {
      void io.plan(input.query).then(result=>{
        if (!active) return;
        beginChinese(result.search_plan); setPending(false); setProgress(undefined);
      }).catch(err=>{
        if (!active) return;
        const message = err instanceof Error ? err.message : String(err);
        setError(new Error(message)); channel("cnki",{state:"error",detail:message});
        setPending(false);setProgress(undefined);
      });
      return;
    }
    const fetchBatch = (continuation?:Record<string,unknown>) => {
      if (!active) return;
      transport.stop = io.stream(input.query, limit,
      event=>{
        if (!active || event.stage === "source") return;
        if (Array.isArray(event.search_plan)) beginChinese(event.search_plan as LiteratureQueryGroup[]);
        if (event.partial_result && typeof event.partial_result === "object"
            && Array.isArray((event.partial_result as LiteratureSearchResponse).papers)) {
          international = mergeLiteratureResults(input.query,searchPlan,
            [completedInternational,event.partial_result as LiteratureSearchResponse].filter((r):r is LiteratureSearchResponse=>Boolean(r)));
          publish();
        }
        channel("international",{state:"searching",detail:String(event.detail || t({zh:"国际来源正在检索",en:"Searching international sources"}))});
        setProgress(current=>({stage:String(event.stage || "search"),detail:String(event.detail || t({zh:"正在检索",en:"Searching"})),
          percent:Math.max(current?.percent || 1, Math.min(100,Number(event.progress_percent) || 1))}));
      },
      result=>{
        if (!active) return;
        if (!chineseStarted) beginChinese(result.search_plan || []);
        completedInternational = mergeLiteratureResults(input.query,searchPlan,
          [completedInternational,result].filter((r):r is LiteratureSearchResponse=>Boolean(r)));
        international = completedInternational; publish();
        const count=completedInternational.total;
        // Finish the first request for EVERY original keyword group. Already
        // started source cursors wait for an explicit request for more results.
        const groups=result.continuation?.groups;
        const unstarted=groups && typeof groups==="object" && Object.values(groups).some(position=>position===null);
        channel("international",{state:unstarted?"searching":"completed",detail:unstarted
          ?t({zh:"已获取 {count} 篇，继续检索其余关键词组",en:"Collected {count} papers; searching remaining keyword groups"},{count})
          :t({zh:"国际来源本轮检索完成，{count} 篇",en:"International search complete: {count} papers"},{count}),count});
        if (!chineseStarted) channel("cnki",{state:"error",detail:t({zh:"未收到中文检索计划，请重试",en:"No Chinese search plan was received. Please retry."})});
        if (result.continuation && unstarted) {
          const next=result.continuation;
          // Provider adapters already enforce rate limits and Retry-After.
          // Do not add the CNKI status-poll delay between international groups.
          void Promise.resolve().then(()=>{if (active) fetchBatch(next);});
        } else {
          nextInternational=result.continuation || undefined;
          patch({hasMore:Boolean(nextInternational),isPending:false,progress:undefined});
        }
      },
      message=>{
        if (!active) return;
        channel("international",{state:"error",detail:message});
        if (!chineseStarted) {setError(new Error(message));channel("cnki",{state:"error",detail:t({zh:"检索计划未生成，中文检索未执行",en:"No search plan was generated, so the Chinese search did not start."})});}
        setPending(false);setProgress(undefined);
      },
      continuation);
    };
    more=()=>{
      if (!active || !nextInternational || state.isPending) return;
      const next=nextInternational;nextInternational=undefined;
      patch({hasMore:false,isPending:true});fetchBatch(next);
    };
    fetchBatch();
  };

  const stopWaiting = () => {
    const stopping=cancel;
    void stopping?.().catch(()=>{
      if (cancel===stopping) setChannels(current=>({...current,cnki:{state:"error",detail:t({zh:"部分采集任务未能停止，请重试停止检索",en:"Some collection tasks could not be stopped. Try stopping again."})}}));
    });
    setPending(false); setCnkiPending(false); setProgress(undefined);patch({hasMore:false});
    setChannels(current=>Object.fromEntries(Object.entries(current).map(([key,value])=>[key,
      ["completed","unavailable","error"].includes(value.state) ? value : {...value,state:"stopped",detail:t({zh:"已停止检索，已获取的结果保留",en:"Search stopped. Collected results are retained."})},
    ])));
  };
  const retryChinese = () => {void retry?.();};
  const loadMore = () => {more?.();};
  const reset = () => {void cancel?.().catch(()=>console.warn(t({zh:"重置时未能停止采集",en:"Collection could not be stopped during reset"}))); cancel = null; retry = null; more = null; state = initial; notify();};
  return {getSnapshot:()=>state, getServerSnapshot:()=>initial,
    subscribe:(listener:()=>void)=>{listeners.add(listener);return ()=>{listeners.delete(listener);};},
    start,stopWaiting,retryChinese,loadMore,reset,setQuery:(query:string)=>patch({query})};
}

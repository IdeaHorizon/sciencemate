import {say, type Language, type Phrase} from "../../../shared/i18n/language.ts";
import type {api, LiteratureQueryGroup, LiteratureSearchResponse} from "@/lib/api";
import type {CnkiCollector, CnkiJob} from "../api/cnki";
import {collectChinese, mergeLiteratureResults, type SearchChannel} from "./bilingual-search.ts";

type Dependencies = {
  stream: typeof api.streamLiteratureSearch;
  plan: (query:string)=>Promise<{search_plan:LiteratureQueryGroup[]}>;
  status: ()=>Promise<{collector:CnkiCollector}>;
  create: (query:string,limit:number)=>Promise<CnkiJob>;
  read: (id:string)=>Promise<CnkiJob>;
  retry: (id:string)=>Promise<unknown>;
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
};

/** The session owns requests. Views only subscribe: navigation never cancels work.
 * A new search, explicit Stop, or end of authentication invalidates old callbacks. */
export function createAcademicSearchSession(io: Dependencies) {
  const initial:Snapshot = {query:"",data:undefined,error:undefined,progress:undefined,
    plan:[],channels:{},isPending:false,cnkiPending:false};
  let state = initial;
  let language:Language;
  const t=(phrase:Phrase,fields?:Record<string,string|number>)=>say(phrase,language,fields);
  let cancel:(()=>void)|null = null;
  let retry:(()=>Promise<void>)|null = null;
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

  const start = (input: { query: string; limit?: number; source?: "all" | "cnki"; language:Language }) => {
    cancel?.();
    language=input.language;
    const onlyChinese = input.source === "cnki", limit = input.limit ?? 200;
    let active = true, chineseStarted = false;
    const transport: {stop?: () => void} = {};
    let searchPlan: LiteratureQueryGroup[] = [], international: LiteratureSearchResponse | undefined;
    let chinese: LiteratureSearchResponse | undefined;
    const jobIds = new Set<string>();
    retry = async () => {
      try { await Promise.all([...jobIds].map(io.retry)); }
      catch (err) { channel("cnki",{state:"error",detail:err instanceof Error ? err.message : String(err)}); }
    };
    cancel = () => {active = false; transport.stop?.();};
    patch({query:input.query});
    setPending(true); setCnkiPending(false); setError(undefined); setData(undefined); setPlan([]);
    setChannels(onlyChinese ? {cnki:{state:"planning",detail:t({zh:"正在提取中文检索词",en:"Generating Chinese search terms"})}} : {
      international:{state:"planning",detail:t({zh:"正在提取英文检索词",en:"Generating English search terms"})}, cnki:{state:"planning",detail:t({zh:"等待中英文检索计划",en:"Waiting for a bilingual search plan"})},
    });
    setProgress({stage:"strategy",detail:t({zh:"正在按研究问题生成中英文检索词",en:"Generating bilingual search terms for your question"}),percent:1});
    const channel = (name: string, value: SearchChannel) => {
      if (active) setChannels(current => ({...current, [name]:value}));
    };
    const publish = () => {
      if (active && (international || chinese)) setData(mergeLiteratureResults(input.query, searchPlan,
        [international, chinese].filter((r): r is LiteratureSearchResponse => Boolean(r)), limit));
    };
    const beginChinese = (groups: LiteratureQueryGroup[]) => {
      if (!active || chineseStarted || !groups.length) return;
      chineseStarted = true; searchPlan = groups; setPlan(groups); setCnkiPending(true);
      void collectChinese(groups, limit, {
        status:()=>io.status(), create:async(query, count)=>{const job=await io.create(query,count);jobIds.add(job.id);return job;},
        read:io.read, sleep:io.sleep, active:()=>active,
        update:(state, results)=>{
          channel("cnki",state);
          if (results.length) {chinese = mergeLiteratureResults(input.query, searchPlan, results, limit); publish();}
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
    transport.stop = io.stream(input.query, limit,
      event=>{
        if (!active || event.stage === "source") return;
        if (Array.isArray(event.search_plan)) beginChinese(event.search_plan as LiteratureQueryGroup[]);
        channel("international",{state:"searching",detail:String(event.detail || t({zh:"国际来源正在检索",en:"Searching international sources"}))});
        setProgress(current=>({stage:String(event.stage || "search"),detail:String(event.detail || t({zh:"正在检索",en:"Searching"})),
          percent:Math.max(current?.percent || 1, Math.min(100,Number(event.progress_percent) || 1))}));
      },
      result=>{
        if (!active) return;
        if (!chineseStarted) beginChinese(result.search_plan || []);
        international = result; publish();
        channel("international",{state:"completed",detail:t({zh:"国际来源检索完成，{count} 篇",en:"International search complete: {count} papers"},{count:result.total}),count:result.total});
        if (!chineseStarted) channel("cnki",{state:"error",detail:t({zh:"未收到中文检索计划，请重试",en:"No Chinese search plan was received. Please retry."})});
        setPending(false);setProgress(undefined);
      },
      message=>{
        if (!active) return;
        channel("international",{state:"error",detail:message});
        if (!chineseStarted) {setError(new Error(message));channel("cnki",{state:"error",detail:t({zh:"检索计划未生成，中文检索未执行",en:"No search plan was generated, so the Chinese search did not start."})});}
        setPending(false);setProgress(undefined);
      },
    );
  };

  const stopWaiting = () => {
    cancel?.(); setPending(false); setCnkiPending(false); setProgress(undefined);
    setChannels(current=>Object.fromEntries(Object.entries(current).map(([key,value])=>[key,
      ["completed","unavailable","error"].includes(value.state) ? value : {...value,state:"stopped",detail:t({zh:"已停止等待；平台会继续处理已创建的采集任务",en:"Stopped waiting. The platform will continue processing collection tasks already created."})},
    ])));
  };
  const retryChinese = () => {void retry?.();};
  const reset = () => {cancel?.(); cancel = null; retry = null; state = initial; notify();};
  return {getSnapshot:()=>state, getServerSnapshot:()=>initial,
    subscribe:(listener:()=>void)=>{listeners.add(listener);return ()=>{listeners.delete(listener);};},
    start,stopWaiting,retryChinese,reset,setQuery:(query:string)=>patch({query})};
}

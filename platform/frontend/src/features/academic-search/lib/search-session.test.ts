import {test} from "node:test";
import assert from "node:assert/strict";
import {createAcademicSearchSession} from "./search-session.ts";
import type {LiteratureSearchResponse, LiteraturePaper} from "@/lib/api";
import type {CnkiJob} from "../api/cnki";

const papers = (title:string,source="crossref") => ({query:title,papers:[{title,source,year:2026,publication_category:source==="cnki"?"中文期刊":"期刊文章"} as LiteraturePaper],total:1} as LiteratureSearchResponse);
const plan = [{label:"主题",en:"subject",zh:"主题"}];
const tick = () => new Promise(resolve=>setImmediate(resolve));
function setup(overrides:Partial<Parameters<typeof createAcademicSearchSession>[0]>={}) {
  type Stream = Parameters<typeof createAcademicSearchSession>[0]["stream"];
  const requests:{progress:Parameters<Stream>[2];done:Parameters<Stream>[3];error:Parameters<Stream>[4];stopped:boolean}[] = [];
  const session = createAcademicSearchSession({
    stream:(_query,_limit,progress,done,error)=>{
      const request = {progress,done,error,stopped:false};requests.push(request);
      return ()=>{request.stopped=true;};
    },
    plan:async()=>({search_plan:plan}),
    status:async()=>({collector:{available:false,mode:"server",detail:"未连接"}}),
    create:async()=>{throw Error("unexpected job");},read:async()=>{throw Error("unexpected read");},
    retry:async()=>{},sleep:async()=>{},...overrides,
  });
  return {session,requests};
}

test("CNKI reset is explicit, shared by categories, and retains collected results",async()=>{
  const sleepers:(()=>void)[]=[];
  const resets:string[][]=[];
  let acknowledge:()=>void=()=>{},recovered=false;
  const job=(id:string)=>({id,query:"主题",state:recovered?"completed":"paused",
    error_code:recovered?null:"access_denied",detail:"HTTP 403",
    result:papers("已有中文论文","cnki")} as CnkiJob);
  const {session,requests}=setup({
    status:async()=>({collector:{available:true,mode:"server",detail:""}}),
    create:async(_query,_limit,kind)=>job(kind!),read:async id=>job(id),
    resetSession:ids=>{resets.push(ids);return new Promise<void>(resolve=>{acknowledge=resolve;});},
    retry:async()=>{throw Error("normal retry must not be used for a reset");},
    sleep:()=>new Promise<void>(resolve=>{sleepers.push(resolve);}),
  });
  session.start({query:"问题",language:"zh"});
  requests[0].progress({stage:"strategy",search_plan:plan});
  requests[0].done(papers("已有英文论文"));
  await tick();
  assert.equal(session.getSnapshot().channels.cnki.resettable,true);
  assert.equal(resets.length,0);
  const before=session.getSnapshot().data;
  session.resetChineseSession();session.resetChineseSession();
  assert.equal(resets.length,1);
  assert.equal(new Set(resets[0]).size,5);
  assert.equal(session.getSnapshot().resettingCnki,true);
  assert.equal(session.getSnapshot().data,before);
  recovered=true;acknowledge();await tick();
  sleepers.splice(0).forEach(resolve=>resolve());await tick();
  assert.equal(session.getSnapshot().resettingCnki,false);
  assert.equal(session.getSnapshot().channels.cnki.state,"completed");
  assert.equal(Boolean(session.getSnapshot().channels.cnki.resettable),false);
  assert.equal(session.getSnapshot().data?.total,2);
  session.reset();session.resetChineseSession();
  assert.equal(resets.length,1);
});

test("a late reset response cannot replace a new search or logout state",async()=>{
  let finish:()=>void=()=>{};
  const {session,requests}=setup({
    status:async()=>({collector:{available:true,mode:"server",detail:""}}),
    create:async()=>({id:"job",state:"paused",error_code:"session_paused"} as CnkiJob),
    resetSession:()=>new Promise<void>(resolve=>{finish=resolve;}),
    sleep:()=>new Promise<void>(()=>{}),
  });
  session.start({query:"旧问题",language:"zh"});
  requests[0].progress({stage:"strategy",search_plan:plan});await tick();
  session.resetChineseSession();session.reset();
  finish();await tick();
  assert.equal(session.getSnapshot().query,"");
  assert.deepEqual(session.getSnapshot().channels,{});
  assert.equal(session.getSnapshot().resettingCnki,false);
});

test("leaving every view does not cancel a search; remount gets finished results and draft",()=>{
  const {session,requests}=setup();
  const leave=session.subscribe(()=>{});
  session.start({language:"zh",query:"原研究问题"});
  session.setQuery("尚未提交的编辑");
  leave();
  requests[0].progress({stage:"scoring",progress_percent:75});
  assert.equal(requests[0].stopped,false);
  assert.equal(session.getSnapshot().progress?.percent,75);
  requests[0].done(papers("论文一"));
  const leaveAgain=session.subscribe(()=>{});
  assert.equal(session.getSnapshot().data?.papers[0].title,"论文一");
  assert.equal(session.getSnapshot().query,"尚未提交的编辑");
  assert.equal(session.getSnapshot().isPending,false);
  leaveAgain();
});

test("a new search cancels the old transport and rejects late old results/progress",()=>{
  const {session,requests}=setup();
  session.start({language:"zh",query:"旧问题"});session.start({language:"zh",query:"新问题"});
  requests[0].done(papers("旧结果"));requests[0].progress({stage:"old"});requests[0].error("old error");
  assert.equal(requests[0].stopped,true);
  assert.equal(session.getSnapshot().data,undefined);
  assert.equal(session.getSnapshot().error,undefined);
  requests[1].done(papers("新结果"));
  assert.equal(session.getSnapshot().data?.query,"新问题");
});

test("explicit stop preserves partial results; logout resets draft/results and gates callbacks",()=>{
  const {session,requests}=setup();
  session.start({language:"zh",query:"问题"});requests[0].done(papers("已有结果"));
  session.stopWaiting();requests[0].done(papers("过期结果"));
  assert.equal(session.getSnapshot().data?.papers[0].title,"已有结果");
  session.reset();requests[0].done(papers("不能跨账号显示"));
  assert.equal(session.getSnapshot().query,"");assert.equal(session.getSnapshot().data,undefined);
});

test("CNKI polling continues without views and merges with international results",async()=>{
  const sleepers:(()=>void)[]=[];
  const job={id:"job",query:"主题",state:"queued"} as CnkiJob;
  const {session,requests}=setup({
    status:async()=>({collector:{available:true,mode:"server",detail:""}}),
    create:async()=>job,
    read:async()=>({...job,state:"completed",result:papers("中文论文","cnki")}),
    sleep:()=>new Promise<void>(resolve=>{sleepers.push(resolve);}),
  });
  const leave=session.subscribe(()=>{});
  session.start({language:"zh",query:"问题"});requests[0].progress({stage:"strategy",search_plan:plan});
  await tick();leave();
  requests[0].done(papers("国际论文"));sleepers.splice(0).forEach(resolve=>resolve());await tick();
  assert.equal(session.getSnapshot().data?.total,2);
  assert.equal(session.getSnapshot().cnkiPending,false);
  assert.equal(session.getSnapshot().channels.cnki.state,"completed");
});

for (const chineseFirst of [true,false]) test(`all results survive when ${chineseFirst?'Chinese':'international'} collection finishes first`,async()=>{
  const batch=(source:string)=>({...papers(source,source),papers:Array.from({length:200},(_,i)=>({
    title:`${source} ${i}`,source,year:2026,publication_category:source==="cnki"?"中文期刊":"期刊文章",
  } as LiteraturePaper)),total:200});
  const sleepers:(()=>void)[]=[];
  const requestedLimits:(number|null)[]=[];
  const job={id:"job",query:"主题",state:"queued"} as CnkiJob;
  const {session,requests}=setup({
    status:async()=>({collector:{available:true,mode:"server",detail:""}}),
    create:async(_query,limit)=>{requestedLimits.push(limit);return job;},
    read:async()=>({...job,state:"completed",result:batch("cnki")}),
    sleep:()=>new Promise<void>(resolve=>{sleepers.push(resolve);}),
  });
  session.start({query:"问题",language:"zh",limit:200});
  requests[0].progress({stage:"strategy",search_plan:plan});
  await tick();
  if (chineseFirst) {sleepers.splice(0).forEach(resolve=>resolve());await tick();}
  requests[0].done(batch("crossref"));
  if (!chineseFirst) {sleepers.splice(0).forEach(resolve=>resolve());await tick();}
  assert.deepEqual(requestedLimits,[200,200,200,200,200]);
  assert.equal(session.getSnapshot().data?.total,400);
  assert.deepEqual(session.getSnapshot().data?.source_counts,{crossref:200,cnki:200});
  assert.equal(session.getSnapshot().data?.papers.at(-1)?.title,"cnki 199");
  assert.equal(session.getSnapshot().cnkiPending,false);
});

test("logout while waiting for the collector prevents new jobs",async()=>{
  let resolveStatus:(value:{collector:{available:boolean;mode:"server";detail:string}})=>void=()=>{};
  let created=0;
  const {session,requests}=setup({status:()=>new Promise(resolve=>{resolveStatus=resolve;}),
    create:async()=>{created++;return {} as CnkiJob;}});
  session.start({language:"zh",query:"问题"});requests[0].progress({stage:"strategy",search_plan:plan});
  session.reset();resolveStatus({collector:{available:true,mode:"server",detail:""}});await tick();
  assert.equal(created,0);assert.equal(session.getSnapshot().data,undefined);
});


test("search progress and completion use the requested interface language",()=>{
  const {session,requests}=setup();
  session.start({query:"particle settling",language:"en"});
  assert.equal(session.getSnapshot().channels.international.detail,"Generating English search terms");
  requests[0].progress({stage:"scoring"});
  assert.equal(session.getSnapshot().progress?.detail,"Searching");
  requests[0].done(papers("Example"));
  assert.equal(session.getSnapshot().channels.international.detail,"International search complete: 1 papers");
});

test("source previews appear while pending and final results replace provisional candidates",()=>{
  const {session,requests}=setup();
  session.start({language:"zh",query:"问题"});
  requests[0].progress({stage:"partial_results",partial_result:papers("待核实候选")});
  assert.equal(session.getSnapshot().data?.papers[0].title,"待核实候选");
  assert.equal(session.getSnapshot().isPending,true);
  requests[0].done(papers("完成核实的论文"));
  assert.deepEqual(session.getSnapshot().data?.papers.map(p=>p.title),["完成核实的论文"]);
  assert.equal(session.getSnapshot().isPending,false);
});

test("failed Chinese collection leaves the spinner but keeps results and recovery polling",async()=>{
  const sleepers:(()=>void)[]=[];
  const {session,requests}=setup({
    status:async()=>({collector:{available:true,mode:"server",detail:""}}),
    create:async()=>({id:"job",query:"主题",state:"error",detail:"浏览器超时",result:papers("中文缓存","cnki")} as CnkiJob),
    sleep:()=>new Promise<void>(resolve=>{sleepers.push(resolve);}),
    read:async()=>({id:"job",query:"主题",state:"completed",result:papers("中文新结果","cnki")} as CnkiJob),
  });
  session.start({query:"问题",language:"zh"});
  requests[0].progress({stage:"strategy",search_plan:plan});
  requests[0].done(papers("国际结果"));
  await tick();
  assert.equal(session.getSnapshot().cnkiPending,false);
  assert.equal(session.getSnapshot().channels.cnki.state,"error");
  assert.equal(session.getSnapshot().data?.total,2);
  sleepers.splice(0).forEach(resolve=>resolve());await tick();
  assert.equal(session.getSnapshot().channels.cnki.state,"completed");
  assert.equal(session.getSnapshot().data?.papers.some(p=>p.title==="中文新结果"),true);
});

test('international pages accumulate without retaining rejected previews or restarting CNKI',async()=>{
 const {session,requests}=setup();
 session.start({query:'问题',language:'zh'});
 const first={...papers('first'),papers:Array.from({length:200},(_,i)=>({title:`first ${i}`,source:'crossref',year:2026,publication_category:'期刊文章'} as LiteraturePaper)),total:200};
 requests[0].done({...first,continuation:{version:1,groups:{cursor:'next'}}});
 assert.equal(session.getSnapshot().isPending,false);
 assert.equal(session.getSnapshot().hasMore,true);
 await tick();assert.equal(requests.length,1);
 session.loadMore();assert.equal(requests.length,2);
 const thesis=(title:string)=>({...papers(title),papers:[{...papers(title).papers[0],publication_category:'学位论文'} as LiteraturePaper]});
 requests[1].progress({stage:'partial_results',partial_result:thesis('to reject')});
 assert.equal(session.getSnapshot().data?.total,201);
 requests[1].done(thesis('accepted'));
 assert.equal(session.getSnapshot().data?.total,201);
 assert.ok(session.getSnapshot().data?.papers.some(p=>p.title==='first 199'));
 assert.ok(!session.getSnapshot().data?.papers.some(p=>p.title==='to reject'));
 assert.equal(session.getSnapshot().isPending,false);
 assert.equal(session.getSnapshot().hasMore,false);
});

test('every original keyword group runs once, and low counts never trigger automatic deep paging',async()=>{
 const {session,requests}=setup({sleep:async()=>{throw Error('international queries do not use the CNKI polling delay');}});
 session.start({query:'保留多组 AND OR 检索策略',language:'zh'});
 const groups=[...plan,{label:'第二研究方向',en:'second AND (method OR technique)',zh:'第二研究方向'}];
 requests[0].progress({stage:'search_plan',search_plan:groups});
 requests[0].done({...papers('first'),continuation:{version:1,groups:{second:null,first:{crossref:{offset:100}}}}});
 await tick();assert.equal(requests.length,2);
 assert.deepEqual(session.getSnapshot().plan,groups);
 requests[1].done({...papers('second'),continuation:{version:1,groups:{first:{crossref:{offset:100}},second:{crossref:{offset:100}}}}});
 await tick();assert.equal(requests.length,2);
 assert.equal(session.getSnapshot().data?.total,2);
 assert.equal(session.getSnapshot().isPending,false);assert.equal(session.getSnapshot().hasMore,true);
 session.loadMore();assert.equal(requests.length,3);
 session.loadMore();assert.equal(requests.length,3);
 session.stopWaiting();session.loadMore();assert.equal(requests.length,3);
});

test('stop cancels unfinished CNKI jobs and prevents the next international batch',async()=>{
 const cancelled:string[]=[];
 let resume:()=>void=()=>{};
 const {session,requests}=setup({status:async()=>({collector:{available:true,mode:'server',detail:''}}),
   create:async()=>({id:'ongoing',query:'主题',state:'working'} as CnkiJob),
   cancelJob:async id=>{cancelled.push(id);},sleep:()=>new Promise<void>(resolve=>{resume=resolve;})});
 session.start({query:'问题',language:'zh'});
 requests[0].progress({stage:'strategy',search_plan:plan});await tick();
 requests[0].done({...papers('saved'),continuation:{version:1,groups:{remaining:null,cursor:{crossref:{offset:100}}}}});
 session.stopWaiting();resume();await tick();
 assert.deepEqual(cancelled,['ongoing']);assert.equal(requests.length,1);
 assert.equal(session.getSnapshot().data?.papers[0].title,'saved');
});

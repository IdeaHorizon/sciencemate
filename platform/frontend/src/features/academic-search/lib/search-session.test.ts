import {test} from "node:test";
import assert from "node:assert/strict";
import {createAcademicSearchSession} from "./search-session.ts";
import type {LiteratureSearchResponse, LiteraturePaper} from "@/lib/api";
import type {CnkiJob} from "../api/cnki";

const papers = (title:string,source="crossref") => ({query:title,papers:[{title,source,year:2026} as LiteraturePaper],total:1} as LiteratureSearchResponse);
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
  let resume:()=>void=()=>{};
  const job={id:"job",query:"主题",state:"queued"} as CnkiJob;
  const {session,requests}=setup({
    status:async()=>({collector:{available:true,mode:"server",detail:""}}),
    create:async()=>job,
    read:async()=>({...job,state:"completed",result:papers("中文论文","cnki")}),
    sleep:()=>new Promise<void>(resolve=>{resume=resolve;}),
  });
  const leave=session.subscribe(()=>{});
  session.start({language:"zh",query:"问题"});requests[0].progress({stage:"strategy",search_plan:plan});
  await tick();leave();
  requests[0].done(papers("国际论文"));resume();await tick();
  assert.equal(session.getSnapshot().data?.total,2);
  assert.equal(session.getSnapshot().cnkiPending,false);
  assert.equal(session.getSnapshot().channels.cnki.state,"completed");
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

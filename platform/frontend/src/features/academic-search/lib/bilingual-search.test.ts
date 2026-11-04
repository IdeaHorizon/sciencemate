import {test} from 'node:test';
import assert from 'node:assert/strict';
import {collectChinese, mergeLiteratureResults} from './bilingual-search.ts';
import type {LiteratureSearchResponse, LiteraturePaper} from '@/lib/api';
import type {CnkiJob} from '../api/cnki';
const paper = (title:string, source='crossref', extra={}) => ({title,year:2026,source,score:0,...extra} as LiteraturePaper);
const result = (papers:LiteraturePaper[]) => ({papers,total:papers.length,from_cache:false} as LiteratureSearchResponse);
const plan = [{label:'question',en:'English keywords',zh:'中文检索词'}];
const collector = {mode:"server" as const,available:true,detail:"自动采集"};
const job = (state:string, papers:LiteraturePaper[]=[]) => ({id:'one',query:'中文检索词',state,result:result(papers)} as CnkiJob);

test('DOI or normalized title/year dedup, metadata and source ranking survive merge',()=>{
 const a=result([paper('One','crossref',{doi:'10.1/a'}),paper('Two')]);
 const b=result([paper('Other title','cnki',{doi:'https://doi.org/10.1/A',abstract:'摘要'}),paper('Two','cnki'),paper('中文三','cnki')]);
 const merged=mergeLiteratureResults('query',plan,[a,b]);
 assert.equal(merged.total,3);assert.equal(merged.papers[0].abstract,'摘要');
 assert.deepEqual(merged.source_counts,{crossref:2,cnki:1});
 assert.deepEqual(merged.papers.map(p=>p.federated_rank),[0,1,2]);
 assert.equal(a.papers[0].abstract,undefined); // Never mutate an in-flight source response.
});

test('a missing/error channel never discards the successful channel',()=>{
 const merged=mergeLiteratureResults('query',plan,[result([paper('中文一','cnki')])]);
 assert.equal(merged.total,1);assert.equal(merged.query,'query');
});

test('international admission diagnostics remain scoped when CNKI results merge',()=>{
 const international=result([paper('English')]);
 international.strategy_diagnostics={admission:{candidate_count:122,eligible_count:8,returned_count:8,
   excluded_by_reason:{relevance:71,facets:43},excluded_samples:{facets:[{title:'Example'}]}}};
 const merged=mergeLiteratureResults('query',plan,[international,result([paper('中文','cnki')])]);
 assert.deepEqual(merged.strategy_diagnostics,international.strategy_diagnostics);
 assert.equal(merged.strategy_diagnostics?.admission?.candidate_count,122);
});

test('one failed query cannot overwrite successful CNKI totals or imply all groups failed',async()=>{
 let active=true;
 const updates:Parameters<Parameters<typeof collectChinese>[2]['update']>[0][]=[];
 const groups=Array.from({length:5},(_,i)=>({...plan[0],label:`主题${i}`,zh:`检索${i}`}));
 await collectChinese(groups,200,{status:async()=>({collector}),
 create:async q=>({...job(q==='检索0'?'completed':'error',q==='检索0'?[paper('期刊','cnki'),paper('学位论文','cnki')]:[]),query:q,detail:'暂无数据，请稍后重试'}),
 read:async()=>{throw Error('unexpected read');},sleep:async()=>{active=false;},active:()=>active,
 update:s=>updates.push(s)}, "zh");
 assert.equal(updates[0].count,2);
 assert.match(updates[0].detail,/已获取 2 篇/);
 assert.match(updates[0].detail,/1\/5 组完成/);
 assert.match(updates[0].detail,/4 组暂未完成/);
 assert.doesNotMatch(updates[0].detail,/暂无数据/);
 assert.equal(updates[0].groups?.filter(g=>g.state==='error').length,4);
 assert.equal(updates[0].groups?.[0].count,2);
});

test('unavailable managed collector is not misreported as zero results',async()=>{
 const updates:string[]=[];
 await collectChinese(plan,200,{status:async()=>({collector:{...collector,available:false,detail:"浏览器未安装"}}),create:async()=>{throw Error('must not create');},read:async()=>job('completed'),
 sleep:async()=>{},active:()=>true,update:s=>{updates.push(s.state);}}, "zh");
 assert.deepEqual(updates,['unavailable']);
});

test('partial data and captcha state survive until completion; keywords are used',async()=>{
 const states:string[]=[],counts:number[]=[],created:string[]=[];
 await collectChinese(plan,200,{status:async()=>({collector}),create:async q=>{created.push(q);return job('waiting_for_verification',[paper('一','cnki')]);},
 read:async()=>job('completed',[paper('一','cnki'),paper('二','cnki')]),sleep:async()=>{},active:()=>true,
 update:(s,r)=>{states.push(s.state);counts.push(r[0].total);}}, "zh");
 assert.deepEqual(created,['中文检索词']);assert.deepEqual(states,['waiting_for_verification','completed']);assert.deepEqual(counts,[1,2]);
});

test('new search/unmount during an await cannot create more old jobs or publish old results',async()=>{
 let active=true,created=0,updated=0;
 await collectChinese([...plan,{...plan[0],zh:'另一组'}],200,{status:async()=>({collector}),
 create:async()=>{created++;active=false;return job('queued');},read:async()=>job('completed'),sleep:async()=>{},active:()=>active,update:()=>{updated++;}}, "zh");
 assert.equal(created,1);assert.equal(updated,0);
});

 test('managed search works without any paired devices',async()=>{
 const states:string[]=[];
 await collectChinese(plan,20,{status:async()=>({collector}),create:async()=>job('completed',[paper('题录','cnki')]),read:async()=>job('completed'),sleep:async()=>{},active:()=>true,update:s=>states.push(s.state)}, "zh");
 assert.deepEqual(states,['completed']);
 });

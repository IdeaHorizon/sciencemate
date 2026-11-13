import {test} from 'node:test';
import assert from 'node:assert/strict';
import {collectChinese, collectChineseCategories, cnkiProgressLabel, limitCategories, mergeLiteratureResults, type SearchChannel} from './bilingual-search.ts';
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

test('full source batches retain journals beyond the first 200 merged records',()=>{
 const international=result(Array.from({length:200},(_,i)=>paper(`International ${i}`)));
 const chinese=result(Array.from({length:200},(_,i)=>paper(`中文题录${i}`,'cnki',{
   publication_category:i<139?'其他':'中文期刊',
 })));
 const merged=mergeLiteratureResults('query',plan,[international,chinese]);
 assert.equal(merged.total,400);
 assert.deepEqual(merged.source_counts,{crossref:200,cnki:200});
 assert.equal(merged.papers.filter(p=>p.publication_category==='中文期刊').length,61);
 assert.equal(merged.papers.at(-1)?.title,'中文题录199');
 assert.equal(mergeLiteratureResults('query',plan,[merged]).total,400);
});

test('same titles with conflicting DOIs remain separate and title-only ambiguity is preserved',()=>{
 const rows=[paper('Same','crossref',{doi:'10.1234/one'}),
   paper('Same','openalex',{doi:'https://doi.org/10.1234/ONE'}),
   paper('Same','cnki',{doi:'10.1234/two'}),paper('Same','cnki')];
 assert.equal(mergeLiteratureResults('query',plan,[result(rows)]).total,3);
 const symbols=[paper('C++ study'),paper('C# study')];
 assert.equal(mergeLiteratureResults('query',plan,[result(symbols)]).total,2);
});

test('merged abstracts retain their actual origin across repeated source responses',()=>{
 const a=result([paper('Research','crossref',{doi:'10.1/a',venue:'Journal',metadata_provenance:{venue:'crossref'}})]);
 const b=result([paper('Research','cnki',{doi:'10.1/a',abstract:'完整摘要',venue:'Other',metadata_provenance:{abstract:'cnki',venue:'cnki'}})]);
 const c=result([paper('Research','openalex',{doi:'10.1/a',abstract:'Different abstract',metadata_provenance:{abstract:'openalex'}})]);
 const before=JSON.stringify([a,b,c]);
 const merged=mergeLiteratureResults('query',plan,[a,b,c]);
 assert.equal(merged.papers[0].abstract,'完整摘要');
 assert.deepEqual(merged.papers[0].metadata_provenance,{venue:'crossref',abstract:'cnki'});
 assert.equal(JSON.stringify([a,b,c]),before);
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

test('uncapped CNKI drains every response page even when the job is already completed',async()=>{
 const calls:(string|number|null|undefined)[]=[];
 const batch=(start:number,end:number,more:boolean,cursor:string)=>({
   ...job('completed',Array.from({length:end-start},(_,i)=>paper(`论文${start+i}`,'cnki'))),
   count:450,paper_ids:Array.from({length:end-start},(_,i)=>String(start+i)),has_more:more,next_cursor:cursor,
 });
 let final:LiteratureSearchResponse|undefined;
 await collectChinese(plan,null,{status:async()=>({collector}),
   create:async(_q,limit)=>{calls.push(limit);return batch(0,200,true,'first');},
   read:async(_id,cursor)=>{calls.push(cursor);return cursor==='first'?batch(200,400,true,'second'):batch(400,450,false,'third');},
   sleep:async()=>{throw Error('do not delay pending result pages');},active:()=>true,
   update:(_s,r)=>{final=r[0];}},'zh');
 assert.deepEqual(calls,[null,'first','second']);
 assert.equal(final?.total,450);
 assert.equal(final?.papers.at(-1)?.title,'论文449');
});

test('incremental metadata updates replace the same CNKI paper without duplicates',async()=>{
 const first={...job('working',[paper('题录','cnki')]),paper_ids:['id'],next_cursor:'v1',has_more:false};
 let final:LiteratureSearchResponse|undefined;
 await collectChinese(plan,null,{status:async()=>({collector}),create:async()=>first,
   read:async(_id,cursor)=>{assert.equal(cursor,'v1');return {...first,state:'completed',next_cursor:'v2',result:result([paper('题录','cnki',{abstract:'补齐摘要'})])};},
   sleep:async()=>{},active:()=>true,update:(_s,r)=>{final=r[0];}},'zh');
 assert.equal(final?.total,1);assert.equal(final?.papers[0].abstract,'补齐摘要');
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

test('each display category has 200 unique places and there is no combined cap',()=>{
 const categories=['期刊文章','中文期刊','学位论文','预印本','书籍','会议文章','其他'];
 const rows=categories.flatMap(category=>Array.from({length:230},(_,i)=>paper(`${category} ${i}`,'crossref',{publication_category:category})));
 const merged=mergeLiteratureResults('query',plan,[result(rows),result(rows)]);
 const limited=limitCategories(merged);
 assert.equal(limited.total,1400);
 for(const category of categories) assert.equal(limited.papers.filter(p=>p.publication_category===category).length,200);
 assert.equal(limited.source_counts?.crossref,1400);
});

test('later query groups receive only the unused category budget and stop at 200',async()=>{
 const labels:Record<string,string>={journal:'中文期刊',thesis:'学位论文',conference:'会议文章',book:'书籍',other:'其他'};
 const queries=['第一组','第二组','不应执行的第三组'];
 const groups=queries.map(zh=>({...plan[0],zh}));
 const limits:Record<string,number[]>={};
 let final:LiteratureSearchResponse[]=[];
 await collectChineseCategories(groups,{status:async()=>({collector}),
  create:async(query,limit,kind)=>{
   assert.notEqual(query,queries[2]);
   (limits[kind!] ||= []).push(limit!);
   const count=query===queries[0]?150:limit!;
   const rows=Array.from({length:count},(_,i)=>paper(`${kind} ${query} ${i}`,'cnki',{publication_category:labels[kind!]}));
   return {...job('completed',rows),id:`${kind}-${query}`};
  },read:async()=>{throw Error('completed jobs must not be polled');},
  sleep:async()=>{},active:()=>true,update:(_state,results)=>{final=results;},
 },'zh');
 assert.equal(Object.keys(limits).length,5);
 for(const values of Object.values(limits)) assert.deepEqual(values,[200,50]);
 assert.equal(mergeLiteratureResults('query',groups,final).total,1000);
});

for (const language of ['zh','en'] as const) {
test(`a waiting journal job cannot prevent theses from arriving or taking their own 200 places (${language})`,async()=>{
 const labels:Record<string,string>={journal:'中文期刊',thesis:'学位论文',conference:'会议文章',book:'书籍',other:'其他'};
 const created:string[]=[];
 const releases:(()=>void)[]=[];
 let sawTheses=false;
 let final:LiteratureSearchResponse[]=[];
 let visibleLabels:string[]=[];
 const task=collectChineseCategories(plan,{status:async()=>({collector}),
  create:async(_q,limit,kind)=>{
   assert.equal(limit,200);created.push(kind!);
   const rows=Array.from({length:kind==='thesis'?230:kind==='journal'?200:1},(_,i)=>paper(`${kind} ${i}`,'cnki',{publication_category:labels[kind!]}));
   return {...job(kind==='journal'?'working':'completed',kind==='journal'?[]:rows),id:kind!};
  },
  read:async id=>({...job('completed',Array.from({length:200},(_,i)=>paper(`journal ${i}`,'cnki',{publication_category:'中文期刊'}))),id}),
  sleep:()=>new Promise<void>(resolve=>releases.push(resolve)),active:()=>true,
  update:(s,batches)=>{final=batches;visibleLabels=(s.groups || []).map(g=>g.label);if(batches.some(r=>r.papers.some(p=>p.publication_category==='学位论文')) && !batches.some(r=>r.papers.some(p=>p.publication_category==='中文期刊'))) sawTheses=true;},
 },language);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(sawTheses,true);
 assert.deepEqual(created,['journal','thesis','conference','book','other']);
 releases.forEach(release=>release());await task;
 const merged=mergeLiteratureResults('query',plan,final);
 assert.equal(merged.total,403);
 assert.equal(merged.papers.filter(p=>p.publication_category==='学位论文').length,200);
 assert.equal(merged.papers.filter(p=>p.publication_category==='中文期刊').length,200);
 assert.deepEqual(visibleLabels,language==='zh'?Object.values(labels):['Chinese journals','Theses','Conference papers','Books','Other']);
});
}

test('unavailable category collector ends as unavailable instead of perpetual searching',async()=>{
 let state='';
 await collectChineseCategories(plan,{status:async()=>({collector:{...collector,available:false}}),
  create:async()=>{throw Error('must not create');},read:async()=>job('completed'),sleep:async()=>{},active:()=>true,
  update:s=>{state=s.state;},
 },'zh');
 assert.equal(state,'unavailable');
});

test('category aggregation preserves the actual denial instead of only a zero count',async()=>{
 let active=true;const details:string[]=[];
 await collectChineseCategories(plan,{status:async()=>({collector}),
  create:async()=>({...job('paused'),detail:'知网拒绝当前访问（HTTP 403）'}),
  read:async()=>{throw Error('stopped');},sleep:async()=>{active=false;},active:()=>active,
  update:s=>{details.push(s.detail);details.push(...(s.groups||[]).map(g=>g.detail));},
 },'zh');
 assert.ok(details.some(detail=>detail.includes('HTTP 403')));
});

for (const scenario of ['abstracts','queued-abstracts','mixed','legacy'] as const) {
 test(`CNKI progress reflects unfinished categories and durable phases: ${scenario}`,async()=>{
  const releases:(()=>void)[]=[];
  let latest:SearchChannel={state:'queued',detail:''};
  const task=collectChineseCategories(plan,{status:async()=>({collector}),
   create:async(_q,_limit,kind)=>({
    ...job(kind==='journal'?'completed':scenario==='queued-abstracts'?'queued':'working'),id:kind!,detail:'',
    phase:scenario==='legacy'?undefined:scenario==='mixed' && kind==='other'?'index':'metadata',
   }),
   read:async id=>({...job('completed'),id}),
   sleep:()=>new Promise<void>(resolve=>releases.push(resolve)),active:()=>true,
   update:s=>{latest=s;},
  },'zh');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(latest.phase,scenario==='mixed'?'mixed':scenario==='legacy'?'index':'metadata');
  const text=cnkiProgressLabel(latest,phrase=>phrase.zh);
  if (scenario==='abstracts') assert.match(text,/中文题录已获取，正在读取知网摘要/);
  if (scenario==='queued-abstracts') {
   assert.equal(latest.state,'queued');assert.match(text,/等待读取知网摘要/);
  }
  if (scenario==='mixed') assert.match(text,/正在获取中文题录，并读取/);
  if (scenario==='legacy') assert.match(text,/正在获取中文题录/);
  releases.forEach(release=>release());await task;
  assert.equal(latest.state,'completed');
 });
}

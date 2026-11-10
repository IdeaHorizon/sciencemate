const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const script=fs.readFileSync(require('node:path').resolve(__dirname,'../app/assets/cnki-extension/cnki-page.js'),'utf8').replace('export async function','async function');
(async()=>{
 for(const [status,expected] of [[403,'access_denied'],[417,'waiting_for_verification'],[429,'rate_limited'],[503,'metadata_unavailable']]){
  const context={document:{querySelectorAll:()=>[]},location:{pathname:'/kns8s/search'},URLSearchParams,AbortSignal,
   fetch:async()=>({ok:false,status})};
  const result=await vm.runInNewContext(script+';cnkiPage("export",{exportId:"fixture-id"})',context);
  assert.equal(result.error,expected);assert.equal(result.httpStatus,status);assert.equal(result.endpoint,'/dm8/API/GetExport');
 }
 console.log('metadata HTTP denials retain their status and pause instead of becoming missing abstracts');
})();

(async()=>{
 for(const [resource,expected] of [['BOOK','图书'],['DISSERTATION','学位论文'],['CONFERENCE','会议'],['JOURNAL','期刊'],['CROSSDB','其他']]){
  const row={querySelector:q=>q==='td.name a.fz14'?{innerText:'研究资料',href:'https://kns.cnki.net/abstract'}:null,querySelectorAll:()=>[]};
  const context={location:{pathname:'/kns8s/search'},document:{body:{innerText:''},
   querySelector:q=>q==='#resource'?{value:resource}:null,
   querySelectorAll:q=>q==='.result-table-list tbody tr'?[row]:[]}};
  const result=await vm.runInNewContext(script+';cnkiPage("state")',context);
  assert.equal(result.rows[0].document_type,expected);
 }
 console.log('filtered tables without a type column use their collection, not a journal default');
})();
(async()=>{
 for(const [status,expected] of [[417,'waiting_for_verification'],[403,'access_denied'],[429,'rate_limited'],[503,'results_unavailable']]){
  let handler;
  class XHR{open(){} addEventListener(_,cb){handler=cb;}}
  const input={value:'',dispatchEvent(){}},field={click(){},getAttribute(){return 'SU'},querySelector(){return null}};
  const button={click(){const x=new XHR();x.open('POST','/kns8s/brief/grid');x.status=status;handler.call(x);}};
  const document={body:{innerText:'暂无数据，请重新检索'},querySelector(q){if(q==='input.search-input')return input;if(q==='input.search-btn')return button;if(q.includes('li.cur')||q.includes('li[data-val='))return field;return null;},querySelectorAll(){return []}};
  const context={document,location:{pathname:'/kns8s/search'},XMLHttpRequest:XHR,Date,setTimeout,Event:class{}};
  const result=await vm.runInNewContext(script+';cnkiPage("search",{query:"LBM",field:"SU"})',context);
  assert.equal(result.error,expected);
 }
 console.log('4 CNKI HTTP response cases passed');
})();

(async()=>{
 // Tencent may leave its verification UI mounted far outside the viewport.
 // Presence/offsetWidth alone must not block a query or prompt the user.
 for (const [top, expected] of [[-999756,false],[180,true]]) {
  const footer={getBoundingClientRect:()=>({width:52,height:18,top,bottom:top+18})};
  const document={body:{innerText:''},querySelector:()=>null,querySelectorAll:q=>q.includes('tcaptcha_transform_dy')?[footer]:[]};
  const context={document,location:{pathname:'/kns8s/AdvSearch'},getComputedStyle:()=>({display:'block',visibility:'visible'}),innerHeight:900};
  const state=await vm.runInNewContext(script+';cnkiPage("state")',context);
  assert.equal(state.captcha,expected);
  if(expected){
   const blocked=await vm.runInNewContext(script+';cnkiPage("search",{query:"LBM"})',context);
   assert.equal(blocked.error,'waiting_for_verification');
  }
 }
 console.log('2 visible/hidden verification cases passed');
})();

(async()=>{
 // An advanced page includes a hidden legacy search form. The active advanced
 // form must receive the query, field, and click, without stale author filters.
 let handler,advancedClicks=0,legacyClicks=0,fieldSelected=false;
 class XHR{open(){} addEventListener(_,cb){handler=cb;}}
 const field={click(){fieldSelected=true;},querySelector(){return null;}};
 const box={querySelector(q){return q.startsWith('li[')?field:{getAttribute:()=>fieldSelected?'SU':'AU'};}};
 const advanced={value:'',getBoundingClientRect:()=>({width:200,height:30}),closest:()=>box,dispatchEvent(){}};
 const author={value:'old author',dispatchEvent(){}};
 const legacy={value:'old query'};
 const document={body:{innerText:''},querySelector(q){
   if(q==='input[data-tipid="gradetxt-1"]')return advanced;
   if(q==='input.search-input')return legacy;
   if(q==='input.search-btn')return {click(){legacyClicks++;}};
   if(q==='input.btn-search')return {click(){advancedClicks++;const x=new XHR();x.open('POST','/kns8s/brief/grid');x.status=417;handler.call(x);}};
   return null;
 },querySelectorAll(q){return q==='input[data-tipid^="gradetxt-"]'?[advanced,author]:[];}};
 const context={document,location:{pathname:'/kns8s/AdvSearch'},XMLHttpRequest:XHR,Date,setTimeout,Event:class{}};
 const result=await vm.runInNewContext(script+';cnkiPage("search",{query:"LBM",field:"SU"})',context);
 assert.equal(result.error,'waiting_for_verification');
 assert.equal(advanced.value,'LBM');assert.equal(author.value,'');
 assert.equal(legacy.value,'old query');assert.equal(advancedClicks,1);assert.equal(legacyClicks,0);
 console.log('advanced form selection case passed');
})();

(async()=>{
 const dialog={getBoundingClientRect:()=>({width:340,height:284,top:205,bottom:489})};
 const document={body:{innerText:''},querySelector:()=>null,querySelectorAll:q=>q.includes('.verifybox')?[dialog]:[]};
 const context={document,location:{pathname:'/kns8s/AdvSearch'},getComputedStyle:()=>({display:'block',visibility:'visible'}),innerHeight:693};
 const state=await vm.runInNewContext(script+';cnkiPage("state")',context);
 assert.equal(state.captcha,true);
 const result=await vm.runInNewContext(script+';cnkiPage("search",{query:"LBM"})',context);
 assert.equal(result.error,'waiting_for_verification');
 console.log('CNKI verifybox blocks automatic submission');
})();

(async()=>{
 // Resolve and hit-test the actual submit button inside the background tab.
 // The bridge receives only the control coordinates; no credentials or text.
 let handler,activations=0;
 class XHR{open(){} addEventListener(_,cb){handler=cb;}}
 const input={value:'',dispatchEvent(){}},field={click(){},getAttribute(){return 'SU'},querySelector(){return null}};
 const button={scrollIntoView(){},getBoundingClientRect:()=>({x:20,y:30,width:100,height:40,top:30,bottom:70}),click(){throw new Error('must use native control activation');}};
 const document={addEventListener(){},removeEventListener(){},elementFromPoint:(x,y)=>x===70&&y===50?button:null,body:{innerText:''},querySelector(q){
  if(q==='input.search-input')return input;if(q==='input.search-btn')return button;
  if(q.includes('li.cur')||q.includes('li[data-val='))return field;return null;
 },querySelectorAll(){return []}};
 const context={document,location:{pathname:'/kns8s/search'},XMLHttpRequest:XHR,Date,setTimeout,Event:class{},
  getComputedStyle:()=>({display:'block',visibility:'visible'}),innerHeight:900};
 const prepared=await vm.runInNewContext(script+';cnkiPage("search",{query:"LBM",field:"SU",prepareOnly:true})',context);
 assert.deepEqual(JSON.parse(JSON.stringify(prepared)),{activation:{x:70,y:50}});
 const x=new XHR();x.open('POST','/kns8s/brief/grid');x.status=417;handler.call(x);
 const result=await vm.runInNewContext(script+';cnkiPage("wait")',context);
 assert.equal(result.error,'waiting_for_verification');assert.equal(input.value,'LBM');
 assert.equal(context.__sciencemateCnkiPending,undefined);
 console.log('background submit hit-tests and activates the native button');
})();

function paginationFixture(){
 const handlers=new Map();let response;
 class XHR{open(){}addEventListener(_,cb){response=cb;}}
 const next={innerText:'下一页',isConnected:true,contains:()=>false,scrollIntoView(){},
   getBoundingClientRect:()=>({x:20,y:30,width:100,height:40,top:30,bottom:70})};
 const makeRow=title=>({querySelector(q){return q==='td.name a.fz14'?{innerText:title,href:'https://kns.cnki.net/abstract'}:null;},querySelectorAll(){return [];}});
 const state={page:1,row:makeRow('first page')};
 const document={body:{innerText:''},elementFromPoint:()=>next,
  addEventListener(type,fn){if(!handlers.has(type))handlers.set(type,new Set());handlers.get(type).add(fn);},
  removeEventListener(type,fn){handlers.get(type)?.delete(fn);},
  querySelector(q){if(q==='.countPageMark')return {innerText:state.page+'/5'};
   if(q==='.result-table-list tbody tr')return state.row;return null;},
  querySelectorAll(q){if(q==='.result-table-list tbody tr')return [state.row];if(q==='.pages a')return [next];return [];}};
 const context=vm.createContext({document,location:{pathname:'/kns8s/search'},XMLHttpRequest:XHR,Date,setTimeout,
   getComputedStyle:()=>({display:'block',visibility:'visible'}),innerHeight:900});
 vm.runInContext(script,context);
 return {state,next,makeRow,context,
  run:(action,args={})=>vm.runInContext(`cnkiPage(${JSON.stringify(action)},${JSON.stringify(args)})`,context),
  reply(){const x=new XHR();x.open('POST','/kns8s/brief/grid');x.status=200;response.call(x);},
  dispatch(type,target){const e={type,target,preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;}};
    for(const fn of [...(handlers.get(type)||[])]){fn(e);if(e.stopped)break;}return e;},
  listening:()=>[...handlers.values()].reduce((n,s)=>n+s.size,0)};
}

(async()=>{
 const f=paginationFixture();await f.run('next',{prepareOnly:true});
 assert.ok(f.listening()>0);
 const download={};
 for(const type of ['pointerdown','mousedown','pointerup','mouseup','click']){
  const event=f.dispatch(type,download);assert.equal(event.prevented,true);assert.equal(event.stopped,true);
 }
 // Even if the pointer later reaches the right button, this rejected activation
 // cannot submit it and then cause a duplicate submission during recovery.
 assert.equal(f.dispatch('click',f.next).prevented,true);
 const result=await f.run('wait');assert.equal(result.error,'browser_activation_moved');
 assert.equal(f.listening(),0);assert.equal(f.context.__sciencemateCnkiPending,undefined);
 console.log('moved pagination clicks cannot activate download/login links');
})();

(async()=>{
 const f=paginationFixture();await f.run('next',{prepareOnly:true});
 assert.equal(f.dispatch('click',f.next).prevented,undefined);assert.equal(f.listening(),0);
 f.reply();let done=false;
 const result=f.run('wait').then(value=>{done=true;return value;});
 await new Promise(resolve=>setTimeout(resolve,20));
 assert.equal(done,false,'HTTP 200 with the old page must not acknowledge a new page');
 f.state.page=2;f.state.row=f.makeRow('second page');
 assert.equal((await result).page,2);
 console.log('pagination waits for the requested page and new rows');
})();

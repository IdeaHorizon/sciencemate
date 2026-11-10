import {test} from "node:test";
import assert from "node:assert/strict";
import {sourceWarning,sourceNotices} from "./source-warning.ts";

test("old cached HTTP error bodies stay out of source notices",()=>{
  for (const [status, body] of [[500,'<!DOCTYPE html><html>NCBI diagnostic</html>'],[503,'{"errMsg":"unavailable"}']] as const) {
    const warning={code:"source_request_failed",sources:["pubmed"],detail:`HTTP ${status}: ${body}`};
    const before=JSON.stringify(warning);
    const message=sourceWarning(warning,p=>p.zh)!;
    assert.match(message,new RegExp(`HTTP ${status}`));
    assert.match(message,/不影响其他来源/);
    assert.doesNotMatch(message,/DOCTYPE|html|errMsg/);
    assert.equal(JSON.stringify(warning),before);
  }
});

test("source rate limiting remains distinct from timeout and server errors",()=>{
  assert.match(sourceWarning({code:"source_request_failed",http_status:429,detail:"HTTP 429"},p=>p.zh) || "",/限流/);
  assert.match(sourceWarning({code:"source_request_failed",reason:"timeout",detail:"timeout"},p=>p.zh) || "",/超时/);
});

test('partial CNKI categories and empty international batches do not duplicate or contradict results',()=>{
  const empty={code:'remote_search_no_match',detail:'远程来源没有合格结果'};
  const cnki={code:'source_incomplete',sources:['cnki'],detail:'知网暂未提供结果'};
  const failure={code:'source_request_failed',sources:['pubmed'],http_status:503,detail:'HTTP 503'};
  const warnings=[empty,empty,cnki,cnki,cnki,failure,failure];
  const before=JSON.stringify(warnings);
  const messages=sourceNotices(warnings,{hasRemoteResults:true,coveredSources:['cnki']},p=>p.zh);
  assert.equal(messages.length,1);assert.match(messages[0],/pubmed/);
  assert.deepEqual(sourceNotices([empty,empty],{hasRemoteResults:false,coveredSources:[]},p=>p.zh),[empty.detail]);
  assert.equal(JSON.stringify(warnings),before);
});

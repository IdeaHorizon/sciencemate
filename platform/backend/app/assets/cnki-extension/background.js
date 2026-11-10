import {cnkiPage} from "./cnki-page.js";

let running = false;
let detailQueue = Promise.resolve();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const read = () => chrome.storage.local.get(null);
const save = value => chrome.storage.local.set(value);
const cleanPaper = row => {const {exportId, ...paper} = row; return paper;};
const paperKey = p => p.title.normalize("NFKC").replace(/\s/g, "").toLowerCase() + "\0" + (p.year || (p.pub_date || "").slice(0,4));

async function api(path, body, config) {
  const cfg = config || await read();
  const response = await fetch(cfg.server + "/api/v1/cnki" + path, {
    method: "POST", headers: {"Content-Type": "application/json", ...(cfg.token ? {Authorization: "Bearer " + cfg.token} : {})},
    body: JSON.stringify(body), signal: AbortSignal.timeout(15000), redirect: "error",
  });
  if (!response.ok) {
    if (response.status === 401) await save({token: "", currentJob: null, state: "disconnected"});
    const error = new Error(response.status === 409 ? "lease_expired" : response.status === 401 ? "disconnected" : "platform_unavailable");
    throw error;
  }
  return response.json();
}

async function badge(state, detail = "") {
  await save({state, detail});
  await chrome.action.setBadgeText({text: state === "waiting_for_verification" ? "验证" : state === "working" ? "采集" : state === "error" ? "!" : ""});
  await chrome.action.setBadgeBackgroundColor({color: state === "working" ? "#2b7567" : "#b76520"});
}

async function execute(tabId, action, args = {}) {
  const tab = await chrome.tabs.get(tabId).catch(() => {throw new Error("browser_closed");});
  if (new URL(tab.url || "about:blank").hostname !== "kns.cnki.net") throw new Error("browser_closed");
  const output = await chrome.scripting.executeScript({target: {tabId}, func: cnkiPage, args: [action, args]});
  const result = output[0]?.result;
  if (!result) throw new Error("cnki_unavailable");
  if (result.error) throw new Error(result.error);
  return result;
}

async function loaded(tabId) {
  for (let n = 0; n < 100; n++) {
    const tab = await chrome.tabs.get(tabId).catch(() => {throw new Error("browser_closed");});
    if (tab.status === "complete") return;
    await sleep(150);
  }
  throw new Error("cnki_timeout");
}

async function searchTab() {
  const cfg = await read();
  if (cfg.tabId) {
    try {
      const tab = await chrome.tabs.get(cfg.tabId);
      if (new URL(tab.url || "about:blank").hostname === "kns.cnki.net") return tab.id;
    } catch {}
  }
  const tab = await chrome.tabs.create({url: "https://kns.cnki.net/kns8s/search", active: false});
  await save({tabId: tab.id});
  await loaded(tab.id);
  return tab.id;
}

async function report(job, state, detail = "") {
  await api(`/worker/jobs/${job.id}/report`, {lease: job.lease, state, detail});
}

async function metadata(tabId, row, cached) {
  if (cached) return {...cleanPaper(row), ...cached, url: row.url};
  const paper = cleanPaper(row);
  try {
    const value = await execute(tabId, "export", {exportId: row.exportId});
    if (paperKey({...row,title:value.title}) !== paperKey(row)) throw new Error("metadata_title_mismatch");
    for (const [key, item] of Object.entries(value)) if (item || !(key in paper)) paper[key] = item;
    if (paper.abstract_truncated) {
      const expand = async () => {
      if ((await read()).detailTabId) throw new Error("waiting_for_verification");
      let detailTab;
      try {
        detailTab = await chrome.tabs.create({url: row.url, active: false});
        await save({detailTabId: detailTab.id});
        await loaded(detailTab.id);
        const detail = await execute(detailTab.id, "abstract");
        if (detail.title?.replace(/\s*(网络首发|附视频)\s*$/, "").trim() === row.title && detail.abstract.length > paper.abstract.length) {
          paper.abstract = detail.abstract; paper.abstract_truncated = detail.abstract_truncated;
        }
      } catch (error) {
        if (["waiting_for_verification","access_denied","rate_limited"].includes(error.message)) {detailTab = null; throw error;}
        // A partial abstract is kept with an explicit truncation marker.
      } finally {
        if (detailTab) {await chrome.tabs.remove(detailTab.id).catch(() => {}); await save({detailTabId: null});}
      }
      };
      const expanded = detailQueue.then(expand);
      detailQueue = expanded.catch(() => {});
      await expanded;
    }
  } catch (error) {
    if (["waiting_for_verification", "access_denied", "rate_limited", "metadata_title_mismatch", "browser_closed"].includes(error.message)) throw error;
    // Bounded attempt; the platform can display an index even when the source
    // provides no abstract. A future refresh may retry incomplete records.
    paper.metadata_attempted = true;
  }
  return paper;
}

async function collect(job) {
  const tabId = await searchTab();
  await loaded(tabId);
  let state = await execute(tabId, "state");
  const resource = {journal:"JOURNAL",thesis:"DISSERTATION",conference:"CONFERENCE",book:"BOOK"}[job.document_kind] || "CROSSDB";
  if (state.captcha) throw new Error("waiting_for_verification");
  if (state.query !== job.query || state.page !== job.page || state.field !== job.field || state.resource !== resource) {
    await chrome.tabs.update(tabId, {url: "https://kns.cnki.net/kns8s/search"});
    await loaded(tabId);
    state = await execute(tabId, "search", {query: job.query, field: job.field});
    if (state.resource !== resource) state = await execute(tabId,"resource",{resource});
    while (state.page < job.page && state.hasNext) {
      state = await execute(tabId, "next");
      await report(job, "working");
    }
  }
  if (state.page !== job.page) throw new Error("result_pages_changed");
  while (true) {
    const cfg = await read();
    if (cfg.paused) throw new Error("paused");
    const remaining = job.limit == null || job.document_kind ? 20 : Math.max(0, job.limit - (job.page - 1) * 20);
    const wanted = state.rows.slice(0, remaining);
    const beforeWindow = row => /^\d{4}-\d{2}-\d{2}/.test(row.pub_date) && row.pub_date.slice(0,10) < job.since;
    const within = wanted.filter(row => !job.since || !beforeWindow(row));
    const receipt = await api(`/worker/jobs/${job.id}/batch`, {lease:job.lease, page:job.page, papers:within.map(cleanPaper), total:state.total});
    const cached = await api("/worker/cache", within.map(cleanPaper));
    const byTitle = new Map(cached.papers.map(p => [paperKey(p), p]));
    const pending = within.filter(row => !(job.accepted || []).includes(row.title));
    // At most two CNKI metadata requests. Upload one completed pair at a time;
    // closing Chrome loses at most the current pair, not all completed pages.
    for (let i = 0; i < pending.length; i += 2) {
      if ((await read()).paused) throw new Error("paused");
      await report(job, "working");
      const results = await Promise.allSettled(pending.slice(i, i+2).map(row => metadata(tabId,row,byTitle.get(paperKey(row)))));
      const papers = results.filter(r => r.status === "fulfilled").map(r => r.value);
      await api(`/worker/jobs/${job.id}/batch`, {lease:job.lease, page:job.page, papers, total:state.total});
      const failed = results.find(r => r.status === "rejected");
      if (failed) throw failed.reason;
      await badge("working", `${job.query} · 第 ${job.page} 页 · 已处理 ${Math.min(i+2,pending.length)}/${pending.length}`);
    }
    const finished = !state.hasNext || !wanted.length || (job.limit != null && (job.document_kind ? receipt.count >= job.limit : job.page * 20 >= job.limit))
      || (job.since && state.dateSorted && state.rows.length > 0 && state.rows.every(beforeWindow));
    await api(`/worker/jobs/${job.id}/batch`, {lease:job.lease, page:job.page, papers:[], total:state.total, page_complete:true, finished:!!finished});
    if (finished) {await save({currentJob:null}); return;}
    job = {...job, page:job.page+1, accepted:[]};
    await save({currentJob:job});
    state = await execute(tabId, "next");
  }
}

async function tick() {
  if (running) return;
  running = true;
  let job;
  try {
    const cfg = await read();
    if (!cfg.token || cfg.paused) return;
    if (cfg.state === "waiting_for_verification") {
      for (const tabId of [cfg.tabId, cfg.detailTabId].filter(Boolean)) {
        try {if ((await execute(tabId,"state")).captcha) {await api("/worker/poll",{state:"waiting_for_verification",detail:"请在本机 Chrome 完成知网验证"}); return;}}
        catch (error) {if (error.message !== "browser_closed") throw error;}
      }
      if (cfg.detailTabId) {
        await chrome.tabs.remove(cfg.detailTabId).catch(() => {});
        await save({detailTabId:null});
      }
    }
    job = cfg.currentJob;
    if (job) {
      try {await report(job,"working");}
      catch (error) {if (error.message !== "lease_expired") throw error; job=null; await save({currentJob:null});}
    }
    if (!job) job = (await api("/worker/poll",{state:"idle"})).job;
    if (!job) {await badge("idle","已连接，等待采集任务");return;}
    await save({currentJob:job});
    await badge("working",job.query);
    await collect(job);
    await badge("idle","本轮 index 已同步");
    setTimeout(() => void tick(),500);
  } catch (error) {
    const state = error.message === "access_denied" ? "paused" : ["waiting_for_verification","browser_closed","paused"].includes(error.message) ? error.message : "error";
    const messages = {waiting_for_verification:"请在本机 Chrome 完成知网验证，之后自动继续", browser_closed:"知网标签页已关闭，点击继续采集", paused:"采集已暂停", rate_limited:"知网暂时限制请求，稍后重试", platform_unavailable:"平台暂时无法连接，已保留采集进度", disconnected:"连接已失效，请重新配对", result_pages_changed:"搜索结果页数已变化，请重新发起检索"};
    const detail = error.message === "access_denied" ? "知网拒绝当前访问（HTTP 403），已暂停请求；请检查知网页面后再继续" : messages[error.message] || "采集暂时未完成，将从已确认的进度重试";
    await badge(state,detail);
    if (job) {
      try {await report(job,state,detail); await save({currentJob:null});} catch {}
    }
    if (state === "browser_closed" || ["rate_limited","access_denied"].includes(error.message)) await save({paused:true});
  } finally {running=false;}
}

async function ensureAlarm() {
  if (!await chrome.alarms.get("cnki-poll")) await chrome.alarms.create("cnki-poll",{periodInMinutes:.5});
}
chrome.alarms.onAlarm.addListener(alarm => {if (alarm.name === "cnki-poll") void tick();});
chrome.runtime.onStartup.addListener(() => {void ensureAlarm(); void tick();});
chrome.runtime.onInstalled.addListener(() => {void ensureAlarm();});
chrome.tabs.onRemoved.addListener((tabId, info) => {
  void read().then(async cfg => {
    if (cfg.tabId === tabId && !info.isWindowClosing) {await save({paused:true,tabId:null});await badge("browser_closed","知网标签页已关闭，点击继续采集");}
  });
});

async function pair(message) {
  const url = new URL(message.server);
  if (url.username || url.password || (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost","127.0.0.1"].includes(url.hostname)))) throw new Error("请使用 HTTPS 平台地址或本机 localhost 地址");
  const server = url.origin;
  const result = await api("/pair",{code:message.code,name:message.name || "我的 Chrome"},{server});
  await save({server,token:result.token,deviceId:result.device_id,paused:false,currentJob:null});
  await chrome.scripting.unregisterContentScripts({ids:["platform-wake"]}).catch(() => {});
  await chrome.scripting.registerContentScripts([{id:"platform-wake",matches:[server+"/*"],js:["platform-bridge.js"]}]);
  const tabs = await chrome.tabs.query({url:server+"/*"});
  for (const tab of tabs) await chrome.scripting.executeScript({target:{tabId:tab.id},files:["platform-bridge.js"]}).catch(() => {});
  await ensureAlarm();
  void tick();
  return {ok:true};
}

chrome.runtime.onMessage.addListener((message,sender,respond) => {
  // Page content scripts may only wake this collector. Pair/pause controls
  // are accepted solely from the extension's own popup.
  if (message.type === "wake") {void tick();respond({ok:true});return;}
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("popup.html")) return;
  const work = async () => {
    if (message.type === "pair") return pair(message);
    if (message.type === "pause") {
      await save({paused:true}); await badge("paused","采集已暂停");
      const cfg=await read(); if(cfg.token) await api("/worker/poll",{state:"paused"}).catch(()=>{});
    }
    if (message.type === "resume") {await save({paused:false}); void tick();}
    if (message.type === "open") {const cfg=await read();const id=cfg.detailTabId || await searchTab();await chrome.tabs.update(id,{active:true});}
    return {ok:true};
  };
  work().then(respond,error=>respond({error:error.message}));
  return true;
});
void ensureAlarm();

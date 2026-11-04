"use client";

import {useCallback,useEffect,useState} from "react";
import {Button} from "@/shared/ui";
import {useT} from "@/shared/i18n";
import {cancelCnkiJob,cnkiStatus,createCnkiJob,retryCnkiJob,type CnkiCategory,type CnkiCollector,type CnkiJob} from "@/features/academic-search/api/cnki";

const labels:Record<string,{zh:string;en:string}>={idle:{zh:"在线待命",en:"Ready"},offline:{zh:"本机离线",en:"Local device offline"},working:{zh:"正在采集",en:"Collecting"},queued:{zh:"等待后台采集",en:"Queued for collection"},completed:{zh:"本轮已完成",en:"Collection complete"},waiting_for_verification:{zh:"等待知网验证",en:"Waiting for CNKI verification"},browser_closed:{zh:"知网标签页已关闭",en:"CNKI tab closed"},paused:{zh:"已暂停",en:"Paused"},error:{zh:"暂时无法采集",en:"Collection unavailable"},revoked:{zh:"已断开",en:"Disconnected"},cancelled:{zh:"已取消",en:"Cancelled"}};
export default function CnkiSettings(){
  const t=useT();
  const [collector,setCollector]=useState<CnkiCollector>(),[jobs,setJobs]=useState<CnkiJob[]>([]);
  const [query,setQuery]=useState(""),[category,setCategory]=useState<CnkiCategory>("domain");
  const [error,setError]=useState(""),[busy,setBusy]=useState(false);
  const refresh=useCallback(async()=>{try{const data=await cnkiStatus();setCollector(data.collector);setJobs(data.jobs);}catch(err){setError(err instanceof Error?err.message:String(err));}},[]);
  useEffect(()=>{void refresh();const id=setInterval(()=>void refresh(),5000);return()=>clearInterval(id);},[refresh]);
  async function action(fn:()=>Promise<unknown>){setBusy(true);setError("");try{await fn();await refresh();}catch(err){setError(err instanceof Error?err.message:String(err));}finally{setBusy(false);}}
  return <div className="settings-page settings-compact-page">
    <header className="settings-page-header"><h1>{t({zh:"知网采集",en:"CNKI collection"})}</h1><p>{t({zh:"平台自动获取中文文献题录，用于搜索和推荐，无需安装扩展。",en:"The platform collects Chinese literature indexes for search and recommendations."})}</p></header>
    {error&&<p role="alert">{error}</p>}
    <section className="settings-compact-section"><h2>{t({zh:"后台采集",en:"Background collection"})}</h2><p>{collector?.detail || t({zh:"正在读取采集状态",en:"Loading collection status"})}</p><p>{t({zh:"知网需要人工验证时会保留进度，验证完成后自动接续。",en:"Progress is saved when CNKI requests verification. Collection resumes after verification."})}</p></section>
    <section className="settings-compact-section"><h2>{t({zh:"每日采集",en:"Daily collection"})}</h2><p>{t({zh:"每 24 小时更新一次。首次采集近 12 个月，后续增量更新；由平台后台执行，关闭网页不会中断；本地部署需保持平台运行。",en:"Updates every 24 hours. The first collection covers the past 12 months; later updates are incremental. Closing this page does not stop background collection. Keep a local deployment running."})}</p>
      <form onSubmit={event=>{event.preventDefault();void action(async()=>{await createCnkiJob(query.trim(),true,category);setQuery("");});}} style={{display:"flex",gap:10,flexWrap:"wrap"}}>
        <select aria-label={t({zh:"采集类型",en:"Collection type"})} value={category} onChange={event=>setCategory(event.target.value as CnkiCategory)}><option value="domain">{t({zh:"领域关键词",en:"Discipline keywords"})}</option><option value="journal">{t({zh:"期刊名称",en:"Journal name"})}</option><option value="scholar">{t({zh:"学者姓名",en:"Scholar name"})}</option><option value="project">{t({zh:"课题关键词",en:"Project keywords"})}</option></select>
        <input aria-label={t({zh:"知网检索词",en:"CNKI search query"})} required maxLength={500} value={query} onChange={event=>setQuery(event.target.value)} placeholder={category==="journal"?t({zh:"例如：计算机学报",en:"For example: Chinese Journal of Computers"}):category==="scholar"?t({zh:"作者姓名",en:"Author name"}):t({zh:"例如：格子玻尔兹曼 颗粒沉降",en:"For example: lattice Boltzmann particle sedimentation"})} style={{flex:1,minWidth:200,padding:8}}/>
        <Button type="submit" disabled={busy||!query.trim()}>{t({zh:"添加每日采集",en:"Add daily collection"})}</Button>
      </form>
      {category==="scholar"&&<p>{t({zh:"作者姓名检索可能包含同名作者，请结合题录中的单位核对。",en:"Name searches may include different authors with the same name. Check their affiliations."})}</p>}
      {jobs.filter(job=>job.enabled).map(job=><div className="settings-row" key={job.id}><span><strong>{job.query}{job.recurring?t({zh:" · 每日",en:" \u00b7 Daily"}):t({zh:" · 搜索",en:" \u00b7 Search"})}</strong><small>{labels[job.state]?t(labels[job.state]):job.state} {t({zh:"· 已获取 {count} · 第 {page} 页",en:"\u00b7 Collected {count} \u00b7 Page {page}"},{count:job.count,page:job.page})}{job.detail?` · ${job.detail}`:""}</small></span>{["waiting_for_verification","error","paused"].includes(job.state)&&<Button disabled={busy} onClick={()=>void action(()=>retryCnkiJob(job.id))}>{t({zh:"重试",en:"Retry"})}</Button>}<Button disabled={busy} onClick={()=>void action(()=>cancelCnkiJob(job.id))}>{job.recurring?t({zh:"取消订阅",en:"Unsubscribe"}):t({zh:"取消任务",en:"Cancel task"})}</Button></div>)}
    </section>
  </div>;
}

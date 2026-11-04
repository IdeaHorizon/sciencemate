"use client";
import { PRODUCT_NAME } from "@/shared/brand";
import type { ReactNode } from "react";
import { useT } from "@/shared/i18n";

const examples = [
  { label: { zh: "研究问题", en: "Research question" }, query: { zh: "颗粒沉降模拟中，如何耦合浸入边界法与格子玻尔兹曼方法？", en: "How are the immersed boundary and lattice Boltzmann methods coupled in particle sedimentation simulations?" } },
  { label: { zh: "文献调研", en: "Literature review" }, query: { zh: "查找 MOF 材料分离 CO₂ 和 N₂ 的综述论文", en: "Find review papers on MOF materials for CO2 and N2 separation." } },
  { label: { zh: "论文标题", en: "Paper title" }, query: { zh: "LineShine: online acceleration for HPC-AI converged scientific computing", en: "LineShine: online acceleration for HPC-AI converged scientific computing" } },
];
export function AcademicSearchLanding({ initial, onExample, children }: { initial: boolean; onExample: (query: string) => void; children: ReactNode }) {
  const t = useT();
  return <div className={initial ? "academic-discovery academic-discovery-initial" : "academic-discovery"}>
    <div className="academic-discovery-main">
      <header className="academic-discovery-heading">
        <div className="academic-discovery-eyebrow">{PRODUCT_NAME} / {t({zh:"学术搜索",en:"Academic search"})}</div>
        <h2>{t({ zh: "从一个问题，", en: "Start with a question." })}<br />{t({ zh: "找到研究的起点", en: "Find your next discovery." })}</h2>
        <p>{t({ zh: "输入研究问题、关键词或论文标题，探索与你相关的学术文献。", en: "Explore relevant literature with a research question, keywords, or a paper title." })}</p>
      </header>
      {children}
      {!initial && <details className="academic-discovery-compact-tips">
        <summary>{t({ zh: "让问题更清楚一点", en: "Make your question clearer" })}</summary>
        <p>{t({ zh: "可以补充研究对象、方法与条件，以及想找的文献类型。直接描述即可，不必自己组织检索表达式。", en: "Add your research subject, methods or conditions, and the type of literature you need. Simply describe your question." })}</p>
      </details>}
      {initial && <div className="academic-discovery-examples">
        <div className="academic-discovery-example-label"><span>{t({ zh: "试试这样搜索", en: "Try a starting point" })}</span><span>{t({ zh: "点击填入 ↗", en: "Click to fill ↗" })}</span></div>
        {examples.map((e, i) => <button key={i} type="button" className="academic-discovery-example" onClick={() => onExample(t(e.query))}><span className="academic-discovery-example-kind"><span>0{i + 1}</span>{t(e.label)}</span><span>{t(e.query)}</span></button>)}
      </div>}
    </div>
    {initial && <aside className="academic-discovery-guide" aria-label={t({ zh: "搜索提问提示", en: "Search tips" })}>
      <div className="academic-discovery-art" aria-hidden="true">
        <svg viewBox="0 0 260 270" fill="none"><g stroke="currentColor" strokeWidth="1"><ellipse cx="130" cy="132" rx="91" ry="40" transform="rotate(-40 130 132)" /><ellipse cx="130" cy="132" rx="91" ry="40" transform="rotate(40 130 132)" /><ellipse cx="130" cy="132" rx="40" ry="94" /><path d="m55 77 141 111M65 195l127-131" strokeDasharray="3 7" /></g><path className="academic-discovery-mark" d="m101 164 29-89 30 89-30-16Z" fill="currentColor" /><circle cx="60" cy="81" r="6" fill="currentColor" /><circle cx="197" cy="181" r="4" fill="currentColor" /><circle cx="130" cy="38" r="3" fill="currentColor" /></svg>
        <span>{PRODUCT_NAME.toUpperCase()} · {t({zh:"探索",en:"Discover"})}</span>
      </div>
      <div className="academic-discovery-guide-copy">
        <h3>{t({ zh: "让问题更清楚一点", en: "Make your question clearer" })}</h3>
        <p className="academic-discovery-guide-intro">{t({ zh: "不用一次写完整，从你最关心的开始。", en: "Start with what matters most to you." })}</p>
        <ol>
          <li><strong>{t({ zh: "研究对象", en: "Research subject" })}</strong><p>{t({ zh: "你关注什么现象、材料或人群？", en: "Which phenomenon, material, or population interests you?" })}</p></li>
          <li><strong>{t({ zh: "方法与条件", en: "Methods and conditions" })}</strong><p>{t({ zh: "是否有指定方法、实验条件或应用场景？", en: "Any particular method, experimental condition, or application?" })}</p></li>
          <li><strong>{t({ zh: "文献范围", en: "Literature scope" })}</strong><p>{t({ zh: "想找综述、实验研究，还是某篇具体论文？", en: "Reviews, experimental studies, or a specific paper?" })}</p></li>
        </ol>
        <p className="academic-discovery-guide-note">{t({ zh: "不需要自己组织复杂的检索表达式，直接描述你的问题即可。", en: "Simply describe your question; no complex search syntax needed." })}</p>
      </div>
    </aside>}
  </div>;
}

"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Plus, Save, Trash2, X } from "lucide-react";
import {
  api,
  type ResearchInstruction,
  type ResearchInstructionScope,
  type SaveResearchSettingsRequest,
} from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Button, Empty, Skeleton } from "@/shared/ui";
import { pushError, pushSuccess } from "@/stores/notification";
import { useAuth } from "@/features/auth/AuthProvider";
import { PersonalInstructionsEditor } from "@/features/instructions";
import { useT, type Phrase } from "@/shared/i18n";

const EMPTY_SETTINGS: SaveResearchSettingsRequest = {
  response_language: "auto",
  citation_style: "author_year",
  evidence_standard: "balanced",
  memory_enabled: true,
  instructions: [],
};

const SCOPE_LABELS: Record<ResearchInstructionScope, Phrase> = {
  all: { zh: "所有回答与研究", en: "All answers and research" },
  literature: { zh: "文献与证据", en: "Literature and evidence" },
  experiments: { zh: "实验与可复现性", en: "Experiments and reproducibility" },
  writing: { zh: "文档与 PDF", en: "Documents and PDFs" },
  review: { zh: "评审", en: "Review" },
};

function editableSettings(settings: SaveResearchSettingsRequest): SaveResearchSettingsRequest {
  return {
    response_language: settings.response_language,
    citation_style: settings.citation_style,
    evidence_standard: settings.evidence_standard,
    memory_enabled: settings.memory_enabled,
    instructions: settings.instructions.map((instruction) => ({ ...instruction })),
  };
}

function settingsEqual(left: SaveResearchSettingsRequest, right: SaveResearchSettingsRequest) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function newInstruction(): ResearchInstruction {
  return {
    id: crypto.randomUUID(),
    title: "",
    scope: "all",
    instruction: "",
    enabled: true,
  };
}

export default function ResearchSettingsPage() {
  const t = useT();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: qk.researchSettings(),
    queryFn: () => api.getResearchSettings(),
  });
  const [draft, setDraft] = useState<SaveResearchSettingsRequest | null>(null);
  const [instructionDraft, setInstructionDraft] = useState<ResearchInstruction | null>(null);

  useEffect(() => {
    if (settings.data && draft === null) setDraft(editableSettings(settings.data));
  }, [draft, settings.data]);

  const current = draft ?? EMPTY_SETTINGS;
  const dirty = useMemo(
    () => !!settings.data && !settingsEqual(current, editableSettings(settings.data)),
    [current, settings.data],
  );

  const save = useMutation({
    mutationFn: (payload: SaveResearchSettingsRequest) => api.saveResearchSettings(payload),
    onSuccess: (saved) => {
      queryClient.setQueryData(qk.researchSettings(), saved);
      setDraft(editableSettings(saved));
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "研究设置没能保存", en: "Could not save research settings" })),
  });

  const applyInstruction = () => {
    if (!instructionDraft) return;
    const title = instructionDraft.title.trim();
    const instruction = instructionDraft.instruction.trim();
    if (!title || !instruction) return;
    const next = { ...instructionDraft, title, instruction };
    setDraft((value) => {
      const base = value ?? EMPTY_SETTINGS;
      const exists = base.instructions.some((item) => item.id === next.id);
      return {
        ...base,
        instructions: exists
          ? base.instructions.map((item) => item.id === next.id ? next : item)
          : [...base.instructions, next],
      };
    });
    setInstructionDraft(null);
  };

  return (
    <div className="settings-page research-settings-page">
      <header className="settings-header research-settings-header">
        <div>
          <span>{t({ zh: "Agent 行为", en: "Agent behavior" })}</span>
          <h1>{t({ zh: "个人指令", en: "Personal instructions" })}</h1>
          <p>{t({ zh: "你可复用的研究偏好和指令，用在新建的会话上。这些指令不会给出任何访问权限。", en: "Your reusable research preferences and guidance for new Sessions. These instructions never grant access." })}</p>
        </div>
        <Button
          size="sm"
          variant="primary"
          iconLeft={<Save size={13} />}
          disabled={!dirty}
          loading={save.isPending}
          onClick={() => save.mutate(current)}
        >{t({ zh: "保存改动", en: "Save changes" })}</Button>
      </header>

      {settings.isLoading && !settings.data && <div className="research-settings-loading"><Skeleton height={60} /><Skeleton height={160} /><Skeleton height={100} /></div>}
      {settings.isError && (
        <Empty title={t({ zh: "读不到研究设置", en: "Research settings unavailable" })} hint={t({ zh: "后端没有返回你实际生效的研究配置。", en: "The backend did not return your effective research configuration." })} />
      )}

      {settings.data && draft && (
        <div className="settings-layout">
          <div className="settings-content">
            <p className="research-effect-note"><strong>{t({ zh: "个人指令", en: "Personal instructions" })}</strong><span>{t({ zh: "只影响新会话", en: "New Sessions only" })}</span><span>{t({ zh: "已有会话不变", en: "Existing Sessions unchanged" })}</span></p>
            <dl className="research-settings-guide" aria-label={t({ zh: "研究默认值怎么生效", en: "How research defaults apply" })}>
              <div><dt>{t({ zh: "存在这里", en: "Saved here" })}</dt><dd>{t({ zh: "回答语言、引用格式、证据标准，以及你可复用的指令。", en: "Response language, citation style, evidence standard, and your reusable instructions." })}</dd></div>
              <div><dt>{t({ zh: "会话建立时冻结", en: "Frozen at start" })}</dt><dd>{t({ zh: "新会话建立时把当时生效的默认值记下来，之后再改这里不会改写已经做过的工作。", en: "A new Session records the effective defaults so later profile edits do not rewrite prior work." })}</dd></div>
              <div><dt>{t({ zh: "在别处强制执行", en: "Enforced elsewhere" })}</dt><dd>{t({ zh: "工具、数据、算力、发布和模型的访问权限，仍然由治理层机械判定。", en: "Tool, data, compute, publication, and model access remain mechanical governance permissions." })}</dd></div>
            </dl>

            <section className="settings-section">
              <div className="settings-section-heading"><div><span>{t({ zh: "回答与证据", en: "Answers and evidence" })}</span><h2>{t({ zh: "回答偏好", en: "Response preferences" })}</h2><p>{t({ zh: "你没有在请求里说得更具体时，模型按这些默认值来。", en: "These defaults guide model output when your request does not specify something more precise." })}</p></div></div>
              <div className="research-default-rows">
                <label><span><strong>{t({ zh: "回答语言", en: "Response language" })}</strong><small>{t({ zh: "影响你新建会话里的回答；你在对话里明确要求可以盖过它。", en: "Guides answers in Sessions you create; an explicit request can override it." })}</small></span><select value={current.response_language} onChange={(event) => setDraft({ ...current, response_language: event.target.value as typeof current.response_language })}><option value="auto">{t({ zh: "跟着提问走", en: "Follow each request" })}</option><option value="zh-CN">{t({ zh: "中文", en: "Chinese" })}</option><option value="en">{t({ zh: "英文", en: "English" })}</option></select></label>
                <label><span><strong>{t({ zh: "引用格式", en: "Citation style" })}</strong><small>{t({ zh: "生成参考文献的默认格式；具体任务或期刊要求可以盖过它。", en: "Default format for generated references; a task or venue requirement can override it." })}</small></span><select value={current.citation_style} onChange={(event) => setDraft({ ...current, citation_style: event.target.value as typeof current.citation_style })}><option value="author_year">{t({ zh: "作者–年份", en: "Author–year" })}</option><option value="numeric">{t({ zh: "数字编号", en: "Numeric" })}</option><option value="apa">APA 7</option></select></label>
                <label><span><strong>{t({ zh: "证据标准", en: "Evidence standard" })}</strong><small>{t({ zh: "影响它偏好什么来源、怎么报告不确定性。不改变数据访问权限。", en: "Guides source preference and uncertainty reporting. It does not change data access." })}</small></span><select value={current.evidence_standard} onChange={(event) => setDraft({ ...current, evidence_standard: event.target.value as typeof current.evidence_standard })}><option value="balanced">{t({ zh: "均衡 · 兼顾质量与广度", en: "Balanced · quality and breadth" })}</option><option value="strict">{t({ zh: "严格 · 优先一手来源", en: "Strict · prefer primary sources" })}</option><option value="exploratory">{t({ zh: "探索 · 更广的发现面", en: "Exploratory · broader discovery" })}</option></select></label>
              </div>
            </section>

            {user && (
              <PersonalInstructionsEditor />
            )}

            <section className="settings-section">
              <div className="settings-section-heading">
                <div><span>{t({ zh: "任务指令", en: "Task guidance" })}</span><h2>{t({ zh: "可复用的任务指令", en: "Reusable task guidance" })}</h2><p>{t({ zh: "短小、限定范围的偏好，作为 PROFILE.md 的补充，冻结进你每次新建的会话。", en: "Short, scoped preferences complement PROFILE.md and are frozen into each new Session you create." })}</p></div>
                <Button size="sm" iconLeft={<Plus size={13} />} onClick={() => setInstructionDraft(newInstruction())}>{t({ zh: "添加指令", en: "Add instruction" })}</Button>
              </div>

              <label className="research-instruction-activation">
                <span><strong>{t({ zh: "把个人指令应用到新会话", en: "Apply personal instructions to new Sessions" })}</strong><small>{t({ zh: "开着的话，下面启用的那些指令会在新会话建立时一起带进去。它不会改动项目知识库，也不影响已有会话。", en: "When enabled, active instructions below are included when a new Session starts. This does not change the Project KB or historical Sessions." })}</small></span>
                <input type="checkbox" checked={current.memory_enabled} onChange={(event) => setDraft({ ...current, memory_enabled: event.target.checked })} />
              </label>

              {current.instructions.length === 0 && !instructionDraft && (
                <div className="research-instruction-empty"><strong>{t({ zh: "还没有个人指令", en: "No personal instructions" })}</strong><span>{t({ zh: "只写你希望在每个新会话里都生效的那几条。", en: "Add only the guidance you want applied across new Sessions." })}</span></div>
              )}

              <div className="research-instruction-list">
                {current.instructions.map((item) => (
                  <div className="research-instruction-row" key={item.id}>
                    <button
                      type="button"
                      className={`research-instruction-state ${item.enabled ? "enabled" : ""}`}
                      aria-label={`${item.enabled ? "Disable" : "Enable"} ${item.title}`}
                      onClick={() => setDraft({ ...current, instructions: current.instructions.map((candidate) => candidate.id === item.id ? { ...candidate, enabled: !candidate.enabled } : candidate) })}
                    >{item.enabled && <Check size={10} />}</button>
                    <span><strong>{item.title}</strong><small>{item.instruction}</small></span>
                    <span>{t(SCOPE_LABELS[item.scope])}</span>
                    <div>
                      <button type="button" aria-label={`Edit ${item.title}`} onClick={() => setInstructionDraft({ ...item })}><Pencil size={12} /></button>
                      <button type="button" aria-label={`Delete ${item.title}`} onClick={() => setDraft({ ...current, instructions: current.instructions.filter((candidate) => candidate.id !== item.id) })}><Trash2 size={12} /></button>
                    </div>
                  </div>
                ))}
              </div>

              {instructionDraft && (
                <form className="research-instruction-form" onSubmit={(event) => { event.preventDefault(); applyInstruction(); }}>
                  <div className="research-instruction-form-heading"><span><strong>{current.instructions.some((item) => item.id === instructionDraft.id) ? t({ zh: "编辑指令", en: "Edit instruction" }) : t({ zh: "新建指令", en: "New instruction" })}</strong><small>{t({ zh: "写得具体、能被检验。", en: "Keep it concrete and testable." })}</small></span><button type="button" aria-label={t({ zh: "关闭指令编辑器", en: "Close instruction editor" })} onClick={() => setInstructionDraft(null)}><X size={14} /></button></div>
                  <div className="research-instruction-fields">
                    <label><span>{t({ zh: "名称", en: "Name" })}</span><input autoFocus required value={instructionDraft.title} onChange={(event) => setInstructionDraft({ ...instructionDraft, title: event.target.value })} placeholder={t({ zh: "例如：方法要可复现", en: "For example: Reproducible methods" })} /></label>
                    <label><span>{t({ zh: "适用范围", en: "Applies to" })}</span><select value={instructionDraft.scope} onChange={(event) => setInstructionDraft({ ...instructionDraft, scope: event.target.value as ResearchInstructionScope })}>{Object.entries(SCOPE_LABELS).map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}</select></label>
                    <label className="research-instruction-wide"><span>{t({ zh: "指令内容", en: "Instruction" })}</span><textarea required value={instructionDraft.instruction} onChange={(event) => setInstructionDraft({ ...instructionDraft, instruction: event.target.value })} placeholder={t({ zh: "方法要写到能复现的程度；缺哪个参数就明说缺哪个。", en: "Prefer methods with enough detail to reproduce; state missing parameters explicitly." })} /></label>
                  </div>
                  <div className="settings-form-actions"><Button type="button" size="sm" variant="ghost" onClick={() => setInstructionDraft(null)}>{t({ zh: "取消", en: "Cancel" })}</Button><Button type="submit" size="sm" variant="primary">{current.instructions.some((item) => item.id === instructionDraft.id) ? t({ zh: "更新指令", en: "Update instruction" }) : t({ zh: "添加指令", en: "Add instruction" })}</Button></div>
                </form>
              )}

              {dirty && <p className="research-unsaved-note">{t({ zh: "有未保存的改动 · 保存一次，之后新建的会话就按它来。", en: "Unsaved changes · save once to apply them to newly created Sessions." })}</p>}
              <p className="research-policy-note">{t({ zh: "数据分析、作图、算力、部署这几项，现在的设置契约里还没有各自独立的范围。这类要求请写在会话的请求里。", en: "Separate scopes for data analysis, figures, compute, and deployment are not available in the current settings contract. Specify those requirements in the Session request." })}</p>
            </section>

            <section className="settings-section">
              <div className="settings-section-heading">
                <div><span>{t({ zh: "权限边界", en: "Access boundary" })}</span><h2>{t({ zh: "实际生效的研究治理", en: "Effective research governance" })}</h2><p>{t({ zh: "这几层说明指令是从哪来的。机构和研究组那两层的 AGENTS 全文，这个接口不给。", en: "These layers explain where guidance comes from. Full institution and research-group AGENTS content is not exposed by this API." })}</p></div>
              </div>
              <div className="research-layer-list">
                {settings.data.effective_layers.map((layer) => (
                  <div className="research-layer-row" key={layer.kind}>
                    <i aria-hidden />
                    <span><strong>{layer.name}</strong><small>{layer.summary}</small></span>
                    <span>{layer.editable ? `${layer.instruction_count} personal instruction${layer.instruction_count === 1 ? "" : "s"}` : t({ zh: "访问策略", en: "Access policy" })}</span>
                    <span>{layer.editable ? t({ zh: "可在本页编辑", en: "Editable on this page" }) : t({ zh: "只读 · 由管理员维护", en: "Read only · managed by administrators" })}</span>
                  </div>
                ))}
              </div>
              <p className="research-policy-note">{t({ zh: "个人指令只影响模型的行为。权限和资源上限是在这一页之外被机械强制执行的。", en: "Personal instructions guide model behavior only. Permissions and resource limits are enforced mechanically outside this page." })}</p>
            </section>
          </div>
        </div>
      )}
    </div>
  );
}

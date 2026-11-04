"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Plus, Save, Trash2, X } from "lucide-react";
import {
  api,
  type EffectiveResearchLayer,
  type ResearchInstruction,
  type ResearchInstructionScope,
  type ResearchSettings,
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

/**
 * 「指令来源」每一层的名字按层级说。后端的 `name` 是存在用户行上的显示名，个人版里是
 * "Dev Researcher" 这类英文占位，中文界面上原样露出来；这一节要回答的是「指令从哪一层
 * 来」，层级名就够了。
 *
 * 助手读的指令只有两层：个人（这一页）和项目（项目设置里的 PROJECT.md）。以前这里还列着
 * 「机构」「研究组」两行，永远显示「未设置」、还说「由管理员维护」—— 那两层在 RFC X2 就
 * 没了，没有文件、没有写入口，助手什么也读不到。
 */
const LAYER_LABEL: Record<EffectiveResearchLayer["kind"], Phrase> = {
  personal: { zh: "个人", en: "Personal" },
};

/**
 * 「指令来源」每一层下面那句小字。后端也给一句 `summary`，但只有英文；这里按页面上
 * 已经有的数（分场景指令开没开、启用了几条）自己说。
 */
function layerSummary(saved: ResearchSettings): Phrase {
  if (!saved.memory_enabled) return { zh: "分场景指令已关闭", en: "Scenario instructions are off" };
  const total = saved.instructions.length;
  if (total === 0) return { zh: "还没有分场景指令", en: "No scenario instructions yet" };
  const enabled = saved.instructions.filter((item) => item.enabled).length;
  return {
    zh: `已启用 ${enabled} 条分场景指令，共 ${total} 条`,
    en: `${enabled} of ${total} scenario instructions enabled`,
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
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "保存失败", en: "Could not save research settings" })),
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
          <h1>{t({ zh: "个人指令", en: "Personal instructions" })}</h1>
          <p>{t({ zh: "告诉助手你的习惯和要求，之后新建的会话都会照着做。", en: "Tell the assistant how you like to work. New sessions will follow it." })}</p>
        </div>
        <Button
          size="sm"
          variant="primary"
          iconLeft={<Save size={13} />}
          disabled={!dirty}
          loading={save.isPending}
          onClick={() => save.mutate(current)}
        >{t({ zh: "保存", en: "Save" })}</Button>
      </header>

      {settings.isLoading && !settings.data && <div className="research-settings-loading"><Skeleton height={60} /><Skeleton height={160} /><Skeleton height={100} /></div>}
      {settings.isError && (
        <Empty title={t({ zh: "无法加载个人指令", en: "Couldn't load personal instructions" })} hint={t({ zh: "请稍后重试。", en: "Please try again later." })} />
      )}

      {settings.data && draft && (
        <div className="settings-layout">
          <div className="settings-content">
            <p className="research-effect-note">{t({ zh: "改动只对之后新建的会话生效，已有会话不变。", en: "Changes apply to sessions you create from now on. Existing sessions stay as they are." })}</p>

            <section className="settings-section">
              <div className="settings-section-heading"><div><h2>{t({ zh: "回答偏好", en: "Response preferences" })}</h2><p>{t({ zh: "对话里没有特别说明时，按这些设置回答。", en: "Used when the conversation does not say otherwise." })}</p></div></div>
              <div className="research-default-rows">
                <label><span><strong>{t({ zh: "回答语言", en: "Response language" })}</strong><small>{t({ zh: "对话里另有要求时，以对话为准。", en: "A request in the conversation takes precedence." })}</small></span><select value={current.response_language} onChange={(event) => setDraft({ ...current, response_language: event.target.value as typeof current.response_language })}><option value="auto">{t({ zh: "跟随提问语言", en: "Match the question" })}</option><option value="zh-CN">{t({ zh: "中文", en: "Chinese" })}</option><option value="en">{t({ zh: "英文", en: "English" })}</option></select></label>
                <label><span><strong>{t({ zh: "引用格式", en: "Citation style" })}</strong><small>{t({ zh: "生成参考文献时使用；期刊有要求时以期刊为准。", en: "Used for generated references. A journal's own requirements take precedence." })}</small></span><select value={current.citation_style} onChange={(event) => setDraft({ ...current, citation_style: event.target.value as typeof current.citation_style })}><option value="author_year">{t({ zh: "作者–年份", en: "Author–year" })}</option><option value="numeric">{t({ zh: "数字编号", en: "Numeric" })}</option><option value="apa">APA 7</option></select></label>
                <label><span><strong>{t({ zh: "证据标准", en: "Evidence standard" })}</strong><small>{t({ zh: "决定优先引用哪类来源，以及怎样说明不确定性。", en: "Which sources to prefer and how to report uncertainty." })}</small></span><select value={current.evidence_standard} onChange={(event) => setDraft({ ...current, evidence_standard: event.target.value as typeof current.evidence_standard })}><option value="balanced">{t({ zh: "均衡", en: "Balanced" })}</option><option value="strict">{t({ zh: "严格（优先一手来源）", en: "Strict (prefer primary sources)" })}</option><option value="exploratory">{t({ zh: "探索（覆盖面更广）", en: "Exploratory (broader coverage)" })}</option></select></label>
              </div>
            </section>

            {user && (
              <PersonalInstructionsEditor />
            )}

            <section className="settings-section">
              <div className="settings-section-heading">
                <div><h2>{t({ zh: "分场景指令", en: "Scenario instructions" })}</h2><p>{t({ zh: "只在某类工作里使用的简短要求，补充长期指令。", en: "Short requirements for particular kinds of work, on top of your standing instructions." })}</p></div>
                <Button size="sm" iconLeft={<Plus size={13} />} onClick={() => setInstructionDraft(newInstruction())}>{t({ zh: "添加指令", en: "Add instruction" })}</Button>
              </div>

              <label className="research-instruction-activation">
                <span><strong>{t({ zh: "启用分场景指令", en: "Use scenario instructions" })}</strong><small>{t({ zh: "关闭后，新会话不再使用下面的指令。", en: "When off, new sessions do not use the instructions below." })}</small></span>
                <input type="checkbox" checked={current.memory_enabled} onChange={(event) => setDraft({ ...current, memory_enabled: event.target.checked })} />
              </label>

              {current.instructions.length === 0 && !instructionDraft && (
                <div className="research-instruction-empty"><strong>{t({ zh: "还没有指令", en: "No instructions yet" })}</strong><span>{t({ zh: "点「添加指令」写下第一条。", en: "Click Add instruction to write your first one." })}</span></div>
              )}

              <div className="research-instruction-list">
                {current.instructions.map((item) => (
                  <div className="research-instruction-row" key={item.id}>
                    <button
                      type="button"
                      className={`research-instruction-state ${item.enabled ? "enabled" : ""}`}
                      aria-label={item.enabled ? t({ zh: "停用「{title}」", en: "Disable {title}" }, { title: item.title }) : t({ zh: "启用「{title}」", en: "Enable {title}" }, { title: item.title })}
                      onClick={() => setDraft({ ...current, instructions: current.instructions.map((candidate) => candidate.id === item.id ? { ...candidate, enabled: !candidate.enabled } : candidate) })}
                    >{item.enabled && <Check size={10} />}</button>
                    <span><strong>{item.title}</strong><small>{item.instruction}</small></span>
                    <span>{t(SCOPE_LABELS[item.scope])}</span>
                    <div>
                      <button type="button" aria-label={t({ zh: "编辑「{title}」", en: "Edit {title}" }, { title: item.title })} onClick={() => setInstructionDraft({ ...item })}><Pencil size={12} /></button>
                      <button type="button" aria-label={t({ zh: "删除「{title}」", en: "Delete {title}" }, { title: item.title })} onClick={() => setDraft({ ...current, instructions: current.instructions.filter((candidate) => candidate.id !== item.id) })}><Trash2 size={12} /></button>
                    </div>
                  </div>
                ))}
              </div>

              {instructionDraft && (
                <form className="research-instruction-form" onSubmit={(event) => { event.preventDefault(); applyInstruction(); }}>
                  <div className="research-instruction-form-heading"><span><strong>{current.instructions.some((item) => item.id === instructionDraft.id) ? t({ zh: "编辑指令", en: "Edit instruction" }) : t({ zh: "新建指令", en: "New instruction" })}</strong><small>{t({ zh: "越具体越好。", en: "The more specific, the better." })}</small></span><button type="button" aria-label={t({ zh: "关闭指令编辑器", en: "Close instruction editor" })} onClick={() => setInstructionDraft(null)}><X size={14} /></button></div>
                  <div className="research-instruction-fields">
                    <label><span>{t({ zh: "名称", en: "Name" })}</span><input autoFocus required value={instructionDraft.title} onChange={(event) => setInstructionDraft({ ...instructionDraft, title: event.target.value })} placeholder={t({ zh: "例如：方法要可复现", en: "For example: Reproducible methods" })} /></label>
                    <label><span>{t({ zh: "适用范围", en: "Applies to" })}</span><select value={instructionDraft.scope} onChange={(event) => setInstructionDraft({ ...instructionDraft, scope: event.target.value as ResearchInstructionScope })}>{Object.entries(SCOPE_LABELS).map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}</select></label>
                    <label className="research-instruction-wide"><span>{t({ zh: "指令内容", en: "Instruction" })}</span><textarea required value={instructionDraft.instruction} onChange={(event) => setInstructionDraft({ ...instructionDraft, instruction: event.target.value })} placeholder={t({ zh: "方法要写到能复现的程度；缺哪个参数就明说缺哪个。", en: "Prefer methods with enough detail to reproduce; state missing parameters explicitly." })} /></label>
                  </div>
                  <div className="settings-form-actions"><Button type="button" size="sm" variant="ghost" onClick={() => setInstructionDraft(null)}>{t({ zh: "取消", en: "Cancel" })}</Button><Button type="submit" size="sm" variant="primary">{current.instructions.some((item) => item.id === instructionDraft.id) ? t({ zh: "更新指令", en: "Update instruction" }) : t({ zh: "添加指令", en: "Add instruction" })}</Button></div>
                </form>
              )}

              {dirty && <p className="research-unsaved-note">{t({ zh: "有未保存的改动。", en: "Unsaved changes." })}</p>}
              <p className="research-policy-note">{t({ zh: "数据分析、作图、算力等方面的要求，请直接在对话里说明。", en: "For data analysis, figures, compute and the like, say what you need directly in the conversation." })}</p>
            </section>

            <section className="settings-section">
              <div className="settings-section-heading">
                <div><h2>{t({ zh: "指令来源", en: "Instruction sources" })}</h2><p>{t({ zh: "助手会同时参考下面两层指令。", en: "The assistant draws on both layers below." })}</p></div>
              </div>
              <div className="research-layer-list">
                {settings.data.effective_layers.map((layer) => (
                  <div className="research-layer-row" key={layer.kind}>
                    <i aria-hidden />
                    <span><strong>{t(LAYER_LABEL[layer.kind])}</strong><small>{t(layerSummary(settings.data))}</small></span>
                    <span>{t({ zh: "你自己设定", en: "Set by you" })}</span>
                    <span>{t({ zh: "在本页编辑", en: "Edit on this page" })}</span>
                  </div>
                ))}
                {/* 项目层每个项目一份，不在这一页：在项目设置的「项目指令」里改。 */}
                <div className="research-layer-row">
                  <i aria-hidden />
                  <span><strong>{t({ zh: "项目", en: "Project" })}</strong><small>{t({ zh: "每个项目单独设置", en: "Set separately for each project" })}</small></span>
                  <span>{t({ zh: "项目负责人设定", en: "Set by the project lead" })}</span>
                  <span>{t({ zh: "在「项目设置」中编辑", en: "Edit in Project settings" })}</span>
                </div>
              </div>
            </section>
          </div>
        </div>
      )}
    </div>
  );
}

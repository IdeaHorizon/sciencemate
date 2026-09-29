"use client";

/**
 * 会话输入框上方的控制条。
 *
 * 改造前这里是三段截断的文字挤在一行：模型名、访问状态、以及一句很长的
 * "Research is still running. You can leave this page..."。信息密度看着不低，
 * 但**没有一样是能点的**，而且长句子把另外两段挤成了省略号——读者既看不全，
 * 也做不了任何事。
 *
 * 重做的原则：**这一行上的每一项都要么是能操作的控件，要么是一眼能读懂的
 * 状态**。凡是"看起来像开关、点了没反应"的都不要——那和平台里那些
 * "机制存在但没接到路径"的假开关是同一类错误，只不过发生在界面上。
 *
 * 所以每个 chip 背后都对应一个真实存在的后端能力：
 *   模式  → PATCH /projects/{id}/config     （operation_mode 真能改）
 *   模型  → PATCH …/sessions/{id}（model_backend_id，随时可换）
 *   分支  → session.gitBranch + 未发布改动数（Git 工作区是真相源）
 *   添加  → POST …/sessions/{id}/files（落进工作区 sources/，agent 直接读）
 *   上下文 → harness 每次 LLM 响应后自报的 `context.updated`（只读状态：占了多少、
 *            离自动压缩线多远；没有报告就不摆这个 chip）
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Bot, Check, GitBranch, Loader2, Paperclip, Zap } from "lucide-react";

import { pushError } from "@/stores/notification";
import { useT, type Phrase } from "@/shared/i18n";
import type { ContextWindowState } from "../lib/context-window";
import { contextWindowTone } from "../lib/context-window";
import { ContextWindowChipLabel, ContextWindowPanel, contextWindowChipTitle } from "./ContextWindowChip";

/**
 * 界面上的三档。存储层只有两态（`assisted` / `autonomous`）——「连续」是
 * `autonomous` + 预授权全部高危类别。
 *
 * 为什么不在后端加第三个枚举值：全代码库有一批 `operation_mode ==
 * AUTONOMOUS` 的判断，新增一个值就等于给每一处悄悄加了 `if continuous:
 * skip`。漏掉哪一处都不报错，只是那条防线/那个能力在新模式下不存在。
 * 所以这一档只活在 UI 的表达层，落库时翻译成既有的两个概念。
 */
export type OperationMode = "assisted" | "autonomous" | "continuous";

const MODE_LABEL: Record<OperationMode, Phrase> = {
  assisted: { zh: "协作", en: "Assisted" },
  autonomous: { zh: "自主", en: "Autonomous" },
  continuous: { zh: "连续", en: "Continuous" },
};

const MODE_HINT: Record<OperationMode, Phrase> = {
  assisted: { zh: "每个决策点都停下来问你", en: "Stops and asks you at every decision point" },
  // 「高风险处」具体指哪些，取决于预授权范围；一个没授权任何类别的自主模式
  // 会停在第一个真实作业提交上，所以这句话要说全，别让开关自吹自擂。
  autonomous: { zh: "自己往下推，未授权的高风险处仍会停下问你", en: "Pushes ahead on its own; still stops at high-risk points you have not pre-authorised" },
  continuous: { zh: "预授权全部高风险操作，一路跑到底、不停", en: "All high-risk actions pre-authorised; runs straight through without stopping" },
};

export type ComposerModelOption = {
  id: string;
  label: string;
  /** 模型名 + 地址那类一眼能认出"这是哪一路"的信息 */
  detail: string;
  ready: boolean;
};

export type ComposerRunState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "answering" }
  | { kind: "readonly"; reason: string }
  | { kind: "unavailable"; reason: string };

/**
 * 会话分支名是 `session/<uuid>`，剥掉前缀之后剩下的就是 36 位 UUID —— 展示层
 * 没有理由出现它。这个组件自己的原则是「每一项要么能操作、要么一眼能读懂」，
 * 一串 UUID 两头都不占。
 *
 * 取末段 8 位做短号：足够在一个项目里区分开，也足够短到不挤掉旁边的 chip。
 * 完整分支名在 title 里（调用处已经写了），要复制的人拿得到。
 */
/** 只给这一行 chip 用的可读体积。数字要给人看，别给字节数。 */
function formatBytes(size: number) {
  if (size >= 1024 ** 3) return `${(size / 1024 ** 3).toFixed(1)} GB`;
  if (size >= 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(size / 1024))} KB`;
}

function branchChipLabel(branch: string) {
  const withoutPrefix = branch.replace(/^session\//, "");
  const uuidish = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuidish.test(withoutPrefix)) return withoutPrefix;
  return `#${withoutPrefix.slice(-8)}`;
}

/**
 * 状态徽标 —— **只在有话可说的时候出现**。
 *
 * 原来空闲时它显示「就绪」（wangd 2026-08-20：「那个就绪是啥意思啊？什么
 * 信息都没有啊？」）。确实没有：能不能打字，输入框自己就说明了；一个恒亮
 * 的"就绪"只是在控制条上占一格。
 *
 * 有信息的是另外几种：在跑（可以离开本页）、等你回答、只读、状态未知。
 */
function StateDot({ state }: { state: ComposerRunState }) {
  const t = useT();
  if (state.kind === "idle") return null;
  const label =
    state.kind === "running" ? t({ zh: "运行中", en: "Running" })
    : state.kind === "answering" ? t({ zh: "等你回答", en: "Needs your input" })
    : state.kind === "readonly" ? t({ zh: "只读", en: "Read only" })
    : t({ zh: "状态未知", en: "Status unknown" });
  const title =
    state.kind === "running" ? t({ zh: "这个会话在后台继续跑，你可以离开本页，不会中断它；也可以在下面直接插话", en: "This session keeps running in the background. You can leave this page without interrupting it, or interject below" })
    : state.kind === "answering" ? t({ zh: "上面有个问题在等你回答", en: "There is a question above waiting for your answer" })
    : state.reason;
  return (
    <span className={`composer-chip composer-chip--state is-${state.kind}`} title={title}>
      <i aria-hidden />
      {label}
    </span>
  );
}

function Popover({
  open,
  onClose,
  children,
}: {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="composer-popover" ref={ref} role="dialog">
      {children}
    </div>
  );
}

export function SessionComposerBar({
  mode,
  onModeChange,
  modeBusy,
  canChangeMode,
  modelLabel,
  modelId,
  models = [],
  onModelChange,
  modelBusy,
  branch,
  unpublishedCount,
  onOpenChanges,
  onAddFile,
  addingFile,
  canAddFile,
  maxFileBytes,
  state,
  contextWindow = null,
}: {
  mode: OperationMode | null;
  onModeChange: (next: OperationMode) => void;
  modeBusy?: boolean;
  canChangeMode: boolean;
  modelLabel: string | null;
  modelId?: string | null;
  models?: readonly ComposerModelOption[];
  onModelChange?: (backendId: string) => void;
  modelBusy?: boolean;
  branch: string | null;
  unpublishedCount: number;
  onOpenChanges?: () => void;
  onAddFile?: (file: File) => void;
  addingFile?: boolean;
  canAddFile: boolean;
  /** 单份文件上限，由后端给（session.materialMaxBytes）。 */
  maxFileBytes?: number;
  state: ComposerRunState;
  /** 调度器上一次请求的窗口占用；null = 还没发过请求，chip 不画。 */
  contextWindow?: ContextWindowState | null;
}) {
  const t = useT();
  const [openMenu, setOpenMenu] = useState<"mode" | "model" | "context" | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  return (
    <div className="composer-bar">
      {/* 模式 —— 真能改，改的是项目级配置，所以标注出来，别让人以为只影响本会话 */}
      <div className="composer-chip-wrap">
        <button
          type="button"
          className={`composer-chip composer-chip--mode${mode === "autonomous" || mode === "continuous" ? " is-on" : ""}`}
          onClick={() => setOpenMenu(openMenu === "mode" ? null : "mode")}
          disabled={!canChangeMode || modeBusy}
          title={mode ? t(MODE_HINT[mode]) : t({ zh: "加载中", en: "Loading" })}
        >
          {modeBusy ? <Loader2 size={13} className="spin" /> : <Zap size={13} />}
          {mode ? t(MODE_LABEL[mode]) : "…"}
        </button>
        <Popover open={openMenu === "mode"} onClose={() => setOpenMenu(null)}>
          <p className="composer-popover-title">{t({ zh: "运行模式", en: "Operation mode" })}</p>
          {(["assisted", "autonomous", "continuous"] as OperationMode[]).map((value) => (
            <button
              key={value}
              type="button"
              className="composer-popover-item"
              onClick={() => {
                setOpenMenu(null);
                if (value !== mode) onModeChange(value);
              }}
            >
              <span>
                <strong>{t(MODE_LABEL[value])}</strong>
                <small>{t(MODE_HINT[value])}</small>
              </span>
              {value === mode && <Check size={14} />}
            </button>
          ))}
          <p className="composer-popover-foot">{t({ zh: "这是", en: "This is" })}<strong>{t({ zh: "项目级", en: "Project level" })}</strong>{t({
            zh: "设置，会影响该项目的所有会话。自主模式下高风险操作（外部作业提交、批量写入）仍然会停下来问你。",
            en: " setting: it applies to every session in this project. In autonomous mode, high-risk actions (submitting external jobs, bulk writes) still stop and ask you.",
          })}
          </p>
        </Popover>
      </div>

      {/* 模型 —— 随时可换。归因靠每条 run 记下自己实际用的那个，不靠锁死会话 */}
      <div className="composer-chip-wrap">
        <button
          type="button"
          className="composer-chip"
          onClick={() => setOpenMenu(openMenu === "model" ? null : "model")}
          disabled={modelBusy}
          title={modelLabel ?? ""}
        >
          {modelBusy ? <Loader2 size={13} className="spin" /> : <Bot size={13} />}
          <span className="composer-chip-text">{modelLabel ?? t({ zh: "未指定模型", en: "No model specified" })}</span>
        </button>
        <Popover open={openMenu === "model"} onClose={() => setOpenMenu(null)}>
          <p className="composer-popover-title">{t({ zh: "本会话使用的模型", en: "Model used by this session" })}</p>
          {models.length === 0 && (
            <p className="composer-popover-foot">{t({ zh: "还没有可用的模型后端。", en: "No model connection is available yet." })}</p>
          )}
          {models.map((item) => (
            <button
              key={item.id}
              type="button"
              className="composer-popover-item"
              disabled={!item.ready}
              onClick={() => {
                setOpenMenu(null);
                if (item.id !== modelId) onModelChange?.(item.id);
              }}
            >
              <span>
                <strong>{item.label}</strong>
                <small>{item.ready ? item.detail : t({ zh: `${item.detail} · 不可用`, en: `${item.detail} · unavailable` })}</small>
              </span>
              {item.id === modelId && <Check size={14} />}
            </button>
          ))}
          <p className="composer-popover-foot">{t({ zh: "换了之后", en: "After switching" })}<strong>{t({ zh: "只影响后面的轮次", en: "Applies to later turns only" })}</strong>{t({
            zh: "，已经跑完的不会重跑。每条 run 都记着它当时实际用的模型，要追溯结论是谁产出的看 run。",
            en: "; work already finished is not re-run. Every run records the model it actually used, so trace a conclusion back through its run.",
          })}
          </p>
          <Link className="composer-popover-link" href="/settings/models">{t({ zh: "管理模型后端 →", en: "Manage model connections →" })}</Link>
        </Popover>
      </div>

      {/* 上下文 —— 只读状态：占了多少、离自动压缩线多远。数是 harness 自报的，
          这里只画；没有报告（会话还没发过请求）就不摆。 */}
      {contextWindow && (
        <div className="composer-chip-wrap">
          <button
            type="button"
            className={`composer-chip composer-chip--context is-${contextWindowTone(contextWindow)}`}
            onClick={() => setOpenMenu(openMenu === "context" ? null : "context")}
            title={contextWindowChipTitle(contextWindow, t)}
            aria-label={contextWindowChipTitle(contextWindow, t)}
          >
            <ContextWindowChipLabel state={contextWindow} />
          </button>
          <Popover open={openMenu === "context"} onClose={() => setOpenMenu(null)}>
            <ContextWindowPanel state={contextWindow} />
          </Popover>
        </div>
      )}

      {/* 分支 —— Git 工作区是真相源，把它摆出来 */}
      {branch && (
        <button
          type="button"
          className="composer-chip"
          onClick={onOpenChanges}
          title={`${t({ zh: "会话分支", en: "Session branch" })} ${branch} · ${unpublishedCount
            ? t({ zh: `${unpublishedCount} 处改动未发布`, en: `${unpublishedCount} unpublished changes` })
            : t({ zh: "无未发布改动", en: "nothing unpublished" })}`}
        >
          <GitBranch size={13} />
          <span className="composer-chip-text">{branchChipLabel(branch)}</span>
          {unpublishedCount > 0 && <em className="composer-chip-badge">{unpublishedCount}</em>}
        </button>
      )}

      {/* 添加文件 —— 平台唯一的上传入口 */}
      {canAddFile && onAddFile && (
        <>
          <button
            type="button"
            className="composer-chip"
            onClick={() => fileRef.current?.click()}
            disabled={addingFile}
            title={
              maxFileBytes
                ? t({ zh: `把文件交进这个会话的工作区，agent 直接按路径读（单份上限 ${formatBytes(maxFileBytes)}）`, en: `Hand a file to this session's workspace; the agent reads it straight from the path (limit ${formatBytes(maxFileBytes)} per file)` })
                : t({ zh: "把文件交进这个会话的工作区，agent 直接按路径读", en: "Hand a file to this session's workspace; the agent reads it straight from the path" })
            }
          >
            {addingFile ? <Loader2 size={13} className="spin" /> : <Paperclip size={13} />}
            {addingFile ? t({ zh: "上传中…", en: "Uploading…" }) : t({ zh: "添加文件", en: "Add a file" })}
          </button>
          <input
            ref={fileRef}
            type="file"
            hidden
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (!file) return;
              // 选完就地判上限：让用户在**开始传**之前知道不行，而不是把
              // 一个 3 GiB 的包传完再收到 413。上限是后端给的那个数，这里
              // 不写常量 —— 写了它就会跟后端分叉，而且没人会发现。
              if (maxFileBytes && file.size > maxFileBytes) {
                pushError(
                  t({
                    zh: `「${file.name}」${formatBytes(file.size)}，超过单份上限 ${formatBytes(maxFileBytes)}。更大的数据集别走上传：让 data 节点在计算侧就地取用，或让部署侧给这个项目挂一个数据集绑定。`,
                    en: `"${file.name}" is ${formatBytes(file.size)}, over the ${formatBytes(maxFileBytes)} per-file limit. `
                      + "Do not upload larger datasets: have the data node read them in place on the compute side, or have the deployment mount a dataset binding for this project.",
                  }),
                );
                return;
              }
              onAddFile(file);
            }}
          />
        </>
      )}

      <StateDot state={state} />
    </div>
  );
}

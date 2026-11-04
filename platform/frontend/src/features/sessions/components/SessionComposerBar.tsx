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
 *   执行位置 → PATCH /projects/{id}/config（execution_target：本机 / WSL 发行版；
 *            只在这台机器上真有第二个选项时才摆，见 RFC_WINDOWS_EXECUTION_TARGETS）
 *   分支  → session.gitBranch + 未发布改动数（Git 工作区是真相源）
 *   添加  → POST …/sessions/{id}/files（落进工作区 sources/，agent 直接读）
 *   上下文 → harness 每次 LLM 响应后自报的 `context.updated`（只读状态：占了多少、
 *            离自动压缩线多远；没有报告就不摆这个 chip）
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Bot, Check, GitBranch, Loader2, Paperclip, Server, Zap } from "lucide-react";

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
  assisted: { zh: "已授权工作继续，需要你的判断时再问", en: "Continues authorized work and asks when your judgment is needed" },
  // 「高风险处」具体指哪些，取决于预授权范围；一个没授权任何类别的自主模式
  // 会停在第一个真实作业提交上，所以这句话要说全，别让开关自吹自擂。
  autonomous: { zh: "自动推进，遇到未授权的高风险操作或必须由你回答的问题时停下", en: "Proceeds on its own; stops for high-risk actions you haven't pre-authorised, or questions only you can answer" },
  // 「连续」也不是一路不停：只能由人回答的问题照样停（core/pause_driver.py 的
  // must_ask_a_person），说明里要写出来。
  continuous: { zh: "预先授权所有高风险操作，只在必须由你回答的问题上停下", en: "All high-risk actions pre-authorised; stops only for questions only you can answer" },
};

export type ComposerModelOption = {
  id: string;
  label: string;
  /** 模型名 + 地址那类一眼能认出"这是哪一路"的信息 */
  detail: string;
  ready: boolean;
};

export type ComposerTargetOption = {
  /** `local` / `wsl:<发行版>` —— 原样存进项目配置 */
  value: string;
  kind: "local" | "wsl" | "ssh";
  name: string;
};

export type ComposerRunState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "answering" }
  | { kind: "readonly"; reason: string }
  | { kind: "unavailable"; reason: string };

/** 只给这一行 chip 用的可读体积。数字要给人看，别给字节数。 */
function formatBytes(size: number) {
  if (size >= 1024 ** 3) return `${(size / 1024 ** 3).toFixed(1)} GB`;
  if (size >= 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(size / 1024))} KB`;
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
    state.kind === "running" ? t({ zh: "会话在后台运行，离开本页不会中断。你也可以在下方继续发消息。", en: "This session keeps running in the background; leaving this page won't stop it. You can also send messages below." })
    : state.kind === "answering" ? t({ zh: "请回答上方的问题", en: "Please answer the question above" })
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
  target = null,
  targets = [],
  onTargetChange,
  targetBusy,
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
  /** 项目当前的执行位置（`local` / `wsl:<发行版>`）。 */
  target?: string | null;
  /** 这台机器上可选的执行位置。少于两个就不摆 chip —— 没得选的开关只是占地方。 */
  targets?: readonly ComposerTargetOption[];
  onTargetChange?: (value: string) => void;
  targetBusy?: boolean;
}) {
  const t = useT();
  const [openMenu, setOpenMenu] = useState<"mode" | "model" | "target" | "context" | null>(null);
  const targetLabel = (option: { kind: string; name: string } | undefined) =>
    !option || option.kind === "local"
      ? t({ zh: "本机", en: "This computer" })
      : t({ zh: "WSL · {name}", en: "WSL · {name}" }, { name: option.name });
  const currentTarget = targets.find((option) => option.value === (target ?? "local"));
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
          title={mode ? t(MODE_HINT[mode]) : t({ zh: "正在加载…", en: "Loading…" })}
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
          {/* 一整句一个 Phrase：拆成三段拼，英文会丢掉中间的空格。 */}
          <p className="composer-popover-foot">{t({
            zh: "这是项目级设置，会影响该项目的所有会话。",
            en: "This is a project-level setting and applies to every session in this project.",
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
            <p className="composer-popover-foot">{t({ zh: "还没有可用的模型。", en: "No model is available yet." })}</p>
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
          <p className="composer-popover-foot">{t({ zh: "切换后只对之后的执行生效。", en: "After switching, it applies to later runs only." })}</p>
          <Link className="composer-popover-link" href="/settings/models">{t({ zh: "管理模型连接 →", en: "Manage model connections →" })}</Link>
        </Popover>
      </div>

      {/* 执行位置 —— 命令在本机跑还是在某个 WSL 发行版里跑。项目级设置，与模式一样标注出来。 */}
      {targets.length > 1 && onTargetChange && (
        <div className="composer-chip-wrap">
          <button
            type="button"
            className={`composer-chip${currentTarget && currentTarget.kind !== "local" ? " is-on" : ""}`}
            onClick={() => setOpenMenu(openMenu === "target" ? null : "target")}
            disabled={!canChangeMode || targetBusy}
            title={t({ zh: "模型命令在哪里跑", en: "Where the assistant's commands run" })}
          >
            {targetBusy ? <Loader2 size={13} className="spin" /> : <Server size={13} />}
            <span className="composer-chip-text">{targetLabel(currentTarget)}</span>
          </button>
          <Popover open={openMenu === "target"} onClose={() => setOpenMenu(null)}>
            <p className="composer-popover-title">{t({ zh: "命令在哪里跑", en: "Where commands run" })}</p>
            {targets.map((option) => (
              <button
                key={option.value}
                type="button"
                className="composer-popover-item"
                onClick={() => {
                  setOpenMenu(null);
                  if (option.value !== (target ?? "local")) onTargetChange(option.value);
                }}
              >
                <span>
                  <strong>{targetLabel(option)}</strong>
                  <small>{option.kind === "local"
                    ? t({ zh: "在这台 Windows 上跑", en: "Runs on this Windows computer" })
                    : t({ zh: "在这个 Linux 发行版里跑：用它自己的 bash、python3 和工具链", en: "Runs inside this Linux distro, with its own bash, python3 and toolchains" })}
                  </small>
                </span>
                {option.value === (target ?? "local") && <Check size={14} />}
              </button>
            ))}
            <p className="composer-popover-foot">{t({
              zh: "这是项目级设置，对之后的执行生效。WSL 发行版里需要装 bubblewrap（sudo apt install bubblewrap），没装时命令会如实报错，不会改在本机跑。",
              en: "This is a project-level setting and applies to later runs. A WSL distro needs bubblewrap installed (sudo apt install bubblewrap); without it commands fail with that reason instead of running on this computer.",
            })}
            </p>
          </Popover>
        </div>
      )}

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

      {/* 改动 —— 点开是这个会话的改动面板。分支名对读者没有意义，只留在 title 里。 */}
      {branch && (
        <button
          type="button"
          className="composer-chip"
          onClick={onOpenChanges}
          title={`${t({ zh: "会话分支", en: "Session branch" })} ${branch} · ${unpublishedCount
            ? t({ zh: "{count} 处改动未发布", en: "{count} unpublished change{s}" }, { count: unpublishedCount, s: unpublishedCount === 1 ? "" : "s" })
            : t({ zh: "没有未发布的改动", en: "No unpublished changes" })}`}
        >
          <GitBranch size={13} />
          <span className="composer-chip-text">{t({ zh: "改动", en: "Changes" })}</span>
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
                ? t({ zh: "上传文件到这个会话的工作区，助手可以直接读取（单个文件不超过 {limit}）", en: "Upload a file to this session's workspace for the assistant to read (up to {limit} per file)" }, { limit: formatBytes(maxFileBytes) })
                : t({ zh: "上传文件到这个会话的工作区，助手可以直接读取", en: "Upload a file to this session's workspace for the assistant to read" })
            }
          >
            {addingFile ? <Loader2 size={13} className="spin" /> : <Paperclip size={13} />}
            {addingFile ? t({ zh: "正在上传…", en: "Uploading…" }) : t({ zh: "添加文件", en: "Add a file" })}
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
                  // 下一步只写算力端这一条：「请管理员挂载数据目录」对应的是部署配置
                  // （sandbox_mount_bindings），界面上没有入口，个人版用户也无从找管理员。
                  t({
                    zh: "「{name}」大小为 {size}，超过单个文件的上限 {limit}。更大的数据请放在算力端，由助手直接读取。",
                    en: "\"{name}\" is {size}, over the {limit} per-file limit. Keep larger data on the compute side for the assistant to read in place.",
                  }, { name: file.name, size: formatBytes(file.size), limit: formatBytes(maxFileBytes) }),
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

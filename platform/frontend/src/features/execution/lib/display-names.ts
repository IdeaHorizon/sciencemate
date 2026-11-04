import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

/**
 * 机器值 → 给人看的名字：节点、执行事件类型、工具参数名。
 *
 * 这些值是后端和事件流里的标识符（`hypothesis`、`tool.started`、`max_results`），
 * 原样显示在界面上，中文界面就是一串英文代号。表里没有的**照原样显示，不猜** ——
 * 新加一个节点或事件类型时，界面上先露出代号，比被猜成一个错的名字好。
 *
 * 成果类型不在这里：那张表在 `features/catalog/lib/kind-label.ts`（它和后端
 * `artifact_policy.py` 对账）。
 */

/**
 * 节点 id → 显示名。
 *
 * 名单取自仓库根目录 `nodes/` 下的目录名：后端按目录发现节点（`core/loader.py`
 * 的 NODES_DIR），所以这就是全部会出现在事件流里的 node_type。`research` 不是
 * 节点，是前端在事件里找不到 node_type 时的兜底（research-map、聊天区节点卡）。
 */
export const NODE_DISPLAY_NAMES: Record<string, Phrase> = {
  literature: { zh: "文献调研", en: "Literature review" },
  // hypothesis 节点承担 Analysis 角色：提假设、出研究协议与实验设计、判定实验证据。
  hypothesis: { zh: "假设与分析", en: "Hypothesis & analysis" },
  data: { zh: "数据准备", en: "Data preparation" },
  experiment: { zh: "实验", en: "Experiment" },
  observation: { zh: "观察研究", en: "Observation" },
  derivation: { zh: "演绎推导", en: "Derivation" },
  // 目录名沿用 postprocess，职责是科学可视化（nodes/postprocess/README.md）。
  postprocess: { zh: "科学可视化", en: "Visualization" },
  writing: { zh: "写作", en: "Writing" },
  _curator: { zh: "知识整理", en: "Knowledge curation" },
  _orchestrator: { zh: "助手", en: "Assistant" },
  _reviewer: { zh: "审稿", en: "Review" },
  research: { zh: "研究", en: "Research" },
  // 项目文件树的 owner（后端 project_repository.py 的 `_owner_of`）除了节点 id，
  // 还会给出这几个不带下划线的工作区主人。
  reviewer: { zh: "审稿", en: "Review" },
  orchestrator: { zh: "助手", en: "Assistant" },
  memory_curator: { zh: "项目记忆", en: "Project memory" },
  platform: { zh: "平台", en: "Platform" },
};

export function nodeDisplayName(nodeType: string | null | undefined, lang: Language): string {
  if (!nodeType) return say(NODE_DISPLAY_NAMES.research, lang);
  const label = NODE_DISPLAY_NAMES[nodeType];
  return label ? say(label, lang) : nodeType;
}

/** 执行事件类型 → 一句短的说法。名单与 `execution-event.ts` 的 EXECUTION_EVENT_KINDS 对齐。 */
export const EVENT_KIND_LABELS: Record<string, Phrase> = {
  "session.started": { zh: "会话开始", en: "Session started" },
  "session.message": { zh: "消息", en: "Message" },
  "agent.message": { zh: "助手说明", en: "Assistant note" },
  "orchestrator.said": { zh: "助手回复", en: "Assistant reply" },
  "interrupt.acknowledged": { zh: "已读到消息", en: "Message read" },
  "session.completed": { zh: "会话结束", en: "Session completed" },
  "step.started": { zh: "步骤开始", en: "Step started" },
  "step.progress": { zh: "步骤进展", en: "Step progress" },
  "step.completed": { zh: "步骤完成", en: "Step completed" },
  "step.failed": { zh: "步骤失败", en: "Step failed" },
  "tool.started": { zh: "开始使用工具", en: "Tool started" },
  "tool.progress": { zh: "工具进展", en: "Tool progress" },
  "tool.retrying": { zh: "工具重试", en: "Tool retrying" },
  "tool.long_running": { zh: "工具运行较久", en: "Tool running long" },
  "tool.completed": { zh: "工具完成", en: "Tool completed" },
  "tool.failed": { zh: "工具失败", en: "Tool failed" },
  "task.created": { zh: "任务创建", en: "Task created" },
  "task.started": { zh: "任务开始", en: "Task started" },
  "task.updated": { zh: "任务更新", en: "Task updated" },
  "task.completed": { zh: "任务完成", en: "Task completed" },
  "task.blocked": { zh: "任务受阻", en: "Task blocked" },
  "artifact.created": { zh: "生成成果", en: "Output created" },
  "artifact.versioned": { zh: "成果新版本", en: "Output versioned" },
  "artifact.frozen": { zh: "成果定稿", en: "Output finalized" },
  "workspace.changed": { zh: "文件改动", en: "Files changed" },
  "review.completed": { zh: "审稿完成", en: "Review completed" },
  "knowledge.updated": { zh: "知识库更新", en: "Knowledge base updated" },
  "knowledge.conflict_detected": { zh: "知识库发现冲突", en: "Knowledge conflict found" },
  "decision.required": { zh: "需要你做决定", en: "Decision needed" },
  "decision.resolved": { zh: "已做决定", en: "Decision made" },
  "permission.required": { zh: "需要授权", en: "Permission needed" },
  "permission.resolved": { zh: "已授权", en: "Permission resolved" },
  "run.queued": { zh: "执行排队", en: "Run queued" },
  "run.started": { zh: "执行开始", en: "Run started" },
  "run.paused": { zh: "执行暂停", en: "Run paused" },
  "run.resumed": { zh: "执行继续", en: "Run resumed" },
  "run.injected": { zh: "收到消息", en: "Message received" },
  "run.cancelled": { zh: "执行取消", en: "Run cancelled" },
  "run.retrying": { zh: "执行重试", en: "Run retrying" },
  "run.recovering": { zh: "执行恢复中", en: "Run recovering" },
  "run.blocked": { zh: "执行受阻", en: "Run blocked" },
  "run.parked": { zh: "执行等待中", en: "Run waiting" },
  "interject.queued": { zh: "消息排队", en: "Message queued" },
  "run.completed": { zh: "执行完成", en: "Run completed" },
  "run.incomplete": { zh: "执行未完成", en: "Run incomplete" },
  "run.failed": { zh: "执行失败", en: "Run failed" },
  "run.status_unknown": { zh: "执行状态未知", en: "Run status unknown" },
  "usage.updated": { zh: "用量更新", en: "Usage updated" },
  "context.updated": { zh: "上下文更新", en: "Context updated" },
  "budget.warning": { zh: "预算提醒", en: "Budget warning" },
  "autonomy.downgraded": { zh: "运行模式降级", en: "Mode downgraded" },
  "autonomy.weak_walls": { zh: "隔离较弱", en: "Weak isolation" },
  "material.added": { zh: "添加文件", en: "File added" },
  "run.rejoined": { zh: "重新连接执行", en: "Run rejoined" },
  "session.recovered": { zh: "接续上一个会话", en: "Continued from a previous Session" },
  "redaction.warning": { zh: "敏感信息提醒", en: "Redaction warning" },
  "record.rejected": { zh: "记录未保存", en: "Record rejected" },
  "connection.lost": { zh: "连接断开", en: "Connection lost" },
  "connection.recovered": { zh: "连接恢复", en: "Connection recovered" },
};

export function eventKindLabel(kind: string, lang: Language): string {
  const label = EVENT_KIND_LABELS[kind];
  return label ? say(label, lang) : kind;
}

/**
 * 工具参数名 → 显示名。只列常用的几个；其余参数在预览里只显示值，不露参数名。
 */
export const TOOL_ARGUMENT_LABELS: Record<string, Phrase> = {
  query: { zh: "检索词", en: "Query" },
  title: { zh: "标题", en: "Title" },
  name: { zh: "名称", en: "Name" },
  path: { zh: "路径", en: "Path" },
  url: { zh: "链接", en: "URL" },
  topic: { zh: "主题", en: "Topic" },
};

export function toolArgumentLabel(name: string, lang: Language): string | null {
  const label = TOOL_ARGUMENT_LABELS[name];
  return label ? say(label, lang) : null;
}

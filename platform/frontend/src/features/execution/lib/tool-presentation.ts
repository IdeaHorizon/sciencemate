import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";
import { toolArgumentLabel } from "./display-names.ts";

const INTERNAL_TOOL_PATTERNS = [
  /(^|_)scratchpad($|_)/,
  /^platform_/,
  /^hook_injection$/,
  /^resolve_research_context$/,
  /^run_node$/,
  /^request_human_input$/,
  /^present_decision_package$/,
];
const OBJECT_KEYS = [
  "query",
  "name",
  "title",
  "artifact_name",
  "filename",
  "source",
  "url",
  "artifact_id",
  "node_type",
  "path",
  "topic",
  "claim_text",
] as const;

const PRIVATE_OR_BULK_ARGUMENT = /(^|_)(content|code|script|body|payload|json|schema|config|headers?|tokens?|secret|password|prompt|messages?|classification|papers_json)($|_)/i;

export type ResearchActivityKind =
  | "research"
  | "figure"
  | "pdf"
  | "experiment"
  | "compute"
  | "deployment"
  | "general";

/**
 * 工具失败时的「下一步」，按工具类别给一句兜底（上游自己写了下一步时用上游的）。
 */
const RECOVERY: Record<ResearchActivityKind, Phrase> = {
  research: { zh: "调整检索范围或资料访问权限后，让助手继续。", en: "Adjust the scope or source access, then ask the assistant to continue." },
  figure: { zh: "检查原始数据和作图要求后，让助手重新生成。", en: "Check the source data and figure request, then ask the assistant to create it again." },
  pdf: { zh: "检查文档源文件后，让助手重新生成 PDF。", en: "Review the document source, then ask the assistant to rebuild the PDF." },
  experiment: { zh: "检查实验输入和可用资源后，重新运行。", en: "Review the experiment inputs and available resources before running it again." },
  compute: { zh: "检查输入数据和算力是否可用，然后重新运行。", en: "Review the inputs and compute availability before running it again." },
  deployment: { zh: "检查部署目标和密钥后，让助手重新部署。", en: "Review the target and API keys, then ask the assistant to deploy again." },
  general: { zh: "检查请求内容后，让助手重试。", en: "Review the request, then ask the assistant to try again." },
};

function humanize(value: string) {
  const words = value.replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "";
}

function clip(value: string, limit = 92) {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length > limit ? `${compact.slice(0, limit - 1).trimEnd()}…` : compact;
}

function objectFrom(argumentsValue?: Record<string, unknown>, fallback?: string) {
  for (const key of OBJECT_KEYS) {
    const value = argumentsValue?.[key];
    if (typeof value === "string" && value.trim()) return clip(value);
    if (typeof value === "number") return String(value);
  }
  const trimmed = fallback?.trim();
  if (!trimmed || /\{|\}|\[|\]|_[a-z]|json|traceback|exception/i.test(trimmed)) return undefined;
  return clip(trimmed);
}

/**
 * 工具名 → 人读得懂的动作。
 *
 * 这里原本是一张英文动词表加一个 `runningAction()`：把 "Search" 前缀换成
 * "Searching"。那是**英语的形态学**，中文没有对应的东西可换 —— 所以两种
 * 说法各写一份，而不是从一份推另一份。
 */
type ToolAction = { action: Phrase; running: Phrase };

const TOOL_ACTIONS: readonly { match: RegExp; copy: ToolAction }[] = [
  {
    match: /deploy|publish_site|hosting|release_app/,
    copy: { action: { zh: "部署应用", en: "Deploy application" }, running: { zh: "正在部署应用", en: "Deploying application" } },
  },
  {
    match: /plot|chart|figure|visuali[sz]|render_image|image_gen/,
    copy: { action: { zh: "生成图", en: "Create figure" }, running: { zh: "正在生成图", en: "Creating figure" } },
  },
  {
    match: /compile_latex|render_manuscript|build_pdf|pdf/,
    copy: { action: { zh: "生成 PDF", en: "Build PDF" }, running: { zh: "正在生成 PDF", en: "Building PDF" } },
  },
  {
    match: /experiment|simulation|benchmark|lammps/,
    copy: { action: { zh: "运行实验", en: "Run experiment" }, running: { zh: "正在运行实验", en: "Running experiment" } },
  },
  {
    match: /semantic_scholar|arxiv|literature|search_sources|search_papers/,
    copy: { action: { zh: "检索文献", en: "Search literature" }, running: { zh: "正在检索文献", en: "Searching literature" } },
  },
  {
    match: /classify_papers|archive_papers|kb_ingest|create_claim/,
    copy: { action: { zh: "整理研究证据", en: "Organize research evidence" }, running: { zh: "正在整理研究证据", en: "Organizing research evidence" } },
  },
  // ── 常用的通用工具（搜网页、读写文件、运行命令……）。要排在下面
  // `_search$` / `^read_` 这两条宽的之前，否则 web_search 会被说成「检索研究记录」。
  {
    match: /web_search/,
    copy: { action: { zh: "搜索网页", en: "Search the web" }, running: { zh: "正在搜索网页", en: "Searching the web" } },
  },
  {
    match: /web_fetch/,
    copy: { action: { zh: "读取网页", en: "Read web page" }, running: { zh: "正在读取网页", en: "Reading web page" } },
  },
  {
    match: /web_download/,
    copy: { action: { zh: "下载数据", en: "Download data" }, running: { zh: "正在下载数据", en: "Downloading data" } },
  },
  {
    match: /^search_files$/,
    copy: { action: { zh: "搜索文件", en: "Search files" }, running: { zh: "正在搜索文件", en: "Searching files" } },
  },
  {
    match: /^search_kb$|kb_search|get_kb_record/,
    copy: { action: { zh: "检索知识库", en: "Search knowledge base" }, running: { zh: "正在检索知识库", en: "Searching knowledge base" } },
  },
  {
    match: /^read_file$/,
    copy: { action: { zh: "读取文件", en: "Read file" }, running: { zh: "正在读取文件", en: "Reading file" } },
  },
  {
    match: /(^|_)write_file$/,
    copy: { action: { zh: "写入文件", en: "Write file" }, running: { zh: "正在写入文件", en: "Writing file" } },
  },
  {
    match: /^edit_file$/,
    copy: { action: { zh: "修改文件", en: "Edit file" }, running: { zh: "正在修改文件", en: "Editing file" } },
  },
  {
    match: /(^|_)run_bash$/,
    copy: { action: { zh: "运行命令", en: "Run command" }, running: { zh: "正在运行命令", en: "Running command" } },
  },
  {
    match: /^list_files$/,
    copy: { action: { zh: "查看文件列表", en: "List files" }, running: { zh: "正在查看文件列表", en: "Listing files" } },
  },
  {
    match: /^freeze_artifact$/,
    copy: { action: { zh: "定稿成果", en: "Finalize output" }, running: { zh: "正在定稿成果", en: "Finalizing output" } },
  },
  {
    match: /^memory_(note|write|maintain)$/,
    copy: { action: { zh: "更新项目记忆", en: "Update project memory" }, running: { zh: "正在更新项目记忆", en: "Updating project memory" } },
  },
  {
    match: /^memory_recall$/,
    copy: { action: { zh: "查阅项目记忆", en: "Recall project memory" }, running: { zh: "正在查阅项目记忆", en: "Recalling project memory" } },
  },
  {
    match: /^report_blocker$/,
    copy: { action: { zh: "报告阻碍", en: "Report a blocker" }, running: { zh: "正在报告阻碍", en: "Reporting a blocker" } },
  },
  {
    match: /^draft_section$/,
    copy: { action: { zh: "撰写章节", en: "Draft section" }, running: { zh: "正在撰写章节", en: "Drafting section" } },
  },
  {
    match: /^revise_section$/,
    copy: { action: { zh: "修改章节", en: "Revise section" }, running: { zh: "正在修改章节", en: "Revising section" } },
  },
  {
    match: /^search_|_search$/,
    copy: { action: { zh: "检索研究记录", en: "Search research records" }, running: { zh: "正在检索研究记录", en: "Searching research records" } },
  },
  {
    match: /^read_|^fetch_|get_source|load_artifact/,
    copy: { action: { zh: "读取资料", en: "Read source material" }, running: { zh: "正在读取资料", en: "Reading source material" } },
  },
  {
    match: /save_artifact|write_artifact|create_artifact/,
    copy: { action: { zh: "保存成果", en: "Save research output" }, running: { zh: "正在保存成果", en: "Saving research output" } },
  },
  {
    match: /run_node|start_node|subagent/,
    copy: { action: { zh: "启动研究子任务", en: "Start research sub-task" }, running: { zh: "正在启动研究子任务", en: "Starting research sub-task" } },
  },
  {
    match: /execute_python|run_python|analy[sz]e|postprocess|compute|calculate|train|inference/,
    copy: { action: { zh: "运行计算", en: "Run computation" }, running: { zh: "正在计算", en: "Running computation" } },
  },
  {
    match: /evidence_chain/,
    copy: { action: { zh: "构建证据链", en: "Build evidence chain" }, running: { zh: "正在构建证据链", en: "Building evidence chain" } },
  },
  {
    match: /query_project_status|runtime_control/,
    copy: { action: { zh: "查看项目状态", en: "Check project status" }, running: { zh: "正在查看项目状态", en: "Checking project status" } },
  },
  {
    match: /list_artifacts|list_sources/,
    copy: { action: { zh: "列出成果", en: "List research outputs" }, running: { zh: "正在列出成果", en: "Listing research outputs" } },
  },
  {
    match: /decision_package|human_input/,
    copy: { action: { zh: "整理决策选项", en: "Prepare a research decision" }, running: { zh: "正在整理决策选项", en: "Preparing a research decision" } },
  },
];

function actionFor(name: string): ToolAction {
  const normalized = name.toLowerCase();
  for (const entry of TOOL_ACTIONS) {
    if (entry.match.test(normalized)) return entry.copy;
  }
  // 兜底：英文界面用工具自己的名字（一个英文标识符，读得懂）；中文界面不露
  // 英文标识符，只说「使用工具」—— 后面 researchToolTitle 会接上它作用的对象。
  const humanized = humanize(name);
  return {
    action: { zh: "使用工具", en: humanized || "Use research tool" },
    running: { zh: "正在使用工具", en: humanized ? `Using ${humanized.toLowerCase()}` : "Using a research tool" },
  };
}

export function researchActivityKindForTool(name?: string): ResearchActivityKind {
  const normalized = name?.toLowerCase() ?? "";
  if (/deploy|publish_site|hosting|release_app/.test(normalized)) return "deployment";
  if (/plot|chart|figure|visuali[sz]|render_image|image_gen/.test(normalized)) return "figure";
  if (/compile_latex|render_manuscript|build_pdf|pdf/.test(normalized)) return "pdf";
  if (/experiment|simulation|benchmark|lammps/.test(normalized)) return "experiment";
  if (/semantic_scholar|arxiv|literature|search|papers|source|kb_|claim|evidence/.test(normalized)) return "research";
  if (/python|analy[sz]e|postprocess|compute|calculate|train|inference/.test(normalized)) return "compute";
  return "general";
}

export function researchActivityRecovery(kind: ResearchActivityKind, lang: Language): string {
  return say(RECOVERY[kind], lang);
}

function readableArgumentValue(value: unknown, lang: Language) {
  if (typeof value === "string" && value.trim()) return clip(value, 120);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return say({ zh: "{count} 项", en: "{count} items" }, lang, { count: value.length });
  return undefined;
}

/**
 * 预览里的一项参数。常用参数名（检索词、路径……）按表翻成人话；表里没有的参数，
 * 中文界面只显示值（参数名是英文标识符），英文界面照旧把参数名写成词。
 */
function argumentPart(key: string, value: string, lang: Language) {
  const label = toolArgumentLabel(key, lang);
  if (label) return lang === "zh" ? `${label}：${value}` : `${label}: ${value}`;
  return lang === "zh" ? value : `${humanize(key)}: ${value}`;
}

export function researchToolInputPreview(argumentsValue: Record<string, unknown> | undefined, lang: Language) {
  if (!argumentsValue) return undefined;
  const parts: string[] = [];
  const usedKeys = new Set<string>();
  for (const key of OBJECT_KEYS) {
    const value = readableArgumentValue(argumentsValue[key], lang);
    if (value) {
      parts.push(argumentPart(key, value, lang));
      usedKeys.add(key);
      break;
    }
  }
  parts.push(...Object.entries(argumentsValue)
    .filter(([key]) => !usedKeys.has(key) && !PRIVATE_OR_BULK_ARGUMENT.test(key))
    .flatMap(([key, value]) => {
      const readable = readableArgumentValue(value, lang);
      return readable ? [argumentPart(key, readable, lang)] : [];
    })
    .slice(0, 3));
  return parts.length ? parts.join(" · ") : undefined;
}

function safeRecovery(value: string | undefined, fallback: string) {
  if (!value) return fallback;
  // 同上：不再按长相判断"像不像技术细节"。上游明确写了下一步该干什么，就用
  // 它的原话 —— 换成我们的通用兜底只会更空洞。只挡住长到不像一句提示的。
  return value.length > 400 ? fallback : value;
}

/**
 * 这次失败是"框架按设计说不"，还是"我们的代码崩了"？
 *
 * 章是 harness 在产生失败的那一层盖的（`core/tool_errors.py`），不是这里猜的。
 * 前者是 ReAct 循环的正常一步（模型下一轮改对），不该进「N failed actions」；
 * 后者模型改多少次参数都没用，必须给人看。
 */
//: ⚠️ 这份名单必须逐字等于 `core/tool_errors.py` 的 LOOP_LEVEL_CODES。
//: 两处各存一份就是两个真相源：上游加一个 code、这里没跟上，那个 code 会静默
//: 落进兜底分支（既不 Declined 也没有专门文案），而且不会有任何报错。
//: `tests/test_tool_error_vocabulary_is_one_table.py` 机械比对这两份。
const LOOP_LEVEL_ERROR_CODES = new Set([
  "rejected",
  "tool_not_registered",
  "missing_parameters",
  "capability_denied",
]);

export function isLoopLevelFailure(code?: string) {
  return Boolean(code && LOOP_LEVEL_ERROR_CODES.has(code));
}

/**
 * `tool_exception` 的正文是 `str(exc)` —— 唯一一类**不是我们写给人看的**文案。
 *
 * 其余每一类（rejected / command_failed / missing_parameters…）都是 harness
 * 作者写给模型看的指导，原样给最有用。异常不是：2026-08-11 那次，一条
 * SQLAlchemy IntegrityError 把整条 INSERT、全部列名和参数值送进了会话正文
 * （backend/tests/test_failures_reach_the_user_as_product_copy.py 守的就是它）。
 *
 * 所以只对这一类设界：**首行 + 200 字**。`KeyError: 'turns'` 原样保留（它本来
 * 就短、就一行），而 SQLAlchemy 那种把 `[SQL: …]` / `[parameters: …]` 换行摊开
 * 的转储会被截在第一行。完整原文仍在 `?view=execution` 的取证视图里。
 */
function exceptionSafe(raw: string, code?: string) {
  if (code !== "tool_exception") return raw;
  const firstLine = raw.split("\n", 1)[0].trim();
  return firstLine.length > 200 ? `${firstLine.slice(0, 199)}…` : firstLine;
}

export function researchToolFailurePresentation(
  name: string | undefined,
  error: { code?: string; message: string; retryable?: boolean; recovery?: string } | undefined,
  lang: Language,
) {
  const raw = (error?.message ?? "").trim();
  const normalized = raw.toLowerCase();
  const loopLevel = isLoopLevelFailure(error?.code);
  // 驳回（参数不对、时机不对……）由助手下一步自己改，用户不需要做什么；
  // 其余按工具类别给一句兜底。上游自己写了下一步时仍用上游的原话（safeRecovery）。
  const fallbackRecovery = loopLevel
    ? say({ zh: "助手会自动调整，无需处理。", en: "The assistant will adjust on its own; nothing to do." }, lang)
    : researchActivityRecovery(researchActivityKindForTool(name), lang);
  // ── 错误正文照原样给 ────────────────────────────────────────────────
  //
  // 上一版在这里按**长相**判断"像不像技术细节"：含 `_[a-z]`（任何 snake_case
  // 标识符）、含花括号方括号、超过 180 字符 —— 命中就整句换成
  // "The tool stopped before producing a usable result."。
  //
  // 拿本机 1038 条真实失败跑这段代码：**899 条（87%）被换掉**，其中 55% 只是
  // 因为正文里出现了一个 snake_case 名字。而我们的报错按设计就要点名字段和
  // 工具（[[契约必须送到调用方]]），于是"写得越好的报错越会被吞掉"——
  // `quality_mode must be one of ['auto','publication',…], got 'draft'` 这种
  // 能让人立刻做对一件事的句子，被换成一句什么都没说的话。
  //
  // 现在分档只决定**标题和下一步**（那是我们能给的增量），正文永远是上游
  // 的原话。分不分得清"是谁的锅"看 `error.code` —— harness 在产生失败的那
  // 一层盖的章，不再猜字符串长相。
  const message = exceptionSafe(raw, error?.code)
    || say({ zh: "工具没有返回失败原因。", en: "The tool reported no reason for this failure." }, lang);
  if (
    /classify_papers|archive_papers/.test(name?.toLowerCase() ?? "")
    && /classification[_ ]?json|papers[_ ]?json|json.*(parse|decode)|extra data/.test(normalized)
  ) {
    return {
      title: say({ zh: "论文分类保存失败", en: "Paper classification was not saved" }, lang),
      message,
      recovery: safeRecovery(error?.recovery, say({ zh: "让助手重新生成分类并保存。", en: "Ask the assistant to regenerate and save the classification." }, lang)),
      loopLevel,
    };
  }
  if (error?.code === "command_failed") {
    return {
      // 外部命令跑了但没成（或压根没起来）。既不是模型调错、也不一定是我们的
      // bug —— 正文里的 stderr 才是唯一有用的东西，标题只负责别误导。
      title: say({ zh: "{action}：命令执行失败", en: "{action} ran a command that failed" }, lang,
        { action: say(actionFor(name ?? "research_tool").action, lang) }),
      message,
      recovery: safeRecovery(error?.recovery,
        say({ zh: "失败原因见上方的命令输出。", en: "Read the command output above; it names what failed." }, lang)),
      loopLevel,
    };
  }
  if (error?.code === "non_dict_result") {
    return {
      // 工具返回值不合规范 = 我们的 bug，不是环境、更不是用户的输入。
      title: say({ zh: "工具返回的结果格式有误", en: "A tool returned a malformed result" }, lang),
      message,
      recovery: safeRecovery(error?.recovery,
        say({ zh: "这是工具本身的问题，重试无法解决。", en: "This is a problem with the tool itself; retrying won't help." }, lang)),
      loopLevel,
    };
  }
  if (error?.code === "toolchain_missing") {
    return {
      // 这台机器缺外部程序（编译器/求解器）。跟 provider_error 同一个道理：
      // 标题必须说清是**环境**缺东西，否则读者会去查自己的稿子——实测那次
      // 界面说的是 "Review the document source"，而源码一个字都没错。
      title: say({ zh: "缺少这一步需要的程序", en: "This machine is missing a tool this step needs" }, lang),
      message,
      recovery: safeRecovery(error?.recovery,
        say({ zh: "在执行这次任务的电脑上安装缺少的程序，然后重新运行这一步。", en: "Install the missing program on the machine running this task, then run this step again." }, lang)),
      loopLevel,
    };
  }
  if (error?.code === "provider_error") {
    return {
      // 模型服务挂了/限额了：要人看，但**不是研究出问题，也不是我们的 bug**。
      // 标题说清是谁的锅，否则读者会去查自己的输入（[[项目-模型服务故障的归属]]）。
      title: say({ zh: "模型服务请求失败", en: "The model service could not complete this request" }, lang),
      message,
      recovery: safeRecovery(error?.recovery,
        say({ zh: "请稍后重试。如果超出了上下文长度，请把任务拆小一些。", en: "Retry once the provider recovers; if it is a context-length limit, split the work into smaller steps." }, lang)),
      loopLevel,
    };
  }
  if (/timeout|timed out|deadline|超时/.test(normalized)) {
    return {
      title: say({ zh: "工具执行超时", en: "The research tool timed out" }, lang),
      message,
      recovery: safeRecovery(error?.recovery, fallbackRecovery),
      loopLevel,
    };
  }
  if (/permission|forbidden|unauthori[sz]ed|credential|api key/.test(normalized)) {
    return {
      title: say({ zh: "工具需要访问权限", en: "The research tool needs access" }, lang),
      message,
      recovery: safeRecovery(error?.recovery, say({ zh: "请在设置中检查权限，然后重新发送请求。", en: "Review access in Settings, then send the request again." }, lang)),
      loopLevel,
    };
  }
  return {
    title: loopLevel
      // 驳回不是事故：只说这一步没执行，下一步说明助手会自己调整。
      ? say({ zh: "{action}：未执行", en: "{action} was not run" }, lang,
        { action: say(actionFor(name ?? "research_tool").action, lang) })
      : say({ zh: "{action}失败", en: "{action} did not complete" }, lang,
        { action: say(actionFor(name ?? "research_tool").action, lang) }),
    message,
    recovery: safeRecovery(error?.recovery, fallbackRecovery),
    loopLevel,
  };
}

export function isVisibleResearchTool(name?: string) {
  if (!name) return false;
  const normalized = name.toLowerCase();
  return !INTERNAL_TOOL_PATTERNS.some((pattern) => pattern.test(normalized));
}

export function researchToolTitle(
  name: string | undefined,
  argumentsValue: Record<string, unknown> | undefined,
  detail: string | undefined,
  lang: Language,
) {
  if (!name) return objectFrom(undefined, detail) ?? say({ zh: "使用研究工具", en: "Use research tool" }, lang);
  const action = say(actionFor(name).action, lang);
  const candidate = objectFrom(argumentsValue, detail);
  const object = candidate?.toLowerCase() === humanize(name).toLowerCase() ? undefined : candidate;
  return object && !action.toLowerCase().includes(object.toLowerCase())
    ? `${action} — ${object}`
    : action;
}

export function researchToolRunningStatus(
  name: string | undefined,
  argumentsValue: Record<string, unknown> | undefined,
  detail: string | undefined,
  lang: Language,
) {
  if (!name) return objectFrom(undefined, detail) ?? say({ zh: "正在使用研究工具", en: "Using a research tool" }, lang);
  const action = say(actionFor(name).running, lang);
  const candidate = objectFrom(argumentsValue, detail);
  const object = candidate?.toLowerCase() === humanize(name).toLowerCase() ? undefined : candidate;
  return object && !action.toLowerCase().includes(object.toLowerCase())
    ? `${action} — ${object}`
    : action;
}

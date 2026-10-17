// 相对路径 + 显式 .ts：这个文件被 node:test 直接跑（没有 tsconfig 的路径别名），和 diagnostics.ts 同一个理由。
import { say, type Language } from "../../../shared/i18n/language.ts";

/**
 * 会话的环境条幅：worker 起来时哪些持久化前提没成立、修好了什么、同一个问题是不是连续出现
 * （后端 `GET /projects/{p}/sessions/{s}/environment`，见 `app/services/session_environment.py`；
 * RFC_WORKER_START_IS_NOT_A_GATE §3.3）。
 *
 * 这里只有**判断**：给一份报告和「用户上次收起的是哪一组」，算出条幅长什么样。
 * 组件只负责画。判断是纯函数，所以能被 node:test 直接验。
 */

export type EnvironmentSeverity = "degraded" | "repaired" | "fatal";

export interface EnvironmentFacts {
  path: string | null;
  exists: boolean | null;
  kind: string | null;
  owner: string | null;
  mode: string | null;
  acl: string | null;
  reparse_tag: string | null;
  error: string | null;
}

export interface EnvironmentHolder {
  pid: number | null;
  spawn_token: string | null;
  alive: boolean | null;
  error?: string | null;
}

export interface EnvironmentRepair {
  kind: string;
  argv: string[] | null;
  command: string | null;
  changes: string;
  reversible: boolean | null;
  needs_consent: boolean;
  automatic: boolean;
}

export interface EnvironmentRepairResult {
  code: string;
  path: string | null;
  command: string | null;
  returncode: number | null;
  stdout: string;
  stderr: string;
}

export interface EnvironmentWarning {
  code: string;
  capability?: string;
  severity: EnvironmentSeverity;
  path: string | null;
  error: string | null;
  consequences: string[];
  backup?: string;
  repair_attempt?: string;
  facts?: EnvironmentFacts | null;
  holders?: EnvironmentHolder[];
  token?: { user: string | null; uid: number | null; integrity: string | null } | null;
  repair?: EnvironmentRepair | null;
  repair_result?: EnvironmentRepairResult | null;
}

export interface SessionEnvironment {
  warnings: EnvironmentWarning[];
  capabilities: Record<string, boolean> | null;
  source: "live" | "last_run" | "hard_failure" | null;
  repeated: number;
  escalated: boolean;
  doctor: {
    paths: EnvironmentFacts[];
    data_root: EnvironmentFacts;
    token: { user: string | null; uid: number | null; integrity: string | null };
    holders: EnvironmentHolder[];
  } | null;
  runId: string | null;
  data_root: string;
}

export type BannerTone = "error" | "warning" | "info";

export interface BannerModel {
  tone: BannerTone;
  title: string;
  lines: string[];
  /** 能一键代执行的那一条（服务端按表重算；这里只带 code / 路径 / 命令原文给人看）。 */
  repair: { code: string; path: string | null; command: string; changes: string } | null;
  /** 同一组问题的指纹：用户收起的是它，它不变就不再弹。 */
  signature: string;
  /** 「详情」里逐条摆的事实。 */
  details: string[];
  escalated: boolean;
}

const CODE_LABELS: Record<string, { zh: string; en: string }> = {
  home_marker_unreadable: { zh: "数据目录里的身份标记读不到", en: "The identity marker in the data directory could not be read" },
  home_marker_rebuilt: { zh: "重建了数据目录里的身份标记", en: "Rebuilt the identity marker in the data directory" },
  state_marker_unreadable: { zh: "会话运行目录的身份标记读不到", en: "The session state marker could not be read" },
  state_marker_rebuilt: { zh: "重建了会话运行目录的身份标记", en: "Rebuilt the session state marker" },
  state_dir_unavailable: { zh: "会话运行目录建不出来", en: "The session state directory could not be created" },
  lock_unavailable: { zh: "会话锁文件建不出来", en: "The session lock file could not be created" },
  activity_file_unavailable: { zh: "活性文件写不进", en: "The activity file could not be written" },
  durable_sink_unavailable: { zh: "事件没有落盘副本", en: "Events have no on-disk copy" },
  state_unreadable: { zh: "上次的运行状态读不回来", en: "The previous run state could not be read back" },
  conversation_unreadable: { zh: "上次的对话读不回来", en: "The previous conversation could not be read back" },
  transcript_unwritable: { zh: "转录写不进磁盘", en: "The transcript could not be written" },
  data_root_permissions_changed: { zh: "升级前后数据目录的权限变了", en: "The data directory's permissions changed across the upgrade" },
  tenant_home_conflict: { zh: "这个数据目录写着别人的身份", en: "This data directory is bound to someone else" },
  runtime_identity_conflict: { zh: "这个运行目录是别的会话的", en: "This state directory belongs to another session" },
};

export function codeLabel(code: string, lang: Language): string {
  const known = CODE_LABELS[code];
  return known ? say(known, lang) : code;
}

/** 这一组问题的指纹（只看还没修好的）：收起之后它不变就不再弹。 */
export function environmentSignature(env: SessionEnvironment | null | undefined): string {
  if (!env) return "";
  const open = env.warnings
    .filter((w) => w.severity !== "repaired")
    .map((w) => `${w.code}@${w.path ?? ""}`)
    .sort();
  const parts = [...open];
  if (env.escalated) parts.push(`escalated#${env.repeated}`);
  return parts.join("|");
}

function factLines(w: EnvironmentWarning, lang: Language): string[] {
  const out: string[] = [];
  if (w.path) out.push(say({ zh: "路径：{path}", en: "Path: {path}" }, lang, { path: w.path }));
  if (w.error) out.push(say({ zh: "错误：{error}", en: "Error: {error}" }, lang, { error: w.error }));
  const f = w.facts;
  if (f) {
    if (f.error) out.push(say({ zh: "探针：{error}", en: "Probe: {error}" }, lang, { error: f.error }));
    if (f.exists === false) out.push(say({ zh: "探针：路径不存在", en: "Probe: the path does not exist" }, lang));
    if (f.owner) out.push(say({ zh: "所有者：{owner}", en: "Owner: {owner}" }, lang, { owner: f.owner }));
    if (f.mode) out.push(say({ zh: "权限：{mode}", en: "Mode: {mode}" }, lang, { mode: f.mode }));
    if (f.acl) out.push(say({ zh: "ACL：{acl}", en: "ACL: {acl}" }, lang, { acl: f.acl }));
    if (f.reparse_tag) out.push(say({ zh: "reparse：{tag}", en: "Reparse tag: {tag}" }, lang, { tag: f.reparse_tag }));
  }
  if (w.token?.user) {
    out.push(say({ zh: "本进程：{user}{il}", en: "This process: {user}{il}" }, lang, {
      user: w.token.user,
      il: w.token.integrity ? ` (${w.token.integrity})` : "",
    }));
  }
  const alive = (w.holders ?? []).filter((h) => h.alive === true);
  if (alive.length > 0) {
    out.push(say({ zh: "本应用派出的进程还握着它：pid {pids}", en: "A process this app spawned still holds it: pid {pids}" }, lang, {
      pids: alive.map((h) => String(h.pid)).join(", "),
    }));
  }
  if (w.repair_attempt) out.push(say({ zh: "自动修复没成：{why}", en: "Automatic repair failed: {why}" }, lang, { why: w.repair_attempt }));
  if (w.backup) out.push(say({ zh: "原文件留在：{backup}", en: "The original was kept at: {backup}" }, lang, { backup: w.backup }));
  if (w.repair_result) {
    out.push(say({ zh: "已执行 `{command}`，返回码 {rc}", en: "Ran `{command}`, exit code {rc}" }, lang, {
      command: w.repair_result.command ?? "",
      rc: w.repair_result.returncode === null ? "?" : String(w.repair_result.returncode),
    }));
    if (w.repair_result.stderr) out.push(w.repair_result.stderr);
  }
  return out;
}

/**
 * 条幅长什么样。`null` = 不画。
 *
 * - 有 fatal（这一轮没起跑）→ 红；有 degraded → 黄；只剩 repaired → 蓝，告诉一声就够。
 * - 用户收起过同一组（指纹相同）→ 不画；组变了（多了一条、修好了一条）→ 再弹。
 * - 同一个问题连续出现（escalated）→ 不许收起：标题说清是第几次，详情里是自动诊断的结果。
 */
export function bannerModel(
  env: SessionEnvironment | null | undefined,
  lang: Language,
  dismissedSignature: string | null,
): BannerModel | null {
  if (!env) return null;
  const warnings = env.warnings ?? [];
  const fatal = warnings.filter((w) => w.severity === "fatal");
  const degraded = warnings.filter((w) => w.severity === "degraded");
  const repaired = warnings.filter((w) => w.severity === "repaired");
  if (fatal.length === 0 && degraded.length === 0 && repaired.length === 0 && !env.escalated) return null;
  const signature = environmentSignature(env);
  if (!env.escalated && dismissedSignature !== null && dismissedSignature === signature) return null;

  const tone: BannerTone = fatal.length > 0 ? "error" : degraded.length > 0 ? "warning" : "info";
  let title: string;
  if (env.escalated) {
    title = say({
      zh: "同一个问题连续出现 {n} 次，平台已自动诊断",
      en: "The same problem has occurred {n} times in a row; the platform ran a diagnosis",
    }, lang, { n: String(env.repeated) });
  } else if (fatal.length > 0) {
    title = say({ zh: "这一轮没有起跑：{what}", en: "This turn did not start: {what}" }, lang, { what: codeLabel(fatal[0].code, lang) });
  } else if (degraded.length > 0) {
    title = degraded.length === 1
      ? say({ zh: "这个会话有一项持久化前提没成立：{what}", en: "One persistence precondition of this session is not met: {what}" }, lang, { what: codeLabel(degraded[0].code, lang) })
      : say({ zh: "这个会话有 {n} 项持久化前提没成立", en: "{n} persistence preconditions of this session are not met" }, lang, { n: String(degraded.length) });
  } else {
    title = repaired.length === 1
      ? say({ zh: "平台修好了一处：{what}", en: "The platform repaired one thing: {what}" }, lang, { what: codeLabel(repaired[0].code, lang) })
      : say({ zh: "平台修好了 {n} 处", en: "The platform repaired {n} things" }, lang, { n: String(repaired.length) });
  }

  const lines: string[] = [];
  for (const w of [...fatal, ...degraded]) {
    for (const c of w.consequences ?? []) if (!lines.includes(c)) lines.push(c);
  }
  if (degraded.length > 0 && lines.length === 0) {
    lines.push(say({ zh: "这一轮照常回答，但它记下的东西可能保不住。", en: "This turn still answers, but what it records may not persist." }, lang));
  }

  const candidate = [...degraded, ...fatal].find(
    (w) => w.repair?.argv && w.repair.command && !(w.repair_result && w.repair_result.returncode === 0),
  );
  const repair = candidate?.repair?.command
    ? { code: candidate.code, path: candidate.path, command: candidate.repair.command, changes: candidate.repair.changes }
    : null;

  const details: string[] = [];
  for (const w of [...fatal, ...degraded, ...repaired]) {
    details.push(`${codeLabel(w.code, lang)} (${w.code})`);
    for (const line of factLines(w, lang)) details.push(`  ${line}`);
  }
  if (env.doctor) {
    details.push(say({ zh: "自动诊断", en: "Automatic diagnosis" }, lang));
    const d = env.doctor;
    for (const p of d.paths ?? []) {
      details.push(`  ${p.path ?? "?"}: ${p.error ?? [p.kind, p.owner, p.mode].filter(Boolean).join(" · ") ?? "?"}`);
    }
    if (d.token?.user) details.push(`  ${say({ zh: "本进程", en: "This process" }, lang)}: ${d.token.user}${d.token.integrity ? ` (${d.token.integrity})` : ""}`);
    const alive = (d.holders ?? []).filter((h) => h.alive === true);
    if (alive.length > 0) details.push(`  ${say({ zh: "本应用的进程还活着", en: "Processes of this app still alive" }, lang)}: ${alive.map((h) => h.pid).join(", ")}`);
  }

  return { tone, title, lines, repair, signature, details, escalated: env.escalated };
}

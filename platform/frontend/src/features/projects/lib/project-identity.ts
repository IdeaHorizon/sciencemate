import type { Project } from "@/lib/api";
import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

export type ProjectIdentity = {
  name: string;
  meta: string;
  state: "loading" | "ready" | "error" | "fallback";
};

/**
 * 项目状态的人读名。
 *
 * 认不出来的值原样交出去，**不猜也不吞**：后端将来加了新状态，界面上会原样
 * 出现那个词（难看，但看得见），而不是显示一个空白或者一个猜出来的中文。
 */
const STATUS_LABELS: Record<string, Phrase> = {
  active: { zh: "进行中", en: "Active" },
  archived: { zh: "已归档", en: "Archived" },
};

function humanizeStatus(status: string, lang: Language) {
  const phrase = STATUS_LABELS[status];
  return phrase ? say(phrase, lang) : status;
}

export function projectIdentity(
  project: Project | undefined,
  state: { loading: boolean; error: boolean },
  lang: Language = "zh",
): ProjectIdentity {
  if (state.loading) {
    return { name: say({ zh: "载入中…", en: "Loading project…" }, lang), meta: say({ zh: "正在解析工作区", en: "Resolving workspace" }, lang), state: "loading" };
  }
  if (project) {
    const status = humanizeStatus(project.status, lang);
    return {
      name: project.name,
      meta: project.research_domain ? `${status} · ${project.research_domain}` : status,
      state: "ready",
    };
  }
  if (state.error) {
    return { name: say({ zh: "读不到这个项目", en: "Project unavailable" }, lang), meta: say({ zh: "项目信息没读出来", en: "Could not load project metadata" }, lang), state: "error" };
  }
  return { name: say({ zh: "研究项目", en: "Research project" }, lang), meta: say({ zh: "项目工作区", en: "Project workspace" }, lang), state: "fallback" };
}


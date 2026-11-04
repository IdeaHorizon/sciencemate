import type { DeveloperSource } from "@/lib/api";
import type { Phrase } from "@/shared/i18n";

type Translate = (phrase: Phrase) => string;

/** 「现在」那一行：五种局面各一句。纯函数，按行为测。 */
export function sayWhereItRuns(told: DeveloperSource | null, t: Translate): string {
  if (!told) return t({ zh: "正在加载…", en: "Loading…" });
  if (told.problem) {
    return t({ zh: `已指向 ${told.declared}，但该目录无法使用：${told.problem} 当前运行的是安装的版本。`,
               en: `Points at ${told.declared}, but that directory cannot be used: ${told.problem} The installed version is running.` });
  }
  if (told.declared && told.active) {
    const head = told.git ? ` (${told.git.branch} @ ${told.git.commit})` : "";
    return t({ zh: `正在从源码目录 ${told.declared}${head} 运行。`, en: `Running from the checkout at ${told.declared}${head}.` });
  }
  if (told.declared) {
    return t({ zh: `已指向 ${told.declared}，重启后生效。当前运行的仍是安装的版本。`,
               en: `Set to ${told.declared}; it takes effect after a restart. The installed version is still running.` });
  }
  if (told.restart_needed && told.running_from) {
    return t({ zh: `已停用，重启后恢复为安装的版本。当前仍从 ${told.running_from} 运行。`,
               en: `Turned off; the installed version returns after a restart. Still running from ${told.running_from} for now.` });
  }
  return t({ zh: "未开启，当前运行的是安装的版本。", en: "Off. Running the installed version." });
}

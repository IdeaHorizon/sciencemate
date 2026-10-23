import type { DeveloperSource } from "@/lib/api";
import type { Phrase } from "@/shared/i18n";

type Translate = (phrase: Phrase) => string;

/** 「现在」那一行：五种局面各一句。纯函数，按行为测。 */
export function sayWhereItRuns(told: DeveloperSource | null, t: Translate): string {
  if (!told) return t({ zh: "正在读…", en: "Reading…" });
  if (told.problem) {
    return t({ zh: `指向 ${told.declared}，但这个目录用不了：${told.problem} 跑的是安装的版本。`,
               en: `Points at ${told.declared}, but that directory cannot be used: ${told.problem} The installed version is running.` });
  }
  if (told.declared && told.active) {
    const head = told.git ? ` (${told.git.branch} @ ${told.git.commit})` : "";
    return t({ zh: `正在从源码目录 ${told.declared}${head} 跑。`, en: `Running from the checkout at ${told.declared}${head}.` });
  }
  if (told.declared) {
    return t({ zh: `已指向 ${told.declared}，重启后生效 —— 现在跑的还是安装的版本。`,
               en: `Set to ${told.declared}; it takes effect after a restart — the installed version is still running.` });
  }
  if (told.restart_needed && told.running_from) {
    return t({ zh: `已停用，重启后回到安装的版本 —— 这个进程还在从 ${told.running_from} 跑。`,
               en: `Turned off; the installed version comes back after a restart — this process is still running from ${told.running_from}.` });
  }
  return t({ zh: "没有开启。跑的是安装包或自更新装上的那份。", en: "Off. Running what the installer or a self-update put here." });
}

/**
 * 「关于」页上「更新」那一行这一刻该说哪句话 —— 纯函数，按后端给的状态说人话。
 *
 * **每一种结果都有一句** —— 包括「查不到」（为什么见 `AboutPanel.tsx` 顶上）。从组件里拿出来
 * 是为了能按行为测：喂一份状态，看它说了什么（`.tsx` 在 node 的测试里跑不起来）。
 */
import type { UpdateStatus } from "@/lib/api";
import type { Phrase } from "@/shared/i18n";
import { sayWhatIsHappening } from "./say-what-is-happening.ts";
import type { UpdatePhase } from "./useUpdate";

type Translate = (phrase: Phrase, fields?: Record<string, string | number>) => string;

export function sayWhatTheCheckFound(status: UpdateStatus | null, phase: UpdatePhase,
                                     lastChecked: Date | null, t: Translate): string {
  const available = status?.available_version;
  const staged = status?.staged_version;
  const needsReinstall = Boolean(available && status?.shell_update?.needs_reinstall);
  // 装与重启时说什么只有一份（和横幅同一个函数）；这里不带秒数 —— 关于页那时画的是 UpdateProgress。
  const happening = sayWhatIsHappening(phase, available ?? staged, 0, t);
  if (happening) return happening;
  if (phase === "checking") return t({ zh: "正在检查…", en: "Checking…" });
  if (!status) return t({ zh: "尚未检查。", en: "Not checked yet." });
  if (status.self_updatable === false) {
    return t({ zh: "从源码运行，请用 git 更新。",
               en: "Running from source; update with git." });
  }
  if (needsReinstall) {
    return t({ zh: `有新版本 ${available}，这次需要重新安装。`,
               en: `Version ${available} is available and needs a fresh install.` });
  }
  // 还在传的那几版只接在「有新版本」「已是最新」后面：别的结果里它不是要紧的事。
  const onItsWay = status.still_publishing?.length
    ? t({ zh: `${status.still_publishing.join("、")} 正在发布，请稍后再检查。`,
          en: ` ${status.still_publishing.join(", ")} is still being published; check again later.` }) : "";
  if (available) return t({ zh: `有新版本 ${available}。`, en: `Version ${available} is available.` }) + onItsWay;
  if (staged) return t({ zh: `更新 ${staged} 已下载，重启后生效。`, en: `Update ${staged} has been downloaded and applies after a restart.` });
  if (status.error) {
    // `error` 是后端按这一屏的语言说的那句（请求带着 X-Interface-Language）。异常原文
    // （`error_cause`）不拼进来，「关于」页把它折在「技术细节」里。
    return t({ zh: `检查失败：${status.error}`, en: `Check failed: ${status.error}` });
  }
  if (!status.reachable) {
    // 可选的那一截**先各自翻好**再拼。内联三元里再套一层模板字符串，会把「这句话
    // 中英都写了」这件事从扫盘闸眼里藏掉（它按行看），于是英文界面上漏出一句中文；
    // 把中文提到 t() 外面的变量里也一样 —— 那就成了一句没配英文的中文。
    const where = status.source
      ? t({ zh: `（${status.source}）`, en: ` (${status.source})` }) : "";
    return t({ zh: `无法连接更新服务器${where}，请检查网络。`,
               en: `Cannot reach the update server${where}. Check your network connection.` });
  }
  const when = lastChecked
    ? t({ zh: `　·　${lastChecked.toLocaleTimeString()} 检查过`,
          en: ` · checked at ${lastChecked.toLocaleTimeString()}` }) : "";
  return t({ zh: `已是最新${when}。`, en: `Up to date${when}.` }) + onItsWay;
}

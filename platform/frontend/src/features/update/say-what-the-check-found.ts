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
  if (!status) return t({ zh: "还没问过。", en: "Not checked yet." });
  if (status.self_updatable === false) {
    return t({ zh: "这是源码目录直接跑的，不走自更新 —— 用 git 拉就是了。",
               en: "Running from a source checkout; updates come from git, not from here." });
  }
  if (needsReinstall) {
    return t({ zh: `有新版本 ${available}，这次改动了应用本体，装着的外壳换不了自己 —— 要重新安装才能拿到。`,
               en: `Version ${available} is available, but it changes the app shell, which cannot replace itself — reinstall to get it.` });
  }
  // 还在传的那几版只接在「有新版本」「已是最新」后面：别的结果里它不是要紧的事。
  const onItsWay = status.still_publishing?.length
    ? t({ zh: `${status.still_publishing.join("、")} 还在发布，文件没传完 —— 过一会儿再查。`,
          en: ` ${status.still_publishing.join(", ")} is still being published — check again in a while.` }) : "";
  if (available) return t({ zh: `有新版本 ${available}。`, en: `Version ${available} is available.` }) + onItsWay;
  if (staged) return t({ zh: `更新 ${staged} 已经下好，重启后生效。`, en: `Update ${staged} is staged and applies on the next restart.` });
  if (status.error) {
    // `error` 是后端的诊断原文（中文）。中文界面照旧给原文；英文界面按种类说一句英文 ——
    // 原来两种语言都直接插原文，英文界面上一离线就是「Check failed: 拿不到更新信息：…」。
    const said = status.error;
    if (status.error_kind === "unreachable") {
      const cause = status.error_cause ?? "";
      return t({ zh: `没查成：${said}`,
                 en: `Check failed: cannot reach the update source — offline, or this machine cannot get there (${cause}).` });
    }
    if (status.error_kind === "nothing_published") {
      return t({ zh: `没查成：${said}`,
                 en: "Check failed: the update source has no fully published release yet — check again in a while." });
    }
    // rejected：拉到了但验不过（签名、清单、大小）。那句诊断只有中文，英文界面上先说清是
    // 哪一种，原文跟在后面当证据 —— 报 bug 时要的正是它。
    return t({ zh: `没查成：${said}`, en: `Check failed: the update on offer did not pass verification (${said}).` });
  }
  if (!status.reachable) {
    // 可选的那一截**先各自翻好**再拼。内联三元里再套一层模板字符串，会把「这句话
    // 中英都写了」这件事从扫盘闸眼里藏掉（它按行看），于是英文界面上漏出一句中文；
    // 把中文提到 t() 外面的变量里也一样 —— 那就成了一句没配英文的中文。
    const where = status.source
      ? t({ zh: `（${status.source}）`, en: ` (${status.source})` }) : "";
    return t({ zh: `连不上更新源${where} —— 可能是离线，或者这台机器到不了那个地址。`,
               en: `Cannot reach the update source${where} — offline, or this machine cannot get there.` });
  }
  const when = lastChecked
    ? t({ zh: `　·　${lastChecked.toLocaleTimeString()} 查过`,
          en: ` · checked at ${lastChecked.toLocaleTimeString()}` }) : "";
  return t({ zh: `已是最新${when}。`, en: `Up to date${when}.` }) + onItsWay;
}

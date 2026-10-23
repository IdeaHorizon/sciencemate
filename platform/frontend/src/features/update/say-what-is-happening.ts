/**
 * 更新**正在进行**时这一刻该说哪句话 —— 横幅和关于页同一份（应用更新的视觉反馈，10-19）。
 *
 * 以前点了「更新」之后：关于页只是把按钮变灰，那行字还写着「有新版本 X」；横幅换了一行小字。
 * 下载 + 校验要十几秒（本机 0.5.6 → 0.5.7 实测 13.6 s），人看到的是「点了没反应」；
 * 「正在重启」只停 1 ms 窗口就被换掉，人看到的是「过一会自己刷新了」。
 *
 * 纯函数：喂阶段、版本、已过去几秒，看它说什么（`.tsx` 在 node 的测试里跑不起来）。
 */
import type { Phrase } from "@/shared/i18n";
import type { UpdatePhase } from "./useUpdate";

type Translate = (phrase: Phrase, fields?: Record<string, string | number>) => string;

export function sayWhatIsHappening(phase: UpdatePhase, version: string | null | undefined,
                                   elapsedSeconds: number, t: Translate): string | null {
  const which = version ?? "";
  if (phase === "installing") {
    const spent = elapsedSeconds >= 3
      ? t({ zh: `（已 ${elapsedSeconds} 秒）`, en: ` (${elapsedSeconds}s so far)` }) : "";
    return t({ zh: `正在下载并校验 ${which}…`, en: `Downloading and verifying ${which}…` }) + spent;
  }
  if (phase === "restarting") {
    // 没有版本号的重启是开发者模式那种（指了源码目录 / 撤了）：不是「完成更新」。
    if (!which) {
      return t({ zh: "即将重启 —— 窗口会自动回来。", en: "Restarting — the window comes back on its own." });
    }
    return t({ zh: `${which} 已就绪，即将重启以完成更新 —— 窗口会自动回来。`,
               en: `${which} is ready. Restarting to finish the update — the window comes back on its own.` });
  }
  return null;
}

/** 重启之后的第一屏：刚从哪一版升到哪一版。 */
export function sayItWasUpdated(from: string, to: string, t: Translate): string {
  return t({ zh: `已更新到 ${to}（原 ${from}）。`, en: `Updated to ${to} (was ${from}).` });
}

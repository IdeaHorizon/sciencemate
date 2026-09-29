import { say, type Language } from "../../../shared/i18n/language.ts";

/**
 * 失败 / 重试这类东西，什么时候该出现在对话主线上。
 *
 * wangd 2026-08-20：「为啥老显示 failed actions 啊，那个有任何用吗？failed
 * 了怎么处理了啊？接着能行吗？…如果重要就想办法解决，如果不重要就他妈删了
 * 不显示。」
 *
 * 判据用这个仓库自己那条（见 `presentChatFailure` 的注释）：**这条消息能让人
 * 多做对一件事吗？** 按这条过一遍：
 *
 * - run 还在跑：一次工具失败了、循环正在自己重试 —— 人此刻做不了任何事，
 *   而界面上一个红色的「1 failed action」抽屉读起来像"你的研究出事了"。
 * - run 已经 completed：那次失败**没有妨碍结果**。事后再把它摆在对话里，
 *   是拿过程当事故。
 * - run 停在非成功终态（failed / incomplete / cancelled），或者有 run 级
 *   失败原文：这时候失败**就是结论本身**，必须给，而且要给得到位。
 *
 * 失败记录一条都没删——它们仍然在执行历史（`?view=execution`）里，那是
 * 取证的地方。这里决定的只是"主线对话要不要摆出来"。
 */
export function shouldShowFailureDrawer({ view, hasRunFailureDetail }: {
  /** 后端那一次现算的局面。 */
  view: { phase: string; outcome: string | null };
  /** run 级失败原文（不是某一次工具失败）。 */
  hasRunFailureDetail: boolean;
}) {
  if (hasRunFailureDetail) return true;
  // 「停在非成功终态，或途中被打断」—— 这以前是一份手写状态名单。
  return view.phase === "interrupted"
    || (view.outcome !== null && view.outcome !== "ok" && view.outcome !== "ok_with_warning");
}

/**
 * 「1 recorded retry」是个不带上下文的计数（wangd：「如果是在重试能不能搞成
 * 那种 retrying 1/3 啥的」）。
 *
 * 事件里本来就带着 `attempt` / `maxAttempts`（后端 execution_ingest 建
 * `run.retrying` 时就写进去了），显示层一直没读。正在重试就说第几次、共几次；
 * 已经重试成功的，事后不再单独报一行——那属于执行历史。
 */
export function retryProgressLabel({ attempt, maxAttempts }: {
  attempt?: number;
  maxAttempts?: number;
}, lang: Language = "zh") {
  if (!attempt || !maxAttempts || maxAttempts < 1) return say({ zh: "重试中", en: "retrying" }, lang);
  return say({ zh: "第 {attempt}/{max} 次重试", en: "retry {attempt}/{max}" }, lang, { attempt, max: maxAttempts });
}

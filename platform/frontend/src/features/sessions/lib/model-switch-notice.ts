import { say, type Language } from "../../../shared/i18n/language.ts";

/**
 * 换完模型之后那句话。
 *
 * 原文案是一句写死的「已换模型；从下一轮开始生效」—— 不看会话在不在跑。
 * 于是在一个**空闲**会话里换模型（什么都没输入、什么都没在跑），界面也告诉
 * 你"下一轮才生效"，读起来像"你还得先浪费一轮"。事实不是这样：
 *
 * - PATCH 立刻把 `session.model_backend_id` 改掉，下一次开轮直接按它取后端；
 * - 后端 `ensure()` 比对 backend fingerprint，对不上且 worker 不忙 → 杀掉旧
 *   worker、用新模型重开一个。
 *
 * 所以空闲时"下一轮"就是**你下一条消息**，没有额外的一轮要等。真正有延迟的
 * 只有一种情况：这一轮正在跑（或停在等人处）—— 那个 worker 进程的模型是
 * spawn 时定死在环境变量里的，改不了当下这一轮。
 *
 * 一句话的口径要等于它回答的那个问题：人问的是"我现在换了，什么时候算数"。
 */
export function modelSwitchNotice({ turnInFlight, modelLabel }: {
  /** 这一轮是否在飞（跑着，或停在等人/等授权处）。 */
  turnInFlight: boolean;
  /** 换成了哪个 —— 换完不说换成了什么，人只能回头再看一眼 chip。 */
  modelLabel: string | null;
}, lang: Language = "zh") {
  const label = modelLabel?.trim();
  const target = label
    ? say({ zh: "已换成 {label}", en: "Switched to {label}" }, lang, { label })
    : say({ zh: "已换模型", en: "Model switched" }, lang);
  return turnInFlight
    ? say({
      zh: "{target}；这一轮已经在跑，仍用原来的模型，下一轮开始生效",
      en: "{target}. This turn is already running on the previous model; the change takes effect from the next turn.",
    }, lang, { target })
    : say({
      zh: "{target}，下一条消息就用它",
      en: "{target}. Your next message uses it.",
    }, lang, { target });
}

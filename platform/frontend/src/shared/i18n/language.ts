/**
 * 界面语言 —— 类型、取值、以及"现在是哪种"。
 *
 * ## 这个开关覆盖到哪里
 *
 * 全部界面：工作区、项目、会话、执行过程、设置页，以及资讯流（含域词表的
 * 人读名）。后端那半边（状态词、资讯文案、失败文案）读 `language_for(user)`，
 * 跟着同一个偏好走。
 *
 * 这不是一句承诺，是一条判据：`every-visible-string-has-both-languages.test.ts`
 * 两个方向各扫一遍全盘 —— 界面上任何一句只有中文、或者只有英文的话都会让
 * 它红。2026-09-15 之前这里写的是"只有资讯流做了本地化"，那时它说的是实话：
 * 一个声称"界面语言"却只改一半的开关，用户会以为它坏了，然后不再相信别的
 * 开关。现在两边都真的换了。
 *
 * ## 为什么语言从设置里读，而不是猜浏览器的
 *
 * `navigator.language` 说的是这台机器装了什么，不是这个人想用什么读科研
 * 资讯。两者经常不一致（英文系统的中文研究者），而猜错的代价是他每次打开
 * 都要重新面对一屏不想要的语言，却找不到在哪改。
 */
export type Language = "zh" | "en";

export const LANGUAGES: readonly Language[] = ["zh", "en"] as const;

/** 语言自己的名字用自己写 —— 一个只认中文的人看不懂 "Chinese"。 */
export const LANGUAGE_LABELS: Record<Language, string> = {
  zh: "中文",
  en: "English",
};

export function isLanguage(value: unknown): value is Language {
  return value === "zh" || value === "en";
}

/**
 * 缺翻译时给什么：**中文原文，不给 key**。
 *
 * 界面上冒出一个 `feed.card.save` 对用户来说就是坏了；一句没翻的中文只是
 * 没翻。两种失败都该修，但它们对用户的伤害不是一个量级。
 */
export type Phrase = Record<Language, string>;

export function say(phrase: Phrase, lang: Language, fields?: Record<string, string | number>): string {
  const text = phrase[lang] ?? phrase.zh;
  if (!fields) return text;
  return text.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in fields ? String(fields[name]) : match);
}

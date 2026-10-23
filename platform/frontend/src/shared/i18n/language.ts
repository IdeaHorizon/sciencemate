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
 * ## 语言存在设置里；没选过的人，按系统语言替他定一次
 *
 * 2026-08-23 定的是「默认中文、不猜」：`navigator.language` 说的是这台机器装了
 * 什么，不是这个人想用什么（英文系统的中文研究者），而猜错的代价是"找不到在哪改"。
 * 2026-10-19 wangd 指出反面：不懂中文的人打开是一屏中文，同样找不到在哪改。
 *
 * 两头的病是同一个：**切换钮只用当前语言写着**。所以现在是三件事一起：
 *
 *   1. 没选过的人，第一次打开按**系统语言列表**定一次（`languageForASystem`：
 *      列表里有中文就中文，否则英文），并存进偏好 —— 后端的文案也读那一份；
 *   2. 开场第一张卡片最上面就是语言，侧栏常驻一个切换钮、登录页也有一个，
 *      **都用目标语言写**（中文界面上写 English，英文界面上写 中文）；
 *   3. 选过之后只认他选的，不再猜。
 *
 * 看整张列表而不是第一项：英文系统的中文研究者，中文常排在第二位。桌面版的
 * WKWebView 只交出第一项，所以列表由跑在他电脑上的后端读（见后端
 * `services/system_language.py`）。
 */
export type Language = "zh" | "en";

export const LANGUAGES: readonly Language[] = ["zh", "en"] as const;

/** 语言自己的名字用自己写 —— 一个只认中文的人看不懂 "Chinese"。 */
export const LANGUAGE_LABELS: Record<Language, string> = {
  zh: "中文",
  en: "English",
};

/**
 * 一个还没选过语言的人，按他的系统语言列表该用哪种：**列表里有中文就中文，否则英文**
 * （wangd 2026-10-19：中英之外的语言一律先给英文）。
 *
 * `zh`、`zh-CN`、`zh-Hans-CN`、`zh_TW` 都算中文；`zha`（壮语）不算。
 */
export function languageForASystem(preferred: readonly string[]): Language {
  return preferred.some((tag) => /^zh(?:[-_]|$)/i.test(tag.trim())) ? "zh" : "en";
}

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

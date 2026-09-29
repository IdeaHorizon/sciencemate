"use client";

import { useCallback } from "react";

import { say, type Phrase } from "./language.ts";
import { useLanguage } from "./useLanguage.ts";

/**
 * 组件里翻一句话：`const t = useT(); … {t({ zh: "项目", en: "Projects" })}`。
 *
 * ## 为什么两种语言写在一起，而不是一张 key 表
 *
 * key 表（`t("nav.projects")` + 另一个文件里的映射）有一个这个仓库反复栽过的
 * 毛病：**两份东西各自演化**。改了文案忘了改表、加了 key 忘了加翻译、删了组件
 * 留下孤儿 key —— 每一种都不报错，只是界面上少一句或者冒出一个 key。
 *
 * 两种语言并排写在调用点，它们在物理上就分不开：加一句话的时候两种语言一起
 * 写，删的时候一起删。代价是调用点长一点，换来的是"漏翻"这件事在语法上就
 * 写不出来（`Phrase` 的两个字段都是必填）。
 *
 * ## 纯函数不许用它
 *
 * 这是个 hook。日期格式化、状态名这类纯函数要语言就**收一个 `lang` 参数**，
 * 由调用方传进去 —— 让纯函数自己去够一个全局，这个仓库就没有一条呈现逻辑
 * 还能被测到了。
 */
export function useT(): (phrase: Phrase, fields?: Record<string, string | number>) => string {
  const lang = useLanguage();
  // 语言不变就还是同一个函数 —— 否则它进不了任何 useCallback/useMemo 的依赖表，
  // 每个用到它的回调都得在「漏依赖」和「每次渲染重建」之间二选一。
  return useCallback(
    (phrase: Phrase, fields?: Record<string, string | number>) => say(phrase, lang, fields),
    [lang],
  );
}

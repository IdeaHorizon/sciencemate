"use client";

import { CircleHelp, ShieldAlert } from "lucide-react";
import type { ChatPause } from "../types";
import { decisionChoiceText, decisionQuestion } from "../lib/decision-package";
import { useLanguage, useT } from "@/shared/i18n";

/**
 * 「这条 run 当时停在这个问题上」—— **记录**，不是入口。
 *
 * 一个还没人回答的问题，不会因为问它的进程死了就不存在了：run 被部署重启掐掉
 * 之后转 `stale_unknown`，而 `summary.pause` 里 question / context / options
 * 一样不少。丢掉它，用户会在记录里看见平台问「是否批准这个高危作业」，底下
 * 既没有可点的东西，也没有一句话说明它已经问不成了。
 *
 * 它和 `HumanInputPrompt` 是**两件事**，所以是两个组件：
 *
 *   HumanInputPrompt  由 `answer.via === "pause"` 构造 —— 后端刚说过"入口是它"
 *   PausedRecord      由一条 run 自己的记录构造 —— 永远只读
 *
 * 这里**没有** `onAnswer` 这个 prop。不是"传了不用"，是根本没有 —— 于是
 * "看起来能点其实点不了"在构造上不可能，也不需要谁记得传一个 `resumable`。
 */
export function PausedRecord({ pause }: { pause: ChatPause }) {
  const t = useT();
  const lang = useLanguage();
  const Icon = pause.kind === "permission" ? ShieldAlert : CircleHelp;
  return (
    <section className="human-input-prompt is-readonly kind-record" aria-label={t({ zh: "已记录的问题", en: "Recorded question" })}>
      <header>
        <Icon size={15} aria-hidden="true" />
        <strong>{t({ zh: "执行中提出的问题", en: "Asked during this run" })}</strong>
      </header>
      <p className="human-input-question">{decisionQuestion(pause, lang)}</p>
      {pause.options.length > 0 && (
        <ul className="human-input-record-options">
          {pause.options.map((option) => (
            <li key={option.id}>{decisionChoiceText(option.choiceId, option, lang, pause.facts).label}</li>
          ))}
        </ul>
      )}
      <footer>
        <small>{t({ zh: "这次执行已中断，无法再回答这个问题。发送新消息即可从中断处继续。", en: "This run was interrupted, so the question can no longer be answered. Send a new message to continue from where it stopped." })}</small>
      </footer>
    </section>
  );
}

"use client";

import { useEffect, useId, useMemo, useState } from "react";
import { ArrowRight, CircleHelp, ShieldAlert } from "lucide-react";
import type { ChatPause } from "../types";
import { summarizePauseFacts } from "../lib/pause-facts";
import { composeAnswer, type Answer } from "../lib/answer";
import { useT, type Phrase } from "@/shared/i18n";

function nodeLabel(value: string | undefined, t: (p: Phrase) => string) {
  if (!value) return t({ zh: "研究 agent", en: "Research agent" });
  return value
    .replace(/^_/, "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function copyFor(pause: ChatPause, t: (p: Phrase) => string) {
  if (pause.kind === "permission") return { title: { zh: "需要你授权", en: "Permission required" }, button: t({ zh: "确认并继续", en: "Confirm and continue" }) };
  if (pause.kind === "decision") return { title: { zh: "研究决策", en: "Research decision" }, button: t({ zh: "提交决策", en: "Submit decision" }) };
  return { title: { zh: "等你回答", en: "Needs your input" }, button: t({ zh: "回答并继续", en: "Answer and continue" }) };
}

/**
 * 图标已经画了一个警示符号，问句开头再来一个 ⚠️ 就是同一件事说两遍。
 * 按「开头的装饰性字符」剥，不按具体字符写名单 —— 换个 emoji 一样管用。
 */
function stripLeadingIcon(text: string) {
  return text.replace(/^[\p{Extended_Pictographic}️‍\s]+/u, "");
}

/**
 * **能点的**那张待答卡片。
 *
 * ## 它为什么没有 `resumable`，也没有可选的 `onAnswer`（2026-09-01）
 *
 * 这两个 prop 曾经让同一个组件表达两件不同的事：「请你回答」和「这里当时问过
 * 一个问题，但已经答不上了」。第二件事是**记录**，不是入口 —— 混在一起的代价
 * 是每个调用点都要自己判断该给哪一种，而判断散在 JSX 里，测不到。
 *
 * 现在记录归 `PausedRecord`（它连 `onAnswer` 这个 prop 都没有，结构上点不动），
 * 这个组件只从 `answer.via === "pause"` 构造出来 —— 也就是后端刚刚回答过
 * 「入口就是这张卡」。所以它永远是可交互的，没有"看起来能点其实点不了"
 * 这个状态可以表达。
 */
export function HumanInputPrompt({
  pause,
  submitting = false,
  onAnswer,
}: {
  pause: ChatPause;
  submitting?: boolean;
  /**
   * 收的是**构造好的** `Answer`，不是字符串加可选 choice。
   *
   * 这个组件不再判断"有没有东西可发"：那是 `composeAnswer` 的事，整个前端
   * 只在那一处判一次。此前这里、workspace、hook、后端 schema 各判一遍，
   * 8-31 改了前两处后两处原样 —— "只点选项不写附言"在 hook 里被静默吞掉，
   * 请求根本没出浏览器（2026-09-03，cuib）。类型上传字符串是编译错误。
   */
  onAnswer: (answer: Answer) => void;
}) {
  const t = useT();
  const titleId = useId();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [note, setNote] = useState("");
  useEffect(() => {
    setSelectedId(null);
    setNote("");
  }, [pause.askingRunId, pause.question]);
  const selected = useMemo(
    () => pause.options.find((option) => option.id === selectedId),
    [pause.options, selectedId],
  );
  // 选项**带真身份**（choiceId）时，身份由 choice_id 承担，正文就让位给人的附言
  // —— REVISE 这种"打回去**并说明为什么**"的选项，后半句靠附言表达，附言可空。
  // 没有身份的老 pause 维持原样：那时正文是唯一判据，两者只能取其一。
  const hasIdentity = Boolean(selected?.choiceId);
  const interactive = !submitting;
  // 「这次提交是什么」只在 composeAnswer 里算一次；null = 没有可发的东西。
  const answer = useMemo(
    () => composeAnswer({ selected, note, offerId: pause.offerId }),
    [selected, note, pause.offerId],
  );
  const canContinue = interactive && answer !== null;
  const labels = copyFor(pause, t);
  const Icon = pause.kind === "permission" ? ShieldAlert : CircleHelp;
  const hasOptions = pause.options.length > 0;
  const facts = useMemo(() => summarizePauseFacts(pause.facts), [pause.facts]);

  /**
   * 自由文本框：有选项时折起来，没选项时它就是主输入，直接展开。
   * 一个二选一的授权问题不需要一个 58px 高的输入框常驻在选项和按钮之间。
   */
  const selectedHasIdentity = hasIdentity;
  const freeform = (
    <textarea
      rows={2}
      value={note}
      // 打字不再清掉选中项：选项回传身份，这里回传理由，两件事不冲突。
      // 只有在选项**没有真身份**时才互斥 —— 那时正文是唯一判据。
      onFocus={() => { if (!selectedHasIdentity) setSelectedId(null); }}
      onChange={(event) => {
        setNote(event.target.value);
        if (!selectedHasIdentity) setSelectedId(null);
      }}
      placeholder={t({ zh: "把它需要的信息补上", en: "Add the information the research agent needs" })}
      disabled={!interactive}
    />
  );

  return (
    <section
      className={`human-input-prompt kind-${pause.kind}${interactive ? "" : " is-readonly"}`}
      aria-labelledby={titleId}
    >
      <header>
        <Icon size={15} aria-hidden="true" />
        <strong id={titleId}>{t(labels.title)}</strong>
        {/* 谁在问 —— 原来这条信息出现两次（标题下一行的整句 + 「Asked by X」一行）。
            压成标题右边一行 meta，说一次。 */}
        <span className="human-input-from">
          {pause.header && <mark>{pause.header}</mark>}
          {nodeLabel(pause.askingNodeType, t)}
        </span>
      </header>

      {/* 问句是这个面板存在的理由，给它最大的视觉权重，且不再挂「Question」标签 */}
      <p className="human-input-question">{stripLeadingIcon(pause.question)}</p>

      {/* 「为什么问你」。这些是呈递方附带的判断依据（review 失败没有、重试还有
          没有额度、要几个人批 …）—— 此前它们全留在呈递方进程里，人在面板上只
          看得见五个动作名，没有任何判据。这里不枚举字段名：上游加什么就显示
          什么（见 summarizePauseFacts）。 */}
      {facts.length > 0 && (
        <ul className="human-input-facts">
          {facts.map((fact) => (
            <li key={fact.key} className={`is-${fact.tone}`}>
              <b>{fact.label}</b>
              {fact.value && <span>{fact.value}</span>}
            </li>
          ))}
        </ul>
      )}

      {/* 能操作的东西紧跟问句。原来它排在第 5 块、面板顶部往下 368px 处
          （面板总高 578px）—— 要划过一整屏原始 payload 才看得见。 */}
      {hasOptions && (
        <div className="human-input-options" role="radiogroup" aria-label={t({ zh: "可选的回答", en: "Available responses" })}>
          {/* 推荐项排首位：无人值守模式会自动选它，UI 上也该第一眼看见 ——
              两边呈现同一个答案，人才能预期"我不管的话会发生什么"。 */}
          {[...pause.options].sort((a, b) => Number(b.recommended) - Number(a.recommended)).map((option) => {
            const checked = option.id === selectedId;
            return (
              <button
                key={option.id}
                type="button"
                role="radio"
                aria-checked={checked}
                aria-disabled={!interactive}
                disabled={!interactive}
                className={checked ? "selected" : ""}
                // 选中不再清空附言：人常常先写完理由再点"打回去"。
                onClick={() => setSelectedId(option.id)}
              >
                <i aria-hidden="true" />
                <span>
                  <strong>{option.label}{option.recommended && <em>{t({ zh: "推荐", en: "Recommended" })}</em>}</strong>
                  {option.description && <small>{option.description}</small>}
                </span>
              </button>
            );
          })}
        </div>
      )}

      {/* 次要材料折起来。context 原来常驻 224px（占面板 39%），里面是后端拼的
          原始 dump：工具名、命中类别、300 字符 payload、以及一句「批准请回复
          批准/同意/approve」—— 最后那句是给模型和纯文本客户端看的，在有真按钮
          的界面上就是把同一件事又说一遍。折起来即可，不必去解析它的措辞。 */}
      {pause.context && (
        // 高危审批的 context 就是**被批的那条命令** —— 后端注释原话："审批一个
        // 看不见内容的高危操作没有意义"。默认折叠时它形同不存在：2026-08-22
        // 实测连排查的人都两次没发现它在（innerText 不含折叠内容，肉眼也不会
        // 点开一个不起眼的 "Context"），差点当成"字段被吞"去修管道。
        // permission 类默认展开；其它类（decision/human_input 的次要材料）维持
        // 折叠 —— 那些的 context 是背景不是审批对象。
        <details className="human-input-detail" open={pause.kind === "permission"}>
          <summary>{t({ zh: "背景", en: "Context" })}</summary>
          <pre>{pause.context}</pre>
        </details>
      )}

      {/* 输入框常驻。我上一版把它折起来了 —— 那是把「少占地方」当成了目标本身：
          选项之外还想说点什么，是这个面板的正常用法，不该每次先点开一层。 */}
      <label className="human-input-freeform">
        <span>{hasOptions ? t({ zh: "或者自己写一个答案", en: "Or write a different answer" }) : t({ zh: "你的回答", en: "Your answer" })}</span>
        {freeform}
      </label>

      <footer>
        {/* 删掉的是标题下那句「X is waiting for approval.」—— 它和标题说的是
            同一件事。这一句留着：它说的不是"有个待办"，而是"**现在什么都没在
            发生**"，高危审批里这条保证有真实价值，且它就在按钮旁边，正是做
            决定的地方。 */}
        <small>{t({ zh: "你不点，它就一直等着。", en: "Nothing continues until you confirm." })}</small>
        <button
          type="button"
          disabled={!canContinue}
          // 按钮亮着就一定有构造好的答复（同一个 `answer` 决定 disabled）。
          // 这里不再拼字符串、不再判 choiceId：选项身份 / 附言 / 老式文案的
          // 取舍全在 composeAnswer 里，组件只把构造结果交出去。
          onClick={() => { if (answer) onAnswer(answer); }}
        >
          {submitting ? "Continuing…" : labels.button}<ArrowRight size={13} />
        </button>
      </footer>
    </section>
  );
}

import type {
  RunActivityDetail,
  RunActivityNarration,
  RunActivityStep,
  RunActivitySaid,
  RunActivityTool,
  RunActivityWorkspaceChange,
} from "./run-activity-detail";
import { audienceOf } from "./event-audience.ts";
import { belongsToRun } from "./run-scope.ts";
import { parseRunLineage } from "./research-map.ts";

/**
 * 把一次 run 的叙述和动作**按发生顺序**排回一条时间线。
 *
 * ## 为什么（wangd 2026-08-13 试用）
 *
 * > 「输出的文字都在上面，然后执行的指令、子节点记录啥的都在最下面，
 * >   应该是类似你自己这种吧，一段话，一段 tool call 细节啥的。」
 *
 * 之前的渲染把同一条事件流拆成两坨：叙述全部提到顶上，动作全部折进底下的
 * 「Research activity」。于是"它说了要干什么"和"它干了什么"隔了一整屏 ——
 * 第 3 轮那句"前两轮查得太宽泛，换精准词"回答不了任何问题，因为它旁边
 * 根本没有那次检索。
 *
 * 事件流本来就带 sequence，这里只是把顺序**还给**渲染层：
 * - 顶层叙述 → 独立段落；
 * - 顶层工具 → 各自一条（组的外壳拆掉 —— 壳只有"N recorded actions"一个
 *   信息，代价是把动作和它的解释隔开）；
 * - 子节点 run → 仍是一个可折叠组（它是委派出去的一整段工作），组内再按
 *   同样的规则交错。
 *
 * 这里只排顺序，怎么折叠、失败的动作去哪，是组件的事。
 */
export type RunTimelineItem =
  //: `child` = 这句话**引出的那个子节点**。派发语和子节点状态卡说的是同一件
  //: 事（"我要起 hypothesis" / "hypothesis 进行中"），分成两行读者要先确认它们
  //: 是不是同一个（wangd 2026-08-19：「这三个是重复的」）。合成一条：话尾那个
  //: `→ hypothesis` 直接带上状态并可点开右栏。
  | { kind: "said"; sequence: number; said: RunActivitySaid; repeats?: number; child?: RunActivityStep }
  | { kind: "narration"; sequence: number; narration: RunActivityNarration }
  | { kind: "tool"; sequence: number; tool: RunActivityTool }
  | { kind: "tools"; sequence: number; tools: RunActivityTool[] }
  | { kind: "workspace"; sequence: number; change: RunActivityWorkspaceChange }
  | { kind: "child"; sequence: number; step: RunActivityStep };

export type ChildTimelineItem =
  | { kind: "narration"; sequence: number; narration: RunActivityNarration }
  | { kind: "tool"; sequence: number; tool: RunActivityTool }
  | { kind: "workspace"; sequence: number; change: RunActivityWorkspaceChange };

/**
 * 跑完的**连续**动作收成一行「Ran N commands」；正在跑的和落单的照旧展开。
 *
 * ## 三档，判据是状态与数量（wangd 2026-08-18 指着参考图纠正）
 *
 *   · **正在跑** → 显示它自己的标题（`Waiting for CI on PR 492`）。收束是
 *     "这一批干完了"的总结，正在跑的东西没有总结可言，收起来等于把当下
 *     正在发生的事藏了；
 *   · **跑完、连续 ≥2 条** → 一行 `Ran N commands`，点开看每一条；
 *   · **跑完、只有 1 条** → 显示它自己的标题（`Merged PR 485`）。
 *     "Ran 1 commands" 既难看又比原标题信息少。
 *
 * 收束只跨**相邻**的动作：中间只要夹了一句话、一张卡、一个子节点，就断开
 * 重新计数 —— 那句话正是它们的分组理由（参考图里每一组前面都有一句）。
 */
const IN_FLIGHT: ReadonlySet<RunActivityTool["status"]> = new Set(["running", "retrying"]);

export function collapseFinishedToolRuns(items: RunTimelineItem[]): RunTimelineItem[] {
  const out: RunTimelineItem[] = [];
  let batch: RunActivityTool[] = [];
  const flush = () => {
    if (batch.length === 0) return;
    if (batch.length === 1) {
      out.push({ kind: "tool", sequence: batch[0].sequence, tool: batch[0] });
    } else {
      out.push({ kind: "tools", sequence: batch[0].sequence, tools: batch });
    }
    batch = [];
  };
  for (const item of items) {
    if (item.kind === "tool" && !IN_FLIGHT.has(item.tool.status)) {
      batch.push(item.tool);
      continue;
    }
    flush();
    out.push(item);
  }
  flush();
  return out;
}

/**
 * 只留**对话受众**的条目 —— 左栏用。
 *
 * 生命周期/计量类条目按事件自己声明的受众判（见 event-audience.ts）。而
 * 独白、工具、said 这些"取决于是谁的 run"的条目，到得了主线的**只有调度器
 * 自己的**（run 作用域在 buildRunTimeline 已经分好了），所以在这里一律属于
 * 对话 —— 拿 audience 表的默认值来判它们，就是拿"右栏该怎么画"回答"左栏
 * 该不该画"，两个问题一个判据（[[两个视图两个问题]]）。
 */
export function conversationOnly(items: RunTimelineItem[]): RunTimelineItem[] {
  return items.filter((item) => {
    if (item.kind === "said") {
      return audienceOf(item.said.receipt ? "interrupt.acknowledged" : "orchestrator.said") === "conversation";
    }
    // 到得了主线的独白**只有调度器自己的**（子节点的进它自己的组，见
    // buildRunTimeline 的 run 作用域）—— 那是"它在跟你说它在干什么"，属于
    // 对话。event-audience 表里 "agent.message": "process" 记的是**默认归属**
    // （右栏、执行历史里子节点独白按过程完整呈现），表自己的注释写明主线
    // 归属由 run 作用域决定 —— 可这里曾拿默认值当判据，audienceOf 是常量
    // process，于是恒 false：调度器整轮说的每一句话在左栏一个字都不剩。
    // 紧挨着下面的 tool 分支就是同一个 bug 修对的那一半，narration 没跟上。
    if (item.kind === "narration") return true;
    if (item.kind === "workspace") return audienceOf("workspace.changed") === "conversation";
    // 子节点卡是"这一轮派了谁"的对话级摘要（细节在右栏），永远留在对话里。
    if (item.kind === "child") return true;
    // 到得了主线的工具调用**只有调度器自己的**（子节点的进它自己的组，见
    // buildRunTimeline）—— 那是"它在干活"，属于对话，和子节点内部过程不是
    // 一回事。2026-08-18 一度把两者一起判成过程，于是调度器在左栏变成了哑巴。
    return true;
  });
}

export function buildRunTimeline(
  activity: Pick<RunActivityDetail, "steps" | "narration">
    & { workspaceChanges?: RunActivityWorkspaceChange[]; said?: RunActivitySaid[] },
  rootRunId: string | undefined,
): RunTimelineItem[] {
  const childRunIds = new Set(
    activity.steps
      .filter((step) => step.kind === "child" && step.runId)
      .map((step) => step.runId as string),
  );
  const items: RunTimelineItem[] = [];
  // 派发前对用户说的那句话 —— **不分层级**，一律上主线。
  //
  // 这里原本写的是 `childRunIds.has(said.runId) → continue`，本意大概是
  // "子节点的话进子节点组"。但 said 不是节点内部独白（那是 narration），
  // 它是一个节点在派出下一个节点之前**对用户**说的话。而
  // `buildChildTimeline` 根本不渲染 said —— 于是被 continue 掉的那句话
  // 渲染在任何地方之外，直接消失。注释写着"永远上主线"，代码在删它。
  //
  // 实测（会话 2276bce7）：hypothesis 派 literature 之前说的
  // 「先做文献调研：项目里还没有任何证据基础，我需要先摸清…」整句不见了。
  // 用户看到的是 hypothesis 组下面凭空冒出一个 literature 组，于是问
  // 「为啥这两个是并列的呀」—— 解释被删了，剩下的就只有并列。
  //
  // 越深层节点说的这句话越该上主线：它是"现在在干啥"的唯一来源，而子节点
  // 组默认是收起的。
  //
  // 边界仍然是边界：这一轮已知的 run 就是根 run 和它派出去的那些，别人家的
  // 话混进来说明上游过滤错了，照样挡掉（见 belongsToRun 的说明）。
  const knownRunIds = new Set(rootRunId ? [...childRunIds, rootRunId] : childRunIds);
  // 连着说同一句话 → 收成一条带次数。
  //
  // 派发宣告曾经发在**所有闸门之前**，被挡下来的派发也留了宣告 —— 实测一个
  // 会话里 writing 说了 17 遍、真跑了 1 次（根因已在 run_node 修：宣告移到
  // 闸门之后）。存量会话里那些重复还在，而且**合法的重复也存在**（真失败真
  // 重派）。所以这里不去重、不判真假，只把**连续相同**的收成一条 ×N ——
  // 如实，且读得下去。中间隔了别的事再说同一句，那是两回事，不合并。
  const saids = [...(activity.said ?? [])]
    .filter((said) => !said.runId || knownRunIds.size === 0 || knownRunIds.has(said.runId))
    .sort((a, b) => a.sequence - b.sequence);
  for (const said of saids) {
    const previous = items.at(-1);
    // 回执不按正文去重（正文恒空，两条不同插话的回执会被错并成 ×2）。
    if (previous?.kind === "said" && !said.receipt && !previous.said.receipt
      && previous.said.text === said.text) {
      previous.repeats = (previous.repeats ?? 1) + 1;
      continue;
    }
    items.push({ kind: "said", sequence: said.sequence, said });
  }
  for (const narration of activity.narration) {
    // 子节点的话进它自己的组；只有顶层 run 的话（或归属不明的旧记录）上主线。
    if (narration.runId && childRunIds.has(narration.runId)) continue;
    if (narration.runId && rootRunId && !belongsToRun({ runId: narration.runId }, rootRunId)) continue;
    items.push({ kind: "narration", sequence: narration.sequence, narration });
  }
  for (const change of activity.workspaceChanges ?? []) {
    // 同叙述：子节点的文件改动进它自己的组，主线只放顶层 run 的。
    if (change.runId && childRunIds.has(change.runId)) continue;
    if (change.runId && rootRunId && !belongsToRun({ runId: change.runId }, rootRunId)) continue;
    items.push({ kind: "workspace", sequence: change.sequence, change });
  }
  for (const step of activity.steps) {
    if (step.kind === "child") {
      items.push({ kind: "child", sequence: step.sequence, step });
      continue;
    }
    for (const tool of step.tools) {
      items.push({ kind: "tool", sequence: tool.sequence, tool });
    }
  }
  items.sort((a, b) => a.sequence - b.sequence);
  return foldChildIntoItsDispatchLine(items);
}

/**
 * 把子节点卡折进**引出它的那句派发语**。
 *
 * 派发语（"我先启动 Analysis 节点来开题…→ hypothesis"）和紧随其后的状态卡
 * （"hypothesis 进行中 · 14 actions"）说的是同一件事，读者却要先确认它们是不是
 * 同一个（wangd 2026-08-19：「这三个是重复的」）。而且话尾那个 `→ hypothesis`
 * 点不动，只有下面那张卡能点 —— 看起来像的那个不是能用的那个。
 *
 * 判据是**声明的关系**，不是相邻：`said.aboutNodeType` 就是这句话在派谁。
 * 只折叠紧跟其后、类型对得上的那一个 —— 同类型的第二次派发（重跑）另起一行，
 * 它是一件新的事。
 */
function foldChildIntoItsDispatchLine(items: RunTimelineItem[]): RunTimelineItem[] {
  const out: RunTimelineItem[] = [];
  //: nodeType → 这条时间线上它的卡，按开跑顺序。同一个节点重派会复用 run id，
  //: 但 `projectRunActivity` 按派发段分卡（2026-09-02）—— 一次派发一张，各戴
  //: 各的结局；这里要挑的是"这句话派出的那一次"。
  const cardsByNodeType = new Map<string, RunActivityStep[]>();
  for (const item of items) {
    const previous = out.at(-1);
    if (item.kind === "child") {
      const nodeType = childNodeTypeOf(item.step);
      if (nodeType) cardsByNodeType.set(nodeType, [...(cardsByNodeType.get(nodeType) ?? []), item.step]);
      if (
        previous?.kind === "said"
        && !previous.child
        && previous.said.aboutNodeType
        && nodeType === previous.said.aboutNodeType
      ) {
        previous.child = item.step;
        continue;
      }
    }
    out.push(item);
  }
  // ── 第二次派发的那句话也得有状态可读（wangd 2026-08-22）────────────────
  //
  // 派发语和它的卡不相邻时（中间隔着别的话），`→ literature` 找不到 child，
  // 退化成 `is-static` —— 一段灰字、点不开、
  // 不说进行中也不说完成。2026-08-22 截图里最新那几行全是这个样子：调度器
  // 刚说完"我让 literature 去抓官方数据"，紧跟着一个死掉的灰标签。
  //
  // 挂上它自己那一段的卡是**如实的**。不挂才是在说谎（"这次派发没有任何运行"）。
  // 派发语在前、它派出的那段在后：取这句话之后最先开跑的那张；话说完没再开跑
  // （派发被闸挡下等）就退回最近那张。
  for (const item of out) {
    if (item.kind !== "said" || item.child || !item.said.aboutNodeType) continue;
    const cards = cardsByNodeType.get(item.said.aboutNodeType) ?? [];
    const card = cards.find((c) => c.sequence >= item.sequence) ?? cards.at(-1);
    if (card) item.child = card;
  }
  return out;
}

function childNodeTypeOf(step: RunActivityStep): string {
  return (step.runId && parseRunLineage(step.runId)?.nodeType) || step.title || "";
}

/**
 * 去掉「回复回声」：与这一轮的回复正文一字不差的顶层叙述不再上时间线。
 *
 * ## 为什么（2026-08-17，会话 c9deb4f2）
 *
 * 调度器一轮收尾的那段话走**两个通道**送达前端：回复契约 → 持久的
 * assistant 消息正文；transcript 摄取 → `agent.message` 叙述事件。单轮 turn
 * 里这俩就是同一句话 —— 用户看到回复正文下面紧跟一条一模一样的「第 1 轮」。
 *
 * 判等就是判身份：两条都源自同一条 llm_response，逐字相同不是巧合。中间轮次
 * 的叙述与回复不同，不受影响 —— 它们只存在于事件流里（刷新后 assistant 消息
 * 只有终稿），必须继续上时间线。
 *
 * 回复缺失（刷新后旧会话、终稿没送到）时原样保留 —— 此时叙述是那段话的
 * 唯一记录，删了内容就丢了。
 */
export function withoutReplyEcho(
  items: RunTimelineItem[],
  replyText: string | undefined | null,
): RunTimelineItem[] {
  const reply = replyText?.trim();
  if (!reply) return items;
  return items.filter((item) =>
    item.kind !== "narration" || item.narration.text.trim() !== reply,
  );
}

/** 子节点组内部的时间线：它自己的叙述、工具与文件改动，同样按发生顺序交错。 */
/** 锚到某条消息的答复/回执在**那条消息的槽位**里渲染，不占 run 窗口 ——
 * 否则同一句话画两遍，或只画在错的位置。没锚的照旧：老事件和 CLI 投递没有
 * message_id，不能让它们无处可去。
 *
 * 判据只认**消息锚**（`repliesToMessageId`）。派发线（带 aboutNodeType、尾巴上
 * 折着子节点卡的那句）在一轮之内说出来时只带提交号，不是回声 —— 2026-09-02
 * 实测它被当回声滤掉，子节点卡随之消失（见 run-activity-detail.ts 的
 * RunActivitySaid.repliesToMessageId 注释）。这个函数从组件里抽出来，是为了
 * 能拿真实事件页当输入做变异测试；组件里的 JSX 链测不到。 */
/** 这句话是不是**派发线** —— 它引出了一个子节点。
 *
 * 一条规则，三个消费者：run 窗口的让位（`withoutMessageAnchoredSaids`）、
 * 折叠（`foldChildIntoItsDispatchLine`）、以及组件里画不画 `→ node` 那个胶囊。
 * 此前它只是 `withoutMessageAnchoredSaids` 里的一个内联布尔，组件那边各判一遍
 * —— 于是插话槽位里的**答复**也被画上了 `→ experiment`，而答复没有子节点卡可
 * 折，标签退化成 `is-static`：一段灰字、点不开、不说进行中也不说完成（#776）。
 *
 * **答复不是派发。** `aboutNodeType` 在一条答复上说的是"这句话在讲哪个节点"
 * （"我把这四条注入到正在跑的 experiment 节点"），不是"这句话派了 experiment"。
 * 给它画一个派发affordance 是在承诺一个不存在的去处。
 *
 * 另一条修法是让槽位里的答复也去折一张卡 —— 那要靠"这句话之后最先开跑 / 最近
 * 的那张"去**猜**它指哪一次派发。猜出来的关系点开就是错的卡；不画才是如实的。
 */
export function isDispatchLine(said: RunActivitySaid, child?: RunActivityStep): boolean {
  // 答复由它锚的那条消息的槽位负责画，它本身不派发任何东西。
  if (said.repliesToMessageId) return false;
  return Boolean(said.aboutNodeType) || Boolean(child);
}

export function withoutMessageAnchoredSaids(items: RunTimelineItem[]): RunTimelineItem[] {
  return items.filter((item) => {
    if (item.kind !== "said") return true;
    // 真锚（回答某条消息）：那条消息的槽位负责画它。
    if (item.said.repliesToMessageId) return false;
    // 只带提交号的**纯答复文字**：它就是这一趟的回复正文，消息侧已经有一份
    // （老数据没有专用锚字段时靠这条让位，见 run-activity-detail.test 的用例）。
    // 派发线不是回声：它记录的是"派了谁"，尾巴上折着子节点卡 —— 留在 run 窗口。
    if (item.said.submissionId && !isDispatchLine(item.said, item.child)) return false;
    return true;
  });
}

/** 锚到某条消息的回执与答复 —— 那条消息的槽位要画的全部内容。
 *
 * 与 withoutMessageAnchoredSaids 是同一条规则的两面：那边把它们从 run 窗口里
 * 让出去，这边把它们收进消息槽位。两边共用一个判据（真锚优先；老数据没有专用
 * 锚字段时，提交号恰好就是那条消息的 id —— 调用方知道 messageId，所以是精确
 * 匹配，不是猜）。抽成纯函数是为了能拿真实事件页做变异测试：#766 的现场就是
 * 让位做了、接住没做 —— 两头落空，而 JSX 里的判断谁都测不到。
 *
 * 回执是"还没人说话"期间的占位：模型一开口就让位；因此有 replies 时不返回
 * receipt。 */
export function saidsAnchoredTo(
  said: readonly RunActivitySaid[],
  messageId: string,
): { replies: RunActivitySaid[]; receipt?: RunActivitySaid } {
  const anchored = said.filter(
    (item) => item.repliesToMessageId === messageId || item.submissionId === messageId,
  );
  const replies = anchored.filter((item) => !item.receipt);
  const receipt = replies.length ? undefined : anchored.findLast((item) => item.receipt);
  return { replies, receipt };
}

export function buildChildTimeline(
  step: RunActivityStep,
  narration: readonly RunActivityNarration[],
  workspaceChanges: readonly RunActivityWorkspaceChange[] = [],
): ChildTimelineItem[] {
  const items: ChildTimelineItem[] = [];
  for (const item of narration) {
    if (!item.runId || !step.runId || !belongsToRun({ runId: item.runId }, step.runId)) continue;
    items.push({ kind: "narration", sequence: item.sequence, narration: item });
  }
  for (const change of workspaceChanges) {
    if (!change.runId || !step.runId || !belongsToRun({ runId: change.runId }, step.runId)) continue;
    items.push({ kind: "workspace", sequence: change.sequence, change });
  }
  for (const tool of step.tools) {
    items.push({ kind: "tool", sequence: tool.sequence, tool });
  }
  return items.sort((a, b) => a.sequence - b.sequence);
}

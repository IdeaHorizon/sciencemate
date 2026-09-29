import type { ChatMessage } from "../types";

export type MessageRunOwner = {
  messageId: string;
  runId: string;
};

/** One owner per distinct Run, always the Run's final attributable message. */
export function messageRunOwners(messages: readonly ChatMessage[]): MessageRunOwner[] {
  const owners = new Map<string, string>();
  for (const message of messages) {
    const runId = message.runId?.trim();
    if (runId) owners.set(runId, message.id);
  }
  return [...owners].map(([runId, messageId]) => ({ messageId, runId }));
}

export type MessageRunSegment = {
  messageId: string;
  runId: string;
  /** 这条消息名下渲染 run 在 (start, end) sequence 窗口内的活动；end=null 表示开放尾窗。 */
  window: { start: number; end: number | null };
  /**
   * 这条消息是**插进**这条 run 的（不是开启它的那条）。挂载点身份不变 ——
   * 它照样切走自己那段窗口（#766 之前的假设"插话不是挂载点"与上面的规则
   * 自相矛盾，SessionWorkspace 里那条槽位分支因此永远走不到）；多出来的是：
   * 锚到这条消息的回执/答复得画在它下面，这件事由它的窗口实例一并负责。
   */
  interjection: boolean;
};

/**
 * 把一个 run 的活动按**消息的 sequence 窗口**切段，而不是整体挂在最后一条
 * 消息下面。
 *
 * 消息和执行事件共用同一个会话级 sequence 空间，所以"这段活动发生在哪两条
 * 消息之间"是库里的事实，不需要猜。旧的 last-writer-wins 归属把整个 run
 * （几百条动作）压到该 run 的最后一条消息下面 —— 于是用户看到的是：所有
 * 编排器的话在上面，所有子节点活动堆在最底下（2026-08-17 用户原话："这个
 * 显示非常的不合理"）。
 *
 * ⚠️ 这句"共用同一个空间"曾经**是假的**，而且假了很久：库里有两个各自从 0
 * 开始的计数器（`next_message_sequence` / `next_event_sequence`）。两个号在
 * 类型上一模一样，拿来比大小不会报错，只会静默地把顺序画错 —— 实测会话
 * e46448f0 消息号 1–5、事件号 1–869，于是一条 run 的全部活动统统落进第一条
 * 消息的开放尾窗，13:32 的动作画在 13:17 的对话上面。
 *
 * 现在两者从同一个发生器领号（后端 `sessions.next_sequence`），而且这件事有
 * 一道绑真库的测试守着（`test_one_session_one_sequence.py`）——前提写在注释里
 * 是守不住的，得有东西在它被改坏时变红。
 *
 * 规则：
 * - run R 的挂载点 = 所有带 runId===R 的消息，外加紧挨着第一个挂载点之前的
 *   那条消息（turn 往往以 user 消息开头，R 在第一句 assistant 话之前就开始
 *   干活了，那些事件落在 user 消息的窗口里）。
 * - 第 k 个挂载点的窗口 = (它的 sequence, 下一个挂载点的 sequence)；最后一个
 *   挂载点的窗口开放到 ∞ —— 后台继续跑的 run 在自己的位置继续追加，新消息
 *   照常出现在更下面。
 * - 没有 sequence 的消息（本轮乐观插入的）用"已知最大 sequence"当窗口起点：
 *   它们必然在尾部，活动也必然在尾部。
 */
export function messageRunSegments(messages: readonly ChatMessage[]): MessageRunSegment[] {
  const maxKnownSequence = messages.reduce(
    (max, message) => (typeof message.sequence === "number" && message.sequence > max ? message.sequence : max),
    Number.NEGATIVE_INFINITY,
  );
  const startOf = (message: ChatMessage, index: number): number => {
    if (typeof message.sequence === "number") return message.sequence;
    // 乐观消息：窗口从最后一条持久消息之后开始。
    void index;
    return maxKnownSequence;
  };

  // 这个 run 名下的**每一条**消息都是一个挂载点，各自领走自己那段窗口。
  //
  // 这里一度只认第一条（"插话不该夺走还在跑的活动的归属"，wangd 2026-08-18：
  // 「我输入之后，正在运行的内容还是出现在我输入的直接下方，按理说应该在上面
  // 才对」）。那个收缩当时是对的 —— 因为**窗口本身没有意义**：消息号和事件号
  // 来自两个各自从 0 开始的计数器，切出来的区间是垃圾，只能整段挂在一处。
  //
  // 序号统一之后（后端 `sessions.next_sequence`），窗口重新是可信的，而它本来
  // 就解决了那个诉求：已经发生过的活动 sequence 小于插话，落在**更早**那段
  // 窗口里，自然渲染在插话上面；之后发生的落在后一段，渲染在下面。这正是
  // "显示啥就是啥"。
  //
  // 反过来说，只认第一条在真数据上就是 2026-08-23 那个症状：一条 run 名下四条
  // 消息、几百条活动，全部压在第一条消息的开放尾窗里 —— 13:32 的动作画在
  // 13:17 的对话上面。
  const mountsByRun = new Map<string, number[]>();
  messages.forEach((message, index) => {
    const runId = message.runId?.trim();
    if (!runId) return;
    mountsByRun.set(runId, [...(mountsByRun.get(runId) ?? []), index]);
  });

  const segments: MessageRunSegment[] = [];
  for (const [runId, mounts] of mountsByRun) {
    const first = mounts[0];
    const withLead =
      first > 0
      && typeof messages[first]?.sequence === "number"
      && typeof messages[first - 1]?.sequence === "number"
      && !messages[first - 1].runId?.trim()
        ? [first - 1, ...mounts]
        : [...mounts];
    const seen = new Set<string>();
    withLead.forEach((messageIndex, k) => {
      const message = messages[messageIndex];
      const start = startOf(message, messageIndex);
      const nextIndex = withLead[k + 1];
      const end =
        nextIndex === undefined ? null : startOf(messages[nextIndex], nextIndex);
      // 两个挂载点窗口相同（都没 sequence 时会发生）→ 只保留后一个，
      // 否则同一段活动渲染两遍。
      const key = `${start}→${end ?? "∞"}`;
      if (seen.has(key)) {
        const previous = segments.findLastIndex(
          (segment) => segment.runId === runId && `${segment.window.start}→${segment.window.end ?? "∞"}` === key,
        );
        if (previous >= 0) segments.splice(previous, 1);
      }
      seen.add(key);
      segments.push({
        messageId: message.id,
        runId,
        window: { start, end },
        interjection: messageIndex !== mounts[0] && mounts.includes(messageIndex),
      });
    });
  }
  return segments;
}

/**
 * Find the most recent persisted message that explicitly owns a Run.
 *
 * commandId and message ordering must never be used to invent this relation:
 * failed turns may end with a durable system message, while older messages and
 * legacy transcripts have no runId at all.
 */
export function latestMessageRun(messages: readonly ChatMessage[]): MessageRunOwner | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    const runId = message.runId?.trim();
    if (runId) return { messageId: message.id, runId };
  }
  return null;
}

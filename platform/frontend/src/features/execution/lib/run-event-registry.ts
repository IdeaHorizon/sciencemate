import { belongsToRun } from "./run-scope.ts";
import type { ExecutionEvent } from "./execution-event.ts";
import type {
  CanonicalRunStreamEnd,
  TransientRunToken,
  TransientRunTokenGap,
} from "./run-event-stream.ts";
import { mergeCanonicalRunEvents } from "./run-event-reader.ts";

export type RunEventRegistrySnapshot = {
  events: ExecutionEvent[];
  assistantDraft: string;
  assistantDraftIncomplete: boolean;
  phase: "connecting" | "live" | "reconnecting" | "terminal" | "error";
  error?: unknown;
  end?: CanonicalRunStreamEnd;
};

export type RunEventRegistryOptions = {
  key: string;
  sessionId: string;
  runId: string;
  initialEvents: readonly ExecutionEvent[];
  readStream: (
    afterSequence: number,
    signal: AbortSignal,
    onEvent: (event: ExecutionEvent) => void,
    onToken: (token: TransientRunToken) => void,
    onTokenGap: (gap: TransientRunTokenGap) => void,
  ) => Promise<CanonicalRunStreamEnd>;
  readFallback: (afterSequence: number) => Promise<ExecutionEvent[]>;
  onEvents: (events: ExecutionEvent[]) => void;
  onTerminal: (end: CanonicalRunStreamEnd) => void | Promise<void>;
  maxReconnectAttempts?: number;
  waitBeforeReconnect?: (attempt: number, signal: AbortSignal) => Promise<void>;
};

type RegistryEntry = {
  options: RunEventRegistryOptions;
  snapshot: RunEventRegistrySnapshot;
  listeners: Set<(snapshot: RunEventRegistrySnapshot) => void>;
  controller: AbortController;
  running: boolean;
  /**
   * 连续失败的次数，**挂在条目上而不是一次 follow 的局部变量上**。
   * 从前每次 follow 从 0 数起：五次失败进 error 之后，任何一次重渲染的 acquire
   * 都会再起一次 follow、再数五次 —— 一条永远契约失败的流就这样以渲染的节奏
   * 无限重开（2026-09-09 node20）。退避与上限必须跨重启生效；收到一条事件才清零。
   */
  reconnectFailures: number;
};

function defaultReconnectDelay(attempt: number, signal: AbortSignal) {
  const milliseconds = Math.min(4_000, 250 * (2 ** (attempt - 1)));
  return new Promise<void>((resolve, reject) => {
    const timeout = globalThis.setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      globalThis.clearTimeout(timeout);
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

/**
 * App-lifetime registry for active durable Run subscriptions. Releasing a page
 * listener never owns or aborts the transport, so ordinary route changes keep
 * the same observer and the Query cache continues receiving in-memory events.
 */
export class CanonicalRunEventRegistry {
  private entries = new Map<string, RegistryEntry>();

  acquire(
    options: RunEventRegistryOptions,
    listener: (snapshot: RunEventRegistrySnapshot) => void,
  ) {
    let entry = this.entries.get(options.key);
    if (!entry) {
      const events = mergeCanonicalRunEvents(
        [],
        options.initialEvents,
        options.sessionId,
        options.runId,
      );
      entry = {
        options,
        snapshot: {
          events,
          assistantDraft: "",
          assistantDraftIncomplete: false,
          phase: "connecting",
        },
        listeners: new Set(),
        controller: new AbortController(),
        running: false,
        reconnectFailures: 0,
      };
      this.entries.set(options.key, entry);
    } else {
      entry.options = options;
      entry.snapshot = {
        ...entry.snapshot,
        events: mergeCanonicalRunEvents(
          entry.snapshot.events,
          options.initialEvents,
          options.sessionId,
          options.runId,
        ),
      };
    }

    entry.listeners.add(listener);
    options.onEvents(entry.snapshot.events);
    listener(entry.snapshot);
    const maximum = options.maxReconnectAttempts ?? 5;
    if (
      !entry.running
      && entry.snapshot.phase !== "terminal"
      && entry.reconnectFailures < maximum
    ) {
      entry.controller = new AbortController();
      entry.running = true;
      void this.follow(entry);
    }

    return () => {
      entry?.listeners.delete(listener);
    };
  }

  snapshot(key: string) {
    return this.entries.get(key)?.snapshot;
  }

  stopAll(reason = "Run event registry stopped") {
    for (const entry of this.entries.values()) {
      entry.controller.abort(new DOMException(reason, "AbortError"));
    }
    this.entries.clear();
  }

  private publish(entry: RegistryEntry, snapshot: RunEventRegistrySnapshot) {
    entry.snapshot = snapshot;
    entry.options.onEvents(snapshot.events);
    for (const listener of entry.listeners) listener(snapshot);
  }

  private async follow(entry: RegistryEntry) {
    const maximum = entry.options.maxReconnectAttempts ?? 5;
    let cursor = entry.snapshot.events.at(-1)?.sequence ?? 0;

    while (!entry.controller.signal.aborted && entry.reconnectFailures < maximum) {
      this.publish(entry, { ...entry.snapshot, phase: entry.reconnectFailures ? "reconnecting" : "live", error: undefined });
      try {
        const end = await entry.options.readStream(
          cursor,
          entry.controller.signal,
          (event) => {
            const events = mergeCanonicalRunEvents(
              entry.snapshot.events,
              [event],
              entry.options.sessionId,
              entry.options.runId,
            );
            cursor = events.at(-1)?.sequence ?? cursor;
            entry.reconnectFailures = 0;
            // 这一轮的叙述已经成为**记录**（agent.message 落了事件），草稿就
            // 只剩下一轮还没说完的话。不清零的话，整个 run 的叙述会在草稿里
            // 越攒越长地挂在页面上，和时间线里的记录逐字重复。
            // 与 token 累积同一把尺子（belongsToRun 覆盖整棵子树）：子节点的
            // 叙述 token 也进这个草稿，那它落库时同样要把草稿翻页。
            const recordedNarration = event.kind === "agent.message"
              && belongsToRun(event, entry.options.runId);
            this.publish(entry, {
              ...entry.snapshot,
              events,
              assistantDraft: recordedNarration ? "" : entry.snapshot.assistantDraft,
              phase: "live",
              error: undefined,
            });
          },
          (token) => {
            if (!belongsToRun(token, entry.options.runId)) return;
            if (entry.snapshot.assistantDraftIncomplete) return;
            this.publish(entry, {
              ...entry.snapshot,
              assistantDraft: entry.snapshot.assistantDraft + token.text,
              phase: "live",
            });
          },
          (gap) => {
            if (!belongsToRun(gap, entry.options.runId)) return;
            this.publish(entry, {
              ...entry.snapshot,
              assistantDraft: "",
              assistantDraftIncomplete: true,
              phase: "live",
            });
          },
        );
        this.publish(entry, {
          ...entry.snapshot,
          assistantDraft: "",
          assistantDraftIncomplete: false,
          phase: "terminal",
          end,
        });
        await entry.options.onTerminal(end);
        entry.running = false;
        return;
      } catch (error) {
        if (entry.controller.signal.aborted) {
          entry.running = false;
          return;
        }
        entry.reconnectFailures += 1;
        let failure: unknown = error;
        try {
          const missed = await entry.options.readFallback(cursor);
          if (missed.length > 0) {
            const events = mergeCanonicalRunEvents(
              entry.snapshot.events,
              missed,
              entry.options.sessionId,
              entry.options.runId,
            );
            cursor = events.at(-1)?.sequence ?? cursor;
            this.publish(entry, { ...entry.snapshot, events, phase: "reconnecting" });
          }
        } catch (fallbackError) {
          failure = fallbackError;
        }
        if (entry.reconnectFailures >= maximum) {
          this.publish(entry, { ...entry.snapshot, phase: "error", error: failure });
          entry.running = false;
          return;
        }
        try {
          await (entry.options.waitBeforeReconnect ?? defaultReconnectDelay)(
            entry.reconnectFailures,
            entry.controller.signal,
          );
        } catch {
          entry.running = false;
          return;
        }
      }
    }
    entry.running = false;
  }
}

export const canonicalRunEventRegistry = new CanonicalRunEventRegistry();

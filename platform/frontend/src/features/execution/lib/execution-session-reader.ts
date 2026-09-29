import {
  ExecutionReadContractError,
  type DurableDecision,
  type ExecutionReadClient,
} from "./execution-read-client.ts";
import {
  LiveEventReconciler,
  type ReconcilerSnapshot,
} from "./live-event-reconciler.ts";

export type ExecutionSessionRead = {
  snapshot: ReconcilerSnapshot;
  decisions: DurableDecision[];
};

export type ExecutionSessionPollingOptions = {
  reader: ExecutionSessionReader;
  intervalMs?: number;
  onRead: (read: ExecutionSessionRead) => void;
  onError: (error: unknown) => void;
  setTimer?: (callback: () => void, intervalMs: number) => unknown;
  clearTimer?: (timer: unknown) => void;
};

/** Reuses one reconciler for initial afterSequence=0 and every incremental pull. */
export class ExecutionSessionReader {
  readonly reconciler: LiveEventReconciler;
  private client: ExecutionReadClient;
  private sessionId: string;
  private expectedProjectId: string;
  private pendingSync?: Promise<ExecutionSessionRead>;

  constructor({
    client,
    sessionId,
    expectedProjectId,
    reconciler = new LiveEventReconciler(),
  }: {
    client: ExecutionReadClient;
    sessionId: string;
    expectedProjectId: string;
    reconciler?: LiveEventReconciler;
  }) {
    this.client = client;
    this.sessionId = sessionId;
    this.expectedProjectId = expectedProjectId;
    this.reconciler = reconciler;
  }

  sync(): Promise<ExecutionSessionRead> {
    if (this.pendingSync) return this.pendingSync;
    const pending = this.readOnce();
    this.pendingSync = pending;
    void pending.finally(() => {
      if (this.pendingSync === pending) this.pendingSync = undefined;
    }).catch(() => undefined);
    return pending;
  }

  private async readOnce(): Promise<ExecutionSessionRead> {
    let pullFailure: unknown;
    const snapshot = await this.reconciler.reconnect(async (afterSequence) => {
      try {
        const page = await this.client.listSessionEvents(this.sessionId, afterSequence);
        if (page.items.some((event) => event.projectId !== this.expectedProjectId)) {
          throw new ExecutionReadContractError(
            "EventPage returned an event outside the URL projectId",
          );
        }
        return page;
      } catch (error) {
        pullFailure = error;
        throw error;
      }
    });

    if (snapshot.phase === "error") {
      throw pullFailure ?? new Error(snapshot.error ?? "Execution event sync failed");
    }

    const decisions = await this.client.listCurrentDecisions(this.sessionId);
    if (decisions.items.some((decision) => decision.projectId !== this.expectedProjectId)) {
      throw new ExecutionReadContractError(
        "Current Decision endpoint returned a Decision outside the URL projectId",
      );
    }
    return { snapshot, decisions: decisions.items };
  }
}

/**
 * Starts one immediate read followed by guarded incremental polls. A slow read
 * cannot overlap the next tick, and stopping the poller suppresses callbacks
 * from an already pending request.
 */
export function startExecutionSessionPolling({
  reader,
  intervalMs = 3_000,
  onRead,
  onError,
  setTimer = (callback, milliseconds) => globalThis.setInterval(callback, milliseconds),
  clearTimer = (timer) => globalThis.clearInterval(timer as ReturnType<typeof setInterval>),
}: ExecutionSessionPollingOptions) {
  let active = true;
  let inFlight = false;

  async function poll() {
    if (!active || inFlight) return;
    inFlight = true;
    try {
      const read = await reader.sync();
      if (active) onRead(read);
    } catch (error) {
      if (active) onError(error);
    } finally {
      inFlight = false;
    }
  }

  void poll();
  const timer = setTimer(() => void poll(), intervalMs);
  return () => {
    active = false;
    clearTimer(timer);
  };
}

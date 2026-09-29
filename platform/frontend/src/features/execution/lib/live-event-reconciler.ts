import type { ExecutionEvent } from "./execution-event.ts";

export type ConnectionPhase =
  | "idle"
  | "live"
  | "offline"
  | "backfilling"
  | "reconnecting"
  | "error";

export type ReconcilerSnapshot = {
  phase: ConnectionPhase;
  events: ExecutionEvent[];
  lastSequence: number;
  bufferedCount: number;
  afterSequence: number | null;
  error?: string;
};

export class SequenceConflictError extends Error {
  constructor(sequence: number) {
    super(`Sequence ${sequence} was received with two different event ids`);
    this.name = "SequenceConflictError";
  }
}

export class EventIdentityConflictError extends Error {
  constructor(eventId: string) {
    super(`Event id ${eventId} was reused with a different canonical event`);
    this.name = "EventIdentityConflictError";
  }
}

export type EventPage = {
  items: ExecutionEvent[];
  afterSequence: number;
  nextAfterSequence: number;
  hasMore: boolean;
};

type FetchAfterSequence = (afterSequence: number) => Promise<EventPage>;

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableValue(item)]),
    );
  }
  return value;
}

function eventIdentitySignature(event: ExecutionEvent) {
  return JSON.stringify(stableValue(event));
}

/**
 * Client-side at-least-once reconciler for the §27.6 afterSequence contract.
 * Only a contiguous prefix becomes visible. Future events wait in a buffer
 * until the missing range is backfilled, so the UI never guesses over gaps.
 */
export class LiveEventReconciler {
  private committed = new Map<number, ExecutionEvent>();
  private buffered = new Map<number, ExecutionEvent>();
  private eventIds = new Set<string>();
  private eventSignatures = new Map<string, string>();
  private phase: ConnectionPhase = "idle";
  private error?: string;

  reset() {
    this.committed.clear();
    this.buffered.clear();
    this.eventIds.clear();
    this.eventSignatures.clear();
    this.phase = "idle";
    this.error = undefined;
  }

  setOffline() {
    this.phase = "offline";
  }

  accept(events: readonly ExecutionEvent[]) {
    try {
      for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
        const signature = eventIdentitySignature(event);
        const priorSignature = this.eventSignatures.get(event.id);
        if (priorSignature && priorSignature !== signature) {
          throw new EventIdentityConflictError(event.id);
        }
        if (this.eventIds.has(event.id)) continue;

        const existing = this.committed.get(event.sequence) ?? this.buffered.get(event.sequence);
        if (existing && existing.id !== event.id) {
          throw new SequenceConflictError(event.sequence);
        }

        this.eventIds.add(event.id);
        this.eventSignatures.set(event.id, signature);
        if (event.sequence <= this.lastSequence()) {
          continue;
        }
        this.buffered.set(event.sequence, event);
      }
      this.drainContiguous();
      this.phase = this.buffered.size > 0 ? "backfilling" : "live";
      this.error = undefined;
    } catch (error) {
      this.phase = "error";
      this.error = error instanceof Error ? error.message : "Unknown sequence error";
      throw error;
    }
  }

  async reconnect(fetchAfterSequence: FetchAfterSequence) {
    this.phase = "reconnecting";
    this.error = undefined;

    try {
      let requestCursor = this.lastSequence();
      for (let pageIndex = 0; pageIndex < 100; pageIndex += 1) {
        const page = await fetchAfterSequence(requestCursor);
        if (page.afterSequence !== requestCursor) {
          throw new Error("EventPage afterSequence does not match the requested cursor");
        }
        if (page.items.length === 0) {
          this.drainContiguous();
          if (this.buffered.size > 0) {
            this.phase = "backfilling";
          } else {
            this.phase = "live";
          }
          return this.snapshot();
        }

        this.accept(page.items);
        if (page.hasMore) {
          if (page.nextAfterSequence <= requestCursor) {
            throw new Error("EventPage nextAfterSequence did not advance");
          }
          requestCursor = page.nextAfterSequence;
          continue;
        }

        if (this.buffered.size > 0) {
          this.phase = "backfilling";
          return this.snapshot();
        }

        if (this.buffered.size === 0) {
          this.phase = "live";
          return this.snapshot();
        }
      }
      throw new Error("Backfill exceeded 100 pages");
    } catch (error) {
      this.phase = "error";
      this.error = error instanceof Error ? error.message : "Reconnect failed";
      return this.snapshot();
    }
  }

  snapshot(): ReconcilerSnapshot {
    const lastSequence = this.lastSequence();
    return {
      phase: this.phase,
      events: [...this.committed.values()].sort((a, b) => a.sequence - b.sequence),
      lastSequence,
      bufferedCount: this.buffered.size,
      afterSequence:
        this.phase === "backfilling" || this.phase === "reconnecting"
          ? lastSequence
          : null,
      error: this.error,
    };
  }

  private lastSequence() {
    if (this.committed.size === 0) return 0;
    return Math.max(...this.committed.keys());
  }

  private drainContiguous() {
    let next = this.lastSequence() + 1;
    while (this.buffered.has(next)) {
      const event = this.buffered.get(next) as ExecutionEvent;
      this.buffered.delete(next);
      this.committed.set(next, event);
      next += 1;
    }
  }
}

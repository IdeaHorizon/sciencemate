import type { ExecutionEvent } from "./execution-event.ts";
import { belongsToRun } from "./run-scope.ts";
import {
  ExecutionReadContractError,
  type ExecutionReadClient,
} from "./execution-read-client.ts";

/** Read every canonical page for one exact Run without inferring descendants. */
export async function readCanonicalRunEvents(
  client: ExecutionReadClient,
  sessionId: string,
  runId: string,
): Promise<ExecutionEvent[]> {
  return readCanonicalRunEventsAfter(client, sessionId, runId, 0);
}

/**
 * Read the durable Run projection after an exclusive session sequence cursor.
 * Sequence values are session-scoped, so a Run-filtered result may legitimately
 * jump over values owned by another Run.
 */
export async function readCanonicalRunEventsAfter(
  client: ExecutionReadClient,
  sessionId: string,
  runId: string,
  initialAfterSequence: number,
): Promise<ExecutionEvent[]> {
  if (!Number.isSafeInteger(initialAfterSequence) || initialAfterSequence < 0) {
    throw new ExecutionReadContractError("Run event cursor must be a non-negative integer");
  }
  const events: ExecutionEvent[] = [];
  let afterSequence = initialAfterSequence;

  while (true) {
    // **一律**带上子节点。这里原来是个参数，而所有调用点取值都一样 ——
    // 结果我只接了首屏那一次，实时轮询那条路照旧不带，UI 上正在跑的那一轮
    // 一个子节点都看不到（2026-08-12 实测）。只有一个取值被走到的参数，
    // 唯一的作用是给"忘了传"留位置。
    const page = await client.listSessionEvents(sessionId, afterSequence, 200, runId, true);
    // 契约照旧：读回来的每一条都必须属于这条 run。要子代时，"属于"扩到
    // 「它自己，或它派出去的直接子节点」—— 边界放宽了，但**仍然是边界**，
    // 不是取消检查（别人的 run 混进来照样要吵）。
    if (page.items.some((event) => !belongsToRun(event, runId))) {
      throw new ExecutionReadContractError("Run event read crossed the requested runId");
    }
    events.push(...page.items);
    if (!page.hasMore) return events;
    if (page.nextAfterSequence <= afterSequence) {
      throw new ExecutionReadContractError("Run event pagination did not advance");
    }
    afterSequence = page.nextAfterSequence;
  }
}

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

function eventSignature(event: ExecutionEvent) {
  return JSON.stringify(stableValue(event));
}

/** Merge at-least-once page/SSE delivery without hiding identity conflicts. */
export function mergeCanonicalRunEvents(
  current: readonly ExecutionEvent[],
  incoming: readonly ExecutionEvent[],
  sessionId: string,
  runId: string,
): ExecutionEvent[] {
  const bySequence = new Map<number, ExecutionEvent>();
  const byId = new Map<string, ExecutionEvent>();

  for (const event of [...current, ...incoming]) {
    // 与读取端同一条边界：要子代时，"属于"扩到「它自己或它的直接子节点」，
    // 但仍然是**边界** —— 别人的 run 混进来照样要吵。
    if (event.sessionId !== sessionId || !belongsToRun(event, runId)) {
      throw new ExecutionReadContractError("Run event merge crossed the requested identity");
    }
    const sequenceOwner = bySequence.get(event.sequence);
    if (sequenceOwner && sequenceOwner.id !== event.id) {
      throw new ExecutionReadContractError(
        `Run event sequence ${event.sequence} has conflicting identities`,
      );
    }
    const idOwner = byId.get(event.id);
    if (idOwner && eventSignature(idOwner) !== eventSignature(event)) {
      throw new ExecutionReadContractError(
        `Run event id ${event.id} has conflicting canonical content`,
      );
    }
    bySequence.set(event.sequence, event);
    byId.set(event.id, event);
  }

  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
}

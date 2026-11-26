import type { ExecutionEvent } from "./execution-event.ts";

export type NodeDispatch = {
  nodeType: string;
  dispatchId?: string;
  background: boolean;
};

/**
 * Upgrade cached/historical dispatches by their declared identity, never by
 * matching JSON, tool names, or words in their text. No free-form field crosses
 * into the dispatch presentation, including with an older backend or worker.
 */
export function nodeDispatchOf(event: ExecutionEvent): NodeDispatch | undefined {
  if (event.kind !== "node.dispatched" && event.kind !== "orchestrator.said") return undefined;
  const raw = event.payload;
  const legacy = event.kind === "orchestrator.said" && (
    event.source.rawEvent === "node_dispatch_announced"
    || (!event.source.rawEvent
      && typeof raw.aboutNodeType === "string" && raw.aboutNodeType.length > 0
      && !raw.repliesToMessageId)
  );
  if (event.kind !== "node.dispatched" && !legacy) return undefined;
  return {
    nodeType: String(raw.nodeType ?? raw.aboutNodeType ?? ""),
    dispatchId: typeof raw.dispatchId === "string" ? raw.dispatchId : undefined,
    background: raw.background === true,
  };
}

/** Canonicalize before page/SSE identity reconciliation, including old caches.
 * An upgraded server can re-deliver the same historical id with the corrected
 * contract. Its dispatch meaning must compare equal without weakening identity
 * checks for other events or for a genuinely different dispatch target.
 */
export function canonicalDispatchEvent(event: ExecutionEvent): ExecutionEvent {
  const dispatch = nodeDispatchOf(event);
  if (!dispatch) return event;
  const payload: Record<string, unknown> = {
    nodeType: dispatch.nodeType,
    background: dispatch.background,
  };
  if (dispatch.dispatchId) payload.dispatchId = dispatch.dispatchId;
  if (event.payload.submissionId) payload.submissionId = String(event.payload.submissionId);
  if (event.kind === "node.dispatched"
    && Object.keys(event.payload).length === Object.keys(payload).length
    && Object.entries(payload).every(([key, value]) => event.payload[key] === value)) return event;
  return { ...event, kind: "node.dispatched", payload };
}

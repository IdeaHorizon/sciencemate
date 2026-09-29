import {
  parseExecutionEvent,
  type ExecutionEvent,
} from "./execution-event.ts";
import type { EventPage } from "./live-event-reconciler.ts";
import { belongsToRun } from "./run-scope.ts";

export type DecisionStatus =
  | "pending"
  | "partially_approved"
  | "resolved"
  | "expired"
  | "cancelled"
  | "superseded";

export type DecisionChoice = {
  choiceId: string;
  label: string;
  description?: string | null;
  consequence?: string | null;
  reversible?: boolean | null;
};

export type DecisionAuthority = {
  authorityType:
    | "initiating_user"
    | "named_users"
    | "project_role"
    | "approval_group";
  authoritySubjects: string[];
  requiredApprovalCount: number;
  actionSetVersion: string;
  policySnapshotId: string;
  expiresAt?: string | null;
};

export type DurableDecision = {
  tenantId: string;
  workspaceId: string;
  projectId: string;
  sessionId: string;
  id: string;
  runId: string;
  attemptNo?: number | null;
  status: DecisionStatus;
  subtype: string;
  prompt: string;
  context?: Record<string, unknown>;
  choices: DecisionChoice[];
  recommendedChoiceId?: string | null;
  selectedChoiceId?: string | null;
  authority: DecisionAuthority;
  acceptedResponseCount: number;
  createdAt: string;
  updatedAt: string;
  resolvedAt?: string | null;
};

export type DecisionListResponse = {
  items: DurableDecision[];
};

export type ExecutionReadClient = {
  listSessionEvents: (
    sessionId: string,
    afterSequence: number,
    limit?: number,
    runId?: string,
    /** 把这条 run 派出去的子节点事件也带上（默认不带）。 */
    includeChildren?: boolean,
  ) => Promise<EventPage>;
  listCurrentDecisions: (sessionId: string) => Promise<DecisionListResponse>;
};

export type FetchLike = (
  input: string,
  init?: RequestInit,
) => Promise<Response>;

/**
 * 服务端在优雅关机时主动收了流（`event: reconnect`）—— **不是**契约违规。
 *
 * 差别在 UI 上是实的：契约违规会走 `apiState="error"` + 文案「返回了本版应用
 * 无法安全展示的数据」；而这里的事实是"服务要重启了，等会儿再连"，该落在
 * offline/stale 那一档，由既有的重试路径接住。
 *
 * 后端为什么不复用 `end` 帧：`end` 的语义是"这条 run 走完了"，拿它冒充重启
 * 会让前端把一条还在跑的 run 记成终态。
 */
export class ExecutionStreamInterruptedError extends Error {
  readonly nextAfterSequence: number;

  constructor(nextAfterSequence: number) {
    super(`Run event stream was interrupted by a server shutdown after sequence ${nextAfterSequence}`);
    this.name = "ExecutionStreamInterruptedError";
    this.nextAfterSequence = nextAfterSequence;
  }
}

export class ExecutionReadContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ExecutionReadContractError";
  }
}

export class ExecutionReadApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string | null;

  constructor({
    status,
    code,
    message,
    requestId,
  }: {
    status: number;
    code: string;
    message: string;
    requestId?: string | null;
  }) {
    super(message);
    this.name = "ExecutionReadApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredRecord(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) throw new ExecutionReadContractError(`${path} must be an object`);
  return value;
}

function requiredString(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new ExecutionReadContractError(`${path} must be a non-empty string`);
  }
  return value;
}

function optionalNullableString(value: unknown, path: string): string | null | undefined {
  if (value === undefined || value === null) return value;
  return requiredString(value, path);
}

function requiredInteger(value: unknown, path: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    throw new ExecutionReadContractError(`${path} must be an integer >= ${minimum}`);
  }
  return value as number;
}

function optionalNullableInteger(value: unknown, path: string, minimum = 0) {
  if (value === undefined || value === null) return value;
  return requiredInteger(value, path, minimum);
}

function optionalNullableBoolean(value: unknown, path: string): boolean | null | undefined {
  if (value === undefined || value === null) return value;
  if (typeof value !== "boolean") {
    throw new ExecutionReadContractError(`${path} must be a boolean or null`);
  }
  return value;
}

const DECISION_STATUSES = new Set<DecisionStatus>([
  "pending",
  "partially_approved",
  "resolved",
  "expired",
  "cancelled",
  "superseded",
]);
const AUTHORITY_TYPES = new Set<DecisionAuthority["authorityType"]>([
  "initiating_user",
  "named_users",
  "project_role",
  "approval_group",
]);

function parseDecisionChoice(value: unknown, index: number): DecisionChoice {
  const choice = requiredRecord(value, `Decision.choices[${index}]`);
  return {
    choiceId: requiredString(choice.choiceId, `Decision.choices[${index}].choiceId`),
    label: requiredString(choice.label, `Decision.choices[${index}].label`),
    description: optionalNullableString(choice.description, `Decision.choices[${index}].description`),
    consequence: optionalNullableString(choice.consequence, `Decision.choices[${index}].consequence`),
    reversible: optionalNullableBoolean(choice.reversible, `Decision.choices[${index}].reversible`),
  };
}

function parseDecisionAuthority(value: unknown): DecisionAuthority {
  const authority = requiredRecord(value, "Decision.authority");
  const authorityType = requiredString(authority.authorityType, "Decision.authority.authorityType");
  if (!AUTHORITY_TYPES.has(authorityType as DecisionAuthority["authorityType"])) {
    throw new ExecutionReadContractError("Decision.authority.authorityType is invalid");
  }
  if (!Array.isArray(authority.authoritySubjects)) {
    throw new ExecutionReadContractError("Decision.authority.authoritySubjects must be an array");
  }
  const authoritySubjects = authority.authoritySubjects.map((subject, index) =>
    requiredString(subject, `Decision.authority.authoritySubjects[${index}]`));
  if (new Set(authoritySubjects).size !== authoritySubjects.length) {
    throw new ExecutionReadContractError("Decision.authority.authoritySubjects must be unique");
  }

  return {
    authorityType: authorityType as DecisionAuthority["authorityType"],
    authoritySubjects,
    requiredApprovalCount: requiredInteger(
      authority.requiredApprovalCount,
      "Decision.authority.requiredApprovalCount",
      1,
    ),
    actionSetVersion: requiredString(
      authority.actionSetVersion,
      "Decision.authority.actionSetVersion",
    ),
    policySnapshotId: requiredString(
      authority.policySnapshotId,
      "Decision.authority.policySnapshotId",
    ),
    expiresAt: optionalNullableString(authority.expiresAt, "Decision.authority.expiresAt"),
  };
}

export function parseDurableDecision(value: unknown): DurableDecision {
  const decision = requiredRecord(value, "Decision");
  const status = requiredString(decision.status, "Decision.status");
  if (!DECISION_STATUSES.has(status as DecisionStatus)) {
    throw new ExecutionReadContractError("Decision.status is invalid");
  }
  if (!Array.isArray(decision.choices) || decision.choices.length === 0) {
    throw new ExecutionReadContractError("Decision.choices must be a non-empty array");
  }

  return {
    tenantId: requiredString(decision.tenantId, "Decision.tenantId"),
    workspaceId: requiredString(decision.workspaceId, "Decision.workspaceId"),
    projectId: requiredString(decision.projectId, "Decision.projectId"),
    sessionId: requiredString(decision.sessionId, "Decision.sessionId"),
    id: requiredString(decision.id, "Decision.id"),
    runId: requiredString(decision.runId, "Decision.runId"),
    attemptNo: optionalNullableInteger(decision.attemptNo, "Decision.attemptNo", 1),
    status: status as DecisionStatus,
    subtype: requiredString(decision.subtype, "Decision.subtype"),
    prompt: requiredString(decision.prompt, "Decision.prompt"),
    context: decision.context === undefined
      ? undefined
      : requiredRecord(decision.context, "Decision.context"),
    choices: decision.choices.map(parseDecisionChoice),
    recommendedChoiceId: optionalNullableString(
      decision.recommendedChoiceId,
      "Decision.recommendedChoiceId",
    ),
    selectedChoiceId: optionalNullableString(
      decision.selectedChoiceId,
      "Decision.selectedChoiceId",
    ),
    authority: parseDecisionAuthority(decision.authority),
    acceptedResponseCount: requiredInteger(
      decision.acceptedResponseCount,
      "Decision.acceptedResponseCount",
    ),
    createdAt: requiredString(decision.createdAt, "Decision.createdAt"),
    updatedAt: requiredString(decision.updatedAt, "Decision.updatedAt"),
    resolvedAt: optionalNullableString(decision.resolvedAt, "Decision.resolvedAt"),
  };
}

export function parseEventPage(value: unknown): EventPage {
  const page = requiredRecord(value, "EventPage");
  if (!Array.isArray(page.items)) {
    throw new ExecutionReadContractError("EventPage.items must be an array");
  }
  const afterSequence = requiredInteger(page.afterSequence, "EventPage.afterSequence");
  const nextAfterSequence = requiredInteger(
    page.nextAfterSequence,
    "EventPage.nextAfterSequence",
  );
  if (typeof page.hasMore !== "boolean") {
    throw new ExecutionReadContractError("EventPage.hasMore must be a boolean");
  }

  const items: ExecutionEvent[] = page.items.map((item, index) =>
    parseExecutionEvent(item, index));
  for (let index = 0; index < items.length; index += 1) {
    if (items[index].sequence <= afterSequence) {
      throw new ExecutionReadContractError(
        "EventPage items must be strictly after afterSequence",
      );
    }
    if (index > 0 && items[index - 1].sequence >= items[index].sequence) {
      throw new ExecutionReadContractError(
        "EventPage items must be ordered by increasing sequence",
      );
    }
  }

  const expectedNext = items.at(-1)?.sequence ?? afterSequence;
  if (nextAfterSequence !== expectedNext) {
    throw new ExecutionReadContractError(
      "EventPage.nextAfterSequence must equal the greatest returned sequence",
    );
  }
  if (page.hasMore && items.length === 0) {
    throw new ExecutionReadContractError("EventPage.hasMore cannot be true for an empty page");
  }

  return { items, afterSequence, nextAfterSequence, hasMore: page.hasMore };
}

export function parseDecisionListResponse(value: unknown): DecisionListResponse {
  const response = requiredRecord(value, "DecisionListResponse");
  if (!Array.isArray(response.items)) {
    throw new ExecutionReadContractError("DecisionListResponse.items must be an array");
  }
  return { items: response.items.map(parseDurableDecision) };
}

export function parseCurrentDecisionListResponse(value: unknown): DecisionListResponse {
  const response = parseDecisionListResponse(value);
  const historical = response.items.find((decision) =>
    decision.status !== "pending" && decision.status !== "partially_approved");
  if (historical) {
    throw new ExecutionReadContractError(
      `Current Decision endpoint returned non-actionable status ${historical.status}`,
    );
  }
  return response;
}

async function responseJson(response: Response): Promise<unknown> {
  try {
    return await response.json() as unknown;
  } catch {
    throw new ExecutionReadContractError("API response was not valid JSON");
  }
}

async function assertOk(response: Response) {
  if (response.ok) return;
  let body: unknown;
  try {
    body = await response.json() as unknown;
  } catch {
    body = undefined;
  }
  const error = isRecord(body) ? body : {};
  throw new ExecutionReadApiError({
    status: response.status,
    code: typeof error.code === "string" ? error.code : "request_failed",
    message: typeof error.message === "string"
      ? error.message
      : `Execution read failed with status ${response.status}`,
    requestId: typeof error.requestId === "string" || error.requestId === null
      ? error.requestId
      : undefined,
  });
}

export function createExecutionReadClient({
  baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1",
  fetchImpl = globalThis.fetch.bind(globalThis),
}: {
  baseUrl?: string;
  fetchImpl?: FetchLike;
} = {}): ExecutionReadClient {
  const normalizedBase = baseUrl.replace(/\/$/, "");

  async function get(path: string): Promise<unknown> {
    const response = await fetchImpl(`${normalizedBase}${path}`, {
      method: "GET",
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    await assertOk(response);
    return responseJson(response);
  }

  return {
    async listSessionEvents(sessionId, afterSequence, limit = 200, runId, includeChildren) {
      if (!Number.isSafeInteger(afterSequence) || afterSequence < 0) {
        throw new ExecutionReadContractError("afterSequence must be a non-negative integer");
      }
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 1000) {
        throw new ExecutionReadContractError("limit must be between 1 and 1000");
      }
      const query = new URLSearchParams({
        afterSequence: String(afterSequence),
        limit: String(limit),
      });
      if (runId) query.set("runId", runId);
      if (includeChildren) query.set("includeChildren", "true");
      const payload = await get(
        `/sessions/${encodeURIComponent(sessionId)}/events?${query.toString()}`,
      );
      const page = parseEventPage(payload);
      if (page.afterSequence !== afterSequence) {
        throw new ExecutionReadContractError(
          "EventPage.afterSequence does not match the requested exclusive cursor",
        );
      }
      if (page.items.some((event) => event.sessionId !== sessionId)) {
        throw new ExecutionReadContractError(
          "EventPage returned an event outside the requested sessionId",
        );
      }
      if (runId && page.items.some((event) => !belongsToRun(event, runId))) {
        throw new ExecutionReadContractError(
          "EventPage returned an event outside the requested runId",
        );
      }
      return page;
    },

    async listCurrentDecisions(sessionId) {
      const payload = await get(
        `/sessions/${encodeURIComponent(sessionId)}/decisions/current`,
      );
      const response = parseCurrentDecisionListResponse(payload);
      if (response.items.some((decision) => decision.sessionId !== sessionId)) {
        throw new ExecutionReadContractError(
          "Current Decision endpoint returned a Decision outside the requested sessionId",
        );
      }
      return response;
    },
  };
}

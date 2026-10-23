import type { RunDetailResponse } from "../../../lib/api.ts";
import { ContractViolation } from "../../../shared/contract-violation.ts";

/** Reject crossed identity and malformed counters instead of rendering them as truth. */
export function assertCanonicalRunDetail(
  detail: RunDetailResponse,
  expectedRunId: string,
): RunDetailResponse {
  if (detail.run?.id !== expectedRunId) {
    throw new ContractViolation(`Run detail identity mismatch: expected ${expectedRunId}`);
  }
  if (!Number.isSafeInteger(detail.eventCount) || detail.eventCount < 0) {
    throw new ContractViolation("Run detail has an invalid canonical eventCount");
  }
  if (!Number.isSafeInteger(detail.run.retryCount) || detail.run.retryCount < 0) {
    throw new ContractViolation("Run detail has an invalid retryCount");
  }
  return detail;
}

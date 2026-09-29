import type {
  RevisionResourceType,
  SessionCandidate,
  SessionCandidateInput,
  SessionChangeSet,
  SessionMergeConflict,
} from "../types";

type JsonRecord = Record<string, unknown>;

const RESOURCE_TYPES = new Set<RevisionResourceType>(["artifact", "project_doc", "project_config"]);

export class SessionRevisionContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SessionRevisionContractError";
  }
}

function record(value: unknown, path: string): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new SessionRevisionContractError(`${path} must be an object`);
  }
  return value as JsonRecord;
}

function field(source: JsonRecord, camel: string, snake: string = camel) {
  return source[camel] ?? source[snake];
}

function string(value: unknown, path: string) {
  if (typeof value !== "string" || !value.trim()) {
    throw new SessionRevisionContractError(`${path} must be a non-empty string`);
  }
  return value;
}

function nullableString(value: unknown, path: string): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string") throw new SessionRevisionContractError(`${path} must be a string or null`);
  return value;
}

function boolean(value: unknown, fallback = false) {
  return typeof value === "boolean" ? value : fallback;
}

function nonNegativeInteger(value: unknown, fallback = 0) {
  return Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : fallback;
}

function resourceType(value: unknown, path: string): RevisionResourceType {
  const type = string(value, path);
  if (!RESOURCE_TYPES.has(type as RevisionResourceType)) {
    throw new SessionRevisionContractError(`${path} must exclude KB and use artifact, project_doc, or project_config`);
  }
  return type as RevisionResourceType;
}

/**
 * ## 这里曾经在校验一份**不存在的**形状（cuib 2026-09-14 报的那条红字）
 *
 * 后端 `api/v1/revisions.py` 的十个端点被 `session_changes.py` 的三个取代，答案
 * **全部改由 git 给**：改了哪些文件 = `git diff --name-only`，多少行 = `--numstat`，
 * 有没有冲突 = 试合并。那份独立的版本账（ChangeSet 行、ChangeItem 行、
 * base/proposed version id）连表一起删了。
 *
 * 而这个适配器留在原地，照旧要求 `items` / `id` / `status` / `createdAt` /
 * `updatedAt`。后端一个都不发 → **每个人、每个会话**，展开改动面板都是
 *
 *     ChangeSet.items must be an array
 *
 * 它为什么能红着活这么久：这个适配器的测试用的是**手写 fixture**，而 fixture 是
 * 照着适配器自己的假设造的 —— 两边都自洽，中间那条真实响应没有任何一处对过。
 * 所以这次连样本一起换：`platform/contracts/fixtures/session-change-set/` 由后端
 * 真实的 `changes_payload()` 产出，两侧共用（与 chat-request 同一套办法）。
 *
 * 形状收成 git 答得出来的那些：路径、统计、补丁、冲突。没有身份行、没有状态机、
 * 没有 version id —— 那些东西的**存储**已经不在了，契约里留着它们只是在要求一个
 * 谁也给不出的东西。
 */
function changedPaths(value: unknown): string[] {
  if (!Array.isArray(value)) {
    throw new SessionRevisionContractError("ChangeSet.changedPaths must be an array");
  }
  return value.map((entry, index) => string(entry, `ChangeSet.changedPaths[${index}]`));
}

export function adaptSessionChangeSet(
  payload: unknown,
  expected: { projectId: string; sessionId: string },
): SessionChangeSet {
  const outer = record(payload, "ChangeSet response");
  const source = outer.changeSet ? record(outer.changeSet, "ChangeSet response.changeSet") : outer;
  const projectId = string(field(source, "projectId", "project_id"), "ChangeSet.projectId");
  const sessionId = string(field(source, "sessionId", "session_id"), "ChangeSet.sessionId");
  if (projectId !== expected.projectId || sessionId !== expected.sessionId) {
    throw new SessionRevisionContractError("ChangeSet identity does not match the requested Project Session");
  }
  const paths = changedPaths(field(source, "changedPaths", "changed_paths"));
  return {
    projectId,
    sessionId,
    changedPaths: paths,
    changeCount: paths.length,
    conflictCount: Array.isArray(source.conflicts) ? source.conflicts.length : 0,
    gitBranch: nullableString(field(source, "gitBranch", "git_branch"), "ChangeSet.gitBranch"),
    gitBaseCommitSha: nullableString(field(source, "gitBaseCommitSha", "git_base_commit_sha"), "ChangeSet.gitBaseCommitSha"),
    gitHeadCommitSha: nullableString(field(source, "gitHeadCommitSha", "git_head_commit_sha"), "ChangeSet.gitHeadCommitSha"),
    aheadBy: nonNegativeInteger(field(source, "aheadBy", "ahead_by")),
    behindBy: nonNegativeInteger(field(source, "behindBy", "behind_by")),
    patch: nullableString(source.patch, "ChangeSet.patch") ?? "",
    additions: nonNegativeInteger(source.additions),
    deletions: nonNegativeInteger(source.deletions),
    filesChanged: nonNegativeInteger(field(source, "filesChanged", "files_changed")),
    worktreeClean: field(source, "worktreeClean", "worktree_clean") === undefined
      ? true
      : boolean(field(source, "worktreeClean", "worktree_clean")),
    patchTruncated: boolean(field(source, "patchTruncated", "patch_truncated")),
  };
}

function previewFlags(source: JsonRecord) {
  const raw = field(source, "previewTruncated", "preview_truncated");
  const flags = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as JsonRecord : {};
  return {
    base: boolean(flags.base),
    theirs: boolean(flags.theirs),
    ours: boolean(flags.ours),
  };
}

function conflict(value: unknown, index: number): SessionMergeConflict {
  const item = record(value, `Conflicts[${index}]`);
  const status = string(item.status, `Conflicts[${index}].status`);
  if (status !== "open" && status !== "resolved") {
    throw new SessionRevisionContractError(`Conflicts[${index}].status is invalid`);
  }
  const resolution = item.resolution === undefined || item.resolution === null
    ? null
    : record(item.resolution, `Conflicts[${index}].resolution`);
  return {
    id: string(item.id, `Conflicts[${index}].id`),
    resourceType: resourceType(field(item, "resourceType", "resource_type"), `Conflicts[${index}].resourceType`),
    resourceKey: string(field(item, "resourceKey", "resource_key"), `Conflicts[${index}].resourceKey`),
    baseVersionId: nullableString(field(item, "baseVersionId", "base_version_id"), `Conflicts[${index}].baseVersionId`),
    projectVersionId: nullableString(field(item, "projectVersionId", "project_version_id"), `Conflicts[${index}].projectVersionId`),
    proposedVersionId: nullableString(field(item, "proposedVersionId", "proposed_version_id"), `Conflicts[${index}].proposedVersionId`),
    status,
    resolution: resolution as SessionMergeConflict["resolution"],
    basePreview: nullableString(field(item, "basePreview", "base_preview"), `Conflicts[${index}].basePreview`),
    theirsPreview: nullableString(field(item, "theirsPreview", "theirs_preview"), `Conflicts[${index}].theirsPreview`),
    oursPreview: nullableString(field(item, "oursPreview", "ours_preview"), `Conflicts[${index}].oursPreview`),
    previewTruncated: previewFlags(item),
  };
}

export function adaptSessionConflicts(payload: unknown) {
  const list = Array.isArray(payload)
    ? payload
    : field(record(payload, "Conflict response"), "items", "conflicts");
  if (!Array.isArray(list)) throw new SessionRevisionContractError("Conflict response must contain an array");
  return list.map(conflict);
}

export function adaptSessionConflict(payload: unknown) {
  const outer = record(payload, "Conflict response");
  return conflict(outer.conflict ?? outer, 0);
}

export function buildSessionCandidateRequest(input: SessionCandidateInput) {
  const resource = resourceType(input.resourceType, "Candidate.resourceType");
  return {
    resourceType: resource,
    resourceKey: string(input.resourceKey, "Candidate.resourceKey"),
    name: string(input.name, "Candidate.name"),
    content: string(input.content, "Candidate.content"),
    artifactType: input.artifactType,
    artifactId: input.artifactId,
    mimeType: input.mimeType,
    description: input.description,
  };
}

export function adaptSessionCandidate(payload: unknown): SessionCandidate {
  const source = record(payload, "Candidate response");
  const version = field(source, "version");
  const sizeBytes = field(source, "sizeBytes", "size_bytes");
  if (!Number.isSafeInteger(version) || (version as number) < 1) {
    throw new SessionRevisionContractError("Candidate.version must be a positive integer");
  }
  if (!Number.isSafeInteger(sizeBytes) || (sizeBytes as number) < 0) {
    throw new SessionRevisionContractError("Candidate.sizeBytes must be a non-negative integer");
  }
  return {
    artifactId: string(field(source, "artifactId", "artifact_id"), "Candidate.artifactId"),
    versionId: string(field(source, "versionId", "version_id"), "Candidate.versionId"),
    version: version as number,
    resourceKey: string(field(source, "resourceKey", "resource_key"), "Candidate.resourceKey"),
    lifecycleStatus: string(field(source, "lifecycleStatus", "lifecycle_status"), "Candidate.lifecycleStatus"),
    checksum: string(source.checksum, "Candidate.checksum"),
    sizeBytes: sizeBytes as number,
    gitCommitSha: nullableString(field(source, "gitCommitSha", "git_commit_sha"), "Candidate.gitCommitSha"),
    repositoryPath: nullableString(field(source, "repositoryPath", "repository_path"), "Candidate.repositoryPath"),
  };
}

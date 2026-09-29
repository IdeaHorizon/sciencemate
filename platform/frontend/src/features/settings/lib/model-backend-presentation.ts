import type { ModelBackend } from "@/lib/api";

function normalizedProvider(provider: string) {
  return provider === "demo" || provider === "local_demo" ? "demo" : provider.trim().toLowerCase();
}

export function modelConnectionIdentity(backend: Pick<ModelBackend, "provider" | "model" | "base_url">) {
  const provider = normalizedProvider(backend.provider);
  const model = backend.model.trim().toLowerCase();
  if (provider === "demo") return `demo:${model}`;
  return `${provider}:${model}:${(backend.base_url ?? "managed").trim().replace(/\/$/, "").toLowerCase()}`;
}

export function modelBackendDisplayName(backend: Pick<ModelBackend, "provider" | "display_name">) {
  return normalizedProvider(backend.provider) === "demo" ? "Local demonstration model" : backend.display_name;
}

export function modelBackendModelLabel(backend: Pick<ModelBackend, "provider" | "model">) {
  return normalizedProvider(backend.provider) === "demo" ? "Deterministic local runtime" : backend.model;
}

export function deduplicateModelBackends(backends: readonly ModelBackend[]) {
  const ranked = [...backends].sort((left, right) => {
    const defaultRank = Number(right.is_default) - Number(left.is_default);
    if (defaultRank) return defaultRank;
    const readyRank = Number(right.status === "ready") - Number(left.status === "ready");
    if (readyRank) return readyRank;
    return Number(right.editable) - Number(left.editable);
  });
  const seen = new Set<string>();
  return ranked.filter((backend) => {
    const identity = modelConnectionIdentity(backend);
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

/**
 * 后端 `backend_status()` 能吐出的**全部**取值。
 *
 * 写死一份名单本来是要避免的事，但这里的替代品更糟：从前 statusLabel 是页面
 * 里一串 if，漏掉了 `credentials_rejected` —— 而那恰恰是整个凭证健康机制唯一
 * 会产出的"坏消息"状态。于是一条 key 已失效的连接，界面上写的是
 * "Status not reported"（2026-08-21 本机实测截图）。
 *
 * 所以名单留着，但配一条测试逐个核对：新增状态没配文案 = 测试红，而不是
 * 悄悄掉进兜底分支。
 */
export const MODEL_BACKEND_STATUSES = [
  "ready",
  "disabled",
  "missing_credentials",
  "credentials_rejected",
  "harness_disabled",
  "unsupported_by_harness",
  // 下面两个是这一层自己的历史取值（legacy provider 列表 / 未知后端）
  "not_configured",
  "unreachable",
] as const;

export function modelBackendStatusLabel(status: string): string {
  switch (status) {
    case "ready":
      return "Ready";
    case "disabled":
      return "Disabled";
    case "missing_credentials":
      return "Credential required";
    case "credentials_rejected":
      // provider 明确拒绝了这把 key。是"坏了"，不是"没数据"。
      return "Credential rejected";
    case "harness_disabled":
      return "Local execution disabled";
    case "unsupported_by_harness":
      return "Not supported by local execution";
    case "not_configured":
      return "Not configured";
    case "unreachable":
      return "Unreachable";
    default:
      return "Status not reported";
  }
}

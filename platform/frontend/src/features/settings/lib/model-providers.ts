/**
 * 建一条模型连接时，provider 那一栏的**合法取值**。
 *
 * 这个词表的权威在后端 `app/services/model_backends.py` 的
 * `HARNESS_COMPATIBLE_PROVIDERS` —— 不在这张表里的 provider，后端照存不误，
 * 但 `backend_status()` 会把它判成 `unsupported_by_harness`：连接建出来了、
 * 长得像好的、就是永远不能用。此前这一栏是个自由输入框，于是"合法取值"只
 * 在存完之后以一个状态词的形式出现，等于让人猜。
 *
 * 进程边界两侧各写一次（前端不 import Python），`model-providers.test.ts`
 * 逐条钉住两边相等 —— 后端加/删 provider 而这里没跟上，测试红。
 */
export const HARNESS_COMPATIBLE_PROVIDERS = [
  "deepseek",
  "kimi",
  "openai",
  "openai_compatible",
  "local",
] as const;

const PROVIDER_LABELS: Record<string, string> = {
  deepseek: "DeepSeek",
  kimi: "Kimi / Moonshot",
  openai: "OpenAI",
  openai_compatible: "OpenAI-compatible endpoint",
  local: "Local / self-hosted gateway",
};

/** 自定义端点必须给地址，官方托管的不用。 */
const BASE_URL_REQUIRED = new Set(["openai_compatible", "local"]);

export function providerLabel(provider: string) {
  return PROVIDER_LABELS[provider] ?? provider;
}

export function providerNeedsBaseUrl(provider: string) {
  return BASE_URL_REQUIRED.has(provider);
}

/**
 * 这条连接可以不填 API key 吗？
 *
 * 判据是**谁提供端点**：填了 base_url = 用户指着自己的服务器，而自建端点
 * （vLLM / SGLang / Ollama / llama.cpp）默认不鉴权。用官方托管端点时 key 是
 * 唯一身份，必需。权威在后端 `model_backends.credential_is_optional`，同一条
 * 判据两侧各写一次，`model-providers.test.ts` 钉住两边说的是同一件事。
 *
 * 这里只决定**文案怎么写**：能不能用仍由后端探针回答（不带 Authorization 头
 * 探一次，端点收了才算数）。
 */
export function credentialIsOptional(baseUrl: string | null | undefined) {
  return (baseUrl ?? "").trim() !== "";
}

/**
 * 下拉里该出现哪些选项。
 *
 * `current` 是正在编辑的那条连接的 provider：它可能是历史遗留的、或者别人
 * 用别的途径建的（`demo`、`local_demo`），不在支持词表里。这种也得列出来 ——
 * 否则一打开编辑框，下拉自动落到别的值上，保存时把这条连接**改成了另一个
 * provider**，而人什么都没点。
 */
export function providerOptions(current?: string) {
  const options = HARNESS_COMPATIBLE_PROVIDERS.map((value) => ({
    value: value as string,
    label: providerLabel(value),
    supported: true,
  }));
  const trimmed = (current ?? "").trim();
  if (trimmed && !options.some((option) => option.value === trimmed)) {
    options.push({ value: trimmed, label: `${providerLabel(trimmed)} (not supported by local execution)`, supported: false });
  }
  return options;
}

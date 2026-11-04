import type { Phrase } from "@/shared/i18n";

/**
 * 开场那张表里的几个现成选项。
 *
 * 每一项的 `label` 都是 `Phrase`，**包括三个牌子名**（两种语言写的一样）。
 * 混着放（三个是 string、一个是 Phrase）的代价 2026-09-16 实测到了：拼显示名时
 * 对 Phrase 那一项拼出 `[object Object] · fake-model`，而这串**被存进了数据库**
 * 当连接的显示名。类型统一之后，忘了 `t()` 是一个类型错误，不是一句乱码。
 */
export type ProviderPreset = {
  id: string;
  label: Phrase;
  base_url: string;
  model: string;
};

export const PROVIDERS: ProviderPreset[] = [
  { id: "deepseek", label: { zh: "DeepSeek", en: "DeepSeek" }, base_url: "https://api.deepseek.com", model: "deepseek-chat" },
  { id: "openai", label: { zh: "OpenAI", en: "OpenAI" }, base_url: "https://api.openai.com/v1", model: "gpt-4o" },
  { id: "anthropic", label: { zh: "Anthropic", en: "Anthropic" }, base_url: "https://api.anthropic.com", model: "claude-sonnet-4-5" },
  { id: "custom", label: { zh: "其他（兼容 OpenAI 的服务）", en: "Other (OpenAI-compatible endpoint)" }, base_url: "", model: "" },
];

/**
 * 这条连接可以不填 API 密钥吗 —— 和设置页同一个判据（`credentialIsOptional`：端点是你自己填的）。
 *
 * 设置页里官方地址留空，所以「填了地址」就等于「用自己的服务」。引导里三家预设把**官方**地址
 * 预先填好了，没有密钥连不上；选「其他（兼容 OpenAI 的服务）」，或者把预设的地址改成了自己的，
 * 才是自建端点 —— 那种通常不鉴权（vLLM、Ollama……），密钥可以留空，保存时会实测一次。
 * 以前这里一律要密钥，于是一条在设置页能存、能用的自建连接，在引导里过不去（11-04 实测）。
 */
export function keyIsOptional(draft: { provider: ProviderPreset; baseUrl: string }): boolean {
  const address = draft.baseUrl.trim();
  return address !== "" && (draft.provider.id === "custom" || address !== draft.provider.base_url);
}

/** 存进库的 provider 名字：预设里的 `custom` 在后端叫 openai_compatible。 */
export function providerForApi(id: string): string {
  return id === "custom" ? "openai_compatible" : id;
}

/**
 * key 存在哪 —— **按后端报的实情说**，不说一句放之四海皆准的话。
 *
 * 原来写的是「key 只存在这台机器上」。就传输而言一直为真，但读的人会理解成
 * "存得安全"，而当时的存储强度是混淆不是加密（#825）。现在存储加固了，可"用了
 * 哪一种"因机器而异 —— 没有钥匙串的环境会退回文件。写死就等于对一部分用户
 * 说了不实的话。
 */
export function keyStorageLine(storage: string): Phrase {
  switch (storage) {
    case "keychain":
      return { zh: "密钥加密保存在这台电脑上，由系统钥匙串保护。", en: "Your key is encrypted on this computer and protected by the system keychain." };
    case "file":
      return { zh: "密钥加密保存在这台电脑上（系统钥匙串不可用，加密用的主密钥存放在应用数据目录中）。", en: "Your key is encrypted on this computer (no system keychain is available, so the master key is kept in the app's data folder)." };
    case "operator":
      return { zh: "密钥加密保存在服务器上，由服务器管理员管理。", en: "Your key is encrypted on the server and managed by its administrator." };
    default:
      return { zh: "密钥只保存在这台电脑上。", en: "The key is stored on this computer only." };
  }
}

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
  { id: "custom", label: { zh: "其他（兼容 OpenAI 的地址）", en: "Other (OpenAI-compatible endpoint)" }, base_url: "", model: "" },
];

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
      return { zh: "key 加密后存在这台机器上，主密钥交给系统钥匙串保管，不会发到别处。", en: "The key is encrypted on this machine, its master key held in the system keychain, and never sent anywhere else." };
    case "file":
      return { zh: "key 加密后存在这台机器上，主密钥放在数据目录里一个仅本人可读的文件中（这台机器没有可用的钥匙串），不会发到别处。", en: "The key is encrypted on this machine, its master key in an owner-only file under the data directory (no keychain is available here), and never sent anywhere else." };
    case "operator":
      return { zh: "key 加密后存在服务器上，主密钥由这台服务器的管理员配置。", en: "The key is encrypted on the server; its master key is configured by this server's administrator." };
    default:
      return { zh: "key 只存在这台机器上，不会发到别处。", en: "The key stays on this machine and is never sent anywhere else." };
  }
}

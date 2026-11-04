"use client";

/** 一条模型连接要填的四样东西。主推理模型和那几个可选角色共用它。 */

import { PROVIDERS, keyIsOptional, type ProviderPreset } from "./lib/providers";
import { useT } from "@/shared/i18n";

export type ConnectionDraft = {
  provider: ProviderPreset;
  baseUrl: string;
  model: string;
  apiKey: string;
};

export function emptyDraft(): ConnectionDraft {
  return { provider: PROVIDERS[0], baseUrl: PROVIDERS[0].base_url, model: PROVIDERS[0].model, apiKey: "" };
}

export function draftIsComplete(draft: ConnectionDraft): boolean {
  return Boolean(draft.baseUrl.trim() && draft.model.trim() && (draft.apiKey.trim() || keyIsOptional(draft)));
}

export function ModelConnectionFields({
  draft,
  onChange,
  idPrefix,
}: {
  draft: ConnectionDraft;
  onChange: (next: ConnectionDraft) => void;
  idPrefix: string;
}) {
  const t = useT();
  const chooseProvider = (id: string) => {
    const next = PROVIDERS.find((item) => item.id === id) ?? PROVIDERS[0];
    // 换供应商就把地址和模型换成它的默认值 —— 留着上一家的地址是最容易出的错。
    onChange({ ...draft, provider: next, baseUrl: next.base_url, model: next.model });
  };
  return (
    <div className="onboarding-fields">
      <label htmlFor={`${idPrefix}-provider`}>{t({ zh: "提供商", en: "Provider" })}
        <select id={`${idPrefix}-provider`} value={draft.provider.id} onChange={(event) => chooseProvider(event.target.value)}>
          {PROVIDERS.map((item) => <option key={item.id} value={item.id}>{t(item.label)}</option>)}
        </select>
      </label>
      <label htmlFor={`${idPrefix}-base`}>{t({ zh: "地址", en: "Address" })}
        <input id={`${idPrefix}-base`} value={draft.baseUrl} spellCheck={false}
          onChange={(event) => onChange({ ...draft, baseUrl: event.target.value })} placeholder="https://…" />
      </label>
      <label htmlFor={`${idPrefix}-model`}>{t({ zh: "模型", en: "Model" })}
        <input id={`${idPrefix}-model`} value={draft.model} spellCheck={false}
          onChange={(event) => onChange({ ...draft, model: event.target.value })}
          placeholder={t({ zh: "模型名", en: "Model name" })} />
      </label>
      <label htmlFor={`${idPrefix}-key`}>{t({ zh: "API 密钥", en: "API key" })}
        <input id={`${idPrefix}-key`} type="password" autoComplete="off" value={draft.apiKey}
          placeholder={keyIsOptional(draft) ? t({ zh: "自建服务不需要密钥时可留空", en: "Leave blank if your service needs no key" }) : undefined}
          onChange={(event) => onChange({ ...draft, apiKey: event.target.value })} />
        {keyIsOptional(draft) && (
          <small>{t({
            zh: "自建服务（vLLM、SGLang、Ollama 等）通常不需要密钥，可以留空。保存时会测试一次连接，需要密钥的话会提示你。",
            en: "Self-hosted services (vLLM, SGLang, Ollama and so on) usually need no key, so you can leave this blank. Saving tests the connection once and tells you if a key is needed.",
          })}</small>
        )}
      </label>
    </div>
  );
}

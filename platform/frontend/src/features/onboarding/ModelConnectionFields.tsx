"use client";

/** 一条模型连接要填的四样东西。主推理模型和那几个可选角色共用它。 */

import { PROVIDERS, type ProviderPreset } from "./lib/providers";
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
  return Boolean(draft.baseUrl.trim() && draft.model.trim() && draft.apiKey.trim());
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
          onChange={(event) => onChange({ ...draft, apiKey: event.target.value })} />
      </label>
    </div>
  );
}

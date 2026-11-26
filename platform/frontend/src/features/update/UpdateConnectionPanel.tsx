"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { useT } from "@/shared/i18n";

export function UpdateConnectionPanel({ mode, busy, check }: {
  mode: "system" | "direct";
  busy: boolean;
  check: () => Promise<unknown>;
}) {
  const t = useT();
  const [choice, setChoice] = useState<"system" | "direct" | null>(null);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const selected = choice ?? mode;
  const apply = async () => {
    setSaving(true);
    setProblem(null);
    try {
      await api.setUpdateConnection(selected);
      await check();
      setChoice(null);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : t({ zh: "无法保存连接方式", en: "Could not save the connection setting" }));
    } finally {
      setSaving(false);
    }
  };
  return <section className="settings-compact-section">
    <h2>{t({ zh: "更新连接", en: "Update connection" })}</h2>
    <p>{t({ zh: "仅用于检查和下载应用更新。默认沿用系统代理；需要时可选择直连。", en: "Applies to application update checks and downloads. Use your system proxy by default, or choose a direct connection." })}</p>
    <label>{t({ zh: "连接方式", en: "Connection mode" })}
      <select value={selected} disabled={busy || saving}
        onChange={(event) => setChoice(event.target.value as "system" | "direct")}>
        <option value="system">{t({ zh: "沿用系统设置", en: "Use system settings" })}</option>
        <option value="direct">{t({ zh: "直连更新服务器", en: "Connect directly to the update server" })}</option>
      </select>
    </label>
    <button type="button" className="settings-secondary-action" disabled={busy || saving || selected === mode}
      onClick={() => void apply()}>
      {t({ zh: "保存并重新检查", en: "Save and check again" })}
    </button>
    {problem && <p role="alert" className="settings-inline-error">{problem}</p>}
  </section>;
}

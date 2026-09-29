"use client";

import { RefreshCw } from "lucide-react";

import { sayWhatTheCheckFound } from "@/features/update/say-what-the-check-found";
import { useUpdate } from "@/features/update/useUpdate";
import { PRODUCT_NAME } from "@/shared/brand";
import { useT, type Phrase } from "@/shared/i18n";
import type { UpdateStatus } from "@/lib/api";

/**
 * 「关于」：我装的是哪一版、从哪来、有没有更新。
 *
 * ## 为什么这一页和横幅说的话不一样
 *
 * 横幅只在**有新版本**时出现，查不到就一个字不说 —— 它是提醒。这一页是人自己走进来
 * 问的，所以每一种结果都得有答案，尤其是「查不到」：不说的话，「没检查」和「检查过
 * 了、已经是最新」在屏幕上长得一模一样，而这两件事该做的事完全相反。
 *
 * 五种结果各有各的话：已是最新 / 有新版本 / 更新已下好等重启 / 这次得重装 /
 * 查不到（离线、源地址不对、这是源码 checkout）。
 *
 * 「已是最新」「有新版本 X」答的都是**现在装得上的**那一版。更新的一版还在发布页上
 * 一个文件一个文件地传时（2026-09-27 的 0.5.5 传了大半天），后端把它列在
 * `still_publishing` 里 —— 不说这一句，「已是最新」就把「新版就在路上」藏掉了。
 */

/** `installed_from` 的人话。后端那四个值是**词表**，这里一个不漏地翻，漏了就会显示原文。 */
const WHERE_IT_CAME_FROM: Record<UpdateStatus["installed_from"], Phrase> = {
  payload: { zh: "自更新装上的载荷", en: "self-updated payload" },
  bundled: { zh: "安装包里自带的", en: "shipped in the installer" },
  repo: { zh: "源码目录直接跑的", en: "running from a source checkout" },
  unknown: { zh: "说不上来", en: "unknown" },
};

export function AboutPanel() {
  const t = useT();
  const { status, phase, problem, lastChecked, check, install, restart } = useUpdate();

  const busy = phase === "checking" || phase === "installing" || phase === "restarting";
  const available = status?.available_version;
  const staged = status?.staged_version;
  const needsReinstall = Boolean(available && status?.shell_update?.needs_reinstall);

  return (
    <div className="settings-page">
      <header className="settings-page-header">
        <h1>{t({ zh: "关于", en: "About" })}</h1>
        <p>{t({ zh: "装的是哪一版，以及有没有新的。", en: "Which version this is, and whether a newer one exists." })}</p>
      </header>

      <section className="settings-compact-section">
        <div className="settings-compact-heading">
          <div>
            <h2>{PRODUCT_NAME}</h2>
            <p>
              {status?.installed_version
                ? <>{t({ zh: "版本", en: "Version" })} <strong>{status.installed_version}</strong>
                    <small>　·　{t(WHERE_IT_CAME_FROM[status.installed_from] ?? WHERE_IT_CAME_FROM.unknown)}</small></>
                : t({ zh: "正在读版本…", en: "Reading version…" })}
            </p>
          </div>
        </div>

        <div className="settings-row-group">
          <div className="settings-row about-update-row">
            <span>
              <strong>{t({ zh: "更新", en: "Updates" })}</strong>
              <small>{sayWhatTheCheckFound(status, phase, lastChecked, t)}</small>
            </span>
            <div className="about-update-actions">
              {staged && !available ? (
                <button type="button" className="settings-primary-action" disabled={busy} onClick={() => void restart()}>
                  {t({ zh: "现在重启", en: "Restart now" })}
                </button>
              ) : available && !needsReinstall ? (
                <button type="button" className="settings-primary-action" disabled={busy}
                        onClick={() => void install(t({ zh: "没有可装的版本", en: "No version available to install" }))}>
                  {t({ zh: `更新到 ${available}`, en: `Update to ${available}` })}
                </button>
              ) : needsReinstall && status?.reinstall_url ? (
                <a className="settings-secondary-action" href={status.reinstall_url} target="_blank" rel="noreferrer">
                  {t({ zh: "去下载安装包", en: "Get the installer" })}
                </a>
              ) : null}
              <button type="button" className="settings-secondary-action" disabled={busy} onClick={() => void check()}>
                <RefreshCw size={14} aria-hidden="true" className={phase === "checking" ? "is-spinning" : undefined} />
                {t({ zh: "检查更新", en: "Check for updates" })}
              </button>
            </div>
          </div>

          {status?.source ? (
            <div className="settings-row">
              <span>
                <strong>{t({ zh: "更新源", en: "Update source" })}</strong>
                <small>{status.source}</small>
              </span>
            </div>
          ) : null}
        </div>

        {problem ? (
          <p className="settings-inline-error" role="alert">
            {t({ zh: "查不到更新：", en: "Could not check for updates: " })}{problem}
          </p>
        ) : null}
        {status?.apply_error ? (
          <p className="settings-inline-error" role="alert">
            {t({ zh: "上一次更新没装上：", en: "The last update did not apply: " })}{status.apply_error}
          </p>
        ) : null}
      </section>
    </div>
  );
}

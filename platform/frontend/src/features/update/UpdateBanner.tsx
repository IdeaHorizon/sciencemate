"use client";

import { sayItWasUpdated } from "@/features/update/say-what-is-happening";
import { UpdateProgress } from "@/features/update/UpdateProgress";
import { useUpdate } from "@/features/update/useUpdate";
import { useT } from "@/shared/i18n";

/**
 * 一行字：「有新版本 X · 现在更新」。点了就下载、验证、暂存，然后重启。
 *
 * 只在**有新版本**时出现。检查失败（离线、源不可达、这是源码 checkout）一个字
 * 都不显示 —— 更新是个方便，不是个提醒；打不到更新源不该在界面上留一条红。
 * 人主动想知道「我是哪一版、有没有更新」的时候去设置里的「关于」页，那儿欠他一句
 * 实话（见 `useUpdate` 的文件头）。
 *
 * 重启之后端口会变、壳会把窗口指到新地址，所以这一页只需要说「正在重启」，
 * 不用自己刷新。重启回来的第一屏说一次「已更新到 X（原 Y）」，点「知道了」不再提。
 *
 * 下载校验与即将重启这两段用 `UpdateProgress`（转圈 + 一句话 + 过了几秒）—— 和关于页同一份。
 */
export function UpdateBanner() {
  const t = useT();
  const { status, phase, problem, elapsedSeconds, install, restart, acknowledge } = useUpdate();

  const available = status?.available_version;
  const staged = status?.staged_version;
  const justUpdated = status?.just_updated;
  if (!available && !staged && !justUpdated && phase === "idle") return null;

  // 这次更新动了应用本体（外壳），而装着的外壳不会自己换：点「现在更新」拿不到它。
  // 那就别给那个按钮 —— 让人点完发现没变，比不给按钮糟得多。如实说，给地址。
  const needsReinstall = Boolean(available && status?.shell_update?.needs_reinstall);

  return (
    <div className="update-banner" role="status">
      {needsReinstall && phase === "idle" ? (
        <>
          <span>{t({ zh: "有新版本 {version}，这次改动了应用本体，需要重新安装才能拿到。", en: "Version {version} is out; it changes the app itself, so it needs a fresh install." }, { version: available ?? "" })}</span>
          {status?.reinstall_url ? (
            <a href={status.reinstall_url} target="_blank" rel="noreferrer">{t({ zh: "去下载安装包", en: "Get the installer" })}</a>
          ) : null}
        </>
      ) : phase === "installing" || phase === "restarting" ? (
        <UpdateProgress phase={phase} version={available ?? staged} elapsedSeconds={elapsedSeconds} />
      ) : justUpdated && phase === "idle" ? (
        <>
          <span>{sayItWasUpdated(justUpdated.from, justUpdated.to, t)}</span>
          {justUpdated.notes_url ? (
            <a href={justUpdated.notes_url} target="_blank" rel="noreferrer">{t({ zh: "看看改了什么", en: "What changed" })}</a>
          ) : null}
          <button type="button" onClick={() => void acknowledge()}>{t({ zh: "知道了", en: "Got it" })}</button>
        </>
      ) : staged && !available ? (
        <>
          <span>{t({ zh: "更新 {version} 已就绪，重启后生效。", en: "Update {version} is ready; it takes effect after a restart." }, { version: staged ?? "" })}</span>
          <button type="button" onClick={() => void restart()}>{t({ zh: "现在重启", en: "Restart now" })}</button>
        </>
      ) : (
        <>
          <span>{t({ zh: "有新版本 {version}", en: "Version {version} is available" }, { version: available ?? "" })}{status?.installed_version ? t({ zh: `（当前 ${status.installed_version}）`, en: `(currently ${status.installed_version})` }) : ""}</span>
          <button type="button" onClick={() => void install(t({ zh: "没有可装的版本", en: "No version available to install" }))}>{t({ zh: "现在更新", en: "Update now" })}</button>
        </>
      )}
      {phase === "failed" && problem ? <em className="update-banner-problem">{problem}</em> : null}
    </div>
  );
}

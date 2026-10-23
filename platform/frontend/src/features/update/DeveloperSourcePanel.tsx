"use client";

import { useCallback, useEffect, useState } from "react";

import { sayWhereItRuns } from "@/features/update/say-where-it-runs";
import { UpdateProgress } from "@/features/update/UpdateProgress";
import type { UpdatePhase } from "@/features/update/useUpdate";
import { api, ApiError, type DeveloperSource } from "@/lib/api";
import { useLanguage, useT } from "@/shared/i18n";

/**
 * 开发者模式：把这台桌面版指到自己的源码目录。
 *
 * 同事改了代码要在桌面版里看效果（2026-10-23）。装好的那份 harness / 界面 / 后端是打包时
 * 拷进去的，改自己的 checkout 对它没影响；以前只有「等重新打包」或「终端起后端 + 浏览器」。
 * 声明一个源码目录之后，launcher 把三样东西都指到它（后端 `services/developer_source.py`）。
 *
 * ## 每一种局面都要有一句话
 *
 * 和「更新」那一行同一条规矩：这一段是人自己走进来看的。没开 / 指了但还没重启 / 正在从
 * 源码跑 / 指的目录不合格 / 刚撤掉还没重启 —— 五种局面各说各的，尤其「指了但没重启」：
 * 不说的话，看起来像开着，跑的还是安装的那份。
 *
 * 组织服务器没有这回事（后端 403）：整段不画，而不是画一段永远报错的。
 */
export function DeveloperSourcePanel({ phase, restart }: {
  phase: UpdatePhase;
  /** 和更新那一行同一个重启（`useUpdate.restart`）：先把「即将重启」停在屏幕上，再真去重启。 */
  restart: () => Promise<void>;
}) {
  const t = useT();
  const language = useLanguage();
  const [told, setTold] = useState<DeveloperSource | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.getDeveloperSource();
      setTold(next);
      setPath((current) => current || next.declared || "");
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) { setUnavailable(true); return; }
      setProblem(error instanceof Error ? error.message : String(error));
    }
  }, []);

  // 后端那几句（目录为什么不合格）是按这一屏的语言说的：换了语言重问一次。
  useEffect(() => { void load(); }, [load, language]);

  const save = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const next = await api.setDeveloperSource(path.trim());
      setTold(next);
      setPath(next.declared ?? "");
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setProblem(null);
    try {
      setTold(await api.clearDeveloperSource());
      setPath("");
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  if (unavailable) return null;

  const restarting = phase === "restarting";
  const trimmed = path.trim();
  const unchanged = trimmed === (told?.declared ?? "");

  return (
    <section className="settings-compact-section">
      <div className="settings-compact-heading">
        <div>
          <h2>{t({ zh: "开发者模式", en: "Developer mode" })}</h2>
          <p>{t({
            zh: "从你自己的源码目录跑这台桌面版。改了 harness（core / shared / nodes），下一条消息就是新代码；改了后端或界面，重启一次。",
            en: "Run this desktop from your own source checkout. Changes to the harness (core / shared / nodes) take effect on the next message; changes to the backend or the interface need one restart.",
          })}</p>
        </div>
      </div>

      <div className="settings-row-group">
        <div className="settings-row about-update-row">
          <span>
            <strong>{t({ zh: "现在", en: "Now" })}</strong>
            <small>{restarting
              ? <UpdateProgress phase={phase} version={null} elapsedSeconds={0} />
              : sayWhereItRuns(told, t)}</small>
          </span>
          {told?.restart_needed && !restarting ? (
            <div className="about-update-actions">
              <button type="button" className="settings-primary-action" disabled={busy} onClick={() => void restart()}>
                {t({ zh: "现在重启", en: "Restart now" })}
              </button>
            </div>
          ) : null}
        </div>

        {told?.declared && !told.problem ? (
          <div className="settings-row">
            <span>
              <strong>{t({ zh: "界面", en: "Interface" })}</strong>
              <small>{told.static_ui
                ? t({ zh: `源码目录里构建过的（${told.static_ui}）`, en: `built in the checkout (${told.static_ui})` })
                : t({ zh: "源码目录里没有构建产物（platform/frontend/out），用安装的那份；要看界面改动先 npm run build。",
                      en: "The checkout has no build output (platform/frontend/out); the installed interface is used. Run npm run build to see interface changes." })}</small>
            </span>
          </div>
        ) : null}

        <div className="settings-row developer-source-row">
          <label className="settings-field developer-source-field">
            <span>{t({ zh: "源码目录（harness-framework 的 checkout，绝对路径）", en: "Source directory (a harness-framework checkout, absolute path)" })}</span>
            <input
              value={path}
              onChange={(event) => setPath(event.target.value)}
              placeholder={t({ zh: "例如 /Users/you/src/harness-framework", en: "e.g. /Users/you/src/harness-framework" })}
              spellCheck={false}
              disabled={busy || restarting}
            />
          </label>
          <div className="about-update-actions">
            <button type="button" className="settings-primary-action" disabled={busy || restarting || !trimmed || unchanged}
                    onClick={() => void save()}>
              {t({ zh: "使用这个目录", en: "Use this directory" })}
            </button>
            {told?.declared ? (
              <button type="button" className="settings-secondary-action" disabled={busy || restarting} onClick={() => void clear()}>
                {t({ zh: "停用", en: "Turn off" })}
              </button>
            ) : null}
          </div>
        </div>
      </div>

      {problem ? (
        <p className="settings-inline-error" role="alert">{problem}</p>
      ) : null}
    </section>
  );
}

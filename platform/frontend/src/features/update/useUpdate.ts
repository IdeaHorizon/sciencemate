"use client";

import { useCallback, useEffect, useState } from "react";
import { useLanguage } from "@/shared/i18n";

import { api, type UpdateStatus } from "@/lib/api";

/**
 * 自更新的那三步 —— 查、装、重启 —— **一份**。
 *
 * 横幅（`UpdateBanner`）和关于页（`AboutSettingsPage`）都要它。各写一份的话，
 * 两处会各自演化：比如后端加了「这次更新换不了壳、要重装」那种情况，横幅认了、
 * 关于页没认，而两边看起来都对。
 *
 * ## 错误在这里**存着**，显不显示由调用方决定
 *
 * 同一个失败在两处的含义不一样：
 * - 横幅是**提醒**。打不到更新源（离线、源不可达、这是源码 checkout）时它一个字
 *   都不该显示 —— 在界面上留一条红，是把「你什么都不用做」说成「出事了」。
 * - 关于页是**回答**。人按了「检查更新」，那就欠他一句话：查到了什么，或者为什么
 *   没查到。这时候静默等于[[feedback_absent_check_looks_like_passed_check]]：
 *   「没检查」和「检查过了没有新版本」长得一模一样。
 *
 * 所以 `check()` 不抛，把失败记在 `problem` 里；横幅不看它，关于页看。
 */
export type UpdatePhase = "idle" | "checking" | "installing" | "restarting" | "failed";

/**
 * 下好、验过之后，先把「即将重启，窗口会自动回来」停在屏幕上这么久，再真去重启。
 * 以前装完立刻重启：那句话只停 1 ms 窗口就被换掉，人看到的是一次没头没尾的刷新。
 */
export const RESTART_NOTICE_MS = 2000;

const wait = (ms: number) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });

export function useUpdate(options?: { checkOnMount?: boolean }) {
  const checkOnMount = options?.checkOnMount ?? true;
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [phase, setPhase] = useState<UpdatePhase>("idle");
  const [problem, setProblem] = useState<string | null>(null);
  /** 这个页面上一次问后端是什么时候。`status.checked_at` 是后端那一刻，这个是这一次。 */
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  /** 这一次安装从什么时候开始 —— 下载校验要十几秒，界面得说出「还在动」。 */
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  useEffect(() => {
    if (phase !== "installing" || startedAt === null) return;
    const tick = () => setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000));
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [phase, startedAt]);

  const check = useCallback(async (): Promise<UpdateStatus | null> => {
    setPhase("checking");
    setProblem(null);
    try {
      const next = await api.getUpdateStatus();
      setStatus(next);
      setLastChecked(new Date());
      setPhase("idle");
      return next;
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
      setPhase("idle");
      return null;
    }
  }, []);

  // 状态里那几句（没查成的原因、上次没装上的原因）是后端按这一屏的语言说的：换了语言重问一次。
  const language = useLanguage();
  useEffect(() => {
    if (!checkOnMount) return;
    let cancelled = false;
    api.getUpdateStatus()
      .then((next) => { if (!cancelled) { setStatus(next); setLastChecked(new Date()); } })
      .catch((error) => { if (!cancelled) setProblem(error instanceof Error ? error.message : String(error)); });
    return () => { cancelled = true; };
  }, [checkOnMount, language]);

  /** 下载、验证、暂存，然后重启。装完才重启，顺序反过来就是重启到同一个旧版本。 */
  const install = useCallback(async (fallbackMessage: string) => {
    setPhase("installing");
    setStartedAt(Date.now());
    setElapsedSeconds(0);
    setProblem(null);
    try {
      const staged = await api.installUpdate();
      setStatus(staged);
      if (!staged.staged_version) {
        setPhase("failed");
        setProblem(staged.error ?? fallbackMessage);
        return;
      }
      setPhase("restarting");
      await wait(RESTART_NOTICE_MS);
      await api.restartForUpdate();
    } catch (error) {
      setPhase("failed");
      setProblem(error instanceof Error ? error.message : String(error));
    }
  }, []);

  /** 人看到「已更新到 X」了：后端记下，之后不再提。失败不打扰人 —— 下次启动再说一次而已。 */
  const acknowledge = useCallback(async () => {
    setStatus((current) => (current ? { ...current, just_updated: null } : current));
    try {
      await api.acknowledgeUpdate();
    } catch {
      // 记不下也不要紧：最坏是下次打开再提示一次。
    }
  }, []);

  const restart = useCallback(async () => {
    setPhase("restarting");
    setProblem(null);
    try {
      await wait(RESTART_NOTICE_MS);
      await api.restartForUpdate();
    } catch (error) {
      setPhase("failed");
      setProblem(error instanceof Error ? error.message : String(error));
    }
  }, []);

  return { status, phase, problem, lastChecked, elapsedSeconds, check, install, restart, acknowledge };
}

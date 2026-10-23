"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api, type InterfaceSettings } from "@/lib/api";
import { queryClient } from "@/lib/query/client";
import { useAuth } from "@/features/auth";
import { languageToSettleOnFirstOpen } from "@/features/settings/lib/first-language";
import { isLanguage, languageForASystem, say, type Language } from "@/shared/i18n";

export const INTERFACE_SETTINGS_CACHE_KEY = "atrium.interface_settings";
/** 登录页上点过的语言 —— 那时还没有账号可存，登录后若账号没选过语言，就用这一个。 */
export const LANGUAGE_BEFORE_SIGN_IN_KEY = "atrium.language_before_sign_in";

export const DEFAULT_INTERFACE_SETTINGS: InterfaceSettings = {
  theme: "system",
  density: "comfortable",
  font_scale: 100,
  reduce_motion: false,
  language: "zh",
  default_landing: "feed",
  show_run_usage: true,
  onboarding_done: false,
  project_guide_done: false,
  execution_detail: "standard",
  auto_collapse_completed_tools: true,
  auto_collapse_completed_steps: true,
  follow_active_run: true,
};

type InterfaceSettingsState = {
  settings: InterfaceSettings;
  ready: boolean;
  saving: boolean;
  error: string | null;
  save: (settings: InterfaceSettings) => Promise<InterfaceSettings>;
  refresh: () => Promise<InterfaceSettings | null>;
  /** 换界面语言。登录前也能点（登录页）：那时只记在这台浏览器上，登录后再存进账号。 */
  chooseLanguage: (language: Language) => Promise<void>;
};

const InterfaceSettingsContext = createContext<InterfaceSettingsState | null>(null);

export function normalizeInterfaceSettings(value: unknown): InterfaceSettings | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Partial<InterfaceSettings>;
  const validLegacyFields = ["system", "light", "dark"].includes(item.theme ?? "")
    && ["comfortable", "compact"].includes(item.density ?? "")
    && [90, 100, 110, 120].includes(item.font_scale ?? 0)
    && typeof item.reduce_motion === "boolean"
    && ["feed", "last_session", "projects", "new_research"].includes(item.default_landing ?? "")
    && typeof item.show_run_usage === "boolean";
  if (!validLegacyFields) return null;
  return {
    ...DEFAULT_INTERFACE_SETTINGS,
    ...item,
    execution_detail: ["summary", "standard", "trace"].includes(item.execution_detail ?? "")
      ? item.execution_detail as InterfaceSettings["execution_detail"]
      : DEFAULT_INTERFACE_SETTINGS.execution_detail,
    auto_collapse_completed_tools: typeof item.auto_collapse_completed_tools === "boolean"
      ? item.auto_collapse_completed_tools
      : DEFAULT_INTERFACE_SETTINGS.auto_collapse_completed_tools,
    auto_collapse_completed_steps: typeof item.auto_collapse_completed_steps === "boolean"
      ? item.auto_collapse_completed_steps
      : DEFAULT_INTERFACE_SETTINGS.auto_collapse_completed_steps,
    follow_active_run: typeof item.follow_active_run === "boolean"
      ? item.follow_active_run
      : DEFAULT_INTERFACE_SETTINGS.follow_active_run,
    // 同上：认得就用、认不得退默认，不进那道全有全无的闸。老缓存里没有这个
    // 字段是常态（0.5.0 之前存的每一份都没有），它不该把整份偏好判成无效。
    onboarding_done: typeof item.onboarding_done === "boolean"
      ? item.onboarding_done
      : DEFAULT_INTERFACE_SETTINGS.onboarding_done,
    project_guide_done: typeof item.project_guide_done === "boolean"
      ? item.project_guide_done
      : DEFAULT_INTERFACE_SETTINGS.project_guide_done,
    // 语言和上面几个一样走"认得就用、认不得退默认"，而**不**进
    // `validLegacyFields` 那道全有全无的闸：那道闸不认识的字段会让整份
    // 偏好被判无效、退回全部默认 —— 一个老缓存就能把用户的主题、密度、
    // 落地页一起复位。
    language: isLanguage(item.language)
      ? item.language
      : DEFAULT_INTERFACE_SETTINGS.language,
  } as InterfaceSettings;
}

/** 上一次摆出来的语言 —— 换了语言，后端按旧语言给的字（角色说明、资讯、状态句）就得重取。 */
let languageOnScreen: string | null = null;

export function applyInterfaceSettings(settings: InterfaceSettings) {
  // 后端的报错照这一屏的语言说：每一问都带上它（见 `api.speaking`）。
  api.speaking(settings.language);
  if (languageOnScreen !== null && languageOnScreen !== settings.language) {
    // 2026-10-19 真跑：开场卡片上点了 English，卡片标题换了，角色说明还是中文 —— 那段是
    // 后端按语言给的，按中文取过一次就留着。查询全部作废、在屏的重取。
    void queryClient.invalidateQueries();
  }
  languageOnScreen = settings.language;
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.lang = settings.language === "zh" ? "zh-CN" : "en";
  root.dataset.theme = settings.theme;
  root.dataset.density = settings.density;
  root.dataset.reduceMotion = String(settings.reduce_motion);
  root.style.setProperty("--interface-font-scale", String(settings.font_scale / 100));
  root.style.colorScheme = settings.theme === "system" ? "light dark" : settings.theme;
}

function readCachedSettings() {
  if (typeof localStorage === "undefined") return null;
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(INTERFACE_SETTINGS_CACHE_KEY) ?? "null");
    return normalizeInterfaceSettings(parsed);
  } catch {
    return null;
  }
}

/** 浏览器给的语言（桌面版的 WKWebView 只给第一项 —— 整张表由后端读，见 refresh）。 */
function browserLanguages(): string[] {
  if (typeof navigator === "undefined") return [];
  return navigator.languages?.length ? [...navigator.languages] : navigator.language ? [navigator.language] : [];
}

function readLanguageBeforeSignIn(): Language | null {
  try {
    const value = typeof localStorage === "undefined" ? null : localStorage.getItem(LANGUAGE_BEFORE_SIGN_IN_KEY);
    return isLanguage(value) ? value : null;
  } catch {
    return null;
  }
}

function forgetLanguageBeforeSignIn() {
  try {
    if (typeof localStorage !== "undefined") localStorage.removeItem(LANGUAGE_BEFORE_SIGN_IN_KEY);
  } catch {
    // 存储被禁用：本来也没记下什么。
  }
}

function cacheAndApply(settings: InterfaceSettings) {
  if (typeof localStorage !== "undefined") {
    localStorage.setItem(INTERFACE_SETTINGS_CACHE_KEY, JSON.stringify(settings));
  }
  applyInterfaceSettings(settings);
}

export function InterfaceSettingsProvider({ children }: { children: React.ReactNode }) {
  const { authenticated, ready: authReady } = useAuth();
  const [settings, setSettings] = useState(DEFAULT_INTERFACE_SETTINGS);
  const [ready, setReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const cached = readCachedSettings();
    if (cached) {
      setSettings(cached);
      applyInterfaceSettings(cached);
    } else {
      // 这台浏览器上从没存过：账号那份还没到（或者还没登录），先按浏览器给的语言摆，
      // 别让一个不懂中文的人在登录页上先看见一屏中文。账号到了之后以 refresh 为准。
      const guessed = {
        ...DEFAULT_INTERFACE_SETTINGS,
        language: readLanguageBeforeSignIn() ?? languageForASystem(browserLanguages()),
      };
      setSettings(guessed);
      applyInterfaceSettings(guessed);
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!authenticated) {
      setReady(authReady);
      return null;
    }
    try {
      const response = await api.getInterfaceSettings();
      let current = normalizeInterfaceSettings(response) ?? DEFAULT_INTERFACE_SETTINGS;
      // 这个人还没选过语言 → 按系统语言替他定一次，并存进账号（后端的文案读的是那一份，
      // 不存的话界面是英文、资讯和失败说明还是中文）。存下之后这一问就答"选过了"。
      // 问不到就不猜，照账号那份摆 —— 切换钮在侧栏上，用目标语言写着。
      const hint = await api.getInterfaceLanguageHint().catch(() => null);
      const language = hint && languageToSettleOnFirstOpen(hint, readLanguageBeforeSignIn(), browserLanguages());
      if (language) {
        const saved = await api.saveInterfaceSettings({ ...current, language }).catch(() => null);
        current = (saved && normalizeInterfaceSettings(saved)) ?? { ...current, language };
        forgetLanguageBeforeSignIn();
      }
      setSettings(current);
      cacheAndApply(current);
      setError(null);
      return current;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : say(
        { zh: "读不到界面设置", en: "Interface settings could not be loaded" },
        // 这个 Provider 自己就是语言的出处，不能反过来去 useLanguage()。
        isLanguage(settings.language) ? settings.language : "zh",
      ));
      return null;
    } finally {
      setReady(true);
    }
  }, [authenticated, authReady, settings.language]);

  useEffect(() => {
    if (!authReady) return;
    void refresh();
  }, [authReady, refresh]);

  const save = useCallback(async (next: InterfaceSettings) => {
    const previous = settings;
    setSettings(next);
    cacheAndApply(next);
    setSaving(true);
    setError(null);
    try {
      const response = await api.saveInterfaceSettings(next);
      const saved = normalizeInterfaceSettings(response) ?? next;
      setSettings(saved);
      cacheAndApply(saved);
      return saved;
    } catch (caught) {
      setSettings(previous);
      cacheAndApply(previous);
      setError(caught instanceof Error ? caught.message : say(
        { zh: "界面设置没能保存", en: "Interface settings could not be saved" },
        isLanguage(settings.language) ? settings.language : "zh",
      ));
      throw caught;
    } finally {
      setSaving(false);
    }
  }, [settings]);

  const chooseLanguage = useCallback(async (language: Language) => {
    if (authenticated) {
      forgetLanguageBeforeSignIn();
      await save({ ...settings, language });
      return;
    }
    // 还没登录（登录页 / 注册页）：没有账号可存。先在这台浏览器上换过来、记一笔，
    // 登录后账号要是没选过语言，就用这一笔（见 refresh）。
    const next = { ...settings, language };
    setSettings(next);
    cacheAndApply(next);
    try {
      localStorage.setItem(LANGUAGE_BEFORE_SIGN_IN_KEY, language);
    } catch {
      // 存储被禁用：这一屏照样换了，只是记不住。
    }
  }, [authenticated, save, settings]);

  const value = useMemo<InterfaceSettingsState>(() => ({
    settings,
    ready,
    saving,
    error,
    save,
    refresh,
    chooseLanguage,
  }), [settings, ready, saving, error, save, refresh, chooseLanguage]);

  return <InterfaceSettingsContext.Provider value={value}>{children}</InterfaceSettingsContext.Provider>;
}

export function useInterfaceSettings() {
  const value = useContext(InterfaceSettingsContext);
  if (!value) throw new Error("useInterfaceSettings must be used within InterfaceSettingsProvider");
  return value;
}

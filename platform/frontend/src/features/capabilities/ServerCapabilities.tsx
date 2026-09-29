"use client";

import { createContext, useContext, useEffect, useState } from "react";

import { API_BASE_URL } from "@/lib/api";

/**
 * 这台 App Server 装配成了什么形态。
 *
 * ## 为什么界面要问这个
 *
 * 个人档里没有登录、没有成员、没有治理 —— 「不教育用户」这条要求落到界面上，
 * 就是那些入口一个都不能出现。判断依据必须来自服务器，且必须在拿到任何身份
 * **之前**就能拿到：前端要先知道"这里要不要登录"，才谈得上登录。
 *
 * 判断一律落在 `features` 上，不落在 `profile` 名字上。名字会变（产品还没定名），
 * 能力不会。`profile` 只用来在界面上说一句"本机 / 组织服务器"。
 */
export type ServerCapabilities = {
  profile: "personal" | "org" | "unknown";
  /**
   * 这份安装是哪种发行（个人版 / 专业版）。同 profile 一样**只用来显示**：专业版
   * 多出来的入口按 features 里的 `connections` 画。读不到 = personal 的方向由后端定。
   */
  edition: "personal" | "pro" | "unknown";
  features: string[];
  /** 凭据主密钥**实际**存在哪。界面要对用户说关于 key 的话，事实只能来自后端。 */
  credentialKeyStorage: "keychain" | "file" | "operator" | "unknown";
};

const UNKNOWN: ServerCapabilities = {
  profile: "unknown", edition: "unknown", features: [], credentialKeyStorage: "unknown",
};

const CapabilitiesContext = createContext<{
  capabilities: ServerCapabilities;
  ready: boolean;
}>({ capabilities: UNKNOWN, ready: false });

export function CapabilitiesProvider({ children }: { children: React.ReactNode }) {
  const [capabilities, setCapabilities] = useState<ServerCapabilities>(UNKNOWN);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE_URL}/capabilities`)
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (cancelled || !payload) return;
        setCapabilities({
          profile: payload.profile ?? "unknown",
          edition: payload.edition === "pro" || payload.edition === "personal" ? payload.edition : "unknown",
          features: Array.isArray(payload.features) ? payload.features : [],
          credentialKeyStorage: payload.credentialKeyStorage ?? "unknown",
        });
      })
      .catch(() => {
        // 问不到就当组织档处理：那一档要登录。**保守的方向是多要一次身份，
        // 不是少要一次** —— 反过来会在一台真有别人的服务器上放行匿名访问。
        if (!cancelled) setCapabilities({
            profile: "unknown", edition: "unknown", features: ["auth"], credentialKeyStorage: "unknown",
          });
      })
      .finally(() => {
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <CapabilitiesContext.Provider value={{ capabilities, ready }}>
      {children}
    </CapabilitiesContext.Provider>
  );
}

export function useCapabilities() {
  return useContext(CapabilitiesContext);
}

/** 这台服务器有没有这项能力。问不到时按"有"处理（同上，保守方向）。 */
export function useHasCapability(feature: string) {
  const { capabilities, ready } = useCapabilities();
  if (!ready) return null; // 还不知道 —— 调用方据此显示"加载中"，别抢答
  return capabilities.features.includes(feature);
}

"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api, type CurrentUser, type UserRole } from "@/lib/api";
import { authSlots } from "./slots";
import { queryClient } from "@/lib/query/client";
import { canonicalRunEventRegistry } from "@/features/execution/lib/run-event-registry";
import { bootstrapSession } from "./session-bootstrap";

const TOKEN_KEY = "atrium.access_token";

type AuthState = {
  user: CurrentUser | null;
  ready: boolean;
  authenticated: boolean;
  /** `organisation` 说进哪个 —— 一台服务器上住着好几个组织。 */
  login: (email: string, password: string, organisation?: string) => Promise<CurrentUser>;
  logout: () => Promise<void>;
  adoptToken: (accessToken: string) => void;
  refreshUser: () => Promise<CurrentUser | null>;
  updateUser: (user: CurrentUser) => void;
  hasPermission: (permission: string) => boolean;
};

const AuthContext = createContext<AuthState | null>(null);

function normalizeUser(user: CurrentUser): CurrentUser {
  const role: UserRole = user.role ?? "researcher";
  return {
    ...user,
    role,
    permissions: user.permissions ?? [],
    governance_scope: user.governance_scope ?? {
      kind: "individual",
      id: user.id,
      // 后端没给范围时不在这里**编一个名字**：编出来的名字是一种语言写死的，
      // 而它会一路显示到界面上。留空，由显示那一层按当前语言说「个人工作区」
      // （workspaceDisplayName / 账号页各自处理）。
      name: "",
    },
  };
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [ready, setReady] = useState(false);

  const clearSession = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    api.clearToken();
    canonicalRunEventRegistry.stopAll("Authentication session ended");
    setUser(null);
    queryClient.clear();
  }, []);

  useEffect(() => {
    api.onUnauthorized(clearSession);
    // 「我是谁」这个问题一律问服务器 —— 判断逻辑在 `bootstrapSession`，那里写了
    // 为什么不能"浏览器里没票就不问"（一句话：个人档永远没有票，于是永远不知道
    // 自己是谁，于是界面上每一处按权限渲染的东西都不显示）。
    void bootstrapSession({
      storedToken: localStorage.getItem(TOKEN_KEY),
      adoptToken: (value) => api.setToken(value),
      askWhoIAm: () => api.getCurrentUser(),
      discardTheToken: clearSession,
    })
      .then((current) => setUser(current ? normalizeUser(current) : null))
      .finally(() => setReady(true));

    return () => api.onUnauthorized(null);
  }, [clearSession]);

  const login = useCallback(async (email: string, password: string, organisation = "") => {
    const signIn = authSlots.signIn;
    if (!signIn) throw new Error("no-sign-in-on-this-edition");
    const token = await signIn(email, password, organisation);
    localStorage.setItem(TOKEN_KEY, token.access_token);
    const current = normalizeUser(await api.getCurrentUser());
    setUser(current);
    return current;
  }, []);

  /**
   * Replace the stored credential without re-authenticating. A password
   * rotation invalidates the token in localStorage server-side; persisting the
   * replacement here is what keeps this tab signed in across a reload.
   */
  const adoptToken = useCallback((accessToken: string) => {
    localStorage.setItem(TOKEN_KEY, accessToken);
    api.setToken(accessToken);
  }, []);

  const logout = useCallback(async () => {
    try {
      await authSlots.signOut?.();
    } finally {
      clearSession();
    }
  }, [clearSession]);

  const refreshUser = useCallback(async () => {
    if (!api.hasToken()) return null;
    const current = normalizeUser(await api.getCurrentUser());
    setUser(current);
    return current;
  }, []);

  const updateUser = useCallback((current: CurrentUser) => {
    setUser(normalizeUser(current));
  }, []);

  const value = useMemo<AuthState>(() => ({
    user,
    ready,
    authenticated: !!user,
    login,
    logout,
    adoptToken,
    refreshUser,
    updateUser,
    hasPermission: (permission) => {
      if (!user) return false;
      return user.permissions?.includes(permission) ?? false;
    },
  }), [user, ready, login, logout, adoptToken, refreshUser, updateUser]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used within AuthProvider");
  return value;
}

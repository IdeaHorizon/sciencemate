/**
 * 登录这件事的插槽 —— 核心只留位置，专业版装配时填进来（`src/pro/wire.ts`）。
 *
 * 个人版没有登录这回事：这些槽全空，AuthProvider / AuthGate 的判据仍然落在服务器
 * 报出的能力上（`auth`），槽只决定"有登录的时候用什么实现"。公开树里没有专业版，
 * 槽永远是空的，而核心一个 import 都不指向它。
 */
import type { ComponentType } from "react";

export type SignIn = (email: string, password: string, organisation?: string) => Promise<{ access_token: string }>;

export const authSlots: {
  /** 登录页在哪；没有 = 这个版本没有登录页。核心不写死 `/login`：公开树里没有那一页。 */
  signInPath: string | null;
  signIn: SignIn | null;
  signOut: (() => Promise<void>) | null;
  /** 拿着一次性口令的人先改密码 —— 盖住整个工作区的那一屏。 */
  mustChangePassword: ComponentType | null;
  /** 「账户与安全」里的改密码那一节。 */
  changePassword: ComponentType | null;
} = { signInPath: null, signIn: null, signOut: null, mustChangePassword: null, changePassword: null };

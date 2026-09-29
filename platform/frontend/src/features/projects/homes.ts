/**
 * 别处的家 —— 本机之外，项目还能住在哪（后端那一半：`app/services/other_homes.py`）。
 *
 * 核心只认「本机」。能把项目安在别处的提供者由发行在装配时登记（专业版登记「组织」：
 * 列出握着的连接、以及「加一个组织」那扇对话框）。一台没登记任何提供者的机器上，
 * 建项目时「放在哪」那一行整个不出现 —— 个人版和从前逐字一样。
 */
import type { ComponentType } from "react";

export type OtherHome = {
  id: string;
  name: string;
  url?: string;
  /** 要先重新登录一下才能选它。 */
  needsSignIn?: boolean;
};

export type HomeAdderProps = {
  onCancel: () => void;
  /** 加好了：这就是新家的 id。 */
  onHeld: (held: { id: string }) => void;
};

export const otherHomes: {
  /** 管别处的家的那一页在哪（专业版：「组织」Tab）；没有 = 不画通向它的链接。 */
  pagePath: string | null;
  /** 这台桌面握着的别处的家；没有提供者 = 只有本机。 */
  list: (() => Promise<OtherHome[]>) | null;
  /** 「加一个」那扇对话框；没有 = 不画那个按钮。 */
  Adder: ComponentType<HomeAdderProps> | null;
} = { pagePath: null, list: null, Adder: null };

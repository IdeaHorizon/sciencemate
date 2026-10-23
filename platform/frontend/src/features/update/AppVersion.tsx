"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { useT } from "@/shared/i18n";

/**
 * 侧栏最底下一行：装着的是哪一版（wangd 10-19：「打开了，为啥我看不到版本号？」——
 * 版本号此前只在「设置 → 关于」里）。点它进关于页：查更新、看更新源都在那儿。
 *
 * 读 `GET /version`（不联网）。一次窗口里不会变：更新会重启后端、壳把窗口整个换掉，
 * 所以不轮询（`staleTime: Infinity`）。读不到就不画 —— 一个版本号不值得一条报错；
 * 但重试照常（默认三次退避），别让启动时的一次 500 把它藏一整个窗口。
 */
export function AppVersion() {
  const t = useT();
  const installed = useQuery({
    queryKey: qk.installedVersion(),
    queryFn: () => api.getInstalledVersion(),
    staleTime: Infinity,
    // 不关重试：读一次失败（后端还没就绪）配上 staleTime: Infinity，整个窗口里就再也看不到版本号了。
  });
  const data = installed.data;
  if (!data) return null;
  const label = data.version
    ? t({ zh: "版本 {version}", en: "Version {version}" }, { version: data.version })
    : data.installed_from === "repo"
      ? t({ zh: "源码运行", en: "Running from source" })
      : null;
  if (!label) return null;
  return (
    <Link href="/settings/about" className="app-version" title={t({ zh: "关于与更新", en: "About and updates" })}>
      {label}
    </Link>
  );
}

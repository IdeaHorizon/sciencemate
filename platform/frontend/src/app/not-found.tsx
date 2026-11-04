"use client";

import Link from "next/link";
import {useT} from "@/shared/i18n";

export default function NotFound() {
  const t = useT();
  return <main className="not-found-page">
    <h1>404</h1>
    <p>{t({zh:"这个页面不存在，或地址已经变更。",en:"This page does not exist or its address has changed."})}</p>
    <Link href="/">{t({zh:"返回首页",en:"Back to home"})}</Link>
  </main>;
}

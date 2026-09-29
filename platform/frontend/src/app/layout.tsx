import type { Metadata } from "next";
import "katex/dist/katex.min.css";
import "./globals.css";
import { Providers } from "./providers";
import { PRODUCT_NAME } from "@/shared/brand";

export const metadata: Metadata = {
  title: PRODUCT_NAME,
  description: "Agent-based Scientific Research Platform",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: `(function(){try{var s=JSON.parse(localStorage.getItem('atrium.interface_settings')||'null');if(!s)return;var r=document.documentElement;r.dataset.theme=s.theme;r.dataset.density=s.density;r.dataset.reduceMotion=String(!!s.reduce_motion);r.style.setProperty('--interface-font-scale',String((s.font_scale||100)/100));r.style.colorScheme=s.theme==='system'?'light dark':s.theme}catch(e){}})();` }} />
      </head>
      <body className="antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

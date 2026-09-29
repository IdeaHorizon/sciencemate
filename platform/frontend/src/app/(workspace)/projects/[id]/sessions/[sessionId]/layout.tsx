import { staticShellParams } from "@/shared/routing/static-shell";

export function generateStaticParams() {
  return staticShellParams("sessionId");
}

export default function SessionLayout({ children }: { children: React.ReactNode }) {
  return children;
}

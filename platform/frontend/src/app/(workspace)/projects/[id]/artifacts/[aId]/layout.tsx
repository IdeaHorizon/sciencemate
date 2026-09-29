import { staticShellParams } from "@/shared/routing/static-shell";

export function generateStaticParams() {
  return staticShellParams("aId");
}

export default function ArtifactDetailLayout({ children }: { children: React.ReactNode }) {
  return children;
}

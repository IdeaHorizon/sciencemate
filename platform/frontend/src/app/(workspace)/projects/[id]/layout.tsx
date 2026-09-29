import { staticShellParams } from "@/shared/routing/static-shell";
import { ProjectShellRoute } from "@/features/projects";

// 静态导出：这一段只产出一份外壳；真 id 由 ProjectShellRoute 从地址栏读。
export function generateStaticParams() {
  return staticShellParams("id");
}

export default function ProjectLayout({ children }: { children: React.ReactNode }) {
  return <ProjectShellRoute>{children}</ProjectShellRoute>;
}

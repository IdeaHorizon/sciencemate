import { AppShell } from "@/shared/layout/AppShell";
import { AuthGate } from "@/features/auth";

/**
 * Workspace shell — used by all post-Phase-3 routes.
 *
 * The legacy app/page.tsx still has its own inline shell. As features are
 * extracted from that page, their new home will be under (workspace)/.
 */
export default function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  return <AuthGate><AppShell>{children}</AppShell></AuthGate>;
}

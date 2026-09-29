"use client";

/**
 * Client-side provider wrapper. Lives outside the root layout so the layout
 * itself can stay a server component.
 *
 * Wraps children with:
 * - QueryClientProvider (server state)
 * - ToastRoot (notifications)
 * - (future) ThemeProvider, ModalRoot, etc.
 */
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "@/lib/query/client";
import { ToastRoot } from "@/shared/ui/ToastRoot";
import { AuthProvider } from "@/features/auth";
import { CapabilitiesProvider } from "@/features/capabilities";
import { InterfaceSettingsProvider } from "@/features/settings/InterfaceSettingsProvider";
import { wire } from "@/edition";

// 发行接线：专业版把导航入口、登录、别处的家填进核心留的槽里。个人版这一句是空操作。
wire();

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <CapabilitiesProvider>
      <AuthProvider>
        <InterfaceSettingsProvider>
          {children}
          <ToastRoot />
        </InterfaceSettingsProvider>
      </AuthProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
}

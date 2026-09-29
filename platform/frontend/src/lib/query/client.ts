/**
 * TanStack Query client + key factory.
 *
 * The client is created once at module load (singleton). The provider lives
 * in app/providers.tsx so the root layout stays a server component.
 */
import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 30s before data is considered stale
      staleTime: 30_000,
      // Don't refetch when window regains focus — too noisy for our long-running flows
      refetchOnWindowFocus: false,
      // Retry once on failure (network blips); the 2nd failure surfaces an error
      retry: 1,
    },
    mutations: {
      retry: 0,
    },
  },
});

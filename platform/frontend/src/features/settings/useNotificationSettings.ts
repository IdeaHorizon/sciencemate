"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";

export function useNotificationSettings() {
  return useQuery({
    queryKey: qk.notificationSettings(),
    queryFn: () => api.getNotificationSettings(),
    staleTime: 60_000,
  });
}

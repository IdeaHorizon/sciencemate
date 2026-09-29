"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";

export function useComputeInventory() {
  return useQuery({
    queryKey: qk.computeInventory(),
    queryFn: () => api.getComputeInventory(),
    refetchInterval: 15_000,
  });
}

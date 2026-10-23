import type { QueryClient } from "@tanstack/react-query";

/** Closing while saves are in flight must refresh from the saved server state. */
export function waitForFeedSettingWrites(client: QueryClient): Promise<void> {
  const cache = client.getMutationCache();
  const keys = new Set(["save-subscriptions", "save-interests", "save-curation"]);
  const pending = () => cache.getAll().some((mutation) =>
    mutation.state.status === "pending" && mutation.options.mutationKey?.[0] === "feed"
    && keys.has(String(mutation.options.mutationKey[1])));
  if (!pending()) return Promise.resolve();
  return new Promise((resolve) => {
    const unsubscribe = cache.subscribe(() => {
      if (!pending()) { unsubscribe(); resolve(); }
    });
    if (!pending()) { unsubscribe(); resolve(); }
  });
}

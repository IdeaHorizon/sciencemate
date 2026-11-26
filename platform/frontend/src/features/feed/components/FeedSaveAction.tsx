"use client";

import type { FeedItem } from "@/lib/api";
import { useFeedEngagement, useSaveFeedItem } from "../hooks/useFeed";
import { SaveFolderAction } from "./SaveFolderAction";

export function FeedSaveAction({ item }: { item: FeedItem }) {
  const save = useSaveFeedItem();
  const { undo } = useFeedEngagement();
  return <SaveFolderAction saved={item.saved} folderId={item.saved_folder_id}
    pending={save.isPending || undo.isPending}
    onSave={(folderId, wasSaved) => save.mutateAsync({ itemId: item.id, saved: wasSaved, folderId })}
    onUnsave={() => undo.mutateAsync({ itemId: item.id, action: "save" })} />;
}

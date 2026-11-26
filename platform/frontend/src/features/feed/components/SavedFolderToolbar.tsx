"use client";

import { useState } from "react";
import { FolderPlus } from "lucide-react";
import { Button } from "@/shared/ui";
import { say, useLanguage } from "@/shared/i18n";
import { useCreateFeedFolder, useFeedFolders } from "../hooks/useFeed";

export function SavedFolderToolbar({ selected, onSelect }: { selected: string; onSelect: (id: string) => void }) {
  const lang = useLanguage();
  const folders = useFeedFolders();
  const create = useCreateFeedFolder();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  return <div className="feed-saved-folders">
    <div className="feed-saved-folder-controls">
      <label>{say({ zh: "收藏夹", en: "Folder" }, lang)}
        <select value={selected} onChange={(event) => onSelect(event.target.value)}>
          <option value="all">{say({ zh: "全部收藏", en: "All saved articles" }, lang)}</option>
          <option value="default">{say({ zh: "默认收藏夹", en: "Default folder" }, lang)}{folders.data?.supported ? ` (${folders.data.default_count})` : ""}</option>
          {(folders.data?.folders ?? []).map((folder) => <option key={folder.id} value={folder.id}>{folder.name} ({folder.item_count})</option>)}
        </select>
      </label>
      <Button type="button" variant="secondary" size="sm" iconLeft={<FolderPlus size={14} />}
        disabled={!folders.data?.supported} onClick={() => setCreating((value) => !value)}>{say({ zh: "新建收藏夹", en: "New folder" }, lang)}</Button>
    </div>
    {folders.data?.supported === false && <p>{say({ zh: "当前服务器尚不支持自建收藏夹，升级后可用。已有默认收藏仍可正常使用。", en: "Custom folders require a server update. Existing default saves remain available." }, lang)}</p>}
    {folders.isError && <p role="alert">{say({ zh: "收藏夹加载失败", en: "Folders could not be loaded" }, lang)} <button type="button" className="feed-action" onClick={() => void folders.refetch()}>{say({ zh: "重试", en: "Retry" }, lang)}</button></p>}
    {creating && <form className="feed-folder-create" onSubmit={async (event) => {
      event.preventDefault();
      try {
        const folder = await create.mutateAsync(name.trim());
        setCreating(false); setName(""); onSelect(folder.id);
      } catch { /* Mutation reports the failure. */ }
    }}>
      <label>{say({ zh: "收藏夹名称", en: "Folder name" }, lang)}<input value={name} maxLength={80} autoFocus required disabled={create.isPending} onChange={(event) => setName(event.target.value)} /></label>
      <Button size="sm" type="submit" variant="primary" disabled={!name.trim()} loading={create.isPending}>{say({ zh: "创建", en: "Create" }, lang)}</Button>
      <Button size="sm" type="button" variant="ghost" disabled={create.isPending} onClick={() => setCreating(false)}>{say({ zh: "取消", en: "Cancel" }, lang)}</Button>
    </form>}
  </div>;
}

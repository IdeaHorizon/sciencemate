"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bookmark, BookmarkCheck, X } from "lucide-react";
import { Button } from "@/shared/ui";
import type { FeedItem } from "@/lib/api";
import { say, useLanguage } from "@/shared/i18n";
import { useCreateFeedFolder, useFeedFolders } from "../hooks/useFeed";
import { fc } from "../lib/feed-copy";

const NEW_FOLDER = "__new_folder__";

/** Shared by feed cards, article details and academic search results. */
export function SaveFolderAction({ saved, folderId, onSave, onUnsave, pending = false }: {
  saved: boolean;
  folderId?: string | null;
  onSave: (folderId: string | null, wasSaved: boolean) => Promise<Pick<FeedItem, "saved" | "saved_folder_id">>;
  onUnsave: () => Promise<unknown>;
  pending?: boolean;
}) {
  const lang = useLanguage();
  const [open, setOpen] = useState(false);
  const [local, setLocal] = useState({ saved, folderId });
  useEffect(() => { setLocal({ saved, folderId }); }, [saved, folderId]);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => { setOpen(false); trigger.current?.focus(); }, []);
  return <>
    <button ref={trigger} type="button" className="feed-action" aria-pressed={local.saved}
      aria-haspopup="dialog" aria-expanded={open}
      title={local.saved ? say({ zh: "管理收藏位置", en: "Manage saved location" }, lang) : fc("action.save", lang)}
      onKeyDown={(event) => event.stopPropagation()}
      onClick={(event) => { event.stopPropagation(); setOpen(true); }}>
      {local.saved ? <BookmarkCheck size={15} /> : <Bookmark size={15} />}
      <span>{local.saved ? fc("action.saved", lang) : fc("action.save", lang)}</span>
    </button>
    {open && <SaveFolderDialog saved={local.saved} folderId={local.folderId} pending={pending} onClose={close}
      onSave={async (next) => { const result = await onSave(next, local.saved); setLocal({ saved: result.saved, folderId: result.saved_folder_id ?? null }); }}
      onUnsave={async () => { await onUnsave(); setLocal({ saved: false, folderId: null }); }} />}
  </>;
}

function SaveFolderDialog({ saved, folderId, onSave, onUnsave, onClose, pending }: {
  saved: boolean;
  folderId?: string | null;
  onSave: (folderId: string | null) => Promise<unknown>;
  onUnsave: () => Promise<unknown>;
  onClose: () => void;
  pending: boolean;
}) {
  const lang = useLanguage();
  const titleId = useId();
  const folders = useFeedFolders();
  const create = useCreateFeedFolder();
  const [selected, setSelected] = useState(folderId ?? "default");
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  const select = useRef<HTMLSelectElement>(null);
  const unavailable = folders.data?.supported === false;
  const busy = pending || submitting || create.isPending;
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const rows = folders.data?.folders ?? [];

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    select.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      const dialogs = document.querySelectorAll('[role="dialog"]');
      if (dialogs[dialogs.length - 1] !== dialog.current) return;
      if (event.key === "Escape") { event.preventDefault(); if (!busyRef.current) onClose(); }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>(
        ['button:not(:disabled)', 'input:not(:disabled)', 'select:not(:disabled)', '[tabindex="0"]'].join(","),
      ) ?? []);
      if (focusable.length === 0) { event.preventDefault(); dialog.current?.focus(); return; }
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = previousOverflow; };
  }, [onClose]);

  const save = async () => {
    if (saved && selected === (folderId ?? "default")) { onClose(); return; }
    setSubmitting(true);
    try {
      let destination = selected === "default" ? null : selected;
      if (selected === NEW_FOLDER) {
        const folder = await create.mutateAsync(name.trim());
        destination = folder.id;
        // Retain a successfully created folder if the following save needs retrying.
        setSelected(folder.id);
      }
      await onSave(destination);
      onClose();
    } catch {
      // The mutation owner displays the server's error; keep the choice for retry.
    } finally { setSubmitting(false); }
  };

  return createPortal(<div className="feed-save-backdrop" onClick={(event) => {
    event.stopPropagation();
    if (event.target === event.currentTarget && !busy) onClose();
  }} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") event.stopPropagation(); }}>
    <div ref={dialog} tabIndex={-1} className="feed-save-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <header><h2 id={titleId}>{saved ? say({ zh: "移动收藏", en: "Move saved article" }, lang) : say({ zh: "收藏文章", en: "Save article" }, lang)}</h2>
        <button type="button" className="feed-action" disabled={busy} onClick={onClose} aria-label={say({ zh: "关闭", en: "Close" }, lang)}><X size={18} /></button>
      </header>
      <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <label>{say({ zh: "收藏夹", en: "Folder" }, lang)}
          <select ref={select} value={selected} disabled={busy} onChange={(event) => setSelected(event.target.value)}>
            <option value="default">{say({ zh: "默认收藏夹", en: "Default folder" }, lang)}</option>
            {selected !== "default" && selected !== NEW_FOLDER && !rows.some((folder) => folder.id === selected) && <option value={selected}>{selected === folderId ? say({ zh: "当前收藏夹", en: "Current folder" }, lang) : name.trim() || say({ zh: "所选收藏夹", en: "Selected folder" }, lang)}</option>}
            {rows.map((folder) => <option key={folder.id} value={folder.id}>{folder.name}</option>)}
            {folders.data?.supported && <option value={NEW_FOLDER}>{say({ zh: "新建收藏夹…", en: "Create a folder…" }, lang)}</option>}
          </select>
        </label>
        {selected === NEW_FOLDER && <label>{say({ zh: "收藏夹名称", en: "Folder name" }, lang)}
          <input value={name} maxLength={80} autoFocus disabled={busy} onChange={(event) => setName(event.target.value)} required />
        </label>}
        {folders.isLoading && <p role="status">{say({ zh: "正在加载收藏夹…", en: "Loading folders…" }, lang)}</p>}
        {unavailable && <p role="status">{say({ zh: "当前服务器尚不支持自建收藏夹，升级后可用。仍可保存到默认收藏夹。", en: "Custom folders require a server update. Saving to the default folder is still available." }, lang)}</p>}
        {folders.isError && <p role="alert">{say({ zh: "收藏夹暂时无法加载。", en: "Folders could not be loaded." }, lang)} <button type="button" className="feed-action" onClick={() => void folders.refetch()}>{say({ zh: "重试", en: "Retry" }, lang)}</button></p>}
        <footer>
          {saved && <Button type="button" variant="ghost" disabled={busy} onClick={async () => {
            setSubmitting(true);
            try { await onUnsave(); onClose(); } catch { /* Mutation reports the failure. */ }
            finally { setSubmitting(false); }
          }}>{fc("action.unsave", lang)}</Button>}
          <Button type="submit" variant="primary" loading={busy}
            disabled={(selected === NEW_FOLDER && !name.trim()) || (unavailable && selected !== "default")}>{saved ? say({ zh: "移动到此收藏夹", en: "Move here" }, lang) : say({ zh: "保存到此收藏夹", en: "Save here" }, lang)}</Button>
        </footer>
      </form>
    </div>
  </div>, document.body);
}

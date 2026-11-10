"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUp, Folder } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/shared/ui";
import { useT } from "@/shared/i18n";

/** null uses the installation default; a string is an explicitly chosen address. */
export function ProjectRootFolder({ value, onChange, disabled }: {
  value: string | null;
  onChange: (path: string | null) => void;
  disabled?: boolean;
}) {
  const t = useT();
  const [browsing, setBrowsing] = useState(false);
  const [path, setPath] = useState<string>();
  const [newFolder, setNewFolder] = useState("");
  const listing = useQuery({
    queryKey: ["project-folders", path],
    queryFn: () => api.listProjectFolders(path),
    retry: false,
  });
  const folders = listing.data?.available ? listing.data : undefined;

  if (listing.data?.available === false) return null;

  const choose = (chosen: string) => {
    onChange(chosen);
    setBrowsing(false);
    setNewFolder("");
  };
  const navigate = (next: string) => { setPath(next); setNewFolder(""); };

  return (
    <fieldset className="project-root-folder" disabled={disabled}>
      <legend>{t({ zh: "根文件夹", en: "Root folder" })}</legend>
      <label className="project-root-option">
        <input type="radio" name="project-root-mode" checked={value === null}
          onChange={() => { onChange(null); setBrowsing(false); }} />
        <span>{t({ zh: "默认位置", en: "Default location" })}</span>
      </label>
      {value === null && <p className="project-root-note">
        {folders
          ? t({ zh: "在 {path} 下自动创建独立的项目文件夹。", en: "Create a separate project folder under {path}." }, { path: folders.default_parent })
          : t({ zh: "自动在应用的项目目录下创建文件夹。", en: "Create a folder in the application's projects directory." })}
      </p>}
      <label className="project-root-option">
        <input type="radio" name="project-root-mode" checked={value !== null}
          onChange={() => onChange("")} />
        <span>{t({ zh: "选择文件夹", en: "Choose a folder" })}</span>
      </label>
      {value !== null && <>
        <div className="project-root-input">
          <input required pattern={".*\\S.*"} aria-label={t({ zh: "根文件夹完整路径", en: "Absolute root folder path" })}
            value={value} onChange={(event) => onChange(event.target.value)}
            placeholder={t({ zh: "输入完整路径，或浏览选择", en: "Enter an absolute path or browse" })} />
          <Button type="button" size="sm" onClick={() => setBrowsing(!browsing)} aria-expanded={browsing}>
            {t({ zh: "浏览…", en: "Browse…" })}
          </Button>
        </div>
        <p className="project-root-note">{t({ zh: "选择空文件夹或输入新路径。项目文件和会话工作区将保存在这里。", en: "Choose an empty folder or enter a new path. Project files and session workspaces will be stored here." })}</p>
      </>}
      {browsing && <div className="project-folder-browser" aria-label={t({ zh: "浏览本机文件夹", en: "Browse local folders" })}>
        <div className="project-folder-toolbar">
          <Button type="button" size="sm" variant="ghost" disabled={!folders?.parent || listing.isFetching}
            aria-label={t({ zh: "上一级文件夹", en: "Parent folder" })}
            onClick={() => folders?.parent && navigate(folders.parent)} iconLeft={<ArrowUp size={14} />}>
            {t({ zh: "上一级", en: "Up" })}
          </Button>
          {folders?.roots.map((root) => <Button type="button" key={root} size="sm" variant="ghost" onClick={() => navigate(root)}>{root}</Button>)}
        </div>
        {folders && <code className="project-folder-path">{folders.path}</code>}
        {listing.isFetching ? <p role="status">{t({ zh: "正在读取…", en: "Loading…" })}</p> : folders && <>
          <div className="project-folder-list">
            {folders.directories.map((folder) => <button type="button" key={folder.path} onClick={() => navigate(folder.path)}>
              <Folder size={14} /><span>{folder.name}</span>
            </button>)}
            {!folders.directories.length && <p>{t({ zh: "没有子文件夹", en: "No subfolders" })}</p>}
          </div>
          <div className="project-root-input">
            <input aria-label={t({ zh: "新文件夹名称", en: "New folder name" })} value={newFolder}
              onChange={(event) => setNewFolder(event.target.value)}
              placeholder={t({ zh: "或在此处新建文件夹", en: "Or create a folder here" })} />
            <Button type="button" size="sm" disabled={!newFolder.trim() || /[\\/]/.test(newFolder) || [".", ".."].includes(newFolder.trim())}
              onClick={() => choose(`${folders.path.replace(/[\\/]$/, "")}/${newFolder.trim()}`)}>
              {t({ zh: "使用新文件夹", en: "Use new folder" })}
            </Button>
          </div>
          <div className="project-folder-actions">
            <Button type="button" size="sm" variant="ghost" onClick={() => setBrowsing(false)}>{t({ zh: "取消", en: "Cancel" })}</Button>
            <Button type="button" size="sm" disabled={!folders.empty} onClick={() => choose(folders.path)}>
              {t({ zh: "选择当前文件夹", en: "Choose this folder" })}
            </Button>
          </div>
          {!folders.empty && <p className="project-root-note">{t({ zh: "当前文件夹已有内容。请选择空文件夹，或填写上方的新文件夹名称。", en: "This folder contains files. Choose an empty folder or enter a new folder name above." })}</p>}
        </>}
      </div>}
      {listing.isError && <div>
        <p role="alert" className="project-root-error">{listing.error.message}</p>
        <Button type="button" size="sm" variant="ghost"
          onClick={() => path ? setPath(undefined) : void listing.refetch()}>
          {path ? t({ zh: "返回主文件夹", en: "Back to home folder" }) : t({ zh: "重试", en: "Retry" })}
        </Button>
      </div>}
    </fieldset>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { qk } from "@/lib/query/keys";

import { fetchFilePreview } from "../api/file-preview";
import { useT, useLanguage } from "@/shared/i18n";

/**
 * 消息正文里内联的一张工作区图 —— 调度器说「你看这张图」时真的看得到图。
 *
 * ## 为什么不是 `<img src="/api/…">`
 *
 * 凭据是内存里的 Bearer header，浏览器发 `<img>` 请求时不会带上它，那条路必然
 * 401（同 FilePreview 的理由）。所以走一次带鉴权的 fetch 拿 blob，再转 object URL。
 *
 * 附带的好处：blob 是不透明源，即使取回来的是带脚本的 SVG，放进 `<img>` 也
 * 不执行。**只用 `<img>`，永远不要内联进 DOM。**
 *
 * ## 取不到就退回成文件名
 *
 * 模型可能写出一个不存在的路径（重构路径时把前缀省略了、或者引用了还没落盘
 * 的文件）。那时画一个裂图比什么都不画更糟 —— 它看起来像平台坏了。这里退回
 * 成一个可点的文件名：信息没丢，也没有假装成功。
 */
export function InlineWorkspaceImage({
  projectId, sessionId, path, alt, onOpen,
}: {
  projectId: string;
  sessionId: string;
  path: string;
  alt: string;
  onOpen: (path: string) => void;
}) {
  const t = useT();
  const lang = useLanguage();
  // 与右栏预览共用 query key：同一张图在正文和右栏各画一次，只下载一次。
  const query = useQuery({
    queryKey: qk.projectFilePreview(projectId, sessionId, path),
    queryFn: () => fetchFilePreview(projectId, path, sessionId || undefined, lang),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const url = useObjectUrl(query.data?.mode === "image" ? query.data.blob : null);
  const label = alt || path.split("/").pop() || path;

  if (query.isLoading) {
    return <span className="inline-workspace-image is-loading">正在读取 {label}…</span>;
  }
  // 取不到、或者服务端说它不是图（扩展名骗人）→ 退回成可点的文件名。
  if (query.error || !url) {
    return (
      <button
        type="button"
        className="chat-run-file-open"
        onClick={() => onOpen(path)}
        title={query.error ? t({ zh: `打不开 ${path}`, en: `Cannot open ${path}` }) : t({ zh: `在右栏打开 ${path}`, en: `Open ${path} in the side panel` })}
      >
        <code>{label}</code>
      </button>
    );
  }

  return (
    <button
      type="button"
      className="inline-workspace-image"
      onClick={() => onOpen(path)}
      title={t({ zh: `${path}（点开在右栏看大图）`, en: `${path} (open it in the side panel for the full size)` })}
    >
      {/* eslint-disable-next-line @next/next/no-img-element --
          next/image 优化不了 blob: URL（它要的是构建期已知的远端/本地资源，
          而这里的字节是运行时带鉴权 fetch 回来的）。换成 <Image> 只会让它
          去请求一个它解析不了的 src。 */}
      <img src={url} alt={alt || path} />
    </button>
  );
}

/** blob → object URL，卸载时 revoke（同 FilePreview 里那份的理由）。 */
function useObjectUrl(blob: Blob | null | undefined): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!blob) {
      setUrl(null);
      return;
    }
    const next = URL.createObjectURL(blob);
    setUrl(next);
    return () => {
      URL.revokeObjectURL(next);
      setUrl(null);
    };
  }, [blob]);
  return url;
}

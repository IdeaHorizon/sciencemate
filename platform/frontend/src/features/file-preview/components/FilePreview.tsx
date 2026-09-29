"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, RefreshCw } from "lucide-react";

import { qk } from "@/lib/query/keys";
import { RichText } from "@/features/chat/components/RichText";

import { fetchFilePreview, FileTooLargeError, type FilePreviewPayload } from "../api/file-preview";
import { useT, useLanguage } from "@/shared/i18n";

/**
 * 一个工作区文件在右栏里长什么样。
 *
 * 节点产出的图（`paper/figures/fig1.png`）和论文（`latex_build/main.pdf`）
 * 此前在界面上只有一个文件名 —— 后端那条 JSON 读取路径遇到二进制直接回
 * `content: null`。这个组件配合新的字节出口把它们真的画出来。
 */

export function FilePreview({
  projectId, sessionId, path,
}: {
  projectId: string;
  sessionId: string;
  path: string;
}) {
  const t = useT();
  const lang = useLanguage();
  const query = useQuery({
    queryKey: qk.projectFilePreview(projectId, sessionId, path),
    queryFn: () => fetchFilePreview(projectId, path, sessionId || undefined, lang),
    // 产出会被重跑覆盖，但**不轮询**：开着一份 PDF 不该每隔几秒重下一遍。
    // 想看新的就点刷新 —— 那是一个明确的意图，不该靠猜。
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  return (
    <div className="file-preview">
      <div className="file-preview-bar">
        <code title={path}>{path}</code>
        <div className="file-preview-bar-actions">
          {query.data && <SizeLabel bytes={query.data.sizeBytes} />}
          <button
            type="button"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
            aria-label={t({ zh: "重新读取这个文件", en: "Re-read this file" })}
            title={t({ zh: "重新读取（节点重跑之后用）", en: "Re-read (after a node runs again)" })}
          >
            <RefreshCw size={12} className={query.isFetching ? "is-spinning" : ""} />
          </button>
          {query.data && <DownloadLink payload={query.data} />}
        </div>
      </div>
      <div className="file-preview-body">
        {query.isLoading && <p className="muted">{t({ zh: "正在读取…", en: "Reading…" })}</p>}
        {query.error && <PreviewError error={query.error} />}
        {query.data && <PreviewBody payload={query.data} />}
      </div>
    </div>
  );
}

function PreviewBody({ payload }: { payload: FilePreviewPayload }) {
  const t = useT();
  // object URL 的生命周期绑在组件上，不绑在 React Query 的缓存里：缓存被清掉
  // 的时机是库说了算的，而 revoke 晚了就是内存泄漏、早了图就变成裂图。
  // 缓存里放的是 blob（惰性数据），URL 在这里现造现销。
  const url = useObjectUrl(
    payload.mode === "image" || payload.mode === "pdf" || payload.mode === "html"
      ? payload.blob
      : null,
  );

  if (payload.mode === "markdown") {
    return (
      <div className="file-preview-text">
        <RichText text={payload.text ?? ""} />
        {payload.textTruncated && <p className="muted">{t({ zh: "（内容过长，只显示了前面一部分）", en: "(too long; only the beginning is shown)" })}</p>}
      </div>
    );
  }

  if (payload.mode === "text") {
    return (
      <div className="file-preview-text">
        <pre><code>{payload.text}</code></pre>
        {payload.textTruncated && <p className="muted">{t({ zh: "（内容过长，只显示了前面一部分）", en: "(too long; only the beginning is shown)" })}</p>}
      </div>
    );
  }

  if (!url) return <p className="muted">{t({ zh: "正在准备…", en: "Preparing…" })}</p>;

  if (payload.mode === "image") {
    // SVG 也走 `<img>` 而不是内联进 DOM：img 里的 SVG 不执行脚本，而
    // dangerouslySetInnerHTML 会让 agent 写的 svg 变成一个 XSS 入口。
    return (
      <div className="file-preview-image">
        {/* eslint-disable-next-line @next/next/no-img-element --
            next/image 优化不了 blob: URL（它要的是构建期已知的远端/本地资源，
            而这里的字节是运行时带鉴权 fetch 回来的）。换成 <Image> 只会让它
            去请求一个它解析不了的 src。 */}
        <img src={url} alt={payload.path} />
      </div>
    );
  }

  if (payload.mode === "pdf") {
    // 浏览器自带的 PDF 阅读器。引 pdf.js 要多背一个大依赖 + 一个 worker，
    // 换来的只是自绘工具栏。
    return (
      <iframe className="file-preview-frame" src={url} title={payload.path} />
    );
  }

  if (payload.mode === "html") {
    // agent 写的网页 = 不可信内容。sandbox 不带 allow-same-origin，页面落进
    // 不透明源：脚本跑不起来，也够不着应用的 storage / cookie。
    return (
      <iframe
        className="file-preview-frame"
        src={url}
        title={payload.path}
        sandbox=""
      />
    );
  }

  return (
    <div className="file-preview-fallback">
      <p>这个格式没法在浏览器里直接看（{payload.mediaType}）。</p>
      <p className="muted">{t({ zh: "下载下来用本机的程序打开。", en: "Download it and open with an app on this machine." })}</p>
    </div>
  );
}

function PreviewError({ error }: { error: unknown }) {
  const t = useT();
  if (error instanceof FileTooLargeError) {
    return (
      <div className="file-preview-fallback">
        <p>文件太大，不在浏览器里打开（{formatBytes(error.sizeBytes)}，上限 {formatBytes(error.maxBytes)}）。</p>
      </div>
    );
  }
  return <p className="file-preview-error">{error instanceof Error ? error.message : t({ zh: "打不开这个文件", en: "This file cannot be opened" })}</p>;
}

function DownloadLink({ payload }: { payload: FilePreviewPayload }) {
  const t = useT();
  const url = useObjectUrl(payload.blob);
  const name = payload.path.split("/").pop() || payload.path;
  if (!url) return null;
  return (
    <a href={url} download={name} aria-label={t({ zh: `下载 ${name}`, en: `Download ${name}` })} title={t({ zh: "下载", en: "Download" })}>
      <Download size={12} />
    </a>
  );
}

function SizeLabel({ bytes }: { bytes: number }) {
  return <small className="file-preview-size">{formatBytes(bytes)}</small>;
}

/** blob → object URL，卸载时 revoke。null 进 null 出，好让调用方按模式决定要不要。 */
function useObjectUrl(blob: Blob | null): string | null {
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

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

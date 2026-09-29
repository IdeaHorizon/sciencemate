import { API_BASE_URL, api } from "@/lib/api";

import {
  isTextualMode,
  normalizeMediaType,
  previewModeFor,
  TEXT_PREVIEW_MAX_CHARS,
  type PreviewMode,
} from "../lib/preview-mode";
import { say, type Language } from "@/shared/i18n";

/**
 * 取一个工作区文件的字节，用来在右栏画出来。
 *
 * ## 为什么不能直接 `<img src="/api/…">`
 *
 * 凭据是内存里的 Bearer header（见 `ApiClient.fetchWithAuth`），不是 cookie。
 * 浏览器发 `<img>` / `<iframe>` 的请求时不会带上它 —— 那条路必然 401。所以
 * 每个预览都是一次带鉴权的 fetch，拿到 blob 再由组件转成 object URL。
 *
 * 附带的好处：blob 是不透明源，sandbox 的 iframe 里跑 agent 写的 HTML 时，
 * 它够不着应用自己的 localStorage / cookie。
 */

export class FileTooLargeError extends Error {
  readonly sizeBytes: number;
  readonly maxBytes: number;

  constructor(message: string, sizeBytes: number, maxBytes: number) {
    super(message);
    this.name = "FileTooLargeError";
    this.sizeBytes = sizeBytes;
    this.maxBytes = maxBytes;
  }
}

export type FilePreviewPayload = {
  path: string;
  mediaType: string;
  mode: PreviewMode;
  blob: Blob;
  sizeBytes: number;
  /** 只有文本类模式有；已按 TEXT_PREVIEW_MAX_CHARS 截断。 */
  text?: string;
  textTruncated?: boolean;
};

export function rawFileUrl(projectId: string, path: string, sessionId?: string): string {
  const session = sessionId ? `&sessionId=${encodeURIComponent(sessionId)}` : "";
  return (
    `${API_BASE_URL}/projects/${encodeURIComponent(projectId)}/repository/raw`
    + `?path=${encodeURIComponent(path)}${session}`
  );
}

export async function fetchFilePreview(
  projectId: string,
  path: string,
  sessionId?: string,
  // 兜底文案要跟着界面语言 —— 后端给了 detail 就用后端那句（它也已经按
  // 用户的语言给了），这里只负责"后端什么都没说"的那一支。
  lang: Language = "zh",
): Promise<FilePreviewPayload> {
  const response = await api.fetchWithAuth(rawFileUrl(projectId, path, sessionId));

  if (!response.ok) {
    // 413 带着上限回来 —— 界面要能说清"多大、上限多少"，而不是笼统的打不开。
    const body = await response.json().catch(() => null);
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (response.status === 413 && detail && typeof detail === "object") {
      const { message, sizeBytes, maxBytes } = detail as {
        message?: string; sizeBytes?: number; maxBytes?: number;
      };
      throw new FileTooLargeError(
        message ?? say({ zh: "文件太大，无法预览", en: "This file is too large to preview" }, lang),
        Number(sizeBytes) || 0,
        Number(maxBytes) || 0,
      );
    }
    const message = typeof detail === "string"
      ? detail
      : say({ zh: "打不开这个文件（{status}）", en: "This file could not be opened ({status})" }, lang, { status: response.status });
    throw new Error(message);
  }

  const blob = await response.blob();
  const mediaType = normalizeMediaType(response.headers.get("content-type") ?? "");
  const mode = previewModeFor(mediaType);

  if (!isTextualMode(mode)) {
    return { path, mediaType, mode, blob, sizeBytes: blob.size };
  }

  const full = await blob.text();
  return {
    path,
    mediaType,
    mode,
    blob,
    sizeBytes: blob.size,
    text: full.slice(0, TEXT_PREVIEW_MAX_CHARS),
    textTruncated: full.length > TEXT_PREVIEW_MAX_CHARS,
  };
}

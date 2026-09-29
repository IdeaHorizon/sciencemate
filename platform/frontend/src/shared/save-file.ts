/**
 * 把一个 Blob 当成下载交给浏览器（或桌面壳）。
 *
 * 走 `<a download>` 这一条：浏览器直接存盘；Mac 壳把它变成一次 WKDownload、弹
 * 保存面板（`platform/desktop/mac/Shell.swift` 的 `decidePolicyFor`）；Windows 壳的
 * WebView2 自己处理下载。三处用的是同一个入口，所以别换成 `window.open`。
 *
 * object URL 不马上收：Mac 上保存面板是模态的，人选位置可能要一会儿，
 * 下载真正读 Blob 可能在那之后。十分钟后收掉。
 */
export function saveBlobAs(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10 * 60 * 1000);
}

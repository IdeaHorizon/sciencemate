import { say, type Language } from "../../../shared/i18n/language.ts";

/**
 * 会话诊断包：后端把这个会话的全部运行记录和同时段的后端日志打成一个 zip
 * （`GET /projects/{p}/sessions/{s}/diagnostics`，见后端
 * `app/services/session_diagnostics.py`）。这里是界面这一侧的两件小事：
 * 存成什么名字、失败时说什么。
 */

/** 存盘用的文件名：服务器在 Content-Disposition 里给了就用它，没给就自己拼一个同形的。 */
export function diagnosticsFilename(contentDisposition: string | null, sessionId: string): string {
  const match = contentDisposition?.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const named = match ? decodeURIComponent(match[1]).trim() : "";
  if (named && !/[\\/]/.test(named)) return named;
  return `session-diagnostics-${sessionId.slice(0, 8)}.zip`;
}

/**
 * 没拿到包时给人看的那句话。
 *
 * 404 有两种：会话不在了（后端说 "Session not found"），或者回答这个请求的
 * 服务器根本没有这个接口（FastAPI 的 "Not Found"）。后一种是真会遇到的 ——
 * 项目住在组织服务器上、而那台服务器比这台桌面旧。说清该升级哪一边，
 * 而不是一句"出错了"。
 */
export function diagnosticsFailureMessage(status: number, detail: unknown, lang: Language): string {
  if (status === 404 && detail === "Not Found") {
    return say({
      zh: "这个会话所在的服务器还不支持诊断包。请让管理员把组织服务器升级到最新版。",
      en: "The server this session lives on does not support diagnostics yet. Ask your administrator to update the organisation server.",
    }, lang);
  }
  if (typeof detail === "string" && detail) {
    return say({ zh: "诊断包没能生成：{detail}", en: "The diagnostics could not be prepared: {detail}" }, lang, { detail });
  }
  return say({ zh: "诊断包没能生成（HTTP {status}）", en: "The diagnostics could not be prepared (HTTP {status})" }, lang, { status });
}

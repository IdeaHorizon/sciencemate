import type {Phrase} from "../../../shared/i18n/language.ts";

/** Also sanitizes old cached responses without mutating diagnostic evidence. */
export function sourceWarning(warning:Record<string,unknown>, t:(phrase:Phrase)=>string): string | undefined {
  const raw = warning.message || warning.detail;
  if (typeof raw !== "string") return undefined;
  if (warning.code !== "source_request_failed") return raw;
  const status = Number(warning.http_status || raw.match(/HTTP\s+(\d{3})/)?.[1]) || undefined;
  const sources = Array.isArray(warning.sources) ? warning.sources.join("、") : "";
  const name = sources || t({zh:"部分来源",en:"Some sources"});
  const code = status ? `（HTTP ${status}）` : "";
  const reason = status === 429 ? t({zh:"请求受到限流",en:"request rate limited"})
    : warning.reason === "timeout" ? t({zh:"请求超时",en:"request timed out"})
    : t({zh:"检索服务暂不可用",en:"search service temporarily unavailable"});
  return `${name}：${reason}${code}。${t({zh:"本次已跳过，不影响其他来源结果。",en:"Skipped for this search; other source results remain available."})}`;
}

/** A batch-level empty response is not an empty search. Keep diagnostics intact. */
export function sourceNotices(warnings:Record<string,unknown>[], options:{hasRemoteResults:boolean;coveredSources:string[]}, t:(phrase:Phrase)=>string):string[] {
  const messages=new Set<string>();
  for (const warning of warnings) {
    if (["source_no_matches","low_relevance_candidates_excluded"].includes(String(warning.code))) continue;
    if (warning.code==="remote_search_no_match" && options.hasRemoteResults) continue;
    if (Array.isArray(warning.sources) && warning.sources.length
        && warning.sources.every(source=>options.coveredSources.includes(String(source)))) continue;
    const detail=sourceWarning(warning,t)?.trim();
    if (detail) messages.add(detail);
  }
  return [...messages];
}

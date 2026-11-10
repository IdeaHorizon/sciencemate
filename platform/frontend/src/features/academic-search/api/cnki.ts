import {api, type LiteratureSearchResponse, type LiteratureQueryGroup} from "@/lib/api";

export type CnkiCategory = "domain" | "journal" | "scholar" | "project";
export type CnkiDocumentKind = "journal" | "thesis" | "conference" | "book" | "other";
export type CnkiJob = {
  id: string; query: string; category: CnkiCategory; recurring: boolean; enabled: boolean;
  state: string; count: number; total: number; page: number; detail: string;
  phase?: "index" | "metadata";
  last_completed_at: string | null; result?: LiteratureSearchResponse;
  paper_ids?: string[]; next_cursor?: string; has_more?: boolean;
  document_kind?: CnkiDocumentKind | null;
};
export type CnkiDevice = {id: string; name: string; online: boolean; state: string; last_seen_at: string | null; detail: string};
export type CnkiCollector = {mode:"server";available:boolean;detail:string};
export const cnkiStatus = (includeJobs = true) => api.request<{collector:CnkiCollector;devices:CnkiDevice[];jobs:CnkiJob[]}>(`/cnki/status?include_jobs=${includeJobs}`);
export function wakeCnki() { window.postMessage({type:"sciencemate-cnki-wake"},window.location.origin); }
export async function createCnkiJob(query: string, recurring = false, category: CnkiCategory = "domain", limit: number | null = null, document_kind?:CnkiDocumentKind) {
  const result = await api.request<CnkiJob>("/cnki/jobs?page_size=200",{method:"POST",body:JSON.stringify({query,recurring,category,limit,document_kind})});
  return result;
}
export const retryCnkiJob = (id:string) => api.request<CnkiJob>(`/cnki/jobs/${encodeURIComponent(id)}/retry`,{method:"POST"});
export const readCnkiJob = (id:string,cursor?:string) => api.request<CnkiJob>(`/cnki/jobs/${encodeURIComponent(id)}?page_size=200${cursor?`&cursor=${encodeURIComponent(cursor)}`:""}`);
export const cancelCnkiJob = (id:string) => api.request(`/cnki/jobs/${encodeURIComponent(id)}`,{method:"DELETE"});

export const planLiteratureSearch = (query:string) => api.request<{search_plan:LiteratureQueryGroup[]}>("/literature/plan",{method:"POST",body:JSON.stringify({query})});

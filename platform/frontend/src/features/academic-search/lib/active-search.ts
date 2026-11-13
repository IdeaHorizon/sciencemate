import {api} from "@/lib/api";
import {planLiteratureSearch,cnkiStatus, createCnkiJob, readCnkiJob, retryCnkiJob, cancelCnkiJob, resetCnkiSession} from "../api/cnki";
import {createAcademicSearchSession} from "./search-session";

export const academicSearchSession = createAcademicSearchSession({
  stream:(...args)=>api.streamLiteratureSearch(...args),
  plan:planLiteratureSearch,
  status:()=>cnkiStatus(false),
  create:(query,limit,kind)=>createCnkiJob(query,false,"domain",limit,kind),
  read:readCnkiJob,retry:retryCnkiJob,cancelJob:cancelCnkiJob,
  resetSession:resetCnkiSession,
  sleep:()=>new Promise(resolve=>setTimeout(resolve,2500)),
});
let owner:string|null = null;
export function setAcademicSearchOwner(userId:string|null) {
  if (owner !== userId) {academicSearchSession.reset();owner = userId;}
}
export function clearAcademicSearchSession() {
  academicSearchSession.reset();owner = null;
}

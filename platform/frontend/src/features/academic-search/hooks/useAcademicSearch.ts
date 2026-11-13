"use client";

import {useLanguage} from "@/shared/i18n";
import {useSyncExternalStore} from "react";
import {academicSearchSession as session} from "../lib/active-search";

export function useAcademicSearch() {
  const language = useLanguage();
  const state = useSyncExternalStore(session.subscribe,session.getSnapshot,session.getServerSnapshot);
  return {...state,isError:Boolean(state.error),start:(input:Omit<Parameters<typeof session.start>[0],"language">)=>session.start({...input,language}),stopWaiting:session.stopWaiting,
    retryChinese:session.retryChinese,resetChineseSession:session.resetChineseSession,loadMore:session.loadMore,setQuery:session.setQuery};
}

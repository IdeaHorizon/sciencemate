"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Sparkles } from "lucide-react";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Skeleton } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { useLanguage } from "@/shared/i18n";
import { curationStatusLine } from "../lib/curation-presentation";
import { fc } from "../lib/feed-copy";

/**
 * 自动挖掘开关。
 *
 * ## 为什么模型不在这里选
 *
 * 「用哪个大模型」在这个平台上有唯一的真相源：模型角色体系
 * （`shared/model_roles.yaml` + 设置 → 模型）。在这里再放一个选择器，就是
 * 第二处"这个用户用哪个模型"的答案 —— 两处迟早不一致，而不一致时两边都
 * 不报错，只是运行时用的和界面显示的不是同一个。
 *
 * 所以这里**显示**当前为你服务的挖掘模型，改则跳去那唯一的地方改。
 *
 * ## 为什么 available 和 enabled 分开显示
 *
 * 「平台没配挖掘模型」和「我自己没开」是两件事，处置完全不同（一个要找管理员，
 * 一个自己点一下）。合成一个"开关是灰的"，用户无从分辨。
 */
export function CurationSwitch() {
  const queryClient = useQueryClient();
  const lang = useLanguage();
  const curation = useQuery({
    queryKey: qk.feedCuration(),
    queryFn: () => api.getFeedCuration(),
  });

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.setFeedCuration(enabled),
    onSuccess: () => {
      // 开关一变，方向和今天的选摘都可能变 —— 整片重取。
      void queryClient.invalidateQueries({ queryKey: ["feed"] });
    },
    onError: (error) => {
      // 后端把"为什么打不开"写成人话（没配模型、去哪配），原样给用户。
      pushError(error instanceof Error ? error.message : fc("curation.toggle_failed", lang));
    },
  });

  if (curation.isLoading) return <Skeleton height={92} />;
  const state = curation.data;
  if (!state) return null;

  const status = curationStatusLine(state, lang);

  return (
    <section className={state.enabled ? "feed-curation feed-curation-on" : "feed-curation"}>
      <header>
        <Sparkles size={16} aria-hidden />
        <div>
          <h3>{fc("curation.title", lang)}</h3>
          <p>{fc("curation.subtitle", lang)}</p>
        </div>
        <label className="feed-switch">
          <input
            type="checkbox"
            role="switch"
            checked={state.enabled}
            disabled={!state.available || toggle.isPending}
            aria-label={fc("curation.switch_label", lang)}
            onChange={(event) => toggle.mutate(event.target.checked)}
          />
          <span aria-hidden />
        </label>
      </header>

      <p className={status.tone === "error" ? "feed-curation-note feed-curation-error" : "feed-curation-note"}>
        {status.tone === "error" && <AlertTriangle size={13} aria-hidden />}
        {status.text}
      </p>

      {/* 模型只显示、不在这里选 —— 见上面的 docstring。 */}
      <p className="feed-curation-model">
        {fc("curation.model_line", lang)}
        {state.model_label ?? fc("curation.model_missing", lang)}
        <Link href="/settings/models">
          {state.available ? fc("curation.model_change", lang) : fc("curation.model_configure", lang)}
        </Link>
      </p>

      {state.enabled && state.inferred_queries.length > 0 && (
        // 检索词也摊开给他看。他能一眼判断"这几个词搜出来的东西会不会是我要的"，
        // 而这正是一个推荐系统最该让人验证的东西。
        <p className="feed-curation-queries">
          {fc("curation.queries_line", lang)}
          {state.inferred_queries.map((query) => (
            <code key={query}>{query}</code>
          ))}
        </p>
      )}
    </section>
  );
}

"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, ChevronRight, Search, Sparkles, X } from "lucide-react";
import { Button, Skeleton } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { CurationSwitch } from "./CurationSwitch";
import { useLanguage } from "@/shared/i18n";
import { fc } from "../lib/feed-copy";
import { useFeedDomains, useFeedInterests, useRejectInferred, useSaveInterests } from "../hooks/useFeed";

/**
 * 选关注方向。
 *
 * ## 为什么不是空白问卷
 *
 * 一个刚注册的科学家面对 155 个 arXiv 分类，最可能的动作是关掉它。所以后端
 * 会从他已有的 Project 方向里机械猜几个（`suggested`），这里**默认勾上**，
 * 让他改而不是让他填。
 *
 * 猜不出来的时候不编 —— 那种情况下他看到的是一个干净的搜索框，而不是六个
 * 随便凑的分类。
 */
export function InterestPicker({
  onDone,
  showSkip = true,
}: {
  onDone?: () => void;
  showSkip?: boolean;
}) {
  const lang = useLanguage();
  const interests = useFeedInterests();
  const catalog = useFeedDomains(true);
  const save = useSaveInterests();
  const reject = useRejectInferred();
  const [selected, setSelected] = useState<string[] | null>(null);
  const [query, setQuery] = useState("");
  // 展开哪些一级学科。默认全折叠 —— 只显示一级，点开才看到二级，避免 88 个
  // 一级 × 数个二级全部平铺、要滚很久。搜索时强制全部展开（过滤结果本来就少）。
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggleExpanded = (archive: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(archive)) next.delete(archive);
      else next.add(archive);
      return next;
    });
  };

  // 服务端的现状是初始值。`null` 表示"还没从服务端拿到"，与"用户清空了选择"
  // 是两回事 —— 用 `[]` 表示后者会让首次加载把已有兴趣冲掉。
  useEffect(() => {
    if (selected === null && interests.data) {
      const initial = interests.data.domains.length
        ? interests.data.domains
        : interests.data.suggested.map((option) => option.domain);
      setSelected(initial);
    }
  }, [interests.data, selected]);

  const groups = useMemo(() => catalog.data?.groups ?? [], [catalog.data]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return groups;
    return groups
      .map((group) => ({
        ...group,
        categories: group.categories.filter(
          (option) =>
            option.label.toLowerCase().includes(needle) ||
            option.domain.toLowerCase().includes(needle),
        ),
      }))
      .filter((group) => group.categories.length > 0 || group.label.toLowerCase().includes(needle));
  }, [groups, query]);

  if (interests.isLoading || catalog.isLoading || selected === null) {
    return <Skeleton height={280} />;
  }
  if (catalog.isError) {
    return (
      <p className="feed-empty-note">{fc("picker.vocabulary_down", lang)}</p>
    );
  }

  const toggle = (domain: string) => {
    setSelected((current) => {
      const list = current ?? [];
      return list.includes(domain) ? list.filter((d) => d !== domain) : [...list, domain];
    });
  };

  const submit = async () => {
    try {
      await save.mutateAsync(selected);
      onDone?.();
    } catch (caught) {
      pushError(caught instanceof Error ? caught.message : fc("picker.save_failed", lang));
    }
  };

  const suggestedIds = new Set(interests.data?.suggested.map((o) => o.domain) ?? []);
  const inferred = interests.data?.inferred ?? [];

  return (
    <div className="feed-picker">
      <div className="feed-picker-search">
        <Search size={15} aria-hidden />
        <input
          type="search"
          value={query}
          placeholder={fc("picker.search.placeholder", lang)}
          aria-label={fc("picker.search.label", lang)}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      <CurationSwitch />

      {inferred.length > 0 && (
        // 推断出来的**单独列**，标明出处、可单独删。混进手选的里面，它就成了
        // 一个替你决定看什么的黑箱；而删掉一条会被记住，下次不再推。
        <section className="feed-inferred">
          <header>
            <Sparkles size={14} aria-hidden />
            <span>{fc("picker.inferred.header", lang)}</span>
          </header>
          <div className="feed-chip-row">
            {inferred.map((option) => (
              <span key={option.domain} className="feed-chip feed-chip-inferred">
                {option.label}
                <button
                  type="button"
                  aria-label={fc("picker.inferred.drop", lang, { label: option.label })}
                  title={fc("picker.inferred.drop_hint", lang)}
                  disabled={reject.isPending}
                  onClick={() => reject.mutate(option.domain)}
                >
                  <X size={12} />
                </button>
              </span>
            ))}
          </div>
        </section>
      )}

      {suggestedIds.size > 0 && !interests.data?.domains.length && (
        <p className="feed-picker-hint">{fc("picker.suggested_hint", lang)}</p>
      )}

      <div className="feed-picker-groups">
        {filtered.map((group) => {
          const isExpanded = query.trim() !== "" || expanded.has(group.archive);
          const hitCount = group.categories.filter((option) =>
            selected.includes(option.domain),
          ).length;
          return (
            <section key={group.archive} className="feed-picker-group">
              <header className="feed-picker-group-head">
                <button
                  type="button"
                  className="feed-group-toggle"
                  aria-expanded={isExpanded}
                  aria-label={
                    isExpanded ? fc("picker.collapse", lang) : fc("picker.expand", lang)
                  }
                  onClick={() => toggleExpanded(group.archive)}
                >
                  <ChevronRight
                    size={15}
                    aria-hidden
                    className={isExpanded ? "feed-chevron feed-chevron-open" : "feed-chevron"}
                  />
                  <span className="feed-group-label">{group.label}</span>
                  {hitCount > 0 && (
                    <span className="feed-group-hit">
                      {fc("picker.group_selected", lang, { n: hitCount })}
                    </span>
                  )}
                </button>
              </header>
              {isExpanded && (
                <div className="feed-chip-row">
                  <button
                    type="button"
                    className={
                      selected.includes(group.archive)
                        ? "feed-chip feed-chip-on feed-chip-archive"
                        : "feed-chip feed-chip-archive"
                    }
                    onClick={() => toggle(group.archive)}
                    title={fc("picker.whole_archive", lang, { label: group.label })}
                  >
                    {selected.includes(group.archive) && <Check size={13} aria-hidden />}
                    {fc("picker.archive_hint", lang)}
                  </button>
                  {group.categories.map((option) => (
                    <button
                      key={option.domain}
                      type="button"
                      className={
                        selected.includes(option.domain)
                          ? "feed-chip feed-chip-on"
                          : "feed-chip"
                      }
                      onClick={() => toggle(option.domain)}
                      title={option.domain}
                    >
                      {selected.includes(option.domain) && <Check size={13} aria-hidden />}
                      {option.label}
                      {suggestedIds.has(option.domain) && (
                        <em>{fc("picker.suggested_tag", lang)}</em>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </section>
          );
        })}
      </div>

      <footer className="feed-picker-foot">
        <div className="feed-picker-foot-left">
          <span className="feed-picker-count">{fc("picker.count", lang, { n: selected.length })}</span>
          {selected.length > 0 && (
            <Button
              variant="ghost"
              title={fc("picker.clear_hint", lang)}
              onClick={() => setSelected([])}
            >
              {fc("picker.clear", lang)}
            </Button>
          )}
        </div>
        <div className="feed-picker-buttons">
          {showSkip && (
            // "什么都不订，只看全站" 也是一个明确的选择。存空列表照样算
            // onboarded —— 否则每次进来都会再弹一次问卷。
            <Button variant="ghost" onClick={() => { setSelected([]); void save.mutateAsync([]).then(() => onDone?.()); }}>
              {fc("picker.skip", lang)}
            </Button>
          )}
          <Button variant="primary" loading={save.isPending} onClick={submit}>
            {fc("picker.save", lang)}
          </Button>
        </div>
      </footer>
    </div>
  );
}

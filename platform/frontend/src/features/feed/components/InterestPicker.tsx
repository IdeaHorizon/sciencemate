"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, ChevronRight, Search, X } from "lucide-react";
import { Button, Skeleton } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { say, useLanguage } from "@/shared/i18n";
import { fc } from "../lib/feed-copy";
import { useFeedDomains, useFeedInterests, useSaveInterests } from "../hooks/useFeed";

/** 手动选择订阅学科，独立于课题自动挖掘。 */
export function InterestPicker({
  onDone,
  onChange,
  showSkip = true,
}: {
  onDone?: () => void;
  onChange?: () => void;
  showSkip?: boolean;
}) {
  const lang = useLanguage();
  const interests = useFeedInterests();
  const catalog = useFeedDomains(true);
  const save = useSaveInterests();
  const [selected, setSelected] = useState<string[] | null>(null);
  const [query, setQuery] = useState("");
  const [activeGroup, setActiveGroup] = useState<string | null>(null);
  useEffect(() => {
    if (!activeGroup) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setActiveGroup(null); };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [activeGroup]);

  // 服务端的现状是初始值。`null` 表示"还没从服务端拿到"，与"用户清空了选择"
  // 是两回事 —— 用 `[]` 表示后者会让首次加载把已有兴趣冲掉。
  useEffect(() => {
    if (selected === null && interests.data) {
      const initial = interests.data.domains;
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
    onChange?.();
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

  const dialogGroup = groups.find((group) => group.archive === activeGroup);

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

      {selected.length > 0 && (
        <section className="feed-selected-domains" aria-label={say({ zh: "已选方向", en: "Selected areas" }, lang)}>
          <header><Check size={14} aria-hidden /><span>{say({ zh: "已选方向", en: "Selected areas" }, lang)}</span></header>
          <div className="feed-chip-row">
            {selected.map((domain) => {
              const index = interests.data?.domains.indexOf(domain) ?? -1;
              const label = index >= 0 ? interests.data?.domain_labels[index] : undefined;
              const catalogLabel = groups.flatMap((group) => [
                ...(group.archive === domain ? [{ label: group.label }] : []),
                ...group.categories.filter((option) => option.domain === domain),
              ])[0]?.label;
              return <span key={domain} className="feed-chip feed-chip-on">{label || catalogLabel || domain}</span>;
            })}
          </div>
        </section>
      )}

      <div className="feed-picker-groups">
        {filtered.map((group) => {
          const hits = group.categories.filter((option) => selected.includes(option.domain)).length;
          return <button key={group.archive} type="button" className="feed-group-toggle"
            aria-haspopup="dialog" onClick={() => setActiveGroup(group.archive)}>
            <span className="feed-group-label">{group.label}</span>
            {(hits > 0 || selected.includes(group.archive)) && <span className="feed-group-hit">{selected.includes(group.archive) ? say({ zh: "整个大类", en: "Entire field" }, lang) : say({ zh: "已选 {count}", en: "{count} selected" }, lang, { count: hits })}</span>}
            <ChevronRight size={15} aria-hidden />
          </button>;
        })}
      </div>
      {dialogGroup && <div className="feed-domain-backdrop" onClick={() => setActiveGroup(null)}>
        <section className="feed-domain-dialog" role="dialog" aria-modal="true" aria-labelledby="feed-domain-dialog-title" onClick={(event) => event.stopPropagation()}>
          <header><h3 id="feed-domain-dialog-title">{dialogGroup.label} · {say({ zh: "二级学科", en: "Subfields" }, lang)}</h3><button type="button" autoFocus aria-label={say({ zh: "关闭", en: "Close" }, lang)} onClick={() => setActiveGroup(null)}><X size={20} /></button></header>
          <div className="feed-chip-row">
            <button type="button" aria-pressed={selected.includes(dialogGroup.archive)} className={selected.includes(dialogGroup.archive) ? "feed-chip feed-chip-on" : "feed-chip"} onClick={() => toggle(dialogGroup.archive)}>
              {selected.includes(dialogGroup.archive) && <Check size={13} aria-hidden />}{say({ zh: "整个{field}", en: "Entire {field}" }, lang, { field: dialogGroup.label })}
            </button>
            {dialogGroup.categories.map((option) => <button key={option.domain} type="button" aria-pressed={selected.includes(option.domain)} className={selected.includes(option.domain) ? "feed-chip feed-chip-on" : "feed-chip"} onClick={() => toggle(option.domain)}>
              {selected.includes(option.domain) && <Check size={13} aria-hidden />}{option.label}
            </button>)}
          </div>
          <footer><Button onClick={() => setActiveGroup(null)}>{say({ zh: "完成选择", en: "Done" }, lang)}</Button></footer>
        </section>
      </div>}

      <footer className="feed-picker-foot">
        <div className="feed-picker-foot-left">
          <span className="feed-picker-count">{fc("picker.count", lang, { n: selected.length })}</span>
          {selected.length > 0 && (
            <Button
              variant="ghost"
              title={fc("picker.clear_hint", lang)}
              onClick={() => { onChange?.(); setSelected([]); }}
            >
              {fc("picker.clear", lang)}
            </Button>
          )}
        </div>
        <div className="feed-picker-buttons">
          {showSkip && (
            // "什么都不订，只看全站" 也是一个明确的选择。存空列表照样算
            // onboarded —— 否则每次进来都会再弹一次问卷。
            <Button variant="ghost" onClick={() => { onChange?.(); setSelected([]); void save.mutateAsync([]).then(() => onDone?.()); }}>
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

"use client";

import { useState } from "react";
import { Link2 } from "lucide-react";
import { Button } from "@/shared/ui";
import { pushError, pushSuccess } from "@/stores/notification";
import { useLanguage } from "@/shared/i18n";
import { fc } from "../lib/feed-copy";
import { useShareLink } from "../hooks/useFeed";

/**
 * 转一条链接进来。
 *
 * ## 为什么这是"发布"的第一形态
 *
 * X / 公众号 / 知乎那些封闭生态没有可用的开放接口，硬爬既不稳也不体面。但
 * 科研信息确实有一大半先在那儿发生。把技术难题转成社区功能：用户看到好东西
 * 贴个链接，平台抓标题和摘要做成卡。
 *
 * 发原创贴很难，转一条链接加一句话人人肯干 —— 冷启动期的用户内容大概率
 * 全从这来。
 */
export function ShareLinkForm({ onShared }: { onShared?: () => void }) {
  const [url, setUrl] = useState("");
  const [comment, setComment] = useState("");
  const [visibility, setVisibility] = useState<"platform" | "organization">("organization");
  const share = useShareLink();
  const lang = useLanguage();

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!url.trim()) return;
    try {
      const item = await share.mutateAsync({ url: url.trim(), comment: comment.trim(), visibility });
      setUrl("");
      setComment("");
      pushSuccess(fc("share.done", lang, { title: item.title.slice(0, 40) }));
      onShared?.();
    } catch (caught) {
      // 后端的拒收原因是**说人话**写的（内网地址、打不开、没标题），
      // 原样给用户 —— 他要能分辨是链接的问题还是平台的问题。
      pushError(caught instanceof Error ? caught.message : fc("share.failed", lang));
    }
  };

  return (
    <form className="feed-share" onSubmit={submit}>
      <div className="feed-share-row">
        <Link2 size={15} aria-hidden />
        <input
          type="url"
          value={url}
          required
          placeholder={fc("share.url.placeholder", lang)}
          aria-label={fc("share.url.label", lang)}
          onChange={(event) => setUrl(event.target.value)}
        />
      </div>
      <div className="feed-share-row">
        <input
          type="text"
          value={comment}
          maxLength={2000}
          placeholder={fc("share.comment.placeholder", lang)}
          aria-label={fc("share.comment.label", lang)}
          onChange={(event) => setComment(event.target.value)}
        />
      </div>
      <div className="feed-share-foot">
        <label className="feed-share-visibility">
          <span>{fc("share.visibility", lang)}</span>
          <select
            value={visibility}
            aria-label={fc("share.visibility", lang)}
            onChange={(event) =>
              setVisibility(event.target.value === "platform" ? "platform" : "organization")
            }
          >
            <option value="organization">{fc("share.visibility.org", lang)}</option>
            <option value="platform">{fc("share.visibility.platform", lang)}</option>
          </select>
        </label>
        <Button type="submit" variant="secondary" size="sm" loading={share.isPending}>
          {fc("share.submit", lang)}
        </Button>
      </div>
    </form>
  );
}

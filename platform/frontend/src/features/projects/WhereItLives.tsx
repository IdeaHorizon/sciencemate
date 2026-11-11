"use client";

/**
 * 放在哪 —— 建项目时挑一个家。
 *
 * ## 为什么这一问在**项目**这一层
 *
 * 组织不是应用的模式，是项目的家。一台桌面可以同时握着好几个组织，而一个人手上
 * 通常既有组里的课题，也有只属于自己的那个。所以「属于谁」是每个项目各自的答案，
 * 不是这台机器的一个开关。
 *
 * 2026-09-22 之前它是开关：整台桌面要么本机、要么某台组织服务器，于是「我开一个
 * project 就想本地搞不属于任何组织」这件事没有路。
 *
 * ## 默认必须是「什么都不说」
 *
 * 一个只想在自己电脑上开个课题的人，不该先回答"你属于哪个组织"。所以本机是默认
 * 选中的那一项，而且**没有连接时这一行整个不出现** —— 个人版和一台还没连过组织
 * 的专业版桌面，看到的建项目对话框和从前逐字一样。
 */

import { Building2, Laptop, Plus, Server } from "lucide-react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { useHasCapability } from "@/features/capabilities";
import { useEffect, useState } from "react";
import { otherHomes, type OtherHome } from "./homes";
import { useT } from "@/shared/i18n";
import { cn } from "@/shared/ui";

export function WhereItLives({
  value, onChange, onAddOne,
}: {
  /** 空串 = 本机。 */
  value: string;
  onChange: (home: string) => void;
  onAddOne?: () => void;
}) {
  const t = useT();
  const canConnectOrganisations = useHasCapability("connections");
  const [held, setHeld] = useState<OtherHome[] | null>(null);
  const servers = useQuery({ queryKey: qk.remoteServers(), queryFn: () => api.listRemoteServers(), retry: false });

  useEffect(() => {
    let alive = true;
    const list = otherHomes.list;
    if (!list) { setHeld([]); return () => { alive = false; }; }
    void list()
      .then((got) => { if (alive) setHeld(got); })
      .catch(() => { if (alive) setHeld([]); });
    return () => { alive = false; };
  }, []);

  // 一条连接都没有 = 没有可挑的，这一行不画。问一个只有一个答案的问题，
  // 就是在教育用户。
  return (
    <div className="project-home">
      <span className="project-home-label">{t({ zh: "后端运行位置", en: "Backend location" })}</span>
      <div className="project-home-choices" role="radiogroup"
           aria-label={t({ zh: "后端运行位置", en: "Backend location" })}>
        <button type="button" role="radio" aria-checked={value === ""}
                className={cn("project-home-choice", value === "" && "is-chosen")}
                onClick={() => onChange("")}>
          <Laptop size={13} />{t({ zh: "本机", en: "This computer" })}
        </button>
        {servers.data?.items.map((server) => <button type="button" key={server.id} role="radio"
          disabled={server.enabled === false}
          title={server.enabled === false ? t({ zh: "请先在运行环境设置中连接这台服务器", en: "Connect this server in Runtime environments first" }) : server.host}
          aria-checked={value === server.id} className={cn("project-home-choice", value === server.id && "is-chosen")}
          onClick={() => onChange(server.id)}><Server size={13} />{server.name}</button>)}
        {(held ?? []).map((one) => (
          <button key={one.id} type="button" role="radio" aria-checked={value === one.id}
                  className={cn("project-home-choice", value === one.id && "is-chosen")}
                  disabled={one.needsSignIn}
                  title={one.needsSignIn
                    ? t({ zh: "需要重新登录，请前往「组织」页面",
                          en: "Sign in again on the Organisations page first" })
                    : one.url}
                  onClick={() => onChange(one.id)}>
            <Building2 size={13} />{one.name}
          </button>
        ))}
        {canConnectOrganisations && onAddOne && otherHomes.Adder && (
          <button type="button" className="project-home-choice project-home-add" onClick={onAddOne}>
            <Plus size={13} />{t({ zh: "添加组织", en: "Add an organisation" })}
          </button>
        )}
      </div>
      <Link className="project-runtime-manage" href="/settings/runtimes">{t({ zh: "添加或管理服务器", en: "Add or manage servers" })}</Link>
      {servers.error && <p role="alert">{servers.error.message}</p>}
      <small className="project-home-note">
        {value === ""
          ? t({ zh: "项目只保存在这台电脑上。",
                en: "Stored on this computer only." })
          : value.startsWith("remote:")
          ? t({ zh: "个人项目在服务器上运行，关闭桌面后仍可继续。", en: "Your personal project runs on the server and continues after closing the desktop." })
          : t({ zh: "项目保存在组织服务器上，组织成员默认可以看到。",
                en: "Stored on the organisation's server; members can see it by default." })}
      </small>
    </div>
  );
}

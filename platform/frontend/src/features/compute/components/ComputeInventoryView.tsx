"use client";

import { ChevronRight } from "lucide-react";
import type { ComputeInventory, ResourceHealth } from "@/lib/api";
import { Skeleton } from "@/shared/ui";
import { useComputeInventory } from "../hooks/useComputeInventory";
import { useT, type Phrase, useLanguage, say, type Language } from "@/shared/i18n";

// 纯函数收 lang，不自己够 hook —— 见 shared/i18n/useT.ts 的「纯函数不许用它」。
function statusLabel(status: ResourceHealth, lang: Language) {
  if (status === "online") return say({ zh: "可用", en: "Available" }, lang);
  if (status === "offline") return say({ zh: "不可用", en: "Unavailable" }, lang);
  return say({ zh: "状态未确认", en: "Status not confirmed" }, lang);
}

function HealthLabel({ status, compact = false }: { status: ResourceHealth; compact?: boolean }) {
  const lang = useLanguage();
  const label = statusLabel(status, lang);
  return (
    <span className={`compute-health health-${status} ${compact ? "is-compact" : ""}`} title={label} aria-label={label}>
      <i /> {!compact && label}
    </span>
  );
}

function bytes(value: number | null) {
  if (value === null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function value(value: number | null, suffix = "") {
  return value === null ? "—" : `${value.toLocaleString()}${suffix}`;
}

// 纯函数收 t，不自己够 hook —— 见 shared/i18n/useT.ts 的「纯函数不许用它」。
function activeWork(inventory: ComputeInventory, t: (phrase: Phrase) => string) {
  if (inventory.recent_jobs.supported) {
    return {
      count: inventory.recent_jobs.items.filter((job) => job.status === "queued" || job.status === "running").length,
      detail: t({ zh: "个作业排队或在跑", en: "queued or running jobs" }),
    };
  }
  if (inventory.schedulers.length > 0) {
    return {
      count: inventory.schedulers.reduce((total, scheduler) => total + scheduler.active_sessions, 0),
      detail: t({ zh: "个会话在跑", en: "active Sessions" }),
    };
  }
  return { count: null, detail: t({ zh: "没有上报", en: "not reported" }) };
}

function Inventory({ inventory, accessLabel }: { inventory: ComputeInventory; accessLabel: string }) {
  const t = useT();
  const lang = useLanguage();
  const capacity = inventory.capacity;
  const active = activeWork(inventory, t);
  return (
    <div className="compute-inventory">
      <div className="compute-status-line" role="status">
        <HealthLabel status={inventory.health.status} />
        <span>{inventory.nodes.length} compute {inventory.nodes.length === 1 ? "node" : "nodes"}</span>
        <span>{inventory.schedulers.length} {inventory.schedulers.length === 1 ? "scheduler" : "schedulers"}</span>
        <time dateTime={inventory.observed_at}>Updated {new Date(inventory.observed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>
      </div>

      <dl className="compute-capacity-summary" aria-label={t({ zh: "可用算力", en: "Available compute capacity" })}>
        <div><dt>CPU</dt><dd>{value(capacity.cpu_logical_cores, " cores")}</dd><small>{t({ zh: "逻辑核", en: "logical cores" })}</small></div>
        <div><dt>{t({ zh: "内存", en: "Memory" })}</dt><dd>{bytes(capacity.memory_available_bytes)}</dd><small>{capacity.memory_total_bytes === null ? "available" : `${bytes(capacity.memory_total_bytes)} total`}</small></div>
        <div><dt>{t({ zh: "存储", en: "Storage" })}</dt><dd>{bytes(capacity.storage_free_bytes)}</dd><small>{capacity.storage_total_bytes === null ? "free" : `${bytes(capacity.storage_total_bytes)} total`}</small></div>
        <div><dt>GPU</dt><dd>{value(capacity.gpu_count)}</dd><small>{capacity.gpu_memory_available_bytes === null ? (capacity.gpu_count === 1 ? "device" : "devices") : `${bytes(capacity.gpu_memory_available_bytes)} VRAM free`}</small></div>
        <div><dt>{t({ zh: "正在跑的", en: "Active work" })}</dt><dd>{active.count === null ? "—" : active.count.toLocaleString()}</dd><small>{active.detail}</small></div>
      </dl>

      <section className="compute-primary-section">
        <header><h2>{t({ zh: "计算节点", en: "Compute nodes" })}</h2><small>{inventory.nodes.length}</small></header>
        <div className="compute-node-list">
          {inventory.nodes.map((node) => (
            <div key={node.id} className="compute-node-row">
              <HealthLabel status={node.status} compact />
              <span className="compute-row-identity"><strong>{node.name}</strong><small>{node.operating_system} · {node.architecture}</small></span>
              <span><strong>{value(node.cpu.logical_cores)}</strong><small>{t({ zh: "CPU 核", en: "CPU cores" })}</small></span>
              <span><strong>{bytes(node.memory.available_bytes)}</strong><small>{t({ zh: "可用内存", en: "memory available" })}</small></span>
              <span><strong>{node.gpu.count === null ? "—" : node.gpu.count}</strong><small>GPU</small></span>
            </div>
          ))}
          {inventory.nodes.length === 0 && <p className="compute-empty-line">{t({ zh: "当前没有登记任何计算节点。", en: "No compute nodes are currently registered." })}</p>}
        </div>
      </section>

      <section className="compute-primary-section">
        <header><h2>{t({ zh: "调度器", en: "Schedulers" })}</h2><small>{inventory.schedulers.length}</small></header>
        <div className="compute-scheduler-list">
          {inventory.schedulers.map((scheduler) => (
            <div key={scheduler.id} className="compute-scheduler-row">
              <HealthLabel status={scheduler.status} compact />
              <span className="compute-row-identity"><strong>{t({ zh: "本机调度器", en: "Local scheduler" })}</strong><small>{t({ zh: "会话执行", en: "Session execution" })}</small></span>
              <span><strong>{scheduler.active_sessions}</strong><small>{t({ zh: "个会话在跑", en: "active Sessions" })}</small></span>
              {scheduler.supports_queue && <span><strong>{scheduler.queue_depth ?? "—"}</strong><small>{t({ zh: "排队中", en: "queued" })}</small></span>}
            </div>
          ))}
          {inventory.schedulers.length === 0 && <p className="compute-empty-line">{t({ zh: "当前没有登记任何调度器。", en: "No scheduler is currently registered." })}</p>}
        </div>
      </section>

      <details className="compute-technical">
        <summary><span><strong>{t({ zh: "技术细节", en: "Technical details" })}</strong><small>{t({ zh: "探针结果、标识与上报边界", en: "Probe results, identifiers, and reporting limits" })}</small></span><ChevronRight size={13} /></summary>
        <div className="compute-technical-body">
          <dl className="compute-technical-meta">
            <div><dt>{t({ zh: "范围", en: "Scope" })}</dt><dd>{t({ zh: "本机开发", en: "Local development" })}</dd></div>
            <div><dt>{t({ zh: "观测于", en: "Observed" })}</dt><dd><time dateTime={inventory.observed_at}>{new Date(inventory.observed_at).toLocaleString()}</time></dd></div>
            <div><dt>{t({ zh: "访问", en: "Access" })}</dt><dd>{accessLabel}</dd></div>
            <div><dt>{t({ zh: "近期作业清单", en: "Recent-job inventory" })}</dt><dd>{inventory.recent_jobs.supported ? t({ zh: "有上报", en: "Reported" }) : t({ zh: "这个后端不提供", en: "Not exposed by this backend" })}</dd></div>
          </dl>

          {inventory.health.checks.length > 0 && (
            <section><h3>{t({ zh: "健康探针", en: "Health probes" })}</h3><div className="compute-probe-list">{inventory.health.checks.map((check) => <div key={check.name}><HealthLabel status={check.status} compact /><code>{check.name}</code><span>{check.detail}</span></div>)}</div></section>
          )}

          {inventory.nodes.length > 0 && (
            <section><h3>{t({ zh: "节点诊断", en: "Node diagnostics" })}</h3>{inventory.nodes.map((node) => <dl className="compute-diagnostic-row" key={node.id}><div><dt>{t({ zh: "节点 ID", en: "Node ID" })}</dt><dd><code>{node.id}</code></dd></div><div><dt>{t({ zh: "组件状态", en: "Component status" })}</dt><dd>CPU {statusLabel(node.cpu.status, lang)} · memory {statusLabel(node.memory.status, lang)} · storage {statusLabel(node.storage.status, lang)} · GPU {statusLabel(node.gpu.status, lang)}</dd></div>{node.gpu.devices.map((device) => <div key={device.id}><dt>{t({ zh: "GPU 设备", en: "GPU device" })}</dt><dd>{device.name} · {bytes(device.memory_available_bytes)} free of {bytes(device.memory_total_bytes)} · {device.utilization_percent === null ? "utilization unknown" : `${device.utilization_percent}% utilized`}</dd></div>)}</dl>)}</section>
          )}

          {inventory.limitations.length > 0 && (
            <section><h3>{t({ zh: "上报边界", en: "Reporting limits" })}</h3><ul>{inventory.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul></section>
          )}
        </div>
      </details>
    </div>
  );
}

export function ComputeInventoryView({ accessLabel }: { accessLabel?: string }) {
  const t = useT();
  const label = accessLabel ?? t({ zh: "已登录的清单访问权限", en: "Authenticated inventory access" });
  const query = useComputeInventory();
  if (query.isLoading && !query.data) return <div className="compute-loading"><Skeleton height={48} rounded="sm" /><Skeleton height={110} rounded="sm" /><Skeleton height={150} rounded="sm" /></div>;
  if (query.isError || !query.data) return <div className="compute-error" role="alert"><strong>{t({ zh: "读不到算力清单", en: "Compute inventory unavailable" })}</strong><span>{query.error instanceof Error ? query.error.message : t({ zh: "应用服务端没有返回算力清单。", en: "The App Server did not return a compute inventory." })}</span></div>;
  return <Inventory inventory={query.data} accessLabel={label} />;
}

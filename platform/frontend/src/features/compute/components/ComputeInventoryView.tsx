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
//
// 「运行中」这一格就是会话执行的那个数：后端只报一个本机的进程内调度器
// （`compute_inventory.py`），它单独成节时只有一行「N 个会话运行中」，和这一格重复，
// 所以那一节并进了这里。
function activeWork(inventory: ComputeInventory, t: (phrase: Phrase) => string) {
  if (inventory.recent_jobs.supported) {
    return {
      count: inventory.recent_jobs.items.filter((job) => job.status === "queued" || job.status === "running").length,
      detail: t({ zh: "个作业排队中或运行中", en: "queued or running jobs" }),
    };
  }
  if (inventory.schedulers.length > 0) {
    return {
      count: inventory.schedulers.reduce((total, scheduler) => total + scheduler.active_sessions, 0),
      detail: t({ zh: "个会话运行中", en: "active Sessions" }),
    };
  }
  return { count: null, detail: t({ zh: "暂无数据", en: "no data" }) };
}

function Inventory({ inventory }: { inventory: ComputeInventory }) {
  const t = useT();
  const lang = useLanguage();
  const capacity = inventory.capacity;
  const active = activeWork(inventory, t);
  return (
    <div className="compute-inventory">
      <div className="compute-status-line" role="status">
        <HealthLabel status={inventory.health.status} />
        <span>{t({ zh: "{n} 个计算节点", en: inventory.nodes.length === 1 ? "{n} compute node" : "{n} compute nodes" }, { n: inventory.nodes.length })}</span>
        <time dateTime={inventory.observed_at}>{t({ zh: "{time} 更新", en: "Updated {time}" }, { time: new Date(inventory.observed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) })}</time>
      </div>

      <dl className="compute-capacity-summary" aria-label={t({ zh: "可用算力", en: "Available compute capacity" })}>
        <div><dt>CPU</dt><dd>{value(capacity.cpu_logical_cores, t({ zh: " 核", en: " cores" }))}</dd><small>{t({ zh: "逻辑核", en: "logical cores" })}</small></div>
        <div><dt>{t({ zh: "内存", en: "Memory" })}</dt><dd>{bytes(capacity.memory_available_bytes)}</dd><small>{capacity.memory_total_bytes === null ? t({ zh: "可用", en: "available" }) : t({ zh: "共 {total}", en: "{total} total" }, { total: bytes(capacity.memory_total_bytes) })}</small></div>
        <div><dt>{t({ zh: "存储", en: "Storage" })}</dt><dd>{bytes(capacity.storage_free_bytes)}</dd><small>{capacity.storage_total_bytes === null ? t({ zh: "空闲", en: "free" }) : t({ zh: "共 {total}", en: "{total} total" }, { total: bytes(capacity.storage_total_bytes) })}</small></div>
        <div><dt>GPU</dt><dd>{value(capacity.gpu_count)}</dd><small>{capacity.gpu_memory_available_bytes === null ? t(capacity.gpu_count === 1 ? { zh: "张卡", en: "device" } : { zh: "张卡", en: "devices" }) : t({ zh: "显存空闲 {free}", en: "{free} VRAM free" }, { free: bytes(capacity.gpu_memory_available_bytes) })}</small></div>
        <div><dt>{t({ zh: "运行中", en: "Running" })}</dt><dd>{active.count === null ? "—" : active.count.toLocaleString()}</dd><small>{active.detail}</small></div>
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
          {inventory.nodes.length === 0 && <p className="compute-empty-line">{t({ zh: "还没有计算节点。", en: "No compute nodes yet." })}</p>}
        </div>
      </section>

      <details className="compute-technical">
        <summary><span><strong>{t({ zh: "技术细节", en: "Technical details" })}</strong><small>{t({ zh: "健康检查、节点 ID 和已知限制", en: "Health checks, node IDs and known limitations" })}</small></span><ChevronRight size={13} /></summary>
        <div className="compute-technical-body">
          <dl className="compute-technical-meta">
            <div><dt>{t({ zh: "范围", en: "Scope" })}</dt><dd>{t({ zh: "本机", en: "This computer" })}</dd></div>
            <div><dt>{t({ zh: "更新时间", en: "Updated" })}</dt><dd><time dateTime={inventory.observed_at}>{new Date(inventory.observed_at).toLocaleString()}</time></dd></div>
            <div><dt>{t({ zh: "近期作业", en: "Recent jobs" })}</dt><dd>{inventory.recent_jobs.supported ? t({ zh: "支持", en: "Supported" }) : t({ zh: "不支持", en: "Not available" })}</dd></div>
          </dl>

          {inventory.health.checks.length > 0 && (
            <section><h3>{t({ zh: "健康检查", en: "Health checks" })}</h3><div className="compute-probe-list">{inventory.health.checks.map((check) => <div key={check.name}><HealthLabel status={check.status} compact /><code>{check.name}</code><span>{check.detail}</span></div>)}</div></section>
          )}

          {inventory.nodes.length > 0 && (
            <section><h3>{t({ zh: "节点诊断", en: "Node diagnostics" })}</h3>{inventory.nodes.map((node) => <dl className="compute-diagnostic-row" key={node.id}><div><dt>{t({ zh: "节点 ID", en: "Node ID" })}</dt><dd><code>{node.id}</code></dd></div><div><dt>{t({ zh: "组件状态", en: "Component status" })}</dt><dd>CPU {statusLabel(node.cpu.status, lang)} · {t({ zh: "内存", en: "memory" })} {statusLabel(node.memory.status, lang)} · {t({ zh: "存储", en: "storage" })} {statusLabel(node.storage.status, lang)} · GPU {statusLabel(node.gpu.status, lang)}</dd></div>{node.gpu.devices.map((device) => <div key={device.id}><dt>{t({ zh: "GPU 设备", en: "GPU device" })}</dt><dd>{device.name} · {t({ zh: "空闲 {free}，共 {total}", en: "{free} free of {total}" }, { free: bytes(device.memory_available_bytes), total: bytes(device.memory_total_bytes) })} · {device.utilization_percent === null ? t({ zh: "利用率未知", en: "utilization unknown" }) : t({ zh: "利用率 {percent}%", en: "{percent}% utilized" }, { percent: device.utilization_percent })}</dd></div>)}</dl>)}</section>
          )}

          {inventory.limitations.length > 0 && (
            <section><h3>{t({ zh: "已知限制", en: "Known limitations" })}</h3><ul>{inventory.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul></section>
          )}
        </div>
      </details>
    </div>
  );
}

export function ComputeInventoryView() {
  const t = useT();
  const query = useComputeInventory();
  if (query.isLoading && !query.data) return <div className="compute-loading"><Skeleton height={48} rounded="sm" /><Skeleton height={110} rounded="sm" /><Skeleton height={150} rounded="sm" /></div>;
  if (query.isError || !query.data) return <div className="compute-error" role="alert"><strong>{t({ zh: "无法加载算力信息", en: "Could not load compute information" })}</strong><span>{query.error instanceof Error ? query.error.message : t({ zh: "请稍后重试。", en: "Please try again later." })}</span></div>;
  return <Inventory inventory={query.data} />;
}

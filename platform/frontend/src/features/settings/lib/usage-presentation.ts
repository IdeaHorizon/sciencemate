import type { UsageDay } from "@/lib/api";

export type UsageSeriesDay = UsageDay & { recorded: boolean };

function isoDate(date: Date) {
  return date.toISOString().slice(0, 10);
}

export function buildUsageSeries(daily: UsageDay[], days: number, now = new Date()): UsageSeriesDay[] {
  const byDate = new Map(daily.map((day) => [day.date, day]));
  const today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  return Array.from({ length: days }, (_, index) => {
    const date = new Date(today);
    date.setUTCDate(today.getUTCDate() - (days - index - 1));
    const key = isoDate(date);
    const recorded = byDate.get(key);
    return recorded ? { ...recorded, recorded: true } : { date: key, total_tokens: 0, run_count: 0, recorded: false };
  });
}

export function usageIntensity(tokens: number, maximum: number) {
  if (tokens <= 0 || maximum <= 0) return 0;
  return Math.max(1, Math.min(4, Math.ceil((tokens / maximum) * 4)));
}

export function formatTokenCount(value: number) {
  return new Intl.NumberFormat(undefined, { notation: value >= 10_000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value);
}

export function formatKnownCost(value: number, currency: string | null) {
  if (!currency) return null;
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 2 }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}

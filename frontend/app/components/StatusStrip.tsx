"use client";

import type { ComponentStatus } from "@/lib/api";

/** Component status strip (§8.1). */

const LABELS: Record<string, string> = {
  data_feed: "Data Feed",
  binance_api: "Binance API",
  risk_engine: "Risk Engine",
  llm_advisor: "LLM Advisor",
  scheduler: "Scheduler",
  order_reconciliation: "Reconciliation",
};

// Order matters: §8.1 lists these five, with reconciliation appended.
const ORDER = [
  "data_feed",
  "binance_api",
  "risk_engine",
  "llm_advisor",
  "scheduler",
  "order_reconciliation",
];

function dotClass(status: string): string {
  switch (status) {
    case "online":
      return "bg-emerald-500";
    case "stale":
      return "bg-amber-500";
    case "error":
      return "bg-red-500";
    default:
      return "bg-zinc-500";
  }
}

function age(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

export default function StatusStrip({
  components,
}: {
  components: ComponentStatus[];
}) {
  const byName = new Map(components.map((c) => [c.component, c]));
  const ordered = ORDER.map((name) => byName.get(name)).filter(
    (c): c is ComponentStatus => Boolean(c),
  );

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      {ordered.map((component) => (
        <div
          key={component.component}
          className="rounded-lg border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900"
          title={component.detail ?? ""}
        >
          <div className="flex items-center gap-2">
            <span
              className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${dotClass(
                component.effective_status,
              )}`}
            />
            <span className="truncate text-sm font-medium">
              {LABELS[component.component] ?? component.component}
            </span>
          </div>
          <div className="mt-1 text-xs text-zinc-500">
            {component.effective_status} · {age(component.age_seconds)}
          </div>
        </div>
      ))}
    </div>
  );
}

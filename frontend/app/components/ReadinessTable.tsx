"use client";

import type { ReactNode } from "react";

import type { ComponentStatus, JobRun, StuckOrder } from "@/lib/api";

/**
 * System readiness table (§8.1).
 *
 * Distinct from the component-health strip above it: that strip is live
 * heartbeats, this table is "can I trust the setup" — config validity,
 * historical run outcomes, and stage/halt state. All of it is read from
 * `status` (a single /api/status fetch), so it renders correctly on mount
 * and after navigating back to this page, not only after a WebSocket event.
 */

type Badge = "NOT_YET" | "PENDING" | "OK" | "ERROR" | "ONLINE" | "OFFLINE";

const BADGE_STYLES: Record<Badge, string> = {
  NOT_YET: "bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400",
  PENDING: "bg-amber-100 text-amber-700 dark:bg-amber-500/10 dark:text-amber-400",
  OK: "bg-emerald-100 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400",
  ONLINE: "bg-emerald-100 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400",
  ERROR: "bg-red-100 text-red-700 dark:bg-red-500/10 dark:text-red-400",
  OFFLINE: "bg-red-100 text-red-700 dark:bg-red-500/10 dark:text-red-400",
};

function StatusBadge({ label }: { label: Badge }) {
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${BADGE_STYLES[label]}`}
    >
      {label}
    </span>
  );
}

function formatWhen(value: string | null | undefined): string {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "never";
  return date.toLocaleString();
}

function jobBadge(job: JobRun | null): Badge {
  if (!job) return "NOT_YET";
  if (job.status === "running") return "PENDING";
  if (job.status === "success") return "OK";
  return "ERROR";
}

function jobDetail(job: JobRun | null): string {
  if (!job) return "no run yet";
  if (job.status === "running") {
    const pct = job.progress !== null ? `${Math.round(job.progress * 100)}%` : "in progress";
    return `running · ${pct}`;
  }
  const when = formatWhen(job.finished_at ?? job.started_at);
  return job.status === "success" ? `succeeded · ${when}` : `failed · ${when}`;
}

function binanceBadge(component: ComponentStatus | undefined): Badge {
  if (!component) return "NOT_YET";
  switch (component.effective_status) {
    case "online":
      return "OK";
    case "stale":
      return "PENDING";
    default:
      return "ERROR";
  }
}

function Row({
  label,
  badge,
  detail,
  action,
}: {
  label: string;
  badge: Badge;
  detail: string;
  action?: ReactNode;
}) {
  return (
    <tr className="border-b border-zinc-100 last:border-0 dark:border-zinc-800">
      <td className="py-2 pr-3 text-sm font-medium">{label}</td>
      <td className="py-2 pr-3">
        <StatusBadge label={badge} />
      </td>
      <td className="py-2 pr-3 text-xs text-zinc-500">{detail}</td>
      <td className="py-2 text-right">{action}</td>
    </tr>
  );
}

export default function ReadinessTable({
  components,
  latestDownload,
  latestTraining,
  stage,
  halted,
  tradingEnabled,
  schedulerRunning,
  ordersNeedingAttention,
  onTestBinance,
  testingBinance,
  binanceTestResult,
}: {
  components: ComponentStatus[];
  latestDownload: JobRun | null;
  latestTraining: JobRun | null;
  stage: string;
  halted: boolean;
  tradingEnabled: boolean;
  schedulerRunning: boolean;
  ordersNeedingAttention: StuckOrder[];
  onTestBinance: () => void;
  testingBinance: boolean;
  binanceTestResult: string | null;
}) {
  const binance = components.find((c) => c.component === "binance_api");

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        System Readiness
      </h2>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full">
          <tbody>
            <Row
              label="Binance API key"
              badge={binanceBadge(binance)}
              detail={binanceTestResult ?? binance?.detail ?? "no heartbeat yet"}
              action={
                <button
                  onClick={onTestBinance}
                  disabled={testingBinance}
                  className="rounded border border-zinc-300 px-2 py-1 text-xs font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
                >
                  {testingBinance ? "Testing…" : "Test connection"}
                </button>
              }
            />
            <Row
              label="Most recent data download"
              badge={jobBadge(latestDownload)}
              detail={jobDetail(latestDownload)}
            />
            <Row
              label="Training pipeline last run"
              badge={jobBadge(latestTraining)}
              detail={jobDetail(latestTraining)}
            />
            <Row label="Stage" badge={halted ? "ERROR" : stage === "setup" ? "PENDING" : "OK"} detail={stage} />
            <Row
              label="Emergency halt"
              badge={halted ? "ERROR" : "OK"}
              detail={halted ? "halted — resume requires the stage PIN" : "not halted"}
            />
            <Row
              label="Trading enabled"
              badge={tradingEnabled ? "OK" : "PENDING"}
              detail={tradingEnabled ? "on" : "off"}
            />
            <Row
              label="Scheduler"
              badge={schedulerRunning ? "ONLINE" : "OFFLINE"}
              detail={schedulerRunning ? "running" : "not running"}
            />
            <Row
              label="Orders needing attention"
              badge={ordersNeedingAttention.length === 0 ? "OK" : "PENDING"}
              detail={
                ordersNeedingAttention.length === 0
                  ? "none"
                  : `${ordersNeedingAttention.length} unresolved`
              }
            />
          </tbody>
        </table>
      </div>
    </section>
  );
}

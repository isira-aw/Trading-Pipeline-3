"use client";

/** Live paper-trading stats (§5.2). */

import type { Performance } from "@/lib/api";

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className="text-xs text-zinc-500">{label}</div>
      <div className={`font-mono text-lg tabular-nums ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

export default function PerformancePanel({
  data,
  error,
}: {
  data: Performance | null;
  error: string | null;
}) {
  const pnl = data?.total_realized_pnl ?? 0;

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        Performance ({data?.stage ?? "—"})
      </h2>

      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

      {data && (
        <>
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Stat
              label="Win rate"
              value={
                data.win_rate === null
                  ? "no data"
                  : `${(data.win_rate * 100).toFixed(1)}%`
              }
            />
            <Stat
              label="Realized P&L"
              value={`${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}`}
              tone={pnl >= 0 ? "text-emerald-600" : "text-red-600"}
            />
            <Stat
              label="Max drawdown"
              value={`${data.max_drawdown_pct.toFixed(2)}%`}
            />
            <Stat label="Closed trades" value={String(data.closed_trades)} />
            <Stat label="Wins / losses" value={`${data.wins} / ${data.losses}`} />
            <Stat label="Open positions" value={String(data.open_lots)} />
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            Realized P&amp;L and win rate are net of fees.
          </p>
        </>
      )}
    </section>
  );
}

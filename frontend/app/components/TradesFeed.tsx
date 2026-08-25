"use client";

import type { Trade } from "@/lib/api";

/** Live-updating recent trades feed (§8.1). */

function badgeClass(value: string): string {
  switch (value) {
    case "filled":
    case "approved":
      return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400";
    case "partial":
    case "resized":
    case "submitted":
      return "bg-amber-500/15 text-amber-700 dark:text-amber-400";
    case "failed":
    case "rejected":
    case "cancelled":
      return "bg-red-500/15 text-red-700 dark:text-red-400";
    default:
      return "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400";
  }
}

function time(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toISOString().slice(11, 19) + "Z";
}

export default function TradesFeed({
  trades,
  error,
}: {
  trades: Trade[];
  error: string | null;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        Recent Trades
      </h2>

      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

      {!error && trades.length === 0 && (
        <p className="mt-3 text-sm text-zinc-500">
          No trades yet. Rejected proposals are recorded in the risk log rather
          than here.
        </p>
      )}

      {trades.length > 0 && (
        <div className="mt-3 max-h-80 overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-white dark:bg-zinc-900">
              <tr className="text-left text-xs uppercase text-zinc-500">
                <th className="pb-2 pr-3 font-medium">Time</th>
                <th className="pb-2 pr-3 font-medium">Symbol</th>
                <th className="pb-2 pr-3 font-medium">Side</th>
                <th className="pb-2 pr-3 text-right font-medium">Qty</th>
                <th className="pb-2 pr-3 text-right font-medium">Price</th>
                <th className="pb-2 pr-3 font-medium">Risk</th>
                <th className="pb-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((trade) => (
                <tr
                  key={trade.id}
                  className="border-t border-zinc-100 dark:border-zinc-800"
                >
                  <td className="py-2 pr-3 font-mono text-xs text-zinc-500">
                    {time(trade.created_at)}
                  </td>
                  <td className="py-2 pr-3 font-medium">
                    {trade.symbol}
                    {trade.needs_attention && (
                      <span
                        className="ml-1 text-red-500"
                        title="Could not be reconciled — needs attention"
                      >
                        ⚠
                      </span>
                    )}
                  </td>
                  <td
                    className={`py-2 pr-3 font-medium ${
                      trade.side === "buy" ? "text-emerald-600" : "text-red-600"
                    }`}
                  >
                    {trade.side}
                  </td>
                  <td className="py-2 pr-3 text-right font-mono tabular-nums">
                    {trade.quantity}
                  </td>
                  <td className="py-2 pr-3 text-right font-mono tabular-nums">
                    {trade.price === null ? "—" : trade.price.toFixed(2)}
                  </td>
                  <td className="py-2 pr-3">
                    <span
                      className={`rounded px-1.5 py-0.5 text-xs ${badgeClass(
                        trade.risk_decision,
                      )}`}
                    >
                      {trade.risk_decision}
                    </span>
                  </td>
                  <td className="py-2">
                    <span
                      className={`rounded px-1.5 py-0.5 text-xs ${badgeClass(
                        trade.status,
                      )}`}
                    >
                      {trade.status}
                    </span>
                    {trade.exit_reason && (
                      <span
                        className="ml-1 rounded bg-zinc-500/15 px-1.5 py-0.5 text-xs text-zinc-600 dark:text-zinc-400"
                        title="Why this position was closed"
                      >
                        {trade.exit_reason.replace(/_/g, " ")}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

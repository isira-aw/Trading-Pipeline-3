"use client";

import type { Position } from "@/lib/api";

/** Open positions table (§8.1). */

function money(value: number | null, digits = 2): string {
  if (value === null || Number.isNaN(value)) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export default function PositionsTable({
  positions,
  error,
}: {
  positions: Position[];
  error: string | null;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        Open Positions
      </h2>

      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

      {!error && positions.length === 0 && (
        <p className="mt-3 text-sm text-zinc-500">No open positions.</p>
      )}

      {positions.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase text-zinc-500">
                <th className="pb-2 pr-3 font-medium">Symbol</th>
                <th className="pb-2 pr-3 text-right font-medium">Qty</th>
                <th className="pb-2 pr-3 text-right font-medium">Entry</th>
                <th className="pb-2 pr-3 text-right font-medium">Current</th>
                <th className="pb-2 pr-3 text-right font-medium">Stop</th>
                <th className="pb-2 text-right font-medium">Unrealized</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((position, index) => (
                <tr
                  key={`${position.symbol}-${position.opened_at}-${index}`}
                  className="border-t border-zinc-100 dark:border-zinc-800"
                >
                  <td className="py-2 pr-3 font-medium">{position.symbol}</td>
                  <td className="py-2 pr-3 text-right font-mono tabular-nums">
                    {money(position.quantity, 8)}
                  </td>
                  <td className="py-2 pr-3 text-right font-mono tabular-nums">
                    {money(position.entry_price)}
                  </td>
                  <td className="py-2 pr-3 text-right font-mono tabular-nums">
                    {position.current_price === null ? (
                      <span className="text-amber-600" title="Price unavailable">
                        —
                      </span>
                    ) : (
                      money(position.current_price)
                    )}
                  </td>
                  <td
                    className="py-2 pr-3 text-right font-mono tabular-nums text-zinc-500"
                    title={
                      position.stop_distance_pct !== null
                        ? `${position.stop_distance_pct.toFixed(2)}% below entry (ATR-based, fixed at open)`
                        : "No stop recorded"
                    }
                  >
                    {position.stop_price === null
                      ? "—"
                      : money(position.stop_price)}
                  </td>
                  <td
                    className={`py-2 text-right font-mono tabular-nums ${
                      position.unrealized_pnl === null
                        ? "text-zinc-500"
                        : position.unrealized_pnl >= 0
                          ? "text-emerald-600"
                          : "text-red-600"
                    }`}
                  >
                    {position.unrealized_pnl === null
                      ? "—"
                      : `${position.unrealized_pnl >= 0 ? "+" : ""}${money(
                          position.unrealized_pnl,
                        )}`}
                    {position.unrealized_pnl_pct !== null && (
                      <span className="ml-1 text-xs">
                        ({position.unrealized_pnl_pct >= 0 ? "+" : ""}
                        {position.unrealized_pnl_pct.toFixed(2)}%)
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

"use client";

import type { Wallet } from "@/lib/api";

/** Wallet panel with value sparkline (§8.1). */

function Sparkline({ points }: { points: number[] }) {
  if (points.length < 2) {
    return (
      <div className="flex h-12 items-center text-xs text-zinc-500">
        Not enough history yet
      </div>
    );
  }

  const min = Math.min(...points);
  const max = Math.max(...points);
  // A flat line would otherwise divide by zero and vanish.
  const range = max - min || 1;
  const width = 240;
  const height = 48;

  const path = points
    .map((value, index) => {
      const x = (index / (points.length - 1)) * width;
      const y = height - ((value - min) / range) * height;
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const rising = points[points.length - 1] >= points[0];

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-12 w-full"
      preserveAspectRatio="none"
      role="img"
      aria-label="Wallet value over time"
    >
      <path
        d={path}
        fill="none"
        strokeWidth={1.5}
        className={rising ? "stroke-emerald-500" : "stroke-red-500"}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

export default function WalletPanel({
  wallet,
  error,
}: {
  wallet: Wallet | null;
  error: string | null;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Wallet
        </h2>
        {wallet && !wallet.live && (
          <span
            className="text-xs text-amber-600 dark:text-amber-500"
            title={wallet.live_error ?? ""}
          >
            last known — exchange unreachable
          </span>
        )}
      </div>

      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

      {wallet && (
        <>
          <div className="mt-2 font-mono text-3xl tabular-nums">
            {wallet.total_value_usdt.toLocaleString(undefined, {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            })}
            <span className="ml-1 text-base text-zinc-500">USDT</span>
          </div>

          <Sparkline points={wallet.history.map((h) => h.total_value_usdt)} />

          <div className="mt-3 space-y-1">
            {Object.entries(wallet.balances).length === 0 && (
              <p className="text-sm text-zinc-500">No balances.</p>
            )}
            {Object.entries(wallet.balances).map(([asset, amount]) => (
              <div
                key={asset}
                className="flex justify-between font-mono text-sm tabular-nums"
              >
                <span className="text-zinc-500">{asset}</span>
                <span>{amount}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

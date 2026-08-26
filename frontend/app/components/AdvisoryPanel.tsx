"use client";

/**
 * Latest LLM advisories (§5.1, §8.1).
 *
 * This is for the operator's judgement, not the system's — nothing here
 * feeds a trading decision. The panel says so explicitly, so a reader never
 * mistakes a bearish note for something the bot acted on.
 */

import type { Advisory, AdvisoriesResponse } from "@/lib/api";

function uncertaintyClass(level: string | null): string {
  switch (level) {
    case "low":
      return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400";
    case "normal":
      return "bg-blue-500/15 text-blue-700 dark:text-blue-400";
    case "elevated":
      return "bg-amber-500/15 text-amber-700 dark:text-amber-400";
    case "high":
      return "bg-red-500/15 text-red-700 dark:text-red-400";
    default:
      return "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400";
  }
}

function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toISOString().slice(0, 16).replace("T", " ") + "Z";
}

function AdvisoryCard({ advisory }: { advisory: Advisory }) {
  const failed = advisory.status !== "ok";

  return (
    <li className="rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-zinc-500">{when(advisory.created_at)}</span>
        <span className="text-xs text-zinc-400">·</span>
        <span className="text-xs text-zinc-500">{advisory.provider}</span>
        {failed ? (
          <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-xs text-red-700 dark:text-red-400">
            {advisory.status ?? "unknown"}
          </span>
        ) : (
          <span
            className={`rounded px-1.5 py-0.5 text-xs ${uncertaintyClass(
              advisory.uncertainty,
            )}`}
            title={advisory.uncertainty_reason ?? ""}
          >
            uncertainty: {advisory.uncertainty ?? "—"}
          </span>
        )}
      </div>

      {failed ? (
        <p className="mt-2 text-sm text-zinc-500">
          {advisory.error ?? "No response recorded."}
        </p>
      ) : (
        <>
          {advisory.macro_summary && (
            <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
              {advisory.macro_summary}
            </p>
          )}

          {advisory.symbols && Object.keys(advisory.symbols).length > 0 && (
            <ul className="mt-2 space-y-0.5">
              {Object.entries(advisory.symbols).map(([symbol, view]) => (
                <li key={symbol} className="text-xs">
                  <span className="font-medium">{symbol}</span>{" "}
                  <span className="text-zinc-500">
                    {view?.view ?? "—"}
                    {view?.comment ? ` — ${view.comment}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}

          {advisory.key_risks && advisory.key_risks.length > 0 && (
            <p className="mt-2 text-xs text-zinc-500">
              Risks: {advisory.key_risks.join(" · ")}
            </p>
          )}
        </>
      )}
    </li>
  );
}

export default function AdvisoryPanel({
  data,
  error,
}: {
  data: AdvisoriesResponse | null;
  error: string | null;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          LLM Advisor
        </h2>
        {data && (
          <span className="text-xs text-zinc-500">
            {data.calls_today}/{data.cap} calls today
          </span>
        )}
      </div>

      <p className="mt-1 text-xs text-zinc-500">
        Context for your judgement only — advisories never place, size or
        block a trade.
      </p>

      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

      {data && data.advisories.length === 0 && !error && (
        <p className="mt-3 text-sm text-zinc-500">
          No advisories yet. The scheduled job runs at the configured hours;
          you can also trigger one from the button below.
        </p>
      )}

      {data && data.advisories.length > 0 && (
        <ul className="mt-3 space-y-2">
          {data.advisories.map((advisory) => (
            <AdvisoryCard key={advisory.id} advisory={advisory} />
          ))}
        </ul>
      )}
    </section>
  );
}

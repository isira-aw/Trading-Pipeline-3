"use client";

/** Read-only promotion gate checklist (§8.1 stage progress widget). */

import type { GateStatus } from "@/lib/api";

export default function GateProgress({
  gate,
  error,
}: {
  gate: GateStatus | null;
  error: string | null;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Promotion gate
        </h2>
        {gate && (
          <span
            className={`text-xs font-medium ${
              gate.passed ? "text-emerald-600" : "text-zinc-500"
            }`}
          >
            {gate.passed ? "all criteria met" : "not met"}
          </span>
        )}
      </div>

      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

      {gate && (
        <>
          <ul className="mt-3 space-y-2">
            {gate.criteria.map((c) => {
              const ratio =
                c.required > 0 && c.current !== null
                  ? c.comparison === "<="
                    ? Math.max(0, 1 - c.current / c.required)
                    : Math.min(c.current / c.required, 1)
                  : 0;
              return (
                <li key={c.name} title={c.detail}>
                  <div className="flex justify-between text-xs">
                    <span className={c.passed ? "text-emerald-600" : ""}>
                      {c.passed ? "✓" : "○"} {c.label}
                    </span>
                    <span className="font-mono tabular-nums text-zinc-500">
                      {c.current === null ? "—" : c.current} {c.comparison}{" "}
                      {c.required}
                    </span>
                  </div>
                  <div className="mt-0.5 h-1.5 w-full rounded bg-zinc-200 dark:bg-zinc-800">
                    <div
                      className={`h-1.5 rounded ${
                        c.passed ? "bg-emerald-500" : "bg-blue-500"
                      }`}
                      style={{ width: `${ratio * 100}%` }}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
          <p className="mt-2 text-xs text-zinc-500">
            Switch to live from Settings once every criterion passes.
          </p>
        </>
      )}
    </section>
  );
}

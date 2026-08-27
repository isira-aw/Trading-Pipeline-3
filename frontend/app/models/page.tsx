"use client";

/**
 * Models page (§8.2).
 *
 * Shows the full §5.1 scoring breakdown per model rather than a single
 * headline number. That matters: a model can post 61% accuracy and still
 * have no edge at all once precision is compared against the base rate, so
 * accuracy alone would actively mislead the promote decision this page
 * exists to support.
 *
 * Promote/Archive call the registry endpoints directly. The refusal rules
 * (disqualified model, candidate no better than incumbent, missing model
 * file) live server-side in model_registry, so this page surfaces their
 * reasons and never re-implements or shortcuts them.
 */

import { useCallback, useEffect, useState } from "react";

import NavBar from "@/app/components/NavBar";
import {
  archiveModel,
  getModels,
  promoteBest,
  promoteModel,
  trainSymbol,
  type ModelRow,
  type ModelsResponse,
} from "@/lib/api";

function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function num(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(digits);
}

function statusClass(status: string): string {
  switch (status) {
    case "active":
      return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400";
    case "candidate":
      return "bg-blue-500/15 text-blue-700 dark:text-blue-400";
    default:
      return "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400";
  }
}

/** One weighted component of the score, as a labelled bar. */
function ScoreBar({
  label,
  value,
  weight,
}: {
  label: string;
  value: number | undefined;
  weight: number | undefined;
}) {
  const filled = Math.max(0, Math.min(value ?? 0, 1));
  return (
    <div>
      <div className="flex justify-between text-xs text-zinc-500">
        <span>
          {label}
          {weight !== undefined && (
            <span className="ml-1 text-zinc-400">×{weight}</span>
          )}
        </span>
        <span className="font-mono tabular-nums">{num(value)}</span>
      </div>
      <div className="mt-0.5 h-1.5 w-full rounded bg-zinc-200 dark:bg-zinc-800">
        <div
          className="h-1.5 rounded bg-blue-500"
          style={{ width: `${filled * 100}%` }}
        />
      </div>
    </div>
  );
}

export default function ModelsPage() {
  const [data, setData] = useState<ModelsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [symbolFilter, setSymbolFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const refresh = useCallback(async () => {
    const result = await getModels();
    if (!result) return;
    if (result.ok) {
      setData(result.data);
      setError(null);
    } else {
      setError(result.error);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const run = async (
    action: () => Promise<{ ok: boolean; error?: string; data?: unknown }>,
    label: string,
  ) => {
    setBusy(true);
    setNotice(`${label}…`);
    const result = await action();
    if (!result.ok) {
      // A refusal is the interesting case — show exactly why.
      setNotice(`${label} refused: ${result.error}`);
    } else {
      const payload = result.data as { promoted?: boolean; reason?: string };
      setNotice(
        payload?.promoted === false
          ? `${label}: ${payload.reason}`
          : `${label} done.`,
      );
      await refresh();
    }
    setBusy(false);
  };

  const models = data?.models ?? [];
  const symbols = Array.from(new Set(models.map((m) => m.symbol))).sort();

  const visible = models.filter(
    (m) =>
      (symbolFilter === "all" || m.symbol === symbolFilter) &&
      (statusFilter === "all" || m.status === statusFilter),
  );

  return (
    <div className="min-h-screen bg-zinc-50 p-4 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100 sm:p-6">
      <div className="mx-auto max-w-7xl space-y-4">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-4">
            <h1 className="text-xl font-semibold">Models</h1>
            <NavBar />
          </div>
          {notice && <span className="text-xs text-zinc-500">{notice}</span>}
        </header>

        {error && (
          <div className="rounded-lg border border-red-500 bg-red-500/10 p-4 text-sm">
            <span className="font-semibold text-red-600 dark:text-red-400">
              Could not load models:
            </span>{" "}
            <span className="text-zinc-600 dark:text-zinc-400">{error}</span>
          </div>
        )}

        {/* Controls */}
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={symbolFilter}
            onChange={(e) => setSymbolFilter(e.target.value)}
            className="rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
          >
            <option value="all">All symbols</option>
            {symbols.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
          >
            <option value="all">All statuses</option>
            <option value="active">Active</option>
            <option value="candidate">Candidate</option>
            <option value="archived">Archived</option>
          </select>

          {symbolFilter !== "all" && (
            <>
              <button
                onClick={() => void run(() => trainSymbol(symbolFilter), "Train")}
                disabled={busy}
                className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
              >
                Train {symbolFilter}
              </button>
              <button
                onClick={() =>
                  void run(() => promoteBest(symbolFilter), "Promote best")
                }
                disabled={busy}
                className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
              >
                Promote best candidate
              </button>
            </>
          )}
        </div>

        {data && (
          <p className="text-xs text-zinc-500">
            Score = weighted precision lift (×{data.scoring_weights.precision_lift}
            ), discrimination (×{data.scoring_weights.discrimination}) and
            realized win rate (×{data.scoring_weights.realized_win_rate}, used
            only after {data.min_trades_for_realized_score} closed trades; the
            weights renormalise until then).
          </p>
        )}

        {visible.length === 0 && !error && (
          <p className="text-sm text-zinc-500">
            No models yet. Train one from the Dashboard or with the button
            above.
          </p>
        )}

        <div className="space-y-3">
          {visible.map((model) => (
            <ModelCard
              key={model.id}
              model={model}
              weights={data?.scoring_weights ?? {}}
              expanded={expanded === model.id}
              onToggle={() =>
                setExpanded(expanded === model.id ? null : model.id)
              }
              busy={busy}
              onPromote={(force) =>
                void run(
                  () => promoteModel(model.id, force),
                  force ? "Force promote" : "Promote",
                )
              }
              onArchive={() =>
                void run(() => archiveModel(model.id), "Archive")
              }
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function FeatureImportance({
  importance,
}: {
  importance: Record<string, number> | undefined;
}) {
  const entries = Object.entries(importance ?? {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 12);

  if (entries.length === 0) {
    return <p className="mt-2 text-xs text-zinc-500">No importance recorded.</p>;
  }

  const max = entries[0][1] || 1;

  return (
    <ul className="mt-2 space-y-1">
      {entries.map(([name, value]) => (
        <li key={name} className="flex items-center gap-2">
          <span className="w-36 shrink-0 truncate text-xs text-zinc-500">
            {name}
          </span>
          <span className="h-2 flex-1 rounded bg-zinc-200 dark:bg-zinc-800">
            <span
              className="block h-2 rounded bg-blue-500"
              style={{ width: `${(value / max) * 100}%` }}
            />
          </span>
          <span className="w-12 shrink-0 text-right font-mono text-xs tabular-nums">
            {value.toFixed(3)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function ModelCard({
  model,
  weights,
  expanded,
  onToggle,
  busy,
  onPromote,
  onArchive,
}: {
  model: ModelRow;
  weights: Record<string, number>;
  expanded: boolean;
  onToggle: () => void;
  busy: boolean;
  onPromote: (force: boolean) => void;
  onArchive: () => void;
}) {
  const importance = Object.entries(model.metrics ?? {});
  void importance;

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="font-semibold">{model.symbol}</h2>
            <span
              className={`rounded px-1.5 py-0.5 text-xs ${statusClass(
                model.status,
              )}`}
            >
              {model.status}
            </span>
            {model.disqualified && (
              <span
                className="rounded bg-red-500/15 px-1.5 py-0.5 text-xs text-red-700 dark:text-red-400"
                title={model.disqualified_reason ?? ""}
              >
                disqualified
              </span>
            )}
            {model.file_missing && (
              <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-xs text-red-700 dark:text-red-400">
                file missing
              </span>
            )}
          </div>
          <p className="mt-0.5 font-mono text-xs text-zinc-500">
            {model.id.slice(0, 8)} · {model.model_type} ·{" "}
            {new Date(model.trained_at).toISOString().slice(0, 16)}Z ·{" "}
            {model.file_size_bytes
              ? `${(model.file_size_bytes / 1024).toFixed(0)} KB`
              : "—"}
          </p>
        </div>

        <div className="flex items-center gap-3">
          <div className="text-right">
            <div className="font-mono text-2xl tabular-nums">
              {num(model.score, 4)}
            </div>
            <div className="text-xs text-zinc-500">score</div>
          </div>
          <div className="flex flex-col gap-1">
            <button
              onClick={() => onPromote(false)}
              disabled={busy || model.status === "active" || model.file_missing}
              className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-40"
            >
              Promote
            </button>
            <button
              onClick={() => onArchive()}
              disabled={busy || model.status === "archived"}
              className="rounded-md border border-zinc-300 px-3 py-1.5 text-xs font-medium hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:hover:bg-zinc-800"
            >
              Archive
            </button>
          </div>
        </div>
      </div>

      {/* The scoring breakdown — the point of this page. */}
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <ScoreBar
          label="Precision lift"
          value={model.score_breakdown?.precision_lift}
          weight={weights.precision_lift}
        />
        <ScoreBar
          label="Discrimination"
          value={model.score_breakdown?.discrimination}
          weight={weights.discrimination}
        />
        <ScoreBar
          label={`Realized win rate${model.used_realized_stats ? "" : " (not yet used)"}`}
          value={model.score_breakdown?.realized_win_rate}
          weight={weights.realized_win_rate}
        />
      </div>

      {model.disqualified && model.disqualified_reason && (
        <p className="mt-3 text-xs text-red-600 dark:text-red-400">
          {model.disqualified_reason} Promote is still available as the
          documented manual override.
        </p>
      )}

      <button
        onClick={onToggle}
        className="mt-3 text-xs text-blue-600 hover:underline dark:text-blue-400"
      >
        {expanded ? "Hide details" : "View details"}
      </button>

      {expanded && (
        <div className="mt-3 grid gap-4 border-t border-zinc-100 pt-3 dark:border-zinc-800 sm:grid-cols-2">
          <div>
            <h3 className="text-xs font-semibold uppercase text-zinc-500">
              Holdout metrics
            </h3>
            <dl className="mt-2 space-y-1 text-sm">
              {[
                ["Accuracy", pct(model.metrics.accuracy)],
                ["Precision (up)", pct(model.metrics.precision)],
                ["Recall", pct(model.metrics.recall)],
                ["ROC AUC", num(model.metrics.roc_auc)],
                ["Base rate", pct(model.metrics.positive_rate)],
                ["Predicted positive", pct(model.metrics.predicted_positive_rate)],
                ["Train / holdout rows",
                  `${model.metrics.train_rows ?? "—"} / ${model.metrics.holdout_rows ?? "—"}`],
              ].map(([label, value]) => (
                <div key={label} className="flex justify-between">
                  <dt className="text-zinc-500">{label}</dt>
                  <dd className="font-mono tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          </div>

          <div>
            <h3 className="text-xs font-semibold uppercase text-zinc-500">
              Realized (paper trading)
            </h3>
            <dl className="mt-2 space-y-1 text-sm">
              {[
                ["Closed positions", String(model.realized.closed_trades)],
                ["Win rate",
                  model.realized.win_rate === null
                    ? "no data yet"
                    : pct(model.realized.win_rate)],
                ["Realized P&L", model.realized.total_realized_pnl.toFixed(2)],
                ["Max drawdown", `${model.realized.max_drawdown_pct.toFixed(2)}%`],
              ].map(([label, value]) => (
                <div key={label} className="flex justify-between">
                  <dt className="text-zinc-500">{label}</dt>
                  <dd className="font-mono tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
            {model.notes && (
              <p className="mt-2 text-xs text-zinc-500">{model.notes}</p>
            )}
          </div>

          <div className="sm:col-span-2">
            <h3 className="text-xs font-semibold uppercase text-zinc-500">
              Feature importance
            </h3>
            <FeatureImportance importance={model.feature_importance} />
          </div>
        </div>
      )}
    </section>
  );
}

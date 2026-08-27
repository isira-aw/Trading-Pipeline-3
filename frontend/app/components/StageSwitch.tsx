"use client";

/**
 * Stage switch with live gate status (§5.4, §8.3).
 *
 * Replaces the disabled placeholder from step 10. The disabled state here
 * is a convenience only — every rule it reflects is enforced server-side by
 * POST /api/stage/switch, which re-checks the PIN and the gate. Anything
 * that can reach the API bypasses this component entirely.
 */

import { useCallback, useEffect, useState } from "react";

import { getGate, switchStage, type GateStatus } from "@/lib/api";

function value(criterion: { current: number | null; name: string }): string {
  if (criterion.current === null) return "no data";
  if (criterion.name === "min_win_rate") {
    return `${(criterion.current * 100).toFixed(1)}%`;
  }
  if (criterion.name === "max_drawdown_pct") {
    return `${criterion.current.toFixed(2)}%`;
  }
  if (criterion.name === "min_paper_trading_days") {
    return `${criterion.current.toFixed(1)} days`;
  }
  return String(criterion.current);
}

function required(criterion: { required: number; name: string }): string {
  if (criterion.name === "min_win_rate") {
    return `${(criterion.required * 100).toFixed(0)}%`;
  }
  return String(criterion.required);
}

export default function StageSwitch({
  onChanged,
}: {
  onChanged?: () => void;
}) {
  const [gate, setGate] = useState<GateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [target, setTarget] = useState("paper");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const result = await getGate();
    if (!result) return;
    if (result.ok) {
      setGate(result.data);
      setError(null);
    } else {
      setError(result.error);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const submit = async () => {
    setBusy(true);
    setNotice(`Switching to ${target}…`);
    const result = await switchStage(target, pin);
    setBusy(false);

    if (!result.ok) {
      setNotice(`Refused: ${result.error}`);
      return;
    }
    setPin("");
    setNotice(
      `Switched to ${target}. Trading is stopped — start it from the dashboard.`,
    );
    await refresh();
    onChanged?.();
  };

  const goingLive = target === "live";
  const blocked = goingLive && gate ? !gate.passed : false;

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Stage switch
        </h2>
        {gate && (
          <span className="text-xs text-zinc-500">
            current stage: <span className="font-medium">{gate.current_stage}</span>
          </span>
        )}
      </div>

      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

      {/* §7/§10: reaching live on the factory PIN must be unmissable. */}
      {gate?.pin_is_default && (
        <div className="mt-3 rounded-md border border-amber-500 bg-amber-500/10 p-3">
          <p className="text-sm font-semibold text-amber-700 dark:text-amber-400">
            Stage PIN is still the factory default
          </p>
          <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
            {gate.pin_warning?.message ??
              "It is the only confirmation step in front of real money."}
          </p>
        </div>
      )}

      {/* Gate checklist */}
      {gate && (
        <div className="mt-3">
          <div className="flex items-center gap-2">
            <span
              className={`inline-block h-2.5 w-2.5 rounded-full ${
                gate.passed ? "bg-emerald-500" : "bg-red-500"
              }`}
            />
            <span className="text-sm font-medium">
              Promotion gate: {gate.passed ? "passed" : "not met"}
            </span>
          </div>

          <ul className="mt-2 space-y-1">
            {gate.criteria.map((criterion) => (
              <li
                key={criterion.name}
                className="flex flex-wrap items-baseline gap-2 text-sm"
                title={criterion.detail}
              >
                <span className={criterion.passed ? "text-emerald-600" : "text-red-600"}>
                  {criterion.passed ? "✓" : "✗"}
                </span>
                <span className="min-w-[170px]">{criterion.label}</span>
                <span className="font-mono tabular-nums text-zinc-600 dark:text-zinc-400">
                  {value(criterion)}
                </span>
                <span className="text-xs text-zinc-500">
                  ({criterion.comparison} {required(criterion)})
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Switch form */}
      <div className="mt-4 flex flex-wrap items-end gap-2">
        <label className="text-sm">
          Target stage
          <select
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            className="mt-1 block rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-950"
          >
            <option value="setup">setup</option>
            <option value="paper">paper</option>
            <option value="live">live</option>
          </select>
        </label>
        <label className="text-sm">
          Stage PIN
          <input
            type="password"
            value={pin}
            onChange={(e) => setPin(e.target.value)}
            autoComplete="off"
            className="mt-1 block rounded-md border border-zinc-300 bg-white px-2 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-950"
          />
        </label>
        <button
          onClick={() => void submit()}
          disabled={busy || !pin || blocked}
          className={`rounded-md px-4 py-2 text-sm font-semibold text-white disabled:opacity-40 ${
            goingLive
              ? "bg-red-600 hover:bg-red-700"
              : "bg-blue-600 hover:bg-blue-700"
          }`}
        >
          {goingLive ? "Switch to LIVE" : `Switch to ${target}`}
        </button>
      </div>

      {blocked && (
        <p className="mt-2 text-xs text-red-600 dark:text-red-400">
          Live is unavailable until every criterion above passes. The server
          enforces this independently — {gate?.summary}
        </p>
      )}

      {goingLive && !blocked && (
        <p className="mt-2 text-xs text-amber-600 dark:text-amber-500">
          This moves the system to real money.
        </p>
      )}

      {notice && <p className="mt-2 text-xs text-zinc-500">{notice}</p>}
    </section>
  );
}

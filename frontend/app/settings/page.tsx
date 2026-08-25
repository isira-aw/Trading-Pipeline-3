"use client";

/**
 * Settings page (§8.3).
 *
 * Every value here writes to the `config` table and is read back by jobs on
 * their next run, so changes take effect without a restart. Schedule
 * intervals additionally rebuild the scheduler's triggers server-side.
 *
 * The stage switch is now the real control (StageSwitch), showing live gate
 * status per criterion and taking the PIN. Its disabled state is UX only —
 * POST /api/stage/switch re-checks both the PIN and the gate server-side.
 *
 * `promotion_gate` is deliberately absent from the editors below: it is
 * PIN-gated via PUT /api/stage/gate, because loosening a threshold is the
 * same decision as switching stages reached by another route.
 */

import { useCallback, useEffect, useState } from "react";

import NavBar from "@/app/components/NavBar";
import StageSwitch from "@/app/components/StageSwitch";
import {
  changePin,
  getConfig,
  updateConfig,
  type ConfigResponse,
} from "@/lib/api";

interface FieldSpec {
  key: string;
  label: string;
  help?: string;
  type: "number" | "text" | "boolean" | "list";
}

const GROUPS: { title: string; note?: string; fields: FieldSpec[] }[] = [
  {
    title: "Universe & data",
    fields: [
      { key: "symbols", label: "Traded symbols", type: "list",
        help: "Comma-separated, e.g. BTCUSDT, ETHUSDT" },
      { key: "interval", label: "Candle interval", type: "text" },
      { key: "history_years", label: "History to download (years)", type: "number" },
    ],
  },
  {
    title: "Model & training",
    fields: [
      { key: "retrain_interval_hours", label: "Retrain every (hours)", type: "number" },
      { key: "target_move_pct", label: "Target move (%)", type: "number",
        help: "The rise the model is trained to predict." },
      { key: "target_horizon_candles", label: "Target horizon (candles)", type: "number" },
    ],
  },
  {
    title: "Exit stops (ATR)",
    note:
      "Volatility-scaled stop, fixed at position open. Not part of the risk " +
      "engine's entry checks.",
    fields: [
      { key: "atr_period", label: "ATR period", type: "number" },
      { key: "atr_stop_multiplier", label: "ATR stop multiplier", type: "number",
        help: "Stop = entry − (multiplier × ATR)." },
    ],
  },
  {
    title: "Risk thresholds",
    fields: [
      { key: "max_position_pct", label: "Max position (% of wallet)", type: "number" },
      { key: "max_total_exposure_pct", label: "Max total exposure (%)", type: "number" },
      { key: "max_daily_loss_pct", label: "Max daily loss (%)", type: "number" },
      { key: "min_confidence", label: "Min model confidence", type: "number" },
      { key: "max_trades_per_day", label: "Max trades per day", type: "number" },
      { key: "min_order_notional_usdt", label: "Min order size (USDT)", type: "number" },
      { key: "volatility_sigma_limit", label: "Volatility sigma limit", type: "number" },
    ],
  },
  {
    title: "Schedule",
    note: "Changing these rebuilds the scheduler's triggers immediately — no restart.",
    fields: [
      { key: "trade_loop_interval_minutes", label: "Trade loop (minutes)", type: "number" },
      { key: "heartbeat_interval_seconds", label: "Heartbeat (seconds)", type: "number" },
      { key: "reconcile_interval_minutes", label: "Reconcile (minutes)", type: "number" },
      { key: "data_refresh_hour_utc", label: "Data refresh hour (UTC)", type: "number" },
    ],
  },
];

export default function SettingsPage() {
  const [data, setData] = useState<ConfigResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    const result = await getConfig();
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

  const current = (key: string): string => {
    if (key in drafts) return drafts[key];
    const value = data?.config[key];
    if (Array.isArray(value)) return value.join(", ");
    if (typeof value === "object" && value !== null) {
      return JSON.stringify(value, null, 0);
    }
    return value === undefined || value === null ? "" : String(value);
  };

  const save = async (spec: FieldSpec) => {
    const raw = current(spec.key);
    let value: unknown = raw;

    if (spec.type === "number") {
      value = Number(raw);
      if (Number.isNaN(value as number)) {
        setNotice(`${spec.label}: not a number.`);
        return;
      }
    } else if (spec.type === "list") {
      value = raw.split(",").map((s) => s.trim()).filter(Boolean);
    } else if (spec.key === "promotion_gate") {
      try {
        value = JSON.parse(raw);
      } catch {
        setNotice(`${spec.label}: invalid JSON.`);
        return;
      }
    }

    setNotice(`Saving ${spec.label}…`);
    const result = await updateConfig(spec.key, value);
    if (!result.ok) {
      setNotice(`${spec.label}: ${result.error}`);
      return;
    }
    const payload = result.data as { rescheduled?: boolean };
    setNotice(
      payload?.rescheduled
        ? `${spec.label} saved — scheduler rebuilt.`
        : `${spec.label} saved — active on next job run.`,
    );
    setDrafts((d) => {
      const next = { ...d };
      delete next[spec.key];
      return next;
    });
    await refresh();
  };

  return (
    <div className="min-h-screen bg-zinc-50 p-4 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100 sm:p-6">
      <div className="mx-auto max-w-4xl space-y-4">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-4">
            <h1 className="text-xl font-semibold">Settings</h1>
            <NavBar />
          </div>
          {notice && <span className="text-xs text-zinc-500">{notice}</span>}
        </header>

        {error && (
          <div className="rounded-lg border border-red-500 bg-red-500/10 p-4 text-sm">
            <span className="font-semibold text-red-600 dark:text-red-400">
              Could not load settings:
            </span>{" "}
            <span className="text-zinc-600 dark:text-zinc-400">{error}</span>
          </div>
        )}

        {data?.pin_is_default && (
          <div className="rounded-lg border border-amber-500 bg-amber-500/10 p-4 text-sm">
            <p className="font-semibold text-amber-700 dark:text-amber-400">
              Stage PIN is still the default (000000)
            </p>
            <p className="mt-1 text-zinc-600 dark:text-zinc-400">
              Change it below before live trading is ever enabled (§10).
            </p>
          </div>
        )}

        {GROUPS.map((group) => (
          <section
            key={group.title}
            className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900"
          >
            <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
              {group.title}
            </h2>
            {group.note && (
              <p className="mt-1 text-xs text-zinc-500">{group.note}</p>
            )}

            <div className="mt-3 space-y-3">
              {group.fields.map((spec) => (
                <div key={spec.key} className="flex flex-wrap items-end gap-2">
                  <label className="flex-1 min-w-[220px]">
                    <span className="block text-sm">{spec.label}</span>
                    {spec.help && (
                      <span className="block text-xs text-zinc-500">
                        {spec.help}
                      </span>
                    )}
                    <input
                      value={current(spec.key)}
                      onChange={(e) =>
                        setDrafts((d) => ({ ...d, [spec.key]: e.target.value }))
                      }
                      className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-950"
                    />
                  </label>
                  <button
                    onClick={() => void save(spec)}
                    disabled={!(spec.key in drafts)}
                    className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40"
                  >
                    Save
                  </button>
                </div>
              ))}
            </div>
          </section>
        ))}

        <PinPanel onDone={(message) => setNotice(message)} />

        <StageSwitch onChanged={() => void refresh()} />

      </div>
    </div>
  );
}

function PinPanel({ onDone }: { onDone: (message: string) => void }) {
  const [currentPin, setCurrentPin] = useState("");
  const [newPin, setNewPin] = useState("");
  const [confirmPin, setConfirmPin] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (newPin !== confirmPin) {
      onDone("New PIN and confirmation do not match.");
      return;
    }
    setBusy(true);
    const result = await changePin(currentPin, newPin);
    setBusy(false);

    if (!result.ok) {
      onDone(`PIN change failed: ${result.error}`);
      return;
    }
    setCurrentPin("");
    setNewPin("");
    setConfirmPin("");
    onDone("Stage PIN changed.");
  };

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        Stage PIN
      </h2>
      <p className="mt-1 text-xs text-zinc-500">
        The current PIN is required. It is verified server-side against a
        salted hash — the hash is never sent to this page.
      </p>

      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <label className="text-sm">
          Current PIN
          <input
            type="password"
            value={currentPin}
            onChange={(e) => setCurrentPin(e.target.value)}
            autoComplete="current-password"
            className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-950"
          />
        </label>
        <label className="text-sm">
          New PIN
          <input
            type="password"
            value={newPin}
            onChange={(e) => setNewPin(e.target.value)}
            autoComplete="new-password"
            className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-950"
          />
        </label>
        <label className="text-sm">
          Confirm new PIN
          <input
            type="password"
            value={confirmPin}
            onChange={(e) => setConfirmPin(e.target.value)}
            autoComplete="new-password"
            className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-950"
          />
        </label>
      </div>

      <button
        onClick={() => void submit()}
        disabled={busy || !currentPin || newPin.length < 4}
        className="mt-3 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40"
      >
        Change PIN
      </button>
    </section>
  );
}

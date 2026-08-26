"use client";

/**
 * Liquidate all holdings (§7 step 6).
 *
 * Visually and physically separate from the emergency stop: stopping and
 * selling are different decisions, and the button that sells everything at
 * market should not sit next to the one you press when worried.
 *
 * Two gates, both enforced server-side: the stage PIN, and an explicit
 * confirmation step that this component makes the operator pass through
 * rather than sending on the first click.
 */

import { useState } from "react";

import { liquidateAll } from "@/lib/api";

export default function LiquidatePanel({ onDone }: { onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [pin, setPin] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setResult(null);
    const response = await liquidateAll(pin, true);
    setBusy(false);
    setConfirming(false);

    if (!response.ok) {
      setResult(`Refused: ${response.error}`);
      return;
    }
    setPin("");
    setResult(
      `Sold ${response.data.placed_count} of ${response.data.attempted_count} holdings to USDT.`,
    );
    onDone();
  };

  return (
    <section className="rounded-lg border border-red-300 bg-white p-4 dark:border-red-900 dark:bg-zinc-900">
      <button
        onClick={() => setOpen(!open)}
        className="text-sm font-semibold uppercase tracking-wide text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
      >
        {open ? "▾" : "▸"} Liquidate all to USDT
      </button>

      {open && (
        <div className="mt-3">
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Sells every non-USDT holding at market price. This is separate
            from the emergency stop, which freezes without selling. Each sale
            goes through the normal order path, so all of them appear in the
            trade history.
          </p>

          <div className="mt-3 flex flex-wrap items-end gap-2">
            <label className="text-sm">
              <span className="block">Stage PIN</span>
              <input
                type="password"
                value={pin}
                onChange={(e) => setPin(e.target.value)}
                autoComplete="off"
                className="mt-1 rounded-md border border-zinc-300 bg-white px-2 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-950"
              />
            </label>

            {!confirming ? (
              <button
                onClick={() => setConfirming(true)}
                disabled={!pin || busy}
                className="rounded-md border border-red-500 px-4 py-2 text-sm font-semibold text-red-600 hover:bg-red-50 disabled:opacity-40 dark:hover:bg-red-950"
              >
                Liquidate all holdings
              </button>
            ) : (
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-red-600">
                  Are you sure? This sells all holdings at market price.
                </span>
                <button
                  onClick={() => void submit()}
                  disabled={busy}
                  className="rounded-md bg-red-600 px-4 py-2 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-40"
                >
                  Yes, sell everything
                </button>
                <button
                  onClick={() => setConfirming(false)}
                  className="rounded-md border border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700"
                >
                  Cancel
                </button>
              </div>
            )}
          </div>

          {result && <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">{result}</p>}
        </div>
      )}
    </section>
  );
}

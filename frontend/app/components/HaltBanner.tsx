"use client";

/**
 * Persistent halt banner and Resume (§7 step 5).
 *
 * Stays visible for as long as the halt is active — it is not dismissible,
 * because a halted system that looks normal is the failure this exists to
 * prevent. Resume takes the stage PIN and deliberately does NOT restart
 * trading; that is a second, separate act.
 */

import { useState } from "react";

import { resumeSystem, type SystemStatus } from "@/lib/api";

export default function HaltBanner({
  status,
  onResumed,
}: {
  status: SystemStatus;
  onResumed: () => void;
}) {
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!status.halted) return null;

  const submit = async () => {
    setBusy(true);
    setError(null);
    const result = await resumeSystem(pin);
    setBusy(false);

    if (!result.ok) {
      setError(result.error);
      return;
    }
    setPin("");
    onResumed();
  };

  const since = status.halted_at
    ? new Date(status.halted_at).toISOString().slice(0, 19).replace("T", " ") + "Z"
    : "unknown time";

  return (
    <div className="rounded-lg border-2 border-red-600 bg-red-600/10 p-4">
      <p className="text-lg font-bold text-red-700 dark:text-red-400">
        TRADING HALTED — Emergency stop active
      </p>
      <p className="mt-1 text-sm text-zinc-700 dark:text-zinc-300">
        Halted at <span className="font-mono">{since}</span>
        {status.halted_reason ? ` · ${status.halted_reason}` : ""}
      </p>
      <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
        Holdings were <strong>not</strong> sold. Stage{" "}
        <span className="font-medium">{status.stage}</span> is preserved and
        will be restored on resume. Use Liquidate below if you want to exit
        positions instead.
      </p>

      <div className="mt-3 flex flex-wrap items-end gap-2">
        <label className="text-sm">
          <span className="block">Stage PIN to resume</span>
          <input
            type="password"
            value={pin}
            onChange={(e) => setPin(e.target.value)}
            autoComplete="off"
            className="mt-1 rounded-md border border-zinc-300 bg-white px-2 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-950"
          />
        </label>
        <button
          onClick={() => void submit()}
          disabled={busy || !pin}
          className="rounded-md bg-zinc-800 px-4 py-2 text-sm font-semibold text-white hover:bg-zinc-900 disabled:opacity-40"
        >
          Resume
        </button>
        <span className="text-xs text-zinc-500">
          Resuming clears the halt but leaves trading stopped.
        </span>
      </div>

      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </div>
  );
}

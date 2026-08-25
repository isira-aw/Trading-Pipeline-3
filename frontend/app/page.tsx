"use client";

/**
 * Main dashboard (§8.1).
 *
 * Data arrives two ways: a polled snapshot for whole-state correctness, and
 * WebSocket events that trigger an immediate refresh of the affected panel.
 * Polling is the floor, not the mechanism — if the socket drops, the page
 * keeps showing true (if slightly older) data instead of silently freezing,
 * and the header says the live feed is down.
 *
 * The stage-progress widget from §8.1 is deliberately absent: the promotion
 * gate (step 12) does not exist yet, so there is nothing correct to show.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import AdvisoryPanel from "@/app/components/AdvisoryPanel";
import NavBar from "@/app/components/NavBar";
import PositionsTable from "@/app/components/PositionsTable";
import StatusStrip from "@/app/components/StatusStrip";
import TradesFeed from "@/app/components/TradesFeed";
import WalletPanel from "@/app/components/WalletPanel";
import {
  downloadData,
  emergencyStop,
  generateAdvisory,
  getAdvisories,
  getPositions,
  getStatus,
  getTrades,
  getWallet,
  startSystem,
  stopSystem,
  type AdvisoriesResponse,
  type Position,
  type SystemStatus,
  type Trade,
  type Wallet,
} from "@/lib/api";
import { createWsClient, type ConnectionState } from "@/lib/ws-client";

const POLL_INTERVAL_MS = 10000;

const STAGE_STYLES: Record<string, string> = {
  setup: "bg-zinc-500",
  paper: "bg-blue-600",
  live: "bg-emerald-600",
  halted: "bg-red-600",
};

export default function Dashboard() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [tradesError, setTradesError] = useState<string | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [positionsError, setPositionsError] = useState<string | null>(null);
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [walletError, setWalletError] = useState<string | null>(null);
  const [advisories, setAdvisories] = useState<AdvisoriesResponse | null>(null);
  const [advisoriesError, setAdvisoriesError] = useState<string | null>(null);
  const [wsState, setWsState] = useState<ConnectionState>("connecting");
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshStatus = useCallback(async () => {
    const result = await getStatus();
    if (result.ok) {
      setStatus(result.data);
      setStatusError(null);
    } else {
      setStatusError(result.error);
    }
  }, []);

  const refreshTrades = useCallback(async () => {
    const result = await getTrades();
    if (result.ok) {
      setTrades(result.data.trades);
      setTradesError(null);
    } else {
      setTradesError(result.error);
    }
  }, []);

  const refreshPositions = useCallback(async () => {
    const result = await getPositions();
    if (result.ok) {
      setPositions(result.data.positions);
      setPositionsError(null);
    } else {
      setPositionsError(result.error);
    }
  }, []);

  const refreshWallet = useCallback(async () => {
    const result = await getWallet();
    if (result.ok) {
      setWallet(result.data);
      setWalletError(null);
    } else {
      setWalletError(result.error);
    }
  }, []);

  const refreshAdvisories = useCallback(async () => {
    const result = await getAdvisories();
    if (result.ok) {
      setAdvisories(result.data);
      setAdvisoriesError(null);
    } else {
      setAdvisoriesError(result.error);
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([
      refreshStatus(),
      refreshTrades(),
      refreshPositions(),
      refreshWallet(),
      refreshAdvisories(),
    ]);
  }, [refreshStatus, refreshTrades, refreshPositions, refreshWallet, refreshAdvisories]);

  // Keep the latest refreshers reachable from the WebSocket callback without
  // tearing down and rebuilding the socket on every render.
  const handlers = useRef({ refreshAll, refreshStatus, refreshTrades, refreshPositions, refreshWallet, refreshAdvisories });
  handlers.current = { refreshAll, refreshStatus, refreshTrades, refreshPositions, refreshWallet, refreshAdvisories };

  useEffect(() => {
    void refreshAll();
    const timer = setInterval(() => void refreshAll(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [refreshAll]);

  useEffect(() => {
    const client = createWsClient({
      onStateChange: setWsState,
      onEvent: (event) => {
        switch (event.event) {
          case "trade_event":
            void handlers.current.refreshTrades();
            void handlers.current.refreshPositions();
            break;
          case "wallet_update":
            void handlers.current.refreshWallet();
            break;
          case "component_status_change":
            void handlers.current.refreshStatus();
            break;
          case "llm_advisory":
            void handlers.current.refreshAdvisories();
            break;
          case "training_progress": {
            const symbol = String(event.symbol ?? "");
            const phase = String(event.phase ?? "");
            setNotice(`Training ${symbol}: ${phase}`);
            break;
          }
          case "system_event":
            setNotice(String(event.message ?? ""));
            void handlers.current.refreshStatus();
            break;
        }
      },
    });
    return () => client.close();
  }, []);

  const runAction = async (action: () => Promise<unknown>, label: string) => {
    setBusy(true);
    setNotice(`${label}…`);
    try {
      await action();
      await refreshAll();
      setNotice(`${label} done.`);
    } finally {
      setBusy(false);
    }
  };

  const halted = status?.stage === "halted";
  const stage = status?.stage ?? "unknown";

  return (
    <div className="min-h-screen bg-zinc-50 p-4 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100 sm:p-6">
      <div className="mx-auto max-w-7xl space-y-4">
        {/* Header */}
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold">Trading Pipeline</h1>
            <NavBar />
            <span
              className={`rounded px-2 py-0.5 text-xs font-medium uppercase text-white ${
                STAGE_STYLES[stage] ?? "bg-zinc-500"
              }`}
            >
              {stage}
            </span>
            <span
              className="flex items-center gap-1.5 text-xs text-zinc-500"
              title={`WebSocket ${wsState}`}
            >
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  wsState === "open"
                    ? "bg-emerald-500"
                    : wsState === "connecting"
                      ? "bg-amber-500"
                      : "bg-red-500"
                }`}
              />
              live {wsState}
            </span>
          </div>

          {/* Emergency stop stays visible regardless of scroll (§8.1). */}
          <button
            onClick={() => void runAction(emergencyStop, "Emergency stop")}
            disabled={busy || halted}
            className="rounded-md bg-red-600 px-4 py-2 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50"
          >
            Emergency Stop
          </button>
        </header>

        {/* Halt banner (§7) */}
        {halted && (
          <div className="rounded-lg border border-red-500 bg-red-500/10 p-4">
            <p className="font-semibold text-red-600 dark:text-red-400">
              TRADING HALTED — Emergency stop active
            </p>
            <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
              Existing holdings were not liquidated. Resuming requires the stage
              PIN (not yet implemented — step 10).
            </p>
          </div>
        )}

        {/* Stuck orders (§1.7) */}
        {status && status.orders_needing_attention.length > 0 && (
          <div className="rounded-lg border border-amber-500 bg-amber-500/10 p-4">
            <p className="font-semibold text-amber-700 dark:text-amber-400">
              {status.orders_needing_attention.length} order(s) need attention
            </p>
            <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
              These could not be reconciled after the retry limit, so the
              position state may be wrong.
            </p>
            <ul className="mt-2 space-y-1 font-mono text-xs">
              {status.orders_needing_attention.map((order) => (
                <li key={order.trade_id}>
                  {order.trade_id.slice(0, 8)} · {order.side} {order.quantity}{" "}
                  {order.symbol} · {order.detail ?? "unknown error"}
                </li>
              ))}
            </ul>
          </div>
        )}

        {statusError && (
          <div className="rounded-lg border border-red-500 bg-red-500/10 p-4 text-sm">
            <span className="font-semibold text-red-600 dark:text-red-400">
              Backend unreachable:
            </span>{" "}
            <span className="text-zinc-600 dark:text-zinc-400">
              {statusError}
            </span>
          </div>
        )}

        {/* Controls (§8.1) */}
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => void runAction(startSystem, "Start system")}
            disabled={busy || halted || status?.trading_enabled}
            className="rounded-md bg-emerald-600 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            Start System
          </button>
          <button
            onClick={() => void runAction(stopSystem, "Stop system")}
            disabled={busy || !status?.trading_enabled}
            className="rounded-md bg-zinc-700 px-3 py-2 text-sm font-medium text-white hover:bg-zinc-800 disabled:opacity-50"
          >
            Stop
          </button>
          <button
            onClick={() => void runAction(generateAdvisory, "LLM advisory")}
            disabled={busy}
            className="rounded-md border border-zinc-300 px-3 py-2 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            Get Advisory
          </button>
          <button
            onClick={() => void runAction(downloadData, "Data download")}
            disabled={busy}
            className="rounded-md border border-zinc-300 px-3 py-2 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            Download Data
          </button>

          {status && !status.trading_allowed && (
            <span className="text-xs text-amber-600 dark:text-amber-500">
              {status.trading_blocked_reason}
            </span>
          )}
          {notice && (
            <span className="ml-auto text-xs text-zinc-500">{notice}</span>
          )}
        </div>

        {/* Component status strip (§8.1) */}
        {status && <StatusStrip components={status.components} />}

        {/* Panels */}
        <div className="grid gap-4 lg:grid-cols-3">
          <WalletPanel wallet={wallet} error={walletError} />
          <div className="lg:col-span-2">
            <PositionsTable positions={positions} error={positionsError} />
          </div>
        </div>

        <div className="grid gap-4 lg:grid-cols-3">
          <AdvisoryPanel data={advisories} error={advisoriesError} />
          <div className="lg:col-span-2">
            <TradesFeed trades={trades} error={tradesError} />
          </div>
        </div>

        <footer className="pt-2 text-xs text-zinc-500">
          {status?.scheduler_running
            ? `Scheduler running · ${status.jobs.length} jobs · next trade loop ${
                status.jobs.find((j) => j.id === "trade_loop")?.next_run_at ??
                "—"
              }`
            : "Scheduler not running"}
        </footer>
      </div>
    </div>
  );
}
